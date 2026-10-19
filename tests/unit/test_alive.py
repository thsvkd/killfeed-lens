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


def test_a_lost_digit_or_a_change_faster_than_deaths_and_recalls_is_not_a_count():
    from kfocr.vision.alive import AliveCountReader

    reader = AliveCountReader()
    s = 1_000_000_000
    reads = [("44", 0.75), ("44", 0.8), ("4", 0.9), ("4", 0.9),  # "44" with a digit lost
             ("30", 0.9), ("30", 0.9),  # 14 deaths in a second: no
             ("41", 0.9), ("41", 0.9),  # 3 deaths: yes
             ("45", 0.9), ("45", 0.9)]  # a recall brings players back
    ocr = Digits(*reads)
    image = np.full((24, 40, 3), 80, dtype=np.uint8)
    results = [reader.read(image, ocr, i, i * s // 2) for i in range(len(reads))]
    assert [r.count if r else None for r in results] == [None, 44, None, None, None, None,
                                                         None, 41, None, 45]
    # a minute on, the last count is too old to judge the next one
    ocr = Digits(("4", 0.9), ("4", 0.9))
    assert reader.read(image, ocr, 20, 90 * s) is None
    assert reader.read(image, ocr, 21, 90 * s + s // 2).count == 4

    # "19" read "9" ten seconds on: 10 deaths in that time may be, a lost digit is not
    reader = AliveCountReader()
    ocr = Digits(("19", 0.9), ("19", 0.9), ("9", 0.9), ("9", 0.9))
    counts = [reader.read(image, ocr, i, at) for i, at in enumerate((0, s // 2, 10 * s, 10 * s + s // 2))]
    assert [r.count if r else None for r in counts] == [None, 19, None, None]


def test_a_misread_rise_never_becomes_the_reference_nor_a_misread_first_count():
    from kfocr.vision.alive import AliveCountReader

    s = 1_000_000_000
    image = np.full((24, 40, 3), 80, dtype=np.uint8)

    def counts(reads):
        ocr = Digits(*[(text, 0.9) for _, text in reads])
        return [r.count if r else None for r in (reader.read(image, ocr, seq, at)
                                                 for seq, (at, _) in enumerate(reads))]

    reader = AliveCountReader()
    # 61 misread "81" (20 back at once: no), "67" (6 back: may be a recall), then 61 again
    assert counts([(0, "61"), (s // 2, "61"), (s, "81"), (3 * s // 2, "81"), (2 * s, "61"),
                   (5 * s // 2, "61"), (3 * s, "67"), (7 * s // 2, "67"), (4 * s, "61"),
                   (9 * s // 2, "61")]) == [None, 61, None, None, None, 61, None, 67, None, 61]
    # the HUD back after a minute with "96" read "9": the full number still gets in
    reader = AliveCountReader()
    assert counts([(0, "9"), (s // 2, "9"), (s, "96"), (3 * s // 2, "96"), (2 * s, "9"),
                   (5 * s // 2, "9"), (3 * s, "95"), (7 * s // 2, "95")]) == [
        None, 9, None, 96, None, None, None, 95]
