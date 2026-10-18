"""Headless board engine for the Electron UI (`kfocr engine`, contract: app/CONTRACT.md).

Live capture, or the scripted demo, feeds a Ledger; every change goes out as one board snapshot, a
UTF-8 JSON line on stdout. Commands come in as JSON lines on stdin, and stdin EOF stops capture and
returns. Only the main loop touches the Ledger: the capture and stdin threads hand everything over
through one queue.
"""
from __future__ import annotations

import functools
import io
import json
import logging
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, Protocol, TextIO

from kfocr.capture.screen import lower_thread_priority
from kfocr.config import AppConfig, save_roi_profile
from kfocr.domain.models import AliveCount, ConfirmedEvent, NormalizedRect
from kfocr.ui.gui import ALIVE_DEFAULT, ROI_DEFAULT, _clock
from kfocr.ui.teams import Ledger, Team

LOG = logging.getLogger("kfocr.engine")
TICK_S = 0.5  # quiet-time housekeeping: knock expiry, capture health
KEEP_EVENTS = 20
_HUD_AGE = re.compile(r" \(\d+초 전 관측\)")  # Ledger.basis' live age; the UI renders it from hud.at
_EOF, _RESET = object(), object()
MATCH_OVER = "경기 종료 · 다음 경기 대기 중"


class _Match(str):
    """Pipeline.publish_match's state ("ended", "playing", "new") on its way to the main loop."""


class _Amended(NamedTuple):
    """An event already applied whose missing badges were read later."""

    event: ConfirmedEvent


# --- snapshot -------------------------------------------------------------------------------
def _ms(t: float) -> int:
    return round(t * 1000) if t else 0


def _rect(r: NormalizedRect | None) -> list[float] | None:
    return [r.x, r.y, r.w, r.h] if r else None


def _team(t: Team) -> dict:
    return {"id": t.id, "kills": t.kills, "knocks": t.knocks, "out": t.eliminated,
            "maybe_out": t.maybe_out, "left": t.remaining, "slots": t.total, "at": _ms(t.touched),
            "players": [{"name": p.name, "st": p.status.value, "at": _ms(p.touched),
                         "back": _ms(p.revived)} for p in t.players.values()]}


def snapshot(led: Ledger, *, tracking: bool, status: str, error: bool,
             rois: tuple[NormalizedRect | None, NormalizedRect | None], mode: str = "live",
             monitor: int = 0) -> dict:
    """Board snapshot v1. Times are time.monotonic() in ms; 0 = never."""
    events = []
    for e in reversed(list(led.recent)[-KEEP_EVENTS:]):
        events.append({"kind": e.event_type.value, "clock": _clock(led, e.observed_at_ns),
                       "killer": e.killer_name, "kt": led.team_of(e.killer_name, e.killer_team),
                       "victim": e.victim_name, "vt": led.team_of(e.victim_name, e.victim_team),
                       "weapon": e.weapon_label, "hs": e.headshot})
    hud = led.alive_count
    return {"v": 1, "now": _ms(time.monotonic()), "mode": mode, "monitor": monitor,
            "tracking": tracking, "status": status, "error": error,
            "players_left": led.players_left, "teams_left": led.teams_left,
            "teams_left_min": led.teams_left_min,
            "start_teams": led.team_count or None, "squad": led.squad_size,
            "hud": {"count": hud.count, "at": _ms(led.alive_count_at)} if hud else None,
            "basis": _HUD_AGE.sub("", led.basis), "kills": led.total_kills,
            "knocked": led.knocked_now, "last_at": _ms(led.last_at) if led.recent else 0,
            "teams": [_team(t) for t in led.ranked()], "events": events,
            "rois": {"killfeed": _rect(rois[0]), "alive": _rect(rois[1])}}


