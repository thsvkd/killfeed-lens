from datetime import UTC, datetime, timedelta
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
    """Stands in for the per-frame name reader (and, with read_lines, the HUD recognizer)."""

    def __init__(self, fail_first=False, fail_calls=()):
        self.calls = 0
        self.fail_calls = {1} if fail_first else set(fail_calls)

    def read(self, image, icons):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("test inference failure")
        return [OCRToken("AAAA", 0.99, (20, 10, 70, 30)),
                OCRToken("BBBB", 0.99, (130, 10, 190, 30))], []


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
    session = Pipeline(config, source, recognizer=text, reader=text, icons=Icons(), sink=sink,
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


def test_live_capture_grabs_only_the_roi_and_keeps_screen_coordinates(tmp_path):
    import cv2

    from kfocr.capture.screen import ScreenSource
    from kfocr.pipeline import Pipeline

    screen = np.full((200, 640, 3), 90, dtype=np.uint8)
    screen[0:100, 320:640] = next(Frames(1).frames()).image_bgr  # killfeed in the top-right

    class Backend:
        resolution, boxes = (640, 200), []

        def grab(self, region=None):
            self.boxes.append(region)
            left, top, right, bottom = region
            return screen[top:bottom, left:right].copy()

        def stop(self):
            pass

    backend = Backend()
    config = setup(tmp_path)
    config.roi.rect = NormalizedRect(0.5, 0, 0.5, 0.5)
    config.debug.save_event_crops = True
    sink = Sink()
    source = ScreenSource(sample_fps=50, backend_factory=lambda monitor: backend)
    session = Pipeline(config, source, recognizer=(r := Text()), reader=r, icons=Icons(), sink=sink).run(max_frames=30)

    assert set(backend.boxes) == {(320, 0, 640, 100)}  # never the whole screen
    assert session.source_resolution == (640, 200)
    (event,) = sink.events
    assert event.source_bbox == (340, 10, 510, 30)  # ROI-relative tokens moved onto the screen
    crop = cv2.imread(event.source_crop_ref)
    assert crop.shape == (20, 170, 3) and np.array_equal(crop, screen[10:30, 340:510])


def test_ocr_error_drops_inference_and_recovers(tmp_path):
    from kfocr.pipeline import Pipeline

    sink = Sink()
    session = Pipeline(setup(tmp_path), Frames(), recognizer=(r := Text(True)), reader=r,
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
        Pipeline(config, source, recognizer=(r := Text()), reader=r, icons=Icons(), sink=Sink(),
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
    Pipeline(setup(tmp_path), MonotonicFrames(), recognizer=(r := Text()), reader=r, icons=Icons(),
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
    session = Pipeline(setup(tmp_path), BlackFrames(), recognizer=text, reader=text, icons=Icons(), sink=sink).run()
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
        Pipeline(config, InterruptedFrames(), recognizer=(r := Text()), reader=r, icons=Icons(), sink=Sink(),
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
    session = Pipeline(setup(tmp_path), Frames(), recognizer=(r := Text(fail_calls=(3, 4))), reader=r,
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
        session = Pipeline(config, Frames(), recognizer=(r := HUDText()), reader=r, icons=Icons(),
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
    session = Pipeline(config, Frames(), recognizer=(r := BrokenHUD()), reader=r, icons=Icons(), sink=sink).run()
    assert len(sink.events) == 1
    assert session.metadata["performance"]["alive_ocr_errors"] > 0
    assert session.metadata["performance"]["ocr_errors"] == 0
    assert "first_observed_alive" not in session.metadata


class Match(Frames):
    """(start_s, frames, hud) spans 100 ms a frame; hud 0 = gone, 1 = first count, 2 = back."""

    def __init__(self, spans):
        super().__init__()
        self.spans = spans

    def frames(self):
        seq = 0
        for start_s, count, shown in self.spans:
            for i in range(count):
                image = np.full((100, 320, 3), 40, dtype=np.uint8)
                image[10:30, 20:70] = 255
                image[10:30, 130:190] = 255
                image[0:20, 256:320] = 41 + shown  # what HUDText answers for this frame
                ns = int(start_s * 1e9) + i * 100_000_000
                yield Frame("synthetic", seq, ns, image, 320, 100)
                seq += 1


class HUDText(Text):
    def __init__(self, counts):
        super().__init__()
        self.counts = counts  # pixel value -> text read

    def read_lines(self, images):
        return [(self.counts.get(int(images[0][0, 0, 0]), ""), 0.99)]


@pytest.mark.parametrize("back_s, back, states", [
    (70, "88", ["ended", "new"]),  # gone a minute (lobby, island), back with more alive
    (70, "38", ["ended", "playing"]),  # gone a minute (alt-tab), fewer alive: the same match
    (250, "38", ["ended", "new"]),  # gone longer than any alt-tab: a new match after all
])
def test_hud_count_back_after_a_long_absence_judges_a_new_match(tmp_path, back_s, back, states):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    start = datetime(2026, 10, 1, tzinfo=UTC)
    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "40", 43: back})
    source = Match([(0, 20, 1), (10, 10, 0), (60, 10, 0), (back_s, 20, 2)])
    last = Pipeline(config, source, recognizer=text, reader=text, icons=Icons(), sink=sink,
                    started_at_utc=start).run()
    store = EventStore(config.storage.sqlite_path)
    found = sorted(store.list_sessions(), key=lambda s: s.started_at_utc)
    assert sink.states == states and all(s.status == "completed" for s in found)
    assert len(found) == (2 if states[-1] == "new" else 1)
    if len(found) == 2:
        first, second = found
        assert first.ended_at_utc == start + timedelta(seconds=1.5)  # the last count seen
        assert second.session_id == last.session_id
        assert (second.started_at_utc - start).total_seconds() >= back_s
        assert len(store.list_events(second.session_id)) == 1  # its own tracker reads the feed afresh
        assert (first.metadata["last_observed_alive"], last.metadata["first_observed_alive"]) == (40, int(back))
    store.close()


@pytest.mark.parametrize("back_s, back, states", [
    (200, "38", ["playing"]),  # the same match read poorly for minutes: no time rule here
    (60, "45", ["playing"]),  # a recall wave
])
def test_a_count_back_after_digits_without_one_needs_a_rise_no_recall_makes(tmp_path, back_s, back,
                                                                           states):
    from kfocr.pipeline import Pipeline

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "40", 43: back, 44: "4O"})  # "4O": digits but no count
    spans = [(0, 20, 1), *((s, 10, 3) for s in range(10, back_s, 30)), (back_s, 20, 2)]
    Pipeline(config, Match(spans), recognizer=text, reader=text, icons=Icons(), sink=sink,
             started_at_utc=datetime(2026, 10, 1, tzinfo=UTC)).run()
    assert sink.states == states


def test_digits_in_the_box_between_matches_do_not_merge_them(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    start = datetime(2026, 10, 1, tzinfo=UTC)
    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    # 40 alive, then a lobby whose "12,345" reads as the HUD shown but no count (never gone
    # 45 s), then the next match at 95
    text = HUDText({41: "", 42: "40", 43: "95", 44: "12,345"})
    source = Match([(0, 20, 1), (10, 10, 3), (40, 10, 3), (70, 10, 3), (90, 20, 2)])
    last = Pipeline(config, source, recognizer=text, reader=text, icons=Icons(), sink=sink,
                    started_at_utc=start).run()
    store = EventStore(config.storage.sqlite_path)
    first, second = sorted(store.list_sessions(), key=lambda s: s.started_at_utc)
    assert sink.states == ["new"] and second.session_id == last.session_id
    assert first.ended_at_utc == start + timedelta(seconds=1.5)  # the last count seen
    assert last.metadata["first_observed_alive"] == 95
    store.close()


def test_one_misread_rise_after_an_absence_does_not_start_a_new_match(tmp_path):
    from kfocr.pipeline import Pipeline

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "40", 43: "38", 44: "48"})  # 38 misread "48" for a second
    source = Match([(0, 20, 1), (10, 10, 0), (60, 10, 0), (70, 10, 3), (71, 60, 2)])
    Pipeline(config, source, recognizer=text, reader=text, icons=Icons(), sink=sink,
             started_at_utc=datetime(2026, 10, 1, tzinfo=UTC)).run()
    assert sink.states == ["ended", "playing"]


def test_a_new_match_after_a_short_gap_is_judged_late(tmp_path):
    from kfocr.pipeline import Pipeline

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    start = datetime(2026, 10, 1, tzinfo=UTC)
    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "40", 43: "95"})
    # 40 alive, gone 48 s (ended), then a new match at 95: a rise the HUD reader holds back until
    # its last count (40 at 1.5 s) is a minute old, so the new match opens at about 62 s, not 50 s
    source = Match([(0, 20, 1), (10, 10, 0), (47, 10, 0), (50, 200, 2)])
    last = Pipeline(config, source, recognizer=text, reader=text, icons=Icons(), sink=sink,
                    started_at_utc=start).run()
    assert sink.states == ["ended", "new"]
    assert last.metadata["first_observed_alive"] == 95
    assert 60 <= (last.started_at_utc - start).total_seconds() <= 63


