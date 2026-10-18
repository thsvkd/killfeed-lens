"""Smoke the packaged app: the Electron UI renders every window on the bundled engine, and the
engine's kfocr.exe parses, streams board snapshots, starts the Tk GUI and runs the bundled OCR.

Used by CI and script/package.ps1, e.g.  python scripts/smoke_packaged.py dist/KillfeedLens-win32-x64
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
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


def _engine_snapshot(exe: Path) -> None:
    """`kfocr.exe engine --demo` prints a contract v1 snapshot at once and exits on stdin EOF."""
    proc = subprocess.Popen([str(exe), "engine", "--demo"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8")
    timer = threading.Timer(120, proc.kill)  # a hung engine fails the smoke instead of blocking
    timer.start()
    try:
        assert proc.stdout is not None and proc.stdin is not None
        line = proc.stdout.readline()
        proc.stdin.close()
        code = proc.wait()
    finally:
        timer.cancel()
    try:
        snap = json.loads(line)
    except ValueError:
        raise SystemExit(f"FAIL engine --demo printed no snapshot: {line!r}") from None
    if snap.get("v") != 1 or snap.get("mode") != "demo" or code != 0:
        raise SystemExit(f"FAIL engine --demo: exit {code}, snapshot {line[:200]!r}")
    print(f"ok   {exe.name} engine --demo (snapshot v1, exit 0 on stdin EOF)")


def _ui_shots(ui: Path) -> None:
    """The fused, asar-packed UI on the bundled engine, by the same check as `npm run smoke`."""
    smoke = Path(__file__).resolve().parents[1] / "app" / "smoke.mjs"
    result = subprocess.run(["node", str(smoke), str(ui)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=360)
    if result.returncode != 0:
        raise SystemExit(f"FAIL {ui.name} --demo --shots\n{result.stdout}\n{result.stderr[-2000:]}")
    print(f"ok   {ui.name} --demo --shots (app/smoke.mjs: a PNG per window, no renderer warnings)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path, help="Packaged app directory (KillfeedLens-win32-x64)")
    parser.add_argument("--work", type=Path, default=Path("build/smoke"))
    args = parser.parse_args()
    app = args.app.resolve()
    ui, exe = app / "KillfeedLens.exe", app / "resources" / "killfeed-lens" / "kfocr.exe"
    for path in (ui, exe):
        if not path.is_file():
            raise SystemExit(f"FAIL missing executable: {path}")
    work = args.work.resolve() / app.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    _run(exe, "--help")
    _engine_snapshot(exe)
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
    _ui_shots(ui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
