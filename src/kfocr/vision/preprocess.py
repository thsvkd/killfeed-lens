from __future__ import annotations

import cv2
import numpy as np


def preprocess(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        gray = image.copy()
    elif image.ndim == 3 and image.shape[2] == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    elif image.ndim == 3 and image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    else:
        raise ValueError("image must be grayscale, BGR, or BGRA")
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
