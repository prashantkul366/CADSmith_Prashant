"""End-to-end check of both natural-language edit paths.

The parameter-patch path must rebuild real geometry without calling a model
at all; the Refiner path must call one and then face the full validation.
Only the network is faked - CadQuery rebuilds the solid either way.

Run:  .venv/bin/python -m app.tests.test_edit_flow
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used-network-is-faked"

from app.server.jobs import JobManager, JobOptions, STATUS_DONE, STATUS_ERROR  # noqa: E402
from app.tests.fake_claude import FakeClaude, patched  # noqa: E402

PROMPT = "A flat washer, 20mm outer diameter, 10.5mm bore, 2mm thick."

PLAN = {
    "description": "Flat washer",
    "components": ["annular disc"],
    "dimensions": {"overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2},
                   "key_dimensions": {"outer_diameter": 20, "bore": 10.5,
                                      "thickness": 2}},
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

# What the Refiner returns for a structural request it cannot express as a
# parameter change: a chamfered top edge.
REFINED = CODE.replace(
    ")\n)\n",
    ")\n).faces('>Z').edges().chamfer(0.4)\n",
).replace(
    "    .extrude(thickness)\n)",
    "    .extrude(thickness)\n).faces('>Z').edges('%Circle').chamfer(0.4)",
)


def wait_for(job, timeout=300):
    deadline = time.time() + timeout
    while job.status not in (STATUS_DONE, STATUS_ERROR):
        if time.time() > deadline:
            raise TimeoutError("job did not finish")
        time.sleep(0.3)


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    runs = Path(tempfile.mkdtemp(prefix="cadsmith_edits_"))
    manager = JobManager(runs)

    fake = FakeClaude(plan=PLAN, code=[CODE],
                      verdicts=[(True, "All constraints met.")])

    print("\nInitial run")
    with patched(fake):
        job = manager.create(PROMPT, JobOptions(max_iterations=1, use_vision=False))
        wait_for(job)
    check("job converged", job.converged)
    check("one version", len(job.versions) == 1, str(len(job.versions)))
    baseline = json.loads((job.directory / "v0" / "geometry.json").read_text())
    check("baseline is 2mm thick",
          abs(baseline["bounding_box"]["zlen"] - 2.0) < 1e-6)

    print("\nParameter patch (no model call)")
    calls_before = len(fake.calls)
    with patched(fake):
        manager.submit_edit(job, "make it 4mm thick")
        wait_for(job)

    check("no model was called", len(fake.calls) == calls_before,
          f"{fake.calls[calls_before:]}")
    check("a new version exists", len(job.versions) == 2, str(len(job.versions)))

    edited = job.versions[-1]
    check("version marked as an edit", edited["source"] == "edit",
          edited.get("source"))
    check("method reported", edited["method"] == "parameter patch",
          edited.get("method"))
    check("the change is recorded",
          edited["changes"] and edited["changes"][0]["name"] == "thickness"
          and edited["changes"][0]["new"] == 4.0,
          str(edited.get("changes")))

    geometry = json.loads(
        (job.directory / f"v{edited['iteration']}" / "geometry.json").read_text())
    check("kernel rebuilt it at 4mm",
          abs(geometry["bounding_box"]["zlen"] - 4.0) < 1e-6,
          f"zlen={geometry['bounding_box']['zlen']}")
    check("diameter untouched",
          abs(geometry["bounding_box"]["xlen"] - 20.0) < 1e-6)
    check("still watertight", geometry["is_valid"])
    check("patched source saved",
          "thickness = 4.0" in
          (job.directory / f"v{edited['iteration']}" / "code.py").read_text())
    check("STL exported for the edit",
          (job.directory / f"v{edited['iteration']}" / "model.stl").exists())
    check("Judge was skipped for a patch",
          edited["judge_passed"] is None, str(edited.get("judge_passed")))

    print("\nStructural edit (Refiner agent)")
    fake.code.append(REFINED)
    fake.verdicts.append((True, "The chamfer is present on the top edge."))
    calls_before = len(fake.calls)
    with patched(fake):
        manager.submit_edit(job, "add a small chamfer to the top edge")
        wait_for(job)

    check("the Refiner was called",
          fake.calls[calls_before:calls_before + 1] == ["refiner"],
          str(fake.calls[calls_before:]))
    check("a third version exists", len(job.versions) == 3, str(len(job.versions)))

    refined = job.versions[-1]
    check("method reported as the agent", refined["method"] == "refiner agent",
          refined.get("method"))
    check("the Judge did run on the agent path",
          refined["judge_passed"] is True, str(refined.get("judge_passed")))
    refined_geometry = json.loads(
        (job.directory / f"v{refined['iteration']}" / "geometry.json").read_text())
    check("chamfer added faces",
          refined_geometry["num_faces"] > geometry["num_faces"],
          f"{geometry['num_faces']} → {refined_geometry['num_faces']}")

    print("\nAn unbuildable change is reported, not silently kept")
    versions_before = len(job.versions)
    with patched(fake):
        manager.submit_edit(job, "set the bore to 19.9mm")
        wait_for(job)
    events = manager.sink(job.id).all()
    last_job_event = [e for e in events if e.phase == "job"][-1]
    unchanged = len(job.versions) == versions_before
    check("either it built, or it was reported and nothing was lost",
          (not unchanged) or last_job_event.status == "failed",
          f"versions={len(job.versions)}, last={last_job_event.status}")

    print("\nA Refiner that hands back an earlier version is refused")
    # Observed live against Qwen3-VL-8B: asked to thicken a wall after two
    # earlier edits, it answered from the original prompt and returned v0
    # byte-for-byte. Recording that would have undone both edits silently.
    from app.server import jobs as _jobs

    first_code = (job.directory / "v0" / "code.py").read_text()
    latest = job.versions[-1]["iteration"]
    check("a verbatim earlier version is spotted",
          _jobs._reverted_version(job, first_code, latest) == 0,
          str(_jobs._reverted_version(job, first_code, latest)))
    check("reformatting does not hide it",
          _jobs._reverted_version(
              job, first_code.replace("\n", "  \n"), latest) == 0)
    check("the version being edited is not itself a revert",
          _jobs._reverted_version(
              job, (job.directory / f"v{latest}" / "code.py").read_text(),
              latest) is None)
    check("genuinely new code is allowed through",
          _jobs._reverted_version(job, first_code + "\nresult = result\n",
                                  latest) is None)

    versions_before = len(job.versions)
    # The Refiner draws from the code queue, so scripting v0's code there is
    # exactly the failure seen live.
    reverting = FakeClaude(plan=PLAN, code=[first_code],
                           verdicts=[(True, "All constraints met.")])
    with patched(reverting):
        manager.submit_edit(job, "add a fillet all round")
        wait_for(job)
    events = manager.sink(job.id).all()
    last = [e for e in events if e.phase == "job"][-1]
    check("no version is recorded for the revert",
          len(job.versions) == versions_before,
          f"{versions_before} -> {len(job.versions)}")
    check("and the person is told what happened",
          last.status == "failed" and "undone your earlier edits" in last.message,
          f"{last.status}: {last.message[:140]}")

    shutil.rmtree(runs, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
