"""Score the team board the GUI would show against official Telemetry at fixed checkpoints.

The board is rebuilt from the session's confirmed events exactly as the live GUI builds it; the
truth is replayed from Telemetry kills, knocks and revives. On-screen badge numbers are not
Telemetry teamIds, so each board team is mapped to the Telemetry team most of its players are in.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any

from kfocr.domain.models import ConfirmedEvent, EventType, Session
from kfocr.domain.tracker import name_key
from kfocr.telemetry.mapping import TelemetryCanonicalEvent, _parse_timestamp
from kfocr.ui.teams import Ledger, Status, make_event

CHECKPOINT_SECONDS = 60
# The board trails the screen by row confirmation and OCR time; read it this much later.
BOARD_LAG_SECONDS = 3.0
_NAME_MATCH_MIN = 0.75  # board spellings carry glued badges and look-alike glyphs


def team_state_report(session: Session, observed: list[ConfirmedEvent], raw: list[dict[str, Any]],
                      canonical: list[TelemetryCanonicalEvent], offset_seconds: float) -> dict[str, Any]:
    """`canonical` is map_telemetry(raw, viewer=...): the events the viewer's feed showed."""
    if session.ended_at_utc is None:
        raise ValueError("Team-state check requires a known session end time")
    members, team_of = _rosters(raw)
    if not members:
        return {"available": False, "reason": "Telemetry has no LogMatchStart roster"}
    timeline = _timeline(raw)
    visible = {e.raw_index for e in canonical}
    events = sorted(observed, key=lambda e: e.observed_at_ns)
    # The oracle board gets every event the viewer's feed showed, read perfectly, plus the true
    # lobby shape: what the board logic could show at best.
    start = next((e for e in raw if e.get("_T") == "LogMatchStart"), {})
    squad = start.get("teamSize") if isinstance(start.get("teamSize"), int) else None
    oracle_events = _oracle_events(session, canonical, team_of, offset_seconds)
    span = (session.ended_at_utc - session.started_at_utc).total_seconds()
    marks = [*range(CHECKPOINT_SECONDS, int(span), CHECKPOINT_SECONDS), span]

    checks: Counter[str] = Counter()
    rows = []
    board, oracle = Ledger(), Ledger(squad_size=squad, teams=len(members))
    feeds = [(board, events, [0]), (oracle, oracle_events, [0])]
    for mark in marks:
        board_ns = session.timeline_origin_ns + int((mark + BOARD_LAG_SECONDS) * 1e9)
        for ledger, feed, applied in feeds:
            while applied[0] < len(feed) and feed[applied[0]].observed_at_ns <= board_ns:
                ledger.apply(feed[applied[0]])
                applied[0] += 1
            ledger.expire(board_ns, 0.0)
        at = session.started_at_utc + timedelta(seconds=mark + offset_seconds)
        status, kills, seen = _truth_at(timeline, visible, team_of, at)
        mapped = _map_board(board, team_of)
        teams_left = sum(any(status.get(n, Status.ALIVE) is not Status.DEAD for n in names)
                         for names in members.values())
        mismatched = []
        for team in sorted(seen):
            names = members[team]
            truth_dead = sum(status.get(n) is Status.DEAD for n in names)
            truth = (truth_dead, kills[team], truth_dead == len(names))
            mine = _team_view([board.teams[k] for k, target in mapped.items() if target == team])
            best = _team_view([oracle.teams[team]] if team in oracle.teams else [])
            checks.update(teams=1)
            for label, got, want in (("truth", mine, truth), ("oracle", mine, best),
                                     ("ceiling", best, truth)):
                checks.update({f"{label}_{field}": got[i] == want[i]
                               for i, field in enumerate(("dead", "kills", "eliminated"))})
            if mine != truth:
                mismatched.append({"telemetry_team": team, "board_badges": sorted(
                    key for key, target in mapped.items() if target == team),
                    "dead": [mine[0], truth[0]], "kills": [mine[1], kills[team]],
                    "eliminated": [mine[2], truth[2]], "oracle": list(best)})
        # Until a confirmed top badge sets the lobby size the board shows "—" (app/common.js).
        shown = bool(board.team_count)
        checks.update(checkpoints=1, teams_left_shown=shown,
                      teams_left=shown and board.teams_left_min <= teams_left <= board.teams_left,
                      oracle_teams_left=oracle.teams_left_min <= teams_left <= oracle.teams_left)
        rows.append({"video_seconds": round(mark, 1),
                     "teams_left": [[board.teams_left_min, board.teams_left], teams_left],
                     "oracle_teams_left": [oracle.teams_left_min, oracle.teams_left],
                     "observed_teams": len(seen), "mismatched_teams": mismatched})

    def rates(label: str) -> dict[str, float]:
        return {field: checks[f"{label}_{field}"] / checks["teams"] if checks["teams"] else 0.0
                for field in ("dead", "kills", "eliminated")}

    def share(key: str) -> float:
        return checks[key] / checks["checkpoints"] if checks["checkpoints"] else 0.0

    return {
        "summary": {
            "checkpoints": checks["checkpoints"], "team_checks": checks["teams"],
            "vs_truth": rates("truth"),
            # Board vs a perfect reading of the same feed: what recognition still costs.
            "vs_oracle": rates("oracle"),
            # Oracle vs truth: what no reading can show (unseen recalls, underfilled squads).
            "oracle_vs_truth": rates("ceiling"),
            "teams_left_in_range": (checks["teams_left"] / checks["teams_left_shown"]
                                    if checks["teams_left_shown"] else 0.0),
            "teams_left_unknown": checks["checkpoints"] - checks["teams_left_shown"],
            "oracle_teams_left_in_range": share("oracle_teams_left"),
        },
        "board_lag_seconds": BOARD_LAG_SECONDS,
        "unmapped_board_badges": sorted(key for key in board.teams
                                        if key is not None and key not in mapped),
        "checkpoints": rows,
    }


