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


def test_overlay_placement_keeps_both_rois_uncovered():
    from kfocr.ui.gui import clear_of_rois

    rois = [(1747, 0, 804, 292), (2280, 300, 264, 60)]
    x, y = clear_of_rois((2280, 80, 264, 400), rois, SCREEN)
    assert 0 <= x <= 2560 - 264 and 0 <= y <= 1440 - 400
    for rx, ry, rw, rh in rois:
        assert x >= rx + rw or x + 264 <= rx or y >= ry + rh or y + 400 <= ry
    assert clear_of_rois((3000, 80, 264, 400), rois, SCREEN) == (3000, 80)  # clear on monitor 2: kept


def test_tracking_off_ends_capture_and_roi_edits_keep_it_off(tmp_path) -> None:
    from types import SimpleNamespace

    from kfocr.config import save_roi_profile
    from kfocr.domain.models import NormalizedRect
    from kfocr.ui.live import LiveCapture

    save_roi_profile(tmp_path / "roi.yaml", NormalizedRect(0.68, 0.0, 0.31, 0.2), (1920, 1080))
    calls: list[str] = []
    live = LiveCapture.__new__(LiveCapture)  # no Tk: the box and the capture are stubbed
    live.base, live.roi_profile, live._thread, live.error, live.tracking = tmp_path, None, None, None, True
    live.box = SimpleNamespace(editing=False, show=lambda _r: calls.append("show"))
    live.alive_box = SimpleNamespace(editing=False, hide=lambda: None)
    live.stop = lambda: calls.append("stop")  # type: ignore[method-assign]
    live.toggle_tracking()
    assert not live.tracking and calls == ["stop"] and live.status == "추적 꺼짐"
    live.start()  # what Enter/Esc on the ROI box call: the ROI is shown, capture stays off
    assert calls == ["stop", "show"] and live._thread is None and live.roi is not None
    live.box.editing = True
    live.toggle_tracking()  # on while the box is being edited: Enter will start it
    assert live.tracking and live._thread is None and live.status == "캡처 대기"
