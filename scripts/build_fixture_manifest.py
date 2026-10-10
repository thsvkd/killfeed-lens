"""Describe local footage without copying/uploading it or assigning invented labels."""

import argparse
import hashlib
import os
from pathlib import Path

import cv2
import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--split", choices=["tuning", "regression", "holdout"], required=True)
    parser.add_argument("--ui-scale", type=float, required=True)
    parser.add_argument("--locale", default="en")
    parser.add_argument("--scene", choices=["bright", "dark"])
    parser.add_argument("--feed", choices=["slow", "burst"])
    parser.add_argument("--notes", default="")
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--started-at")
    parser.add_argument("--roi-profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture = cv2.VideoCapture(str(args.video))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not capture.isOpened() or fps <= 0:
            parser.error("Cannot decode video or determine its frame rate")
        width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = capture.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    finally:
        capture.release()
    digest = hashlib.sha256()
    with args.video.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    manifest = {"manifest_version": 1, "id": args.id or args.video.stem,
                "video": os.path.relpath(args.video.resolve(), args.output.parent.resolve()),
                "video_sha256": digest.hexdigest(), "resolution": [width, height],
                "ui_scale": args.ui_scale, "locale": args.locale, "duration_sec": duration,
                "split": args.split, "notes": args.notes}
    for key in ("scene", "feed"):
        value = getattr(args, key)
        if value:
            manifest[key] = value
    for key in ("telemetry", "roi_profile"):
        path = getattr(args, key)
        if path:
            manifest[key] = os.path.relpath(path.resolve(), args.output.parent.resolve())
    if args.started_at:
        manifest["started_at_utc"] = args.started_at
    if args.telemetry:
        telemetry_digest = hashlib.sha256()
        with args.telemetry.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                telemetry_digest.update(block)
        manifest["telemetry_sha256"] = telemetry_digest.hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
