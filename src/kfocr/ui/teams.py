from __future__ import annotations

import math
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
# Badges number the teams 1..N in a lobby of about 100 players, so the highest badge seen tells
# squads (~25 teams) from duos (~50) and solos. A badge counts once it was seen again at least a
# second later: one bad frame misreads every row on it alike (38 -> 88 on two rows of one frame).
# ponytail: assumes a full public lobby; pass squad_size/teams for custom or small matches.
LOBBY_PLAYERS = 100
_BADGE_APART_NS = 1_000_000_000
# Teams that leave the lobby keep their numbers (2 of 27 squads and 2 of 53 duos on two recorded
# matches), so up to this many never-seen badges may be no team at all.
# ponytail: measured on two matches; retune with more telemetry.
LOBBY_LEAVERS = 2
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
    died_ns: int = 0  # feed time of the last death
    kills: int = 0  # credited to this player, so they follow it out of the unknown-badge bucket
    knocks: int = 0


@dataclass
class Team:
    """`id` is the on-screen team badge number; None collects events whose badge was unreadable."""

    id: int | None
    squad_size: int = SQUAD_SIZE
    players: dict[str, Player] = field(default_factory=dict)
    kills: int = 0
    knocks: int = 0  # knocks dealt
    touched: float = 0.0
    attacked: float = 0.0  # time.monotonic() of the last kill/knock it dealt (UI colours it)
    hit: float = 0.0  # ... and of the last one it took
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
    def maybe_out(self) -> bool:
        """Every seen member is down but a full squad was never seen: the feed cannot tell a wiped,
        underfilled team from one whose unseen member is alive. A member knocked after the last
        death seen rules it out: PUBG knocks a player only while a teammate still stands (the last
        one standing dies at once), and comebacks (revive, AED) need one too; a death after the
        knock may have been that teammate's. Narrowing this (e.g. one unseen slot at most)
        dropped range coverage from 23/23 to 8/23 checkpoints on a squad match."""
        players = self.players.values()
        if (self.id is None or not self.players or self.eliminated
                or len(self.players) >= self.squad_size
                or any(p.status is Status.ALIVE for p in players)):
            return False
        last_death = max((p.died_ns for p in players if p.status is Status.DEAD), default=-1)
        return not any(p.status is Status.KNOCKED and p.knocked_ns > last_death for p in players)

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
    without it the highest confirmed badge stands in for it, and before any badge the totals only
    cover teams that appeared in the feed. `squad_size` left out is inferred from the badges.
    `scale` is feed-seconds per wall-second (only the demo runs faster than real time).
    """

    def __init__(self, squad_size: int | None = None, teams: int | None = None, keep: int = 300,
                 scale: float = 1.0):
        self.fixed_squad = squad_size is not None
        self.squad_size = squad_size or SQUAD_SIZE
        self.start_teams = teams
        self.top_badge = 0
        self._badge_seen: dict[int, int] = {}
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

    def _saw_badges(self, ns: int, *badges: int | None) -> None:
        for badge in {b for b in badges if b is not None}:
            first = self._badge_seen.setdefault(badge, ns)
            if ns - first < _BADGE_APART_NS or badge <= self.top_badge:
                continue
            self.top_badge = badge
            # ponytail: a misread repeated over a second ("19" read "49" twice) still shrinks the
            # squad for good. Waiting for a second team's badge cost the duo replay 2.5 points of
            # dead/eliminated; revisit if a live match shows a wrong squad size.
            ratio = LOBBY_PLAYERS / badge
            size = SQUAD_SIZE if ratio >= 3 else 2 if ratio >= 1.5 else 1
            if not self.fixed_squad and size < self.squad_size:
                self.squad_size = size
                for team in self.teams.values():
                    team.squad_size = size

    @property
    def team_count(self) -> int:
        """The start team count teams_left counts down from: given, else the highest confirmed
        badge (0: unknown yet)."""
        return self.start_teams or self.top_badge

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
                moved, home = orphans.players.pop(name), self._team(team)
                kept = home.players.setdefault(name, moved)
                if kept is not moved:
                    kept.kills, kept.knocks = kept.kills + moved.kills, kept.knocks + moved.knocks
                home.kills, home.knocks = home.kills + moved.kills, home.knocks + moved.knocks
                orphans.kills, orphans.knocks = orphans.kills - moved.kills, orphans.knocks - moved.knocks
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
        self._saw_badges(ns, event.killer_team, event.victim_team)
        killer_name = self._resolve(event.killer_name, event.killer_team)
        victim_name = self._resolve(event.victim_name, event.victim_team)
        killer_key = self._attach(killer_name, event.killer_team)
        victim_key = self._attach(victim_name, event.victim_team)

        if kind in _CREDIT or kind is EventType.KNOCK:
            if killer_name or killer_key is not None:
                killer = self._team(killer_key)
                killer.touched, killer.last_ns = now, ns
                player = None
                if killer_name:
                    player = killer.players.setdefault(killer_name, Player(killer_name))
                    if player.status is not Status.ALIVE:  # acting players are alive, however we got here
                        self._revive(killer, player, now)
                friendly = killer_key is not None and killer_key == victim_key
                if not friendly:
                    killer.attacked = now
                    if kind in _CREDIT:
                        killer.kills += 1
                        if player:
                            player.kills += 1
                    else:
                        killer.knocks += 1
                        if player:
                            player.knocks += 1

        if victim_name or victim_key is not None:
            victim = self._team(victim_key)
            victim.touched, victim.hit, victim.last_ns = now, now, ns
            name = victim_name or f"?{len(victim.players) + 1}"
            player = victim.players.setdefault(name, Player(name))
            if kind is EventType.KNOCK:
                if player.status is Status.DEAD:  # knocked again => it came back (recall/AED/revive)
                    self._revive(victim, player, now)
                if player.status is not Status.DEAD:  # a repeat knock restarts the bleed-out clock
                    player.status, player.touched, player.knocked_ns = Status.KNOCKED, now, ns
            elif kind in _LETHAL and player.status is not Status.DEAD:
                player.status, player.touched, player.died_ns = Status.DEAD, now, ns
            self._bleed_out(victim, now)

    @staticmethod
    def _bleed_out(team: Team, now: float) -> None:
        if team.eliminated:  # whole squad down: knocked members bleed out with it
            for member in team.players.values():
                if member.status is Status.KNOCKED:
                    member.status, member.touched = Status.DEAD, now

    def amend(self, event: ConfirmedEvent) -> None:
        """An applied event's badges read late: its unknown-team players go home with their
        status and kills, as a later sighting with the badge would move them."""
        self._saw_badges(event.observed_at_ns, event.killer_team, event.victim_team)
        for name, team in ((event.killer_name, event.killer_team), (event.victim_name, event.victim_team)):
            if team is not None:
                self._attach(self._resolve(name, team), team)
                if (home := self.teams.get(team)) is not None:  # no empty card for a misread
                    self._bleed_out(home, time.monotonic())
        for i, shown in enumerate(self.recent):
            if shown.event_id == event.event_id:
                self.recent[i] = event

    # --- views --------------------------------------------------------------------------
    def ranked(self) -> list[Team]:
        """Live teams first, the ones the board knows best on top (the overlay shows those
        first): fewest never-seen slots, most remaining players, most kills; then the teams that
        may be wiped and the unknown-badge bucket; eliminated teams last."""
        def key(t: Team):
            return (t.eliminated, t.id is None, t.maybe_out, t.total - len(t.players),
                    -t.remaining, -t.kills, t.id or 0)
        return sorted(self.teams.values(), key=key)

    def _known(self) -> list[Team]:
        return [t for t in self.teams.values() if t.id is not None]

    @property
    def placed(self) -> int:
        """Players the feed shows alive or knocked in badged teams still in. Some can be deaths the
        reader missed: more of them than the HUD count proves that."""
        return sum(p.status is not Status.DEAD for t in self._known() if not t.eliminated
                   for p in t.players.values())

    def _hud_floor(self) -> int:
        return math.ceil(self.alive_count.count / self.squad_size) if self.alive_count else 0

    @property
    def teams_left(self) -> int:
        known = self._known()
        if not self.team_count:
            return sum(not t.eliminated for t in known)
        left = max(0, max(self.team_count, len(known)) - sum(t.eliminated for t in known))
        if self.alive_count is None:
            return left
        # A team nobody standing was seen in (may be wiped, never seen) is still in only if one of
        # the HUD's players the board cannot place is in it, and those mostly fill the never-seen
        # slots of the teams seen standing (their teammates): take 90% of those slots as filled.
        # ponytail: a likelihood, not a bound. On 6 matches it kept the true count in range wherever
        # the plain bound did, also on a perfect reading of the feed (taking all of them as filled
        # undercut it once there), and narrowed the range (live 3: mean width 9.9 -> 5.8). Lower it
        # if a match misses. A team known only by a killer's badge (name unread) stands too.
        count, live = self.alive_count.count, [t for t in known if not t.eliminated]
        standing = [t for t in live if not t.players
                    or any(p.status is not Status.DEAD for p in t.players.values())]
        others = len(live) - len(standing) + max(0, self.team_count - len(known))
        filled = sum(t.total - len(t.players) for t in standing) * 9 // 10
        rest = max(0, count - self.placed - filled)
        return max(self._hud_floor(), min(count, len(standing) + min(others, rest)))  # <= left

    @property
    def teams_left_min(self) -> int:
        """With the HUD count: every team left has someone alive, so at least count / squad size.
        Teams the feed shows standing do not raise it, some of them are deaths the reader missed
        (live match 3: a floor from them was wrong at 17 of 22 checkpoints; given the true HUD
        count, this one keeps the true teams left in range at 90-100% of checkpoints on 3 live and
        3 recorded matches).
        Without it: teams_left without the teams that may already be wiped (Team.maybe_out) and,
        while the top badge stands in for the team count, the lobby leavers among never-seen badges."""
        if self.alive_count is not None and self.team_count:
            return self._hud_floor()
        leavers = 0
        if not self.start_teams:
            unseen = sum(b not in self._badge_seen for b in range(1, self.top_badge + 1))
            leavers = min(LOBBY_LEAVERS, unseen)
        return max(0, self.teams_left - sum(t.maybe_out for t in self._known()) - leavers)

    @property
    def players_left(self) -> int:
        if self.alive_count is not None:
            return self.alive_count.count
        seen = self._known()
        unseen = max(0, self.team_count - len(seen)) * self.squad_size
        # Lobbies run short teams (a live squad match showed badges up to 30, i.e. 120 slots), so
        # full unseen squads can add up past the lobby: cap at the lobby less the deaths seen.
        dead = sum(t.dead for t in self.teams.values())
        return max(0, min(sum(t.remaining for t in seen) + unseen, LOBBY_PLAYERS - dead))

    @property
    def unseen_unsure(self) -> bool:
        """The HUD count leaves room for fewer than half of all never-seen squad slots to be alive
        (lobbies run short teams, and the feed misses deaths), so they are drawn unsure instead of
        assumed alive.
        ponytail: one share for every team; per-team odds would need team sizes the feed never shows."""
        if self.alive_count is None or not self.team_count:
            return False
        known = self._known()
        slots = sum(t.total - len(t.players) for t in known if not t.eliminated)
        slots += max(0, self.team_count - len(known)) * self.squad_size
        return slots > 0 and self.alive_count.count - self.placed < slots / 2

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
        if self.top_badge:
            return f"팀 번호 최대 {self.top_badge}번 기준 {self.top_badge}팀 x {self.squad_size}명 추정"
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
