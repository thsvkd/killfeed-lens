from datetime import UTC, datetime
from uuid import UUID

import numpy as np
import pytest

from kfocr.config import AppConfig
from kfocr.domain.models import Frame, NormalizedRect, OCRToken, VisualElement, VisualKind


class Frames:
    def __init__(self, count=40):
        self.count = count
        self.stopped = False

    def start(self):
        pass

    def frames(self):
        for seq in range(self.count):
            image = np.full((100, 320, 3), 40, dtype=np.uint8)
            image[10:30, 20:70] = 255
            image[10:30, 130:190] = 255
            yield Frame("synthetic", seq, seq * 100_000_000, image, 320, 100)

    def stop(self):
        self.stopped = True


class Text:
    def __init__(self, fail_first=False, fail_calls=()):
        self.calls = 0
        self.fail_calls = {1} if fail_first else set(fail_calls)

    def recognize(self, image):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("test inference failure")
        return [OCRToken("AAAA", 0.99, (20, 10, 70, 30)),
                OCRToken("BBBB", 0.99, (130, 10, 190, 30))]


class Icons:
    def detect(self, image):
        return [VisualElement(VisualKind.ICON, "kill_marker", 0.99, (85, 10, 115, 30))]


class Sink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def setup(tmp_path):
    config = AppConfig()
    config.capture.sample_fps = 10
    config.roi.rect = NormalizedRect(0, 0, 1, 1)
    config.storage.sqlite_path = str(tmp_path / "events.db")
    config.debug.artifact_dir = str(tmp_path / "artifacts")
    return config


