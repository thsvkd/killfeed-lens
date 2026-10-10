from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from kfocr.domain.models import Frame


class CaptureState(StrEnum):
    STOPPED = "stopped"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True)
class CaptureHealth:
    state: CaptureState
    reason: str | None = None
    detail: str | None = None
    consecutive_bad_frames: int = 0
    retry_count: int = 0
    resolution: tuple[int, int] | None = None
    last_frame_at_ns: int | None = None


class FrameSource(Protocol):
    def start(self) -> None: ...

    def frames(self) -> Iterator[Frame]: ...

    def stop(self) -> None: ...
