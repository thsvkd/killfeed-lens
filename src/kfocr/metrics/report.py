from __future__ import annotations

import json
from typing import Any


def report_json(report: dict[str, Any], *, indent: int = 2) -> str:
    """Serialize a validation report deterministically for local persistence/export."""

    return json.dumps(report, indent=indent, sort_keys=True, ensure_ascii=False)


def format_report(report: dict[str, Any]) -> str:
    """Render the compact human-facing portion used by the CLI report command."""

    if report.get("status") != "complete":
        return "\n".join([
            f"Validation status: {report.get('status', 'unmeasured')}",
            f"observed events: {report.get('observed_events', 'unknown')}",
            report.get("message", "Metrics are unmeasured until validation completes."),
        ])
    event = report.get("event_metrics", {})
    field = report.get("field_accuracy", {})
    tracking = report.get("tracking", {})
    counts = report.get("sample_counts", {})
    coverage = report.get("evaluation_coverage", {})
    return "\n".join(
        [
            "Validation report",
            (
                f"events: TP={event.get('tp', 0)} FP={event.get('fp', 0)} "
                f"FN={event.get('fn', 0)} precision={_percent(event.get('precision'))} "
                f"recall={_percent(event.get('recall'))} F1={_percent(event.get('f1'))}"
            ),
            (
                "exact: "
                f"killer={_field_percent(field, 'killer_exact')} "
                f"victim={_field_percent(field, 'victim_exact')} "
                f"pair={_field_percent(field, 'pair_exact')} "
                f"type={_field_percent(field, 'event_type')}"
            ),
            (
                f"tracking: duplicates={tracking.get('duplicate_count', 0)} "
                f"false_events={tracking.get('false_event_count', 0)}"
            ),
            (
                f"samples: observed={counts.get('observed', 0)} "
                f"telemetry={counts.get('telemetry', 0)} "
                f"unresolved={counts.get('unresolved_telemetry', 0)}"
            ),
            (
                f"evaluation coverage: observed={coverage.get('observed_evaluated', 0)}/"
                f"{coverage.get('observed_total', counts.get('observed', 0))} "
                f"telemetry={coverage.get('telemetry_evaluated', 0)}/"
                f"{coverage.get('telemetry_total', counts.get('telemetry', 0))}"
            ),
        ]
    )


def _field_percent(fields: dict[str, Any], name: str) -> str:
    value = fields.get(name, {})
    return _percent(value.get("accuracy") if isinstance(value, dict) else None)


def _percent(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{100.0 * value:.1f}%"
