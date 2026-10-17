"""Live screen capture inside the GUI process: Pipeline on a worker thread, Tk on the main thread."""
from __future__ import annotations

import logging
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from kfocr.capture.base import CaptureState
from kfocr.capture.screen import ScreenSource
from kfocr.config import AppConfig, apply_roi_profile, load_config, save_roi_profile
from kfocr.domain.models import NormalizedRect
from kfocr.ui.gui import RoiBox
from kfocr.ui.sidecar import EventSink

LOG = logging.getLogger("kfocr.live")
# Icon templates harvested for PUBG UI patch 2026-10; used when config sets no icon_template_dir.
BUNDLED_ICONS = Path(__file__).resolve().parents[1] / "assets" / "icons" / "2026-10"
POLICY = "PUBG/KRAFTON의 공식 승인 제품이 아닙니다. 개발은 녹화영상·리플레이·커스텀 매치 우선입니다."


def data_dir(frozen: bool | None = None, environ: dict[str, str] | None = None) -> Path:
    """Installed/frozen app: %LOCALAPPDATA%\\killfeed-lens. From source: the repo's data/."""
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return Path("data")
    env = os.environ if environ is None else environ
    return Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "killfeed-lens"


def log_path(base: Path) -> Path:
    return base / "logs" / "killfeed-lens.log"


def start_file_log(base: Path) -> Path:
    path = log_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys.stderr, "name", None) != str(path):  # windowed entry already writes stderr here
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)
    return path


def live_config(base: Path, roi_profile: Path | None = None) -> AppConfig:
    """<base>/config.yaml if present; default outputs go to <base>; bundled icon templates unless
    the config names its own; ROI from the profile or <base>/roi.yaml."""
    path = base / "config.yaml"
    config = load_config(path if path.exists() else None)
    defaults = AppConfig()
    if config.storage.sqlite_path == defaults.storage.sqlite_path:
        config.storage.sqlite_path = str(base / "kfocr.sqlite3")
    if config.debug.artifact_dir == defaults.debug.artifact_dir:
        config.debug.artifact_dir = str(base / "sessions")
    if config.vision.icon_template_dir is None:
        config.vision.icon_template_dir = str(BUNDLED_ICONS)
    roi = roi_profile or base / "roi.yaml"
    if roi.exists():
        apply_roi_profile(config, roi)
    return config


class _LiveScreenSource(ScreenSource):
    """ScreenSource whose frame loop another thread can end. The pipeline thread then releases the
    camera itself, so dxcam is never released mid-grab."""

    closing = False

    def start(self) -> None:
        super().start()
        self._running = not self.closing

    def request_stop(self) -> None:
        self.closing = True
        self._running = False


_STATE = {CaptureState.HEALTHY: "캡처 중", CaptureState.DEGRADED: "캡처 불안정",
          CaptureState.ERROR: "캡처 오류"}


class LiveCapture:
    """Start/stop/recalibrate from the Tk thread; `status`, `roi` and `box` are used by the GUI poll."""

    def __init__(self, root: tk.Tk, sink: EventSink, base: Path, roi_profile: Path | None = None):
        self.root, self.sink, self.base, self.roi_profile = root, sink, base, roi_profile
        self.roi: NormalizedRect | None = None
        self.config: AppConfig | None = None
        self.source: _LiveScreenSource | None = None
        self.idle = "캡처 대기"
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self.box = RoiBox(root, self._confirmed, self.start)  # Esc: restart with the old ROI

    @property
    def status(self) -> str:
        if self.error:
            return f"캡처 오류: {self.error}"
        if not (self._thread and self._thread.is_alive() and self.source and self.config):
            return self.idle
        health = self.source.health
        text = _STATE.get(health.state, "캡처 준비 중…")
        if health.state is CaptureState.DEGRADED and health.reason:
            text += f" ({health.reason})"
        return text

    def begin(self) -> None:
        """First run: no ROI yet -> guide calibration, then capture."""
        LOG.info(POLICY)
        self.start()
        if self.roi is None and self.error is None and messagebox.askokcancel(
                "ROI 보정", "킬피드 영역(ROI)이 아직 보정되지 않았습니다.\n\n확인을 누르면 영역 창이 뜹니다. "
                "테두리를 끌어 크기를, 안쪽을 끌어 위치를 킬피드에 맞춘 뒤 Enter(취소: Esc)를 누르세요.\n\n"
                + POLICY,
                parent=self.root):
            self.recalibrate()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():  # stop() timed out; two cameras would clash
            self.error = "이전 캡처가 아직 종료되지 않았습니다. 잠시 후 다시 시도하세요"
            return
        self.error = None
        try:
            config = live_config(self.base, self.roi_profile)
        except (ValueError, OSError) as exc:
            LOG.error("live capture setup failed: %s", exc)
            self.error = str(exc)
            return
        self.config, self.roi = config, config.roi.rect
        if self.roi is None:
            self.idle = "ROI 보정 필요: 'ROI 조정'을 누르세요"
            return
        self.box.show(self.roi)
        self.source = _LiveScreenSource(config.capture.monitor, config.capture.sample_fps)
        self._thread = threading.Thread(target=self._work, args=(config, self.source),
                                        name="kfocr-live", daemon=True)
        self._thread.start()

    def _work(self, config: AppConfig, source: _LiveScreenSource) -> None:
        from kfocr.pipeline import Pipeline

        LOG.info("live capture starting: db=%s monitor=%s icons=%s",
                 Path(config.storage.sqlite_path).resolve(), config.capture.monitor,
                 config.vision.icon_template_dir)
        try:
            session = Pipeline(config, source, sink=self.sink).run()
            LOG.info("live capture stopped: session_id=%s", session.session_id)
            self.idle = "캡처 중지됨"
        except Exception as exc:
            if source.closing:  # stopped before the first frame; nothing to report
                LOG.info("live capture stopped before the first frame")
                return
            LOG.exception("live capture failed")
            self.error = str(exc) or type(exc).__name__

    def stop(self, timeout: float = 15) -> None:
        if self.source:
            self.source.request_stop()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                LOG.warning("live capture did not stop within %ss", timeout)

    def recalibrate(self) -> None:
        """Stop capture and open the ROI box; Enter saves it and restarts, Esc keeps the old ROI."""
        self.stop()
        if self.config and self.config.capture.monitor != 0:
            # ponytail: the box is placed and normalized on Tk's primary screen; map dxcam's output
            # rect onto the virtual desktop if capturing another monitor ever matters.
            messagebox.showwarning("ROI 보정", "영역 창은 주 모니터 기준입니다. capture.monitor가 0이 "
                                   "아니면 ROI가 어긋날 수 있습니다.", parent=self.root)
        self.box.edit(self.roi)

    def toggle_box(self) -> None:
        if self.box.editing:
            self.box.cancel()  # restarts with the old ROI, which shows it again
            self.box.hide()
        elif self.box.visible:
            self.box.hide()
        elif self.roi:
            self.box.show(self.roi)

    def _confirmed(self, rect: NormalizedRect) -> None:
        output = self.base / "roi.yaml"
        try:
            save_roi_profile(output, rect, self.box.screen)
            self.roi_profile = output
            LOG.info("roi profile saved: %s", output.resolve())
        except OSError as exc:
            LOG.warning("roi profile not saved: %s", exc)
            messagebox.showwarning("ROI 보정", f"ROI를 저장하지 않았습니다: {exc}", parent=self.root)
        self.start()
