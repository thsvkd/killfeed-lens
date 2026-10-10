from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Any

from kfocr.domain.models import ConfirmedEvent, EventType, Session
from kfocr.telemetry.mapping import TelemetryCanonicalEvent

_ALIGNMENT_TOLERANCE_SECONDS = 5.0
_OFFSET_OUTLIER_FLOOR_SECONDS = 2.0


def validate_events(
    session: Session,
    observed: list[ConfirmedEvent],
    canonical: list[TelemetryCanonicalEvent],
) -> dict[str, Any]:
    """Align observed rows to resolvable telemetry and return a JSON-safe report."""

    if session.ended_at_utc is None:
        raise ValueError("Validation requires a known session end time")

    offset_candidates = [event for event in canonical if _is_resolved(event)]
    offset, offset_samples, offset_inliers = _estimate_offset(
        session, observed, offset_candidates
    )
    shifted_start = session.started_at_utc + timedelta(seconds=offset)
    shifted_end = session.ended_at_utc + timedelta(seconds=offset)
    lower_bound = shifted_start - timedelta(seconds=_ALIGNMENT_TOLERANCE_SECONDS)
    upper_bound = shifted_end + timedelta(seconds=_ALIGNMENT_TOLERANCE_SECONDS)
    temporal = [
        event for event in canonical if lower_bound <= event.occurred_at_utc <= upper_bound
    ]
    excluded_temporal = [
        event for event in canonical if not lower_bound <= event.occurred_at_utc <= upper_bound
    ]
    resolved = [event for event in temporal if _is_resolved(event)]
    unresolved = [event for event in temporal if not _is_resolved(event)]
    matches = _assign_events(session, observed, resolved, offset)

    matched_observed = {item[0] for item in matches}
    matched_telemetry = {item[1] for item in matches}
    unmatched_observed_candidates = [
        i for i in range(len(observed)) if i not in matched_observed
    ]
    unresolved_assignments = _associate_unresolved(
        session,
        observed,
        unresolved,
        offset,
        unmatched_observed_candidates,
    )
    excluded_observed = {observed_index for observed_index, _ in unresolved_assignments}
    unmatched_observed_indexes = [
        i for i in unmatched_observed_candidates if i not in excluded_observed
    ]
    unmatched_telemetry_indexes = [i for i in range(len(resolved)) if i not in matched_telemetry]

    tp = len(matches)
    fp = len(unmatched_observed_indexes)
    fn = len(unmatched_telemetry_indexes)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = _ratio(2 * precision * recall, precision + recall)

    match_rows = [
        _match_row(observed[observed_index], resolved[telemetry_index], delta, score)
        for observed_index, telemetry_index, delta, score in matches
    ]
    duplicate_indexes = _duplicate_indexes(observed, matches, unmatched_observed_indexes)
    unresolved_by_telemetry = {
        telemetry_index: observed_index
        for observed_index, telemetry_index in unresolved_assignments
    }
    unresolved_rows = []
    for telemetry_index, event in enumerate(unresolved):
        row = _telemetry_row(event, None)
        candidate_observed = unresolved_by_telemetry.get(telemetry_index)
        row["candidate_observed_index"] = candidate_observed
        unresolved_rows.append(row)
    evaluated_observed = len(observed) - len(excluded_observed)

    return {
        "schema_version": 1,
        "status": "complete",
        "event_metrics": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "field_accuracy": {
            "killer_exact": _accuracy(match_rows, "killer_exact"),
            "victim_exact": _accuracy(match_rows, "victim_exact"),
            "pair_exact": _accuracy(match_rows, "pair_exact"),
            "event_type": _accuracy(match_rows, "event_type_exact"),
        },
        "tracking": {
            "duplicate_count": len(duplicate_indexes),
            "duplicate_rate": _ratio(len(duplicate_indexes), len(observed)),
            "false_event_count": fp,
            "false_event_rate": _ratio(fp, evaluated_observed),
            "merge_error_rate": {
                "available": False,
                "reason": "requires labeled track-lineage fixtures",
            },
            "split_error_rate": {
                "available": False,
                "reason": "requires labeled track-lineage fixtures",
            },
        },
        "alignment": {
            "offset_seconds": offset,
            "offset_sample_count": offset_samples,
            "offset_inlier_count": offset_inliers,
            "time_tolerance_seconds": _ALIGNMENT_TOLERANCE_SECONDS,
        },
        "sample_counts": {
            "observed": len(observed),
            "evaluated_observed": evaluated_observed,
            "excluded_unresolved_observed": len(excluded_observed),
            "telemetry": len(canonical),
            "temporal_telemetry": len(temporal),
            "excluded_outside_session": len(excluded_temporal),
            "resolvable_telemetry": len(resolved),
            "unresolved_telemetry": len(unresolved),
            "matched": tp,
        },
        "evaluation_coverage": {
            "observed_evaluated": evaluated_observed,
            "observed_total": len(observed),
            "observed_ratio": _ratio(evaluated_observed, len(observed)),
            "telemetry_evaluated": len(resolved),
            "telemetry_total": len(canonical),
            "telemetry_ratio": _ratio(len(resolved), len(canonical)),
        },
        "temporal_coverage": {
            "session_start_utc": session.started_at_utc.isoformat(),
            "session_end_utc": session.ended_at_utc.isoformat(),
            "shifted_start_utc": shifted_start.isoformat(),
            "shifted_end_utc": shifted_end.isoformat(),
            "tolerance_seconds": _ALIGNMENT_TOLERANCE_SECONDS,
            "included": len(temporal),
            "excluded": len(excluded_temporal),
            "excluded_telemetry": [
                _telemetry_row(event, None) for event in excluded_temporal
            ],
        },
        "confidence_buckets": _confidence_buckets(
            observed, matched_observed, excluded_observed
        ),
        "matches": match_rows,
        "unmatched_observed": [_observed_row(observed[i], i) for i in unmatched_observed_indexes],
        "unmatched_telemetry": [
            _telemetry_row(resolved[i], i) for i in unmatched_telemetry_indexes
        ],
        "unresolved_semantics": unresolved_rows,
        "unresolved_observed": [
            _observed_row(observed[index], index) for index in sorted(excluded_observed)
        ],
        "duplicate_observed_indexes": duplicate_indexes,
        "reproducibility": _reproducibility(session),
    }


