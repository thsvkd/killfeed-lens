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

    assert emitted == []
    emitted = tracker.update([event(3, 30, visual_fingerprint="same-row")], 300_000_000)

    assert len(emitted) == 1
    assert emitted[0].killer_name == "Alpha"


def test_tracker_requires_the_emitted_spelling_itself_to_repeat() -> None:
    misread = dict(killer_name_raw="A1pha", killer_name="A1pha", visual_fingerprint="row")
    for first, second in ((event(1, **misread, confidence_total=0.95), event(2, visual_fingerprint="row")),
                          (event(1, visual_fingerprint="row"), event(2, **misread, confidence_total=0.95))):
        tracker = TemporalTracker(session_id=uuid4())
        assert tracker.update([first], 100_000_000) == []
        assert tracker.update([second], 200_000_000) == []


def test_tracker_does_not_reemit_visible_row_after_skipped_failed_inference() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    assert tracker.update([event(1)], 100_000_000) == []
    assert len(tracker.update([event(2)], 200_000_000)) == 1
    # 800ms: inference failed, so the pipeline does not call update at all.
    assert tracker.update([event(9)], 900_000_000) == []
    assert tracker.update([event(10)], 1_000_000_000) == []


def test_tracker_expires_after_long_blind_streak_to_keep_new_identical_event() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    assert tracker.update([event(1)], 100_000_000) == []
    assert len(tracker.update([event(2)], 200_000_000)) == 1
    # No successful update for ~10s, then the same pair appears again beyond the duplicate window.
    assert tracker.update([event(104)], 10_400_000_000) == []
    assert len(tracker.update([event(105)], 10_500_000_000)) == 1


def test_tracker_does_not_merge_names_that_differ_only_by_punctuation() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    punctuated = event(1, killer_name="A-B", victim_name="C_D")
    plain = event(2, killer_name="AB", victim_name="CD")

    assert tracker.update([punctuated], 100_000_000) == []
    assert tracker.update([plain], 200_000_000) == []
    assert len(tracker.update([replace(plain, frame_seq=3)], 300_000_000)) == 1


def test_tracker_emits_simultaneous_identical_rows_once() -> None:
    # A player dies once: two identical kill rows on screen are one event, not two.
    tracker = TemporalTracker(session_id=uuid4())

    assert tracker.update([event(1, 20), event(1, 80)], 100_000_000) == []
    emitted = tracker.update([event(2, 10), event(2, 70)], 200_000_000)

    assert len(emitted) == 1


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
    # Within the duplicate window the same pair is the same event; after it, a new one.
    assert tracker.update([event(9)], 900_000_000) == []
    assert tracker.update([event(10)], 1_000_000_000) == []
    assert tracker.update([], 10_200_000_000) == []
    assert tracker.update([event(104)], 10_400_000_000) == []
    assert len(tracker.update([event(105)], 10_500_000_000)) == 1


def test_tracker_is_pending_only_while_a_new_row_waits_for_its_confirming_frame() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    assert not tracker.pending
    tracker.update([event(1)], 100_000_000)
    assert tracker.pending  # first sighting: read the next frame too
    assert len(tracker.update([event(2)], 200_000_000)) == 1
    assert not tracker.pending  # emitted
    tracker.update([event(3, victim_name="Charlie")], 300_000_000)
    tracker.update([], 400_000_000)
    assert not tracker.pending  # the new row was not seen in the last update


def test_tracker_keeps_simultaneous_rows_whose_victims_are_only_alike() -> None:
    # Clanmates "Kilo-0870" and "Milo-0870" killed by one player sit on screen together.
    tracker = TemporalTracker(session_id=uuid4())

    def rows(frame: int) -> list[CandidateEvent]:
        return [event(frame, 20, victim_name="Kilo-0870", visual_fingerprint="a"),
                event(frame, 50, victim_name="Milo-0870", visual_fingerprint="b")]

    assert tracker.update(rows(1), 100_000_000) == []
    emitted = tracker.update(rows(2), 200_000_000)

    assert sorted(e.victim_name for e in emitted) == ["Kilo-0870", "Milo-0870"]


def test_tracker_suppresses_drifting_spelling_of_an_already_emitted_row() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    assert tracker.update([event(1)], 100_000_000) == []
    assert len(tracker.update([event(2)], 200_000_000)) == 1
    # OCR glues the team badge for a while: a new track, but the same kill.
    glued = dict(killer_name="35Alpha", victim_name="Bravo38", visual_fingerprint="other")
    assert tracker.update([event(3, **glued)], 300_000_000) == []
    assert tracker.update([event(4, **glued)], 400_000_000) == []
    # The killer misread (lost clan brackets) is still the same kill of the same victim.
    misread = dict(killer_name="44WJ4Alpha", visual_fingerprint="third")
    assert tracker.update([event(7, **misread)], 410_000_000) == []
    assert tracker.update([event(8, **misread)], 420_000_000) == []
    # A knock of the same pair, or a different victim, is a new event.
    assert len(tracker.update([event(5, victim_name="Charlie"), event(5, y=40, event_type=EventType.KNOCK)],
                              500_000_000)) == 0
    emitted = tracker.update([event(6, victim_name="Charlie"), event(6, y=40, event_type=EventType.KNOCK)],
                             600_000_000)
    assert {(e.victim_name, e.event_type) for e in emitted} == {("Charlie", EventType.KILL), ("Bravo", EventType.KNOCK)}


