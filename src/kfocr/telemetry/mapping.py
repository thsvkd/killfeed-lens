from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kfocr.domain.models import EventType


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


def map_telemetry(events: list[dict[str, Any]]) -> list[TelemetryCanonicalEvent]:
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
        canonical.append(
            TelemetryCanonicalEvent(
                occurred_at_utc=occurred_at,
                event_type=EventType.SUICIDE if event.get("isSuicide") is True else EventType.KILL,
                actor_candidates=list(distinct_actors) if len(distinct_actors) == 1 else [],
                victim=victim,
                dBNO_maker=dbno_maker,
                finisher=finisher,
                killer=killer,
                raw_type=raw_type,
                raw_index=raw_index,
            )
        )
    return canonical


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
