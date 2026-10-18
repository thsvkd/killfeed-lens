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
    "capture: {sample_fps: 0}", "capture: {inference_fps_cap: 61}",
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


def test_default_cadence_keeps_two_frame_confirmation_and_cap_follows_capture_rate(tmp_path):
    from kfocr.config import AppConfig, inference_timing_ms, load_config

    config = AppConfig()
    config.validate()
    interval, refresh, longest = inference_timing_ms(config.capture, config.tracker)
    assert round(interval, 1) == 33.3 and refresh == 400  # unchanged rows still re-read at 2.5 Hz
    assert longest <= config.tracker.max_gap_ms <= config.tracker.evidence_window_ms
    path = tmp_path / "slow.yaml"
    path.write_text("capture: {sample_fps: 10}\n")  # older configs: cap no longer above capture
    slow = load_config(path)
    assert inference_timing_ms(slow.capture, slow.tracker)[0] == 100


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


def test_two_roi_profile_roundtrip_and_legacy_profile(tmp_path):
    from kfocr.config import AppConfig, apply_roi_profile, load_config, save_roi_profile
    from kfocr.domain.models import NormalizedRect

    path = tmp_path / "roi.yaml"
    feed = NormalizedRect(0.6, 0.1, 0.4, 0.2)
    alive = NormalizedRect(0.92, 0.01, 0.04, 0.04)
    save_roi_profile(path, feed, (1920, 1080), 100, alive_rect=alive)
    config = AppConfig()
    apply_roi_profile(config, path)
    assert config.roi.rect == feed and config.roi.alive_rect == alive
    assert config.roi.resolution == (1920, 1080) and config.roi.ui_scale == 100
    with_alive_hash = config.config_hash
    save_roi_profile(path, feed, (1920, 1080), 100)
    legacy = load_config(path)
    assert legacy.roi.rect == feed and legacy.roi.alive_rect is None
    assert legacy.config_hash != with_alive_hash


@pytest.mark.parametrize("rect", ["{x: 0.9, y: 0, w: 0.2, h: 0.1}", "[0, 0, 1, 1]"])
def test_invalid_alive_roi_is_rejected(tmp_path, rect):
    from kfocr.config import load_config

    path = tmp_path / "roi.yaml"
    path.write_text(f"roi:\n  alive_rect: {rect}\n")
    with pytest.raises(ValueError, match="ROI"):
        load_config(path)
