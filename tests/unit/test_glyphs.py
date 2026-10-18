"""Glyph-template name reading and the marker-row layout, on rows composed from the stored glyphs."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from kfocr.domain.models import VisualElement, VisualKind
from kfocr.domain.parser import nickname
from kfocr.vision.glyphs import ASSETS, GlyphTemplates, NameReader, read_glyphs

ASSET = ASSETS / "2560x1440.npz"
MARKER = ASSETS.parent / "icons" / "2026-10" / "kill_marker.png"
STRIP, TEXT = 28, 230


def glyph(char: str, kind: str = "glyphs") -> np.ndarray:
    """The stored canvas of a name glyph (or badge digit) closest to its class mean, cut to its
    non-empty columns."""
    with np.load(ASSET) as data:
        chars = data["chars" if kind == "glyphs" else "digit_chars"]
        canvases = data[kind][chars == char].astype(np.float32) / 255
    flat = canvases.reshape(len(canvases), -1)
    canvas = canvases[int(np.argmax(flat @ flat.mean(axis=0)))]
    columns = np.flatnonzero(canvas.max(axis=0) > 0)
    return canvas[:, columns[0]:columns[-1] + 1]


def compose(word: str, gap: int = 2, kind: str = "glyphs") -> np.ndarray:
    parts: list[np.ndarray] = []
    for char in word:
        canvas = glyph(char, kind)
        parts += [canvas, np.zeros((canvas.shape[0], gap), np.float32)]
    return np.hstack(parts[:-1])


def runs(window: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.concatenate([[0], (window >= 0.5).any(axis=0).astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist(), strict=True))


def test_templates_read_back_a_name_composed_from_their_own_glyphs():
    word = "Kf_9lI1O0x-w"  # l/I/1 and O/0 are told apart by shape
    window = compose(word)
    text, scores = read_glyphs(GlyphTemplates.load(ASSET), window, runs(window))
    assert text == word
    assert min(scores) > 0.9


def synthetic_row(killer: str, victim: str) -> tuple[np.ndarray, VisualElement]:
    """A killfeed row: coloured badge, killer, kill marker, "[TAG]" victim, coloured badge."""
    image = np.full((44, 460, 3), STRIP, np.uint8)
    top = 12  # text window rows top..top+22; the marker spans baseline-18..baseline+4

    def paint(window: np.ndarray, x: int, y: int = top) -> int:  # white over what is beneath
        h, w = window.shape
        under = image[y:y + h, x:x + w].astype(np.float32)
        image[y:y + h, x:x + w] = under + window[..., None] * (TEXT - under)
        return x + w

    def badge(x: int, digits: str) -> int:  # centred on the marker, as tall as the row
        cv2.rectangle(image, (x, top - 2), (x + 22, top + 20), (150, 70, 120), -1)
        return paint(compose(digits, 1, "digits"), x + 4, top - 2) + 4

    x = badge(6, "12") + 9
    x = paint(compose(killer), x) + 30
    marker = cv2.imread(str(MARKER), cv2.IMREAD_GRAYSCALE)
    image[top - 2:top + 20, x:x + 20] = marker[..., None]
    icon = VisualElement(VisualKind.ICON, "kill_marker", 0.99, (x, top - 2, x + 20, top + 20))
    x = paint(compose("[AB]"), x + 30) + 8
    badge(paint(compose(victim), x) + 9, "34")
    return image, icon


def test_reader_finds_names_and_borderless_badges_from_the_row_layout():
    image, icon = synthetic_row("Alpha_7", "bravo-Q2")
    crops: list[np.ndarray] = []

    def read_lines(images):
        crops.extend(images)
        return [("99", 0.99)] * len(images)

    reader = NameReader(read_lines, GlyphTemplates.load(ASSET), GlyphTemplates.load(ASSET, "digits"))
    tokens, badges = reader.read(image, [icon])
    assert [token.text for token in tokens] == ["Alpha_7", "bravo-Q2"]
    assert crops == []  # names and badge digits both read by templates
    assert [badge.label for badge in sorted(badges, key=lambda b: b.bbox)] == ["12", "34"]
    left, right = sorted(badge.bbox for badge in badges)
    assert left[2] <= tokens[0].bbox[0] and tokens[1].bbox[2] <= right[0]
    assert tokens[1].bbox[0] > icon.bbox[2] + 40  # the victim's "[AB]" tag is not part of its name
    assert all(type(v) is int for e in tokens + badges for v in e.bbox)  # stored as JSON


@pytest.mark.parametrize("read, expected", [("[AB] bravo.Q 2", "bravo_Q_2"), ("||", None)])
def test_without_templates_names_fall_back_to_recognition_in_the_name_alphabet(read, expected):
    image, icon = synthetic_row("Alpha_7", "bravo-Q2")
    calls: list[int] = []

    def read_lines(images):
        calls.append(len(images))
        return [("12", 0.99)] * len(images) if len(calls) == 1 else [(read, 0.9)] * len(images)

    tokens, badges = NameReader(read_lines).read(image, [icon])
    assert calls == [2, 2] and len(badges) == 2
    assert [token.text for token in tokens] == ([expected, expected] if expected else [])


def test_nickname_keeps_the_pubg_alphabet_after_dropping_clan_tags():
    assert nickname("[TAG] Na.me 1") == "Na_me_1"
    assert nickname("[TAG]") is None
    assert nickname("a#b-c") == "ab-c"


def test_an_unchanged_row_is_reused_once_two_reads_agree_even_after_scrolling():
    image, icon = synthetic_row("Alpha_7", "bravo-Q2")
    calls: list[int] = []

    def read_lines(images):
        calls.append(len(images))
        return [("12", 0.99)] * len(images) if len(calls) % 2 else [("bravo_Q2", 0.9)] * len(images)

    reader = NameReader(read_lines)
    first = reader.read(image, [icon])
    reader.read(image, [icon])  # agrees with the first read: trusted from now on
    down = 7  # the feed scrolled: same row, lower in the image
    shifted = np.full_like(image, STRIP)
    shifted[down:] = image[:-down]
    moved = VisualElement(icon.kind, icon.label, icon.confidence,
                          (icon.bbox[0], icon.bbox[1] + down, icon.bbox[2], icon.bbox[3] + down))
    third = reader.read(shifted, [moved])
    assert len(calls) == 4 and reader.cache_hits == 1  # no recognition for the scrolled row
    assert [t.text for t in third[0]] == [t.text for t in first[0]]
    assert [t.bbox[1] - down for t in third[0]] == [t.bbox[1] for t in first[0]]
    other, icon2 = synthetic_row("Alpha_7", "brave-Q2")  # one glyph differs
    reader.read(other, [icon2])
    assert reader.cache_misses == 3


def test_a_reading_short_of_a_badge_is_not_reused_once_the_match_shows_badges():
    image, icon = synthetic_row("Alpha_7", "bravo-Q2")
    calls: list[int] = []

    def read_lines(images):  # badges, then names; after the first read one badge stays unread
        calls.append(len(images))
        if len(calls) % 2 == 0:
            return [("bravo_Q2", 0.9)] * len(images)
        return [("12", 0.99)] * len(images) if len(calls) == 1 else [("12", 0.99), ("", 0.1)]

    reader = NameReader(read_lines)
    for _ in range(4):
        reader.read(image, [icon])
    assert reader.cache_hits == 0 and len(calls) == 8


def test_one_kill_shown_twice_reads_as_two_rows():
    row, icon = synthetic_row("Alpha_7", "bravo-Q2")
    image = np.vstack([row, row])
    below = VisualElement(icon.kind, icon.label, icon.confidence,
                          (icon.bbox[0], icon.bbox[1] + row.shape[0], icon.bbox[2], icon.bbox[3] + row.shape[0]))
    reader = NameReader(lambda images: [("12", 0.99)] * len(images),
                        GlyphTemplates.load(ASSET), GlyphTemplates.load(ASSET, "digits"))
    for _ in range(3):  # the second read finds both rows alike one cached reading
        tokens, _ = reader.read(image, [icon, below])
        assert len(tokens) == 4
