"""Killfeed names without a text detector.

Each kill/knock marker is one row. Its names are runs of ink columns between the team badges, the
weapon icon and the marker; a "[TAG]" is dropped at its closing bracket. Glyphs are read by
nearest-neighbour NCC against glyph templates cut from captures at the same resolution, and a name
the templates do not explain well is read by recognition-only OCR on its own crop instead.
Badges are found from the same layout: the digits at a row's ends sit on a coloured fill that
differs from the row strip, which still holds in compressed video where the black border blurs.
"""
from __future__ import annotations

import hashlib
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from kfocr.domain.models import BBox, OCRToken, VisualElement, VisualKind
from kfocr.domain.parser import nickname
from kfocr.logging import log_event
from kfocr.vision.badges import find_badges, read_badges

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "glyphs"
# Marker template heights at 2560x1440; a row's pixel geometry scales with its marker.
_MARKER_HEIGHT = {"kill_marker": 22, "knock_marker": 17}
_UP, _DOWN = 16, 5  # text window: baseline-16 (above the caps) .. baseline+5 (descenders)
_CANVAS = 20  # glyph canvas width; the widest glyph ("M", "W", "m") is ~15px at 1440p
MIN_SCORE = 0.8  # a name whose weakest glyph matches worse is read by recognition instead
_BADGE_ROWS = 23  # badge box height at 2560x1440
_DIGIT_SCORE = 0.8  # lower lets blurred "1"s pass as other digits (live 49 read as 19)

ReadLines = Callable[[list[np.ndarray]], list[tuple[str, float]]]
Run = tuple[int, int]


def _odd(value: float) -> int:
    return max(3, int(round(value)) | 1)


def _runs(columns: np.ndarray, offset: int = 0) -> list[Run]:
    """[start, end) runs of True, shifted by offset."""
    edges = np.diff(np.concatenate([[0], columns.astype(np.int8), [0]]))
    return [(int(a) + offset, int(b) + offset)
            for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True)]


def _groups(blobs: list[Run], gap: float) -> list[list[Run]]:
    groups: list[list[Run]] = []
    for blob in blobs:
        if groups and blob[0] - groups[-1][-1][1] < gap:
            groups[-1].append(blob)
        else:
            groups.append([blob])
    return groups


@dataclass
class Name:
    blobs: list[Run]  # glyph ink columns of the name itself
    start: int  # first text column on this side ("[TAG]" included), for badge-to-name gaps

    @property
    def end(self) -> int:
        return self.blobs[-1][1]


