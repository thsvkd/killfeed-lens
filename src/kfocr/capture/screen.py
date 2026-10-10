from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Protocol

import numpy as np

from kfocr.capture.base import CaptureHealth, CaptureState
from kfocr.domain.models import Frame


class _Backend(Protocol):
    def grab(self) -> np.ndarray | None: ...

    def stop(self) -> None: ...


class _DXCamBackend:
    def __init__(self, monitor: int) -> None:
        try:
            import dxcam
        except ImportError as exc:
            raise RuntimeError(
                "Screen capture requires the Windows extra: pip install 'killfeed-lens[windows]'"
            ) from exc
        self._camera = dxcam.create(
            device_idx=0,
            output_idx=monitor,
            output_color="BGR",
            backend="dxgi",
        )

    def grab(self) -> np.ndarray | None:
        return self._camera.grab(new_frame_only=False)

    def stop(self) -> None:
        try:
            self._camera.stop()
        finally:
            release = getattr(self._camera, "release", None)
            if release is not None:
                release()


class ScreenSource:
    def __init__(
        self,
        monitor: int = 0,
        sample_fps: float = 12,
        *,
        black_frame_limit: int = 3,
        stale_frame_limit: int = 12,
        recovery_frame_count: int = 2,
        max_retries: int = 3,
        backend_factory: Callable[[int], _Backend] | None = None,
    ) -> None:
        if monitor < 0 or sample_fps <= 0:
            raise ValueError("monitor must be non-negative and sample_fps must be positive")
        if min(black_frame_limit, stale_frame_limit, recovery_frame_count) <= 0:
            raise ValueError("capture health thresholds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.monitor = monitor
        self.sample_fps = float(sample_fps)
        self.black_frame_limit = black_frame_limit
        self.stale_frame_limit = stale_frame_limit
        self.recovery_frame_count = recovery_frame_count
        self.max_retries = max_retries
        self._backend_factory = backend_factory or _DXCamBackend
        self._backend: _Backend | None = None
        self._running = False
        self._seq = 0
        self._expected_resolution: tuple[int, int] | None = None
        self._pending_resolution: tuple[int, int] | None = None
        self._black_count = 0
        self._stale_count = 0
        self._recovery_count = 0
        self.health = CaptureHealth(CaptureState.STOPPED)

    def start(self) -> None:
        self.stop()
        try:
            self._backend = self._backend_factory(self.monitor)
        except Exception as exc:
            self.health = CaptureHealth(
                CaptureState.ERROR, "backend_failure", str(exc), retry_count=0
            )
            raise RuntimeError(f"Unable to start screen capture backend: {exc}") from exc
        self._running = True
        self._seq = 0
        self._expected_resolution = None
        self._pending_resolution = None
        self._black_count = 0
        self._stale_count = 0
        self._recovery_count = 0
        self.health = CaptureHealth(CaptureState.HEALTHY)

    def frames(self) -> Iterator[Frame]:
        if not self._running or self._backend is None:
            raise RuntimeError("ScreenSource.start() must be called before frames()")
        interval_ns = round(1_000_000_000 / self.sample_fps)
        next_capture_ns = time.monotonic_ns()

        while self._running and self._backend is not None:
            now_ns = time.monotonic_ns()
            if now_ns < next_capture_ns:
                time.sleep((next_capture_ns - now_ns) / 1_000_000_000)
            next_capture_ns = max(next_capture_ns + interval_ns, time.monotonic_ns())
            try:
                image = self._backend.grab()
            except StopIteration:
                self._retry_backend(RuntimeError("capture backend stopped unexpectedly"))
                continue
            except Exception as exc:
                self._retry_backend(exc)
                continue

            captured_at_ns = time.monotonic_ns()
            if image is None:
                self._stale_count += 1
                if self._stale_count >= self.stale_frame_limit:
                    detail = "capture backend returned no current frame"
                    self._degrade("stale_frames", detail, self._stale_count)
                    self._retry_backend(RuntimeError(detail), reason="stale_frames")
                    self._stale_count = 0
                continue
            self._stale_count = 0
            if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
                detail = "capture backend returned an invalid BGR frame"
                self._degrade("backend_failure", detail, 1)
                self._retry_backend(RuntimeError(detail))
                continue
            height, width = image.shape[:2]
            if width <= 0 or height <= 0:
                detail = f"capture backend returned invalid resolution {width}x{height}"
                self._degrade("invalid_resolution", detail, 1)
                self._retry_backend(RuntimeError(detail), reason="invalid_resolution")
                continue

            resolution = (width, height)
            if self._expected_resolution is None:
                self._expected_resolution = resolution
            elif self._pending_resolution is not None:
                if resolution != self._pending_resolution:
                    self._pending_resolution = resolution
                    detail = f"capture resolution changed again to {resolution}"
                    self._degrade("resolution_changed", detail, 1)
                    self._retry_backend(RuntimeError(detail), reason="resolution_changed")
                    continue
                self._expected_resolution = resolution
                self._pending_resolution = None
            elif resolution != self._expected_resolution:
                self._pending_resolution = resolution
                self._degrade(
                    "resolution_changed",
                    f"expected {self._expected_resolution}, received {resolution}",
                    1,
                )
                self._retry_backend(
                    RuntimeError("capture resolution changed"), reason="resolution_changed"
                )
                continue

            if not np.any(image):
                self._black_count += 1
                if self._black_count >= self.black_frame_limit:
                    detail = "capture returned consecutive black frames"
                    self._degrade("black_frames", detail, self._black_count)
                    self._retry_backend(RuntimeError(detail), reason="black_frames")
                    self._black_count = 0
                continue
            self._black_count = 0

            if not self._record_good_frame(captured_at_ns, resolution):
                continue
            yield Frame(
                source_id=f"screen:{self.monitor}",
                seq=self._seq,
                captured_at_ns=captured_at_ns,
                image_bgr=image,
                width=width,
                height=height,
            )
            self._seq += 1

    def stop(self) -> None:
        preserve_error = self.health.state is CaptureState.ERROR
        self._running = False
        if self._backend is not None:
            try:
                self._backend.stop()
            finally:
                self._backend = None
        if not preserve_error:
            self.health = CaptureHealth(CaptureState.STOPPED)

    def _degrade(self, reason: str, detail: str, bad_count: int) -> None:
        self._recovery_count = 0
        self.health = CaptureHealth(
            CaptureState.DEGRADED,
            reason,
            detail,
            consecutive_bad_frames=bad_count,
            retry_count=self.health.retry_count,
            resolution=self._expected_resolution,
            last_frame_at_ns=self.health.last_frame_at_ns,
        )

    def _record_good_frame(self, captured_at_ns: int, resolution: tuple[int, int]) -> bool:
        if self.health.state is CaptureState.DEGRADED:
            self._recovery_count += 1
            if self._recovery_count < self.recovery_frame_count:
                self.health = CaptureHealth(
                    CaptureState.DEGRADED,
                    self.health.reason,
                    self.health.detail,
                    retry_count=self.health.retry_count,
                    resolution=resolution,
                    last_frame_at_ns=captured_at_ns,
                )
                return False
        self._recovery_count = 0
        self.health = CaptureHealth(
            CaptureState.HEALTHY,
            retry_count=self.health.retry_count,
            resolution=resolution,
            last_frame_at_ns=captured_at_ns,
        )
        return True

    def _retry_backend(self, error: Exception, *, reason: str = "backend_failure") -> None:
        retries = self.health.retry_count
        if self._backend is not None:
            try:
                self._backend.stop()
            except Exception as exc:
                error = exc
            finally:
                self._backend = None
        while retries < self.max_retries:
            retries += 1
            try:
                self._backend = self._backend_factory(self.monitor)
            except Exception as exc:
                error = exc
                continue
            self.health = CaptureHealth(
                CaptureState.DEGRADED,
                reason,
                str(error),
                retry_count=retries,
                resolution=self._expected_resolution,
                last_frame_at_ns=self.health.last_frame_at_ns,
            )
            return
        self.health = CaptureHealth(
            CaptureState.ERROR,
            reason,
            str(error),
            retry_count=retries,
            resolution=self._expected_resolution,
            last_frame_at_ns=self.health.last_frame_at_ns,
        )
        raise RuntimeError(
            f"Screen capture {reason} after {retries} reconnect attempts: {error}"
        ) from error
