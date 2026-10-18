import cv2
import numpy as np

from kfocr.vision.change import EdgeChangeDetector


def _frame(k: int, rows: int, rng: np.random.Generator) -> np.ndarray:
    """Killfeed rows over a game background that moves every frame (stripes + bright specks)."""
    img = np.full((120, 400, 3), 70, np.uint8)
    img[:, :, 1] = (70 + 40 * np.sin((np.arange(400) + 7 * k) / 15)).astype(np.uint8)
    img[rng.integers(0, 120, 60), rng.integers(0, 400, 60)] = 255
    for i in range(rows):
        cv2.putText(img, "PLAYER_ONE  >  PLAYER_TWO", (10, 25 + 30 * i),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    return img


def test_ink_gate_ignores_a_moving_background_but_reads_a_new_row():
    rng = np.random.default_rng(0)
    detector = EdgeChangeDetector()
    read = _frame(0, 2, rng)
    detector.score(read, read)
    static = [detector.score(read, _frame(k, 2, rng)) for k in range(1, 6)]
    assert not any(detector.should_infer(s) for s in static)  # same rows, moving game behind
    appeared = [detector.score(read, _frame(k, 3, rng)) for k in (6, 7)]
    assert detector.should_infer(appeared[1])  # a new row counts once it held for two frames