def test_a_run_closed_in_the_lobby_ends_its_session_with_the_match(tmp_path):
    from kfocr.pipeline import Pipeline

    start = datetime(2026, 10, 1, tzinfo=UTC)
    config = setup(tmp_path)
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "40"})
    session = Pipeline(config, Match([(0, 20, 1), (60, 10, 0)]), recognizer=text, reader=text,
                       icons=Icons(), sink=Sink(), started_at_utc=start).run()
    assert session.ended_at_utc == start + timedelta(seconds=1.5)  # the last count, not 60.9 s


def test_a_capture_started_in_the_lobby_clears_the_board_when_the_match_starts(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    class Matches(Sink):
        def __init__(self):
            super().__init__()
            self.states = []

        def publish_match(self, state):
            self.states.append(state)

    config, sink = setup(tmp_path), Matches()
    config.roi.rect = NormalizedRect(0, 0, 0.7, 1)
    config.roi.alive_rect = NormalizedRect(0.8, 0, 0.2, 0.2)
    text = HUDText({41: "", 42: "90"})
    Pipeline(config, Match([(0, 10, 0), (50, 20, 1)]), recognizer=text, reader=text, icons=Icons(),
             sink=sink).run()
    assert sink.states == ["new"]  # the board may hold the last match
    store = EventStore(config.storage.sqlite_path)
    lobby, match = sorted(store.list_sessions(), key=lambda s: s.started_at_utc)
    assert lobby.ended_at_utc <= match.started_at_utc  # the match's session starts with it
    assert (match.started_at_utc - lobby.started_at_utc).total_seconds() >= 50
    store.close()


def test_a_badge_read_after_the_event_went_out_reaches_the_db_and_the_board(tmp_path):
    from kfocr.pipeline import Pipeline
    from kfocr.storage.db import EventStore

    class LateBadge(Text):
        def read(self, image, icons):
            tokens, _ = super().read(image, icons)
            badges = [VisualElement(VisualKind.BADGE, "3", 0.99, (5, 10, 15, 30))]
            if self.calls > 6:  # the victim's badge (right of its name) reads only after it went out
                badges.append(VisualElement(VisualKind.BADGE, "8", 0.99, (195, 10, 205, 30)))
            return tokens, badges

    class Amended(Sink):
        def __init__(self):
            super().__init__()
            self.amended = []

        def publish_amended(self, event):
            self.amended.append(event)

    config, sink, text = setup(tmp_path), Amended(), LateBadge()
    session = Pipeline(config, Frames(), recognizer=text, reader=text, icons=Icons(), sink=sink).run()
    store = EventStore(config.storage.sqlite_path)
    (stored,) = store.list_events(session.session_id)
    assert sink.events[0].victim_team is None and stored.victim_team == 8
    assert [(e.event_id, e.victim_team) for e in sink.amended] == [(stored.event_id, 8)]
    assert session.metadata["performance"]["amended_events"] == 1
    store.close()


def test_failing_reads_of_a_pending_row_fall_back_to_the_refresh_pace(tmp_path):
    from kfocr.pipeline import Pipeline

    # The first read sees a new row (pending), every later read fails: an unchanged feed is
    # then read only at the refresh pace, not on every frame the cap allows.
    text = Text(fail_calls=range(2, 100))
    session = Pipeline(setup(tmp_path), Frames(), recognizer=text, reader=text, icons=Icons(), sink=Sink()).run()
    assert session.metadata["performance"]["ocr_errors"] == text.calls - 1 <= 10
