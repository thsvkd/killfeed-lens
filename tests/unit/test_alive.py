import numpy as np
import pytest


class Digits:
    def __init__(self, *reads):
        self.reads = iter(reads)

    def read_lines(self, images):
        return [next(self.reads)]


def test_alive_count_needs_repeated_evidence_and_allows_recall_increases():
    from kfocr.vision.alive import AliveCountReader

    reader = AliveCountReader()
    ocr = Digits(("96", 0.99), ("96", 0.99), ("95", 0.99), ("95", 0.99),
                 ("96", 0.99), ("96", 0.99))
    image = np.full((24, 40, 3), 80, dtype=np.uint8)
    results = [reader.read(image, ocr, i, i * 400_000_000) for i in range(6)]
    assert [r.count if r else None for r in results] == [None, 96, None, 95, None, 96]
    assert results[-1].observed_at_ns == 2_000_000_000


@pytest.mark.parametrize("bad", [("196", 0.99), ("9O", 0.99), ("96 생존", 0.99),
                                 ("96", 0.5), ("96", float("nan")), ("", 1.0)])
def test_bad_alive_read_breaks_evidence_instead_of_confirming_a_number(bad):
    from kfocr.vision.alive import AliveCountReader

    reader = AliveCountReader()
    ocr = Digits(("96", 0.99), bad, ("96", 0.99), ("96", 0.99))
    image = np.full((24, 40, 3), 80, dtype=np.uint8)
    results = [reader.read(image, ocr, i, i * 300_000_000) for i in range(4)]
    assert [r.count if r else None for r in results] == [None, None, None, 96]


def test_same_frame_or_old_evidence_cannot_confirm_alive_count():
    from kfocr.vision.alive import AliveCountReader

    reader = AliveCountReader()
    ocr = Digits(*[("2", 0.99)] * 5)
    image = np.full((24, 40, 3), 80, dtype=np.uint8)
    assert reader.read(image, ocr, 0, 0) is None
    assert reader.read(image, ocr, 0, 0) is None
    assert reader.read(image, ocr, 1, 2_000_000_000) is None
    assert reader.read(image, ocr, 2, 2_400_000_000).count == 2
