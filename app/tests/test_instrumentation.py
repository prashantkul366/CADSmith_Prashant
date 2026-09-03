"""End-to-end check of the instrumentation layer against the real CAD kernel.

Only the Anthropic HTTP call is faked.  CadQuery/OCCT executes for real, VTK
renders for real, and the stock ``autofab`` pipeline drives the whole thing -
so this exercises the refinement loop, the artifact bundles and the event
stream exactly as the web app will.

Run:  .venv/bin/python -m app.tests.test_instrumentation
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server.events import (
    EventSink,
    PHASE_EXECUTE,
    PHASE_JUDGE,
    PHASE_RENDER,
    PHASE_VERSION,
    STATUS_FAILED,
    STATUS_OK,
)
from app.server.instrument import (
    InstrumentedPipeline,
    RunContext,
    install_agent_hooks,
    set_context,
)
from app.tests.fake_claude import FakeClaude, patched

PROMPT = (
    "A rectangular mounting plate 80mm x 60mm x 4mm thick with four "
    "3.4mm holes in a 70mm x 50mm pattern."
)

PLAN = {
    "description": "Rectangular mounting plate with four clearance holes",
    "components": ["base plate", "four M3 clearance holes"],
    "dimensions": {
        "overall_bbox": {"xlen": 80, "ylen": 60, "zlen": 4},
        "key_dimensions": {"length": 80, "width": 60, "thickness": 4, "hole_dia": 3.4},
    },
    "constraints": {"num_holes": 4, "hole_diameter": 3.4},
    "acceptance_criteria": {"volume_error_threshold_pct": 5},
    "notes": "Centre the hole pattern on the plate.",
}

# First attempt is deliberately wrong (2mm thick) so the Judge rejects it and
# the refinement loop has to run - the behaviour the whole app is built around.
CODE_WRONG = """import cadquery as cq

length, width, thickness = 80.0, 60.0, 2.0
hole_dia = 3.4

