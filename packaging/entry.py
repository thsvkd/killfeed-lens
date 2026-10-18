"""Frozen entry point for kfocr.exe (console): the regular `kfocr` CLI.

The installed app ships it in the Electron UI's resources\\killfeed-lens and runs `kfocr.exe
engine`; `kfocr.exe gui --live` still starts the old Tk GUI.
"""

import sys

from kfocr.cli import main
from kfocr.ui.live import data_dir, log_path

if sys.stdout is None or sys.stderr is None:
    # Started without a console (e.g. by a GUI process): keep prints and tracebacks in the app log.
    # A stream that exists stays as it is: `kfocr.exe engine` writes its snapshots to the stdout pipe.
    path = log_path(data_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    log = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

sys.exit(main(sys.argv[1:]))
