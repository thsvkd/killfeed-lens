from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from uuid import UUID, uuid4

from kfocr.domain.confidence import event_confidence
from kfocr.domain.models import CandidateEvent, ConfirmedEvent, EventType

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# ponytail: a killfeed row stays about 6 s on screen; retune if PUBG changes the feed duration.
_DUPLICATE_WINDOW_NS = 10_000_000_000
# A new row's team badges read 200-300 ms after its names (frame by frame on 1440p OBS footage),
# so a row with a named side still unbadged waits this long once the match has shown badges.
# ponytail: measured on two matches; it adds up to this much confirm latency to those rows.
_BADGE_WAIT_NS = 500_000_000


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


_OCR_CONFUSABLE = str.maketrans("oil", "011")


def name_key(name: str | None) -> str:
    """Comparison-only spelling: punctuation dropped and OCR look-alikes folded together."""
    return _NON_ALNUM.sub("", _normalized(name)).translate(_OCR_CONFUSABLE)


def _same_player(left: str | None, right: str | None) -> bool:
    """OCR spellings of one name drift between frames (glued badge digits, "_" vs ".", O vs 0).
    Names whose letters match but whose digits differ other than by a glued 1-2 digit badge at
    either end ("Player2"/"Player10") are different players: squads often number their names."""
    a, b = name_key(left), name_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if re.sub(r"\d", "", a) == re.sub(r"\d", "", b):
        short, long = sorted((a, b), key=len)
        extra = len(long) - len(short)
        return 0 < extra <= 2 and (long.startswith(short) or long.endswith(short)) and (
            long.replace(short, "", 1).isdigit()
        )
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def same_teammate(left: str | None, right: str | None) -> bool:
    """Within one squad a name read only in part ("kyeongg" of "da_kyeongg")
    is the same player too, unless the unread part is digits only: numbered squadmates
    ("kim"/"kim2") are left to _same_player."""
    if _same_player(left, right):
        return True
    short, long = sorted((name_key(left), name_key(right)), key=len)
    # ponytail: 4+ shared chars, so squadmates like "Bob"/"Bobby" stay apart; shorter partial
    # reads ("AL" of "LA11AL") stay split. Lower it only with real squads that need it.
    return len(short) >= 4 and short in long and re.search(r"[a-z]", long.replace(short, "", 1)) is not None


def _reread(old: str | None, new: str | None) -> bool:
    """A victim read again as its row scrolls: one name a part of the other (the row cut
    mid-move), one character glued in, or the new read too short to be a name at all (PUBG names
    have 4-16 characters; an earlier short read may still be a squadmate's real kill)."""
    a, b = name_key(old), name_key(new)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    if len(_normalized(new)) < 4 or short in long:
        return True
    return len(long) - len(short) == 1 and any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _most_seen(teams: Iterable[int | None]) -> int | None:
    """Badges flicker between frames on compressed video: the most seen one wins, the first
    given on a tie (pass the latest first: a row still fading in reads worst)."""
    votes = Counter(team for team in teams if team is not None).most_common(1)
    return votes[0][0] if votes else None


def _sure(teams: Iterable[int | None], reads: int) -> int | None:
    """The badge read most, if read at least `reads` times (one stray read fills nothing)."""
    votes = Counter(team for team in teams if team is not None).most_common(1)
    return votes[0][0] if votes and votes[0][1] >= reads else None


def _center_y(candidate: CandidateEvent) -> float:
    return (candidate.source_bbox[1] + candidate.source_bbox[3]) / 2


