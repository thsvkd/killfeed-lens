from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kfocr.domain.models import ConfirmedEvent


def serialize_event(event: ConfirmedEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "session_id": str(event.session_id),
        "observed_at_ns": event.observed_at_ns,
        "frame_seq": event.frame_seq,
        "killer_name_raw": event.killer_name_raw,
        "victim_name_raw": event.victim_name_raw,
        "killer_name": event.killer_name,
        "victim_name": event.victim_name,
        "killer_team": event.killer_team,
        "victim_team": event.victim_team,
        "event_type": event.event_type.value,
        "weapon_label": event.weapon_label,
        "headshot": event.headshot,
        "confidence_row": event.confidence_row,
        "confidence_killer": event.confidence_killer,
        "confidence_victim": event.confidence_victim,
        "confidence_event_type": event.confidence_event_type,
        "confidence_total": event.confidence_total,
        "source_bbox": list(event.source_bbox),
        "source_crop_ref": event.source_crop_ref,
        "visual_fingerprint": event.visual_fingerprint,
        "confirmed_at_ns": event.confirmed_at_ns,
        "dedup_key": event.dedup_key,
        "tracker_evidence_count": event.tracker_evidence_count,
    }


def export_events(
    events: Iterable[ConfirmedEvent], path: str | Path, format: str = "jsonl"
) -> None:
    if format not in {"jsonl", "csv"}:
        raise ValueError(f"Unsupported export format: {format}")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = [serialize_event(event) for event in events]
    if format == "jsonl":
        with destination.open("w", encoding="utf-8", newline="") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return
    if format == "csv":
        if rows:
            fieldnames = list(rows[0])
        else:
            fieldnames = [
                "event_id",
                "session_id",
                "observed_at_ns",
                "frame_seq",
                "killer_name_raw",
                "victim_name_raw",
                "killer_name",
                "victim_name",
                "killer_team",
                "victim_team",
                "event_type",
                "weapon_label",
                "headshot",
                "confidence_row",
                "confidence_killer",
                "confidence_victim",
                "confidence_event_type",
                "confidence_total",
                "source_bbox",
                "source_crop_ref",
                "visual_fingerprint",
                "confirmed_at_ns",
                "dedup_key",
                "tracker_evidence_count",
            ]
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                csv_row = dict(row)
                csv_row["source_bbox"] = json.dumps(csv_row["source_bbox"])
                writer.writerow(csv_row)
        return