def _estimate_offset(
    session: Session,
    observed: list[ConfirmedEvent],
    canonical: list[TelemetryCanonicalEvent],
) -> tuple[float, int, int]:
    observed_by_pair: dict[tuple[str, str], list[ConfirmedEvent]] = defaultdict(list)
    telemetry_by_pair: dict[tuple[str, str], list[TelemetryCanonicalEvent]] = defaultdict(list)
    for observed_event in observed:
        if (
            observed_event.confidence_total >= 0.8
            and observed_event.killer_name
            and observed_event.victim_name
        ):
            observed_by_pair[(observed_event.killer_name, observed_event.victim_name)].append(
                observed_event
            )
    for telemetry_event in canonical:
        actor = telemetry_event.actor_candidates[0]
        telemetry_by_pair[(actor, telemetry_event.victim or "")].append(telemetry_event)

    deltas: list[float] = []
    for pair in observed_by_pair.keys() & telemetry_by_pair.keys():
        observed_group = sorted(observed_by_pair[pair], key=lambda event: event.observed_at_ns)
        telemetry_group = sorted(
            telemetry_by_pair[pair], key=lambda event: event.occurred_at_utc
        )
        # Repeated identical pairs are safe offset evidence only when chronological
        # one-to-one pairing is complete. Unequal counts could silently shift every
        # later occurrence after one missed OCR row.
        if len(observed_group) != len(telemetry_group):
            continue
        for observed_event, telemetry_event in zip(observed_group, telemetry_group, strict=False):
            if not _types_compatible(observed_event.event_type, telemetry_event.event_type):
                continue
            observed_time = _observed_wall_time(session, observed_event)
            deltas.append((telemetry_event.occurred_at_utc - observed_time).total_seconds())

    if len(deltas) < 2:
        return 0.0, len(deltas), 0
    median = statistics.median(deltas)
    deviations = [abs(value - median) for value in deltas]
    median_absolute_deviation = statistics.median(deviations)
    threshold = max(_OFFSET_OUTLIER_FLOOR_SECONDS, 3.0 * median_absolute_deviation)
    inliers = [value for value in deltas if abs(value - median) <= threshold]
    if len(inliers) < 2:
        return 0.0, len(deltas), 0
    return float(statistics.median(inliers)), len(deltas), len(inliers)


