from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from kfocr.domain.models import CandidateEvent, ConfirmedEvent, EventType, Session
from kfocr.storage.db import EventStore
from kfocr.storage.export import export_events, serialize_event


def session(status: str = "running") -> Session:
    return Session(
        session_id=uuid4(),
        source_type="video",
        started_at_utc=datetime(2026, 10, 6, tzinfo=UTC),
        ended_at_utc=None,
        source_resolution=(1920, 1080),
        roi_profile_id="default",
        app_version="0.1.0",
        git_commit=None,
        config_hash="config",
        model_manifest_hash="models",
        status=status,
        timeline_origin_ns=42,
        metadata={"fixture": "synthetic"},
    )


def confirmed(session_id) -> ConfirmedEvent:
    candidate = CandidateEvent(
        observed_at_ns=100,
        frame_seq=2,
        killer_name_raw="A1pha",
        victim_name_raw="Bravo",
        killer_name="Alpha",
        victim_name="Bravo",
        killer_team=1,
        victim_team=2,
        event_type=EventType.KILL,
        weapon_label="rifle",
        headshot=True,
        confidence_row=0.96,
        confidence_killer=0.9,
        confidence_victim=0.95,
        confidence_event_type=0.99,
        confidence_total=0.94,
        source_bbox=(1, 2, 3, 4),
        source_crop_ref="crop.png",
        visual_fingerprint="phash",
    )
    return ConfirmedEvent(
        **candidate.__dict__,
        event_id=uuid4(),
        session_id=session_id,
        confirmed_at_ns=200,
        dedup_key="key",
        tracker_evidence_count=2,
    )


def test_store_round_trips_session_event_and_report(tmp_path) -> None:
    item = session()
    event = confirmed(item.session_id)
    store = EventStore(tmp_path / "events.sqlite3")

    store.start_session(item)
    store.append_event(event)
    store.append_event(event)
    store.update_session_metadata(item.session_id, {"frames": 12})
    store.save_report(str(item.session_id), {"f1": 0.95, "matches": [{"event_id": str(event.event_id)}]})

    expected_session = replace(item, metadata={"fixture": "synthetic", "frames": 12})
    assert store.get_session(item.session_id) == expected_session
    assert store.list_sessions() == [expected_session]
    assert store.list_events(item.session_id) == [event]
    assert store.get_report(item.session_id) == {
        "f1": 0.95,
        "matches": [{"event_id": str(event.event_id)}],
    }
    store.close()


def test_store_wal_and_explicit_recovery(tmp_path) -> None:
    item = session()
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    store.start_session(item)
    assert store.journal_mode == "wal"
    store.close()

    reopened = EventStore(path)
    assert reopened.get_session(item.session_id).status == "running"
    assert reopened.recover_running_sessions() == 1
    recovered = reopened.get_session(item.session_id)
    assert recovered.status == "crashed"
    assert recovered.ended_at_utc == item.started_at_utc
    assert recovered.metadata["recovery"]["ended_at_source"] == "session_start"
    reopened.close()


def test_recovery_uses_last_heartbeat_instead_of_restart_time(tmp_path) -> None:
    item = session()
    heartbeat = item.started_at_utc + timedelta(seconds=5)
    store = EventStore(tmp_path / "events.sqlite3")
    store.start_session(item)
    store.heartbeat_session(item.session_id, heartbeat)
    store.close()

    reopened = EventStore(tmp_path / "events.sqlite3")
    assert reopened.recover_running_sessions() == 1
    recovered = reopened.get_session(item.session_id)
    assert recovered.ended_at_utc == heartbeat
    assert recovered.metadata["last_observed_at_utc"] == heartbeat.isoformat()
    assert recovered.metadata["recovery"]["ended_at_source"] == "heartbeat"
    reopened.close()


def test_recovery_falls_back_to_latest_persisted_event_timeline(tmp_path) -> None:
    item = session()
    event = replace(confirmed(item.session_id), observed_at_ns=item.timeline_origin_ns + 2_000_000_000)
    store = EventStore(tmp_path / "events.sqlite3")
    store.start_session(item)
    store.append_event(event)
    store.close()

    reopened = EventStore(tmp_path / "events.sqlite3")
    assert reopened.recover_running_sessions() == 1
    recovered = reopened.get_session(item.session_id)
    assert recovered.ended_at_utc == item.started_at_utc + timedelta(seconds=2)
    assert recovered.metadata["recovery"]["ended_at_source"] == "latest_event"
    reopened.close()


def test_finish_session_and_exports(tmp_path) -> None:
    item = session()
    event = confirmed(item.session_id)
    store = EventStore(tmp_path / "events.sqlite3")
    store.start_session(item)
    store.append_event(event)
    ended = datetime(2026, 10, 6, 1, tzinfo=UTC)
    store.finish_session(item.session_id, "completed", ended)
    assert store.get_session(item.session_id) == replace(item, status="completed", ended_at_utc=ended)

    jsonl = tmp_path / "events.jsonl"
    csv = tmp_path / "events.csv"
    export_events([event], jsonl)
    export_events([event], csv, format="csv")

    assert '"event_type": "kill"' in jsonl.read_text()
    assert "killer_name" in csv.read_text()
    assert serialize_event(event)["event_id"] == str(event.event_id)
    assert serialize_event(event)["visual_fingerprint"] == "phash"
    store.close()
