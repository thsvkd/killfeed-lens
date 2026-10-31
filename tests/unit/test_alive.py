import numpy as np
import pytest


class Digits:
    def __init__(self, *reads):
        self.reads = iter(reads)

    def read_lines(self, images):
        return [next(self.reads)]


@pytest.mark.parametrize("background", [0, 80, 255])
def test_empty_or_solid_bright_hud_has_no_numeric_crop(background):
    from kfocr.vision.alive import _number_crop

    image = np.full((58, 103, 3), background, dtype=np.uint8)
    assert _number_crop(image, "") is None


def test_unaligned_bright_terrain_and_small_noise_keep_the_original_hud_crop():
    from kfocr.vision.alive import _number_crop

    image = np.full((58, 103, 3), 65, dtype=np.uint8)
    image[3:20, 10:18] = 245
    image[35:52, 50:58] = 245
    image[25, ::3] = 255
    assert _number_crop(image, "") is None


def _hud_digits(text):
    import cv2

    # Hershey glyphs are generated locally: no game screenshot or system font dependency.
    glyphs = np.full((44, 110, 3), 65, dtype=np.uint8)
    cv2.putText(glyphs, text, (0, 34), cv2.FONT_HERSHEY_DUPLEX, 1.3,
                (245, 245, 245), 3, cv2.LINE_AA)
    ys, xs = np.where(glyphs[:, :, 0] > 170)
    glyphs = glyphs[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    width = 28 * len(text) // 3
    image = np.full((58, 103, 3), 65, dtype=np.uint8)
    image[16:40, 84 - width:84] = cv2.resize(glyphs, (width, 24),
                                           interpolation=cv2.INTER_AREA)
    return image


@pytest.mark.parametrize("text", ["100", "10", "99", "1", "0"])
def test_condensed_hud_digits_are_read_without_losing_repeated_zeroes(text):
    """A padded narrow '100' used to read as '10' at 0.90 confidence."""
    pytest.importorskip("rapidocr_onnxruntime")
    from kfocr.vision.alive import AliveCountReader
    from kfocr.vision.text import RapidOCRRecognizer

    image = _hud_digits(text)
    reader, recognizer = AliveCountReader(), RapidOCRRecognizer()
    assert reader.read(image, recognizer, 0, 0) is None
    reading = reader.read(image, recognizer, 1, 400_000_000)
    assert reading is not None and reading.count == int(text)


@pytest.mark.parametrize("brightness", [179, 175, 160, 145, 130])
def test_a_dimmer_last_zero_is_not_trimmed_from_a_correct_hundred(brightness):
    pytest.importorskip("rapidocr_onnxruntime")
    from kfocr.vision.alive import AliveCountReader
    from kfocr.vision.text import RapidOCRRecognizer

    image = _hud_digits("100")
    zero = image[16:40, 75:84].astype(float)
    image[16:40, 75:84] = np.round(65 + (zero - 65) * (brightness - 65) / 180)
    reader, recognizer = AliveCountReader(), RapidOCRRecognizer()
    assert reader.read(image, recognizer, 0, 0) is None
    reading = reader.read(image, recognizer, 1, 400_000_000)
    assert reading is not None and reading.count == 100


@pytest.mark.parametrize("retry", [None, ("100", 0.5), ("1O0", 0.99), ("10", 0.99),
                                  ("100", float("nan")), ("200", 0.99)])
def test_failed_crop_retry_preserves_the_original_count_and_hud_presence(retry):
    from kfocr.vision.alive import AliveCountReader

    class UnreliableCrop:
        def read_lines(self, images):
            if images[0].shape[:2] == (58, 103):
                return [("10", 0.99)]
            if retry is None:
                raise RuntimeError("crop OCR failed")
            return [retry]

    reader, recognizer = AliveCountReader(), UnreliableCrop()
    image = _hud_digits("100")
    assert reader.read(image, recognizer, 0, 0) is None
    reading = reader.read(image, recognizer, 1, 400_000_000)
    assert reading is not None and reading.count == 10
    assert reader.shown_ns == 400_000_000


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
