"""Smoke a packaged executable: the CLI parses, the GUI starts and the bundled OCR models run.

Used by CI and script/package.ps1, e.g.  python scripts/smoke_packaged.py dist/killfeed-lens/kfocr.exe
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def _run(exe: Path, *args: str) -> None:
    result = subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
    if result.returncode != 0:
        raise SystemExit(f"FAIL {exe.name} {' '.join(args)} -> exit {result.returncode}\n"
                         f"{result.stdout}\n{result.stderr}")
    print(f"ok   {exe.name} {' '.join(args)}")


def _video(path: Path) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 12, (640, 160))
    for i in range(24):
        frame = np.full((160, 640, 3), 30, np.uint8)
        cv2.putText(frame, f"Player{i % 3} knocked out Enemy{i % 5}", (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("--work", type=Path, default=Path("build/smoke"))
    args = parser.parse_args()
    exe = args.exe.resolve()
    if not exe.is_file():
        raise SystemExit(f"FAIL missing executable: {exe}")
    work = args.work.resolve() / exe.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    _run(exe, "--help")
    _run(exe, "gui", "--demo", "--exit-after", "3")
    video, db = work / "smoke.mp4", work / "smoke.sqlite3"
    _video(video)
    _run(exe, "replay", str(video), "--roi", "0", "0", "1", "1", "--db", str(db))
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT status, metadata_json FROM sessions").fetchall()
    if len(rows) != 1 or rows[0][0] != "completed":
        raise SystemExit(f"FAIL replay session not completed: {rows}")
    performance = json.loads(rows[0][1])["performance"]
    if performance["inference_calls"] <= 0 or performance["ocr_errors"] != 0:
        raise SystemExit(f"FAIL bundled OCR did not run cleanly: {performance}")
    print(f"ok   bundled OCR ran {performance['inference_calls']} inferences without errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
