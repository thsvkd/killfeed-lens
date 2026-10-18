"""Read only the HUD's numeric alive count; never infer team membership from it."""
from __future__ import annotations

import math
import re
from collections import deque

import cv2
import numpy as np

from kfocr.domain.models import AliveCount


class AliveCountReader:
    def __init__(self, confidence_min: float = 0.88, evidence_frames: int = 2,
                 evidence_window_ms: int = 750):
        self.confidence_min = confidence_min
        self.evidence_frames = evidence_frames
        self.window_ns = evidence_window_ms * 1_000_000
        self._value: int | None = None
        self._times: deque[int] = deque(maxlen=evidence_frames)
        self._last_seq = -1
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
        if len(self._times) < self.evidence_frames:
            return None
        return AliveCount(value, frame_seq, observed_at_ns)