def test_tracker_keeps_teammates_whose_names_differ_only_by_digits() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    tracker.update([event(1, victim_name="Player1")], 100_000_000)
    assert len(tracker.update([event(2, victim_name="Player1")], 200_000_000)) == 1
    tracker.update([event(3, victim_name="Player2", visual_fingerprint="b")], 300_000_000)
    assert len(tracker.update([event(4, victim_name="Player2", visual_fingerprint="b")], 400_000_000)) == 1


def test_tracker_crops_the_row_where_it_is_when_confirmed() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    tracker.update([event(1, 80, confidence_total=0.95)], 100_000_000)
    emitted = tracker.update([event(2, 55, confidence_total=0.90)], 200_000_000)
    assert emitted[0].source_bbox == (0, 55, 200, 20)


def test_tracker_waits_for_a_new_rows_badges_once_the_match_shows_badges() -> None:
    tracker = TemporalTracker(session_id=uuid4())
    shown = event(1, 200, killer_name="Other", victim_name="Rival", killer_team=3, victim_team=5)
    tracker.update([shown], 100_000_000)
    tracker.update([replace(shown, frame_seq=2)], 200_000_000)
    # The new row's badges show a few frames after its names; a misread one is outvoted.
    rows = [event(3), event(4, killer_team=7), event(5, killer_team=8),
            event(6, killer_team=8, victim_team=9)]
    held = [tracker.update([row], row.frame_seq * 100_000_000) for row in rows[:3]]
    assert held == [[], [], []] and tracker.pending
    [out] = tracker.update([rows[3]], 600_000_000)
    assert (out.killer_team, out.victim_team, out.observed_at_ns) == (8, 9, 300_000_000)

    # A held row that drops out of the reads is still confirmed once the wait is over; on a badge
    # tie the later read wins.
    lost = TemporalTracker(session_id=uuid4())
    for frame, row in enumerate((shown, replace(shown, frame_seq=2), event(3, killer_team=7),
                                 event(4, killer_team=8)), 1):
        lost.update([row], frame * 100_000_000)
    assert lost.update([], 700_000_000) == []
    [late] = lost.update([], 900_000_000)
    assert (late.observed_at_ns, late.killer_team, late.victim_team) == (300_000_000, 8, None)

    solo = TemporalTracker(session_id=uuid4())  # no badges in the match: confirm right away
    solo.update([event(1)], 100_000_000)
    assert len(solo.update([event(2)], 200_000_000)) == 1



def _badged_tracker(**settings: int) -> TemporalTracker:
    tracker = TemporalTracker(session_id=uuid4(), **settings)
    shown = event(1, 200, killer_name="Other", victim_name="Rival", killer_team=3, victim_team=5)
    tracker.update([shown], 100_000_000)
    tracker.update([replace(shown, frame_seq=2)], 200_000_000)
    return tracker


def test_a_held_row_that_drops_out_is_not_taken_for_its_alike_neighbour() -> None:
    tracker = _badged_tracker()
    def kilo(frame: int) -> CandidateEvent:
        return event(frame, 20, victim_name="Kilo-0870", visual_fingerprint="a", killer_team=1, victim_team=2)
    emitted = []
    for frame in (3, 4):  # together on screen; the lower row's badges are not read yet
        emitted += tracker.update([kilo(frame), event(frame, 50, victim_name="Milo-0870", visual_fingerprint="b")],
                                  frame * 100_000_000)
    for frame in range(5, 10):  # the held row drops out of the reads, its neighbour stays
        emitted += tracker.update([kilo(frame)], frame * 100_000_000)
    assert sorted(e.victim_name for e in emitted) == ["Kilo-0870", "Milo-0870"]


def test_a_held_row_confirms_before_a_short_max_gap_expires_it() -> None:
    tracker = _badged_tracker(max_gap_ms=300)
    tracker.update([event(3, killer_team=7)], 300_000_000)
    tracker.update([event(4, killer_team=7)], 400_000_000)
    emitted = [e for ns in range(450_000_000, 950_000_000, 50_000_000) for e in tracker.update([], ns)]
    assert [(e.killer_team, e.observed_at_ns) for e in emitted] == [(7, 300_000_000)]

def test_same_player_merges_ocr_drift_but_not_numbered_teammates() -> None:
    from kfocr.domain.tracker import _same_player

    same = [("xxraini38", "xxraini"), ("35WSTCMBB", "WSTCMBB"), ("zhoumo_030", "zhoum0_03o"),
            ("MengChaoOba", "MengChao0ba"), ("ZhanG___1874", "ZhanG_1874")]
    different = [("Player1", "Player2"), ("Player2", "Player10"), ("Kay", "Kai"), ("abc1", "abd1"), ("", "x")]
    assert all(_same_player(a, b) for a, b in same)
    assert not any(_same_player(a, b) for a, b in different)

