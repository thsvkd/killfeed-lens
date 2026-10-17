"""Per-team match status: a cards window plus a toggleable always-on-top overlay (tkinter).

Colour never carries meaning alone: alive = filled circle, assumed-alive (squad slot never seen
in the feed) = dashed ring, knocked = diamond, dead = cross, comeback = up-triangle.  Palette is
Okabe-Ito based.  The overlay never takes keyboard focus, so the game keeps its input.
"""
from __future__ import annotations

import ctypes
import json
import queue
import random
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont
from typing import TYPE_CHECKING, Literal, TextIO

from kfocr.domain.models import AliveCount, ConfirmedEvent, EventType, NormalizedRect
from kfocr.ui.teams import Ledger, Status, Team, make_event
from kfocr.vision.roi import pixel_rect

if TYPE_CHECKING:
    from kfocr.ui.live import LiveCapture

BG, PANEL, LINE = "#0D1117", "#161B22", "#2A323D"
TEXT, DIM, FAINT = "#E6EDF3", "#8B949E", "#4C5560"
ALIVE, KNOCK, DEAD, HOT, KILLC = "#56B4E9", "#E69F00", "#6E7681", "#F0E442", "#F78166"
FLASH_S = 4.0  # how long a fresh change stays highlighted
OVERLAY_W, CARDS_SIZE = 264, (1040, 780)
HOTKEY_LABEL = "Ctrl+Alt+O"
VERB = {EventType.KILL: "처치", EventType.KNOCK: "기절", EventType.FINISH: "마무리",
        EventType.BLEEDOUT: "출혈사", EventType.SUICIDE: "자살", EventType.ENVIRONMENT: "환경사",
        EventType.UNKNOWN: "?"}
_FAM = {"ui": "TkDefaultFont", "num": "TkDefaultFont"}


# --- small drawing helpers ------------------------------------------------------------------
def _mix(a: str, b: str, t: float) -> str:
    t = min(1.0, max(0.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb, strict=True))


def _f(size: int, bold: bool = False, num: bool = False) -> tuple[str, int, str]:
    return (_FAM["num" if num else "ui"], -size, "bold" if bold else "normal")


def _t(c: tk.Canvas, x: float, y: float, s: object, size: int, fill: str = TEXT,
       anchor: Literal["w", "e", "center"] = "w", bold: bool = False, num: bool = False,
       **kw) -> int:
    return c.create_text(x, y, text=str(s), fill=fill, anchor=anchor, font=_f(size, bold, num), **kw)


def _heat(touched: float, now: float) -> float:
    return max(0.0, 1.0 - (now - touched) / FLASH_S) if touched else 0.0


def _badge(team: int | None) -> str:
    return "??" if team is None else f"{team:02d}"


def _clip(s: str | None, n: int) -> str:
    s = s or "-"
    return s if len(s) <= n else s[: n - 1] + "…"


def _ago(sec: float) -> str:
    s = int(sec)
    return f"{s}초 전" if s < 60 else f"{s // 60}분 {s % 60:02d}초 전"


def _clock(led: Ledger, ns: int) -> str:
    s = max(0, (ns - (led.first_ns or 0)) // 1_000_000_000)
    return f"{s // 60:02d}:{s % 60:02d}"


def _up(c: tk.Canvas, x: float, y: float, fill: str, s: int = 4) -> None:
    c.create_polygon(x, y - 2 * s, x + s + 1, y, x - s - 1, y, fill=fill, outline=fill)


def _pip(c: tk.Canvas, x: float, y: float, r: float, status: Status, assumed: bool = False,
         halo: float = 0.0, bg: str = PANEL, back: float = 0.0) -> None:
    if halo > 0.05:
        c.create_oval(x - r - 5, y - r - 5, x + r + 5, y + r + 5, outline=_mix(bg, HOT, halo),
                      width=2)
    if back > 0.05:  # came back (revive / recall / AED / expired knock): marker above the pip
        _up(c, x, y - r - (6 if r > 8 else 2), _mix(bg, HOT, back), 5 if r > 8 else 3)
    if status is Status.ALIVE and assumed:
        c.create_oval(x - r, y - r, x + r, y + r, outline=ALIVE, width=2, dash=(2, 2))
    elif status is Status.ALIVE:
        c.create_oval(x - r, y - r, x + r, y + r, fill=ALIVE, outline=ALIVE)
    elif status is Status.KNOCKED:
        r += 1
        c.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y, fill=KNOCK, outline=KNOCK)
    else:
        d = r * 0.75
        c.create_line(x - d, y - d, x + d, y + d, fill=DEAD, width=2)
        c.create_line(x - d, y + d, x + d, y - d, fill=DEAD, width=2)


def _icon(c: tk.Canvas, kind: str, x: float, y: float, r: float = 5, fill: str = TEXT) -> None:
    """kind: kill (crosshair), knock (diamond), dead (cross), other (hollow square)."""
    if kind == "kill":
        c.create_oval(x - r, y - r, x + r, y + r, outline=fill, width=2)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c.create_line(x + dx * (r - 2), y + dy * (r - 2), x + dx * (r + 3), y + dy * (r + 3),
                          fill=fill, width=2)
    elif kind == "knock":
        c.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y, fill=fill, outline=fill)
    elif kind == "dead":
        c.create_line(x - r, y - r, x + r, y + r, fill=fill, width=2)
        c.create_line(x - r, y + r, x + r, y - r, fill=fill, width=2)
    else:
        c.create_rectangle(x - r + 1, y - r + 1, x + r - 1, y + r - 1, outline=fill, width=2)


