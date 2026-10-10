import hashlib
import json
import subprocess
import sys

import cv2
import numpy as np
import pytest
import yaml


def test_manifest_builder_records_real_video_metadata_and_local_paths(tmp_path):
    video = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 100))
    assert writer.isOpened()
    for _ in range(20):
        writer.write(np.full((100, 160, 3), 40, dtype=np.uint8))
    writer.release()
    output = tmp_path / "dataset" / "clip.yaml"
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text("[]")
    result = subprocess.run([sys.executable, "scripts/build_fixture_manifest.py", str(video),
                             "--split", "regression", "--ui-scale", "100",
                             "--scene", "bright", "--feed", "slow",
                             "--telemetry", str(telemetry),
                             "--notes", "synthetic fixture", "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    manifest = yaml.safe_load(output.read_text())
    assert manifest["resolution"] == [160, 100]
    assert manifest["duration_sec"] == 2
    assert manifest["split"] == "regression"
    assert manifest["scene"] == "bright"
    assert manifest["feed"] == "slow"
    assert manifest["notes"] == "synthetic fixture"
    assert (output.parent / manifest["video"]).resolve() == video
    assert len(manifest["video_sha256"]) == 64
    assert manifest["telemetry_sha256"] == hashlib.sha256(telemetry.read_bytes()).hexdigest()


def test_benchmark_refuses_holdout_without_explicit_flag(tmp_path):
    manifest = tmp_path / "holdout.yaml"
    manifest.write_text("id: holdout\nvideo: missing.mp4\nsplit: holdout\n")
    result = subprocess.run([sys.executable, "scripts/benchmark.py", str(manifest),
                             "--output", str(tmp_path / "result.json")],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "holdout" in result.stderr.lower()
    assert not (tmp_path / "result.json").exists()


def test_static_audit_accepts_pixel_dependencies_and_detects_forbidden_import(tmp_path):
    result = subprocess.run([sys.executable, "scripts/audit_capabilities.py"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.py").write_text("import pyautogui\n")
    result = subprocess.run([sys.executable, "scripts/audit_capabilities.py", "--root", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "pyautogui" in json.loads(result.stdout)["findings"][0]["capability"]


def test_static_audit_scans_scripts_and_parses_pep508_dependency_names(tmp_path):
    (tmp_path / "src").mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bad.py").write_text("from scapy.all import sniff\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "audit-test"\nversion = "0.0.0"\n'
        'dependencies = ["scapy<3", "pyautogui[image]~=0.9"]\n'
    )

    result = subprocess.run(
        [sys.executable, "scripts/audit_capabilities.py", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    findings = json.loads(result.stdout)["findings"]
    assert any(item["file"] == "scripts/bad.py" for item in findings)
    assert {item["capability"] for item in findings if item["file"] == "pyproject.toml"} == {
        "scapy<3",
        "pyautogui[image]~=0.9",
    }


def test_benchmark_rejects_video_hash_mismatch_before_decode(tmp_path):
    video = tmp_path / "not-a-video.bin"
    video.write_bytes(b"fixture bytes")
    manifest = tmp_path / "fixture.yaml"
    manifest.write_text(
        "id: fixture\nvideo: not-a-video.bin\nsplit: regression\nvideo_sha256: deadbeef\n"
    )

    result = subprocess.run(
        [sys.executable, "scripts/benchmark.py", str(manifest), "--output", str(tmp_path / "out.json")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "sha256 mismatch" in result.stderr.lower()
    assert "decode" not in result.stderr.lower()


def test_fixture_inspection_records_actual_hashes_and_rejects_metadata_mismatch(tmp_path):
    from scripts.benchmark import inspect_fixture

    video = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 100))
    assert writer.isOpened()
    for _ in range(20):
        writer.write(np.full((100, 160, 3), 40, dtype=np.uint8))
    writer.release()
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text("[]")
    manifest = {
        "id": "fixture",
        "video": video.name,
        "telemetry": telemetry.name,
        "resolution": [160, 100],
        "duration_sec": 2.0,
    }

    inspected = inspect_fixture(tmp_path / "fixture.yaml", manifest)

    assert inspected["video_sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert inspected["telemetry_sha256"] == hashlib.sha256(telemetry.read_bytes()).hexdigest()
    assert inspected["recorded_duration_sec"] == 2.0
    with pytest.raises(ValueError, match="resolution"):
        inspect_fixture(tmp_path / "fixture.yaml", {**manifest, "resolution": [1920, 1080]})
    with pytest.raises(ValueError, match="duration"):
        inspect_fixture(tmp_path / "fixture.yaml", {**manifest, "duration_sec": 12})


def test_acceptance_snapshot_reports_coverage_without_inventing_acceptance() -> None:
    from scripts.benchmark import acceptance_snapshot

    fixtures = [
        {
            "dataset": {
                "resolution": [1920, 1080],
                "ui_scale": 100,
                "scene": "bright",
                "feed": "slow",
                "recorded_duration_sec": 60,
            },
            "validation": {"status": "complete", "sample_counts": {"observed": 10}},
        }
    ]

    snapshot = acceptance_snapshot(fixtures)

    assert snapshot["status"] == "not_established"
    assert snapshot["sample_counts"]["observed_events"] == 10
    assert snapshot["recorded_duration_sec"] == 60
    assert snapshot["present_strata"] == {
        "resolutions": ["1920x1080"],
        "ui_scales": [100],
        "scenes": ["bright"],
        "feeds": ["slow"],
    }
    assert set(snapshot["missing_strata"]["resolutions"]) == {"2560x1440"}


def test_acceptance_snapshot_evaluates_thresholds_only_after_matrix_is_complete() -> None:
    from scripts.benchmark import acceptance_snapshot

    fixtures = []
    for index, (resolution, scale, scene, feed) in enumerate(
        [
            ([1920, 1080], 100, "bright", "slow"),
            ([2560, 1440], 100, "dark", "burst"),
            ([1920, 1080], 80, "dark", "slow"),
            ([2560, 1440], 80, "bright", "burst"),
        ]
    ):
        fixtures.append(
            {
                "dataset": {
                    "split": "holdout",
                    "resolution": resolution,
                    "ui_scale": scale,
                    "scene": scene,
                    "feed": feed,
                    "recorded_duration_sec": 450,
                },
                "validation": {
                    "status": "complete",
                    "sample_counts": {"observed": 25, "telemetry": 25, "matched": 25},
                    "event_metrics": {"tp": 25, "fp": 0, "fn": 0},
                    "field_accuracy": {"pair_exact": {"correct": 25, "total": 25}},
                    "tracking": {"duplicate_count": 0},
                },
                "fixture": index,
            }
        )

    snapshot = acceptance_snapshot(fixtures)

    assert snapshot["status"] == "pass"
    assert snapshot["metrics"]["f1"] == 1
    assert all(snapshot["criteria"].values())

    for fixture in fixtures:
        fixture["dataset"]["split"] = "regression"
    assert acceptance_snapshot(fixtures)["status"] == "not_established"
