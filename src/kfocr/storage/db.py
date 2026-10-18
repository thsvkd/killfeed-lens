from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from kfocr.domain.models import ConfirmedEvent, EventType, Session


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.journal_mode = str(
            self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        ).casefold()
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              session_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              started_at_utc TEXT NOT NULL,
              ended_at_utc TEXT,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              roi_profile_id TEXT NOT NULL,
              app_version TEXT NOT NULL,
              git_commit TEXT,
              config_hash TEXT NOT NULL,
              model_manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              timeline_origin_ns INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              observed_at_ns INTEGER NOT NULL,
              wall_time_utc TEXT,
              frame_seq INTEGER NOT NULL,
              killer_name_raw TEXT,
              victim_name_raw TEXT,
              killer_name TEXT,
              victim_name TEXT,
              killer_team INTEGER,
              victim_team INTEGER,
              event_type TEXT NOT NULL,
              weapon_label TEXT,
              headshot INTEGER,
              confidence_row REAL NOT NULL,
              confidence_total REAL NOT NULL,
              confidence_killer REAL NOT NULL,
              confidence_victim REAL NOT NULL,
              confidence_event_type REAL NOT NULL,
              source_bbox_json TEXT NOT NULL,
              source_crop_ref TEXT,
              visual_fingerprint TEXT,
              confirmed_at_ns INTEGER NOT NULL,
              dedup_key TEXT NOT NULL,
              tracker_evidence_count INTEGER NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
              session_id TEXT PRIMARY KEY,
              report_json TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS validation_matches (
              session_id TEXT NOT NULL,
              event_id TEXT NOT NULL,
              telemetry_event_id TEXT NOT NULL DEFAULT '',
              telemetry_type TEXT,
              match_score REAL,
              result TEXT,
              detail_json TEXT,
              PRIMARY KEY(session_id, event_id, telemetry_event_id),
              FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );
            """
        )
        event_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "visual_fingerprint" not in event_columns:
            self._connection.execute("ALTER TABLE events ADD COLUMN visual_fingerprint TEXT")
        self._connection.commit()

    def start_session(self, session: Session) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions (
                  session_id, source_type, started_at_utc, ended_at_utc, width, height,
                  roi_profile_id, app_version, git_commit, config_hash, model_manifest_hash,
                  status, timeline_origin_ns, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session.session_id),
                    session.source_type,
                    session.started_at_utc.isoformat(),
                    session.ended_at_utc.isoformat() if session.ended_at_utc else None,
                    session.source_resolution[0],
                    session.source_resolution[1],
                    session.roi_profile_id,
                    session.app_version,
                    session.git_commit,
                    session.config_hash,
                    session.model_manifest_hash,
                    session.status,
                    session.timeline_origin_ns,
                    json.dumps(session.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get_session(self, session_id: UUID | str) -> Session | None:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self) -> list[Session]:
        rows = self._connection.execute(
            "SELECT * FROM sessions ORDER BY started_at_utc DESC, session_id DESC"
        ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def append_event(self, event: ConfirmedEvent) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO events (
                  event_id, session_id, observed_at_ns, frame_seq,
                  killer_name_raw, victim_name_raw, killer_name, victim_name,
                  killer_team, victim_team, event_type, weapon_label, headshot,
                  confidence_row, confidence_total, confidence_killer, confidence_victim,
                  confidence_event_type, source_bbox_json, source_crop_ref,
                  visual_fingerprint, confirmed_at_ns, dedup_key, tracker_evidence_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.session_id),
                    event.observed_at_ns,
                    event.frame_seq,
                    event.killer_name_raw,
                    event.victim_name_raw,
                    event.killer_name,
                    event.victim_name,
                    event.killer_team,
                    event.victim_team,
                    event.event_type.value,
                    event.weapon_label,
                    None if event.headshot is None else int(event.headshot),
                    event.confidence_row,
                    event.confidence_total,
                    event.confidence_killer,
                    event.confidence_victim,
                    event.confidence_event_type,
                    json.dumps(event.source_bbox),
                    event.source_crop_ref,
                    event.visual_fingerprint,
                    event.confirmed_at_ns,
                    event.dedup_key,
                    event.tracker_evidence_count,
                ),
            )

    def amend_event_teams(self, event: ConfirmedEvent) -> None:
        """Badges read after the event was stored."""
        with self._connection:
            self._connection.execute(
                "UPDATE events SET killer_team = ?, victim_team = ? WHERE event_id = ?",
                (event.killer_team, event.victim_team, str(event.event_id)),
            )

    def update_session_metadata(
        self, session_id: UUID | str, metadata: dict[str, Any]
    ) -> None:
        row = self._connection.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        merged = json.loads(row[0])
        merged.update(metadata)
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
                (json.dumps(merged, ensure_ascii=False, sort_keys=True), str(session_id)),
            )

    def heartbeat_session(
        self, session_id: UUID | str, last_observed_at_utc: datetime
    ) -> None:
        if last_observed_at_utc.tzinfo is None:
            raise ValueError("Session heartbeat must include a timezone")
        row = self._connection.execute(
            "SELECT started_at_utc, metadata_json FROM sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        started_at = datetime.fromisoformat(row["started_at_utc"])
        if last_observed_at_utc < started_at:
            raise ValueError("Session heartbeat precedes session start")
        metadata = json.loads(row["metadata_json"])
        metadata["last_observed_at_utc"] = last_observed_at_utc.isoformat()
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), str(session_id)),
            )

    def list_events(self, session_id: UUID | str) -> list[ConfirmedEvent]:
        rows = self._connection.execute(
            """
            SELECT * FROM events WHERE session_id = ?
            ORDER BY observed_at_ns, confirmed_at_ns, event_id
            """,
            (str(session_id),),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def finish_session(
        self, session_id: UUID | str, status: str, ended_at_utc: datetime
    ) -> None:
        if ended_at_utc.tzinfo is None:
            raise ValueError("Session end must include a timezone")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET status = ?, ended_at_utc = ? WHERE session_id = ?",
                (status, ended_at_utc.isoformat(), str(session_id)),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown session: {session_id}")

    def recover_running_sessions(self) -> int:
        running = self._connection.execute(
            """
            SELECT session_id, started_at_utc, timeline_origin_ns, metadata_json
            FROM sessions WHERE status = 'running'
            """
        ).fetchall()
        recovered_at = datetime.now(UTC)
        with self._connection:
            for row in running:
                started_at = datetime.fromisoformat(row["started_at_utc"])
                metadata = json.loads(row["metadata_json"])
                heartbeat = metadata.get("last_observed_at_utc")
                if heartbeat:
                    ended_at = max(started_at, datetime.fromisoformat(heartbeat))
                    source = "heartbeat"
                else:
                    latest_event = self._connection.execute(
                        "SELECT MAX(observed_at_ns) FROM events WHERE session_id = ?",
                        (row["session_id"],),
                    ).fetchone()[0]
                    if latest_event is not None:
                        elapsed_ns = max(0, latest_event - row["timeline_origin_ns"])
                        ended_at = started_at + timedelta(seconds=elapsed_ns / 1_000_000_000)
                        source = "latest_event"
                    else:
                        ended_at = started_at
                        source = "session_start"
                metadata["recovery"] = {
                    "ended_at_estimated": True,
                    "ended_at_source": source,
                    "recovered_at_utc": recovered_at.isoformat(),
                }
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'crashed', ended_at_utc = ?, metadata_json = ?
                    WHERE session_id = ? AND status = 'running'
                    """,
                    (
                        ended_at.isoformat(),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        row["session_id"],
                    ),
                )
        return len(running)

    def save_report(self, session_id: UUID | str, report: dict[str, Any]) -> None:
        session_key = str(session_id)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        matches = report.get("matches")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO reports (session_id, report_json) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET report_json = excluded.report_json
                """,
                (session_key, encoded),
            )
            self._connection.execute(
                "DELETE FROM validation_matches WHERE session_id = ?", (session_key,)
            )
            if isinstance(matches, list):
                for index, match in enumerate(matches):
                    if not isinstance(match, dict):
                        continue
                    self._connection.execute(
                        """
                        INSERT INTO validation_matches VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_key,
                            str(match.get("event_id", index)),
                            str(match.get("telemetry_event_id") or ""),
                            match.get("telemetry_type"),
                            match.get("match_score"),
                            match.get("result"),
                            json.dumps(match, ensure_ascii=False, sort_keys=True),
                        ),
                    )

    def get_report(self, session_id: UUID | str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT report_json FROM reports WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> Session:
        return Session(
            session_id=UUID(row["session_id"]),
            source_type=row["source_type"],
            started_at_utc=datetime.fromisoformat(row["started_at_utc"]),
            ended_at_utc=(
                datetime.fromisoformat(row["ended_at_utc"]) if row["ended_at_utc"] else None
            ),
            source_resolution=(row["width"], row["height"]),
            roi_profile_id=row["roi_profile_id"],
            app_version=row["app_version"],
            git_commit=row["git_commit"],
            config_hash=row["config_hash"],
            model_manifest_hash=row["model_manifest_hash"],
            status=row["status"],
            timeline_origin_ns=row["timeline_origin_ns"],
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ConfirmedEvent:
        headshot = row["headshot"]
        return ConfirmedEvent(
            event_id=UUID(row["event_id"]),
            session_id=UUID(row["session_id"]),
            observed_at_ns=row["observed_at_ns"],
            frame_seq=row["frame_seq"],
            killer_name_raw=row["killer_name_raw"],
            victim_name_raw=row["victim_name_raw"],
            killer_name=row["killer_name"],
            victim_name=row["victim_name"],
            killer_team=row["killer_team"],
            victim_team=row["victim_team"],
            event_type=EventType(row["event_type"]),
            weapon_label=row["weapon_label"],
            headshot=None if headshot is None else bool(headshot),
            confidence_row=row["confidence_row"],
            confidence_total=row["confidence_total"],
            confidence_killer=row["confidence_killer"],
            confidence_victim=row["confidence_victim"],
            confidence_event_type=row["confidence_event_type"],
            source_bbox=tuple(json.loads(row["source_bbox_json"])),
            source_crop_ref=row["source_crop_ref"],
            visual_fingerprint=row["visual_fingerprint"],
            confirmed_at_ns=row["confirmed_at_ns"],
            dedup_key=row["dedup_key"],
            tracker_evidence_count=row["tracker_evidence_count"],
        )