def _event_icon(c: tk.Canvas, kind: EventType, x: float, y: float, r: float = 5) -> None:
    if kind is EventType.KNOCK:
        _icon(c, "knock", x, y, r, KNOCK)
    elif kind in (EventType.KILL, EventType.FINISH):
        _icon(c, "dead", x, y, r, KILLC)
    else:
        _icon(c, "other", x, y, r, DIM)


def _verb_color(kind: EventType) -> str:
    return KNOCK if kind is EventType.KNOCK else KILLC if kind in (
        EventType.KILL, EventType.FINISH) else DIM


def _pips(c: tk.Canvas, team: Team, x: float, y: float, r: float, step: float, now: float,
          bg: str = PANEL) -> None:
    pips = team.pips()
    for i, (status, name) in enumerate(pips):
        player = team.players.get(name) if name else None
        halo = _heat(player.touched, now) if player else 0.0
        back = _heat(player.revived, now) if player else 0.0
        _pip(c, x + i * step, y, r, status, assumed=name is None, halo=halo, bg=bg, back=back)


def _legend(c: tk.Canvas, x: float, y: float) -> None:
    for kind, label in (("alive", "생존"), ("ring", "미확인(생존 가정)"), ("knock", "기절"),
                        ("dead", "사망"), ("up", "복귀(부활·리콜)")):
        if kind == "up":
            _up(c, x, y + 3, HOT)
        elif kind == "alive":
            _pip(c, x, y, 5, Status.ALIVE)
        elif kind == "ring":
            _pip(c, x, y, 5, Status.ALIVE, assumed=True)
        elif kind == "knock":
            _pip(c, x, y, 5, Status.KNOCKED)
        else:
            _pip(c, x, y, 5, Status.DEAD)
        x = c.bbox(_t(c, x + 12, y, label, 11, DIM))[2] + 18


def _waiting(c: tk.Canvas, w: int, y: int) -> int:
    _t(c, w / 2, y, "킬피드 이벤트를 기다리는 중…", 14, DIM, anchor="center")
    return y + 40


# --- overlay: narrow always-on-top strip ----------------------------------------------------------------------
OVERLAY_ROWS = 12


def draw_overlay(c: tk.Canvas, led: Ledger, w: int, now: float) -> int:
    w = OVERLAY_W
    live = [t for t in led.ranked() if not t.eliminated]
    hot = [t for t in live[OVERLAY_ROWS:] if _heat(t.touched, now) > 0.02][:3]  # never hide a change
    shown = live[:OVERLAY_ROWS - len(hot)] + hot
    top, row = 62, 24
    foot = top + max(len(shown), 1) * row + (20 if len(live) > len(shown) else 0)
    h = foot + 54
    _t(c, 14, 28, led.players_left, 34, TEXT, bold=True, num=True)
    stale = led.alive_count is not None and now - led.alive_count_at >= 5
    label = ("인원 · 마지막 관측" if stale else "남은 인원 · 화면") if led.alive_count else "남은 인원 · 추정"
    _t(c, 14, 51, label, 10, DIM)
    _t(c, w - 14, 25, led.teams_left, 24, TEXT, anchor="e", bold=True, num=True)
    _t(c, w - 14, 51, "남은 팀 · 추정", 10, DIM, anchor="e")
    c.create_line(0, top - 2, w, top - 2, fill=LINE)
    if not shown:
        _t(c, w / 2, top + 12, "대기 중…", 12, DIM, anchor="center")
    for i, team in enumerate(shown):
        y = top + i * row + row / 2
        heat = _heat(team.touched, now)
        if heat > 0.02:
            c.create_rectangle(0, y - row / 2, w, y + row / 2, fill=_mix(BG, HOT, 0.3 * heat),
                               outline="")
            c.create_rectangle(0, y - row / 2, 3, y + row / 2, fill=HOT, outline="")
        _t(c, 14, y, _badge(team.id), 15, TEXT, bold=True, num=True)
        _pips(c, team, 62, y, 6, 18, now, bg=BG)
        _icon(c, "kill", w - 40, y, 4, DIM)
        _t(c, w - 14, y, team.kills, 14, TEXT, anchor="e", bold=True, num=True)
    if len(live) > len(shown):
        _t(c, 14, foot - 10, f"+ {len(live) - len(shown)}팀 더", 11, DIM)
    c.create_line(0, foot, w, foot, fill=LINE)
    if led.recent:
        e = led.recent[-1]
        heat = _heat(led.last_at, now)
        c.create_rectangle(0, foot + 1, w, h, fill=_mix(BG, HOT, 0.2 * heat), outline="")
        _event_icon(c, e.event_type, 18, foot + 17)
        kt, vt = led.team_of(e.killer_name, e.killer_team), led.team_of(e.victim_name, e.victim_team)
        who = f"{_badge(kt)} {_clip(e.killer_name, 8)} → {_badge(vt)} {_clip(e.victim_name, 8)}"
        _t(c, 32, foot + 17, who, 12, TEXT)
        _t(c, 32, foot + 38, f"{VERB[e.event_type]} · {_ago(now - led.last_at)}", 11,
           _verb_color(e.event_type))
    c.create_rectangle(0, 0, w - 1, h - 1, outline=LINE)
    return h


