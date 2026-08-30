"""Record demo runs with real geometry and scripted agent replies.

Every artifact these runs produce is genuine: CadQuery builds the solid, the
OCCT kernel measures it, VTK renders the three views the Judge inspects.  What
is *not* genuine is the agent dialogue - the design plan, the CadQuery source
and the Judge's wording are scripted here rather than returned by Claude, so
the app can be demonstrated and tested without an API key.

Runs seeded this way are tagged ``source: "fixture"`` in meta.json and the UI
labels them accordingly.  A run recorded with a real key is tagged ``live``;
once you have one, prefer it for demos.

Usage:
    .venv/bin/python -m app.tools.seed_demo_run            # seed all scenarios
    .venv/bin/python -m app.tools.seed_demo_run --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server.jobs import JobManager, JobOptions, STATUS_DONE, STATUS_ERROR
from app.tests.fake_claude import FakeClaude, patched

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


# --- scenario 1: converges only after the Judge rejects the first attempt ---

BRACKET_PROMPT = (
    "A mounting bracket with a 100mm x 60mm base plate 10mm thick, two "
    "vertical support walls 45mm tall at each end, and four 8mm mounting "
    "holes in a rectangular pattern on the base."
)

BRACKET_PLAN = {
    "description": "L-profile mounting bracket with two end supports and a "
                   "four-hole mounting pattern.",
    "components": ["base plate", "two vertical support walls",
                   "four mounting holes"],
    "dimensions": {
        "overall_bbox": {"xlen": 100, "ylen": 60, "zlen": 55},
        "key_dimensions": {
            "base_length": 100, "base_width": 60, "base_thickness": 10,
            "support_height": 45, "support_thickness": 8, "hole_diameter": 8,
        },
    },
    "constraints": {
        "volume_estimate": 118000, "num_holes": 4, "hole_diameter": 8,
        "symmetry": "mirror about the YZ plane",
    },
    "acceptance_criteria": {"volume_error_threshold_pct": 5,
                            "bbox_iou_threshold": 0.90},
    "notes": "Walls rise in +Z from the base; centre the hole pattern.",
}

# First attempt: the walls are built only as tall as the requested 45mm
# *including* the plate, so the part comes out 10mm short overall.
BRACKET_V0 = """import cadquery as cq

base_length = 100.0
base_width = 60.0
base_thickness = 10.0
support_height = 45.0
support_thickness = 8.0
hole_diameter = 8.0

# Base plate, sitting on the XY plane
result = cq.Workplane('XY').box(
    base_length, base_width, base_thickness, centered=(True, True, False)
)

# Four mounting holes on a rectangular pattern
result = (
    result.faces('>Z').workplane()
    .rect(base_length - 34.0, base_width - 22.0, forConstruction=True)
    .vertices()
    .hole(hole_diameter)
)

# Vertical support walls at each end
for side in (-1, 1):
    wall = (
        cq.Workplane('XY')
        .center(side * (base_length / 2.0 - support_thickness / 2.0), 0)
        .box(support_thickness, base_width, support_height,
             centered=(True, True, False))
    )
    result = result.union(wall)
"""

# Refined: the walls rise 45mm *above* the plate, as the prompt specifies.
BRACKET_V1 = BRACKET_V0.replace(
    "        .box(support_thickness, base_width, support_height,",
    "        .box(support_thickness, base_width, support_height + base_thickness,",
)

BRACKET_VERDICTS = [
    (False,
     "The overall height measures 45.0mm but the prompt requires supports "
     "45mm tall standing on a 10mm base plate, so the part should reach "
     "55mm in Z. In the front profile view the walls are clearly flush with "
     "the top of the plate rather than rising above it. Extend the wall "
     "height to support_height + base_thickness."),
    (True,
     "All constraints met. The bounding box is 100.0 x 60.0 x 55.0mm, "
     "matching a 10mm base with 45mm walls. Four through holes are visible "
     "in the high-angle rear view on a rectangular pattern, and the solid "
     "is watertight."),
]

# --- scenario 2: converges on the first attempt ---

WASHER_PROMPT = (
    "A flat washer: outer diameter 20mm, inner diameter 10.5mm, thickness "
    "2mm. The washer lies flat on the XY plane, centered at the origin, "
    "with thickness extruded in the +Z direction."
)

WASHER_PLAN = {
    "description": "Flat annular washer.",
    "components": ["annular disc"],
    "dimensions": {
        "overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2},
        "key_dimensions": {"outer_diameter": 20, "inner_diameter": 10.5,
                           "thickness": 2},
    },
    "constraints": {"volume_estimate": 455, "num_holes": 1,
                    "hole_diameter": 10.5, "symmetry": "axisymmetric about Z"},
    "acceptance_criteria": {"volume_error_threshold_pct": 5,
                            "bbox_iou_threshold": 0.90},
    "notes": "Two concentric circles extruded in +Z.",
}

WASHER_V0 = """import cadquery as cq

