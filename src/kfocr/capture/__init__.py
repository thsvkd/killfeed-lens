"""Pixel-only frame sources."""

from .base import CaptureHealth, CaptureState, FrameSource
from .replay import ReplaySource
from .screen import ScreenSource

__all__ = ["CaptureHealth", "CaptureState", "FrameSource", "ReplaySource", "ScreenSource"]
