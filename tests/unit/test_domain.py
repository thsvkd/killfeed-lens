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
    assert parsed.source_crop_ref is None
    assert parsed.visual_fingerprint == "visual-hash"
