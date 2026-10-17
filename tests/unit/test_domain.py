from __future__ import annotations

from dataclasses import replace

from kfocr.domain.models import (
    CandidateEvent,
    EventType,
    KillfeedRowCandidate,
    VisualElement,
    VisualKind,
)
from kfocr.domain.parser import EventParser


def element(kind: VisualKind, label: str, confidence: float, x: int) -> VisualElement:
    return VisualElement(kind=kind, label=label, confidence=confidence, bbox=(x, 0, x + 30, 20))


def candidate(**changes: object) -> CandidateEvent:
    base = CandidateEvent(
        observed_at_ns=100,
        frame_seq=1,
        killer_name_raw="Alpha",
        victim_name_raw="Bravo",
        killer_name="Alpha",
        victim_name="Bravo",
        killer_team=None,
        victim_team=None,
        event_type=EventType.KILL,
        weapon_label=None,
        headshot=None,
        confidence_row=1.0,
        confidence_killer=1.0,
        confidence_victim=1.0,
        confidence_event_type=1.0,
        confidence_total=0.9,
        source_bbox=(0, 0, 200, 20),
        source_crop_ref="abc",
    )
    return replace(base, **changes)


def test_parser_uses_icon_geometry_instead_of_input_order() -> None:
    row = KillfeedRowCandidate(
        frame_seq=4,
        observed_at_ns=123,
        bbox=(10, 20, 300, 30),
        elements=[
            element(VisualKind.TEXT, "Victim", 0.91, 220),
            element(VisualKind.ICON, "kill", 0.96, 150),
            element(VisualKind.TEXT, "Killer", 0.94, 50),
        ],
        row_confidence=0.95,
        crop_phash="feedface",
    )

    parsed = EventParser().parse(row)

    assert parsed is not None
    assert parsed.killer_name == "Killer"
    assert parsed.victim_name == "Victim"
    assert parsed.event_type is EventType.KILL
    assert parsed.source_crop_ref is None
    assert parsed.visual_fingerprint == "feedface"


def test_parser_preserves_unknown_and_penalizes_missing_name() -> None:
    row = KillfeedRowCandidate(
        frame_seq=5,
        observed_at_ns=456,
        bbox=(0, 0, 250, 25),
        elements=[
            element(VisualKind.TEXT, "OnlyPlayer", 0.9, 20),
            element(VisualKind.ICON, "new-patch-icon", 0.99, 150),
        ],
        row_confidence=0.9,
        crop_phash=None,
    )

    parsed = EventParser().parse(row)

    assert parsed is not None
    assert parsed.event_type is EventType.UNKNOWN
    assert parsed.victim_name is None
    assert parsed.confidence_victim == 0.0
    assert parsed.confidence_total < 0.65


def test_parser_rejects_row_without_any_name() -> None:
    row = KillfeedRowCandidate(
        frame_seq=6,
        observed_at_ns=789,
        bbox=(0, 0, 100, 20),
        elements=[element(VisualKind.ICON, "kill", 0.99, 50)],
        row_confidence=0.99,
        crop_phash=None,
    )

    assert EventParser().parse(row) is None


def test_parser_no_icon_fallback_requires_two_spatially_separate_names() -> None:
    row = KillfeedRowCandidate(
        frame_seq=7,
        observed_at_ns=999,
        bbox=(0, 0, 300, 20),
        elements=[
            element(VisualKind.TEXT, "Left", 0.9, 10),
            element(VisualKind.TEXT, "Right", 0.9, 200),
        ],
        row_confidence=0.9,
        crop_phash=None,
    )

    parsed = EventParser().parse(row)

    assert parsed is not None
    assert parsed.killer_name == "Left"
    assert parsed.victim_name == "Right"
    assert parsed.event_type is EventType.UNKNOWN
    assert parsed.confidence_row == 0.0  # no icon or separator: no killfeed structure evidence


def test_parser_no_icon_text_cannot_reach_confirmation_without_separator() -> None:
    def row(*middle: VisualElement) -> KillfeedRowCandidate:
        return KillfeedRowCandidate(
            frame_seq=1, observed_at_ns=0, bbox=(0, 0, 300, 20),
            elements=[element(VisualKind.TEXT, "Alive 42", 0.99, 10), *middle,
                      element(VisualKind.TEXT, "Ping 18", 0.99, 200)],
            row_confidence=0.99, crop_phash=None,
        )

    hud = EventParser().parse(row())
    assert hud is not None and hud.confidence_total + 0.10 < 0.88  # even with full temporal
    piped = EventParser().parse(row(element(VisualKind.TEXT, "|", 0.99, 100)))
    assert piped is not None and piped.confidence_row == 0.0
    separated = EventParser().parse(row(element(VisualKind.TEXT, "→", 0.99, 100)))
    assert separated is not None and separated.confidence_row == 0.99


