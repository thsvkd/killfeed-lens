from __future__ import annotations

import json
import os
import sys
from types import ModuleType

import cv2
import numpy as np
import pytest

from kfocr.domain.models import NormalizedRect, OCRToken, VisualElement, VisualKind
from kfocr.vision.change import EdgeChangeDetector
from kfocr.vision.icons import TemplateIconRecognizer
from kfocr.vision.preprocess import preprocess
from kfocr.vision.roi import crop_roi, pixel_rect
from kfocr.vision.rows import RowAssembler
from kfocr.vision.text import RapidOCRRecognizer


def test_roi_conversion_uses_exclusive_pixel_bounds_and_returns_an_independent_crop():
    image = np.arange(10 * 20 * 3, dtype=np.uint8).reshape(10, 20, 3)
    rect = NormalizedRect(0.25, 0.2, 0.5, 0.5)

    assert pixel_rect(rect, width=20, height=10) == (5, 2, 15, 7)
    cropped = crop_roi(image, rect)
    assert np.array_equal(cropped, image[2:7, 5:15])
    cropped[:] = 0
    assert np.any(image[2:7, 5:15])


def test_edge_change_detector_responds_to_geometry_but_not_color_only_background_shift():
    previous = np.full((80, 120, 3), 40, dtype=np.uint8)
    current = np.full((80, 120, 3), 160, dtype=np.uint8)
    cv2.rectangle(previous, (10, 20), (50, 40), (255, 255, 255), 2)
    cv2.rectangle(current, (50, 20), (90, 40), (255, 255, 255), 2)
    detector = EdgeChangeDetector(threshold=0.02)

    score = detector.score(previous, current)

    assert 0.02 < score <= 1.0
    assert detector.should_infer(score)
    assert not detector.should_infer(0.019)


def test_preprocess_is_pure_and_preserves_image_geometry():
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    image[5:15, 6:25] = (30, 120, 220)
    original = image.copy()

    processed = preprocess(image)

    assert processed.shape == image.shape[:2]
    assert processed.dtype == np.uint8
    assert np.array_equal(image, original)
    assert not np.shares_memory(image, processed)


def test_rapidocr_adapter_converts_quadrilaterals_to_original_xyxy_coordinates():
    class FakeEngine:
        def __call__(self, image):
            assert image.shape == (100, 200, 3)
            return [
                [[[10.2, 20.9], [80.8, 19.1], [81.3, 42.2], [9.7, 43.0]], "  Alpha  ", 0.91]
            ], [0.01, 0.02, 0.03]

    recognizer = RapidOCRRecognizer(engine=FakeEngine())
    tokens = recognizer.recognize(np.zeros((100, 200, 3), dtype=np.uint8))

    assert tokens == [OCRToken(text="Alpha", confidence=0.91, bbox=(9, 19, 82, 43))]
    assert recognizer.model_manifest()["engine"] == "rapidocr-onnxruntime"


def test_default_rapidocr_disables_runtime_telemetry_before_engine_creation(monkeypatch):
    calls: list[str] = []
    onnxruntime = ModuleType("onnxruntime")

    def disable_telemetry_events():
        assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
        calls.append("disable_telemetry")

    onnxruntime.disable_telemetry_events = disable_telemetry_events
    rapidocr = ModuleType("rapidocr_onnxruntime")

    class FakeRapidOCR:
        def __init__(self):
            assert calls == ["disable_telemetry"]
            calls.append("create_engine")

    rapidocr.RapidOCR = FakeRapidOCR
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", rapidocr)

    RapidOCRRecognizer()

    assert calls == ["disable_telemetry", "create_engine"]


def test_injected_rapidocr_engine_has_no_runtime_telemetry_side_effect(monkeypatch):
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    engine = object()

    recognizer = RapidOCRRecognizer(engine=engine)

    assert recognizer._engine is engine
    assert "ORT_DISABLE_TELEMETRY" not in os.environ


def test_rapidocr_adapter_skips_nonfinite_confidence_and_bbox_values():
    class FakeEngine:
        def __call__(self, image):
            box = [[1, 2], [20, 2], [20, 12], [1, 12]]
            return [
                [box, "Good", 0.9],
                [box, "NaN confidence", float("nan")],
                [box, "Infinite confidence", float("inf")],
                [[[float("nan"), 2], [20, 2], [20, 12], [1, 12]], "Bad box", 0.9],
            ], None

    tokens = RapidOCRRecognizer(engine=FakeEngine()).recognize(
        np.zeros((20, 30, 3), dtype=np.uint8)
    )

    assert tokens == [OCRToken("Good", 0.9, (1, 2, 20, 12))]


