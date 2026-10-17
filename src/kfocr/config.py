from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from kfocr.domain.models import NormalizedRect


@dataclass
class CaptureConfig:
    sample_fps: float = 12
    inference_fps_cap: float = 4
    monitor: int = 0


@dataclass
class ROIConfig:
    profile: str = "uncalibrated"
    rect: NormalizedRect | None = None
    ui_scale: float | None = None
    resolution: tuple[int, int] | None = None
    alive_rect: NormalizedRect | None = None


@dataclass
class TrackerConfig:
    evidence_frames: int = 2
    evidence_window_ms: int = 750
    max_gap_ms: int = 500


@dataclass
class ConfidenceConfig:
    confirmed_min: float = 0.88
    candidate_min: float = 0.65


@dataclass
class StorageConfig:
    sqlite_path: str = "data/kfocr.sqlite3"


@dataclass
class DebugConfig:
    save_full_frames: bool = False
    save_event_crops: bool = False
    artifact_dir: str = "data/sessions"
    retention_days: int = 7


@dataclass
class TelemetryConfig:
    enabled: bool = True
    api_key_env: str = "PUBG_API_KEY"
    poll_interval_sec: float = 30
    max_wait_sec: float = 900


@dataclass
class VisionConfig:
    change_threshold: float = 0.02
    icon_template_dir: str | None = None


def inference_timing_ms(capture: CaptureConfig, tracker: TrackerConfig) -> tuple[float, float, float]:
    interval = 1000 / capture.inference_fps_cap
    refresh = max(interval, min(400, tracker.max_gap_ms * 0.8))
    return interval, refresh, refresh + 1000 / capture.sample_fps


@dataclass
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    roi: ROIConfig = field(default_factory=ROIConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)

    @property
    def config_hash(self) -> str:
        values = asdict(self)
        # Output locations do not affect extraction; benchmark temp paths must be reproducible.
        values["storage"].pop("sqlite_path")
        values["debug"].pop("artifact_dir")
        # Template location is machine-specific; template files and thresholds are in the model hash.
        values["vision"].pop("icon_template_dir")
        raw = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def validate(self) -> None:
        _number(self.capture.sample_fps, 1, 60, "capture.sample_fps")
        _number(self.capture.inference_fps_cap, 0.1, 4, "capture.inference_fps_cap")
        if self.capture.inference_fps_cap > self.capture.sample_fps:
            raise ValueError("Inference FPS cannot exceed capture FPS")
        _integer(self.capture.monitor, 0, 64, "capture.monitor")
        _integer(self.tracker.evidence_frames, 2, 100, "tracker.evidence_frames")
        _integer(self.tracker.evidence_window_ms, 1, 10000, "tracker.evidence_window_ms")
        _integer(self.tracker.max_gap_ms, 1, 10000, "tracker.max_gap_ms")
        _, _, longest_inference_gap_ms = inference_timing_ms(self.capture, self.tracker)
        if longest_inference_gap_ms > 500:
            raise ValueError("Capture/OCR cadence must allow a periodic refresh within 500ms")
        if self.tracker.max_gap_ms < longest_inference_gap_ms:
            raise ValueError("Tracker max_gap_ms must cover the sampled inference cadence")
        if self.tracker.evidence_window_ms < longest_inference_gap_ms * (
            self.tracker.evidence_frames - 1
        ):
            raise ValueError("Evidence window is too short for the required distinct frames")
        _number(self.confidence.candidate_min, 0, 1, "confidence.candidate_min")
        _number(self.confidence.confirmed_min, 0, 1, "confidence.confirmed_min")
        if self.confidence.candidate_min > self.confidence.confirmed_min:
            raise ValueError("Candidate threshold cannot exceed confirmed threshold")
        for value in (self.debug.save_full_frames, self.debug.save_event_crops,
                      self.telemetry.enabled):
            if not isinstance(value, bool):
                raise ValueError("Boolean configuration must use true/false")
        _integer(self.debug.retention_days, 1, 3650, "debug.retention_days")
        _number(self.vision.change_threshold, 0, 1, "vision.change_threshold")
        _number(self.telemetry.poll_interval_sec, 0.1, 900, "telemetry.poll_interval_sec")
        _number(self.telemetry.max_wait_sec, 0, 900, "telemetry.max_wait_sec")
        for text_value in (self.storage.sqlite_path, self.debug.artifact_dir,
                      self.telemetry.api_key_env, self.roi.profile):
            if not isinstance(text_value, str) or not text_value:
                raise ValueError("Paths, profile and environment variable must be nonempty strings")
        if self.roi.ui_scale is not None:
            _number(self.roi.ui_scale, 1, 1000, "roi.ui_scale")
        if self.roi.resolution is not None:
            if len(self.roi.resolution) != 2:
                raise ValueError("ROI resolution must be [width, height]")
            for dim in self.roi.resolution:
                _integer(dim, 1, 32768, "roi.resolution")
        if self.vision.icon_template_dir is not None and not isinstance(
            self.vision.icon_template_dir, str
        ):
            raise ValueError("Icon template directory must be a path string")


def _number(value, lower: float, upper: float, label: str) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError(f"Invalid numeric limit: {label}")


def _integer(value, lower: int, upper: int, label: str) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"Invalid integer limit: {label}")


_CONFIG_PATH_KEYS = {
    "storage": ("sqlite_path",),
    "debug": ("artifact_dir",),
    "vision": ("icon_template_dir",),
}


def load_config(path: str | Path | None = None) -> AppConfig:
    config = AppConfig()
    if path is not None:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError("Invalid YAML configuration") from exc
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a mapping")
        for section, values in raw.items():
            if section not in {item.name for item in fields(config)}:
                raise ValueError(f"Unknown configuration section: {section}")
            target = getattr(config, section)
            if not isinstance(values, dict) or set(values) - {item.name for item in fields(target)}:
                raise ValueError(f"Invalid configuration keys in {section}")
            values = dict(values)
            for key in _CONFIG_PATH_KEYS.get(section, ()):
                if isinstance(values.get(key), str) and values[key]:
                    # Paths written in a config file are relative to that file, not the cwd.
                    values[key] = str(Path(path).resolve().parent / values[key])
            if section == "roi":
                for key in ("rect", "alive_rect"):
                    if values.get(key) is not None:
                        try:
                            values[key] = NormalizedRect(**values[key])
                        except (TypeError, ValueError) as exc:
                            raise ValueError(f"Invalid normalized ROI: {key}") from exc
                if values.get("resolution") is not None:
                    try:
                        values["resolution"] = tuple(values["resolution"])
                    except TypeError as exc:
                        raise ValueError("Invalid ROI resolution") from exc
            setattr(config, section, type(target)(**values))
    config.validate()
    return config


def apply_roi_profile(config: AppConfig, path: str | Path) -> None:
    profile = load_config(path)
    if profile.roi.rect is None:
        raise ValueError("ROI profile must contain a calibrated rect")
    config.roi = profile.roi


def save_roi_profile(path: Path, rect: NormalizedRect, resolution: tuple[int, int],
                     ui_scale: float | None = None, *,
                     alive_rect: NormalizedRect | None = None) -> None:
    profile = {"roi": {"profile": path.stem, "rect": asdict(rect),
                       "resolution": list(resolution), "ui_scale": ui_scale}}
    if alive_rect is not None:
        profile["roi"]["alive_rect"] = asdict(alive_rect)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
