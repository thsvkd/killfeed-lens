from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from kfocr.domain.models import AliveCount, ConfirmedEvent, EventType
from kfocr.domain.tracker import name_key, same_teammate

SQUAD_SIZE = 4
# A knocked player either dies (feed shows it) or is revived (the feed never shows that). PUBG's
# exact bleed-out time is not documented (pubg.wiki.gg/wiki/DBNO only says it drains and speeds up
# with repeated knocks), so use a conservative bound.
# ponytail: a death the OCR missed shows as alive after this; make it per-knock-count if measured.
KNOCK_TIMEOUT_NS = 90 * 1_000_000_000
_LETHAL = {EventType.KILL, EventType.FINISH, EventType.BLEEDOUT, EventType.SUICIDE,
           EventType.ENVIRONMENT}
_CREDIT = {EventType.KILL, EventType.FINISH}


class Status(StrEnum):
    ALIVE = "alive"
    KNOCKED = "knocked"
    DEAD = "dead"


@dataclass
class Player:
    name: str
    status: Status = Status.ALIVE
    touched: float = 0.0  # time.monotonic() of the last change, for UI highlighting
    revived: float = 0.0  # time.monotonic() of the last comeback (revive/recall/AED/expiry)
    knocked_ns: int = 0


@dataclass
class Team:
    """`id` is the on-screen team badge number; None collects events whose badge was unreadable."""

    id: int | None
    squad_size: int = SQUAD_SIZE
    players: dict[str, Player] = field(default_factory=dict)
    kills: int = 0
    knocks: int = 0  # knocks dealt
    touched: float = 0.0
    last_ns: int = 0

    def _count(self, status: Status) -> int:
        return sum(p.status is status for p in self.players.values())

    @property
    def dead(self) -> int:
        return self._count(Status.DEAD)

    @property
    def knocked(self) -> int:
        return self._count(Status.KNOCKED)

    @property
    def total(self) -> int:
        # Unseen squad members are assumed to exist; the unknown bucket assumes nothing.
        return len(self.players) if self.id is None else max(self.squad_size, len(self.players))

    @property
    def eliminated(self) -> bool:
        """Nobody left standing: every squad slot is dead or knocked (unseen slots count as alive)."""
        return self.id is not None and self.dead + self.knocked >= self.squad_size

    @property
    def remaining(self) -> int:
        return 0 if self.eliminated else self.total - self.dead

    def pips(self) -> list[tuple[Status, str | None]]:
        """One entry per squad slot; slots never seen in the feed are (ALIVE, None) = assumed."""
        seen: list[tuple[Status, str | None]] = [(p.status, p.name) for p in self.players.values()]
        return seen + [(Status.ALIVE, None)] * (self.total - len(seen))


