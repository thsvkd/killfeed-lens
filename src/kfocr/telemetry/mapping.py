from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kfocr.domain.models import EventType

# A knock farther than this from every member of the viewer's team never reaches that viewer's
# killfeed; kills are shown at any range. Telemetry locations are in centimetres.
# ponytail: fitted on one Sanhok duo match (shown <= 955 m, hidden >= 1,064 m); re-fit if a
# later match disagrees.
KNOCK_FEED_RANGE_CM = 100_000
_POSITION_MAX_AGE_SECONDS = 30  # positions are logged every ~10 s and stop at death
Track = list[tuple[datetime, float, float]]


@dataclass(frozen=True)
class TelemetryCanonicalEvent:
    occurred_at_utc: datetime
    event_type: EventType
    actor_candidates: list[str]
    victim: str | None
    dBNO_maker: str | None
    finisher: str | None
    killer: str | None
    raw_type: str
    raw_index: int


def map_telemetry(
    events: list[dict[str, Any]], viewer: str | None = None
) -> list[TelemetryCanonicalEvent]:
    """With a viewer, knocks out of that viewer's killfeed range are left out."""
    tracks = _feed_tracks(events, viewer) if viewer else []
    canonical: list[TelemetryCanonicalEvent] = []
    for raw_index, event in enumerate(events):
        raw_type = event.get("_T")
        if raw_type not in {"LogPlayerKillV2", "LogPlayerMakeGroggy"}:
            continue
        occurred_at = _parse_timestamp(event.get("_D"))
        if occurred_at is None:
            raise ValueError(
                f"Supported telemetry event at index {raw_index} has invalid or missing _D"
            )

        victim = _character_name(event.get("victim"))
        if raw_type == "LogPlayerMakeGroggy":
            if tracks and not _in_feed_range(tracks, occurred_at, event.get("victim")):
                continue
            attacker = _character_name(event.get("attacker"))
            canonical.append(
                TelemetryCanonicalEvent(
                    occurred_at_utc=occurred_at,
                    event_type=EventType.KNOCK,
                    actor_candidates=[attacker] if attacker else [],
                    victim=victim,
                    dBNO_maker=None,
                    finisher=None,
                    killer=attacker,
                    raw_type=raw_type,
                    raw_index=raw_index,
                )
            )
            continue

        dbno_maker = _character_name(event.get("dBNOMaker"))
        finisher = _character_name(event.get("finisher"))
        killer = _character_name(event.get("killer"))
        distinct_actors = {name for name in (dbno_maker, finisher, killer) if name}
        # The feed names whoever finished the victim (live: 48 of 48 finishes of another
        # player's knock, a teammate's too, where Telemetry credits the knocker as the killer).
        actors = [finisher] if finisher else list(distinct_actors) if len(distinct_actors) == 1 else []
        canonical.append(
            TelemetryCanonicalEvent(
                occurred_at_utc=occurred_at,
                event_type=EventType.SUICIDE if event.get("isSuicide") is True else EventType.KILL,
                actor_candidates=actors,
                victim=victim,
                dBNO_maker=dbno_maker,
                finisher=finisher,
                killer=killer,
                raw_type=raw_type,
                raw_index=raw_index,
            )
        )
    return canonical


def _feed_tracks(
    events: list[dict[str, Any]], viewer: str
) -> list[tuple[datetime | None, list[Track]]]:
    """From when on whose position samples decide the viewer's feed range: the viewer's team, then,
    once it is wiped, the team of the player the spectator camera follows, which is the killer of
    the one it followed before (live match 3, a solo-queued player dead after 90 s: knock FP
    34 -> 2, FN 2 -> 5). Empty if the viewer is unknown."""
    tracks: dict[Any, dict[str, Track]] = defaultdict(lambda: defaultdict(list))
    team_of: dict[str, Any] = {}
    died: dict[str, datetime] = {}
    killer_of: dict[str, str | None] = {}
    for event in events:
        victim = _character_name(event.get("victim"))
        if (event.get("_T") == "LogPlayerKillV2" and victim
                and (when := _parse_timestamp(event.get("_D"))) is not None):
            died[victim] = when
            killer_of[victim] = (_character_name(event.get("killer"))
                                 or _character_name(event.get("finisher")))
        character = event.get("character")
        if event.get("_T") == "LogPlayerCreate" and (name := _character_name(character)):
            died.pop(name, None)  # recalled (Blue Chip): alive again
        if event.get("_T") != "LogPlayerPosition" or not isinstance(character, dict):
            continue
        name = str(character.get("name"))
        team_of[name] = character.get("teamId")
        at, point = _parse_timestamp(event.get("_D")), _point(character)
        if at is not None and point is not None:
            tracks[team_of[name]][name].append((at, *point))
    if viewer not in team_of:
        return []
    segments: list[tuple[datetime | None, Any]] = [(None, team_of[viewer])]
    members = [name for name, team in team_of.items() if team == team_of[viewer]]
    if all(name in died for name in members):  # wiped: the camera follows the killers
        followed = max(members, key=died.__getitem__)
        since = died[followed]
        while (killer := killer_of.get(followed)) is not None and killer in team_of and (
                killer not in died or died[killer] > since):
            segments.append((since, team_of[killer]))
            if killer not in died:
                break
            followed, since = killer, died[killer]
    return [(start, list(tracks[team].values())) for start, team in segments]


def _in_feed_range(
    segments: list[tuple[datetime | None, list[Track]]], at: datetime, victim: object
) -> bool:
    point = _point(victim)
    if point is None:
        return True  # unknown location: keep the knock rather than hide a possible miss
    tracks = next(tracks for start, tracks in reversed(segments) if start is None or start <= at)
    for track in tracks:
        sample_at, x, y = min(track, key=lambda sample: abs((sample[0] - at).total_seconds()))
        if (abs((sample_at - at).total_seconds()) <= _POSITION_MAX_AGE_SECONDS
                and math.dist((x, y), point) <= KNOCK_FEED_RANGE_CM):
            return True
    return False


def _point(character: object) -> tuple[float, float] | None:
    location = character.get("location") if isinstance(character, dict) else None
    if not isinstance(location, dict):
        return None
    x, y = location.get("x"), location.get("y")
    if not isinstance(x, int | float) or not isinstance(y, int | float):
        return None
    return float(x), float(y)


def _character_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    return name if isinstance(name, str) and name else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
