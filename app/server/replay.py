"""Replay a recorded run at presentation speed.

A live demo can fail for reasons that have nothing to do with the work: a
rate limit, a flaky network, a part that happens not to converge this time.
Replay removes that risk without faking anything - it re-emits the events a
real run produced, against the artifacts that run actually exported.  The
geometry, the source, the renders and the Judge's words are all the originals.

What replay does change is timing.  Real gaps are scaled down and capped, so
a forty-second Opus call does not stall a demo; the recorded durations stay
in each event's own data for anyone who looks.

Replays are marked as such in the UI and on disk.  They are a recording, not
a simulation, and they are never presented as a fresh run.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from .events import EventSink, PHASE_JOB, STATUS_INFO

#: Longest pause between two replayed events, in seconds.
MAX_GAP = 2.5
#: Shortest, so steps stay legible rather than flashing past.
MIN_GAP = 0.18


def is_replayable(job_dir: Path) -> bool:
    """A run can be replayed if it has an event log and at least one version."""
    return ((job_dir / "events.jsonl").is_file()
            and any(job_dir.glob("v*/model.stl")))


def clone_run(source_dir: Path, runs_dir: Path) -> tuple[str, Path]:
    """Copy a recorded run's artifacts into a new job directory.

    Only the version bundles are copied.  The pipeline's scratch directory
    holds the runner scripts and intermediate exports, which a replay has no
    use for.
    """
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-replay-{uuid.uuid4().hex[:4]}"
    target = runs_dir / job_id
    target.mkdir(parents=True, exist_ok=True)

    for version_dir in sorted(source_dir.glob("v*")):
        if version_dir.is_dir():
            shutil.copytree(version_dir, target / version_dir.name,
                            dirs_exist_ok=True)
    return job_id, target


def recorded_events(source_dir: Path) -> list[dict]:
    events: list[dict] = []
    log = source_dir / "events.jsonl"
    if not log.is_file():
        return events
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def scaled_gaps(events: list[dict], speed: float) -> list[float]:
    """Delays to wait before each event, preserving the run's shape."""
    gaps = [0.0]
    for previous, current in zip(events, events[1:]):
        raw = max(0.0, current.get("ts", 0) - previous.get("ts", 0))
        gaps.append(min(MAX_GAP, max(MIN_GAP, raw / max(speed, 0.01))))
    return gaps


def replay_into(
    sink: EventSink,
    source_dir: Path,
    speed: float = 6.0,
    should_stop=None,
) -> int:
    """Re-emit a recorded run's events into ``sink``. Returns the count."""
    events = recorded_events(source_dir)
    if not events:
        sink.emit(PHASE_JOB, STATUS_INFO, "That run has no recorded events.")
        return 0

    gaps = scaled_gaps(events, speed)
    emitted = 0
    for event, gap in zip(events, gaps):
        if should_stop is not None and should_stop():
            break
        if gap:
            time.sleep(gap)
        data = dict(event.get("data") or {})
        data["replayed"] = True
        sink.emit(
            event.get("phase", "log"),
            event.get("status", "info"),
            event.get("message", ""),
            **data,
        )
        emitted += 1
    return emitted
