from __future__ import annotations

import cv2
import numpy as np


class EdgeChangeDetector:
    def __init__(self, threshold: float = 0.02) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        self.threshold = float(threshold)

    def score(self, previous: np.ndarray, current: np.ndarray) -> float:
        if previous.shape[:2] != current.shape[:2]:
            return 1.0
        previous_edges = cv2.Canny(_gray(previous), 60, 160)
        current_edges = cv2.Canny(_gray(current), 60, 160)
        return float(np.count_nonzero(previous_edges != current_edges) / previous_edges.size)

    def should_infer(self, score: float) -> bool:
        return score >= self.threshold


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ValueError("image must be grayscale, BGR, or BGRA")
