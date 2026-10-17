from pathlib import Path

import pytest


def test_config_loads_roi_and_hash_is_independent_of_yaml_order(tmp_path):
    from kfocr.config import load_config

    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("capture:\n  sample_fps: 10\nroi:\n  rect: {x: 0.5, y: 0.0, w: 0.5, h: 0.4}\n")
    b.write_text("roi:\n  rect: {h: 0.4, w: 0.5, y: 0.0, x: 0.5}\ncapture:\n  sample_fps: 10\n")
    first = load_config(a)
    assert first.roi.rect.x == 0.5
    assert first.capture.sample_fps == 10
    assert first.config_hash == load_config(b).config_hash
    assert first.debug.save_full_frames is False


@pytest.mark.parametrize("text", [
    "capture: {sample_fps: 0}", "capture: {inference_fps_cap: 50}",
    "confidence: {candidate_min: 0.9, confirmed_min: 0.8}",
    "telemetry: {api_key: secret}", "tracker: {evidence_frames: 1}",
    "capture: {sample_fps: .nan}", "capture: {sample_fps: '12'}",
    "capture: {inference_fps_cap: 1}",
    "tracker: {max_gap_ms: 100}",
    "tracker: {evidence_window_ms: 350}",
])
def test_config_rejects_invalid_limits_and_plaintext_secrets(tmp_path, text):
    from kfocr.config import load_config

    path = tmp_path / "invalid.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


def test_config_relative_paths_resolve_next_to_config_file(tmp_path, monkeypatch):
    from kfocr.config import load_config

    (tmp_path / "data").mkdir()
    (tmp_path / "elsewhere").mkdir()
    path = tmp_path / "data" / "config.yaml"
    path.write_text("storage: {sqlite_path: db.sqlite3}\ndebug: {artifact_dir: sessions}\n"
                    "vision: {icon_template_dir: icons/2026-10}\n")
    monkeypatch.chdir(tmp_path / "elsewhere")
    config = load_config(Path("..") / "data" / "config.yaml")
    base = (tmp_path / "data").resolve()
    assert Path(config.storage.sqlite_path) == base / "db.sqlite3"
    assert Path(config.debug.artifact_dir) == base / "sessions"
    assert Path(config.vision.icon_template_dir) == base / "icons" / "2026-10"


def test_config_hash_ignores_output_locations_but_changes_with_inference_policy():
    from kfocr.config import AppConfig

    a, b = AppConfig(), AppConfig()
    b.storage.sqlite_path = "/tmp/random-benchmark-location/events.db"
    b.debug.artifact_dir = "/tmp/random-output"
    b.vision.icon_template_dir = "/elsewhere/icons/2026-10"
    assert a.config_hash == b.config_hash
    b.confidence.confirmed_min = 0.9
    assert a.config_hash != b.config_hash