def _assign_events(
    session: Session,
    observed: list[ConfirmedEvent],
    canonical: list[TelemetryCanonicalEvent],
    offset_seconds: float,
) -> list[tuple[int, int, float, float]]:
    edges: list[tuple[float, float, int, int]] = []
    for observed_index, observed_event in enumerate(observed):
        observed_time = _observed_wall_time(session, observed_event) + timedelta(
            seconds=offset_seconds
        )
        for telemetry_index, telemetry_event in enumerate(canonical):
            actor_exact = observed_event.killer_name == telemetry_event.actor_candidates[0]
            victim_exact = observed_event.victim_name == telemetry_event.victim
            if not actor_exact and not victim_exact:
                continue
            delta = abs((telemetry_event.occurred_at_utc - observed_time).total_seconds())
            if delta > _ALIGNMENT_TOLERANCE_SECONDS:
                continue
            type_compatible = _types_compatible(observed_event.event_type, telemetry_event.event_type)
            if not type_compatible:
                continue
            score = (
                0.35 * float(actor_exact)
                + 0.35 * float(victim_exact)
                + 0.20 * float(type_compatible)
                + 0.10 * (1.0 - delta / _ALIGNMENT_TOLERANCE_SECONDS)
            )
            edges.append((-score, delta, observed_index, telemetry_index))

    used_observed: set[int] = set()
    used_telemetry: set[int] = set()
    assignments: list[tuple[int, int, float, float]] = []
    for negative_score, delta, observed_index, telemetry_index in sorted(edges):
        if observed_index in used_observed or telemetry_index in used_telemetry:
            continue
        used_observed.add(observed_index)
        used_telemetry.add(telemetry_index)
        assignments.append((observed_index, telemetry_index, delta, -negative_score))
    assignments.sort(key=lambda item: item[0])
    return assignments


def _types_compatible(observed: EventType, telemetry: EventType) -> bool:
    if observed == EventType.UNKNOWN:
        return True
    if observed == telemetry:
        return True
    return observed == EventType.KILL and telemetry in {
        EventType.SUICIDE,
        EventType.FINISH,
        EventType.BLEEDOUT,
    }


def _associate_unresolved(
    session: Session,
    observed: list[ConfirmedEvent],
    unresolved: list[TelemetryCanonicalEvent],
    offset_seconds: float,
    candidate_observed_indexes: list[int],
) -> list[tuple[int, int]]:
    edges: list[tuple[float, int, int]] = []
    for observed_index in candidate_observed_indexes:
        observed_event = observed[observed_index]
        observed_time = _observed_wall_time(session, observed_event) + timedelta(
            seconds=offset_seconds
        )
        for telemetry_index, telemetry_event in enumerate(unresolved):
            possible_actors = {
                actor
                for actor in (
                    telemetry_event.dBNO_maker,
                    telemetry_event.finisher,
                    telemetry_event.killer,
                )
                if actor
            }
            if (
                observed_event.killer_name not in possible_actors
                or observed_event.victim_name != telemetry_event.victim
                or not _types_compatible(observed_event.event_type, telemetry_event.event_type)
            ):
                continue
            delta = abs((telemetry_event.occurred_at_utc - observed_time).total_seconds())
            if delta <= _ALIGNMENT_TOLERANCE_SECONDS:
                edges.append((delta, observed_index, telemetry_index))

    used_observed: set[int] = set()
    used_telemetry: set[int] = set()
    assignments: list[tuple[int, int]] = []
    for _, observed_index, telemetry_index in sorted(edges):
        if observed_index in used_observed or telemetry_index in used_telemetry:
            continue
        used_observed.add(observed_index)
        used_telemetry.add(telemetry_index)
        assignments.append((observed_index, telemetry_index))
    return assignments


def _is_resolved(event: TelemetryCanonicalEvent) -> bool:
    return len(event.actor_candidates) == 1 and event.victim is not None


def _observed_wall_time(session: Session, event: ConfirmedEvent):
    timeline_seconds = (event.observed_at_ns - session.timeline_origin_ns) / 1_000_000_000
    return session.started_at_utc + timedelta(seconds=timeline_seconds)