# --- feeds: where events come from ---------------------------------------------------------
class Feed(Protocol):
    mode: str
    error: str | None

    @property
    def status(self) -> str: ...
    @property
    def rois(self) -> tuple[NormalizedRect | None, NormalizedRect | None]: ...
    @property
    def monitor(self) -> int: ...
    def load(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def save_rois(self, rect: NormalizedRect, alive: NormalizedRect | None,
                  resolution: tuple[int, int]) -> None: ...


class _QueueSink:
    """EventSink for Pipeline's worker thread: everything goes to the engine's main loop."""

    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox

    def publish(self, event: ConfirmedEvent) -> None:
        self.inbox.put(event)

    def publish_alive_count(self, reading: AliveCount) -> None:
        self.inbox.put(reading)

    def publish_match(self, state: str) -> None:
        self.inbox.put(_Match(state))

    def publish_amended(self, event: ConfirmedEvent) -> None:
        self.inbox.put(_Amended(event))


class LiveFeed:
    """ui.live.LiveCapture without Tk: Pipeline on a worker thread, ROIs in <base>/roi.yaml."""

    mode = "live"

    def __init__(self, base: Path, roi_profile: Path | None, inbox: queue.Queue):
        self.base, self.roi_profile, self.sink = base, roi_profile, _QueueSink(inbox)
        self.config: AppConfig | None = None
        self.source: Any = None
        self.error: str | None = None
        self.idle = "캡처 대기"
        self._thread: threading.Thread | None = None

    @property
    def rois(self) -> tuple[NormalizedRect | None, NormalizedRect | None]:
        return (self.config.roi.rect, self.config.roi.alive_rect) if self.config else (None, None)

    @property
    def monitor(self) -> int:
        return self.config.capture.monitor if self.config else 0

    @property
    def status(self) -> str:
        from kfocr.capture.base import CaptureState
        from kfocr.ui.live import _STATE

        if self.error:
            return f"캡처 오류: {self.error}"
        if not (self._thread and self._thread.is_alive() and self.source):
            return self.idle
        health = self.source.health
        text = _STATE.get(health.state, "캡처 준비 중…")
        if health.state is CaptureState.DEGRADED and health.reason:
            text += f" ({health.reason})"
        return text

    def load(self) -> None:
        from kfocr.ui.live import live_config

        self.error = None
        try:
            self.config = live_config(self.base, self.roi_profile)
            res = self.config.roi.resolution
            if (self.config.roi.rect is not None and self.config.roi.alive_rect is None
                    and res and abs(res[0] / res[1] - 16 / 9) < 0.02):
                # The HUD count gives the real player count and the match start/end. The default
                # box fits PUBG's 16:9 HUD (checked on 2560x1440 recordings); other screens need
                # the ROI editor, the box could land on another number there.
                self.config.roi.alive_rect = ALIVE_DEFAULT
        except (ValueError, OSError) as exc:
            LOG.error("live capture setup failed: %s", exc)
            self.error = str(exc)

    def start(self) -> None:
        from kfocr.ui.live import _LiveScreenSource

        if self._thread and self._thread.is_alive():  # stop() timed out; two cameras would clash
            self.error = "이전 캡처가 아직 종료되지 않았습니다. 잠시 후 다시 시도하세요"
            return
        self.load()
        if self.config is None or self.error:
            return
        if self.config.roi.rect is None:
            self.idle = "ROI 보정 필요: 'ROI 조정'을 누르세요"
            return
        self.source = _LiveScreenSource(self.config.capture.monitor, self.config.capture.sample_fps)
        self._thread = threading.Thread(target=self._work, args=(self.config, self.source),
                                        name="kfocr-live", daemon=True)
        self._thread.start()

    def _work(self, config: AppConfig, source: Any) -> None:
        from kfocr.pipeline import Pipeline

        lower_thread_priority()  # OCR runs on this thread (ORT intra_op 1); the game comes first
        LOG.info("live capture starting: db=%s monitor=%s icons=%s",
                 Path(config.storage.sqlite_path).resolve(), config.capture.monitor,
                 config.vision.icon_template_dir)
        try:
            session = Pipeline(config, source, sink=self.sink).run()
            LOG.info("live capture stopped: session_id=%s", session.session_id)
            self.idle = "캡처 중지됨"
        except Exception as exc:
            if source.closing:  # stopped before the first frame; nothing to report
                LOG.info("live capture stopped before the first frame")
                return
            LOG.exception("live capture failed")
            self.error = str(exc) or type(exc).__name__

    def stop(self, timeout: float = 15) -> None:
        if self.source:
            self.source.request_stop()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                LOG.warning("live capture did not stop within %ss", timeout)

    def save_rois(self, rect: NormalizedRect, alive: NormalizedRect | None,
                  resolution: tuple[int, int]) -> None:
        output = self.base / "roi.yaml"
        ui_scale = self.config.roi.ui_scale if self.config else None
        save_roi_profile(output, rect, resolution, ui_scale, alive_rect=alive)
        self.roi_profile = output
        LOG.info("roi profile saved: %s", output.resolve())


class DemoFeed:
    """gui.simulate_match on a thread, a new seed per match. Stopping pauses the match."""

    mode = "demo"
    monitor = 0
    error: str | None = None
    status = "데모 경기 재생 중"
    HOLD_S = 12  # the final board stays up this long before the next match

    def __init__(self, inbox: queue.Queue, speed: float = 1.0, teams: int = 25):
        self.inbox, self.speed, self.teams = inbox, speed, teams
        self.rois: tuple[NormalizedRect | None, NormalizedRect | None] = (ROI_DEFAULT, ALIVE_DEFAULT)
        self._seed = 7
        self._script: Any = None
        self._pending: tuple[float, ConfirmedEvent] | None = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def load(self) -> None:
        pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._play, name="kfocr-demo", daemon=True)
        self._thread.start()

    def _play(self) -> None:
        from kfocr.ui.gui import simulate_match

        while True:
            if self._script is None:
                self._script = simulate_match(seed=self._seed, teams=self.teams)
            self._pending = self._pending or next(self._script, None)
            if self._pending is None:
                if self._halt.wait(self.HOLD_S):
                    return
                self._seed, self._script = self._seed + 1, None
                self.inbox.put(_RESET)
                continue
            delay, event = self._pending
            if self._halt.wait(delay / self.speed):
                return  # paused: the same event comes next
            self._pending = None
            self.inbox.put(event)

    def stop(self) -> None:
        self._halt.set()
        if self._thread:
            self._thread.join(2)

    def save_rois(self, rect: NormalizedRect, alive: NormalizedRect | None,
                  resolution: tuple[int, int]) -> None:
        self.rois = (rect, alive)  # the demo never writes the real profile


