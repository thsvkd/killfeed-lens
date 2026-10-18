import json

import numpy as np
import pytest

from kfocr.config import AppConfig
from kfocr.domain.models import Frame, NormalizedRect, OCRToken
from kfocr.templates import build, harvest


def frames(count=8):
    for seq in range(count):
        image = np.full((100, 320, 3), 40, dtype=np.uint8)
        image[10:30, 20:70] = 255
        image[10:30, 130:190] = 255
        for offset in range(16):  # An X-shaped icon between the two names.
            image[12 + offset, 92 + offset] = image[12 + offset, 107 - offset] = 230
        yield Frame("synthetic", seq, seq * 250_000_000, image, 320, 100)


class Text:
    def recognize(self, image):
        return [OCRToken("AAAA", 0.99, (20, 10, 70, 30)),
                OCRToken("BBBB", 0.99, (130, 10, 190, 30))]

    def read(self, image, icons):  # the pipeline's per-frame name reader
        return self.recognize(image), []


def test_harvest_build_then_replay_confirms_typed_event(tmp_path):
    from kfocr.pipeline import Pipeline

    candidates = tmp_path / "candidates"
    clusters = harvest(frames(), NormalizedRect(0, 0, 1, 1), Text(), candidates)

    assert [(item["id"], item["count"]) for item in clusters] == [("c0001", 8)]
    assert (candidates / "clusters" / "c0001" / "icon.png").is_file()
    assert json.loads((candidates / "labels.json").read_text()) == {"c0001": ""}
    with pytest.raises(ValueError, match="No labelled clusters"):
        build(candidates, candidates / "labels.json", tmp_path / "icons", 0.92)
    with pytest.raises(ValueError, match="new or empty"):
        harvest(frames(), NormalizedRect(0, 0, 1, 1), Text(), candidates)
    for bad in ({"c0001": "../escaped"}, {"c0001": "kill:x"}, {"c0001": 5}, {"c9999": "kill"}):
        (candidates / "labels.json").write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            build(candidates, candidates / "labels.json", tmp_path / "icons", 0.92)
    assert not (tmp_path / "icons").exists() and not (tmp_path / "escaped_c0001.png").exists()

    (candidates / "labels.json").write_text(json.dumps({"c0001": "kill_marker"}))
    manifest = build(candidates, candidates / "labels.json", tmp_path / "icons", 0.92)

    class Source:
        def start(self):
            pass

        def frames(self):
            return frames(16)

        def stop(self):
            pass

    config = AppConfig()
    config.roi.rect = NormalizedRect(0, 0, 1, 1)
    config.storage.sqlite_path = str(tmp_path / "events.db")
    config.vision.icon_template_dir = str(manifest.parent)
    events = []

    class Sink:
        def publish(self, event):
            events.append(event)

    Pipeline(config, Source(), recognizer=Text(), reader=Text(), sink=Sink()).run()
    assert [(e.killer_name, e.victim_name, e.event_type.value) for e in events] == [
        ("AAAA", "BBBB", "kill")]


def test_interrupted_live_harvest_keeps_collected_clusters(tmp_path):
    def live():
        yield from frames(3)
        raise KeyboardInterrupt

    clusters = harvest(live(), NormalizedRect(0, 0, 1, 1), Text(), tmp_path / "live")
    assert [item["count"] for item in clusters] == [3]
    assert json.loads((tmp_path / "live" / "labels.json").read_text()) == {"c0001": ""}
