"""Local pixel-only killfeed recognition."""

import os

# Before numpy loads: its OpenBLAS would fan every small glyph-matching matmul out over all
# cores and spin; with PUBG running one thread cost 2.8 ms CPU per name instead of 10 ms.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "0.5.0"

