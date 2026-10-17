from __future__ import annotations

import re
from collections.abc import Callable

import cv2
import numpy as np

from kfocr.domain.models import BBox, VisualElement, VisualKind

# A team badge is a solid colour box with white digits, outlined by a 1px pure-black border.
# The killfeed background is translucent and never reaches pure black under the border, so the
# border column on the name side plus a top or bottom border row locates the box; OCR reading
# the whole row glues badge digits to names ("49U_Ula") or drops them, so the box is read alone.
_BLACK = 8
_DIGITS = re.compile(r"\d{1,2}")


def find_badges(image: np.ndarray, *, min_height: int = 10, max_height: int = 60) -> list[BBox]:
    if image.ndim != 3 or image.size == 0:
        return []
    black = image.max(axis=2) <= _BLACK
    height, width = black.shape
    padded = np.zeros((height + 2, width), np.int8)
    padded[1:-1] = black
    edges = np.diff(padded, axis=0).T  # per column: +1 where a black run starts, -1 past its end
    xs, tops = np.nonzero(edges == 1)
    bottoms = np.nonzero(edges == -1)[1]
    sizes = bottoms - tops
    # Vectorised prefilter (dark scenes hold thousands of runs): a border column has a non-black
    # neighbour beside most of its length, where black background or text outline has none.
    beside = black.copy()
    beside[:, 1:] &= black[:, :-1]
    beside[:, :-1] &= black[:, 1:]
    open_rows = np.vstack([np.zeros((1, width), np.int32), np.cumsum(black & ~beside, axis=0)])
    keep = (sizes >= min_height) & (sizes <= max_height)
    xs, tops, bottoms, sizes = xs[keep], tops[keep], bottoms[keep], sizes[keep]
    keep = open_rows[bottoms - 2, xs] - open_rows[tops + 2, xs] >= 0.8 * (sizes - 4)
    found: list[BBox] = []
    for x, top, bottom in zip(xs[keep], tops[keep], bottoms[keep], strict=True):
        box = _box_beside(image, black, int(x), int(top), int(bottom))
        if box is not None:
            found.append(box)
    boxes: list[BBox] = []
    for box in sorted(found, key=lambda b: (b[1], b[0])):
        if not any(abs(box[0] - o[0]) < 5 and abs(box[1] - o[1]) < 5 for o in boxes):
            boxes.append(box)
    return boxes


def _box_beside(image: np.ndarray, black: np.ndarray, x: int, top: int, bottom: int) -> BBox | None:
    """The fill on either side of a black border column, if it is boxed like a badge."""
    size, width = bottom - top, black.shape[1]
    inner = slice(top + 2, bottom - 2)
    for step in (-1, 1):
        start = x + step
        if not 0 <= start < width or black[inner, start].mean() > 0.2:
            continue
        if np.median(image[inner, start].astype(np.int16), axis=0).max() < 25:
            continue  # a dark background, not a coloured fill
        end = start
        while 0 <= end < width and not black[inner, end].all() and abs(end - start) <= 1.8 * size:
            end += step
        span = abs(end - start)
        clipped = not 0 <= end < width  # cut by the image edge; the far border is missing
        if not (0.45 * size <= span <= 1.6 * size or (clipped and span >= 0.3 * size)):
            continue
        left, right = (end + 1, start + 1) if step < 0 else (start, end)
        left, right = max(0, left), min(width, right)
        cut = top == 0 or bottom == black.shape[0]  # the box runs off the image edge
        if not cut and max(black[top, left:right].mean(), black[bottom - 1, left:right].mean()) < 0.7:
            continue
        white = image[top:bottom, left:right].min(axis=2) > 170
        rows = np.flatnonzero(white.any(axis=1))
        # Digits sit inside fill margins; black text outlines in dark scenes box whole letters.
        if (white.mean() < 0.03 or rows[0] < 0.15 * size or size - 1 - rows[-1] < 0.15 * size
                or rows[-1] - rows[0] + 1 > 0.65 * size):
            continue
        return (left, top, right, bottom)
    return None


def read_badges(
    image: np.ndarray, read: Callable[[list[np.ndarray]], list[tuple[str, float]]]
) -> list[VisualElement]:
    """Read each badge box on its own; boxes whose text is not a team number are dropped."""
    boxes = find_badges(image)
    if not boxes:
        return []
    # At native size the recognizer drops a narrow "1" or a repeated digit ("41"/"44" -> "4").
    crops = [cv2.copyMakeBorder(cv2.resize(image[top:bottom, left:right], None, fx=2, fy=2,
                                           interpolation=cv2.INTER_CUBIC),
                                8, 8, 12, 12, cv2.BORDER_REPLICATE)
             for left, top, right, bottom in boxes]
    return [VisualElement(VisualKind.BADGE, text.strip(), float(confidence), box)
            for box, (text, confidence) in zip(boxes, read(crops), strict=True)
            if _DIGITS.fullmatch(text.strip())]


def mask_badges(image: np.ndarray, badges: list[VisualElement]) -> np.ndarray:
    """Paint read badges with the row background so row OCR cannot glue their digits onto the
    names beside them; a black box instead made the detector skip an adjacent name."""
    if not badges:
        return image
    masked = image.copy()
    for badge in badges:
        left, top, right, bottom = badge.bbox
        beside = np.concatenate([image[top:bottom, max(0, left - 4):max(0, left - 2)].reshape(-1, 3),
                                 image[top:bottom, right + 2:right + 4].reshape(-1, 3)])
        masked[top:bottom, max(0, left - 1):right + 1] = np.median(beside, axis=0) if len(beside) else 0
    return masked
