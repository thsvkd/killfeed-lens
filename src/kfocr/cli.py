from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

import cv2
import httpx
import yaml

from kfocr import __version__
from kfocr.config import AppConfig, apply_roi_profile, load_config
from kfocr.domain.models import NormalizedRect
from kfocr.logging import configure_logging, log_event


def _common(parser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--verbose", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kfocr", description="Local pixel-only killfeed history")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("replay", "run"):
        sub = commands.add_parser(command)
        _common(sub)
        if command == "replay":
            sub.add_argument("video", type=Path)
            speed = sub.add_mutually_exclusive_group()
            speed.add_argument("--realtime", action="store_true")
            speed.add_argument("--max-speed", action="store_true")
            sub.add_argument("--started-at", help="Original recording start in timezone-aware ISO 8601")
        else:
            sub.add_argument("--source", choices=["screen"], default="screen")
            sub.add_argument("--monitor", type=int)
            sub.add_argument("--console", action="store_true", help="Console history (default)")
        sub.add_argument("--roi-profile", type=Path)
        sub.add_argument("--roi", nargs=4, type=float, metavar=("X", "Y", "W", "H"))
        sub.add_argument("--save-crops", action="store_true")
        sub.add_argument("--jsonl", type=Path, help="Append confirmed observed events")
        sub.add_argument("--max-frames", type=int, help="Stop after N sampled frames")
    calibrate = commands.add_parser("calibrate")
    _common(calibrate)
    calibrate.add_argument("--monitor", type=int, default=0)
    calibrate.add_argument("--image", type=Path, help="Calibrate from an existing screenshot")
    calibrate.add_argument("--rect", nargs=4, type=float, metavar=("X", "Y", "W", "H"))
    calibrate.add_argument("--output", type=Path, default=Path("data/roi.yaml"))
    calibrate.add_argument("--ui-scale", type=float)
    validate = commands.add_parser("validate")
    _common(validate)
    validate.add_argument("session_id", type=UUID)
    validate.add_argument("--shard", default="steam")
    validate.add_argument("--player")
    validate.add_argument("--match-id")
    validate.add_argument("--telemetry", type=Path, help="Offline official Telemetry JSON")
    validate.add_argument("--wait", action="store_true", help="Bounded polling for match availability")
    report = commands.add_parser("report")
    _common(report)
    report.add_argument("session_id", type=UUID)
    report.add_argument("--format", choices=["text", "json"], default="text")
    export = commands.add_parser("export")
    _common(export)
    export.add_argument("session_id", type=UUID)
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--output", type=Path, required=True)
    sessions = commands.add_parser("sessions", help="List local capture sessions")
    _common(sessions)
    return parser


def _config(args) -> AppConfig:
    config = load_config(args.config)
    if args.db:
        config.storage.sqlite_path = str(args.db)
    if getattr(args, "roi_profile", None):
        apply_roi_profile(config, args.roi_profile)
    if getattr(args, "roi", None):
        config.roi.rect = NormalizedRect(*args.roi)
        config.roi.profile = "explicit-cli-roi"
    if getattr(args, "monitor", None) is not None:
        config.capture.monitor = args.monitor
    if getattr(args, "save_crops", False):
        config.debug.save_event_crops = True
    config.validate()
    return config