class Ledger:
    """Folds confirmed killfeed events into per-team state.

    `teams` (start team count) is optional: with it, never-seen teams count as full squads;
    without it the totals only cover teams that appeared in the feed.  `scale` is feed-seconds per
    wall-second (only the demo runs faster than real time).
    """

    def __init__(self, squad_size: int = SQUAD_SIZE, teams: int | None = None, keep: int = 300,
                 scale: float = 1.0):
        self.squad_size = squad_size
        self.start_teams = teams
        self.scale = scale
        self.teams: dict[int | None, Team] = {}
        self.recent: deque[ConfirmedEvent] = deque(maxlen=keep)
        self.first_ns: int | None = None
        self.last_ns = 0
        self.last_at = 0.0  # time.monotonic() of the latest event, for UI highlighting
        self._team_of: dict[str, int] = {}
        self.alive_count: AliveCount | None = None
        self.alive_count_at = 0.0

    def observe_alive_count(self, reading: AliveCount) -> None:
        self.alive_count = reading
        self.alive_count_at = time.monotonic()

    # --- lookup -------------------------------------------------------------------------
    def team_of(self, name: str | None, team: int | None) -> int | None:
        if team is not None:
            return team
        return self._team_of.get(name) if name else None

    def _team(self, key: int | None) -> Team:
        if key not in self.teams:
            self.teams[key] = Team(key, self.squad_size)
        return self.teams[key]

    def _resolve(self, name: str | None, team: int | None) -> str | None:
        """Map an OCR spelling variant onto the player already on the board: an exact name
        first, then the same folded spelling, then (within a known squad) a partial read."""
        if not name:
            return name
        members = self.teams[team].players if team is not None and team in self.teams else {}
        orphans = self.teams[None].players if None in self.teams else {}
        if name in members or name in orphans:
            return name
        key = name_key(name)
        pool = [*orphans] if team is not None else [*orphans, *self._team_of]
        same = next((known for known in pool if key and name_key(known) == key), None)
        if same is None:
            same = next((known for known in members if same_teammate(known, name)), None)
        return same or name

    def _attach(self, name: str | None, team: int | None) -> int | None:
        """Resolve a player to a team, learning name->team and re-homing unknown-team players."""
        if team is None:
            return self.team_of(name, None)
        if name:
            self._team_of[name] = team
            orphans = self.teams.get(None)
            if orphans and name in orphans.players:
                self._team(team).players.setdefault(name, orphans.players.pop(name))
                if not (orphans.players or orphans.kills or orphans.knocks):
                    del self.teams[None]
        return team

    # --- aggregation --------------------------------------------------------------------
    def _revive(self, team: Team, player: Player, now: float) -> None:
        player.status = Status.ALIVE
        player.touched = player.revived = team.touched = now

    def expire(self, now_ns: int, now: float) -> None:
        """Knocked players with no death in KNOCK_TIMEOUT_NS are assumed revived."""
        for team in self.teams.values():
            for player in team.players.values():
                if player.status is Status.KNOCKED and now_ns - player.knocked_ns >= KNOCK_TIMEOUT_NS:
                    self._revive(team, player, now)

    def tick(self, now: float) -> None:
        """Advance the feed clock between events so quiet periods still expire knocks."""
        if self.first_ns is not None:
            self.expire(self.last_ns + int((now - self.last_at) * 1e9 * self.scale), now)

    def apply(self, event: ConfirmedEvent) -> None:
        now = self.last_at = time.monotonic()
        ns = self.last_ns = event.observed_at_ns
        if self.first_ns is None:
            self.first_ns = ns
        self.recent.append(event)
        self.expire(ns, now)
        kind = event.event_type
        if kind is EventType.UNKNOWN:
            return
        killer_name = self._resolve(event.killer_name, event.killer_team)
        victim_name = self._resolve(event.victim_name, event.victim_team)
        killer_key = self._attach(killer_name, event.killer_team)
        victim_key = self._attach(victim_name, event.victim_team)

        if kind in _CREDIT or kind is EventType.KNOCK:
            if killer_name or killer_key is not None:
                killer = self._team(killer_key)
                killer.touched, killer.last_ns = now, ns
                if killer_name:
                    player = killer.players.setdefault(killer_name, Player(killer_name))
                    if player.status is not Status.ALIVE:  # acting players are alive, however we got here
                        self._revive(killer, player, now)
                friendly = killer_key is not None and killer_key == victim_key
                if not friendly:
                    if kind in _CREDIT:
                        killer.kills += 1
                    else:
                        killer.knocks += 1

        if victim_name or victim_key is not None:
            victim = self._team(victim_key)
            victim.touched, victim.last_ns = now, ns
            name = victim_name or f"?{len(victim.players) + 1}"
            player = victim.players.setdefault(name, Player(name))
            if kind is EventType.KNOCK:
                if player.status is Status.DEAD:  # knocked again => it came back (recall/AED/revive)
                    self._revive(victim, player, now)
                if player.status is not Status.DEAD:  # a repeat knock restarts the bleed-out clock
                    player.status, player.touched, player.knocked_ns = Status.KNOCKED, now, ns
            elif kind in _LETHAL and player.status is not Status.DEAD:
                player.status, player.touched = Status.DEAD, now
            if victim.eliminated:  # whole squad down: knocked members bleed out with it
                for member in victim.players.values():
                    if member.status is Status.KNOCKED:
                        member.status, member.touched = Status.DEAD, now

    # --- views --------------------------------------------------------------------------
    def ranked(self) -> list[Team]:
        """Alive teams first by remaining players, then kills; eliminated teams last."""
        def key(t: Team):
            return (t.eliminated, -t.remaining, -t.kills, t.id is None, t.id or 0)
        return sorted(self.teams.values(), key=key)

    def _known(self) -> list[Team]:
        return [t for t in self.teams.values() if t.id is not None]

    @property
    def teams_left(self) -> int:
        if self.start_teams:
            return max(0, self.start_teams - sum(t.eliminated for t in self._known()))
        return sum(not t.eliminated for t in self._known())

    @property
    def players_left(self) -> int:
        if self.alive_count is not None:
            return self.alive_count.count
        seen = self._known()
        unseen = max(0, (self.start_teams or 0) - len(seen)) * self.squad_size
        return sum(t.remaining for t in seen) + unseen

    @property
    def knocked_now(self) -> int:
        return sum(t.knocked for t in self.teams.values() if not t.eliminated)

    @property
    def total_kills(self) -> int:
        return sum(t.kills for t in self.teams.values())

    @property
    def basis(self) -> str:
        if self.alive_count is not None:
            age = int(max(0, time.monotonic() - self.alive_count_at))
            freshness = f" ({age}초 전 관측)" if age >= 5 else ""
            return f"전체 인원: 화면 숫자{freshness} · 팀별 인원·남은 팀: 추정"
        if self.start_teams:
            return f"시작 {self.start_teams}팀 x {self.squad_size}명 가정"
        return f"관측된 {len(self._known())}팀 기준 (미관측 인원은 생존 가정)"


def make_event(
    killer: str | None,
    killer_team: int | None,
    victim: str | None,
    victim_team: int | None,
    kind: EventType = EventType.KILL,
    ns: int = 0,
    weapon: str | None = None,
    headshot: bool | None = None,
) -> ConfirmedEvent:
    """Build a ConfirmedEvent carrying only what the GUI cares about (demo, JSONL replay, tests)."""
    return ConfirmedEvent(
        observed_at_ns=ns, frame_seq=0, killer_name_raw=killer, victim_name_raw=victim,
        killer_name=killer, victim_name=victim, killer_team=killer_team, victim_team=victim_team,
        event_type=kind, weapon_label=weapon, headshot=headshot, confidence_row=1.0,
        confidence_killer=1.0, confidence_victim=1.0, confidence_event_type=1.0,
        confidence_total=1.0, source_bbox=(0, 0, 0, 0), event_id=uuid4(),
        session_id=UUID(int=0), confirmed_at_ns=ns, dedup_key="", tracker_evidence_count=1,
    )