def test_template_recognizer_is_strictly_unknown_when_assets_are_not_configured():
    recognizer = TemplateIconRecognizer()

    assert recognizer.detect(np.zeros((40, 60, 3), dtype=np.uint8)) == []
    assert recognizer.engine_manifest()["status"] == "unknown"


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"templates": []},
        {"icons": [{"label": "kill", "file": "kill.png", "threshold": "high"}]},
    ],
)
def test_template_recognizer_rejects_malformed_explicit_manifests(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="icon template manifest"):
        TemplateIconRecognizer(tmp_path)


def test_template_recognizer_rejects_explicit_missing_asset_directory(tmp_path):
    with pytest.raises(ValueError, match="template directory"):
        TemplateIconRecognizer(tmp_path / "missing")


def test_template_recognizer_loads_declared_assets_and_reports_hashes(tmp_path):
    template = np.zeros((8, 10), dtype=np.uint8)
    template[:, 4:6] = 255
    cv2.imwrite(str(tmp_path / "rifle.png"), template)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 7,
                "icons": [{"label": "rifle", "file": "rifle.png", "threshold": 0.99}],
            }
        ),
        encoding="utf-8",
    )
    image = np.zeros((30, 40, 3), dtype=np.uint8)
    image[11:19, 15:25, :] = template[:, :, None]
    recognizer = TemplateIconRecognizer(tmp_path, tmp_path / "manifest.json")

    detected = recognizer.detect(image)

    assert detected[0].label == "rifle"
    assert detected[0].kind is VisualKind.ICON
    assert detected[0].bbox == (15, 11, 25, 19)
    model_manifest = recognizer.engine_manifest()
    assert model_manifest["templates"][0]["sha256"].startswith("sha256:")
    assert model_manifest["templates"][0]["threshold"] == 0.99
    assert model_manifest["manifest"]["version"] == 7
    assert model_manifest["manifest"]["sha256"].startswith("sha256:")
    assert model_manifest["matching"]["method"] == "TM_CCOEFF_NORMED"
    assert recognizer.model_manifest() == model_manifest

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 7,
                "icons": [{"label": "rifle", "file": "rifle.png", "threshold": 0.75}],
            }
        ),
        encoding="utf-8",
    )
    changed = TemplateIconRecognizer(tmp_path).model_manifest()
    assert changed != model_manifest
    assert changed["templates"][0]["threshold"] == 0.75


def test_overlapping_conflicting_icon_templates_degrade_to_unknown(tmp_path):
    template = np.zeros((14, 18), dtype=np.uint8)
    cv2.line(template, (2, 3), (15, 10), 255, 2)
    cv2.circle(template, (5, 10), 2, 180, -1)
    cv2.imwrite(str(tmp_path / "kill.png"), template)
    cv2.imwrite(str(tmp_path / "knock.png"), template)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "icons": [
                    {"label": "kill_marker", "file": "kill.png", "threshold": 0.99},
                    {"label": "knock_marker", "file": "knock.png", "threshold": 0.99},
                ]
            }
        ),
        encoding="utf-8",
    )
    image = np.zeros((40, 60), dtype=np.uint8)
    image[12:26, 20:38] = template

    detected = TemplateIconRecognizer(tmp_path).detect(image)

    assert detected == [
        VisualElement(VisualKind.ICON, "unknown", pytest.approx(1.0), (20, 12, 38, 26))
    ]


def test_row_assembler_clusters_by_vertical_overlap_sorts_elements_and_fingerprints_crop():
    tokens = [
        OCRToken("Victim", 0.8, (80, 12, 120, 24)),
        OCRToken("Killer", 0.9, (5, 10, 45, 22)),
        OCRToken("Other", 0.7, (5, 50, 45, 62)),
    ]
    icons = [VisualElement(VisualKind.ICON, "rifle", 0.95, (52, 11, 72, 23))]
    image = np.full((80, 140, 3), 127, dtype=np.uint8)

    rows = RowAssembler().assemble(tokens, icons, 7, 123_000, image)

    assert len(rows) == 2
    assert [element.label for element in rows[0].elements] == ["Killer", "rifle", "Victim"]
    assert rows[0].bbox == (5, 10, 120, 24)
    assert rows[0].frame_seq == 7
    assert rows[0].observed_at_ns == 123_000
    assert rows[0].row_confidence == pytest.approx(0.8833333333333333)
    assert rows[0].crop_phash is not None and len(rows[0].crop_phash) == 16
    assert [element.label for element in rows[1].elements] == ["Other"]