def _capture(args, config: AppConfig) -> None:
    if config.roi.rect is None:
        raise ValueError("Calibrate an ROI first: kfocr calibrate; then supply --roi-profile data/roi.yaml")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be positive")
    if args.command == "run":
        if sys.platform != "win32":
            raise ValueError("Live screen capture requires Windows; use replay on this platform")
        print("PUBG/KRAFTON의 공식 승인 제품이 아닙니다. 개발은 녹화영상·리플레이·커스텀 매치 우선입니다.",
              file=sys.stderr)
        from kfocr.capture.screen import ScreenSource

        source = ScreenSource(config.capture.monitor, config.capture.sample_fps)
        started = None
    else:
        from kfocr.capture.replay import ReplaySource

        source = ReplaySource(args.video, config.capture.sample_fps, args.realtime)
        started = datetime.fromisoformat(args.started_at.replace("Z", "+00:00")) if args.started_at else None
        if started is not None and started.tzinfo is None:
            raise ValueError("--started-at must include a timezone, for example 2026-10-01T12:00:00Z")
    if config.vision.icon_template_dir is None:
        print("검증된 아이콘 템플릿이 없어 이벤트 종류는 unknown으로 처리합니다.", file=sys.stderr)
    from kfocr.pipeline import Pipeline
    from kfocr.ui.sidecar import ConsoleSink, JsonlSink, MultiSink

    jsonl = JsonlSink(args.jsonl) if args.jsonl else None
    console = ConsoleSink()
    sink = MultiSink(console, jsonl) if jsonl else console
    try:
        session = Pipeline(config, source, sink=sink, started_at_utc=started).run(args.max_frames)
        print(f"session_id={session.session_id}")
    finally:
        if jsonl:
            jsonl.close()


