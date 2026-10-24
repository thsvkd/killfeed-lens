from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from itertools import combinations
from uuid import UUID, uuid4

from kfocr.domain.models import AliveCount, ConfirmedEvent, EventType
from kfocr.domain.tracker import name_key, same_teammate

SQUAD_SIZE = 4
# Per-life inference thresholds based on PUBG update 15.2 (pubg.com/en/news/1725).
# Update 23.2 changed stationary bleeding (pubg.com/en/news/6292); the feed cannot see movement.
# These are not guaranteed death times. No death row means an unconfirmed survival estimate.
KNOCK_SECONDS = (90, 30, 30, 10, 7, 5, 4, 3, 2)
KNOCK_TIMEOUT_NS = KNOCK_SECONDS[0] * 1_000_000_000
# Near-simultaneous sightings of a still-downed player are one knock, not another stack.
# Keep this short: a real revive and another knock can occur while the first row is on screen.
_KNOCK_DUPLICATE_NS = 3 * 1_000_000_000
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
# Recalls bring dead players back in waves (telemetry of 6 matches: +1..+7 on the HUD count, 2-4
# times a match). A larger rise is a misread, not a recall.
RECALL_MAX = 8
# A rise counts between counts the HUD held this long: one misread second (30 -> 24 -> 30) is no
# recall wave, and a recall and a death close together net out.
# ponytail: a misread held longer still counts as a recall; undo a held rise a held fall soon
# reverses if live matches show one.
_COUNT_HELD_NS = 3 * 1_000_000_000
# A dead player shown dying again this much later came back (recall) and died again; sooner is the
# same death read twice.
_DIED_AGAIN_NS = 30 * 1_000_000_000
_FULL_SQUAD_MATCH = 0.6  # same results from 0.5 to 0.8 on live matches 3 and 4
_LETHAL = {EventType.KILL, EventType.FINISH, EventType.BLEEDOUT, EventType.SUICIDE,
           EventType.ENVIRONMENT}
_CREDIT = {EventType.KILL, EventType.FINISH}


def _numbered(a: str, b: str) -> bool:
    """Names only their digits tell apart ("Tiger3"/"Tiger4"): squads number their names, so these
    are two players unless one is the other with a glued badge (tracker._same_player)."""
    ka, kb = name_key(a), name_key(b)
    return ka != kb and re.sub(r"\d", "", ka) == re.sub(r"\d", "", kb) and not same_teammate(a, b)


class Status(StrEnum):
    ALIVE = "alive"
    KNOCKED = "knocked"
    DEAD = "dead"