# --- engine ---------------------------------------------------------------------------------
class Engine:
    """Main loop: applies events and commands, emits a snapshot whenever the board changed."""

    def __init__(self, feed: Feed, new_ledger: Callable[[], Ledger], inbox: queue.Queue,
                 out: TextIO):
        self.feed, self.new_ledger, self.inbox, self.out = feed, new_ledger, inbox, out
        self.led = new_ledger()
        self.tracking, self.editing = True, False
        self.warning: str | None = None
        self.match_over = False
        self.seq = 0
        self._last: dict | None = None

    @property
    def status(self) -> tuple[str, bool]:
        if self.editing:
            return "영역 선택 중 · Enter 확정 · Esc 취소", False
        if not self.tracking:
            return "추적 꺼짐", False
        if self.warning:
            return self.warning, True
        if self.match_over and not self.feed.error:
            return MATCH_OVER, False
        return self.feed.status, bool(self.feed.error)

    def command(self, cmd: dict) -> None:
        """tracking on/off; calibrate (pause capture while the UI edits ROIs); roi (save the
        edited rects, resume); cancel (resume with the old ROIs). Others are ignored."""
        kind = cmd.get("cmd")
        if kind == "tracking":
            self.tracking, self.warning = bool(cmd.get("on")), None
        elif kind == "calibrate":
            self.editing = True
        elif kind == "cancel":
            self.editing = False
        elif kind == "roi":
            self.editing = False
            try:
                rect = NormalizedRect(*cmd["killfeed"])
                alive = NormalizedRect(*cmd["alive"]) if cmd.get("alive") else None
                width, height = (int(v) for v in cmd["screen"])
                if width <= 0 or height <= 0:
                    raise ValueError("screen size must be positive")
                self.feed.save_rois(rect, alive, (width, height))
                self.warning = None
            except (KeyError, TypeError, ValueError, OSError) as exc:
                LOG.warning("roi not saved: %s", exc)
                self.warning = f"ROI를 저장하지 않았습니다: {exc}"
        else:
            LOG.info("unknown engine command ignored: %r", kind)
            return
        if self.tracking and not self.editing:
            self.feed.stop()  # a restart picks up a new ROI; a no-op when it was not running
            self.feed.start()
        else:
            self.feed.stop()
            self.feed.load()  # keep the ROIs in the snapshot current while paused
        self.match_over = False  # a fresh capture says "new" itself if a match starts later

    def handle(self, item: object) -> None:
        if item is _RESET:
            self.led = self.new_ledger()
        elif isinstance(item, _Match):
            self.match_over = item == "ended"
            if item == "new":  # the board keeps the last match until the next one starts
                self.led = self.new_ledger()
        elif isinstance(item, _Amended):
            self.led.amend(item.event)
        elif isinstance(item, AliveCount):
            self.led.observe_alive_count(item)
        elif isinstance(item, ConfirmedEvent):
            self.led.apply(item)
        elif isinstance(item, dict):
            self.command(item)

    def emit(self) -> None:
        status, error = self.status
        snap = snapshot(self.led, tracking=self.tracking, status=status, error=error,
                        rois=self.feed.rois, mode=self.feed.mode, monitor=self.feed.monitor)
        body = {k: v for k, v in snap.items() if k != "now"}
        if body == self._last:
            return
        self._last, self.seq = body, self.seq + 1
        snap["seq"] = self.seq
        self.out.write(json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.out.flush()

    def _read(self, stdin: TextIO) -> None:
        for line in stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                LOG.warning("engine: not a JSON command line, ignored")
                continue
            if isinstance(cmd, dict):
                self.inbox.put(cmd)
        self.inbox.put(_EOF)  # the UI is gone

    def run(self, stdin: TextIO) -> None:
        threading.Thread(target=self._read, args=(stdin,), name="kfocr-stdin", daemon=True).start()
        self.feed.start()
        try:
            self.emit()
            while True:
                try:  # everything already queued goes out as one snapshot
                    item = self.inbox.get(timeout=TICK_S)
                    while item is not _EOF:
                        self.handle(item)
                        item = self.inbox.get_nowait()
                    return
                except queue.Empty:
                    pass
                self.led.tick(time.monotonic())
                self.emit()
        finally:
            self.feed.stop()


def run(*, demo: bool = False, speed: float = 1.0, roi_profile: Path | None = None,
        stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    if speed <= 0:
        raise ValueError("--speed must be positive")
    if stdout is None or stdin is None:
        if not (isinstance(sys.stdout, io.TextIOWrapper) and isinstance(sys.stdin, io.TextIOWrapper)):
            raise RuntimeError("kfocr engine needs stdin/stdout pipes (use the console kfocr.exe)")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # whatever the console code page
        sys.stdin.reconfigure(encoding="utf-8")
        stdout, stdin = stdout or sys.stdout, stdin or sys.stdin
    inbox: queue.Queue = queue.Queue()
    feed: Feed
    new_ledger: Callable[[], Ledger]
    if demo:
        feed = DemoFeed(inbox, speed)
        # the demo rosters are 4-man squads; inferred from its badges (1..40) it would be duos
        new_ledger = functools.partial(Ledger, squad_size=4, teams=feed.teams, scale=speed)
    else:
        from kfocr.ui.live import POLICY, data_dir, start_file_log

        base = data_dir()
        start_file_log(base)
        LOG.info(POLICY)
        feed, new_ledger = LiveFeed(base, roi_profile, inbox), Ledger
    Engine(feed, new_ledger, inbox, stdout).run(stdin)