def _contrast(band: np.ndarray, scale: float) -> tuple[np.ndarray, np.ndarray]:
    """Text is a local peak above the translucent strip: ink where a pixel clears half the way from
    the background (wide median) to the local text level (small max). The darkest channel keeps
    white and grey text while coloured scenery and badges drop out."""
    gray = band.min(axis=2)
    background = cv2.medianBlur(gray, _odd(31 * scale)).astype(np.int16)
    contrast = gray.astype(np.int16) - background
    size = _odd(9 * scale)
    level = cv2.dilate(gray, np.ones((size, size), np.uint8)).astype(np.int16) - background
    return contrast, (contrast > level // 2) & (level >= 18)


def _closing_bracket(ink: np.ndarray, x0: int, x1: int, scale: float) -> int | None:
    """Column of the last "]" in [x0, x1): a bar from above the caps to below the baseline with a
    short bottom serif (ink below the baseline, empty middle) two or three columns to its left
    that ends within five. The top serif can sit above the window. An underscore running under
    the next glyph's stem looks like a serif too, but runs on."""
    up = int(round(_UP * scale))
    # Brackets reach baseline+3; blur bleeds other tall glyphs at most a row below the baseline.
    low = ink[up + 2:up + 2 + max(2, round(2 * scale))].any(axis=0)
    middle = ink[round(5 * scale):max(round(5 * scale) + 1, up - round(3 * scale))]
    full = (ink[:max(3, round(3 * scale))].any(axis=0) & low
            & (ink[2:up + 1].mean(axis=0) >= 0.9))
    for x in np.flatnonzero(full[x0:x1])[::-1] + x0:
        beyond = int(x - round(6 * scale))
        if beyond >= 0 and low[beyond]:
            continue
        for step in (2, 3):
            serif = int(x - round(step * scale))
            if serif >= 0 and low[serif] and not middle[:, serif].any():
                return int(x)
    return None


class Row:
    """One marker row of an image: its text window (ink and contrast, marker and black-boxed badges
    cleared), the glyph runs on each side of the marker and, once badges are settled, the names."""

    def __init__(self, image: np.ndarray, marker: VisualElement, boxed: list[BBox]):
        height, width = image.shape[:2]
        left, top, right, bottom = marker.bbox
        self.scale = s = (bottom - top) / _MARKER_HEIGHT.get(marker.label, 22)
        self.centre = yc = (top + bottom) / 2
        band0, band1 = max(0, int(yc - 36 * s)), min(height, int(yc + 36 * s) + 1)
        contrast, ink = _contrast(image[band0:band1], s)
        pad = max(1, (bottom - top) // 4) + round(2 * s)
        ink[:, max(0, left - pad):right + pad] = False  # the marker and its grey shadow
        mine = [box for box in boxed if box[1] <= yc <= box[3]]
        for box in mine:
            ink[:, max(0, box[0] - round(2 * s)):box[2] + round(2 * s)] = False
        self.left_edge = max([box[2] + 1 for box in mine if box[2] <= left], default=0)
        self.right_edge = min([box[0] - 1 for box in mine if box[0] >= right], default=width)
        self.boxed_left = any(box[2] <= left for box in mine)
        self.boxed_right = any(box[0] >= right for box in mine)
        # Baseline: the row most glyph runs end on (descenders, underscores and box edges are the
        # minority, and the dashes end higher), within the row's own strip and a few rows of where
        # the marker puts it (baseline = marker centre + 7 at 1440p), so a neighbouring row that
        # is sliding in cannot pull it.
        p0, p1 = max(0, int(yc - 12 * s) - band0), min(band1, int(yc + 12 * s)) - band0
        near = ink[p0:p1, self.left_edge:self.right_edge]
        expected = int(round(yc + 7 * s)) - band0 - p0
        bottoms = [bottom for a, b in _runs(near.any(axis=0)) if near[:, a:b].sum() >= 6 * s * s
                   and abs((bottom := int(np.flatnonzero(near[:, a:b].any(axis=1))[-1])) - expected) <= 3 * s]
        if bottoms:
            counts = np.bincount(bottoms)
            base = band0 + p0 + int(np.flatnonzero(counts == np.max(counts))[-1])
        else:
            base = int(round(yc + 7 * s))
        up, down = int(round(_UP * s)), int(round(_DOWN * s))
        self.top = base - up
        rows = up + down + 1
        def window(source: np.ndarray, dtype: type) -> np.ndarray:
            out: np.ndarray = np.zeros((rows, width), dtype)
            lo, hi = max(self.top, band0), min(self.top + rows, band1)
            if hi > lo:
                out[lo - self.top:hi - self.top] = source[lo - band0:hi - band0]
            return out

        self.ink, self.contrast = window(ink, bool), window(contrast, np.int16)
        self.marker = marker
        self.width = width
        killer_end, victim_start = left - pad, right + pad
        killer_side = self._trim(self._blobs(self.left_edge, max(self.left_edge, killer_end)))
        # The weapon silhouette (the headshot icon sits right of it) is the wide run next to the
        # marker; wide runs farther out are scenery beside the row.
        weapon = [a for a, b in killer_side if b - a >= 24 * s and b >= killer_end - 80 * s]
        self.killer_side = [blob for blob in killer_side
                            if blob[1] - blob[0] < 24 * s and (not weapon or blob[1] <= weapon[-1])]
        self.victim_side = [blob for blob in self._trim(self._blobs(
            min(victim_start, self.right_edge), self.right_edge)) if blob[1] - blob[0] < 24 * s]
        self.settle([])

    @property
    def height(self) -> int:
        return self.ink.shape[0]

    def settle(self, badges: list[BBox]) -> None:
        """Names from the side runs, leaving out runs inside this row's (read) badge boxes."""
        mine = [box for box in badges if _in_row(box, self)]

        def outside(blobs: list[Run]) -> list[Run]:
            return [blob for blob in blobs
                    if not any(box[0] <= blob[0] and blob[1] <= box[2] for box in mine)]

        self.killer = self.name(outside(self.killer_side), True)
        self.victim = self.name(outside(self.victim_side), False)

    def badge_candidates(self, image: np.ndarray) -> list[list[BBox]]:
        """Badge boxes, nearest the name first, at each row end the black-border search missed."""
        return [_badge_boxes(image, self, left)
                for left, boxed in ((True, self.boxed_left), (False, self.boxed_right)) if not boxed]

    def _blobs(self, x0: int, x1: int) -> list[Run]:
        return _runs(self.ink[:, x0:x1].any(axis=0), x0) if x1 > x0 else []

    def _trim(self, blobs: list[Run]) -> list[Run]:
        """Drop specks at either end of a side (marker shadow, badge border remnants)."""
        speck = 6 * self.scale ** 2
        while blobs and self.ink[:, blobs[0][0]:blobs[0][1]].sum() < speck:
            blobs = blobs[1:]
        while blobs and self.ink[:, blobs[-1][0]:blobs[-1][1]].sum() < speck:
            blobs = blobs[:-1]
        return blobs

    def name(self, blobs: list[Run], left: bool) -> Name | None:
        """The name nearest the marker: the first space-separated group of 2+ glyph runs, after
        the closing bracket of a "[TAG]" when the group is one. On the killer side the name's
        start moves to an adjacent tag's start, so the badge beyond the tag still reads as near."""
        s, mask = self.scale, self.ink
        groups = _groups(blobs, 7 * s)
        order = groups[::-1] if left else groups
        for index, group in enumerate(order):
            bracket = _closing_bracket(mask, group[0][0], group[-1][1], s)
            text = group if bracket is None else [blob for blob in group if blob[0] > bracket]
            if len(text) < 2:
                continue
            start = group[0][0] if bracket is not None else text[0][0]
            if left and bracket is None and index + 1 < len(order):
                tag = order[index + 1]
                if (text[0][0] - tag[-1][1] <= 14 * s
                        and _closing_bracket(mask, tag[0][0], tag[-1][1], s) is not None):
                    start = tag[0][0]
            return Name(text, start)
        return None


def _badge_boxes(image: np.ndarray, row: Row, left: bool) -> list[BBox]:
    """The badge at one row end, by colour: scanning outward from the middle of the name, the
    first run of columns (8-30px at 1440p) whose fill colour, with the text and its halo left out,
    is not the row strip's. Digits glued to a name still sit on the fill, so they split off here,
    and the box needs no border, which compressed video blurs."""
    name = row.killer if left else row.victim
    if name is None:
        return []
    s, width = row.scale, row.width
    first, last = name.blobs[0][0], name.end
    x0 = max(0, (name.start if left else first) - round(120 * s))
    x1 = last if left else min(width, last + round(120 * s))
    # Rows inside a badge box only: the window reaches past the box, where the strip shows.
    y0, y1 = max(0, int(row.centre - 10 * s)), min(image.shape[0], int(row.centre + 10 * s) + 1)
    if x1 - x0 < 8 or y1 - y0 < 4:
        return []
    ink = np.zeros((y1 - y0, x1 - x0), np.uint8)
    w0, w1 = max(y0, row.top), min(y1, row.top + row.height)
    if w1 > w0:
        ink[w0 - y0:w1 - y0] = row.ink[w0 - row.top:w1 - row.top, x0:x1]
    halo = cv2.dilate(ink, np.ones((5, 5), np.uint8))
    pixels = image[y0:y1, x0:x1].astype(np.float32)
    pixels[halo.astype(bool)] = np.nan
    with warnings.catch_warnings():  # all-NaN columns stay NaN
        warnings.simplefilter("ignore", RuntimeWarning)
        columns = np.nanmedian(pixels, axis=0)  # (x1 - x0, 3); NaN where text covers the column
        middle = (first + last) // 2 - x0
        quarter = max(1, (last - first) // 4)
        strip = np.nanmedian(columns[max(0, middle - quarter):middle + quarter], axis=0)
    if not np.isfinite(strip).all():
        return []
    # Runs of columns whose fill is not the strip's; text-covered (NaN) columns bridge, a change of
    # hue (badge beside scenery) splits. Shading within a badge only changes brightness.
    valid = np.isfinite(columns).all(axis=1)
    safe = np.where(valid[:, None], columns, 0)
    norms = np.linalg.norm(safe, axis=1)
    hues = safe / np.maximum(norms, 1.0)[:, None]
    strip_norm = float(np.linalg.norm(strip))
    if strip_norm < 1:
        similar = norms < 25
    else:
        similar = ((np.linalg.norm(hues - strip / strip_norm, axis=1) < 0.12)
                   & (norms > 0.55 * strip_norm) & (norms < 1.8 * strip_norm))
    fill = valid & ~similar & (np.linalg.norm(safe - strip, axis=1) >= 25)
    xs = np.flatnonzero(valid).tolist()  # NaN columns neither extend nor break a run
    segments: list[list[int]] = []
    for i, x in enumerate(xs):
        if not fill[x]:
            continue
        prev = xs[i - 1] if i else None
        if (prev is not None and fill[prev] and segments and segments[-1][1] == prev + 1
                and float(np.linalg.norm(hues[x] - hues[prev])) < 0.2):
            segments[-1][1] = x + 1
        else:
            segments.append([x, x + 1])
    # Grow each run over the badge's own shading and digits: text-covered columns and fill of a
    # similar hue, until the strip (or another hue) starts; the digits' dark band would split it.
    grown = []
    for a, b in segments:
        seed = hues[a]
        stop = valid & (similar | (np.linalg.norm(hues - seed, axis=1) >= 0.3))
        for step in (-1, 1):
            x = a - 1 if step < 0 else b
            while 0 <= x < len(columns) and (b - a) < 30 * s and not stop[x]:
                a, b = (x, b) if step < 0 else (a, x + 1)
                x += step
        grown.append((a, b))
    spans = sorted({(a, b) for a, b in grown if 8 * s <= b - a <= 30 * s})
    spans = [span for span in spans
             if (span[1] + x0 <= first + quarter if left else span[0] + x0 >= last - quarter)]
    found = []
    brightest = np.nan_to_num(columns, nan=255).max(axis=1)
    for a, b in sorted(spans, reverse=left)[:3]:
        # The box's dark border, darker than both its fill and the strip, gives exact sides; the
        # span may stop at the first digit, so the far side is looked for up to a box width away.
        inside = columns[a:b][valid[a:b]]
        if not len(inside):
            continue
        dark = brightest < 0.5 * min(float(np.max(np.median(inside, axis=0))), float(np.max(strip)))
        start = next((x for x in range(a, max(-1, a - round(6 * s)), -1) if dark[x]), None)
        if start is not None:
            end = next((x for x in range(start + round(10 * s), min(len(columns), start + round(30 * s)))
                        if dark[x]), None)
            if end is not None:
                found.append((start + x0, max(0, int(row.centre - 11.5 * s)), end + 1 + x0,
                              min(image.shape[0], int(row.centre + 11.5 * s) + 1)))
                continue
        found.append(_complete(image, row, (a + x0, b + x0)))
    return found


def _complete(image: np.ndarray, row: Row, span: Run) -> BBox:
    """A badge box from a fill span the digits' shading may have cut short: take in the runs it
    overlaps, then runs at most 3px from those (the other digit). A name is a space away and a
    bracket bar is as tall as the window, so both stay out."""
    s, (a, b) = row.scale, span
    lo0 = max(0, a - round(24 * s))
    runs = [run for run in _runs(row.ink[:, lo0:b + round(24 * s)].any(axis=0), lo0)
            if not (run[1] - run[0] <= 3 * s and row.ink[:, run[0]:run[1]].any(axis=1).mean() >= 0.8)]
    digits = [run for run in runs if run[1] > a and run[0] < b]
    if digits:
        lo, hi = digits[0][0], digits[-1][1]
        for run in reversed(runs):
            if run[1] <= lo and lo - run[1] <= 3 * s and hi - run[0] <= 22 * s:
                lo = run[0]
        for run in runs:
            if run[0] >= hi and run[0] - hi <= 3 * s and run[1] - lo <= 22 * s:
                hi = run[1]
        a, b = min(a, lo - round(2 * s)), max(b, hi + round(2 * s))
    yc = row.centre
    return (max(0, a), max(0, int(yc - 11.5 * s)), min(row.width, b),
            min(image.shape[0], int(yc + 11.5 * s) + 1))


class GlyphTemplates:
    """Glyph instances (canvas images) and their characters; nearest neighbour by NCC."""

    def __init__(self, glyphs: np.ndarray, chars: np.ndarray, widths: np.ndarray,
                 source: str = "", sha256: str = "unavailable"):
        count, height, width = glyphs.shape
        vectors = glyphs.reshape(count, -1).astype(np.float32)
        vectors -= vectors.mean(axis=1, keepdims=True)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-6)
        grid = vectors.reshape(count, height, width)
        # Each instance also shifted one column either way: centring a glyph on the canvas jitters.
        shifted = [vectors] + [np.roll(grid, d, axis=2).reshape(count, -1) for d in (1, -1)]
        self.height = height
        self.matrix = np.concatenate(shifted)
        self.chars = np.tile(chars, 3)
        self.widths = np.tile(widths.astype(np.int32), 3)
        self.count = count
        self.source = source
        self.sha256 = sha256

    @classmethod
    def load(cls, path: str | Path, kind: str = "glyphs") -> GlyphTemplates | None:
        """Name glyphs ("glyphs") or badge digits ("digits") of a template file, if it has them."""
        path = Path(path)
        keys = ("glyphs", "chars", "widths") if kind == "glyphs" else ("digits", "digit_chars", "digit_widths")
        with np.load(path, allow_pickle=False) as data:
            if keys[0] not in data.files:
                return None
            return cls(data[keys[0]], data[keys[1]], data[keys[2]], path.name,
                       hashlib.sha256(path.read_bytes()).hexdigest())

    def match(self, vectors: np.ndarray, widths: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """Best score and character per glyph vector; ink widths more than 1px apart cost."""
        scores = vectors @ self.matrix.T - 0.05 * np.maximum(
            0, np.abs(self.widths[None, :] - np.asarray(widths)[:, None]) - 1)
        best = scores.argmax(axis=1)
        return scores[np.arange(len(best)), best], self.chars[best]


def _window(row: Row) -> np.ndarray:
    """The row's text window as contrast in [0, 1] of the row's text level."""
    level = np.percentile(row.contrast[row.ink], 95) if row.ink.any() else 1.0
    return np.clip(row.contrast.astype(np.float32) / max(float(level), 1.0), 0, 1)


def glyph_canvas(window: np.ndarray, a: int, b: int) -> np.ndarray:
    """Glyph columns [a, b) of a text window plus one anti-aliased column each side, centred on a
    zero canvas: what a template stores."""
    a, b = max(0, a - 1), min(window.shape[1], b + 1)
    width = min(b - a, _CANVAS)
    canvas = np.zeros((window.shape[0], _CANVAS), np.float32)
    offset = (_CANVAS - width) // 2
    canvas[:, offset:offset + width] = window[:, a:a + width]
    return canvas


def _vector(window: np.ndarray, a: int, b: int) -> np.ndarray:
    vector = glyph_canvas(window, a, b).ravel()
    vector = vector - vector.mean()
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def _read_run(memo: dict[Run, tuple[float, str]], a: int, b: int,
              depth: int = 2) -> tuple[float, list[str], list[float]]:
    """Best reading of one ink run, which may hold touching glyphs: the whole run against splits
    in two (the left part split once more), by width-weighted score with a 0.06 margin per split.
    `memo` holds the best match of every sub-span."""
    score, char = memo[(a, b)]
    best = (score, [char], [score])
    if depth and b - a >= 6:
        for cut in range(a + 2, b - 1):
            left = _read_run(memo, a, cut, depth - 1)
            right_score, right_char = memo[(cut, b)]
            split = (left[0] * (cut - a) + right_score * (b - cut)) / (b - a) - 0.06
            if split > best[0]:
                best = (split, left[1] + [right_char], left[2] + [right_score])
    return best


def read_glyphs(templates: GlyphTemplates, window: np.ndarray,
                blobs: list[Run]) -> tuple[str, list[float]]:
    """Text and per-glyph scores of one name. Pieces matching no glyph (score < 0.5) are icon or
    border debris and are dropped."""
    if not blobs:
        return "", []
    scores, chars = templates.match(np.stack([_vector(window, a, b) for a, b in blobs]),
                                    [b - a for a, b in blobs])
    text, kept = [], []
    for (a, b), score, char in zip(blobs, scores, chars, strict=True):
        pieces, piece_scores = [str(char)], [float(score)]
        if score < 0.9 and b - a >= 6:  # poorly explained: touching glyphs
            spans = [(x, y) for x in range(a, b) for y in range(x + 1, b + 1)]
            sub_scores, sub_chars = templates.match(
                np.stack([_vector(window, x, y) for x, y in spans]), [y - x for x, y in spans])
            memo = {span: (float(sc), str(ch)) for span, sc, ch in zip(spans, sub_scores, sub_chars,
                                                                       strict=True)}
            _, pieces, piece_scores = _read_run(memo, a, b)
        for char_, score_ in zip(pieces, piece_scores, strict=True):
            if score_ >= 0.5:
                text.append(char_)
                kept.append(score_)
    return "".join(text), kept


def badge_window(image: np.ndarray, box: BBox) -> np.ndarray | None:
    """A badge box's middle rows as brightness in [0, 1] above its fill: the white digits come
    out near 1 while the fill, its shading and the border stay near 0."""
    left, top, right, bottom = box
    y0 = (top + bottom) // 2 - _BADGE_ROWS // 2
    if y0 < 0 or right - left < 4:
        return None
    crop = image[y0:y0 + _BADGE_ROWS, left:right].min(axis=2).astype(np.float32)
    if crop.shape[0] != _BADGE_ROWS:
        return None
    fill, peak = float(np.median(crop)), float(np.percentile(crop, 98))
    return np.clip((crop - fill) / (peak - fill), 0, 1) if peak - fill >= 30 else None


def digit_runs(window: np.ndarray) -> list[Run]:
    """Digit columns of a badge window (border rows left out); a bar as tall as the box is a
    bracket or the box side, not a digit."""
    ink = window > 0.5
    return [run for run in _runs(ink[3:-3].any(axis=0)) if window[:, run[0]:run[1]].sum() >= 3
            and ink[:, run[0]:run[1]].any(axis=1).mean() < 0.85]


_TILE = 8  # columns per compared tile: about a glyph, so one changed glyph changes a tile


@dataclass(eq=False)  # compared by identity
class _Seen:
    """A row read before: where it was (marker column, compared columns) and its tiles' images."""
    label: str
    x: int
    zone: Run
    pixels: np.ndarray  # the row window over the zone columns
    tokens: list[OCRToken]  # boxes relative to the row's window top
    badges: list[VisualElement]
    trusted: bool = False  # two fresh reads of the unchanged row agreed


def _zone(row: Row) -> Run:
    """The columns a row's reading depends on: from beyond the killer's badge to beyond the
    victim's, leaving out scenery farther out."""
    s, (left, _, right, _) = row.scale, row.marker.bbox
    x0 = row.killer.start - round(40 * s) if row.killer else left - round(10 * s)
    x1 = row.victim.end + round(40 * s) if row.victim else right + round(10 * s)
    return max(0, x0), min(row.width, x1)


def _tiles(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mean-free 8-column tiles of a window strip and their norms."""
    width = pixels.shape[1] // _TILE * _TILE
    tiles = pixels[:, :width].reshape(pixels.shape[0], -1, _TILE).transpose(1, 0, 2).reshape(width // _TILE, -1)
    tiles = tiles - tiles.mean(axis=1, keepdims=True)
    return tiles, np.linalg.norm(tiles, axis=1)


def _alike(seen: np.ndarray, now: np.ndarray) -> bool:
    """The same row content, allowing the baseline estimate one row of jitter: every tile with
    text in either still correlates (NCC >= 0.85); a faint tile (scenery, a fading edge) may
    flicker, but no clear text may appear or vanish."""
    if seen.shape != now.shape:
        return False
    a, na = _tiles(seen[1:-1])
    for dy in (0, -1, 1):
        b, nb = _tiles(now[1 + dy:now.shape[0] - 1 + dy])
        text = np.maximum(na, nb) >= 1.5
        if (np.minimum(na, nb)[text] < 0.5).any():
            continue
        ncc = (a[text] * b[text]).sum(axis=1) / np.maximum(na[text] * nb[text], 1e-6)
        if (ncc >= 0.85).all():
            return True
    return False


def _same_reading(a: _Seen, b: _Seen) -> bool:
    return (sorted(t.text for t in a.tokens) == sorted(t.text for t in b.tokens)
            and sorted(x.label for x in a.badges) == sorted(x.label for x in b.badges))


def _down(box: BBox, dy: int) -> BBox:
    return box[0], box[1] - dy, box[2], box[3] - dy


def _in_row(box: BBox, row: Row) -> bool:
    return box[1] <= row.centre <= box[3]


def name_crop(image: np.ndarray, row: Row, name: Name) -> np.ndarray:
    """A recognition crop of one name, its pad columns painted with the crop's median so a
    neighbouring bracket or badge border does not read as "l"; upscaled 2x."""
    height, width = image.shape[:2]
    s = row.scale
    a, b = name.blobs[0][0], name.end
    x0, x1 = max(0, a - round(4 * s)), min(width, b + round(4 * s))
    crop = image[max(0, row.top - round(3 * s)):min(height, row.top + row.height + round(2 * s)),
                 x0:x1].copy()
    fill = np.median(crop.reshape(-1, 3), axis=0)
    crop[:, :max(0, a - 1 - x0)] = fill
    crop[:, b + 1 - x0:] = fill
    return cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


class NameReader:
    """Killfeed text per frame: badges and names, with no text detector.

    Glyph templates read the names when a set exists for the capture resolution; recognition-only
    OCR (read_lines) reads the badge digits and any name the templates do not explain."""

    def __init__(self, read_lines: ReadLines | None = None, templates: GlyphTemplates | None = None,
                 digits: GlyphTemplates | None = None, cache_size: int = 64) -> None:
        self.read_lines = read_lines
        self.templates = templates
        self.digits = digits
        # Rows read before, newest last. Killfeed rows are static apart from scrolling up (and fading),
        # so an unchanged row is read once.
        self._seen: list[_Seen] = []
        self._cache_size = cache_size
        self.cache_hits = self.cache_misses = 0
        self._badged = False  # a badge was read: not solo, so a name without one is unread yet

    @classmethod
    def for_resolution(cls, width: int, height: int,
                       read_lines: ReadLines | None = None) -> NameReader:
        path = ASSETS / f"{width}x{height}.npz"
        if not path.is_file():
            return cls(read_lines)
        return cls(read_lines, GlyphTemplates.load(path), GlyphTemplates.load(path, "digits"))

    def model_manifest(self) -> dict[str, Any]:
        if self.templates is None:
            return {"engine": "glyph-templates", "status": "unavailable"}
        return {"engine": "glyph-templates", "status": "ready", "file": self.templates.source,
                "sha256": f"sha256:{self.templates.sha256}",
                "glyphs": self.templates.count, "digits": self.digits.count if self.digits else 0,
                "min_score": MIN_SCORE}

    def _digits(self, image: np.ndarray, box: BBox) -> tuple[VisualElement | None, int, float]:
        """A badge read by the digit templates (or None), the digit runs the box holds and how
        much the box looks like 1-2 badge digits (the weakest digit's score, 0 if it does not)."""
        window = badge_window(image, box)
        if window is None:
            return None, 0, 0.0
        runs = digit_runs(window)
        if self.digits is None or window.shape[0] != self.digits.height:
            return None, len(runs), 1e-3 if runs else 0.0  # no templates: any box with runs
        text, scores = read_glyphs(self.digits, window, runs)
        likeness = min(scores) if 1 <= len(text) <= 2 and text.isdigit() and len(text) >= len(runs) else 0.0
        if likeness >= _DIGIT_SCORE:
            return VisualElement(VisualKind.BADGE, text, likeness, box), len(runs), likeness
        return None, len(runs), likeness

    def read(self, image: np.ndarray,
             icons: list[VisualElement]) -> tuple[list[OCRToken], list[VisualElement]]:
        """Name tokens (killer token spans from its clan tag, so the badge gap stays short) and
        badge elements for the rows of the detected markers. A row seen before, unchanged but
        for scrolling, reuses its reading."""
        boxed = find_badges(image)
        rows = [Row(image, icon, boxed) for icon in icons if icon.label in _MARKER_HEIGHT]
        windows = {id(row): _window(row) for row in rows}
        matches: list[_Seen | None] = []
        for row in rows:  # identical rows (one kill shown twice)
            matches.append(next((seen for seen in reversed(self._seen)  # each claim their own entry
                                 if not any(seen is claimed for claimed in matches)
                                 and seen.label == row.marker.label and abs(seen.x - row.marker.bbox[0]) <= 1
                                 and _alike(seen.pixels, windows[id(row)][:, seen.zone[0]:seen.zone[1]])), None))
        # A reading is reused only once two fresh reads of the unchanged row agreed: the first one
        # can catch a row mid-animation.
        fresh = [row for row, seen in zip(rows, matches, strict=True) if seen is None or not seen.trusted]
        self.cache_hits += len(rows) - len(fresh)
        self.cache_misses += len(fresh)
        tokens, badges = self._read_rows(image, fresh, boxed, windows) if fresh else ([], [])
        self._badged = self._badged or bool(badges)
        out_tokens: list[OCRToken] = []
        out_badges: list[VisualElement] = []
        for row, seen in zip(rows, matches, strict=True):
            if seen is None or not seen.trusted:
                zone = _zone(row)
                read = _Seen(row.marker.label, row.marker.bbox[0], zone, windows[id(row)][:, zone[0]:zone[1]].copy(),
                             [OCRToken(t.text, t.confidence, _down(t.bbox, row.top)) for t in tokens
                              if _in_row(t.bbox, row)],
                             [VisualElement(b.kind, b.label, b.confidence, _down(b.bbox, row.top)) for b in badges
                              if _in_row(b.bbox, row)])
                # A new row's badges show 200-300 ms after its names, faint enough to pass as
                # unchanged: a reading short of a badge stays fresh once the match shows badges.
                read.trusted = (seen is not None and _same_reading(seen, read)
                                and not (self._badged and len(read.badges) < len(read.tokens)))
                if seen is not None:
                    self._seen.remove(seen)
                seen = read
            else:
                self._seen.remove(seen)
            self._seen.append(seen)
            out_tokens += [OCRToken(t.text, t.confidence, _down(t.bbox, -row.top)) for t in seen.tokens]
            out_badges += [VisualElement(b.kind, b.label, b.confidence, _down(b.bbox, -row.top))
                           for b in seen.badges]
        del self._seen[:-self._cache_size]
        return out_tokens, out_badges

    def _read_rows(self, image: np.ndarray, rows: list[Row], boxed: list[BBox],
                   windows: dict[int, np.ndarray]) -> tuple[list[OCRToken], list[VisualElement]]:
        boxed = [box for box in boxed if any(_in_row(box, row) for row in rows)]
        badges: list[VisualElement] = []
        unread: dict[BBox, int] = {}  # box -> digit runs it holds
        for box in boxed:
            badge, runs, _ = self._digits(image, box)
            if badge is not None:
                badges.append(badge)
            else:
                unread[box] = runs
        for row in rows:  # row ends without a bordered box: the first candidate that reads, else
            for candidates in row.badge_candidates(image):  # recognition of the likeliest
                reads = []
                for box in candidates:
                    reads.append((box, *self._digits(image, box)))
                    if reads[-1][1] is not None:
                        break
                badge = reads[-1][1] if reads else None
                if badge is not None:
                    badges.append(badge)
                elif reads:
                    box, _, runs, likeness = max(reads, key=lambda read: read[3])
                    if likeness > 0:
                        unread[box] = runs
        if unread and self.read_lines is not None:
            try:  # a box read with fewer digits than it holds lost one; unknown beats wrong
                badges += [badge for badge in read_badges(image, self.read_lines, boxes=list(unread))
                           if len(badge.label) >= unread[badge.bbox]]
            except Exception:  # badges only refine teams; keep the names
                log_event("badge.error")
        confirmed = [badge.bbox for badge in badges]
        for row in rows:
            row.settle(confirmed)
        names = [(row, name) for row in rows for name in (row.killer, row.victim) if name]
        texts: list[tuple[str, float] | None] = [None] * len(names)
        templates = self.templates
        for index, (row, name) in enumerate(names):
            if templates is not None and row.height == templates.height:
                text, scores = read_glyphs(templates, windows[id(row)], name.blobs)
                if scores and len(text) >= 3 and min(scores) >= MIN_SCORE:
                    texts[index] = (text, min(scores))
        fallback = [index for index, read in enumerate(texts) if read is None]
        if fallback and self.read_lines is not None:
            lines = self.read_lines([name_crop(image, *names[index]) for index in fallback])
            for index, (text, confidence) in zip(fallback, lines, strict=True):
                cleaned = nickname(text)
                if cleaned:
                    texts[index] = (cleaned, float(confidence))
        tokens = []
        for (row, name), read in zip(names, texts, strict=True):
            if read is None:
                continue
            start = name.blobs[0][0]
            if name is row.killer:  # reach back to the left badge (past a tag) so it reads as near
                start = max([box[2] for box in confirmed if _in_row(box, row) and box[2] <= start],
                            default=name.start)
            tokens.append(OCRToken(read[0], read[1], (start, row.top, name.end, row.top + row.height)))
        return tokens, badges
