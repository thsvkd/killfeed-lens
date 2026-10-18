from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from kfocr.domain.models import VisualElement, VisualKind

_MATCH_METHOD = cv2.TM_CCOEFF_NORMED
_MATCH_METHOD_NAME = "TM_CCOEFF_NORMED"
_OVERLAP_IOU = 0.3


@dataclass(frozen=True)
class _Template:
    label: str
    image: np.ndarray
    path: Path
    threshold: float


class TemplateIconRecognizer:
    def __init__(
        self,
        template_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ) -> None:
        self.template_dir = Path(template_dir) if template_dir is not None else None
        self.manifest_path = Path(manifest_path) if manifest_path is not None else None
        self._manifest_metadata: dict[str, Any] | None = None
        self._templates = self._load_templates()

    def detect(self, image: np.ndarray) -> list[VisualElement]:
        if not self._templates or image.size == 0:
            return []
        gray = _gray(image)
        detections: list[VisualElement] = []
        for template in self._templates:
            height, width = template.image.shape[:2]
            if height > gray.shape[0] or width > gray.shape[1]:
                continue
            scores = cv2.matchTemplate(gray, template.image, _MATCH_METHOD)
            ys, xs = np.where(scores >= template.threshold)
            candidates = sorted(
                ((float(scores[y, x]), int(x), int(y)) for y, x in zip(ys, xs, strict=True)),
                reverse=True,
            )
            accepted: list[tuple[int, int, int, int]] = []
            for confidence, x, y in candidates:
                bbox = (x, y, x + width, y + height)
                if any(_iou(bbox, prior) > _OVERLAP_IOU for prior in accepted):
                    continue
                accepted.append(bbox)
                detections.append(
                    VisualElement(VisualKind.ICON, template.label, confidence, bbox)
                )
        return sorted(_resolve_conflicts(detections), key=lambda item: (item.bbox[1], item.bbox[0]))

    def engine_manifest(self) -> dict[str, Any]:
        return {
            "engine": "template",
            "status": "ready" if self._templates else "unknown",
            "manifest": self._manifest_metadata,
            "matching": {
                "method": _MATCH_METHOD_NAME,
                "overlap_iou": _OVERLAP_IOU,
                "conflict_label": "unknown",
            },
            "templates": [
                {
                    "label": template.label,
                    "file": template.path.name,
                    "threshold": template.threshold,
                    "sha256": f"sha256:{hashlib.sha256(template.path.read_bytes()).hexdigest()}",
                }
                for template in self._templates
            ],
        }

    def model_manifest(self) -> dict[str, Any]:
        return self.engine_manifest()

    def _load_templates(self) -> list[_Template]:
        if self.template_dir is None:
            return []
        if not self.template_dir.is_dir():
            raise ValueError(f"Icon template directory does not exist: {self.template_dir}")
        manifest_path = self.manifest_path or self.template_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Icon template manifest does not exist: {manifest_path}")
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read icon template manifest: {manifest_path}") from exc
        if (
            not isinstance(manifest, dict)
            or "icons" not in manifest
            or not isinstance(manifest["icons"], list)
        ):
            raise ValueError("Invalid icon template manifest: expected an object with an icons list")
        manifest_version = manifest.get("manifest_version", 1)
        if isinstance(manifest_version, bool) or not isinstance(manifest_version, (int, str)):
            raise ValueError("Invalid icon template manifest: manifest_version must be int or string")
        self._manifest_metadata = {
            "file": manifest_path.name,
            "version": manifest_version,
            "sha256": f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        }
        templates: list[_Template] = []
        for index, entry in enumerate(manifest.get("icons", [])):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("label"), str)
                or not entry["label"].strip()
                or not isinstance(entry.get("file"), str)
                or not entry["file"].strip()
            ):
                raise ValueError(
                    f"Invalid icon template manifest entry {index}: label and file are required"
                )
            path = self.template_dir / str(entry["file"])
            try:
                path.resolve().relative_to(self.template_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid icon template manifest entry {index}: file escapes template directory"
                ) from exc
            try:
                threshold = float(entry.get("threshold", 0.88))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid icon template manifest entry {index}: threshold must be numeric"
                ) from exc
            if not 0 <= threshold <= 1:
                raise ValueError(
                    f"Invalid icon template manifest entry {index}: threshold must be in [0, 1]"
                )
            if not path.is_file():
                raise ValueError(f"Icon template asset does not exist: {path}")
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None or image.size == 0:
                raise ValueError(f"Unable to decode icon template asset: {path}")
            templates.append(_Template(str(entry["label"]), image, path, threshold))
        return templates


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ValueError("image must be grayscale, BGR, or BGRA")


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if not intersection:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area_a + area_b - intersection)


def _resolve_conflicts(detections: list[VisualElement]) -> list[VisualElement]:
    resolved: list[VisualElement] = []
    for detection in sorted(detections, key=lambda item: item.confidence, reverse=True):
        overlap_index = next(
            (
                index
                for index, prior in enumerate(resolved)
                if _iou(detection.bbox, prior.bbox) > _OVERLAP_IOU
            ),
            None,
        )
        if overlap_index is None:
            resolved.append(detection)
            continue
        prior = resolved[overlap_index]
        if detection.label != prior.label:
            resolved[overlap_index] = VisualElement(
                VisualKind.ICON,
                "unknown",
                min(prior.confidence, detection.confidence),
                (
                    min(prior.bbox[0], detection.bbox[0]),
                    min(prior.bbox[1], detection.bbox[1]),
                    max(prior.bbox[2], detection.bbox[2]),
                    max(prior.bbox[3], detection.bbox[3]),
                ),
            )
    return resolved
