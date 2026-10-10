from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import cv2

from kfocr.domain.models import Frame


class ReplaySource:
    def __init__(
        self,
        path: str | Path,
        sample_fps: float = 12,
        realtime: bool = False,
    ) -> None:
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self.path = Path(path)
        self.sample_fps = float(sample_fps)
        self.realtime = realtime
        self._capture: cv2.VideoCapture | None = None
        self._running = False

    def start(self) -> None:
        self.stop()
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"Unable to open replay video: {self.path}")
        self._capture = capture
        self._running = True

    def frames(self) -> Iterator[Frame]:
        if not self._running or self._capture is None:
            raise RuntimeError("ReplaySource.start() must be called before frames()")

        capture = self._capture
        source_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if source_fps <= 0:
            raise RuntimeError(f"Replay video has no usable frame rate: {self.path}")
        effective_sample_fps = min(self.sample_fps, source_fps)
        source_index = 0
        output_seq = 0
        next_source_index = 0
        first_wall_ns = time.monotonic_ns()

        while self._running:
            ok, image = capture.read()
            if not ok:
                break
            if source_index == next_source_index:
                timeline_ns = round(source_index * 1_000_000_000 / source_fps)
                if self.realtime:
                    remaining_ns = first_wall_ns + timeline_ns - time.monotonic_ns()
                    if remaining_ns > 0:
                        time.sleep(remaining_ns / 1_000_000_000)
                height, width = image.shape[:2]
                yield Frame(
                    source_id=str(self.path),
                    seq=output_seq,
                    captured_at_ns=timeline_ns,
                    image_bgr=image,
                    width=width,
                    height=height,
                )
                output_seq += 1
                candidate = round(output_seq * source_fps / effective_sample_fps)
                next_source_index = max(source_index + 1, candidate)
            source_index += 1

    def stop(self) -> None:
        self._running = False
        if self._capture is not None:
            self._capture.release()
            self._capture = None