def _team_view(teams: list) -> tuple[int, int, bool]:
    return (sum(t.dead for t in teams), sum(t.kills for t in teams),
            any(t.eliminated for t in teams))


def _oracle_events(session: Session, canonical, team_of: dict[str, int],
                   offset_seconds: float) -> list[ConfirmedEvent]:
    out = []
    for event in canonical:
        killer = event.killer if event.event_type is not EventType.KNOCK else (
            event.actor_candidates[0] if event.actor_candidates else None)
        seconds = (event.occurred_at_utc - session.started_at_utc).total_seconds() - offset_seconds
        kind = EventType.KILL if event.event_type is EventType.SUICIDE else event.event_type
        out.append(make_event(killer, team_of.get(killer or ""), event.victim,
                              team_of.get(event.victim or ""), kind,
                              ns=session.timeline_origin_ns + int(seconds * 1e9)))
    return sorted(out, key=lambda e: e.observed_at_ns)


def _rosters(raw: list[dict[str, Any]]) -> tuple[dict[int, set[str]], dict[str, int]]:
    start = next((e for e in raw if e.get("_T") == "LogMatchStart"), {})
    members: dict[int, set[str]] = defaultdict(set)
    for wrapper in start.get("characters", []):
        character = wrapper.get("character", {}) if isinstance(wrapper, dict) else {}
        if isinstance(character.get("name"), str) and isinstance(character.get("teamId"), int):
            members[character["teamId"]].add(character["name"])
    return dict(members), {name: team for team, names in members.items() for name in names}


def _timeline(raw: list[dict[str, Any]]):
    out = []
    for index, event in enumerate(raw):
        kind = event.get("_T")
        if kind not in {"LogPlayerKillV2", "LogPlayerMakeGroggy", "LogPlayerRevive",
                        "LogPlayerCreate"}:
            continue
        at = _parse_timestamp(event.get("_D"))
        actor = event.get("killer" if kind == "LogPlayerKillV2" else "attacker")
        # A recalled player (Blue Chip, comeback) is created again after death.
        victim = event.get("character" if kind == "LogPlayerCreate" else "victim")
        if at is not None and isinstance(victim, dict):
            out.append((at, index, kind, victim.get("name"),
                        actor.get("name") if isinstance(actor, dict) else None))
    return sorted(out, key=lambda item: item[0])


def _truth_at(timeline, visible: set[int], team_of: dict[str, int], at):
    """Player status, kills per team and the teams the viewer's feed has shown, at `at`."""
    status: dict[str, Status] = {}
    kills: Counter[int] = Counter()
    seen: set[int] = set()
    for when, index, kind, victim, actor in timeline:
        if when > at:
            break
        if kind in {"LogPlayerRevive", "LogPlayerCreate"}:
            status[victim] = Status.ALIVE
            continue
        status[victim] = Status.DEAD if kind == "LogPlayerKillV2" else Status.KNOCKED
        if kind == "LogPlayerKillV2" and actor in team_of and team_of[actor] != team_of.get(victim):
            kills[team_of[actor]] += 1
        if kind == "LogPlayerKillV2" or index in visible:
            seen.update(team_of[name] for name in (victim, actor) if name in team_of)
    return status, kills, seen


def _map_board(board: Ledger, team_of: dict[str, int]) -> dict[int, int]:
    """Board badge -> Telemetry teamId by majority of its players' best name match."""
    keys = {name_key(name): name for name in team_of}
    mapped = {}
    for badge, team in board.teams.items():
        if badge is None:
            continue
        votes = Counter(team_of[name] for player in team.players
                        if (name := _match(player, keys)) is not None)
        if votes:
            mapped[badge] = votes.most_common(1)[0][0]
    return mapped


def _match(spelling: str, keys: dict[str, str]) -> str | None:
    key = name_key(spelling)
    if key in keys:
        return keys[key]
    score, best = max(((SequenceMatcher(None, key, known).ratio(), known) for known in keys),
                      default=(0.0, ""))
    return keys[best] if score >= _NAME_MATCH_MIN else None