# --- main window: team card grid ---------------------------------------------------------------
def draw_cards(c: tk.Canvas, led: Ledger, w: int, now: float) -> int:
    pad, gap, card_h = 18, 12, 118
    _t(c, pad, 32, led.players_left, 38, TEXT, bold=True, num=True)
    x = c.bbox(c.find_all()[-1])[2] + 8
    _t(c, x, 40, "명 남음 · 화면" if led.alive_count else "명 남음 · 추정", 13, DIM)
    _t(c, x + 145, 32, led.teams_left, 38, TEXT, bold=True, num=True)
    _t(c, c.bbox(c.find_all()[-1])[2] + 8, 40, "팀 남음 · 추정", 13, DIM)
    _t(c, w - pad, 24, f"킬 {led.total_kills} · 기절 중 {led.knocked_now}", 12, DIM, anchor="e")
    _t(c, w - pad, 46, led.basis, 11, DIM, anchor="e")
    _legend(c, pad + 4, 72)
    teams = sorted(led.teams.values(), key=lambda t: (t.id is None, t.id or 0))
    if not teams:
        return _waiting(c, w, 160)
    cols = max(1, (w - 2 * pad + gap) // (190 + gap))
    cw = (w - 2 * pad - gap * (cols - 1)) / cols
    for i, team in enumerate(teams):
        x0 = pad + (i % cols) * (cw + gap)
        y0 = 96 + (i // cols) * (card_h + gap)
        x1, y1 = x0 + cw, y0 + card_h
        dead = team.eliminated
        heat = _heat(team.touched, now)
        c.create_rectangle(x0, y0, x1, y1, fill=BG if dead else PANEL,
                           outline=_mix(LINE, HOT, heat), width=3 if heat > 0.05 else 1)
        fg = FAINT if dead else TEXT
        _t(c, x0 + 14, y0 + 24, _badge(team.id), 26, fg, bold=True, num=True)
        if dead:
            c.create_line(x0 + 12, y0 + 24, x0 + 52, y0 + 24, fill=FAINT, width=3)
        _t(c, x1 - 14, y0 + 24, team.kills, 24, fg, anchor="e", bold=True, num=True)
        _icon(c, "kill", x1 - 52, y0 + 25, 6, DIM if not dead else FAINT)
        _t(c, x1 - 14, y0 + 47, f"기절시킴 {team.knocks}", 10, DIM, anchor="e")
        _pips(c, team, x0 + 24, y0 + 70, 11, 31, now)
        status = "탈락" if dead else f"남은 {team.remaining} · 기절 {team.knocked}"
        _t(c, x0 + 14, y0 + 98, status, 12, FAINT if dead else DIM, bold=dead)
        if team.touched:
            _t(c, x1 - 14, y0 + 98, _ago(now - team.touched), 10, DIM, anchor="e")
    rows = (len(teams) + cols - 1) // cols
    return 96 + rows * (card_h + gap) + pad


# --- event plumbing -------------------------------------------------------------------------
class GuiSink:
    """EventSink that is safe to call from the pipeline worker thread."""

    def __init__(self, events: queue.Queue[ConfirmedEvent | AliveCount]):
        self.events = events

    def publish(self, event: ConfirmedEvent) -> None:
        self.events.put(event)

    def publish_alive_count(self, reading: AliveCount) -> None:
        self.events.put(reading)


def simulate_match(seed: int = 7, teams: int = 25) -> Iterator[tuple[float, ConfirmedEvent]]:
    """Scripted fake match: (seconds to wait, event).

    Includes unreadable badges, silent revives, one squad knocked out together (wipe by knock)
    and, later, one of its players coming back (recall) by showing up in the feed again.
    """
    rng = random.Random(seed)
    first = ["Snow", "Night", "Iron", "Ghost", "Blue", "Mad", "Red", "Lucky", "Dark", "Neo",
             "Tiger", "Mono", "Zero", "Hyper", "Pan", "곰돌이", "밤하늘", "초코"]
    last = ["Fox", "Wolf", "Hawk", "Kid", "Rain", "Moon", "King", "Bear", "Cat", "Jin", "Ace",
            "Bolt", "여우", "호랑", "번개"]
    used: set[str] = set()

    def fresh() -> str:
        while (n := rng.choice(first) + rng.choice(last)) in used:
            pass
        used.add(n)
        return n

    guns = ["M416", "AKM", "Kar98k", "SCAR-L", "Mini14", "S12K", "Vector", "Win94"]
    roster = {t: {fresh(): Status.ALIVE for _ in range(4)} for t in rng.sample(range(1, 41), teams)}
    clock = 0.0

    def live() -> list[int]:
        return [t for t, r in roster.items() if any(s is not Status.DEAD for s in r.values())]

    def foe(exclude: int) -> tuple[int, str] | None:
        pool = [t for t in live() if t != exclude
                and any(s is Status.ALIVE for s in roster[t].values())]
        if not pool:
            return None
        team = rng.choice(pool)
        return team, rng.choice([n for n, s in roster[team].items() if s is Status.ALIVE])

    def ev(delay: float, kind: EventType, killer: tuple[int | None, str | None],
           victim: tuple[int | None, str]) -> tuple[float, ConfirmedEvent]:
        nonlocal clock
        clock += delay
        return delay, make_event(killer[1], killer[0], victim[1], victim[0], kind,
                                 int(clock * 1e9), rng.choice(guns) if killer[1] else None,
                                 rng.random() < 0.25 if killer[1] else None)

    step, wiped = 0, None
    while len(live()) > 1:
        step += 1
        if step == 22:  # a full squad is knocked one after another; the feed never shows more
            full = [t for t, r in roster.items() if all(s is Status.ALIVE for s in r.values())]
            wiped = rng.choice(full) if full else None
            if wiped is not None and (hit := foe(wiped)):
                for name in list(roster[wiped]):
                    yield ev(0.5, EventType.KNOCK, hit, (wiped, name))
                    roster[wiped][name] = Status.DEAD  # they bleed out unobserved
        if step == 60 and wiped is not None:  # recall: one of them is back and fights
            back = next(iter(roster[wiped]))
            roster[wiped][back] = Status.ALIVE
            if target := foe(wiped):
                roster[target[0]][target[1]] = Status.KNOCKED
                yield ev(1.0, EventType.KNOCK, (wiped, back), target)
        delay = rng.uniform(0.3, 1.4)
        vt = rng.choice(live())
        victim, state = rng.choice([(n, s) for n, s in roster[vt].items() if s is not Status.DEAD])
        vbadge = None if rng.random() < 0.04 else vt
        if rng.random() < 0.04:
            roster[vt][victim] = Status.DEAD
            yield ev(delay, EventType.ENVIRONMENT, (None, None), (vbadge, victim))
            continue
        if state is Status.KNOCKED and (roll := rng.random()) < 0.2:
            roster[vt][victim] = Status.ALIVE  # revived: nothing shows up in the feed
            clock += delay
            continue
        if state is Status.KNOCKED and roll < 0.4:
            roster[vt][victim] = Status.DEAD
            yield ev(delay, EventType.BLEEDOUT, (None, None), (vbadge, victim))
            continue
        if not (hit := foe(vt)):
            continue
        abadge = None if rng.random() < 0.05 else hit[0]
        if state is Status.KNOCKED:
            kind = EventType.FINISH
        else:
            kind = EventType.KILL if rng.random() < 0.3 else EventType.KNOCK
        roster[vt][victim] = Status.KNOCKED if kind is EventType.KNOCK else Status.DEAD
        yield ev(delay, kind, (abadge, hit[1]), (vbadge, victim))


def event_from_row(row: dict) -> ConfirmedEvent | AliveCount:
    if row.get("type") == "alive_count":
        return AliveCount(row["count"], int(row["frame_seq"]), int(row["observed_at_ns"]))
    return make_event(row.get("killer_name"), row.get("killer_team"), row.get("victim_name"),
                      row.get("victim_team"), EventType(row["event_type"]),
                      int(row["observed_at_ns"]), row.get("weapon_label"), row.get("headshot"))


def play_demo(sink: GuiSink, speed: float, stop: threading.Event) -> None:
    for delay, event in simulate_match():
        if stop.wait(delay / speed):
            return
        sink.publish(event)


def tail_jsonl(sink: GuiSink, stream: TextIO, stop: threading.Event) -> None:
    """Feed existing lines, then keep following the file (e.g. while `kfocr run --jsonl` writes)."""
    buffer = ""
    while not stop.is_set():
        chunk = stream.readline()
        if not chunk:
            stop.wait(0.3)
            continue
        buffer += chunk
        if not buffer.endswith("\n"):  # writer is mid-line
            continue
        line, buffer = buffer.strip(), ""
        try:
            if line:
                record = event_from_row(json.loads(line))
                if isinstance(record, AliveCount):
                    sink.publish_alive_count(record)
                else:
                    sink.publish(record)
        except (ValueError, KeyError, TypeError):
            continue  # skip malformed rows


# --- Windows glue: stay in front without taking focus, global hotkey ------------------------
_EX_NOACTIVATE, _EX_TOOLWINDOW, _EX_TOPMOST = 0x08000000, 0x00000080, 0x00000008
_EX_TRANSPARENT = 0x00000020  # with Tk's WS_EX_LAYERED (alpha): mouse falls through to the game
_SWP_KEEP = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
_SWP_FRAMECHANGED = 0x0020


def _user32():
    if sys.platform != "win32":
        raise OSError("user32 is Windows-only")
    u = ctypes.windll.user32
    u.GetParent.restype = wintypes.HWND
    u.GetParent.argtypes = [wintypes.HWND]
    u.GetWindowLongW.restype = ctypes.c_long
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return u


def pin_on_top(widget: tk.Misc, want: int = _EX_NOACTIVATE | _EX_TOOLWINDOW | _EX_TOPMOST,
               drop: int = 0) -> int:
    """Keep a toplevel above other windows without activating it; returns its extended style.

    `want`/`drop`: extended styles to set/clear (toolwindow: no taskbar/alt-tab entry).
    Does nothing off Windows. Cannot draw over exclusive-fullscreen games (borderless is fine).
    """
    if sys.platform != "win32":
        return 0
    u = _user32()
    hwnd = u.GetParent(widget.winfo_id())
    style = u.GetWindowLongW(hwnd, -20)
    flags = _SWP_KEEP
    if (style | want) & ~drop != style:
        u.SetWindowLongW(hwnd, -20, (style | want) & ~drop)
        flags |= _SWP_FRAMECHANGED
    u.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
    return u.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF


class Hotkey:
    """Global Ctrl+Alt+O (works while the game has focus). Own message-loop thread; no-op elsewhere.

    RegisterHotKey delivers WM_HOTKEY to the registering thread, and Tk's loop would swallow it,
    so a dedicated thread owns the registration and only sets a flag that the Tk thread polls.
    """

    MODIFIERS = 0x0002 | 0x0001 | 0x4000  # CONTROL | ALT | NOREPEAT
    VK_O, WM_HOTKEY, WM_QUIT = 0x4F, 0x0312, 0x0012

    def __init__(self) -> None:
        self.pressed = threading.Event()
        self.ok = False
        self._ready = threading.Event()
        self._tid = 0
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if sys.platform == "win32":
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self._ready.wait(2)
        return self.ok

    def _loop(self) -> None:
        if sys.platform != "win32":
            return
        u = ctypes.windll.user32
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0x400, 0x400, 0)  # create this thread's queue
        self.ok = bool(u.RegisterHotKey(None, 1, self.MODIFIERS, self.VK_O))
        self._ready.set()
        if not self.ok:
            return
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                self.pressed.set()
        u.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if sys.platform == "win32" and self._thread and self.ok:
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
            self._thread.join(2)


# --- windows --------------------------------------------------------------------------------
def hide_from_capture(widget: tk.Misc) -> bool:
    """Exclude a toplevel from screen capture (incl. DXGI desktop duplication) so the killfeed OCR
    never reads the overlay's own text. Windows 10 2004+; False elsewhere or on failure."""
    if sys.platform != "win32":
        return False
    u = _user32()
    return bool(u.SetWindowDisplayAffinity(u.GetParent(widget.winfo_id()), 0x11))


def clear_of(box: tuple[float, float, float, float], roi: tuple[float, float, float, float],
             screen: tuple[float, float], gap: int = 8) -> tuple[int, int]:
    """Shortest move of box (x, y, w, h) that leaves the ROI uncovered and stays on screen."""
    x, y, w, h = box
    rx, ry, rw, rh = roi
    if x >= rx + rw or x + w <= rx or y >= ry + rh or y + h <= ry:
        return int(x), int(y)
    spots = [(x, ry + rh + gap), (x, ry - h - gap), (rx - w - gap, y), (rx + rw + gap, y)]
    fits = [(a, b) for a, b in spots if 0 <= a <= screen[0] - w and 0 <= b <= screen[1] - h]
    # ponytail: no fitting side keeps the overlap (capture is still excluded); add corner spots
    # if a ROI ever fills most of the screen.
    a, b = min(fits or [(x, y)], key=lambda p: abs(p[0] - x) + abs(p[1] - y))
    return int(a), int(b)


def clear_of_rois(box: tuple[float, float, float, float],
                  rois: list[tuple[float, float, float, float]],
                  screen: tuple[float, float], gap: int = 8) -> tuple[int, int]:
    """Find a position that avoids every ROI, including when moving past one meets another."""
    x, y, w, h = box
    if all(x >= rx + rw or x + w <= rx or y >= ry + rh or y + h <= ry for rx, ry, rw, rh in rois):
        return int(x), int(y)  # clear already: leave it, even on another monitor
    xs, ys = {x}, {y}
    for rx, ry, rw, rh in rois:
        xs.update((rx - w - gap, rx + rw + gap))
        ys.update((ry - h - gap, ry + rh + gap))
    spots = [(a, b) for a in xs for b in ys
             if 0 <= a <= screen[0] - w and 0 <= b <= screen[1] - h
             and all(a >= rx + rw or a + w <= rx or b >= ry + rh or b + h <= ry
                     for rx, ry, rw, rh in rois)]
    a, b = min(spots or [(x, y)], key=lambda p: abs(p[0] - x) + abs(p[1] - y))
    return int(a), int(b)


# --- ROI box: a live selection frame over the game ------------------------------------------
GRIP, ROI_MIN = 8, 24  # px: edge band that resizes; smallest box side
ROI_DEFAULT = NormalizedRect(0.68, 0.0, 0.31, 0.2)  # PUBG killfeed corner, top-right
ROI_LINE, ROI_FILL, ROI_KEY = "#58A6FF", "#1F3A5F", "#FF00FE"  # key: see-through colour
_CURSOR = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we", "nw": "size_nw_se",
           "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw", "": "fleur"}


