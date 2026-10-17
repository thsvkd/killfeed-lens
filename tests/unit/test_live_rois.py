from kfocr.config import load_config, save_roi_profile
from kfocr.domain.models import NormalizedRect
from kfocr.ui.live import LiveCapture


class Box:
    screen = (1920, 1080)
    editing = False
    visible = False

    def __init__(self, root, on_confirm, on_cancel, **kwargs):
        pass

    def show(self, rect):
        self.editing, self.visible = False, True

    def edit(self, rect):
        self.editing, self.visible = True, True

    def hide(self):
        self.editing, self.visible = False, False


def test_recalibrating_either_roi_preserves_the_other_and_tracking_stays_off(tmp_path, monkeypatch):
    monkeypatch.setattr("kfocr.ui.live.RoiBox", Box)
    path = tmp_path / "roi.yaml"
    feed = NormalizedRect(0.6, 0.1, 0.4, 0.2)
    alive = NormalizedRect(0.92, 0.01, 0.04, 0.04)
    save_roi_profile(path, feed, (1920, 1080), 90, alive_rect=alive)
    live = LiveCapture(None, None, tmp_path)
    live.tracking = False
    live.start()
    new_feed = NormalizedRect(0.6, 0.15, 0.4, 0.2)
    live._confirmed(new_feed)
    saved = load_config(path)
    assert saved.roi.rect == new_feed and saved.roi.alive_rect == alive
    assert saved.roi.ui_scale == 90 and live.source is None
    new_alive = NormalizedRect(0.9, 0.01, 0.04, 0.04)
    live._alive_confirmed(new_alive)
    saved = load_config(path)
    assert saved.roi.rect == new_feed and saved.roi.alive_rect == new_alive
    assert live.roi == new_feed and live.alive_roi == new_alive
    assert live.source is None and not live.tracking


def test_two_step_calibration_does_not_start_capture_before_second_roi(tmp_path, monkeypatch):
    monkeypatch.setattr("kfocr.ui.live.RoiBox", Box)
    live = LiveCapture(None, None, tmp_path)
    live.tracking = False
    live.start()
    live.recalibrate_alive()  # with no killfeed yet, select it first
    assert live.box.editing and not live.alive_box.editing
    feed = NormalizedRect(0.6, 0.1, 0.4, 0.2)
    live._confirmed(feed)
    assert live.alive_box.editing and live.source is None
    # Cancel the second step: retain the saved killfeed, with no invented HUD ROI.
    live._cancel_calibration()
    assert load_config(tmp_path / "roi.yaml").roi.rect == feed
    assert live.alive_roi is None and live.source is None