@dataclass
class Player:
    name: str
    status: Status = Status.ALIVE
    touched: float = 0.0  # time.monotonic() of the last change, for UI highlighting
    revived: float = 0.0  # time.monotonic() of the last observed comeback (not inferred expiry)
    knocked_ns: int = 0
    downs: int = 0  # knocks taken in this life; ordinary revives keep this, recalls reset it
    died_ns: int = 0  # feed time of the last death
    seen_ns: int = 0  # feed time it was last shown standing (acting, or knocked)
    sure: bool = True  # False: a knock ran out with no news (most likely revived, maybe dead)
    kills: int = 0  # credited to this player, so they follow it out of the unknown-badge bucket
    knocks: int = 0

    @property
    def knock_seconds(self) -> int:
        return KNOCK_SECONDS[min(max(self.downs, 1), len(KNOCK_SECONDS)) - 1]


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
        self._badge_conflicts: set[str] = set()
        self.alive_count: AliveCount | None = None
        self.alive_count_at = 0.0
        # Players the HUD count says came back (recall) that the board still has dead: until the
        # feed shows them, they hold their share of the count (draw()).
        self.returned = 0
        self._held: int | None = None  # the last count shown for _COUNT_HELD_NS
        self._shown_since = 0

    def observe_alive_count(self, reading: AliveCount) -> None:
        if self.alive_count is None or reading.count != self.alive_count.count:
            self._shown_since = reading.observed_at_ns
        elif reading.observed_at_ns - self._shown_since >= _COUNT_HELD_NS and reading.count != self._held:
            if self._held is not None and 0 < reading.count - self._held <= RECALL_MAX:
                dead = sum(t.dead for t in self.teams.values())
                self.returned = min(dead, self.returned + reading.count - self._held)
            self._held = reading.count
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
                for team in list(self.teams.values()):
                    self._squeeze(team)

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
        if same is None and members and key:
            # PUBG names have 4-16 characters, so a shorter read is part of one; and a squad holds
            # squad_size players, so a further name close to one of them is it read wrong (live
            # match 4: 5 of 30 squads grew a fifth member, a 2-4 character part or a name one
            # letter off). A further name unlike all of them is someone else under a misread
            # badge: merging those too cost 2.7 points of dead and eliminated on that match.
            def close(known: str) -> float:
                return SequenceMatcher(None, name_key(known), key).ratio()
            if len(name) < 4:
                same = max((known for known in members if key in name_key(known)), key=close, default=None)
            if same is None and len(members) >= self.squad_size:
                # No _numbered() guard here: a digit misread of a member outnumbers numbered
                # strangers (the guard cost live match 4 0.6 points of alive).
                best = max(members, key=close)
                if close(best) >= _FULL_SQUAD_MATCH:
                    same = best
        return same or name

    def _home(self, name: str | None, team: int | None) -> int | None:
        """A name already on another badged team is that player under a misread badge: PUBG names
        are unique (3 OBS recordings: 6 players read into a second team, the first badge right for
        all 6)."""
        known = self._team_of.get(name) if name and team is not None else None
        home = self.teams.get(known) if known is not None and known != team else None
        if home is not None and len(name_key(name)) >= 4 and name in home.players:
            self._badge_conflicts.add(name)
            return known
        return team

    def _squeeze(self, team: Team) -> None:
        """A squad holds squad_size players. Past that, one of them is a member read twice before
        the squad filled up (live match 4: a spelling one letter off, a 3-character part): a read
        too short to be a name goes to the member most like it, else the closest pair at
        _FULL_SQUAD_MATCH or more is one player, but never two names only their digits tell apart
        (squads number their names, tracker._same_player). Unrelated extra names are preserved in
        the unassigned bucket; a member with conflicting badge evidence moves there first.
        ponytail: a short read goes to the most alike member, often a guess; the team's dead count
        is what it gets right. Leaving out members seen standing after it cost live match 4 2.7
        points of dead."""
        def close(a: str, b: str) -> float:
            return SequenceMatcher(None, name_key(a), name_key(b)).ratio()

        def last(p: Player) -> int:
            return max(p.seen_ns, p.died_ns, p.knocked_ns)

        while team.id is not None and len(team.players) > self.squad_size:
            names = list(team.players)
            short = [n for n in names if len(n) < 4]
            if short:
                gone = short[0]
                keep = max((n for n in names if n != gone), key=lambda n: close(n, gone))
            else:
                best: tuple[float, str, str] | None = None
                for a, b in combinations(names, 2):
                    if not _numbered(a, b) and (best is None or close(a, b) > best[0]):
                        longer, shorter = sorted((a, b), key=lambda n: -len(name_key(n)))
                        best = (close(a, b), longer, shorter)
                if best is None or best[0] < _FULL_SQUAD_MATCH:
                    gone = next((n for n in names if n in self._badge_conflicts), names[-1])
                    moved = team.players.pop(gone)
                    self._team_of.pop(gone, None)
                    unknown = self._team(None)
                    unknown.players[gone] = moved
                    team.kills, team.knocks = team.kills - moved.kills, team.knocks - moved.knocks
                    unknown.kills, unknown.knocks = unknown.kills + moved.kills, unknown.knocks + moved.knocks
                    continue
                _, keep, gone = best
            kept, dropped = team.players[keep], team.players.pop(gone)
            same_life = kept.died_ns == dropped.died_ns
            downs = max(kept.downs, dropped.downs) if same_life else dropped.downs
            if last(dropped) > last(kept):  # the latest read tells its state
                if kept.status is Status.DEAD and dropped.status is not Status.DEAD:
                    self.returned = max(0, self.returned - 1)  # back (recall), as apply() counts it
                kept.status, kept.died_ns, kept.knocked_ns = dropped.status, dropped.died_ns, dropped.knocked_ns
                kept.seen_ns, kept.sure = dropped.seen_ns, dropped.sure
                kept.downs = downs
            elif same_life:
                kept.downs = max(kept.downs, dropped.downs)  # aliases are not separate knocks
            self._team_of.pop(gone, None)
            kept.touched, kept.revived = max(kept.touched, dropped.touched), max(kept.revived, dropped.revived)
            kept.kills, kept.knocks = kept.kills + dropped.kills, kept.knocks + dropped.knocks

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
        if player.status is Status.DEAD:
            player.downs = 0
            player.knocked_ns = 0
        player.status = Status.ALIVE
        player.touched = player.revived = team.touched = now

    def expire(self, now_ns: int, now: float) -> None:
        """Knocked players with no death by their per-life threshold are estimated alive (the feed never
        shows revives; telemetry: revives take 13-99 s, median 28 s), but a death the reader missed
        looks the same: unconfirmed until the feed shows them again, they are the first drawn
        unsure when the HUD count has no room for everyone (draw())."""
        for team in self.teams.values():
            for player in team.players.values():
                if (player.status is Status.KNOCKED
                        and now_ns - player.knocked_ns >= player.knock_seconds * 1_000_000_000):
                    player.status = Status.ALIVE
                    player.touched = team.touched = now
                    player.sure = False

    def tick(self, now: float) -> None:
        """Advance the feed clock between events so quiet periods still expire knocks."""
        if self.first_ns is not None:
            self.expire(self.last_ns + int((now - self.last_at) * 1e9 * self.scale), now)

    def apply(self, event: ConfirmedEvent) -> None:
        if any(e.event_id == event.event_id for e in self.recent):
            return
        now = self.last_at = time.monotonic()
        ns = event.observed_at_ns
        self.last_ns = max(self.last_ns, ns)
        if self.first_ns is None:
            self.first_ns = ns
        self.recent.append(event)
        self.expire(self.last_ns, now)
        kind = event.event_type
        if kind is EventType.UNKNOWN:
            return
        self._saw_badges(ns, event.killer_team, event.victim_team)
        killer_team = self._home(event.killer_name, event.killer_team)
        victim_team = self._home(event.victim_name, event.victim_team)
        killer_name = self._resolve(event.killer_name, killer_team)
        victim_name = self._resolve(event.victim_name, victim_team)
        killer_key = self._attach(killer_name, killer_team)
        victim_key = self._attach(victim_name, victim_team)

        if kind in _CREDIT or kind is EventType.KNOCK:
            if killer_name or killer_key is not None:
                killer = self._team(killer_key)
                killer.touched, killer.last_ns = now, ns
                player = None
                if killer_name:
                    player = killer.players.setdefault(killer_name, Player(killer_name))
                    # A delayed finish/bleed-out still credits its dead or downed attacker. A
                    # new knock is standing evidence; a HUD recall also permits a new-life kill.
                    standing = kind is not EventType.FINISH and (
                                player.status is Status.ALIVE or ns > player.died_ns
                                and (kind is EventType.KNOCK or player.status is Status.DEAD
                                     and self.returned > 0))
                    if standing:
                        if player.status is Status.DEAD:
                            self.returned = max(0, self.returned - 1)
                        if player.status is not Status.ALIVE:
                            self._revive(killer, player, now)
                        player.seen_ns, player.sure = ns, True
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
                if player.status is Status.DEAD and ns > player.died_ns:
                    self.returned = max(0, self.returned - 1)
                    self._revive(victim, player, now)
                duplicate = (player.downs > 0 and ns <= player.knocked_ns
                             or player.status is Status.KNOCKED
                             and ns - player.knocked_ns < _KNOCK_DUPLICATE_NS)
                if player.status is not Status.DEAD and not duplicate:
                    player.downs += 1
                    player.status, player.touched, player.knocked_ns = Status.KNOCKED, now, ns
                    player.seen_ns, player.sure = ns, True
            elif kind in _LETHAL and player.status is not Status.DEAD:
                player.status, player.touched, player.died_ns = Status.DEAD, now, ns
            elif kind in _LETHAL and ns - player.died_ns >= _DIED_AGAIN_NS:  # came back, died again
                self.returned = max(0, self.returned - 1)
                player.died_ns = ns
                player.downs = 0  # a second death belongs to another life
        for key in {killer_key, victim_key} - {None}:
            if key in self.teams:
                self._squeeze(self.teams[key])
                self._bleed_out(self.teams[key], now, ns)

    @staticmethod
    def _bleed_out(team: Team, now: float, ns: int) -> None:
        if team.eliminated:  # whole squad down: knocked members bleed out with it
            for member in team.players.values():
                if member.status is Status.KNOCKED:
                    member.status, member.touched, member.died_ns = Status.DEAD, now, ns

    def amend(self, event: ConfirmedEvent) -> None:
        """An applied event's badges read late: its unknown-team players go home with their
        status and kills, as a later sighting with the badge would move them."""
        self._saw_badges(event.observed_at_ns, event.killer_team, event.victim_team)
        for name, team in ((event.killer_name, event.killer_team), (event.victim_name, event.victim_team)):
            if team is not None:
                team = self._home(name, team)
                self._attach(self._resolve(name, team), team)
                if (home := self.teams.get(team)) is not None:  # no empty card for a misread
                    self._squeeze(home)
                    self._bleed_out(home, time.monotonic(), event.observed_at_ns)
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

    def draw(self) -> dict[int | None, tuple[dict[str, str], int]]:
        """What the board draws for each team: every player's state (alive, knocked, dead or
        unsure) and how many of its never-seen slots show assumed alive (the rest unsure).

        With the HUD count (it counts knocked players too) alive marks cannot outnumber the players
        alive: the count less the recalled players the board still has dead goes to knocked
        players first, then to players by how recently the feed showed them standing (unconfirmed
        ones last), then to never-seen slots of the teams seen standing, most recent first, then to
        players whose badge was not read. Whoever it leaves no room for is drawn unsure: deaths
        the feed or the reader missed, members of underfilled squads (live match 4 endgame: 2
        alive, the board drew 35 alive marks). On 3 live matches a team's drawn standing marks equal its players alive 67-79% of the time
        (was 67-74%); drawing ran-out knocks unsure regardless cost up to 4 points of that (most
        of them were revived).
        Without the count: never-seen slots stay assumed unless the team may be wiped."""
        out: dict[int | None, tuple[dict[str, str], int]] = {}
        for t in self.teams.values():
            states = {n: p.status.value for n, p in t.players.items()}
            open_slots = 0 if t.id is None or t.eliminated else t.total - len(t.players)
            out[t.id] = (states, 0 if t.maybe_out else open_slots)
        if self.alive_count is None:
            return out
        budget = max(0, self.alive_count.count - self.returned)
        live = [t for t in self.teams.values() if t.id is not None and not t.eliminated]
        standing = sorted(((t, n, p) for t in live for n, p in t.players.items()
                           if p.status is not Status.DEAD),
                          key=lambda x: (x[2].status is not Status.KNOCKED, not x[2].sure, -x[2].seen_ns))
        drawn: set[int | None] = set()
        for t, n, _ in standing:
            if budget > 0:
                budget -= 1
                drawn.add(t.id)
            else:
                out[t.id][0][n] = "unsure"
        for t in sorted(live, key=lambda t: (t.id not in drawn, -t.last_ns)):
            states, _ = out[t.id]
            slots = 0 if t.maybe_out else t.total - len(t.players)
            out[t.id] = (states, min(slots, budget))
            budget -= out[t.id][1]
        # Unread badges: most are members of the teams above, so only what is left over.
        for n, st in (out[None][0] if None in out else {}).items():
            if st != "dead":
                if budget > 0:
                    budget -= 1
                else:
                    out[None][0][n] = "unsure"
        return out

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