def test_parser_preserves_name_punctuation_and_separates_visual_fingerprint() -> None:
    row = KillfeedRowCandidate(
        frame_seq=8,
        observed_at_ns=1_000,
        bbox=(0, 0, 300, 20),
        elements=[
            element(VisualKind.TEXT, " AAA- ", 0.9, 10),
            element(VisualKind.ICON, "kill", 0.95, 100),
            element(VisualKind.TEXT, " -BBB ", 0.9, 200),
        ],
        row_confidence=0.9,
        crop_phash="visual-hash",
    )

    parsed = EventParser().parse(row)

    assert parsed is not None
    assert (parsed.killer_name, parsed.victim_name) == ("AAA-", "-BBB")
    assert (parsed.killer_team, parsed.victim_team) == (None, None)
    assert parsed.source_crop_ref is None
    assert parsed.visual_fingerprint == "visual-hash"


def text(label: str, left: int, right: int, confidence: float = 0.9) -> VisualElement:
    return VisualElement(VisualKind.TEXT, label, confidence, (left, 0, right, 20))


def test_parser_reads_pubg_layout_names_without_badges_clans_or_icon_debris() -> None:
    # [25] Killer <weapon> <headshot> X [CLAN] Victim [29], as OCR splits a real row.
    row = KillfeedRowCandidate(
        frame_seq=1, observed_at_ns=0, bbox=(0, 0, 430, 20),
        elements=[
            text("??", 0, 18),                 # avatar debris, its own run
            text("30[AB]", 40, 90), text("Kil1er", 92, 140),
            text("+", 230, 236),               # headshot icon read as a glyph
            VisualElement(VisualKind.ICON, "kill_marker", 0.95, (250, 0, 270, 20)),
            text("[CD]Vic.tim", 280, 360), text("29", 362, 380),
            text("42 160", 400, 430),           # HUD counter after a gap
        ],
        row_confidence=0.9, crop_phash=None,
    )

    parsed = EventParser().parse(row)

    assert parsed is not None and parsed.event_type is EventType.KILL
    assert (parsed.killer_name, parsed.victim_name) == ("Kil1er", "Vic_tim")
    assert (parsed.killer_team, parsed.victim_team) == (30, 29)
    assert (parsed.killer_name_raw, parsed.victim_name_raw) == ("30[AB] Kil1er", "[CD]Vic.tim")


def test_mask_icons_blanks_marker_with_antialias_margin() -> None:
    import numpy as np

    from kfocr.vision.icons import mask_icons

    image = np.full((30, 60, 3), 200, dtype=np.uint8)
    masked = mask_icons(image, [VisualElement(VisualKind.ICON, "kill_marker", 0.9, (20, 6, 40, 26))])
    assert not masked[1:31, 15:45].any() and masked[0, 0].all() and image.all()


def test_canonical_name_keeps_names_through_ocr_bracket_and_dot_noise() -> None:
    from kfocr.domain.parser import _canonical_name

    cases = {"30[LG]": None, "404]": None, "[AU]Name": "Name", "Name29]": "Name29",
             "[CLAN Name": "Name", "bitnaa.": "bitnaa_", "bit.naa": "bit_naa", "12345": "12345"}
    assert {raw: _canonical_name(raw) for raw in cases} == cases


def test_parser_reads_team_badges_as_separate_overlapping_tokens() -> None:
    row = KillfeedRowCandidate(
        frame_seq=1, observed_at_ns=0, bbox=(0, 0, 300, 23),
        elements=[
            text("25", 2, 21), text("Killer", 16, 120),   # OCR boxes overlap by a few pixels
            VisualElement(VisualKind.ICON, "knock_marker", 0.9, (150, 0, 187, 20)),
            text("Victim 7", 200, 280),                    # badge kept inside the run by OCR
        ],
        row_confidence=0.9, crop_phash=None,
    )

    parsed = EventParser().parse(row)

    assert parsed is not None and parsed.event_type is EventType.KNOCK
    assert (parsed.killer_name, parsed.killer_team) == ("Killer", 25)
    assert (parsed.victim_name, parsed.victim_team) == ("Victim", 7)


def test_parser_prefers_read_badge_boxes_over_digits_in_names() -> None:
    def badge(label: str, left: int, right: int) -> VisualElement:
        return VisualElement(VisualKind.BADGE, label, 0.99, (left, 0, right, 20))

    row = KillfeedRowCandidate(
        frame_seq=1, observed_at_ns=0, bbox=(0, 0, 400, 23),
        elements=[
            badge("41", 0, 20), text("Killer", 25, 120),
            VisualElement(VisualKind.ICON, "kill_marker", 0.9, (150, 0, 187, 20)),
            text("han2", 200, 250), text("2", 285, 297),  # "han2____2": "____" unread
            badge("46", 300, 320),
        ],
        row_confidence=0.9, crop_phash=None,
    )

    parsed = EventParser().parse(row)

    assert parsed is not None
    assert (parsed.killer_team, parsed.victim_team) == (41, 46)
