import pytest


def test_ocr_sessions_run_one_thread_without_spin_wait():
    pytest.importorskip("rapidocr_onnxruntime")
    from rapidocr_onnxruntime.utils import infer_engine

    from kfocr.vision.text import ENGINE_SETTINGS, _quiet_sessions

    _quiet_sessions()
    _quiet_sessions()  # constructing several recognizers must not stack wrappers
    build = infer_engine.OrtInferSession._init_sess_opts
    assert getattr(build, "kfocr_quiet", False)
    options = build({"intra_op_num_threads": ENGINE_SETTINGS["intra_op_num_threads"],
                     "inter_op_num_threads": 1})
    assert options.intra_op_num_threads == 1
    assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert options.get_session_config_entry("session.inter_op.allow_spinning") == "0"
