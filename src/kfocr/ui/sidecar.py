from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, TextIO

from kfocr.domain.models import AliveCount, ConfirmedEvent


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

    def publish_alive_count(self, reading: AliveCount) -> None:
        seconds = max(0, (reading.observed_at_ns - self.origin_ns) / 1e9)
        print(f"{seconds:09.3f} [ALIVE  ] {reading.count}", file=self.stream, flush=True)


class JsonlSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")

    def publish(self, event: ConfirmedEvent) -> None:
        from kfocr.storage.export import serialize_event

        self._stream.write(json.dumps(serialize_event(event), ensure_ascii=False) + "\n")
        self._stream.flush()

    def publish_alive_count(self, reading: AliveCount) -> None:
        self._stream.write(json.dumps({"type": "alive_count", **asdict(reading)}) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class MultiSink:
    def __init__(self, *sinks: EventSink):
        self.sinks = sinks

    def publish(self, event: ConfirmedEvent) -> None:
        for sink in self.sinks:
            sink.publish(event)

    def publish_alive_count(self, reading: AliveCount) -> None:
        for sink in self.sinks:
            publish = getattr(sink, "publish_alive_count", None)
            if publish is not None:
                publish(reading)