def edge_at(x: float, y: float, w: float, h: float, grip: int = GRIP) -> str:
    """Which edge/corner ("n", "se", ...) a point inside a w x h box is on; "" = interior."""
    return ("n" if y < grip else "s" if y >= h - grip else "") + (
        "w" if x < grip else "e" if x >= w - grip else "")


def drag_box(box: tuple[int, int, int, int], edge: str, dx: int, dy: int,
             screen: tuple[int, int], least: int = ROI_MIN) -> tuple[int, int, int, int]:
    """box (x, y, w, h) after dragging `edge` by (dx, dy) ("" moves it), kept on screen."""
    x, y, w, h = box
    sw, sh = screen
    if not edge:
        return min(max(0, x + dx), sw - w), min(max(0, y + dy), sh - h), w, h
    left, top, right, bottom = x, y, x + w, y + h
    if "w" in edge:
        left = min(max(0, left + dx), right - least)
    if "e" in edge:
        right = max(min(sw, right + dx), left + least)
    if "n" in edge:
        top = min(max(0, top + dy), bottom - least)
    if "s" in edge:
        bottom = max(min(sh, bottom + dy), top + least)
    return left, top, right - left, bottom - top


def box_to_rect(box: tuple[int, int, int, int], screen: tuple[int, int]) -> NormalizedRect:
    x, y, w, h = box
    sw, sh = screen
    return NormalizedRect(x / sw, y / sh, min(w / sw, 1 - x / sw), min(h / sh, 1 - y / sh))


