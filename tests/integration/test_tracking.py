from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from kfocr.domain.models import CandidateEvent, EventType
from kfocr.domain.tracker import TemporalTracker


def event(frame: int, y: int = 0, **changes: object) -> CandidateEvent:
    base = CandidateEvent(
        observed_at_ns=frame * 100_000_000,
        frame_seq=frame,
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
        source_bbox=(0, y, 200, 20),
        source_crop_ref=None,
    )
    return replace(base, **changes)


def test_tracker_confirms_after_two_frames_and_does_not_repeat_visible_row() -> None:
    tracker = TemporalTracker(session_id=uuid4())

    assert tracker.update([event(1, 80)], 100_000_000) == []
    emitted = tracker.update([event(2, 55)], 200_000_000)
    assert len(emitted) == 1
    assert emitted[0].tracker_evidence_count == 2

    for frame in range(3, 20):
        assert tracker.update([event(frame, 55)], frame * 100_000_000) == []


def test_tracker_does_not_count_the_same_frame_twice() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    same_frame = event(1)

    assert tracker.update([same_frame], 100_000_000) == []
    assert tracker.update([same_frame], 150_000_000) == []
    assert len(tracker.update([event(2)], 200_000_000)) == 1


def test_tracker_matches_small_ocr_variation_while_row_scrolls() -> None:
    tracker = TemporalTracker(session_id=UUID(int=1))

    assert tracker.update([event(1, 90, visual_fingerprint="same-row")], 100_000_000) == []
    emitted = tracker.update(
        [event(2, 45, killer_name_raw="A1pha", killer_name="A1pha",
               confidence_killer=0.85, visual_fingerprint="same-row")],
        200_000_000,
    )

    assert len(emitted) == 1
    assert emitted[0].killer_name == "Alpha"


def test_tracker_does_not_merge_names_that_differ_only_by_punctuation() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    punctuated = event(1, killer_name="A-B", victim_name="C_D")
    plain = event(2, killer_name="AB", victim_name="CD")

    assert tracker.update([punctuated], 100_000_000) == []
    assert tracker.update([plain], 200_000_000) == []
    assert len(tracker.update([replace(plain, frame_seq=3)], 300_000_000)) == 1


def test_tracker_keeps_simultaneous_identical_rows_as_two_tracks() -> None:
    tracker = TemporalTracker(session_id=uuid4())

    assert tracker.update([event(1, 20), event(1, 80)], 100_000_000) == []
    emitted = tracker.update([event(2, 10), event(2, 70)], 200_000_000)

    assert len(emitted) == 2
    assert len({item.event_id for item in emitted}) == 2


def test_tracker_does_not_confirm_low_confidence_or_missing_name() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    incomplete = event(
        1,
        victim_name_raw=None,
        victim_name=None,
        confidence_victim=0.0,
        confidence_total=0.6,
    )

    assert tracker.update([incomplete], 100_000_000) == []
    assert tracker.update([replace(incomplete, frame_seq=2)], 200_000_000) == []


def test_tracker_never_promotes_unknown_to_kill() -> None:
    tracker = TemporalTracker(session_id=uuid4(), confirmed_min=0.80)
    unknown = event(1, event_type=EventType.UNKNOWN, confidence_event_type=0.8)

    tracker.update([unknown], 100_000_000)
    emitted = tracker.update([replace(unknown, frame_seq=2)], 200_000_000)

    assert len(emitted) == 1
    assert emitted[0].event_type is EventType.UNKNOWN


def test_tracker_expires_gap_then_allows_a_later_identical_event() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    tracker.update([event(1)], 100_000_000)
    assert len(tracker.update([event(2)], 200_000_000)) == 1

    assert tracker.update([], 800_000_000) == []
    assert tracker.update([event(9)], 900_000_000) == []
    assert len(tracker.update([event(10)], 1_000_000_000)) == 1
