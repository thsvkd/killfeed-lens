from __future__ import annotations

import cv2
import numpy as np

# Ink: bright strokes narrower than the kernel (text, icons, badge digits) in any colour channel,
# so tinted squad rows count too. A top-hat ignores the large, slowly changing background.
_KERNEL = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
_NEAR = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
_INK_LEVEL = 80


class EdgeChangeDetector:
    """Did the killfeed text change since the frame last read?

    The feed is translucent over the moving game, so raw edges changed on ~50% of 30 Hz OBS
    frames. This scores *stable* ink instead: ink present in this frame and (within 1 px) in the
    frame before; text persists across frames, a moving background mostly does not. The score
    is the share of ROI pixels whose stable ink appeared or vanished (1 px tolerance). On three
    OBS segments it cut reads (incl. the 400 ms refresh) from 45-52% to 13-27% of frames and
    still fired for every replayed event; the extra frame it waits is covered by the tracker's
    pending re-read. `previous` should be a frame earlier passed as `current` (the pipeline
    passes the last read ROI); any other image is compared by its plain ink.
    """

    def __init__(self, threshold: float = 0.008) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        self.threshold = float(threshold)
        self._last: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None  # image, ink, stable
        self._base: tuple[np.ndarray, np.ndarray] | None = None  # previous image, its stable ink

    def score(self, previous: np.ndarray, current: np.ndarray) -> float:
        if previous.shape[:2] != current.shape[:2]:
            return 1.0
        ink = _ink(current)
        last = self._last
        stable = ink & _near(last[1]) if last is not None and last[1].shape == ink.shape else ink
        if self._base is None or self._base[0] is not previous:
            known = last[2] if last is not None and last[0] is previous else None
            self._base = (previous, known if known is not None else _ink(previous))
        self._last = (current, ink, stable)
        base = self._base[1]
        moved = np.count_nonzero(stable & ~_near(base)) + np.count_nonzero(base & ~_near(stable))
        return float(moved / ink.size)

    def should_infer(self, score: float) -> bool:
        return score >= self.threshold


def _ink(image: np.ndarray) -> np.ndarray:
    hat = cv2.morphologyEx(_bgr(image), cv2.MORPH_TOPHAT, _KERNEL)
    if hat.ndim == 3:  # channel max; cv2 is ~13x faster here than hat.max(axis=2)
        b, g, r = cv2.split(hat)
        hat = cv2.max(cv2.max(b, g), r)
    return hat >= _INK_LEVEL


def _near(mask: np.ndarray) -> np.ndarray:
    return cv2.dilate(mask.view(np.uint8), _NEAR) > 0


def _bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 3):
        return image
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    raise ValueError("image must be grayscale, BGR, or BGRA")
