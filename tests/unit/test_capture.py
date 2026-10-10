from __future__ import annotations

import cv2
import numpy as np
import pytest

from kfocr.capture.base import CaptureState
from kfocr.capture.replay import ReplaySource
from kfocr.capture.screen import ScreenSource, _DXCamBackend


def _write_video(path, fps: float = 6.0, frame_count: int = 6) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (32, 24)
    )
    assert writer.isOpened()
    for index in range(frame_count):
        writer.write(np.full((24, 32, 3), index * 30, dtype=np.uint8))
    writer.release()


def test_replay_max_speed_preserves_source_timeline_while_sampling(tmp_path):
    path = tmp_path / "timeline.avi"
    _write_video(path)
    source = ReplaySource(path, sample_fps=3, realtime=False)

    source.start()
    frames = list(source.frames())
    source.stop()

    assert [frame.seq for frame in frames] == [0, 1, 2]
    assert [frame.captured_at_ns for frame in frames] == [0, 333_333_333, 666_666_667]
    assert [round(float(frame.image_bgr.mean()) / 30) for frame in frames] == [0, 2, 4]


def test_replay_rejects_missing_or_unreadable_input(tmp_path):
    source = ReplaySource(tmp_path / "missing.mp4")

    with pytest.raises(RuntimeError, match="Unable to open replay video"):
        source.start()


class _FakeBackend:
    def __init__(self, results):
        self.results = iter(results)
        self.stopped = False

    def grab(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result

    def stop(self):
        self.stopped = True


def test_dxcam_backend_requests_latest_frame_when_desktop_pixels_are_unchanged():
    expected = np.full((8, 12, 3), 42, dtype=np.uint8)

    class Camera:
        def grab(self, *, new_frame_only):
            assert new_frame_only is False
            return expected

    backend = _DXCamBackend.__new__(_DXCamBackend)
    backend._camera = Camera()

    assert backend.grab() is expected


def test_screen_source_does_not_yield_black_frames_and_exposes_degraded_health():
    black = np.zeros((8, 12, 3), dtype=np.uint8)
    good = np.full((8, 12, 3), 100, dtype=np.uint8)
    backend = _FakeBackend([black, black, black, good, good])
    source = ScreenSource(
        sample_fps=1000,
        black_frame_limit=3,
        recovery_frame_count=2,
        backend_factory=lambda monitor: backend,
    )

    source.start()
    iterator = source.frames()
    recovered = next(iterator)

    assert np.array_equal(recovered.image_bgr, good)
    assert source.health.state is CaptureState.HEALTHY
    assert recovered.seq == 0
    source.stop()


def test_screen_source_exhausts_bounded_retries_when_black_capture_never_recovers():
    black = np.zeros((8, 12, 3), dtype=np.uint8)
    backends = [_FakeBackend([black, black]) for _ in range(2)]
    source = ScreenSource(
        sample_fps=1000,
        black_frame_limit=2,
        max_retries=1,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    with pytest.raises(RuntimeError, match="black_frames"):
        list(source.frames())

    assert source.health.state is CaptureState.ERROR
    assert source.health.reason == "black_frames"
    assert source.health.retry_count == 1
    source.stop()
    assert source.health.state is CaptureState.ERROR


def test_screen_source_emits_identical_valid_frames_without_marking_them_stale():
    frame = np.full((8, 12, 3), 50, dtype=np.uint8)
    backend = _FakeBackend([frame, frame.copy(), frame.copy()])
    source = ScreenSource(
        sample_fps=1000,
        stale_frame_limit=2,
        backend_factory=lambda monitor: backend,
    )

    source.start()
    iterator = source.frames()
    assert [next(iterator).seq for _ in range(3)] == [0, 1, 2]
    assert source.health.state is CaptureState.HEALTHY
    source.stop()


def test_screen_source_treats_missing_backend_frames_as_bounded_stale_failure():
    backends = [_FakeBackend([None, None]) for _ in range(2)]
    source = ScreenSource(
        sample_fps=1000,
        stale_frame_limit=2,
        max_retries=1,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    with pytest.raises(RuntimeError, match="stale_frames"):
        list(source.frames())

    assert source.health.state is CaptureState.ERROR
    assert source.health.retry_count == 1


def test_screen_source_retries_invalid_dimensions_and_fails_loudly():
    invalid = np.empty((0, 12, 3), dtype=np.uint8)
    backends = [_FakeBackend([invalid]) for _ in range(2)]
    source = ScreenSource(
        sample_fps=1000,
        max_retries=1,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    with pytest.raises(RuntimeError, match="invalid_resolution"):
        list(source.frames())

    assert source.health.state is CaptureState.ERROR


def test_screen_source_uses_bounded_backend_retries_and_reports_error():
    backends = [_FakeBackend([RuntimeError("lost")]) for _ in range(3)]
    source = ScreenSource(
        sample_fps=1000,
        max_retries=2,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    with pytest.raises(RuntimeError, match="backend_failure"):
        list(source.frames())

    assert source.health.state is CaptureState.ERROR
    assert source.health.reason == "backend_failure"
    assert source.health.retry_count == 2


def test_screen_source_treats_backend_iterator_termination_as_failure():
    backends = [_FakeBackend([]) for _ in range(2)]
    source = ScreenSource(
        sample_fps=1000,
        max_retries=1,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    with pytest.raises(RuntimeError, match="backend_failure"):
        list(source.frames())

    assert source.health.state is CaptureState.ERROR
    assert source.health.retry_count == 1


def test_screen_source_skips_abrupt_resolution_change_until_backend_recovers():
    original = np.full((8, 12, 3), 10, dtype=np.uint8)
    resized = np.full((10, 16, 3), 20, dtype=np.uint8)
    replacement = np.full((10, 16, 3), 30, dtype=np.uint8)
    backends = [_FakeBackend([original, resized]), _FakeBackend([replacement, replacement.copy()])]
    source = ScreenSource(
        sample_fps=1000,
        recovery_frame_count=2,
        backend_factory=lambda monitor: backends.pop(0),
    )

    source.start()
    iterator = source.frames()
    assert next(iterator).width == 12
    recovered = next(iterator)

    assert recovered.width == 16
    assert recovered.height == 10
    assert int(recovered.image_bgr[0, 0, 0]) == 30
    assert source.health.state is CaptureState.HEALTHY
    assert source.health.resolution == (16, 10)
    assert recovered.seq == 1
    source.stop()
