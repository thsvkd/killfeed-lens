from __future__ import annotations

import cv2
import numpy as np

from kfocr.domain.models import (
    KillfeedRowCandidate,
    OCRToken,
    VisualElement,
    VisualKind,
)


class RowAssembler:
    def __init__(self, *, vertical_tolerance: float = 0.75) -> None:
        if vertical_tolerance <= 0:
            raise ValueError("vertical_tolerance must be positive")
        self.vertical_tolerance = vertical_tolerance

    def assemble(
        self,
        tokens: list[OCRToken],
        icons: list[VisualElement],
        frame_seq: int,
        observed_at_ns: int,
        image: np.ndarray,
    ) -> list[KillfeedRowCandidate]:
        elements = [
            VisualElement(VisualKind.TEXT, token.text, token.confidence, token.bbox)
            for token in tokens
        ] + list(icons)
        if not elements:
            return []
        clusters: list[list[VisualElement]] = []
        for element in sorted(elements, key=lambda item: (_center_y(item), item.bbox[0])):
            best: list[VisualElement] | None = None
            best_distance = float("inf")
            for cluster in clusters:
                height = max(_height(item) for item in cluster + [element])
                distance = abs(_center_y(element) - np.mean([_center_y(item) for item in cluster]))
                if distance <= height * self.vertical_tolerance and distance < best_distance:
                    best = cluster
                    best_distance = distance
            (best if best is not None else _new_cluster(clusters)).append(element)

        rows: list[KillfeedRowCandidate] = []
        image_height, image_width = image.shape[:2]
        for cluster in clusters:
            ordered = sorted(cluster, key=lambda item: item.bbox[0])
            left = max(0, min(item.bbox[0] for item in ordered))
            top = max(0, min(item.bbox[1] for item in ordered))
            right = min(image_width, max(item.bbox[2] for item in ordered))
            bottom = min(image_height, max(item.bbox[3] for item in ordered))
            crop = image[top:bottom, left:right]
            rows.append(
                KillfeedRowCandidate(
                    frame_seq=frame_seq,
                    observed_at_ns=observed_at_ns,
                    bbox=(left, top, right, bottom),
                    elements=ordered,
                    row_confidence=float(np.mean([item.confidence for item in ordered])),
                    crop_phash=_dhash(crop) if crop.size else None,
                )
            )
        return sorted(rows, key=lambda row: (row.bbox[1], row.bbox[0]))


def _new_cluster(clusters: list[list[VisualElement]]) -> list[VisualElement]:
    cluster: list[VisualElement] = []
    clusters.append(cluster)
    return cluster


def _center_y(element: VisualElement) -> float:
    return (element.bbox[1] + element.bbox[3]) / 2


def _height(element: VisualElement) -> int:
    return max(1, element.bbox[3] - element.bbox[1])


def _dhash(image: np.ndarray) -> str:
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = resized[:, 1:] > resized[:, :-1]
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bit)
    return f"{value:016x}"
