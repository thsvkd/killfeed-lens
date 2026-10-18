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


def test_region_frame_must_lie_inside_its_screen():
    from kfocr.domain.models import Frame

    region = np.zeros((10, 20, 3), dtype=np.uint8)
    assert Frame("test", 0, 0, region, 30, 10, origin=(10, 0)).image_bgr is region
    for origin in [(11, 0), (0, 1), (-1, 0)]:
        with pytest.raises(ValueError):
            Frame("test", 0, 0, region, 30, 10, origin=origin)
    with pytest.raises(ValueError):
        Frame("test", 0, 0, np.zeros((10, 20), dtype=np.uint8), 30, 10, origin=(0, 0))

