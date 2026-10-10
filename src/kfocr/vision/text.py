from __future__ import annotations

import hashlib
import math
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

from kfocr.domain.models import OCRToken


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
            engine = RapidOCR()
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
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
