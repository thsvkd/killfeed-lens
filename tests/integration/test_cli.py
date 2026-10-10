import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import cv2
import numpy as np

from kfocr.domain.models import Session


def test_cli_help_and_numeric_calibration(tmp_path, capsys):
    from kfocr.cli import main

    image = tmp_path / "screen.png"
    cv2.imwrite(str(image), np.full((100, 200, 3), 60, dtype=np.uint8))
    profile = tmp_path / "roi.yaml"
    assert main(["calibrate", "--image", str(image), "--rect", "0.5", "0", "0.5", "0.5",
                 "--output", str(profile), "--ui-scale", "100"]) == 0
    from kfocr.config import load_config

    config = load_config(profile)
    assert config.roi.resolution == (200, 100)
    assert config.roi.rect.x == 0.5
    assert config.roi.ui_scale == 100
    assert "roi" in capsys.readouterr().out


def test_replay_requires_explicit_roi_without_creating_database(tmp_path, capsys):
    from kfocr.cli import main

    database = tmp_path / "db.sqlite3"
    assert main(["replay", "missing.mp4", "--db", str(database)]) == 2
    assert "Calibrate" in capsys.readouterr().err
    assert not database.exists()


def test_validate_local_telemetry_without_api_key_and_report(tmp_path, monkeypatch, capsys):
    from kfocr.cli import main
    from kfocr.storage.db import EventStore

    monkeypatch.delenv("PUBG_API_KEY", raising=False)
    db = tmp_path / "db.sqlite3"
    start = datetime(2026, 10, 1, tzinfo=UTC)
    session = Session(uuid4(), "video", start, start + timedelta(seconds=60), (1920, 1080),
                      "test", "0.1.0", None, "config", "model", "completed")
    store = EventStore(str(db))
    store.start_session(session)
    store.close()
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text(json.dumps([{"_T": "LogPlayerMakeGroggy", "_D": start.isoformat(),
                                     "attacker": {"name": "AAAA"}, "victim": {"name": "BBBB"}}]))
    assert main(["validate", str(session.session_id), "--telemetry", str(telemetry),
                 "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["report", str(session.session_id), "--format", "json", "--db", str(db)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ground_truth_source"] == "local"
    assert report["sample_counts"]["observed"] == 0
    assert report["sample_counts"]["telemetry"] == 1


def test_live_capture_is_explicitly_windows_only(capsys):
    import sys

    from kfocr.cli import main

    if sys.platform != "win32":
        assert main(["run", "--source", "screen", "--roi", "0", "0", "1", "1"]) == 2
        assert "Windows" in capsys.readouterr().err


def test_pending_report_does_not_present_unmeasured_metrics_as_zero(capsys):
    from kfocr.metrics.report import format_report

    output = format_report({"status": "pending", "observed_events": 7})
    assert "pending" in output
    assert "7" in output
    assert "TP=0" not in output


def test_listing_sessions_recovers_crashed_writer_but_respects_active_lock(tmp_path, capsys):
    from kfocr.cli import main
    from kfocr.pipeline import _session_lock
    from kfocr.storage.db import EventStore

    db = tmp_path / "events.db"
    store = EventStore(str(db))
    session = Session(uuid4(), "video", datetime.now(UTC), None, (1920, 1080),
                      "test", "0.1.0", None, "config", "model", "running")
    store.start_session(session)
    with _session_lock(str(db)):
        assert main(["sessions", "--db", str(db)]) == 0
        assert store.get_session(session.session_id).status == "running"
    assert main(["sessions", "--db", str(db)]) == 0
    assert store.get_session(session.session_id).status == "crashed"
    store.close()


def test_wait_retries_missing_asset_and_telemetry_404(tmp_path, monkeypatch):
    import httpx

    from kfocr.cli import main
    from kfocr.storage.db import EventStore

    db = tmp_path / "wait.db"
    start = datetime(2026, 10, 1, tzinfo=UTC)
    session = Session(uuid4(), "video", start, start + timedelta(seconds=30), (1920, 1080),
                      "test", "0.1.0", None, "config", "model", "completed")
    store = EventStore(str(db))
    store.start_session(session)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("telemetry: {poll_interval_sec: 0.1, max_wait_sec: 1}\n")

    class AvailabilityClient:
        match_calls = 0
        telemetry_calls = 0

        def __init__(self, **kwargs):
            pass

        def get_match(self, shard, match_id):
            type(self).match_calls += 1
            if self.match_calls == 1:
                return {"included": []}
            return {"included": [{"type": "asset", "attributes": {
                "URL": "https://telemetry-cdn.pubg.com/test.json"}}]}

        def get_telemetry(self, url):
            type(self).telemetry_calls += 1
            if self.telemetry_calls == 1:
                request = httpx.Request("GET", url)
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("Not available", request=request, response=response)
            return []

        def close(self):
            pass

    monkeypatch.setattr("kfocr.telemetry.client.PubgApiClient", AvailabilityClient)
    assert main(["validate", str(session.session_id), "--match-id", "test", "--wait",
                 "--db", str(db), "--config", str(cfg)]) == 0
    assert AvailabilityClient.match_calls == 3
    assert AvailabilityClient.telemetry_calls == 2
    assert store.get_report(session.session_id)["status"] == "complete"
    store.close()


def test_failed_validation_preserves_last_completed_report(tmp_path):
    from kfocr.cli import main
    from kfocr.storage.db import EventStore

    db = tmp_path / "history.db"
    start = datetime(2026, 10, 1, tzinfo=UTC)
    session = Session(uuid4(), "video", start, start + timedelta(seconds=30), (1920, 1080),
                      "test", "0.1.0", None, "config", "model", "completed")
    store = EventStore(str(db))
    store.start_session(session)
    store.save_report(session.session_id, {"status": "complete", "event_metrics": {"tp": 7}})
    malformed = tmp_path / "invalid.json"
    malformed.write_text("{}")
    assert main(["validate", str(session.session_id), "--telemetry", str(malformed),
                 "--db", str(db)]) == 2
    assert store.get_report(session.session_id)["previous_completed_report"]["event_metrics"]["tp"] == 7
    store.close()
