from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from kfocr.domain.models import Session


@dataclass(frozen=True)
class MatchCandidate:
    match_id: str
    confidence: float
    player_included: bool
    overlap_ratio: float
    start_delta_seconds: float
    duration_delta_seconds: float


@dataclass(frozen=True)
class MatchDecision:
    status: Literal["matched", "ambiguous", "no_match"]
    match: dict[str, Any] | None
    match_id: str | None
    confidence: float
    reason: str
    candidates: tuple[MatchCandidate, ...]


def find_match(
    session: Session,
    matches: list[dict[str, Any]],
    player: str,
    *,
    auto_accept_min: float = 0.90,
    ambiguous_min: float = 0.70,
    ambiguity_margin: float = 0.05,
) -> MatchDecision:
    """Select a completed match conservatively from full JSON-API match objects."""

    scored: list[tuple[MatchCandidate, dict[str, Any]]] = []
    for match in matches:
        candidate = _score_match(session, match, player)
        if candidate is not None:
            scored.append((candidate, match))
    scored.sort(key=lambda item: item[0].confidence, reverse=True)
    candidates = tuple(candidate for candidate, _ in scored)
    if not scored:
        return MatchDecision("no_match", None, None, 0.0, "no valid match candidates", ())

    best, best_match = scored[0]
    if not best.player_included or best.confidence < ambiguous_min:
        return MatchDecision(
            "no_match",
            None,
            None,
            best.confidence,
            "best candidate lacks exact player membership or temporal confidence",
            candidates,
        )

    if len(scored) > 1 and best.confidence - scored[1][0].confidence < ambiguity_margin:
        return MatchDecision(
            "ambiguous",
            None,
            None,
            best.confidence,
            "top match candidates are too close to select safely",
            candidates,
        )

    if best.confidence < auto_accept_min:
        return MatchDecision(
            "ambiguous",
            None,
            None,
            best.confidence,
            "best candidate requires manual review",
            candidates,
        )
    return MatchDecision(
        "matched",
        best_match,
        best.match_id,
        best.confidence,
        "exact player membership and session timing agree",
        candidates,
    )


def _score_match(
    session: Session, match: dict[str, Any], player: str
) -> MatchCandidate | None:
    data = match.get("data")
    if not isinstance(data, dict):
        return None
    match_id = data.get("id")
    attributes = data.get("attributes")
    if not isinstance(match_id, str) or not isinstance(attributes, dict):
        return None
    match_start = _parse_timestamp(attributes.get("createdAt"))
    duration = attributes.get("duration")
    if match_start is None or not isinstance(duration, (int, float)) or duration <= 0:
        return None
    if session.ended_at_utc is None:
        return None

    match_end = match_start + timedelta(seconds=float(duration))
    session_duration = (session.ended_at_utc - session.started_at_utc).total_seconds()
    overlap_seconds = max(
        0.0,
        (
            min(session.ended_at_utc, match_end)
            - max(session.started_at_utc, match_start)
        ).total_seconds(),
    )
    shorter_duration = min(session_duration, float(duration))
    overlap_ratio = overlap_seconds / shorter_duration if shorter_duration > 0 else 0.0
    start_delta = abs((session.started_at_utc - match_start).total_seconds())
    duration_delta = abs(session_duration - float(duration))
    player_included = player in _participant_names(match)

    membership_score = 0.40 if player_included else 0.0
    overlap_score = 0.30 * min(1.0, overlap_ratio)
    start_score = 0.20 * max(0.0, 1.0 - start_delta / 300.0)
    duration_score = 0.10 * max(0.0, 1.0 - duration_delta / 600.0)
    confidence = round(membership_score + overlap_score + start_score + duration_score, 6)
    return MatchCandidate(
        match_id=match_id,
        confidence=confidence,
        player_included=player_included,
        overlap_ratio=round(overlap_ratio, 6),
        start_delta_seconds=start_delta,
        duration_delta_seconds=duration_delta,
    )


def _participant_names(match: dict[str, Any]) -> set[str]:
    included = match.get("included")
    if not isinstance(included, list):
        return set()
    names: set[str] = set()
    for resource in included:
        if not isinstance(resource, dict) or resource.get("type") != "participant":
            continue
        attributes = resource.get("attributes")
        stats = attributes.get("stats") if isinstance(attributes, dict) else None
        name = stats.get("name") if isinstance(stats, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


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
