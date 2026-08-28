"""Small data objects shared by the prototype services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceInput:
    """A piece of source material attached to an event."""

    content: str
    artifact_uri: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True)
class Event:
    """An immutable event returned from the event store."""

    id: str
    session_id: str
    seq: int
    event_type: str
    payload: Any
    occurred_at: str
    content_hash: str