class RoiBox:
    """Borderless always-on-top ROI frame, drawn like the drag selection.

    Editing: edges resize, inside drags it, Enter confirms, Esc cancels (keys need its focus; click
    it to get it back from the game). Confirmed: a faint outline that mouse and keyboard fall
    through and screen capture never sees. ROI is normalized to Tk's primary screen.
    """

    def __init__(self, root: tk.Misc, on_confirm: Callable[[NormalizedRect], None],
                 on_cancel: Callable[[], None], *, label: str = "킬피드",
                 default_rect: NormalizedRect = ROI_DEFAULT):
        self.on_confirm, self.on_cancel = on_confirm, on_cancel
        self.label, self.default_rect = label, default_rect
        self.editing, self.focused = False, False
        self.box, self._press = (0, 0, 1, 1), (0, 0, (0, 0, 1, 1), "")
        top = self.top = tk.Toplevel(root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        self.c = tk.Canvas(top, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.c.bind("<Configure>", lambda _e: self._draw())
        self.c.bind("<Motion>", self._hover)
        self.c.bind("<ButtonPress-1>", self._grab)
        self.c.bind("<B1-Motion>", self._drag)
        top.bind("<FocusIn>", lambda _e: self._focus(True))
        top.bind("<FocusOut>", lambda _e: self._focus(False))
        for key in ("<Return>", "<KP_Enter>"):
            top.bind(key, lambda _e: self.confirm())
        top.bind("<Escape>", lambda _e: self.cancel())

    @property
    def screen(self) -> tuple[int, int]:
        return self.top.winfo_screenwidth(), self.top.winfo_screenheight()

    @property
    def visible(self) -> bool:
        return self.top.state() == "normal"

    def edit(self, rect: NormalizedRect | None) -> None:
        self.editing = True
        self.top.attributes("-transparentcolor", "")
        self.top.attributes("-alpha", 0.55)
        self._show(rect or self.default_rect)
        self.top.focus_force()

    def show(self, rect: NormalizedRect) -> None:
        """Confirmed look: see-through inside, faint outline, inert to input."""
        self.editing = False
        self.top.attributes("-alpha", 0.6)
        self.top.attributes("-transparentcolor", ROI_KEY)
        self._show(rect)

    def hide(self) -> None:
        self.editing = False
        self.top.withdraw()

    def confirm(self) -> None:
        if self.editing:
            rect = box_to_rect(self.box, self.screen)
            self.show(rect)
            self.on_confirm(rect)

    def cancel(self) -> None:
        if self.editing:
            self.hide()
            self.on_cancel()

    def keep_on_top(self) -> None:
        """Re-assert z-order and input/capture styles; Tk and games can reset them."""
        if not self.visible:
            return
        if self.editing:
            pin_on_top(self.top, _EX_TOOLWINDOW | _EX_TOPMOST, _EX_NOACTIVATE | _EX_TRANSPARENT)
        else:
            pin_on_top(self.top, _EX_NOACTIVATE | _EX_TOOLWINDOW | _EX_TOPMOST | _EX_TRANSPARENT)
        if not hide_from_capture(self.top) and not self.editing:
            self.top.withdraw()  # it sits right on the killfeed: never let the OCR read the outline

    def _show(self, rect: NormalizedRect) -> None:
        sw, sh = self.screen
        left, top, right, bottom = pixel_rect(rect, sw, sh)
        self.box = (left, top, right - left, bottom - top)
        self.top.geometry(f"{self.box[2]}x{self.box[3]}+{left}+{top}")
        self.top.deiconify()
        self.top.update_idletasks()
        self.keep_on_top()
        self._draw()

    def _focus(self, on: bool) -> None:
        self.focused = on
        self._draw()

    def _hover(self, e: tk.Event) -> None:
        if self.editing:
            self.c.configure(cursor=_CURSOR[edge_at(e.x, e.y, self.box[2], self.box[3])])

    def _grab(self, e: tk.Event) -> None:
        if self.editing:
            self.top.focus_force()
            self._press = (e.x_root, e.y_root, self.box, edge_at(e.x, e.y, self.box[2], self.box[3]))

    def _drag(self, e: tk.Event) -> None:
        if not self.editing:
            return
        x0, y0, box, edge = self._press
        self.box = drag_box(box, edge, e.x_root - x0, e.y_root - y0, self.screen)
        x, y, w, h = self.box
        self.top.geometry(f"{w}x{h}+{x}+{y}")

    def _draw(self) -> None:
        c, (_, _, w, h) = self.c, self.box
        c.delete("all")
        if not self.editing:
            c.configure(bg=ROI_KEY, cursor="")
            c.create_rectangle(1, 1, w - 1, h - 1, outline=ROI_LINE, width=2)
            return
        line = ROI_LINE if self.focused else DIM
        c.configure(bg=ROI_FILL)
        c.create_rectangle(1, 1, w - 1, h - 1, outline=line, width=3)
        c.create_line(w / 2, 0, w / 2, h, fill=line)  # crosshair, as in the drag selection
        c.create_line(0, h / 2, w, h / 2, fill=line)
        for hx, hy in ((0, 0), (w, 0), (0, h), (w, h)):
            c.create_rectangle(hx - GRIP, hy - GRIP, hx + GRIP, hy + GRIP, fill=line, outline="")
        hint = self.label + " · " + ("Enter 확정 · Esc 취소" if self.focused else "클릭하면 Enter/Esc 사용") + \
            " · 테두리: 크기 · 안쪽: 이동"
        _t(c, w / 2, 16, hint, 12, TEXT, anchor="center", bold=True)
        _t(c, w / 2, h - 14, f"{w}x{h}", 11, TEXT, anchor="center", num=True)


class App:
    """Cards window (main) + optional always-on-top overlay, both drawn from one Ledger."""

    def __init__(self, root: tk.Tk, events: queue.Queue[ConfirmedEvent | AliveCount], ledger: Ledger,
                 hotkey: Hotkey, overlay: bool, roi: NormalizedRect | None = None,
                 live: LiveCapture | None = None):
        families = set(tkfont.families(root))
        _FAM["ui"] = next((n for n in ("Malgun Gothic", "맑은 고딕", "Noto Sans KR", "Segoe UI")
                           if n in families), "TkDefaultFont")
        _FAM["num"] = next((n for n in ("Bahnschrift", "Consolas") if n in families), _FAM["ui"])
        self.root, self.events, self.led, self.hotkey = root, events, ledger, hotkey
        self.roi, self.live = roi, live
        self._rois: tuple[NormalizedRect | None, NormalizedRect | None] = (None, None)
        self.dirty, self._second, self._overlay_h = True, -1, 0
        root.title("killfeed-lens · 팀 현황")
        root.configure(bg=BG)
        root.geometry(f"{CARDS_SIZE[0]}x{CARDS_SIZE[1]}+80+40")
        bar = tk.Frame(root, bg=PANEL)
        bar.pack(fill="x")
        self.toggle = tk.Button(bar, command=self.toggle_overlay, bg=LINE, fg=TEXT, bd=0, padx=14,
                                pady=5, activebackground=FAINT, activeforeground=TEXT,
                                font=_f(12), cursor="hand2")
        self.toggle.pack(side="left", padx=10, pady=8)
        keys = f"{HOTKEY_LABEL}로 게임 중에도 켜고 끔" if hotkey.ok else f"{HOTKEY_LABEL} 등록 실패"
        tk.Label(bar, text=f"{keys} · PUBG는 '전체 창 모드(테두리 없음)'에서만 오버레이가 보임",
                 bg=PANEL, fg=DIM, font=_f(11)).pack(side="left", padx=4)
        if live:
            roi_bar = tk.Frame(root, bg=PANEL)
            roi_bar.pack(fill="x")
            for text, command in (("킬피드 ROI 조정", live.recalibrate),
                                  ("생존 인원 ROI 조정", live.recalibrate_alive),
                                  ("ROI 표시/숨김", live.toggle_box)):
                tk.Button(roi_bar, text=text, command=command, bg=LINE, fg=TEXT, bd=0, padx=14, pady=5,
                          activebackground=FAINT, activeforeground=TEXT, font=_f(12),
                          cursor="hand2").pack(side="left", padx=(10, 0), pady=(0, 8))
            tk.Label(roi_bar, text="생존 인원은 숫자만 지정 · Enter 확정 / Esc 취소",
                     bg=PANEL, fg=DIM, font=_f(11)).pack(side="left", padx=12, pady=(0, 8))
            self.track = tk.Button(bar, command=live.toggle_tracking, bg=LINE, fg=TEXT, bd=0,
                                   padx=14, pady=5, activebackground=FAINT, activeforeground=TEXT,
                                   font=_f(12), cursor="hand2")
            self.track.pack(side="right", padx=(0, 10), pady=8)
        self.status = tk.Label(bar, bg=PANEL, fg=TEXT, font=_f(11))
        self.status.pack(side="right", padx=4)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: setattr(self, "dirty", True))
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120),
                                                                            "units"))
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_command(label="오버레이 켜기/끄기", command=self.toggle_overlay)
        self.menu.add_command(label="닫기 (Esc)", command=root.destroy)
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        root.bind("<Key>", lambda e: root.destroy() if e.keysym in ("Escape", "q") else None)
        self._build_overlay()
        self.set_overlay(overlay)

    def _build_overlay(self) -> None:
        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=BG)
        top.attributes("-alpha", 0.92)
        top.attributes("-topmost", True)
        self.ov = tk.Canvas(top, width=OVERLAY_W, height=200, bg=BG, highlightthickness=0)
        self.ov.pack()
        self._pos = (top.winfo_screenwidth() - OVERLAY_W - 16, 80)
        self._off = (0, 0)
        self._place(200)
        self.ov.bind("<ButtonPress-1>", lambda e: setattr(
            self, "_off", (e.x_root - self._pos[0], e.y_root - self._pos[1])))
        self.ov.bind("<B1-Motion>", self._drag)
        self.ov.bind("<ButtonRelease-1>", lambda _e: self._place(self._overlay_h or 200))
        self.ov.bind("<Double-Button-1>", lambda _e: self.set_overlay(False))

    def _drag(self, e: tk.Event) -> None:
        self._pos = (e.x_root - self._off[0], e.y_root - self._off[1])
        self.top.geometry(f"+{self._pos[0]}+{self._pos[1]}")

    def _place(self, h: int) -> None:
        """Size the overlay and push it off the killfeed ROI (ROI is normalized to the captured
        monitor; assumed to be Tk's primary screen)."""
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        rects = [self.live.roi, self.live.alive_roi] if self.live else [self.roi]
        rois = [(r.x * sw, r.y * sh, r.w * sw, r.h * sh) for r in rects if r is not None]
        self._pos = clear_of_rois((*self._pos, OVERLAY_W, h), rois, (sw, sh))
        self.top.geometry(f"{OVERLAY_W}x{h}+{self._pos[0]}+{self._pos[1]}")

    @property
    def overlay_on(self) -> bool:
        return self.top.state() == "normal"

    def toggle_overlay(self) -> None:
        self.set_overlay(not self.overlay_on)

    def set_overlay(self, show: bool) -> None:
        if show:
            self.top.deiconify()  # never focus_force: the game must keep keyboard input
            pin_on_top(self.top)
        else:
            self.top.withdraw()
        self.toggle.configure(text="오버레이 끄기" if show else "오버레이 켜기")
        self.dirty = True

    def redraw(self, now: float) -> None:
        c = self.canvas
        c.delete("all")
        h = draw_cards(c, self.led, max(c.winfo_width(), 400), now)
        c.configure(scrollregion=(0, 0, c.winfo_width(), max(h, c.winfo_height())))
        if self.overlay_on:
            self.ov.delete("all")
            h = draw_overlay(self.ov, self.led, OVERLAY_W, now)
            if h != self._overlay_h:
                self._overlay_h = h
                self.ov.configure(height=h)
                self._place(h)

    def poll(self) -> None:
        changed = False
        try:
            while True:
                record = self.events.get_nowait()
                if isinstance(record, AliveCount):
                    self.led.observe_alive_count(record)
                else:
                    self.led.apply(record)
                changed = True
        except queue.Empty:
            pass
        now = time.monotonic()
        self.led.tick(now)
        if self.live and self.status["text"] != self.live.status:
            self.status.configure(text=self.live.status, fg=KILLC if self.live.error else TEXT)
        if self.live:
            text = "킬피드 추적 끄기" if self.live.tracking else "킬피드 추적 켜기"
            if self.track["text"] != text:
                self.track.configure(text=text)
        if self.hotkey.pressed.is_set():
            self.hotkey.pressed.clear()
            self.toggle_overlay()
        if int(now) != self._second and self.overlay_on:
            pin_on_top(self.top)  # games and other windows can push it down; re-assert each second
            hide_from_capture(self.top)  # needs the mapped hwnd, so not in set_overlay
        if int(now) != self._second and self.live:
            self.live.box.keep_on_top()
            self.live.alive_box.keep_on_top()
            rois = (self.live.roi, self.live.alive_roi)
            if rois != self._rois:  # re-calibrated: push the overlay off the new ROIs
                self._rois = rois
                self._place(self._overlay_h or 200)
        animating = any(now - t.touched < FLASH_S for t in self.led.teams.values() if t.touched)
        if changed or animating or self.dirty or int(now) != self._second:
            self._second, self.dirty = int(now), False
            self.redraw(now)
        self.root.after(80, self.poll)