def _calibrate(args) -> None:
    if args.image:
        image = cv2.imread(str(args.image))
        if image is None:
            raise ValueError("Cannot read calibration image")
    else:
        from kfocr.capture.screen import ScreenSource

        source = ScreenSource(args.monitor)
        try:
            source.start()
            image = next(source.frames()).image_bgr
        finally:
            source.stop()
    height, width = image.shape[:2]
    if args.rect:
        rect = NormalizedRect(*args.rect)
    else:
        try:
            x, y, w, h = cv2.selectROI("Select killfeed ROI; Enter confirms; Esc cancels", image,
                                     showCrosshair=True, fromCenter=False)
        finally:
            cv2.destroyAllWindows()
        if w <= 0 or h <= 0:
            raise ValueError("Calibration cancelled; no profile saved")
        rect = NormalizedRect(x / width, y / height, w / width, h / height)
    if args.ui_scale is not None and not 1 <= args.ui_scale <= 1000:
        raise ValueError("UI scale must be in [1, 1000]")
    profile = {"roi": {"profile": args.output.stem, "rect": asdict(rect),
                       "resolution": [width, height], "ui_scale": args.ui_scale}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    print(f"roi_profile={args.output}")


class ValidationPendingError(ValueError):
    pass


def _telemetry_asset(match: dict) -> str:
    for included in match.get("included", []):
        if included.get("type") == "asset":
            url = included.get("attributes", {}).get("URL")
            if url:
                return url
    raise ValidationPendingError("Match has no official Telemetry asset yet")


def _fetch_online_telemetry(args, config: AppConfig, session):
    from kfocr.telemetry.client import PubgApiClient
    from kfocr.telemetry.match_finder import find_match

    client = PubgApiClient(api_key=os.environ.get(config.telemetry.api_key_env))
    deadline = time.monotonic() + (config.telemetry.max_wait_sec if args.wait else 0)
    try:
        while True:
            # Missing player identity is permanent; only match/asset availability is polled.
            if args.match_id is None:
                player = client.get_player(args.shard, args.player)
                players = player.get("data", [])
                if not players:
                    raise ValueError("Configured player was not found")
                ids = players[0].get("relationships", {}).get("matches", {}).get("data", [])
            try:
                if args.match_id:
                    match, match_id = client.get_match(args.shard, args.match_id), args.match_id
                else:
                    matches = []
                    for item in ids[:20]:
                        try:
                            matches.append(client.get_match(args.shard, item["id"]))
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code != 404:
                                raise
                    decision = find_match(session, matches, args.player)
                    log_event("telemetry.match_candidate", status=decision.status,
                              confidence=decision.confidence)
                    if decision.status == "ambiguous":
                        raise ValueError("Ambiguous match: use --match-id after reviewing candidates")
                    if decision.status != "matched":
                        raise ValidationPendingError("No confident session-to-match match yet")
                    if decision.match is None or decision.match_id is None:
                        raise ValueError("Match selection returned no confirmed match")
                    match, match_id = decision.match, decision.match_id
                return client.get_telemetry(_telemetry_asset(match)), match_id
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                pending_reason = "Official match/Telemetry is not available yet"
            except ValidationPendingError as exc:
                pending_reason = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValidationPendingError(pending_reason + "; bounded wait exhausted")
            time.sleep(min(remaining, config.telemetry.poll_interval_sec, 30))
    finally:
        client.close()


def _validate(args, config: AppConfig, store) -> None:
    from kfocr.metrics.report import format_report
    from kfocr.telemetry.mapping import map_telemetry
    from kfocr.telemetry.validator import validate_events

    session = store.get_session(args.session_id)
    if session is None:
        raise ValueError("Unknown session ID")
    if session.status == "running":
        raise ValueError("Stop capture before post-match validation")
    source = "local" if args.telemetry else "pubg_api"
    try:
        if args.telemetry:
            raw = json.loads(args.telemetry.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
                raise ValueError("Telemetry file must contain an event list")
            match_id = args.match_id
        else:
            if not config.telemetry.enabled:
                raise ValueError("Online Telemetry is disabled in config")
            if not args.player and not args.match_id:
                raise ValueError("Supply --player for automatic match selection or --match-id explicitly")
            raw, match_id = _fetch_online_telemetry(args, config, session)
        report = validate_events(session, store.list_events(args.session_id), map_telemetry(raw))
        report.update({"ground_truth_source": source, "match_id": match_id})
        store.save_report(args.session_id, report)
        print(format_report(report))
        log_event("validation.completed", session_id=args.session_id)
    except Exception as exc:
        # Do not retain exception strings that may contain API URLs or secrets.
        previous = store.get_report(args.session_id)
        last_complete = (previous if previous and previous.get("status") == "complete"
                         else previous.get("previous_completed_report") if previous else None)
        failure = {"status": "pending" if isinstance(exc, ValidationPendingError) else "error",
                   "ground_truth_source": source,
                   "message": "Validation could not complete; observed history is retained"}
        if last_complete is not None:
            failure["previous_completed_report"] = last_complete
        store.save_report(args.session_id, failure)
        raise


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(args.verbose)
    store = None
    try:
        config = _config(args)
        if args.command in ("run", "replay"):
            _capture(args, config)
        elif args.command == "calibrate":
            _calibrate(args)
        else:
            from kfocr.storage.db import EventStore

            store = EventStore(config.storage.sqlite_path)
            from kfocr.pipeline import PipelineBusyError, _session_lock

            try:
                with _session_lock(config.storage.sqlite_path):
                    store.recover_running_sessions()
            except PipelineBusyError:
                pass  # An active capture owns the session; read commands must leave it running.
            if args.command == "validate":
                _validate(args, config, store)
            elif args.command == "sessions":
                for session in store.list_sessions():
                    print(f"{session.session_id} {session.status} {session.source_type} "
                          f"{session.started_at_utc.isoformat()}")
            elif args.command == "report":
                from kfocr.metrics.report import format_report

                report = store.get_report(args.session_id)
                if report is None:
                    pending_session = store.get_session(args.session_id)
                    if pending_session is None:
                        raise ValueError("Unknown session ID")
                    report = {"status": "pending", "session_id": str(args.session_id),
                              "observed_events": len(store.list_events(args.session_id)),
                              "performance": pending_session.metadata.get("performance", {})}
                print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json"
                      else format_report(report))
            elif args.command == "export":
                from kfocr.storage.export import export_events

                if store.get_session(args.session_id) is None:
                    raise ValueError("Unknown session ID")
                export_events(store.list_events(args.session_id), args.output, args.format)
                print(f"export={args.output}")
        return 0
    except KeyboardInterrupt:
        print("Capture stopped; session marked aborted.", file=sys.stderr)
        return 130
    except httpx.HTTPError:
        print("kfocr: PUBG API request failed within the bounded retry budget.", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("kfocr: SQLite storage failed; check the database path, permissions and disk space.",
              file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as exc:
        # API client uses safe errors; never print generic transport exception request objects.
        message = str(exc)
        secret = os.environ.get("PUBG_API_KEY")
        if secret:
            message = message.replace(secret, "[REDACTED]")
        print(f"kfocr: {message}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
