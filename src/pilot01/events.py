"""Append-only event model and JSONL serialization.

Every run produces one ordered event stream. The stream is the primary audit
artifact of the experiment: it records what each node was given, what it
returned, and which transition the runner took.

Immutability is structural, not conventional:

* :class:`Event` is a frozen pydantic model;
* :class:`EventCollector` exposes no update, delete, reorder or truncate
  operation, and hands out an immutable tuple snapshot;
* ``event_id`` is a dense, monotonic sequence, so append order is recoverable
  from the log itself.

No database, no async, no external sink. A JSONL writer is all Gate 1 needs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EventType",
    "Event",
    "EventCollector",
    "utc_now",
]


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    AGENT_INPUT_CREATED = "agent_input_created"
    AGENT_OUTPUT_RECEIVED = "agent_output_received"
    TRANSITION = "transition"
    PROTOCOL_ERROR = "protocol_error"
    RUN_COMPLETED = "run_completed"


def utc_now() -> datetime:
    """Default clock: timezone-aware UTC."""
    return datetime.now(timezone.utc)


class Event(BaseModel):
    """One immutable log record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: int = Field(ge=0, description="Dense, monotonic, per-run sequence.")
    run_id: str
    event_type: EventType
    node: str | None = None
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class EventCollector:
    """An in-memory append-only event sink.

    The clock is injectable so runs can be made byte-for-byte reproducible in
    tests. Payloads are stored as plain JSON-compatible dicts; callers are
    responsible for ``model_dump(mode="json")``-ing anything richer.
    """

    def __init__(self, run_id: str, clock: Callable[[], datetime] | None = None) -> None:
        self._run_id = run_id
        self._clock = clock or utc_now
        self._events: list[Event] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    def append(
        self,
        event_type: EventType,
        *,
        node: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Append one event and return it.

        This is the only mutating operation on the collector.
        """
        timestamp = self._clock()
        if timestamp.tzinfo is None:
            raise ValueError("event clock must return a timezone-aware datetime")
        event = Event(
            event_id=len(self._events),
            run_id=self._run_id,
            event_type=event_type,
            node=node,
            timestamp=timestamp,
            payload=payload or {},
        )
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[Event, ...]:
        """Immutable snapshot, in append order."""
        return tuple(self._events)

    def of_type(self, event_type: EventType) -> tuple[Event, ...]:
        return tuple(event for event in self._events if event.event_type is event_type)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def to_jsonl(self) -> str:
        """Serialize the whole stream as JSON Lines (one event per line)."""
        return "".join(f"{event.model_dump_json()}\n" for event in self._events)

    def write_jsonl(self, path: str | Path) -> Path:
        """Write the stream to ``path``, creating parent directories."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_jsonl(), encoding="utf-8")
        return destination
