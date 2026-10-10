from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Protocol, TextIO

from kfocr.domain.models import ConfirmedEvent


class EventSink(Protocol):
    def publish(self, event: ConfirmedEvent) -> None: ...


class ConsoleSink:
    def __init__(self, origin_ns: int = 0, stream: TextIO | None = None):
        self.origin_ns = origin_ns
        self.stream = stream or sys.stdout

    def publish(self, event: ConfirmedEvent) -> None:
        seconds = max(0, (event.observed_at_ns - self.origin_ns) / 1e9)
        minutes, seconds = divmod(seconds, 60)
        actor = event.killer_name or "?"
        victim = event.victim_name or "?"
        if event.killer_team is not None:
            actor = f"[{event.killer_team:02d}] {actor}"
        if event.victim_team is not None:
            victim = f"[{event.victim_team:02d}] {victim}"
        print(f"{int(minutes):02d}:{seconds:06.3f} [{event.event_type.value.upper():7s}] "
              f"{actor} -> {victim} conf={event.confidence_total:.2f}",
              file=self.stream, flush=True)


class JsonlSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")

    def publish(self, event: ConfirmedEvent) -> None:
        from kfocr.storage.export import serialize_event

        self._stream.write(json.dumps(serialize_event(event), ensure_ascii=False) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class MultiSink:
    def __init__(self, *sinks: EventSink):
        self.sinks = sinks

    def publish(self, event: ConfirmedEvent) -> None:
        for sink in self.sinks:
            sink.publish(event)