def run(*, overlay: bool, demo: bool, jsonl: TextIO | None, speed: float = 1.0,
        teams: int | None = None, exit_after: float | None = None,
        roi: NormalizedRect | None = None, live_dir: Path | None = None,
        roi_profile: Path | None = None) -> None:
    if sys.platform == "win32":  # dxcam turns this on at the first grab anyway; Tk must agree from
        ctypes.windll.user32.SetProcessDPIAware()  # the start so ROI pixels match captured ones
    root = tk.Tk()
    events: queue.Queue[ConfirmedEvent | AliveCount] = queue.Queue()
    sink, stop, hotkey = GuiSink(events), threading.Event(), Hotkey()
    hotkey.start()
    ledger = Ledger(teams=teams or (25 if demo else None), scale=speed if demo else 1.0)
    live = None
    if live_dir is not None:
        from kfocr.ui.live import LiveCapture

        live = LiveCapture(root, sink, live_dir, roi_profile)
        root.after(300, live.begin)
    App(root, events, ledger, hotkey, overlay, roi, live).poll()
    if demo:
        threading.Thread(target=play_demo, args=(sink, speed, stop), daemon=True).start()
    elif jsonl is not None:
        threading.Thread(target=tail_jsonl, args=(sink, jsonl, stop), daemon=True).start()
    if exit_after is not None:
        root.after(int(exit_after * 1000), root.destroy)
    try:
        root.mainloop()
    finally:
        stop.set()
        if live:
            live.stop()  # finalizes the session; the window is already gone
        hotkey.stop()