@dataclass
class _Track:
    first_seen_ns: int
    last_seen_ns: int
    representative: CandidateEvent
    latest: CandidateEvent
    evidence: list[tuple[int, CandidateEvent]] = field(default_factory=list)
    seen_ns: set[int] = field(default_factory=set)  # every update that read this row
    emitted: bool = False
    held: bool = False  # confirmable but waiting for its badges (_BADGE_WAIT_NS)
    # The event this row emitted, or for a re-read the event it repeats: badges it reads later
    # fill that event's missing ones.
    confirmed: ConfirmedEvent | None = None


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
        # A held row must confirm before it can expire.
        self._badge_wait_ns = min(_BADGE_WAIT_NS, self.max_gap_ns)
        self._tracks: list[_Track] = []
        self._recent: list[tuple[int, EventType, str | None, _Track]] = []
        self._last_update_ns: int | None = None
        self._badged = False  # some row showed a badge: not solo, so unbadged rows are unread
        self._amended: list[ConfirmedEvent] = []

    @property
    def pending(self) -> bool:
        """A row seen in the last update still waits for its confirming frame, so the caller should
        read the next frame even if the feed looks unchanged (bounded by the evidence window)."""
        return any(
            not track.emitted
            and track.last_seen_ns == self._last_update_ns
            and track.last_seen_ns - track.first_seen_ns <= self.evidence_window_ns
            for track in self._tracks
        )

    def update(
        self, candidates: list[CandidateEvent], observed_at_ns: int
    ) -> list[ConfirmedEvent]:
        self._last_update_ns = observed_at_ns
        # ponytail: after a long blind streak (OCR failures, black frames, capture loss) expire
        # anyway, trading a possible duplicate for not swallowing a new identical event; retune
        # the 4x max_gap cap against real footage. A held row was confirmed already and emits now.
        self._tracks = [
            track
            for track in self._tracks
            if observed_at_ns - track.last_seen_ns <= 4 * self.max_gap_ns
            or (track.held and not track.emitted)
        ]
        eligible = [item for item in candidates if item.confidence_total >= self.candidate_min]
        self._badged = self._badged or any(
            item.killer_team is not None or item.victim_team is not None for item in eligible)

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
                        evidence=[(observed_at_ns, candidate)],
                        seen_ns={observed_at_ns},
                    )
                )

        emitted: list[ConfirmedEvent] = []
        for track in self._tracks:
            # Only a track seen in this frame may emit, so the crop and frame refer to it; a row
            # held for its badges may have dropped out of the reads meanwhile and emits anyway.
            if track.emitted or (track.last_seen_ns != observed_at_ns and not track.held):
                continue
            track.evidence = [
                item
                for item in track.evidence
                if track.last_seen_ns - item[0] <= self.evidence_window_ns
            ]
            # Evidence counts per exact name pair: fuzzy matching keeps one track, but the
            # emitted spelling must itself repeat.
            groups: dict[tuple[str, str], list[CandidateEvent]] = {}
            for _, observed in track.evidence:
                key = (_normalized(observed.killer_name), _normalized(observed.victim_name))
                groups.setdefault(key, []).append(observed)
            if not groups:
                continue
            group = max(
                groups.values(),
                key=lambda items: (len(items), max(item.confidence_total for item in items)),
            )
            evidence_count = len(group)
            if evidence_count < self.evidence_frames:
                continue
            candidate = max(group, key=lambda item: item.confidence_total)
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
            newest = [seen for _, seen in reversed(track.evidence)]
            killer_team = _most_seen(seen.killer_team for seen in newest)
            victim_team = _most_seen(seen.victim_team for seen in newest)
            if self._badged and observed_at_ns - track.first_seen_ns < self._badge_wait_ns and (
                    killer_team is None or victim_team is None):
                track.held = True  # still pending, so the caller reads the next frame
                continue
            track.emitted = True
            # A row whose spelling drifted can lose its track and come back as a new one. A player
            # dies once and a revive takes ~10 s, so the same victim within the window is the same
            # event even when the killer reads differently (lost clan brackets, glued badge).
            # An earlier row on screen together with this one (read in its last frame) with a
            # merely similar victim is a different row ("Kilo-0870"/"Milo-0870" killed
            # together); an identical one is the same kill shown twice. With the same killer, type
            # and victim badge, a victim read only in part or with a glued character is that kill
            # again too (a row re-read while it scrolls down: 5 of 6 false events on a squad replay).
            self._recent = [
                item for item in self._recent if observed_at_ns - item[0] <= _DUPLICATE_WINDOW_NS
            ]
            rows = [*self._tracks, *(item[3] for item in self._recent)]
            repeated = next((
                other for _, kind, victim, other in reversed(self._recent)
                if (kind is candidate.event_type or EventType.UNKNOWN in (kind, candidate.event_type))
                and (name_key(victim) == name_key(candidate.victim_name) or not any(  # on screen
                    row.confirmed is other.confirmed and track.last_seen_ns in row.seen_ns
                    for row in rows))
                and (_same_player(victim, candidate.victim_name) or (
                    other.confirmed is not None and kind is candidate.event_type
                    and other.confirmed.victim_team is not None
                    and other.confirmed.victim_team == victim_team
                    and _same_player(other.confirmed.killer_name, candidate.killer_name)
                    and _reread(victim, candidate.victim_name)))), None)
            if repeated is not None:
                track.emitted, track.confirmed = True, repeated.confirmed
                continue
            self._recent.append((observed_at_ns, candidate.event_type, candidate.victim_name, track))
            track.representative = candidate
            values = dict(candidate.__dict__)
            values["confidence_total"] = confidence
            values["killer_team"], values["victim_team"] = killer_team, victim_team
            values["observed_at_ns"] = track.first_seen_ns
            # Rows scroll: crop where the row is now, not where the best frame saw it.
            values["source_bbox"] = track.latest.source_bbox
            track.confirmed = ConfirmedEvent(
                **values,
                event_id=uuid4(),
                session_id=self.session_id,
                confirmed_at_ns=observed_at_ns,
                dedup_key=self._dedup_key(track),
                tracker_evidence_count=evidence_count,
            )
            emitted.append(track.confirmed)
        self._amend(observed_at_ns, emitted)
        # Expire only rows missing from this successful observation; failed inference never
        # calls update, so it cannot make a still-visible emitted row look new again. A held row
        # outlives this: _badge_wait_ns <= max_gap_ns, so it emits first.
        self._tracks = [
            track
            for track in self._tracks
            if observed_at_ns - track.last_seen_ns <= self.max_gap_ns
        ]
        return emitted

    def take_amended(self) -> list[ConfirmedEvent]:
        """Events emitted in earlier updates whose missing badges later reads filled in."""
        amended, self._amended = self._amended, []
        return amended

    def _amend(self, observed_at_ns: int, emitted: list[ConfirmedEvent]) -> None:
        """A row keeps being read after it emitted (and its re-reads with it): a badge it lacked
        then and reads now, twice, fills the event in (badges flicker in a burst of rows)."""
        tracks = [*self._tracks, *(item[3] for item in self._recent)]
        for track in self._tracks:
            event = track.confirmed
            if (event is None or track.last_seen_ns != observed_at_ns
                    or None not in (event.killer_team, event.victim_team)):
                continue
            seen = [candidate for _, candidate in track.evidence]
            killer = event.killer_team if event.killer_team is not None else _sure(
                (c.killer_team for c in seen), self.evidence_frames)
            victim = event.victim_team if event.victim_team is not None else _sure(
                (c.victim_team for c in seen), self.evidence_frames)
            if (killer, victim) == (event.killer_team, event.victim_team):
                continue
            fixed = replace(event, killer_team=killer, victim_team=victim)
            for other in tracks:
                if other.confirmed is event:
                    other.confirmed = fixed
            index = next((i for i, item in enumerate(emitted) if item is event), None)
            if index is not None:  # emitted this update: not out yet, so no amendment needed
                emitted[index] = fixed
            else:
                self._amended = [e for e in self._amended if e.event_id != event.event_id] + [fixed]

    def _observe(
        self, track: _Track, candidate: CandidateEvent, observed_at_ns: int
    ) -> None:
        track.last_seen_ns = observed_at_ns
        track.seen_ns.add(observed_at_ns)
        if candidate.frame_seq != track.latest.frame_seq:
            track.evidence.append((observed_at_ns, candidate))
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
