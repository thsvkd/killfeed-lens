from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import numpy as np

BBox = tuple[int, int, int, int]


@dataclass(frozen=True)
class Frame:
    source_id: str
    seq: int
    captured_at_ns: int
    image_bgr: np.ndarray
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.seq < 0 or self.captured_at_ns < 0:
            raise ValueError("Frame dimensions and timeline must be valid")
        if self.image_bgr.shape != (self.height, self.width, 3):
            raise ValueError("Frame must contain a BGR image matching its dimensions")


@dataclass(frozen=True)
class NormalizedRect:
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (self.x, self.y, self.w, self.h)):
            raise ValueError("ROI coordinates must be finite values in [0, 1]")
        if self.w <= 0 or self.h <= 0 or self.x + self.w > 1 or self.y + self.h > 1:
            raise ValueError("ROI must have positive size and fit within the frame")


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    bbox: BBox


class VisualKind(StrEnum):
    TEXT = "text"
    ICON = "icon"


@dataclass(frozen=True)
class VisualElement:
    kind: VisualKind
    label: str
    confidence: float
    bbox: BBox


@dataclass
class KillfeedRowCandidate:
    frame_seq: int
    observed_at_ns: int
    bbox: BBox
    elements: list[VisualElement]
    row_confidence: float
    crop_phash: str | None


class EventType(StrEnum):
    KILL = "kill"
    KNOCK = "knock"
    UNKNOWN = "unknown"
    FINISH = "finish"
    BLEEDOUT = "bleedout"
    SUICIDE = "suicide"
    ENVIRONMENT = "environment"


@dataclass(kw_only=True)
class CandidateEvent:
    observed_at_ns: int
    frame_seq: int
    killer_name_raw: str | None
    victim_name_raw: str | None
    killer_name: str | None
    victim_name: str | None
    killer_team: int | None
    victim_team: int | None
    event_type: EventType
    weapon_label: str | None
    headshot: bool | None
    confidence_row: float
    confidence_killer: float
    confidence_victim: float
    confidence_event_type: float
    confidence_total: float
    source_bbox: BBox
    source_crop_ref: str | None = None
    visual_fingerprint: str | None = None


@dataclass(kw_only=True)
class ConfirmedEvent(CandidateEvent):
    event_id: UUID
    session_id: UUID
    confirmed_at_ns: int
    dedup_key: str
    tracker_evidence_count: int


@dataclass
class Session:
    session_id: UUID
    source_type: Literal["screen", "video"]
    started_at_utc: datetime
    ended_at_utc: datetime | None
    source_resolution: tuple[int, int]
    roi_profile_id: str
    app_version: str
    git_commit: str | None
    config_hash: str
    model_manifest_hash: str
    status: Literal["running", "completed", "crashed", "aborted"]
    timeline_origin_ns: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.started_at_utc.tzinfo is None:
            raise ValueError("Session start must include a timezone")
        if self.ended_at_utc is not None and self.ended_at_utc < self.started_at_utc:
            raise ValueError("Session end precedes start")
