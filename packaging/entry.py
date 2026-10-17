"""Frozen entry point for KillfeedLens.exe (windowed), the portable exe and kfocr.exe (console).

Without arguments the GUI executables start `kfocr gui --live`; with arguments every executable is
the regular `kfocr` CLI.
"""

import sys
from pathlib import Path

from kfocr.cli import main
from kfocr.ui.live import data_dir, log_path

if sys.stderr is None:  # windowed build: no console, keep prints and tracebacks in the app log
    path = log_path(data_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)

argv = sys.argv[1:]
if not argv and Path(sys.executable).stem.lower() != "kfocr":
    argv = ["gui", "--live"]
sys.exit(main(argv))
