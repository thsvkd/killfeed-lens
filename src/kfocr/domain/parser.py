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
# Only arrows count as killfeed structure; |, -, > are common in generic HUD text.
_ARROW_SEPARATOR = re.compile(r"^(?:→|⇒|➜)$")
# A "[CLAN]" tag (badge digits may be glued before it), a "[CLAN" piece whose "]" OCR dropped,
# or a short "404]" fragment whose "[" OCR dropped. Names never contain brackets, and Telemetry
# names carry no clan tag.
_CLAN_TAG = re.compile(r"\[[^\]]*\]|^\[[^\]]*$|^[A-Za-z0-9]{1,4}\]")
_TEAM_BADGE = re.compile(r"\d{1,3}")
_NOT_NAME_CHAR = re.compile(r"[^A-Za-z0-9_-]")
# Team badges show 1-2 digits; "30[LG]" is a badge glued to a clan tag.
_BADGE = re.compile(r"\d{1,2}")
_BADGE_BEFORE_CLAN = re.compile(r"(\d{1,2})\[")


def _center_x(element: VisualElement) -> float:
    return (element.bbox[0] + element.bbox[2]) / 2


def _clean_name(label: str) -> str | None:
    name = label.strip()
    if not name or _UI_SEPARATOR.fullmatch(name) or _TEAM_TOKEN.fullmatch(name):
        return None
    return name


def _canonical_name(raw: str) -> str | None:
    """PUBG names are [A-Za-z0-9_-] without spaces: drop team badges and clan tags, keep the
    longest piece. OCR reads "_" as "." often enough that "." maps back to "_"."""
    pieces = [_NOT_NAME_CHAR.sub("", _CLAN_TAG.sub("", token).replace(".", "_"))
              for token in raw.split()]
    names = [piece for piece in pieces if piece and not _TEAM_BADGE.fullmatch(piece)]
    return max(names, key=_name_rank, default=None)


def _name_rank(name: str | None) -> tuple[bool, int]:
    # All-digit leftovers are usually HUD counters, so a name with letters wins.
    return (name is not None and len(name) >= 4 and not name.isdigit(), len(name or ""))


def _name_run(
    texts: list[tuple[VisualElement, str]], line_height: int
) -> tuple[VisualElement, str] | None:
    """Split texts into runs at gaps wider than a text line (icons, avatars, HUD) and return
    the run holding the longest name; icon and avatar debris reads as short fragments."""
    runs: list[list[tuple[VisualElement, str]]] = []
    for item in sorted(texts, key=lambda item: item[0].bbox[0]):
        if runs and item[0].bbox[0] - runs[-1][-1][0].bbox[2] <= line_height:
            runs[-1].append(item)
        else:
            runs.append([item])
    if not runs:
        return None
    run = max(runs, key=lambda run: _name_rank(_canonical_name(" ".join(item[1] for item in run))))
    elements = [item[0] for item in run]
    merged = VisualElement(
        VisualKind.TEXT,
        " ".join(item[1] for item in run),
        sum(item.confidence for item in elements) / len(elements),
        (min(item.bbox[0] for item in elements), min(item.bbox[1] for item in elements),
         max(item.bbox[2] for item in elements), max(item.bbox[3] for item in elements)),
    )
    return merged, merged.label


def _team(
    run: tuple[VisualElement, str] | None, texts: list[VisualElement], badges: list[VisualElement],
    line_height: int, *, left: bool
) -> int | None:
    """Read the team badge beside a name run: the killer's sits on its left, the victim's on
    its right. A badge box read on its own wins; otherwise fall back to row OCR, where the
    badge is its own token or inside the run ("30[LG] Name", "Name 30")."""
    if run is None:
        return None
    element, raw = run
    boxed = [(gap, badge) for badge in badges
             if -line_height // 2 <= (gap := _gap(element, badge, left)) <= 3 * line_height]
    if boxed:
        return int(min(boxed, key=lambda pair: pair[0])[1].label)
    pieces = raw.split()
    edge = pieces[0] if left else pieces[-1]
    glued = _BADGE_BEFORE_CLAN.match(edge) if left else None
    if glued:
        return int(glued.group(1))
    if len(pieces) > 1 and _BADGE.fullmatch(edge):
        return int(edge)
    gaps = [(_gap(element, item, left), item) for item in texts if _BADGE.fullmatch(item.label.strip())]
    # OCR boxes overlap. Unread trailing "____" can push the badge further out, so a farther
    # badge counts only when no other text sits between it and the name.
    near = [(gap, item) for gap, item in gaps if -line_height // 2 <= gap and (
            gap <= line_height or gap <= 3 * line_height and not any(
                other is not item and _between(other, element, item, left) for other in texts))]
    return int(min(near, key=lambda pair: pair[0])[1].label) if near else None


def _gap(run: VisualElement, badge: VisualElement, left: bool) -> int:
    return run.bbox[0] - badge.bbox[2] if left else badge.bbox[0] - run.bbox[2]


def _between(other: VisualElement, run: VisualElement, badge: VisualElement, left: bool) -> bool:
    x = _center_x(other)
    return badge.bbox[2] <= x <= run.bbox[0] if left else run.bbox[2] <= x <= badge.bbox[0]


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
        killer_team = victim_team = None
        if icon is not None:
            # Killfeed layout: killer, weapon/headshot icons, type marker, victim.
            icon_x = _center_x(icon)
            line_height = max(item[0].bbox[3] - item[0].bbox[1] for item in texts)
            killer = _name_run([item for item in texts if _center_x(item[0]) < icon_x], line_height)
            victim = _name_run([item for item in texts if _center_x(item[0]) > icon_x], line_height)
            row_texts = [item for item in row.elements if item.kind is VisualKind.TEXT]
            badges = [item for item in row.elements if item.kind is VisualKind.BADGE]
            killer_team = _team(killer, row_texts, badges, line_height, left=True)
            victim_team = _team(victim, row_texts, badges, line_height, left=False)
        elif len(texts) >= 2:
            ordered = sorted(texts, key=lambda item: _center_x(item[0]))
            left_text, right_text = ordered[0], ordered[-1]
            if left_text[0].bbox[2] <= right_text[0].bbox[0]:
                killer, victim = left_text, right_text

        # Without an icon, well-read text alone is not killfeed structure; require a separator
        # between the names or the row cannot contribute structural confidence.
        row_conf = row.row_confidence
        if icon is None:
            separated = killer is not None and victim is not None and any(
                element.kind is VisualKind.TEXT
                and _ARROW_SEPARATOR.fullmatch(element.label.strip())
                and killer[0].bbox[2] <= _center_x(element) <= victim[0].bbox[0]
                for element in row.elements
            )
            if not separated:
                row_conf = 0.0

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
            row=row_conf,
            killer=killer_conf,
            victim=victim_conf,
            event_type=type_conf,
        )
        return CandidateEvent(
            observed_at_ns=row.observed_at_ns,
            frame_seq=row.frame_seq,
            killer_name_raw=killer[1] if killer else None,
            victim_name_raw=victim[1] if victim else None,
            killer_name=_canonical_name(killer[1]) if killer else None,
            victim_name=_canonical_name(victim[1]) if victim else None,
            killer_team=killer_team,
            victim_team=victim_team,
            event_type=kind,
            weapon_label=None,
            headshot=None,
            confidence_row=row_conf,
            confidence_killer=killer_conf,
            confidence_victim=victim_conf,
            confidence_event_type=type_conf,
            confidence_total=total,
            source_bbox=row.bbox,
            source_crop_ref=None,
            visual_fingerprint=row.crop_phash,
        )