result = (
    cq.Workplane('XY')
    .box(length, width, thickness, centered=(True, True, False))
    .faces('>Z').workplane()
    .rect(70.0, 50.0, forConstruction=True)
    .vertices()
    .hole(hole_dia)
)
"""

CODE_RIGHT = CODE_WRONG.replace("thickness = 80.0, 60.0, 2.0", "thickness = 80.0, 60.0, 4.0")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cadsmith_test_"))
    job_dir = work / "job"
    job_dir.mkdir(parents=True)

    install_agent_hooks()
    sink = EventSink(path=job_dir / "events.jsonl")
    ctx = RunContext(sink=sink, job_dir=job_dir, part_name="plate")
    set_context(ctx)

    fake = FakeClaude(
        plan=PLAN,
        code=[CODE_WRONG, CODE_RIGHT],
        verdicts=[
            (False, "Thickness measures 2mm but the prompt specifies 4mm."),
            (True, "All constraints met."),
        ],
    )

    pipeline = InstrumentedPipeline(
        output_dir=str(job_dir / "work"),
        max_error_retries=3,
        max_refinement_iterations=5,
        verbose=False,
        use_vision=True,
    )

    with patched(fake):
        result = pipeline.run(PROMPT, name="plate")

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    print("\nPipeline outcome")
    check("converged", result.converged)
    check("two iterations ran", len(result.iterations) == 2,
          f"got {len(result.iterations)}")
    check("agent call order", fake.calls == [
        "planner", "coder", "judge", "refiner", "judge"],
        str(fake.calls))

    geometry = result.final_geometry or {}
    bbox = geometry.get("bounding_box", {})
    check("final thickness is 4mm (refinement applied)",
          abs(bbox.get("zlen", 0) - 4.0) < 1e-6, f"zlen={bbox.get('zlen')}")
    check("final solid is watertight", bool(geometry.get("is_valid")))

    print("\nEvent stream")
    events = sink.all()
    kinds = [(e.phase, e.status) for e in events]
    check("execute succeeded twice",
          kinds.count((PHASE_EXECUTE, STATUS_OK)) == 2)
    check("judge ran twice", kinds.count((PHASE_JUDGE, STATUS_OK)) == 2)
    check("three-view render produced",
          (PHASE_RENDER, STATUS_OK) in kinds,
          "VTK offscreen unavailable" if (PHASE_RENDER, STATUS_FAILED) in kinds else "")
    check("no failed events", not [k for k in kinds if k[1] == STATUS_FAILED],
          str([e.message[:60] for e in events if e.status == STATUS_FAILED]))
    versions = [e for e in events if e.phase == PHASE_VERSION]
    check("two version bundles announced", len(versions) == 2)
    check("first version rejected, second passed",
          len(versions) == 2
          and versions[0].data["passed"] is False
          and versions[1].data["passed"] is True)
    check("events persisted to disk", (job_dir / "events.jsonl").exists()
          and len((job_dir / "events.jsonl").read_text().splitlines()) == len(events))

    print("\nArtifact bundles")
    for n in (0, 1):
        vdir = job_dir / f"v{n}"
        for artifact in ("code.py", "model.stl", "model.step",
                         "geometry.json", "validation.json", "render.png"):
            path = vdir / artifact
            check(f"v{n}/{artifact}", path.exists() and path.stat().st_size > 0,
                  f"{path.stat().st_size}B" if path.exists() else "missing")

    print("\nSilent-pass correction (validator.py:174)")
    sink2 = EventSink(path=None)
    ctx2 = RunContext(sink=sink2, job_dir=work / "job2", part_name="plate")
    set_context(ctx2)
    # Planner and Coder answer normally; only the Judge call fails, which is
    # what a rate limit or network blip looks like mid-run.
    broken = JudgeOutage(plan=PLAN, code=[CODE_RIGHT], verdicts=[])
    pipeline2 = InstrumentedPipeline(
        output_dir=str(work / "job2" / "work"),
        max_refinement_iterations=0, verbose=False, use_vision=False,
    )
    with patched(broken):
        result2 = pipeline2.run(PROMPT, name="plate")
    check("a failing Judge does NOT silently converge", not result2.converged)
    check("the outage is reported as a failed judge event",
          (PHASE_JUDGE, STATUS_FAILED) in [(e.phase, e.status) for e in sink2.all()])

    print("\nA prompt with no part in it is refused, not built")
    # A smaller model answers "write me a poem" by filling the plan schema in
    # rather than declining, so the JSONDecodeError path never fires. Observed
    # live against Qwen3-VL-8B: the app spent 10 model calls and 280 seconds
    # producing a featureless block.
    from autofab import agents as _agents
    from app.server.instrument import PipelineMessage

    empty_plan = {
        "description": "A poetic description of the sea.",
        "components": [],
        "dimensions": {"overall_bbox": {"xlen": 0, "ylen": 0, "zlen": 0},
                       "key_dimensions": {}},
        "notes": "This is a poetic request, not a 3D part.",
    }

    for label, ground in (("with grounding on", True),
                          ("with grounding off", False)):
        sink3 = EventSink(path=job_dir / f"events_empty_{int(ground)}.jsonl")
        set_context(RunContext(sink=sink3, job_dir=job_dir, part_name="x",
                               ground_dimensions=ground))
        poet = FakeClaude(plan=empty_plan, code=[CODE_RIGHT],
                          verdicts=[(True, "ok")])
        try:
            with patched(poet):
                _agents.plan("write me a poem about the sea")
            check(f"refused {label}", False, "no error raised")
        except PipelineMessage as exc:
            check(f"refused {label}", True)
            check(f"the message names the problem {label}",
                  "no components and no overall size" in str(exc),
                  str(exc)[:120])
            check(f"and quotes the Planner's own note {label}",
                  "not a 3D part" in str(exc), str(exc)[:200])
        except Exception as exc:                       # noqa: BLE001
            check(f"refused {label}", False,
                  f"{type(exc).__name__}: {exc}")

    print("\nA real plan is still allowed through")
    sink4 = EventSink(path=job_dir / "events_realplan.jsonl")
    set_context(RunContext(sink=sink4, job_dir=job_dir, part_name="x"))
    with patched(FakeClaude(plan=PLAN, code=[CODE_RIGHT],
                            verdicts=[(True, "ok")])):
        allowed = _agents.plan(PROMPT)
    check("a sized plan is returned untouched", allowed == PLAN)

    for label, plan in (
        ("a component with no bbox is a part", {
            "components": ["boss"],
            "dimensions": {"overall_bbox": {"xlen": 0, "ylen": 0, "zlen": 0}}}),
        ("a bbox with no components is a part", {
            "components": [],
            "dimensions": {"overall_bbox": {"xlen": 10, "ylen": 0, "zlen": 0}}}),
        ("a plan missing dimensions entirely", {"components": ["boss"]}),
    ):
        sink5 = EventSink(path=job_dir / "events_probe.jsonl")
        set_context(RunContext(sink=sink5, job_dir=job_dir, part_name="x"))
        try:
            with patched(FakeClaude(plan=plan, code=[CODE_RIGHT],
                                    verdicts=[(True, "ok")])):
                _agents.plan(PROMPT)
            check(label, True)
        except PipelineMessage as exc:
            check(label, False, f"wrongly refused: {exc}")

    set_context(None)
    shutil.rmtree(work, ignore_errors=True)

    print(f"\n{'='*58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


class JudgeOutage(FakeClaude):
    """Answers every agent normally except the Judge, which times out."""

    def create(self, *, system: str = "", **kwargs):
        if self._route(system) == "judge":
            raise RuntimeError("simulated API outage")
        return super().create(system=system, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
