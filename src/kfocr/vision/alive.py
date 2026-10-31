"""Read only the HUD's numeric alive count; never infer team membership from it."""
from __future__ import annotations

import math
import re
from collections import deque

import cv2
import numpy as np

from kfocr.domain.models import AliveCount

# The HUD digits read right at 0.55-0.99 confidence (a "66" stayed under 0.88 for 17 s), and the
# typical misread drops a digit at high confidence ("44" read "4" at 0.89). So the bar is low and
# every count is checked against the last one instead. On three recorded matches: wrong counts
# 8 -> 0, lag behind the HUD p90 2.5-4 s -> 0.5-1 s; either check alone let one misread through.
CONFIDENCE_MIN = 0.7
# On five recorded matches deaths came at most 5 a second, 8 in 10 s and 21 a minute, and recalls
# brought back at most 7 at once: the count moves no faster than this either way, so within a
# minute of the last count a misread cannot become the reference the next counts are judged by.
# The next match comes minutes later.
_STEP_MAX, _STEP_PER_S = 8, 0.5
# The last count stays the reference this long. pipeline.py judges match boundaries on the counts
# let through here (gone MATCH_ENDED_NS, back NEW_MATCH_RISE higher): a next match within this of
# the last count is judged once it expires (test_a_new_match_after_a_short_gap_is_judged_late).
_FRESH_NS = 60_000_000_000


def _lost_digit(short: int, long: int) -> bool:
    """`short` reads as `long` with one digit lost ("9" for 19 or 90)."""
    a, b = str(short), str(long)
    return len(a) == len(b) - 1 and any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def _number_crop(image: np.ndarray, text: str) -> tuple[np.ndarray, int] | None:
    """Retry only when aligned bright glyphs outnumber the recognized digits."""
    low = image.min(axis=2) if image.ndim == 3 else image
    high = image.max(axis=2) if image.ndim == 3 else image
    mask = np.asarray((low >= 180) & (high.astype(int) - low <= 40), dtype=np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    glyphs = [(x, y, w, h) for x, y, w, h, area in stats[1:]
              if h >= max(8, image.shape[0] / 4) and w <= h and area >= w * h / 5]
    if not 1 <= len(glyphs) <= 3:
        return None
    # A dimmer digit may be absent from this mask. Never shorten an already complete read.
    if re.fullmatch(r"[0-9]{1,3}", text) and len(text) >= len(glyphs):
        return None
    height = max(h for _, _, _, h in glyphs)
    bottoms = [y + h for _, y, _, h in glyphs]
    if (min(h for _, _, _, h in glyphs) < height * 0.7
            or max(bottoms) - min(bottoms) > height * 0.2):
        return None
    pad = max(2, round(height / 10))
    x0, y0 = min(x for x, _, _, _ in glyphs), min(y for _, y, _, _ in glyphs)
    x1, y1 = max(x + w for x, _, w, _ in glyphs), max(bottoms)
    return (image[max(0, y0 - pad):min(image.shape[0], y1 + pad),
                  max(0, x0 - pad):min(image.shape[1], x1 + pad)], len(glyphs))


class AliveCountReader:
    def __init__(self, confidence_min: float = CONFIDENCE_MIN, evidence_frames: int = 2,
                 evidence_window_ms: int = 750):
        self.confidence_min = confidence_min
        self.evidence_frames = evidence_frames
        self.window_ns = evidence_window_ms * 1_000_000
        self._value: int | None = None
        self._times: deque[int] = deque(maxlen=evidence_frames)
        self._last_seq = -1
        self._accepted: AliveCount | None = None
        self._unjudged = False  # the reference came with nothing to judge it by (after a gap)
        # Last read that saw a number at all, confirmed or not: the HUD is on screen (match
        # boundaries). Misreads like "G1" for 61 or a low-confidence "90" still count as shown.
        self.shown_ns: int | None = None

    def read(self, image: np.ndarray, recognizer, frame_seq: int,
             observed_at_ns: int) -> AliveCount | None:
        if frame_seq <= self._last_seq:
            return None
        self._last_seq = frame_seq
        value = None
        try:
            if image.size and np.any(image):
                # Recognition only avoids a text detection pass for this single, tight number crop.
                scale = max(1.0, 48 / image.shape[0])
                line = cv2.resize(image, None, fx=scale, fy=scale,
                                  interpolation=cv2.INTER_CUBIC)
                read_lines = getattr(recognizer, "read_lines", None)
                reads = (read_lines([line]) if read_lines else
                         [(t.text, t.confidence) for t in recognizer.recognize(line)])
                if len(reads) == 1:
                    text, confidence = reads[0]
                    text = text.strip()
                    # Padding shrinks condensed/repeated digits in the OCR model's fixed-height
                    # input. Retry a visible three-glyph "100" read as "10", without replacing
                    # valid original evidence when the optional crop is incomplete or fails.
                    crop = _number_crop(image, text)
                    if crop is not None:
                        number, digits = crop
                        scale = max(1.0, 48 / number.shape[0])
                        line = cv2.resize(number, None, fx=scale, fy=scale,
                                          interpolation=cv2.INTER_CUBIC)
                        try:
                            retry = (read_lines([line]) if read_lines else
                                     [(t.text, t.confidence) for t in recognizer.recognize(line)])
                            if len(retry) == 1:
                                candidate, score = retry[0]
                                candidate = candidate.strip()
                                if (math.isfinite(score) and self.confidence_min <= score <= 1
                                        and re.fullmatch(rf"[0-9]{{{digits}}}", candidate)
                                        and int(candidate) <= 100):
                                    text, confidence = candidate, score
                        except Exception:
                            pass  # optional correction must not discard the original HUD read
                    # no HUD (lobby, loading, starting island) reads "" or noise below 0.2
                    if confidence >= 0.5 and re.search(r"[0-9]", text):
                        self.shown_ns = observed_at_ns
                    if (math.isfinite(confidence) and self.confidence_min <= confidence <= 1
                            and re.fullmatch(r"[0-9]{1,3}", text) and int(text) <= 100):
                        value = int(text)
        except Exception:
            self._value = None
            self._times.clear()
            raise
        if value is None or value != self._value:
            self._times.clear()
        self._value = value
        if value is None:
            return None
        while self._times and observed_at_ns - self._times[0] > self.window_ns:
            self._times.popleft()
        self._times.append(observed_at_ns)
        if len(self._times) < self.evidence_frames or self._implausible(value, observed_at_ns):
            return None
        self._accepted = AliveCount(value, frame_seq, observed_at_ns)
        return self._accepted

    def _implausible(self, value: int, observed_at_ns: int) -> bool:
        """A change faster than players die or come back, or the last count with a digit lost
        ("19" -> "9"). A first count after a gap is taken as is; if it was itself such a misread
        ("96" read "9" as the HUD came back), the full number replaces it. Called once per
        candidate: it also keeps `_unjudged` up to date."""
        last = self._accepted
        if last is None or observed_at_ns - last.observed_at_ns >= _FRESH_NS:
            self._unjudged = True
            return False
        if self._unjudged and _lost_digit(last.count, value):
            return False
        seconds = (observed_at_ns - last.observed_at_ns) / 1e9
        bad = (_lost_digit(value, last.count)
               or abs(value - last.count) > _STEP_MAX + _STEP_PER_S * seconds)
        self._unjudged = self._unjudged and (bad or value == last.count)  # a plausible change
        return bad
