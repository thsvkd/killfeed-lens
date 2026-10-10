"""Small structured logs that avoid sensitive request payloads."""

import json
import logging


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    # HTTP debug traces can contain Authorization headers. Never enable them.
    for name in ("httpx", "httpcore", "onnxruntime"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(name: str, **fields) -> None:
    logging.getLogger("kfocr").info(json.dumps({"event": name, **fields}, default=str))

