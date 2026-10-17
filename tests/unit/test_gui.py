from kfocr.ui.gui import clear_of

ROI, SCREEN = (1747, 0, 804, 292), (2560, 1440)


def test_overlay_is_pushed_off_the_killfeed_roi_by_the_shortest_move() -> None:
    assert clear_of((2280, 80, 264, 400), ROI, SCREEN) == (2280, 300)  # below the ROI
    assert clear_of((1700, 900, 264, 400), ROI, SCREEN) == (1700, 900)  # no overlap: untouched
    assert clear_of((1800, 100, 264, 1200), ROI, SCREEN) == (1475, 100)  # too tall: go left


def test_roi_box_edges_resize_and_interior_moves_within_the_screen() -> None:
    from kfocr.ui.gui import box_to_rect, drag_box, edge_at

    assert [edge_at(*p, 100, 50) for p in ((2, 2), (50, 2), (98, 48), (50, 25))] == ["nw", "n",
                                                                                 "se", ""]
    box = (100, 100, 200, 100)
    assert drag_box(box, "", 50, -500, SCREEN) == (150, 0, 200, 100)  # move, clamped to the top
    assert drag_box(box, "se", 40, 20, SCREEN) == (100, 100, 240, 120)
    assert drag_box(box, "w", 500, 0, SCREEN) == (276, 100, 24, 100)  # never thinner than 24px
    assert drag_box(box, "n", 0, -500, SCREEN) == (100, 0, 200, 200)
    rect = box_to_rect((1747, 1148, 813, 292), SCREEN)  # flush with the bottom-right corner
    assert rect.x + rect.w <= 1 and rect.y + rect.h <= 1