def _match_row(
    observed: ConfirmedEvent,
    telemetry: TelemetryCanonicalEvent,
    delta_seconds: float,
    score: float,
) -> dict[str, Any]:
    killer_exact = observed.killer_name == telemetry.actor_candidates[0]
    victim_exact = observed.victim_name == telemetry.victim
    return {
        "event_id": str(observed.event_id),
        "telemetry_event_id": str(telemetry.raw_index),
        "telemetry_index": telemetry.raw_index,
        "telemetry_type": telemetry.raw_type,
        "match_score": score,
        "result": "matched",
        "time_delta_seconds": delta_seconds,
        "killer_exact": killer_exact,
        "victim_exact": victim_exact,
        "pair_exact": killer_exact and victim_exact,
        "event_type_exact": observed.event_type == telemetry.event_type,
    }


def _accuracy(rows: list[dict[str, Any]], field: str) -> dict[str, int | float]:
    correct = sum(bool(row[field]) for row in rows)
    return {"correct": correct, "total": len(rows), "accuracy": _ratio(correct, len(rows))}


def _confidence_buckets(
    observed: list[ConfirmedEvent],
    matched_indexes: set[int],
    excluded_indexes: set[int],
) -> dict[str, dict[str, int | float]]:
    buckets = (
        (0.0, 0.70, "0.00-0.70"),
        (0.70, 0.80, "0.70-0.80"),
        (0.80, 0.90, "0.80-0.90"),
        (0.90, math.inf, "0.90-1.00"),
    )
    result: dict[str, dict[str, int | float]] = {}
    for lower, upper, label in buckets:
        indexes = [
            index
            for index, event in enumerate(observed)
            if lower <= event.confidence_total < upper
        ]
        evaluated_indexes = [index for index in indexes if index not in excluded_indexes]
        correct = sum(index in matched_indexes for index in evaluated_indexes)
        result[label] = {
            "count": len(indexes),
            "evaluated_count": len(evaluated_indexes),
            "excluded_unresolved_count": len(indexes) - len(evaluated_indexes),
            "correct": correct,
            "accuracy": _ratio(correct, len(evaluated_indexes)),
        }
    return result


def _duplicate_indexes(
    observed: list[ConfirmedEvent],
    matches: list[tuple[int, int, float, float]],
    unmatched_indexes: list[int],
) -> list[int]:
    matched_indexes = [item[0] for item in matches]
    duplicates: list[int] = []
    seen_keys: dict[str, int] = {}
    for index, event in enumerate(observed):
        if event.dedup_key in seen_keys:
            duplicates.append(index)
        else:
            seen_keys[event.dedup_key] = index
    for index in unmatched_indexes:
        event = observed[index]
        for matched_index in matched_indexes:
            matched = observed[matched_index]
            if (
                event.killer_name == matched.killer_name
                and event.victim_name == matched.victim_name
                and event.event_type == matched.event_type
                and abs(event.observed_at_ns - matched.observed_at_ns) <= 2_000_000_000
            ):
                duplicates.append(index)
                break
    return sorted(set(duplicates))


def _observed_row(event: ConfirmedEvent, index: int) -> dict[str, Any]:
    return {
        "observed_index": index,
        "event_id": str(event.event_id),
        "killer": event.killer_name,
        "victim": event.victim_name,
        "event_type": event.event_type.value,
        "confidence": event.confidence_total,
    }


def _telemetry_row(
    event: TelemetryCanonicalEvent, resolved_index: int | None
) -> dict[str, Any]:
    return {
        "resolved_index": resolved_index,
        "telemetry_index": event.raw_index,
        "telemetry_type": event.raw_type,
        "occurred_at_utc": event.occurred_at_utc.isoformat(),
        "actor_candidates": list(event.actor_candidates),
        "victim": event.victim,
        "event_type": event.event_type.value,
        "dBNO_maker": event.dBNO_maker,
        "finisher": event.finisher,
        "killer": event.killer,
    }


def _reproducibility(session: Session) -> dict[str, Any]:
    return {
        "session_id": str(session.session_id),
        "app_version": session.app_version,
        "git_commit": session.git_commit,
        "config_hash": session.config_hash,
        "model_manifest_hash": session.model_manifest_hash,
        "source_resolution": list(session.source_resolution),
        "roi_profile_id": session.roi_profile_id,
        "source_type": session.source_type,
        "started_at_utc": session.started_at_utc.isoformat(),
        "ended_at_utc": session.ended_at_utc.isoformat() if session.ended_at_utc else None,
        "timeline_origin_ns": session.timeline_origin_ns,
        "metadata": _json_safe(session.metadata),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0
