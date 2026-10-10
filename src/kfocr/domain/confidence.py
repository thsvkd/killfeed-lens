from __future__ import annotations

import math


def clamp_confidence(value: float) -> float:
    normalized = float(value)
    if not math.isfinite(normalized):
        return 0.0
    return max(0.0, min(1.0, normalized))


def event_confidence(
    *,
    row: float,
    killer: float,
    victim: float,
    event_type: float,
    temporal: float = 0.0,
) -> float:
    """Return the explicit, inspectable confidence score from SPEC section 14."""
    return clamp_confidence(
        0.20 * clamp_confidence(row)
        + 0.30 * clamp_confidence(killer)
        + 0.30 * clamp_confidence(victim)
        + 0.10 * clamp_confidence(event_type)
        + 0.10 * clamp_confidence(temporal)
    )