outer_diameter = 20.0
inner_diameter = 10.5
thickness = 2.0

# Concentric circles extruded to form an annulus
result = (
    cq.Workplane('XY')
    .circle(outer_diameter / 2.0)
    .circle(inner_diameter / 2.0)
    .extrude(thickness)
)
"""

WASHER_VERDICTS = [
    (True,
     "All constraints met. The kernel reports a 20.0 x 20.0 x 2.0mm bounding "
     "box and a volume of 455.5mm3, consistent with an annulus of 20mm outer "
     "and 10.5mm inner diameter. The central bore is clearly visible in the "
     "high-angle view and the solid is watertight."),
]


SCENARIOS = {
    "bracket": {
        "prompt": BRACKET_PROMPT,
        "plan": BRACKET_PLAN,
        "code": [BRACKET_V0, BRACKET_V1],
        "verdicts": BRACKET_VERDICTS,
        "options": JobOptions(max_iterations=3, use_vision=True),
        "note": "two iterations - the Judge rejects the first attempt",
    },
    "washer": {
        "prompt": WASHER_PROMPT,
        "plan": WASHER_PLAN,
        "code": [WASHER_V0],
        "verdicts": WASHER_VERDICTS,
        "options": JobOptions(max_iterations=3, use_vision=True),
        "note": "converges on the first attempt",
    },
}


def seed(name: str, manager: JobManager) -> str:
    scenario = SCENARIOS[name]
    fake = FakeClaude(
        plan=scenario["plan"],
        code=list(scenario["code"]),
        verdicts=list(scenario["verdicts"]),
    )

    print(f"  seeding '{name}' ({scenario['note']}) …", flush=True)
    with patched(fake):
        job = manager.create(scenario["prompt"], scenario["options"])
        deadline = time.time() + 600
        while job.status not in (STATUS_DONE, STATUS_ERROR):
            if time.time() > deadline:
                raise TimeoutError(f"Scenario '{name}' did not finish in time.")
            time.sleep(0.4)

    if job.status == STATUS_ERROR:
        raise RuntimeError(f"Scenario '{name}' failed: {job.error}")

    # Mark provenance: the geometry is real, the agent dialogue was scripted.
    job.source = "fixture"
    meta_file = job.directory / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta["source"] = "fixture"
    meta["fixture_note"] = (
        "Real CadQuery geometry and real VTK renders; agent replies were "
        "scripted by app/tools/seed_demo_run.py, not returned by Claude."
    )
    meta_file.write_text(json.dumps(meta, indent=2))

    print(f"    -> {job.id}: converged={job.converged}, "
          f"{len(job.versions)} version(s)")
    return job.id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="List the available scenarios and exit.")
    parser.add_argument("--only", nargs="+", choices=sorted(SCENARIOS),
                        help="Seed only these scenarios.")
    args = parser.parse_args()

    if args.list:
        for name, scenario in SCENARIOS.items():
            print(f"{name:10} {scenario['note']}")
        return 0

    manager = JobManager(RUNS_DIR)
    print(f"Recording demo runs into {RUNS_DIR}")
    for name in (args.only or list(SCENARIOS)):
        seed(name, manager)
    print("\nDone. Open the app and pick a run from History.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
