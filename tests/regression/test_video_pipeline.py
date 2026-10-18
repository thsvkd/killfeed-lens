from __future__ import annotations

import json
from datetime import UTC, datetime

import cv2
import numpy as np

from kfocr.capture.replay import ReplaySource
from kfocr.config import AppConfig
from kfocr.domain.models import EventType, NormalizedRect
from kfocr.pipeline import Pipeline
from kfocr.storage.db import EventStore


def _write_test_only_icon_assets(directory) -> np.ndarray:
    """Create a synthetic marker for plumbing tests, not a PUBG recognition asset."""
    marker = np.zeros((80, 80), dtype=np.uint8)
    diamond = np.array([[40, 5], [73, 40], [40, 73], [7, 40]], dtype=np.int32)
    cv2.polylines(marker, [diamond], True, 255, 5, cv2.LINE_AA)
    cv2.line(marker, (18, 53), (58, 20), 180, 4, cv2.LINE_AA)
    cv2.circle(marker, (56, 57), 7, 230, -1, cv2.LINE_AA)
    cv2.imwrite(str(directory / "synthetic-kill.png"), marker)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "fixture_notice": "test-only synthetic marker; not PUBG training evidence",
                "icons": [
                    {
                        "label": "kill_marker",
                        "file": "synthetic-kill.png",
                        "threshold": 0.75,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return marker


def _write_synthetic_killfeed_video(path, marker: np.ndarray) -> None:
    fps = 10.0
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (1280, 240)
    )
    assert writer.isOpened()
    for frame_index in range(20):
        frame = np.full((240, 1280, 3), 24, dtype=np.uint8)
        for index, letter in enumerate("AAAA"):  # killfeed glyphs never touch across a name
            cv2.putText(
                frame,
                letter,
                (40 + 75 * index, 155),
                cv2.FONT_HERSHEY_SIMPLEX,
                3,
                (255, 255, 255),
                5,
                cv2.LINE_AA,
            )
        frame[75:155, 570:650] = marker[:, :, None]
        cv2.putText(
            frame,
            "BBBB",
            (760, 155),
            cv2.FONT_HERSHEY_SIMPLEX,
            3,
            (255, 255, 255),
            5,
            cv2.LINE_AA,
        )
        frame[frame_index % 2, 0] = (25, 25, 25)
        writer.write(frame)
    writer.release()


def test_real_local_ocr_template_and_pipeline_store_one_synthetic_video_event(tmp_path):
    marker = _write_test_only_icon_assets(tmp_path)
    video_path = tmp_path / "synthetic-killfeed.avi"
    _write_synthetic_killfeed_video(video_path, marker)
    config = AppConfig()
    config.capture.sample_fps = 10
    config.capture.inference_fps_cap = 4
    config.roi.rect = NormalizedRect(0, 0, 1, 1)
    config.roi.profile = "test-only-full-frame"
    config.tracker.evidence_window_ms = 1_000
    config.tracker.max_gap_ms = 700
    config.storage.sqlite_path = str(tmp_path / "events.sqlite3")
    config.debug.artifact_dir = str(tmp_path / "artifacts")
    config.vision.icon_template_dir = str(tmp_path)

    session = Pipeline(
        config,
        ReplaySource(video_path, sample_fps=10, realtime=False),
        started_at_utc=datetime(2026, 10, 6, tzinfo=UTC),
    ).run()

    store = EventStore(config.storage.sqlite_path)
    events = store.list_events(session.session_id)
    store.close()
    assert len(events) == 1
    assert (events[0].killer_name, events[0].victim_name) == ("AAAA", "BBBB")
    assert events[0].event_type is EventType.KILL
    assert events[0].tracker_evidence_count >= 2
    assert session.metadata["model_manifest"]["ocr"]["status"] == "ready"
    assert session.metadata["model_manifest"]["icons"]["status"] == "ready"
