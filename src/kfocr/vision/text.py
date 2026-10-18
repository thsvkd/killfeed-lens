from __future__ import annotations

import hashlib
import logging
import math
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

from kfocr.domain.models import OCRToken

# ponytail: ONNX Runtime defaults to one busy-waiting thread per core (~8 cores at 1440p, which
# drops game frames). One thread, no spin-wait: with PUBG running a second thread made a name read
# ~30% faster for ~1.3x the CPU, and its spin-wait burnt ~32 ms CPU after every sparse call.
# Killfeed text is always horizontal, so the angle classifier only costs time. Raise threads
# if latency matters more than the game's CPU.
ENGINE_SETTINGS: dict[str, Any] = {"intra_op_num_threads": 1, "inter_op_num_threads": 1, "use_cls": False}
# rapidocr builds its own SessionOptions and only exposes thread counts; _quiet_sessions adds these.
SESSION_CONFIG = {"session.intra_op.allow_spinning": "0", "session.inter_op.allow_spinning": "0"}


def _quiet_sessions() -> None:
    try:
        from rapidocr_onnxruntime.utils import infer_engine

        build = infer_engine.OrtInferSession._init_sess_opts
    except (ImportError, AttributeError):  # another rapidocr layout: OCR still works, spins
        logging.getLogger(__name__).warning("cannot disable ONNX Runtime spin-wait")
        return
    if getattr(build, "kfocr_quiet", False):
        return

    def options(config: dict[str, Any]) -> Any:
        opts = build(config)
        for key, value in SESSION_CONFIG.items():
            opts.add_session_config_entry(key, value)
        return opts

    options.kfocr_quiet = True  # type: ignore[attr-defined]
    infer_engine.OrtInferSession._init_sess_opts = staticmethod(options)


class RapidOCRRecognizer:
    def __init__(self, *, engine: Any | None = None) -> None:
        if engine is None:
            os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
            try:
                import onnxruntime
            except ImportError as exc:
                raise RuntimeError(
                    "Local OCR requires the OCR extra: pip install 'killfeed-lens[ocr]'"
                ) from exc
            onnxruntime.disable_telemetry_events()
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "Local OCR requires the OCR extra: pip install 'killfeed-lens[ocr]'"
                ) from exc
            _quiet_sessions()
            engine = RapidOCR(**ENGINE_SETTINGS)
        self._engine = engine

    def recognize(self, image: np.ndarray) -> list[OCRToken]:
        if image.size == 0:
            return []
        output = self._engine(image)
        result = output[0] if isinstance(output, tuple) else output
        if not result:
            return []
        height, width = image.shape[:2]
        tokens: list[OCRToken] = []
        for item in result:
            if len(item) < 3:
                continue
            points, text, confidence = item[:3]
            normalized_text = str(text).strip()
            if not normalized_text:
                continue
            try:
                normalized_confidence = float(confidence)
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
            except (IndexError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(normalized_confidence)
                or not xs
                or not ys
                or not all(math.isfinite(value) for value in (*xs, *ys))
            ):
                continue
            bbox = (
                max(0, math.floor(min(xs))),
                max(0, math.floor(min(ys))),
                min(width, math.ceil(max(xs))),
                min(height, math.ceil(max(ys))),
            )
            if bbox[0] < bbox[2] and bbox[1] < bbox[3]:
                tokens.append(OCRToken(normalized_text, normalized_confidence, bbox))
        return tokens

    def read_lines(self, images: list[np.ndarray]) -> list[tuple[str, float]]:
        """Recognition only, one text line per image (no detection), batched."""
        if not images:
            return []
        results, _ = self._engine.text_rec(images)
        return [(str(text), float(confidence)) for text, confidence in results]

    def engine_manifest(self) -> dict[str, Any]:
        return model_manifest()

    def model_manifest(self) -> dict[str, Any]:
        return self.engine_manifest()


def model_manifest() -> dict[str, Any]:
    try:
        version = metadata.version("rapidocr-onnxruntime")
        import rapidocr_onnxruntime
    except (metadata.PackageNotFoundError, ImportError):
        return {"engine": "rapidocr-onnxruntime", "status": "unavailable", "models": []}

    package_root = Path(rapidocr_onnxruntime.__file__).resolve().parent
    models = []
    for path in sorted((package_root / "models").glob("*.onnx")):
        models.append({"file": path.name, "sha256": f"sha256:{_sha256(path)}"})
    return {
        "engine": "rapidocr-onnxruntime",
        "version": version,
        "status": "ready" if models else "unknown",
        "models": models,
        "settings": ENGINE_SETTINGS,
        "session_config": SESSION_CONFIG,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