def test_persistent_video_rows_store_once_and_complete_session(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    config, source, text, sink = setup(tmp_path), Frames(), Text(), Sink()
    session = Pipeline(config, source, recognizer=text, icons=Icons(), sink=sink,
                       started_at_utc=datetime(2026, 10, 1, tzinfo=UTC)).run()
    store = EventStore(config.storage.sqlite_path)
    events = store.list_events(session.session_id)
    assert len(events) == len(sink.events) == 1
    assert (events[0].killer_name, events[0].victim_name) == ("AAAA", "BBBB")
    assert session.status == "completed"
    assert store.get_session(session.session_id).status == "completed"
    assert source.stopped
    assert 2 <= text.calls <= 16  # 4 seconds, cap 4 FPS; periodic refresh still runs.
    assert not (tmp_path / "artifacts").exists()
    assert session.metadata["performance"]["confirmed_events"] == 1
    assert isinstance(session.session_id, UUID)
    store.close()


def test_ocr_error_drops_inference_and_recovers(tmp_path):
    from kfocr.pipeline import Pipeline

    sink = Sink()
    session = Pipeline(setup(tmp_path), Frames(), recognizer=Text(True),
                       icons=Icons(), sink=sink).run()
    assert len(sink.events) == 1
    assert session.metadata["performance"]["ocr_errors"] == 1


def test_db_failure_is_bounded_and_session_aborts(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    config = setup(tmp_path)

    class FailingStore(EventStore):
        def append_event(self, event):
            raise OSError("disk full")

    store = FailingStore(config.storage.sqlite_path)
    source = Frames(100)
    with pytest.raises(RuntimeError, match="persist"):
        Pipeline(config, source, recognizer=Text(), icons=Icons(), sink=Sink(),
                 store=store).run()
    assert source.stopped
    assert store.list_sessions()[0].status == "aborted"
    store.close()


def test_multisink_console_uses_session_relative_time(tmp_path):
    import io

    from kfocr.pipeline import Pipeline
    from kfocr.ui.sidecar import ConsoleSink, MultiSink

    class MonotonicFrames(Frames):
        def frames(self):
            from dataclasses import replace

            for frame in super().frames():
                yield replace(frame, captured_at_ns=frame.captured_at_ns + 900_000_000_000)

    output = io.StringIO()
    Pipeline(setup(tmp_path), MonotonicFrames(), recognizer=Text(), icons=Icons(),
             sink=MultiSink(ConsoleSink(stream=output), Sink())).run()
    assert output.getvalue().startswith("00:00.000")


def test_black_video_never_emits_rows_even_if_ocr_would_hallucinate(tmp_path):
    from kfocr.pipeline import Pipeline

    class BlackFrames(Frames):
        def frames(self):
            for seq in range(self.count):
                image = np.zeros((100, 320, 3), dtype=np.uint8)
                yield Frame("black", seq, seq * 100_000_000, image, 320, 100)

    text, sink = Text(), Sink()
    session = Pipeline(setup(tmp_path), BlackFrames(), recognizer=text, icons=Icons(), sink=sink).run()
    assert sink.events == []
    assert text.calls == 0
    assert session.metadata["performance"]["black_frames"] == 40


def test_failed_historical_replay_ends_on_video_timeline(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    class InterruptedFrames(Frames):
        def frames(self):
            for frame in super().frames():
                if frame.seq == 12:
                    raise RuntimeError("corrupt video")
                yield frame

    config = setup(tmp_path)
    old_start = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="corrupt video"):
        Pipeline(config, InterruptedFrames(), recognizer=Text(), icons=Icons(), sink=Sink(),
                 started_at_utc=old_start).run()
    store = EventStore(config.storage.sqlite_path)
    session = store.list_sessions()[0]
    assert session.status == "aborted"
    assert (session.ended_at_utc - old_start).total_seconds() == 1.1
    store.close()


def test_ocr_failures_do_not_reemit_persistent_row(tmp_path):
    from kfocr.pipeline import Pipeline

    sink = Sink()
    # Two failures (800ms, 1200ms) leave a gap longer than max_gap since the last success.
    session = Pipeline(setup(tmp_path), Frames(), recognizer=Text(fail_calls=(3, 4)),
                       icons=Icons(), sink=sink).run()
    assert session.metadata["performance"]["ocr_errors"] == 2
    assert len(sink.events) == 1


@pytest.mark.parametrize("feed_fails", [False, True])
def test_hud_count_reaches_gui_and_jsonl_even_with_static_or_failing_killfeed(tmp_path, feed_fails):
    import io
    import queue

    from kfocr.pipeline import Pipeline
    from kfocr.ui.gui import GuiSink, event_from_row
    from kfocr.ui.sidecar import JsonlSink, MultiSink
    from kfocr.ui.teams import Ledger

    class HUDText(Text):
        def __init__(self):
            super().__init__(fail_calls=range(1, 100) if feed_fails else ())
            self.digits = iter(["96", "96", "95", "95", "96", "96"] + ["96"] * 30)

        def read_lines(self, images):
            return [(next(self.digits), 0.99)]

    config = setup(tmp_path)
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    events = queue.Queue()
    output = tmp_path / "events.jsonl"
    jsonl = JsonlSink(output)
    try:
        session = Pipeline(config, Frames(), recognizer=HUDText(), icons=Icons(),
                           sink=MultiSink(GuiSink(events), jsonl)).run()
    finally:
        jsonl.close()
    # Exercise the same mixed stream consumer as `gui --jsonl`.
    import json

    ledger, counts = Ledger(teams=25), []
    for line in io.StringIO(output.read_text(encoding="utf-8")):
        record = event_from_row(json.loads(line))
        from kfocr.domain.models import AliveCount

        if isinstance(record, AliveCount):
            counts.append(record.count)
            ledger.observe_alive_count(record)
        else:
            ledger.apply(record)
    assert counts[:3] == [96, 95, 96]
    assert ledger.players_left == 96
    assert len(ledger.recent) == (0 if feed_fails else 1)
    assert events.qsize() == len(output.read_text(encoding="utf-8").splitlines())
    assert session.metadata["first_observed_alive"] == 96
    assert session.metadata["last_observed_alive"] == 96


def test_hud_ocr_failures_do_not_suppress_killfeed_events(tmp_path):
    from kfocr.pipeline import Pipeline

    class BrokenHUD(Text):
        def read_lines(self, images):
            raise RuntimeError("HUD recognition failed")

    config, sink = setup(tmp_path), Sink()
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    session = Pipeline(config, Frames(), recognizer=BrokenHUD(), icons=Icons(), sink=sink).run()
    assert len(sink.events) == 1
    assert session.metadata["performance"]["alive_ocr_errors"] > 0
    assert session.metadata["performance"]["ocr_errors"] == 0
    assert "first_observed_alive" not in session.metadata
