"""Event schema and sink for streaming live CADSmith pipeline progress.

The pipeline in ``autofab/`` is synchronous and reports progress only by
printing.  This module defines the structured event stream the web app
consumes instead.  Nothing here imports ``autofab`` - the instrumentation
that produces these events lives in ``instrument.py``.

Events are append-only per job.  Each sink keeps the full history in memory
and mirrors it to ``events.jsonl`` on disk, so a browser that reconnects (or
arrives late) can replay the run from sequence 0 without special-casing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


# Pipeline phases, in the order they first occur.  The web UI maps these onto
# its five progress stages; the loop phases (error_fix, refine) can repeat.
PHASE_PLAN = "plan"          # Planner agent: prompt -> design plan
PHASE_CODE = "code"          # Coder agent: design plan -> CadQuery script
PHASE_EXECUTE = "execute"    # Executor: script -> OCCT solid, STEP/STL
PHASE_ERROR_FIX = "error_fix"  # Error Refiner: broken script -> fixed script
PHASE_RENDER = "render"      # VTK three-view render for the Judge
PHASE_JUDGE = "judge"        # Validator: kernel checks + Opus Judge
PHASE_REFINE = "refine"      # Refiner: judge feedback -> corrected script
PHASE_ITERATION = "iteration"  # Outer-loop boundary
PHASE_VERSION = "version"    # A new artifact bundle is available to the client
PHASE_JOB = "job"            # Job lifecycle: queued / running / done / failed
PHASE_LOG = "log"            # Raw console line from the pipeline itself
PHASE_EDIT = "edit"          # Natural-language edit: which path was taken
PHASE_GROUND = "ground"      # Standard dimensions retrieved for the Planner
PHASE_SPEC = "spec"          # Kernel-measured checks against the design plan
PHASE_CATALOG = "catalog"    # A standard part served instead of generated

STATUS_STARTED = "started"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_INFO = "info"


@dataclass
class Event:
    """One observable moment in a pipeline run."""

    seq: int
    ts: float
    phase: str
    status: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_sse(self) -> str:
        """Serialise as a Server-Sent Events frame."""
        return f"id: {self.seq}\ndata: {json.dumps(self.to_dict())}\n\n"


class EventSink:
    """Thread-safe, append-only event log for a single job.

    The pipeline runs on a worker thread and calls :meth:`emit`; the SSE
    endpoint runs on the event loop and calls :meth:`since`.  A lock guards
    the list, which is the only shared state.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._closed = False
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate any log from a previous run of the same job id.
            self.path.write_text("")

    def emit(self, phase: str, status: str, message: str = "", **data: Any) -> Event:
        with self._lock:
            event = Event(
                seq=len(self._events),
                ts=time.time(),
                phase=phase,
                status=status,
                message=message,
                data=data,
            )
            self._events.append(event)
            if self.path:
                # Best effort: a disk problem must never kill a running job.
                try:
                    with self.path.open("a") as f:
                        f.write(json.dumps(event.to_dict()) + "\n")
                except OSError:
                    pass
            return event

    def since(self, seq: int) -> list[Event]:
        """Return every event with sequence number >= ``seq``."""
        with self._lock:
            return self._events[seq:]

    def all(self) -> list[Event]:
        return self.since(0)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @classmethod
    def from_file(cls, path: Path, keep_appending: bool = False) -> "EventSink":
        """Rebuild a sink from a persisted log.

        ``keep_appending`` reopens the log for writing, so a finished run
        restored after a restart can still record later activity - an edit
        applied to it, say - instead of losing those events.  The constructor
        is deliberately bypassed: it truncates, which would erase the history
        being loaded.
        """
        sink = cls(path=None)
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sink._events.append(Event(**raw))
        if keep_appending:
            sink.path = path
            sink._closed = False
        else:
            sink._closed = True
        return sink
