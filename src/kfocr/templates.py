"""Harvest killfeed icon candidates from a recording and build a template manifest from labels."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from kfocr.domain.models import Frame, NormalizedRect, OCRToken, VisualKind
from kfocr.vision.preprocess import preprocess
from kfocr.vision.roi import crop_roi
from kfocr.vision.rows import RowAssembler, _dhash

SKIP_LABELS = {"", "noise", "skip"}
_LABEL = re.compile(r"[A-Za-z0-9_-]+")


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def icon_gaps(tokens: list[OCRToken], image: np.ndarray) -> list[dict[str, Any]]:
    """Return the widest gap between text boxes of each row: where the killfeed icon sits."""
    gaps: list[dict[str, Any]] = []
    for row in RowAssembler().assemble(tokens, [], 0, 0, image):
        texts = [item for item in row.elements if item.kind is VisualKind.TEXT]
        if len(texts) < 2:
            continue
        height = row.bbox[3] - row.bbox[1]
        left, right = max(pairwise(texts), key=lambda pair: pair[1].bbox[0] - pair[0].bbox[2])
        # Ordinary word spacing is narrower than half a text line; an icon is not.
        if right.bbox[0] - left.bbox[2] < height / 2:
            continue
        bbox = (left.bbox[2], row.bbox[1], right.bbox[0], row.bbox[3])
        gaps.append({"bbox": bbox, "row_bbox": row.bbox, "left": left.label, "right": right.label})
    return gaps


def harvest(
    frames: Iterable[Frame],
    rect: NormalizedRect,
    recognizer: Any,
    output: Path,
    *,
    max_distance: int = 6,
) -> list[dict[str, Any]]:
    """Crop icon gaps from every frame and cluster near-identical crops by dHash distance."""
    # Cluster IDs restart at c0001 each run, so reusing a directory would pair old labels
    # with different icons.
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Harvest output must be a new or empty directory: {output}")
    clusters: list[dict[str, Any]] = []
    output.mkdir(parents=True, exist_ok=True)
    with (output / "candidates.jsonl").open("w", encoding="utf-8") as log:
        try:
            for frame in frames:
                roi = crop_roi(frame.image_bgr, rect)
                if not np.any(roi):
                    continue
                for gap in icon_gaps(recognizer.recognize(preprocess(roi)), roi):
                    left, top, right, bottom = gap["bbox"]
                    crop = roi[top:bottom, left:right]
                    if crop.std() < 1:  # A flat gap holds no icon and cannot be template-matched.
                        continue
                    digest = _dhash(crop)
                    cluster = next((item for item in clusters
                                    if _hamming(item["dhash"], digest) <= max_distance), None)
                    if cluster is None:
                        cluster = {"id": f"c{len(clusters) + 1:04d}", "dhash": digest, "count": 0,
                                   "size": [right - left, bottom - top], "first_ms": frame.captured_at_ns // 1_000_000}
                        clusters.append(cluster)
                        folder = output / "clusters" / cluster["id"]
                        folder.mkdir(parents=True, exist_ok=True)
                        row_left, row_top, row_right, row_bottom = gap["row_bbox"]
                        if not (cv2.imwrite(str(folder / "icon.png"), crop)
                                and cv2.imwrite(str(folder / "row.png"),
                                                roi[row_top:row_bottom, row_left:row_right])):
                            raise RuntimeError(f"Unable to write icon candidate: {folder}")
                    cluster["count"] += 1
                    log.write(json.dumps({"cluster": cluster["id"], "frame_seq": frame.seq,
                                          "t_ms": frame.captured_at_ns // 1_000_000, "bbox": gap["bbox"],
                                          "left": gap["left"], "right": gap["right"]},
                                         ensure_ascii=False) + "\n")
        except KeyboardInterrupt:
            pass  # Live harvest ends with Ctrl+C; keep what was collected.
    clusters.sort(key=lambda item: item["count"], reverse=True)
    (output / "clusters.json").write_text(json.dumps(clusters, indent=2), encoding="utf-8")
    (output / "labels.json").write_text(json.dumps({item["id"]: "" for item in clusters}, indent=2),
                                        encoding="utf-8")
    return clusters


def build(candidates: Path, labels_path: Path, output: Path, threshold: float) -> Path:
    """Copy labelled cluster icons into a template directory with a loadable manifest.json."""
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be in [0, 1]")
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    if not isinstance(labels, dict):
        raise ValueError("labels.json must map cluster IDs to labels")
    known = {item["id"] for item in json.loads((candidates / "clusters.json").read_text(encoding="utf-8"))}
    # labels.json may be written by hand or by an AI labeler: validate before touching disk.
    chosen = []
    for cluster_id, label in sorted(labels.items()):
        if label is not None and not isinstance(label, str):
            raise ValueError(f"Label for {cluster_id} must be a string")
        label = (label or "").strip()
        if label.casefold() in SKIP_LABELS:
            continue
        if cluster_id not in known:
            raise ValueError(f"Unknown cluster in labels: {cluster_id}")
        if not _LABEL.fullmatch(label):
            raise ValueError(f"Label for {cluster_id} may only use letters, digits, _ and -: {label!r}")
        chosen.append((cluster_id, label))
    if not chosen:
        raise ValueError("No labelled clusters; fill labels.json first")
    output.mkdir(parents=True, exist_ok=True)
    icons = []
    for cluster_id, label in chosen:
        name = f"{label}_{cluster_id}.png"
        shutil.copyfile(candidates / "clusters" / cluster_id / "icon.png", output / name)
        icons.append({"label": label, "file": name, "threshold": threshold})
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps({"manifest_version": output.name, "icons": icons}, indent=2),
                        encoding="utf-8")
    from kfocr.vision.icons import TemplateIconRecognizer

    TemplateIconRecognizer(output)  # Fail now, not at capture time, if the manifest cannot load.
    return manifest
