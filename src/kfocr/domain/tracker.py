from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from uuid import UUID, uuid4

from kfocr.domain.confidence import event_confidence
from kfocr.domain.models import CandidateEvent, ConfirmedEvent, EventType

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _name_similarity(left: str | None, right: str | None) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.25
    if a != b and _NON_ALNUM.sub("", a) == _NON_ALNUM.sub("", b):
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _center_y(candidate: CandidateEvent) -> float:
    return (candidate.source_bbox[1] + candidate.source_bbox[3]) / 2


@dataclass
class _Track:
    first_seen_ns: int
    last_seen_ns: int
    representative: CandidateEvent
    latest: CandidateEvent
    evidence_ns: list[int] = field(default_factory=list)
    emitted: bool = False


class TemporalTracker:
    def __init__(
        self,
        session_id: UUID,
        evidence_frames: int = 2,
        evidence_window_ms: int = 750,
        max_gap_ms: int = 500,
        confirmed_min: float = 0.88,
        candidate_min: float = 0.65,
    ) -> None:
        if evidence_frames < 1 or min(evidence_window_ms, max_gap_ms) < 0:
            raise ValueError("Tracker frame count and time windows must be non-negative")
        if not 0 <= candidate_min <= confirmed_min <= 1:
            raise ValueError("Tracker confidence thresholds are invalid")
        self.session_id = session_id
        self.evidence_frames = evidence_frames
        self.evidence_window_ns = evidence_window_ms * 1_000_000
        self.max_gap_ns = max_gap_ms * 1_000_000
        self.confirmed_min = confirmed_min
        self.candidate_min = candidate_min
        self._tracks: list[_Track] = []

    def update(
        self, candidates: list[CandidateEvent], observed_at_ns: int
    ) -> list[ConfirmedEvent]:
        self._tracks = [
            track
            for track in self._tracks
            if observed_at_ns - track.last_seen_ns <= self.max_gap_ns
        ]
        eligible = [item for item in candidates if item.confidence_total >= self.candidate_min]

        assignments: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(self._tracks):
            for candidate_index, candidate in enumerate(eligible):
                score = self._match_score(track.latest, candidate)
                if score >= 0.58:
                    assignments.append((score, track_index, candidate_index))

        used_tracks: set[int] = set()
        used_candidates: set[int] = set()
        for _, track_index, candidate_index in sorted(assignments, reverse=True):
            if track_index in used_tracks or candidate_index in used_candidates:
                continue
            used_tracks.add(track_index)
            used_candidates.add(candidate_index)
            self._observe(self._tracks[track_index], eligible[candidate_index], observed_at_ns)

        for candidate_index, candidate in enumerate(eligible):
            if candidate_index not in used_candidates:
                self._tracks.append(
                    _Track(
                        first_seen_ns=observed_at_ns,
                        last_seen_ns=observed_at_ns,
                        representative=candidate,
                        latest=candidate,
                        evidence_ns=[observed_at_ns],
                    )
                )

        emitted: list[ConfirmedEvent] = []
        for track in self._tracks:
            if track.emitted:
                continue
            track.evidence_ns = [
                timestamp
                for timestamp in track.evidence_ns
                if observed_at_ns - timestamp <= self.evidence_window_ns
            ]
            evidence_count = len(track.evidence_ns)
            if evidence_count < self.evidence_frames:
                continue
            candidate = track.representative
            confidence = event_confidence(
                row=candidate.confidence_row,
                killer=candidate.confidence_killer,
                victim=candidate.confidence_victim,
                event_type=candidate.confidence_event_type,
                temporal=min(1.0, evidence_count / self.evidence_frames),
            )
            if (
                confidence < self.confirmed_min
                or candidate.killer_name is None
                or candidate.victim_name is None
            ):
                continue
            track.emitted = True
            values = dict(candidate.__dict__)
            values["confidence_total"] = confidence
            values["observed_at_ns"] = track.first_seen_ns
            emitted.append(
                ConfirmedEvent(
                    **values,
                    event_id=uuid4(),
                    session_id=self.session_id,
                    confirmed_at_ns=observed_at_ns,
                    dedup_key=self._dedup_key(track),
                    tracker_evidence_count=evidence_count,
                )
            )
        return emitted

    def _observe(
        self, track: _Track, candidate: CandidateEvent, observed_at_ns: int
    ) -> None:
        track.last_seen_ns = observed_at_ns
        if candidate.frame_seq != track.latest.frame_seq:
            track.evidence_ns.append(observed_at_ns)
        if candidate.confidence_total > track.representative.confidence_total:
            track.representative = candidate
        track.latest = candidate

    @staticmethod
    def _match_score(left: CandidateEvent, right: CandidateEvent) -> float:
        if (
            left.event_type is not EventType.UNKNOWN
            and right.event_type is not EventType.UNKNOWN
            and left.event_type is not right.event_type
        ):
            return 0.0
        killer = _name_similarity(left.killer_name, right.killer_name)
        victim = _name_similarity(left.victim_name, right.victim_name)
        if min(killer, victim) < 0.45:
            return 0.0
        fuzzy_names = (
            _normalized(left.killer_name) != _normalized(right.killer_name)
            or _normalized(left.victim_name) != _normalized(right.victim_name)
        )
        same_fingerprint = bool(left.visual_fingerprint) and (
            left.visual_fingerprint == right.visual_fingerprint
        )
        if fuzzy_names and not same_fingerprint:
            return 0.0
        height = max(1, left.source_bbox[3] - left.source_bbox[1])
        geometry = max(0.0, 1.0 - abs(_center_y(left) - _center_y(right)) / (height * 8))
        fingerprint = float(same_fingerprint)
        type_score = 1.0 if left.event_type is right.event_type else 0.5
        return 0.35 * killer + 0.35 * victim + 0.15 * type_score + 0.10 * geometry + 0.05 * fingerprint

    def _dedup_key(self, track: _Track) -> str:
        item = track.representative
        bucket_ns = max(1, self.evidence_window_ns)
        material = "\x1f".join(
            (
                _normalized(item.killer_name),
                _normalized(item.victim_name),
                item.event_type.value,
                str(track.first_seen_ns // bucket_ns),
                item.visual_fingerprint or "",
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()
