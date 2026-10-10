import numpy as np
import pytest


def test_roi_rejects_out_of_frame_and_non_finite_coordinates():
    from kfocr.domain.models import NormalizedRect

    for values in [(0.9, 0, 0.2, 0.3), (0, 0, 0, 0.2), (float("nan"), 0, 0.2, 0.2)]:
        with pytest.raises(ValueError):
            NormalizedRect(*values)


def test_frame_rejects_mismatched_dimensions():
    from kfocr.domain.models import Frame

    with pytest.raises(ValueError):
        Frame("test", 0, 0, np.zeros((10, 20, 3), dtype=np.uint8), 30, 10)

