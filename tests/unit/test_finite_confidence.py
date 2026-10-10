from __future__ import annotations

import math

import pytest

from kfocr.domain.confidence import clamp_confidence, event_confidence


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_confidence_is_rejected_to_zero(value):
    assert clamp_confidence(value) == 0.0


def test_event_confidence_remains_finite_when_inputs_are_nonfinite():
    confidence = event_confidence(
        row=float("nan"),
        killer=float("inf"),
        victim=float("-inf"),
        event_type=0.9,
        temporal=1.0,
    )

    assert math.isfinite(confidence)
    assert confidence == pytest.approx(0.19)
