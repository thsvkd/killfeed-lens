from __future__ import annotations

import re

from kfocr.domain.confidence import event_confidence
from kfocr.domain.models import (
    CandidateEvent,
    EventType,
    KillfeedRowCandidate,
    VisualElement,
    VisualKind,
)

_TEAM_TOKEN = re.compile(r"^[\[({]?\d{1,3}[\])}]?$|^team\s*\d+$", re.IGNORECASE)
_UI_SEPARATOR = re.compile(r"^(?:→|⇒|➜|›|>|\||-|–|—)$")


def _center_x(element: VisualElement) -> float:
    return (element.bbox[0] + element.bbox[2]) / 2


def _clean_name(label: str) -> str | None:
    name = label.strip()
    if not name or _UI_SEPARATOR.fullmatch(name) or _TEAM_TOKEN.fullmatch(name):
        return None
    return name


def _event_type(label: str) -> EventType:
    normalized = label.casefold().replace("_", " ").replace("-", " ")
    if "knock" in normalized or "groggy" in normalized or "down" in normalized:
        return EventType.KNOCK
    if "kill" in normalized or "skull" in normalized:
        return EventType.KILL
    return EventType.UNKNOWN


class EventParser:
    """Parse assembled row elements by geometry, independent of OCR result order."""

    def parse(self, row: KillfeedRowCandidate) -> CandidateEvent | None:
        texts = [
            (element, name)
            for element in row.elements
            if element.kind is VisualKind.TEXT and (name := _clean_name(element.label)) is not None
        ]
        if not texts:
            return None

        icons = [element for element in row.elements if element.kind is VisualKind.ICON]
        recognized_icons = [item for item in icons if _event_type(item.label) is not EventType.UNKNOWN]
        icon = max(recognized_icons or icons, key=lambda item: item.confidence, default=None)
        kind = _event_type(icon.label) if icon is not None else EventType.UNKNOWN

        killer: tuple[VisualElement, str] | None = None
        victim: tuple[VisualElement, str] | None = None
        if icon is not None:
            icon_x = _center_x(icon)
            left = [item for item in texts if _center_x(item[0]) < icon_x]
            right = [item for item in texts if _center_x(item[0]) > icon_x]
            killer = max(left, key=lambda item: _center_x(item[0]), default=None)
            victim = min(right, key=lambda item: _center_x(item[0]), default=None)
        elif len(texts) >= 2:
            ordered = sorted(texts, key=lambda item: _center_x(item[0]))
            left_text, right_text = ordered[0], ordered[-1]
            if left_text[0].bbox[2] <= right_text[0].bbox[0]:
                killer, victim = left_text, right_text

        # With an unrecognized icon, retaining the observed side is useful for diagnostics.
        if icon is not None and killer is None and victim is None:
            nearest = min(texts, key=lambda item: abs(_center_x(item[0]) - _center_x(icon)))
            if _center_x(nearest[0]) < _center_x(icon):
                killer = nearest
            else:
                victim = nearest

        if killer is None and victim is None:
            return None

        killer_conf = killer[0].confidence if killer else 0.0
        victim_conf = victim[0].confidence if victim else 0.0
        type_conf = icon.confidence if icon is not None and kind is not EventType.UNKNOWN else 0.0
        total = event_confidence(
            row=row.row_confidence,
            killer=killer_conf,
            victim=victim_conf,
            event_type=type_conf,
        )
        return CandidateEvent(
            observed_at_ns=row.observed_at_ns,
            frame_seq=row.frame_seq,
            killer_name_raw=killer[1] if killer else None,
            victim_name_raw=victim[1] if victim else None,
            killer_name=killer[1] if killer else None,
            victim_name=victim[1] if victim else None,
            killer_team=None,
            victim_team=None,
            event_type=kind,
            weapon_label=None,
            headshot=None,
            confidence_row=row.row_confidence,
            confidence_killer=killer_conf,
            confidence_victim=victim_conf,
            confidence_event_type=type_conf,
            confidence_total=total,
            source_bbox=row.bbox,
            source_crop_ref=None,
            visual_fingerprint=row.crop_phash,
        )
