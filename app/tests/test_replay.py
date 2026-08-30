"""Check that a replay is a faithful recording, not a re-run.

The point of replay is that a demo cannot fail for reasons unrelated to the
work.  That only holds if a replay reproduces the original's events and
artifacts exactly, calls no model, and is never presented as a fresh run.

Run:  .venv/bin/python -m app.tests.test_replay
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used-network-is-faked"

from app.server.jobs import JobManager, JobOptions, STATUS_DONE, STATUS_ERROR  # noqa: E402
from app.server.replay import MAX_GAP, scaled_gaps  # noqa: E402
from app.tests.fake_claude import FakeClaude, patched  # noqa: E402

PROMPT = "A flat washer, 20mm outer diameter, 10.5mm bore, 2mm thick."

PLAN = {
    "description": "Flat washer",
    "components": ["annular disc"],
    "dimensions": {"overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2},
                   "key_dimensions": {"outer_diameter": 20, "thickness": 2}},
    "constraints": {"num_holes": 1},
    "acceptance_criteria": {"volume_error_threshold_pct": 5},
    "notes": "Concentric circles extruded.",
}

CODE = """import cadquery as cq

outer_diameter = 20.0
bore = 10.5
thickness = 2.0

result = (
    cq.Workplane('XY')
    .circle(outer_diameter / 2.0)
    .circle(bore / 2.0)
    .extrude(thickness)
)
"""

VERDICT = "All constraints met. The bore is clear and the solid is watertight."


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(job, timeout=300):
    deadline = time.time() + timeout
    while job.status not in (STATUS_DONE, STATUS_ERROR):
        if time.time() > deadline:
            raise TimeoutError("job did not finish")
        time.sleep(0.2)


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    print("\nPacing")
    events = [{"ts": 0.0}, {"ts": 40.0}, {"ts": 40.05}, {"ts": 42.0}]
    gaps = scaled_gaps(events, speed=6.0)
    check("first event is immediate", gaps[0] == 0.0)
    check("a long agent call is capped", gaps[1] == MAX_GAP, f"{gaps[1]}s")
    check("a fast step stays legible", gaps[2] >= 0.18, f"{gaps[2]}s")
    check("total is demo-length", sum(gaps) < 8.0, f"{sum(gaps):.1f}s")

    runs = Path(tempfile.mkdtemp(prefix="cadsmith_replay_"))
    manager = JobManager(runs)

    print("\nRecording a run")
    fake = FakeClaude(plan=PLAN, code=[CODE], verdicts=[(True, VERDICT)])
    with patched(fake):
        original = manager.create(
            PROMPT, JobOptions(max_iterations=1, use_vision=True))
        wait_for(original)
    check("original converged", original.converged)
    original_events = manager.sink(original.id).all()
    check("events recorded", len(original_events) > 5, str(len(original_events)))
    check("replayable", manager.can_replay(original))

    print("\nReplaying it")
    calls_before = len(fake.calls)
    started = time.time()
    replay = manager.create_replay(original, speed=40.0)
    wait_for(replay)
    elapsed = time.time() - started

    check("no model was called", len(fake.calls) == calls_before,
          str(fake.calls[calls_before:]))
    check("it is a distinct job", replay.id != original.id)
    check("marked as a replay", replay.source == "replay", replay.source)
    check("it names what it replays", replay.replay_of == original.id)
    check("finished at demo speed", elapsed < 60, f"{elapsed:.1f}s")

    replay_events = manager.sink(replay.id).all()
    original_signature = [(e.phase, e.status) for e in original_events]
    replay_signature = [(e.phase, e.status) for e in replay_events]
    # The replay adds its own queued/finished bookends around the recording.
    check("every recorded event is re-emitted, in order",
          original_signature == replay_signature[1:-1],
          f"{len(original_signature)} vs {len(replay_signature) - 2}")
    check("replayed events are labelled",
          all(e.data.get("replayed") for e in replay_events[1:-1]))

    print("\nArtifacts are the originals")
    for artifact in ("model.stl", "model.step", "code.py", "render.png",
                     "geometry.json", "validation.json"):
        source = original.directory / "v0" / artifact
        copy = replay.directory / "v0" / artifact
        check(f"v0/{artifact} identical",
              copy.exists() and digest(source) == digest(copy),
              "missing" if not copy.exists() else "")

    judge_text = [e for e in replay_events if e.phase == "judge"
                  and e.status == "ok"]
    check("the Judge's own words are replayed",
          judge_text and VERDICT in judge_text[0].message)

    print("\nThe original is untouched")
    check("original still has one version", len(original.versions) == 1)
    check("original's scratch dir was not copied",
          not (replay.directory / "work").exists())

    shutil.rmtree(runs, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
