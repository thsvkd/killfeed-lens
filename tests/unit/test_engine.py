from __future__ import annotations

import io
import json
import queue
import threading
import time

import pytest

from kfocr.domain.models import AliveCount, ConfirmedEvent, EventType, NormalizedRect
from kfocr.ui import engine as eng
from kfocr.ui.teams import Ledger, make_event

K, D = EventType.KNOCK, EventType.KILL
KILLFEED = NormalizedRect(0.68, 0.0, 0.31, 0.2)


class Lines:
    """An engine stdin the test writes line by line; close() is EOF."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def send(self, obj) -> None:
        self.q.put(obj if isinstance(obj, str) else json.dumps(obj))

    def close(self) -> None:
        self.q.put(None)

    def __iter__(self):
        return iter(self.q.get, None)


class FakeFeed:
    mode, monitor, error, status = "live", 0, None, "캡처 중"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rois: tuple = (KILLFEED, None)
        self.saved: list[tuple] = []
        self.settings = {"save_event_crops": False}

    def load(self) -> None:
        self.calls.append("load")

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def save_rois(self, rect, alive, resolution) -> None:
        self.saved.append((rect, alive, resolution))
        self.rois = (rect, alive)

    def save_settings(self, save_event_crops) -> None:
        self.settings = {"save_event_crops": save_event_crops}


def start_engine(feed):
    inbox, out, stdin = queue.Queue(), io.StringIO(), Lines()
    thread = threading.Thread(target=eng.Engine(feed, Ledger, inbox, out).run, args=(stdin,),
                              daemon=True)
    thread.start()
    return inbox, out, stdin, thread


def snaps(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def wait_for(check, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_snapshot_mirrors_the_ledger_for_the_ui():
    led = Ledger(squad_size=4, teams=25)
    led.apply(make_event("a1", 7, "b1", 25, K, ns=1_000_000_000, weapon="M416"))
    led.apply(make_event("a1", 7, "b2", None, D, ns=3_000_000_000))  # unreadable victim badge

    snap = eng.snapshot(led, tracking=True, status="캡처 중", error=False, rois=(KILLFEED, None))

    assert (snap["v"], snap["squad"], snap["start_teams"], snap["mode"]) == (1, 4, 25, "live")
    assert [t["id"] for t in snap["teams"]] == [t.id for t in led.ranked()]  # overlay order
    victim = next(t for t in snap["teams"] if t["id"] == 25)
    assert victim["slots"] == 4 and victim["left"] == 4 and victim["at"] > 0
    assert victim["players"] == [{"name": "b1", "st": "knocked", "at": victim["at"], "back": 0}]
    assert next(t for t in snap["teams"] if t["id"] == 7)["kills"] == 1
    assert [e["kind"] for e in snap["events"]] == ["kill", "knock"]  # newest first
    assert snap["events"][1] == {"kind": "knock", "clock": "00:00", "killer": "a1", "kt": 7,
                                 "victim": "b1", "vt": 25, "weapon": "M416", "hs": None}
    assert snap["events"][0]["clock"] == "00:02"
    assert snap["rois"] == {"killfeed": [0.68, 0.0, 0.31, 0.2], "alive": None}
    assert json.loads(json.dumps(snap, ensure_ascii=False)) == snap


def test_snapshot_marks_underfilled_teams_that_may_be_wiped_and_gives_the_teams_left_range():
    led = Ledger(squad_size=4, teams=25)
    led.apply(make_event("a1", 7, "b1", 25, K))  # team 25: two seen, both down, two never seen
    led.apply(make_event("a1", 7, "b2", 25, D))
    for i in range(4):  # team 9: a full squad down is eliminated, not "maybe"
        led.apply(make_event("a2", 7, f"c{i}", 9, D))

    snap = eng.snapshot(led, tracking=True, status="", error=False, rois=(None, None))

    flags = {t["id"]: t["maybe_out"] for t in snap["teams"]}
    assert flags == {25: True, 9: False, 7: False}
    assert (snap["teams_left"], snap["teams_left_min"]) == (24, 23)
    # the per-team flags (Team.maybe_out) are exactly what Ledger.teams_left_min subtracts
    assert sum(flags.values()) == snap["teams_left"] - snap["teams_left_min"]
    led.apply(make_event("b3", 25, "a1", 7, K))  # a third member of 25 acts: alive after all
    snap = eng.snapshot(led, tracking=True, status="", error=False, rois=(None, None))
    assert not next(t for t in snap["teams"] if t["id"] == 25)["maybe_out"]
    assert snap["teams_left_min"] == snap["teams_left"]


def test_snapshot_start_teams_falls_back_to_the_confirmed_badge_count():
    led = Ledger()  # live: no start team count given
    snap = eng.snapshot(led, tracking=True, status="", error=False, rois=(None, None))
    assert snap["start_teams"] is None  # unknown: the UI shows "—" and an empty gauge

    led.apply(make_event("a1", 24, "b1", 3, K, ns=1_000_000_000))
    led.apply(make_event("a1", 24, "b2", 3, D, ns=3_000_000_000))  # badge 24 seen twice, apart
    snap = eng.snapshot(led, tracking=True, status="", error=False, rois=(None, None))
    assert snap["start_teams"] == led.top_badge == 24  # so the gauge has its denominator


def test_snapshot_leaves_the_hud_age_to_the_ui(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("kfocr.ui.teams.time.monotonic", lambda: clock[0])
    led = Ledger()
    led.observe_alive_count(AliveCount(60, 0, 0))
    clock[0] = 130.0
    assert "30초 전" in led.basis

    snap = eng.snapshot(led, tracking=True, status="", error=False, rois=(None, None))

    # an age in the text would change every second and force a snapshot every second
    assert "초 전" not in snap["basis"] and "화면" in snap["basis"]
    assert snap["hud"] == {"count": 60, "at": 100_000} and snap["players_left"] == 60


def test_commands_pause_capture_and_save_edited_rois():
    feed = FakeFeed()
    _, out, stdin, thread = start_engine(feed)
    wait_for(lambda: snaps(out))
    assert feed.calls == ["start"] and snaps(out)[0]["tracking"] is True

    stdin.send({"cmd": "tracking", "on": False})
    wait_for(lambda: snaps(out)[-1]["status"] == "추적 꺼짐")
    assert snaps(out)[-1]["tracking"] is False and feed.calls[-2:] == ["stop", "load"]

    stdin.send("not json")
    stdin.send({"cmd": "warp"})  # unknown commands are ignored
    stdin.send({"cmd": "calibrate"})
    wait_for(lambda: "영역 선택" in snaps(out)[-1]["status"])
    stdin.send({"cmd": "roi", "killfeed": [0.6, 0, 0.35, 0.25], "alive": [0.9, 0.01, 0.05, 0.04],
                "screen": [2560, 1440]})
    wait_for(lambda: snaps(out)[-1]["rois"]["killfeed"] == [0.6, 0, 0.35, 0.25])
    assert feed.saved == [(NormalizedRect(0.6, 0, 0.35, 0.25), NormalizedRect(0.9, 0.01, 0.05, 0.04),
                           (2560, 1440))]
    assert snaps(out)[-1]["status"] == "추적 꺼짐"  # still off; the new ROI applies once it is on
    assert "start" not in feed.calls[1:]

    stdin.send({"cmd": "tracking", "on": True})
    wait_for(lambda: feed.calls[-1] == "start")
    stdin.close()
    thread.join(5)
    assert not thread.is_alive() and feed.calls[-1] == "stop"  # EOF stops capture and returns


def test_a_bad_roi_is_reported_and_capture_resumes_with_the_old_one():
    feed = FakeFeed()
    _, out, stdin, thread = start_engine(feed)
    stdin.send({"cmd": "calibrate"})
    stdin.send({"cmd": "roi", "killfeed": [0.9, 0, 0.5, 0.2], "screen": [2560, 1440]})  # off frame
    wait_for(lambda: snaps(out) and snaps(out)[-1]["error"])
    assert "ROI를 저장하지 않았습니다" in snaps(out)[-1]["status"] and not feed.saved
    assert snaps(out)[-1]["rois"]["killfeed"] == [0.68, 0.0, 0.31, 0.2]
    assert feed.calls[-2:] == ["stop", "start"]
    stdin.close()
    thread.join(5)


def test_settings_command_saves_restarts_capture_and_rejects_bad_values():
    feed = FakeFeed()
    _, out, stdin, thread = start_engine(feed)
    wait_for(lambda: snaps(out))
    assert snaps(out)[0]["settings"] == {"save_event_crops": False}

    stdin.send({"cmd": "settings", "save_event_crops": True})
    wait_for(lambda: snaps(out)[-1]["settings"]["save_event_crops"])
    assert feed.calls[-2:] == ["stop", "start"] and not snaps(out)[-1]["error"]

    stdin.send({"cmd": "settings", "save_event_crops": "yes"})
    wait_for(lambda: snaps(out)[-1]["error"])
    assert "설정을 저장하지 않았습니다" in snaps(out)[-1]["status"]
    assert snaps(out)[-1]["settings"] == {"save_event_crops": True}  # kept
    stdin.close()
    thread.join(5)


def test_events_and_hud_readings_go_out_once_per_change():
    feed = FakeFeed()
    inbox, out, stdin, thread = start_engine(feed)
    wait_for(lambda: snaps(out))
    inbox.put(make_event("a1", 7, "b1", 25, K))
    inbox.put(make_event("a1", 7, "b2", 25, K))
    inbox.put(AliveCount(97, 1, 0))
    wait_for(lambda: snaps(out)[-1]["hud"])
    last = snaps(out)[-1]
    assert (last["knocked"], last["players_left"], len(last["events"])) == (2, 97, 2)

    time.sleep(eng.TICK_S * 2.5)
    assert snaps(out)[-1]["seq"] == last["seq"]  # nothing changed, nothing new on stdout
    assert [s["seq"] for s in snaps(out)] == list(range(1, last["seq"] + 1))
    stdin.close()
    thread.join(5)


def test_a_finished_match_stays_on_the_board_until_the_next_one_starts():
    feed = FakeFeed()
    inbox, out, stdin, thread = start_engine(feed)
    wait_for(lambda: snaps(out))
    sink = eng._QueueSink(inbox)
    inbox.put(make_event("a1", 7, "b1", 25, D))
    sink.publish_match("ended")
    wait_for(lambda: snaps(out)[-1]["status"] == eng.MATCH_OVER)
    assert len(snaps(out)[-1]["events"]) == 1
    sink.publish_match("new")
    wait_for(lambda: not snaps(out)[-1]["events"])
    assert snaps(out)[-1]["status"] == FakeFeed.status
    for clear in (lambda: sink.publish_match("playing"),  # back after an alt-tab
                  lambda: inbox.put({"cmd": "tracking", "on": True})):  # capture restarted
        sink.publish_match("ended")
        wait_for(lambda: snaps(out)[-1]["status"] == eng.MATCH_OVER)
        clear()
        wait_for(lambda: snaps(out)[-1]["status"] == FakeFeed.status)
    stdin.close()
    thread.join(5)


def test_demo_feed_plays_and_pauses():
    inbox: queue.Queue = queue.Queue()
    feed = eng.DemoFeed(inbox, speed=50)
    feed.start()
    time.sleep(0.4)
    feed.stop()
    played = [inbox.get_nowait() for _ in range(inbox.qsize())]
    assert played and all(isinstance(e, ConfirmedEvent) for e in played)
    time.sleep(0.2)
    assert inbox.empty()  # stopped means paused
    feed.start()
    time.sleep(0.3)
    feed.stop()
    assert not inbox.empty()  # resumed where it left off


def test_live_feed_waits_for_an_roi_and_saves_it_where_live_config_reads_it(tmp_path):
    feed = eng.LiveFeed(tmp_path, None, queue.Queue())
    feed.start()
    assert feed._thread is None and "ROI 보정 필요" in feed.status and feed.rois == (None, None)

    alive = NormalizedRect(0.9, 0.01, 0.05, 0.04)
    feed.save_rois(KILLFEED, alive, (2560, 1440))
    feed.load()
    assert feed.rois == (KILLFEED, alive) and (tmp_path / "roi.yaml").exists()

    feed.save_rois(KILLFEED, None, (2560, 1440))  # no HUD box saved: read PUBG's spot anyway
    feed.load()
    assert feed.rois == (KILLFEED, eng.ALIVE_DEFAULT)
    feed.save_rois(KILLFEED, None, (2560, 1080))  # not 16:9: the default box may miss the HUD
    feed.load()
    assert feed.rois == (KILLFEED, None)


def test_live_feed_merges_settings_into_config_yaml_and_keeps_other_keys(tmp_path):
    feed = eng.LiveFeed(tmp_path, None, queue.Queue())
    assert feed.settings == {"save_event_crops": False}  # before load
    feed.save_settings(True)  # no config.yaml yet: created
    feed.load()
    assert feed.error is None and feed.settings == {"save_event_crops": True}

    (tmp_path / "config.yaml").write_text(
        "capture:\n  monitor: 1\ndebug:\n  save_full_frames: true\n", encoding="utf-8")
    feed.save_settings(True)
    feed.load()
    assert feed.config.capture.monitor == 1 and feed.config.debug.save_full_frames
    assert feed.settings == {"save_event_crops": True}
    feed.save_settings(False)
    feed.load()
    assert feed.settings == {"save_event_crops": False} and feed.config.capture.monitor == 1

    bad = "capture: [unclosed\n"
    (tmp_path / "config.yaml").write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        feed.save_settings(True)
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == bad  # never overwritten


def test_demo_feed_keeps_settings_in_memory():
    feed = eng.DemoFeed(queue.Queue())
    assert feed.settings == {"save_event_crops": False}
    feed.save_settings(True)
    assert feed.settings == {"save_event_crops": True}


def test_cli_wires_the_engine_and_rejects_a_bad_speed():
    from kfocr.cli import main

    assert main(["engine", "--demo", "--speed", "0"]) == 2
