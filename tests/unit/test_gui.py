from kfocr.ui.gui import clear_of

ROI, SCREEN = (1747, 0, 804, 292), (2560, 1440)


def test_overlay_is_pushed_off_the_killfeed_roi_by_the_shortest_move() -> None:
    assert clear_of((2280, 80, 264, 400), ROI, SCREEN) == (2280, 300)  # below the ROI
    assert clear_of((1700, 900, 264, 400), ROI, SCREEN) == (1700, 900)  # no overlap: untouched
    assert clear_of((1800, 100, 264, 1200), ROI, SCREEN) == (1475, 100)  # too tall: go left
