"""Run local fixture manifests. Holdout must be explicitly selected after tuning freezes."""

import argparse
import hashlib
import json
import platform
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import yaml

from kfocr.capture.replay import ReplaySource
from kfocr.config import apply_roi_profile, load_config
from kfocr.pipeline import Pipeline
from kfocr.storage.db import EventStore
from kfocr.telemetry.mapping import map_telemetry
from kfocr.telemetry.validator import validate_events


class QuietSink:
    def publish(self, event) -> None:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_fixture(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Hash inputs first, then decode and verify any declared source metadata."""
    video = manifest_path.parent / str(manifest["video"])
    try:
        video_sha256 = _sha256(video)
    except OSError as exc:
        raise ValueError(f"Cannot read fixture video: {video}") from exc
    declared_video_hash = manifest.get("video_sha256")
    if declared_video_hash and str(declared_video_hash).casefold() != video_sha256:
        raise ValueError(f"Video SHA256 mismatch: {video}")

    telemetry_sha256 = None
    if manifest.get("telemetry"):
        telemetry = manifest_path.parent / str(manifest["telemetry"])
        try:
            telemetry_sha256 = _sha256(telemetry)
        except OSError as exc:
            raise ValueError(f"Cannot read fixture telemetry: {telemetry}") from exc
        declared_telemetry_hash = manifest.get("telemetry_sha256")
        if declared_telemetry_hash and str(declared_telemetry_hash).casefold() != telemetry_sha256:
            raise ValueError(f"Telemetry SHA256 mismatch: {telemetry}")

    capture = cv2.VideoCapture(str(video))
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not capture.isOpened() or fps <= 0:
            raise ValueError(f"Cannot decode video or determine frame rate: {video}")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = float(capture.get(cv2.CAP_PROP_FRAME_COUNT)) / fps
    finally:
        capture.release()

    declared_resolution = manifest.get("resolution")
    if declared_resolution is not None and list(declared_resolution) != [width, height]:
        raise ValueError(
            f"Declared resolution {declared_resolution!r} does not match source {[width, height]!r}"
        )
    declared_duration = manifest.get("duration_sec")
    tolerance = max(0.1, 1.0 / fps + 1e-6)
    if declared_duration is not None and abs(float(declared_duration) - duration) > tolerance:
        raise ValueError(
            f"Declared duration {declared_duration!r} does not match source {duration:.6f}"
        )
    return {
        "video_sha256": video_sha256,
        "telemetry_sha256": telemetry_sha256,
        "resolution": [width, height],
        "recorded_duration_sec": duration,
    }


def acceptance_snapshot(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    required_resolutions = {"1920x1080", "2560x1440"}
    required_scenes = {"bright", "dark"}
    required_feeds = {"slow", "burst"}
    resolutions: set[str] = set()
    ui_scales: set[int | float] = set()
    scenes: set[str] = set()
    feeds: set[str] = set()
    duration = 0.0
    observed = telemetry = matched = 0
    true_positive = false_positive = false_negative = 0
    pair_correct = pair_total = duplicate_count = 0
    all_measured = bool(fixtures)
    holdout_only = bool(fixtures)
    for fixture in fixtures:
        dataset = fixture.get("dataset", {})
        holdout_only = holdout_only and dataset.get("split") == "holdout"
        resolution = dataset.get("resolution")
        if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
            resolutions.add(f"{resolution[0]}x{resolution[1]}")
        if dataset.get("ui_scale") is not None:
            ui_scales.add(dataset["ui_scale"])
        if dataset.get("scene"):
            scenes.add(str(dataset["scene"]))
        if dataset.get("feed"):
            feeds.add(str(dataset["feed"]))
        duration += float(dataset.get("recorded_duration_sec") or 0)
        validation = fixture.get("validation", {})
        all_measured = all_measured and validation.get("status") == "complete"
        counts = validation.get("sample_counts", {})
        observed += int(counts.get("observed", 0))
        telemetry += int(counts.get("telemetry", 0))
        matched += int(counts.get("matched", 0))
        event_metrics = validation.get("event_metrics", {})
        true_positive += int(event_metrics.get("tp", 0))
        false_positive += int(event_metrics.get("fp", 0))
        false_negative += int(event_metrics.get("fn", 0))
        pair = validation.get("field_accuracy", {}).get("pair_exact", {})
        pair_correct += int(pair.get("correct", 0))
        pair_total += int(pair.get("total", 0))
        duplicate_count += int(validation.get("tracking", {}).get("duplicate_count", 0))

    present = {
        "resolutions": sorted(resolutions),
        "ui_scales": sorted(ui_scales),
        "scenes": sorted(scenes),
        "feeds": sorted(feeds),
    }
    missing = {
        "resolutions": sorted(required_resolutions - resolutions),
        "ui_scales_needed": max(0, 2 - len(ui_scales)),
        "scenes": sorted(required_scenes - scenes),
        "feeds": sorted(required_feeds - feeds),
    }
    coverage_complete = not any(
        (missing["resolutions"], missing["ui_scales_needed"], missing["scenes"], missing["feeds"])
    )
    enough_samples = observed >= 100 and duration >= 1_800
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    pair_accuracy = pair_correct / pair_total if pair_total else 0
    false_events_per_30_minutes = false_positive / duration * 1_800 if duration else 0
    duplicate_rate = duplicate_count / observed if observed else 0
    metrics = {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "pair_exact_accuracy": pair_accuracy,
        "false_events_per_30_minutes": false_events_per_30_minutes,
        "duplicate_rate": duplicate_rate,
    }
    criteria = {
        "f1_at_least_0_95": f1 >= 0.95,
        "pair_exact_at_least_0_97": pair_accuracy >= 0.97,
        "false_events_at_most_1_per_30_minutes": false_events_per_30_minutes <= 1,
        "duplicate_rate_below_0_005": duplicate_rate < 0.005,
    }
    established = all_measured and holdout_only and coverage_complete and enough_samples
    return {
        "status": ("pass" if all(criteria.values()) else "fail") if established else "not_established",
        "requirements": {
            "minimum_observed_events": 100,
            "minimum_recorded_duration_sec": 1_800,
            "required_resolutions": sorted(required_resolutions),
            "minimum_distinct_ui_scales": 2,
            "required_scenes": sorted(required_scenes),
            "required_feeds": sorted(required_feeds),
            "evaluation_split": "holdout",
        },
        "sample_counts": {
            "fixtures": len(fixtures),
            "observed_events": observed,
            "telemetry_events": telemetry,
            "matched_events": matched,
        },
        "recorded_duration_sec": duration,
        "present_strata": present,
        "missing_strata": missing,
        "all_fixtures_measured": all_measured,
        "holdout_only": holdout_only,
        "metrics": metrics if established else None,
        "criteria": criteria if established else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    fixtures = []
    for path in args.manifests:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("split") not in (
            "dev", "tuning", "regression", "holdout"
        ):
            parser.error(f"Invalid fixture manifest: {path}")
        if manifest["split"] == "holdout" and not args.holdout:
            parser.error("Holdout evaluation requires --holdout; freeze thresholds/models first")
        if not manifest.get("id") or not manifest.get("video"):
            parser.error(f"Fixture needs id and video: {path}")
        try:
            inspected = inspect_fixture(path, manifest)
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
        fixtures.append((path, manifest, inspected))
    results = []
    with tempfile.TemporaryDirectory(prefix="kfocr-benchmark-") as temporary:
        for index, (path, manifest, inspected) in enumerate(fixtures):
            config = load_config(args.config)
            config.storage.sqlite_path = str(Path(temporary) / f"{index}.sqlite3")
            if manifest.get("roi_profile"):
                apply_roi_profile(config, path.parent / manifest["roi_profile"])
            if config.roi.rect is None:
                parser.error("Each benchmark needs calibrated ROI config/profile")
            started = (datetime.fromisoformat(manifest["started_at_utc"].replace("Z", "+00:00"))
                       if manifest.get("started_at_utc") else None)
            if started is not None and started.tzinfo is None:
                parser.error("Fixture started_at_utc needs timezone")
            config.roi.ui_scale = manifest.get("ui_scale")
            session = Pipeline(config, ReplaySource(path.parent / manifest["video"],
                                config.capture.sample_fps), sink=QuietSink(),
                                started_at_utc=started).run()
            dataset = {"fixture_id": manifest["id"], "split": manifest["split"],
                       "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "video_sha256": inspected["video_sha256"],
                       "telemetry_sha256": inspected["telemetry_sha256"],
                       "resolution": inspected["resolution"],
                       "recorded_duration_sec": inspected["recorded_duration_sec"],
                       "ui_scale": manifest.get("ui_scale"), "locale": manifest.get("locale"),
                       "scene": manifest.get("scene"), "feed": manifest.get("feed"),
                       "notes": manifest.get("notes", "")}
            session.metadata["benchmark_dataset"] = dataset
            result = {"dataset": dataset, "session_id": str(session.session_id),
                      "config_hash": session.config_hash, "model_manifest_hash": session.model_manifest_hash,
                      "performance": session.metadata["performance"]}
            store = EventStore(config.storage.sqlite_path)
            try:
                if manifest.get("telemetry"):
                    raw = json.loads((path.parent / manifest["telemetry"]).read_text(encoding="utf-8"))
                    result["validation"] = validate_events(session, store.list_events(session.session_id),
                                                           map_telemetry(raw))
                else:
                    result["validation"] = {"status": "unmeasured", "reason": "No ground truth provided"}
            finally:
                store.close()
            results.append(result)
    report = {"schema_version": 1, "hardware": {"platform": platform.platform(),
               "cpu": platform.processor() or platform.machine(), "gpu": "unmeasured",
               "cpu_utilization": "unmeasured", "ram_usage": "unmeasured"},
              "scope": "Recorded-video benchmark; Windows/game coexistence remains unmeasured",
              "fixtures": results, "acceptance": acceptance_snapshot(results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
