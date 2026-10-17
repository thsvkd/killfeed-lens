from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import cv2
import numpy as np

from kfocr import __version__
from kfocr.config import AppConfig, inference_timing_ms
from kfocr.domain.models import Frame, Session
from kfocr.logging import log_event
from kfocr.ui.sidecar import ConsoleSink


class PipelineBusyError(RuntimeError):
    pass


@contextmanager
def _session_lock(path: str):
    """Allow one writer pipeline per DB; OS releases the lock after crashes."""
    lock_path = Path(path + ".capture.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PipelineBusyError("Another capture pipeline is using this database") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=2, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


class Pipeline:
    def __init__(self, config: AppConfig, source, *, recognizer=None, icons=None, sink=None,
                 store=None, started_at_utc: datetime | None = None, manifest: dict | None = None):
        self.config = config
        self.source = source
        self.recognizer = recognizer
        self.icons = icons
        self.sink = sink
        self.store = store
        self.started_at_utc = started_at_utc
        self.manifest = manifest
        self.performance: dict[str, Any] = {"capture_frames": 0, "inference_calls": 0, "ocr_errors": 0,
                            "confirmed_events": 0, "candidate_reject_count": 0,
                            "dropped_frames": 0, "sink_errors": 0, "black_frames": 0,
                            "queue_depth_max": 0}
        self._ocr_ms: deque[float] = deque(maxlen=10000)
        self._confirm_ms: deque[float] = deque(maxlen=10000)
        self._db_ms: deque[float] = deque(maxlen=10000)

    def _frames(self, live: bool):
        if not live:
            yield from self.source.frames()
            return
        latest: queue.Queue = queue.Queue(maxsize=2)
        stop = threading.Event()
        sentinel = object()

        def offer(item):
            while not stop.is_set():
                try:
                    latest.put_nowait(item)
                    self.performance["queue_depth_max"] = max(
                        self.performance["queue_depth_max"], latest.qsize())
                    return
                except queue.Full:
                    try:
                        latest.get_nowait()
                        self.performance["dropped_frames"] += 1
                    except queue.Empty:
                        pass

        def capture():
            try:
                for frame in self.source.frames():
                    if stop.is_set():
                        break
                    offer(frame)
            except Exception as exc:
                offer(exc)
            finally:
                offer(sentinel)

        thread = threading.Thread(target=capture, name="kfocr-capture", daemon=True)
        thread.start()
        try:
            while True:
                item = latest.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop.set()
            self.source.stop()
            thread.join(timeout=3)

    def run(self, max_frames: int | None = None) -> Session:
        from kfocr.domain.parser import EventParser
        from kfocr.domain.tracker import TemporalTracker
        from kfocr.storage.db import EventStore
        from kfocr.vision.badges import mask_badges, read_badges
        from kfocr.vision.change import EdgeChangeDetector
        from kfocr.vision.icons import TemplateIconRecognizer, mask_icons
        from kfocr.vision.preprocess import preprocess
        from kfocr.vision.roi import crop_roi, pixel_rect
        from kfocr.vision.rows import RowAssembler
        from kfocr.vision.text import RapidOCRRecognizer

        self.config.validate()
        if self.config.roi.rect is None:
            raise ValueError("Calibrate an ROI or provide --roi / --roi-profile before processing")
        own_store = self.store is None
        recognizer = self.recognizer or RapidOCRRecognizer()
        icons = self.icons or TemplateIconRecognizer(self.config.vision.icon_template_dir)
        read_lines = getattr(recognizer, "read_lines", None)  # badges need recognition-only OCR
        manifest = self.manifest or {
            "manifest_version": 1,
            "ocr": getattr(recognizer, "model_manifest", lambda: {
                "engine": type(recognizer).__name__, "checksum": "unavailable"})(),
            "icons": getattr(icons, "model_manifest", lambda: {
                "model": type(icons).__name__, "checksum": "unavailable"})(),
        }
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        store = self.store or EventStore(self.config.storage.sqlite_path)
        detector = EdgeChangeDetector(self.config.vision.change_threshold)
        assembler, parser = RowAssembler(), EventParser()
        session = None
        frames = None
        last_infer_ns: int | None = None
        last_heartbeat_ns: int | None = None
        heartbeat_errors = 0
        interval_ms, refresh_ms, _ = inference_timing_ms(self.config.capture, self.config.tracker)
        previous_roi = None
        pending: deque = deque(maxlen=32)
        start_wall = self.started_at_utc or datetime.now(UTC)
        start_perf = time.perf_counter()
        end_ns = 0

        def persist():
            for _ in range(len(pending)):
                event, attempts = pending.popleft()
                started = time.perf_counter()
                try:
                    store.append_event(event)
                except Exception as exc:
                    log_event("storage.error", attempt=attempts + 1)
                    if attempts >= 2:
                        raise RuntimeError("Unable to persist confirmed events after 3 attempts") from exc
                    pending.append((event, attempts + 1))
                    continue
                self._db_ms.append((time.perf_counter() - started) * 1000)
                try:
                    sink.publish(event)
                except Exception:
                    self.performance["sink_errors"] += 1
                    log_event("sidecar.error")

        try:
            with _session_lock(self.config.storage.sqlite_path):
                store.recover_running_sessions()
                self.source.start()
                from kfocr.capture.screen import ScreenSource

                live = isinstance(self.source, ScreenSource)
                frames = self._frames(live)
                for frame in frames:
                    end_ns = frame.captured_at_ns
                    self.performance["capture_frames"] += 1
                    if session is None:
                        session = Session(uuid4(), "screen" if live else "video", start_wall, None,
                                          (frame.width, frame.height), self.config.roi.profile,
                                          __version__, _git_commit(), self.config.config_hash,
                                          manifest_hash, "running", frame.captured_at_ns,
                                          {"model_manifest": manifest, "platform": platform.platform(),
                                           "ui_scale": self.config.roi.ui_scale,
                                           "roi": asdict(self.config.roi.rect),
                                           "last_observed_at_utc": start_wall.isoformat(),
                                           "ranked_live_usage_approved": False})
                        store.start_session(session)
                        self._prune_artifacts(session.session_id)
                        tracker = TemporalTracker(session.session_id, **asdict(self.config.tracker),
                                                  **asdict(self.config.confidence))
                        sink = self.sink or ConsoleSink(session.timeline_origin_ns)
                        for child_sink in getattr(sink, "sinks", (sink,)):
                            if isinstance(child_sink, ConsoleSink):
                                child_sink.origin_ns = session.timeline_origin_ns
                        log_event("capture.started", session_id=session.session_id,
                                  source=session.source_type)
                    persist()
                    if last_heartbeat_ns is None or end_ns - last_heartbeat_ns >= 1_000_000_000:
                        observed_wall = start_wall + timedelta(
                            seconds=max(0, (end_ns - session.timeline_origin_ns) / 1e9))
                        try:
                            store.heartbeat_session(session.session_id, observed_wall)
                        except Exception as exc:
                            heartbeat_errors += 1
                            log_event("storage.error", operation="heartbeat", attempt=heartbeat_errors)
                            if heartbeat_errors >= 3:
                                raise RuntimeError("Unable to persist session heartbeat") from exc
                        else:
                            last_heartbeat_ns, heartbeat_errors = end_ns, 0
                            session.metadata["last_observed_at_utc"] = observed_wall.isoformat()
                    if not np.any(frame.image_bgr):
                        self.performance["black_frames"] += 1
                        log_event("capture.degraded", reason="black_frame")
                        previous_roi = None
                        if max_frames is not None and self.performance["capture_frames"] >= max_frames:
                            break
                        continue
                    roi = crop_roi(frame.image_bgr, self.config.roi.rect)
                    elapsed = float("inf") if last_infer_ns is None else end_ns - last_infer_ns
                    interval, refresh_ns = interval_ms * 1e6, refresh_ms * 1e6
                    changed = previous_roi is None or detector.should_infer(
                        detector.score(previous_roi, roi))
                    if elapsed >= interval and (changed or elapsed >= refresh_ns):
                        last_infer_ns, previous_roi = end_ns, roi.copy()
                        started = time.perf_counter()
                        self.performance["inference_calls"] += 1
                        ocr_failed = False
                        try:
                            visual = icons.detect(roi)
                            try:
                                badges = read_badges(roi, read_lines) if read_lines else []
                            except Exception:  # badges only refine teams; keep the row OCR
                                log_event("badge.error", frame_seq=frame.seq)
                                badges = []
                            tokens = recognizer.recognize(
                                preprocess(mask_icons(mask_badges(roi, badges), visual)))
                            rows = assembler.assemble(tokens, visual + badges, frame.seq, end_ns, roi)
                            candidates = [event for row in rows if (event := parser.parse(row))]
                        except Exception:
                            self.performance["ocr_errors"] += 1
                            log_event("ocr.error", frame_seq=frame.seq)
                            candidates, ocr_failed = [], True
                        self._ocr_ms.append((time.perf_counter() - started) * 1000)
                        x, y, _, _ = pixel_rect(self.config.roi.rect, frame.width, frame.height)
                        candidates = [replace(c, source_bbox=(c.source_bbox[0] + x,
                            c.source_bbox[1] + y, c.source_bbox[2] + x, c.source_bbox[3] + y))
                            for c in candidates]
                        self.performance["candidate_reject_count"] += sum(
                            c.confidence_total < self.config.confidence.candidate_min for c in candidates)
                        # A failed inference is not evidence that rows disappeared.
                        confirmed = [] if ocr_failed else tracker.update(candidates, end_ns)
                        self._save_frame(frame, session)
                        for event in confirmed:
                            self._save_crop(event, frame, session)
                            if len(pending) == pending.maxlen:
                                raise RuntimeError("Unable to persist: confirmed event buffer is full")
                            pending.append((event, 0))
                            self.performance["confirmed_events"] += 1
                            self._confirm_ms.append((event.confirmed_at_ns - event.observed_at_ns) / 1e6)
                            log_event("tracker.confirmed", event_id=event.event_id)
                        persist()
                    if max_frames is not None and self.performance["capture_frames"] >= max_frames:
                        break
                if session is None:
                    raise RuntimeError("Capture source produced no frames")
                for _ in range(3):
                    if not pending:
                        break
                    persist()
                session.status = "completed"
                session.ended_at_utc = start_wall + timedelta(
                    seconds=max(0, (end_ns - session.timeline_origin_ns) / 1e9))
                store.finish_session(session.session_id, session.status, session.ended_at_utc)
        except BaseException:
            if session is not None:
                session.status = "aborted"
                session.ended_at_utc = start_wall + timedelta(
                    seconds=max(0, (end_ns - session.timeline_origin_ns) / 1e9))
                try:
                    store.finish_session(session.session_id, "aborted", session.ended_at_utc)
                except Exception:
                    log_event("storage.error", operation="finalize")
            raise
        finally:
            if frames is not None:
                frames.close()
            self.source.stop()
            if session is not None:
                duration = max(1e-9, time.perf_counter() - start_perf)
                for label, values in (("ocr_latency_ms", self._ocr_ms),
                                      ("confirmation_latency_ms", self._confirm_ms),
                                      ("db_write_latency_ms", self._db_ms)):
                    self.performance[label] = {
                        "p50": float(np.percentile(values, 50)) if values else None,
                        "p95": float(np.percentile(values, 95)) if values else None}
                self.performance["processing_seconds"] = duration
                self.performance["capture_fps_processing"] = self.performance["capture_frames"] / duration
                self.performance["inference_fps_processing"] = self.performance["inference_calls"] / duration
                session.metadata["performance"] = self.performance
                try:
                    store.update_session_metadata(session.session_id, session.metadata)
                except Exception:
                    log_event("storage.error", operation="metadata")
            if own_store:
                store.close()
        return session

    def _save_crop(self, event, frame: Frame, session: Session) -> None:
        if not self.config.debug.save_event_crops:
            return
        left, top, right, bottom = event.source_bbox
        path = Path(self.config.debug.artifact_dir) / str(session.session_id) / "crops"
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{event.event_id}.png"
        if not cv2.imwrite(str(target), frame.image_bgr[top:bottom, left:right]):
            raise RuntimeError("Unable to save opt-in event crop")
        event.source_crop_ref = str(target)

    def _save_frame(self, frame: Frame, session: Session) -> None:
        if not self.config.debug.save_full_frames:
            return
        path = Path(self.config.debug.artifact_dir) / str(session.session_id) / "frames"
        path.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path / f"{frame.seq:08d}.png"), frame.image_bgr):
            raise RuntimeError("Unable to save opt-in debug frame")

    def _prune_artifacts(self, current: UUID) -> None:
        if not (self.config.debug.save_full_frames or self.config.debug.save_event_crops):
            return
        root = Path(self.config.debug.artifact_dir)
        if not root.exists():
            return
        cutoff = time.time() - self.config.debug.retention_days * 86400
        for directory in root.iterdir():
            try:
                identifier = UUID(directory.name)
            except ValueError:
                continue
            if (identifier != current and directory.is_dir() and not directory.is_symlink()
                    and directory.stat().st_mtime < cutoff):
                shutil.rmtree(directory)
