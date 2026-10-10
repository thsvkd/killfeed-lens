from __future__ import annotations

import math

import numpy as np

from kfocr.domain.models import NormalizedRect


def pixel_rect(rect: NormalizedRect, width: int, height: int) -> tuple[int, int, int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    left = max(0, min(width, math.floor(rect.x * width)))
    top = max(0, min(height, math.floor(rect.y * height)))
    right = max(left + 1, min(width, math.ceil((rect.x + rect.w) * width)))
    bottom = max(top + 1, min(height, math.ceil((rect.y + rect.h) * height)))
    return left, top, right, bottom


def crop_roi(image: np.ndarray, rect: NormalizedRect) -> np.ndarray:
    if image.ndim not in (2, 3):
        raise ValueError("image must be a grayscale or color array")
    height, width = image.shape[:2]
    left, top, right, bottom = pixel_rect(rect, width, height)
    return image[top:bottom, left:right].copy()
