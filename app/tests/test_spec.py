"""Kernel-measured specification checks, against real built solids.

Nothing here is mocked: every case builds geometry with CadQuery and measures
it the way the app does.  Several cases are lifted from runs where the vision
Judge got the answer wrong, which is the whole reason this layer exists.

Run:  .venv/bin/python -m app.tests.test_spec
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cadquery as cq  # noqa: E402

from app.server import spec  # noqa: E402


def plate(holes: int = 4, diameter: float = 6.0):
    """80 x 60 x 8 plate with `holes` of the corner holes actually cut."""
    corners = [(-30, -20), (30, -20), (30, 20), (-30, 20)][:holes]
    part = cq.Workplane("XY").box(80, 60, 8)
    if corners:
        part = (part.faces(">Z").workplane()
                .pushPoints(corners).hole(diameter))
    return part


PLATE_PLAN = {
    "dimensions": {"overall_bbox": {"xlen": 80, "ylen": 60, "zlen": 8}},
    "constraints": {"num_holes": 4, "hole_diameter": 6},
}


def main() -> int:
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="cadsmith_spec_"))

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    def as_step(part, name: str) -> Path:
        path = work / f"{name}.step"
        cq.exporters.export(part, str(path))
        return path

    # -- counting bores ----------------------------------------------------
    print("\nCounting holes in a real solid")

    check("four holes are counted as four",
          spec.bores(plate(4).val()) == [6.0, 6.0, 6.0, 6.0],
          str(spec.bores(plate(4).val())))
    check("one hole is counted as one",
          spec.bores(plate(1).val()) == [6.0])
    check("a plain block has none",
          spec.bores(cq.Workplane("XY").box(20, 20, 20).val()) == [])

    tube = cq.Workplane("XY").circle(20).circle(15).extrude(50)
    check("a tube's bore counts, its outside diameter does not",
          spec.bores(tube.val()) == [30.0], str(spec.bores(tube.val())))

    shaft = cq.Workplane("XY").circle(10).extrude(40)
    check("a plain shaft has no hole",
          spec.bores(shaft.val()) == [], str(spec.bores(shaft.val())))

    stepped = (cq.Workplane("XY").circle(15).extrude(40)
               .faces(">Z").workplane().circle(10).extrude(60))
    check("neither does a stepped shaft",
          spec.bores(stepped.val()) == [], str(spec.bores(stepped.val())))

    filleted = cq.Workplane("XY").box(40, 40, 10).edges("|Z").fillet(4)
    check("corner fillets are not mistaken for holes",
          spec.bores(filleted.val()) == [], str(spec.bores(filleted.val())))

    mixed = (cq.Workplane("XY").box(60, 40, 20)
             .faces(">Z").workplane().hole(22)
             .faces(">Z").workplane().pushPoints([(-20, 0), (20, 0)]).hole(6))
    check("holes of different sizes are all found",
          spec.bores(mixed.val()) == [6.0, 6.0, 22.0],
          str(spec.bores(mixed.val())))

    blind = (cq.Workplane("XY").box(40, 40, 20)
             .faces(">Z").workplane().hole(8, 10))
    check("a blind hole counts too", spec.bores(blind.val()) == [8.0],
          str(spec.bores(blind.val())))

    # -- the verdict that matters ------------------------------------------
    print("\nThe case the vision Judge got wrong")
    # A plate carrying one hole where four were asked for. The Judge passed
    # this, on a render byte-identical to one it had just rejected.
    report = spec.check(PLATE_PLAN, as_step(plate(1), "one_hole"))
    check("a plate missing three holes is refused", not report.ok,
          report.summary())
    check("and the reason names the hole count",
          any(c.key == "num_holes" and not c.passed for c in report.checks))
    check("the failure is expressed as a measurement",
          "measured 1" in report.feedback(), report.feedback()[:90])

    report = spec.check(PLATE_PLAN, as_step(plate(4), "four_holes"))
    check("the correct plate is accepted", report.ok, report.summary())
    check("every hard check passed",
          all(c.passed for c in report.checks if c.hard))

    # -- not blocking correct work -----------------------------------------
    print("\nCorrect parts are never blocked")

    # The Planner counted a bearing block's two fixings and not its bore, so
    # the part legitimately measures one more hole than planned.
    surplus = spec.check(
        {"constraints": {"num_holes": 2, "hole_diameter": 6}},
        as_step(mixed, "mixed"))
    check("more holes than planned does not block", surplus.ok,
          surplus.summary())
    check("but it is still reported",
          any("advisory" in c.label and c.key == "num_holes"
              for c in surplus.checks))

    # The Planner put a 40mm tube's axis on X; the model stood it on Z.
    axis_swap = spec.check(
        {"dimensions": {"overall_bbox": {"xlen": 50, "ylen": 40, "zlen": 40}}},
        as_step(tube, "tube"))
    check("the same part in another orientation is not blocked", axis_swap.ok,
          axis_swap.summary())

    # The Planner stated a 20mm rod 100 long as 100 x 100 x 20.
    rod_plan = {"dimensions": {"overall_bbox": {"xlen": 100, "ylen": 100,
                                                "zlen": 20}}}
    rod = spec.check(rod_plan, as_step(shaft, "rod"))
    check("a wrong planned bounding box does not block a correct part",
          rod.ok, rod.summary())
    check("though the disagreement is shown",
          any(c.key == "bbox" and not c.passed for c in rod.checks))

    # volume_estimate is routinely out by an order of magnitude.
    vol = spec.check({"constraints": {"volume_estimate": 3840}},
                     as_step(plate(4), "vol"))
    check("a wildly wrong volume estimate does not block", vol.ok)

    # -- hard checks still bite --------------------------------------------
    print("\nHard checks still bite")

    wrong_dia = spec.check(
        {"constraints": {"num_holes": 4, "hole_diameter": 10}},
        as_step(plate(4), "dia"))
    check("a hole diameter nothing matches is refused", not wrong_dia.ok,
          wrong_dia.summary())

    empty = spec.check({}, as_step(plate(4), "noplan"))
    check("a plan claiming nothing measurable blocks nothing", empty.ok)
    check("watertightness is checked even with no plan at all",
          [c.key for c in empty.checks] == ["solid_valid"],
          str([c.key for c in empty.checks]))

    missing = spec.check(PLATE_PLAN, work / "does_not_exist.step")
    check("an unreadable part is reported, not treated as a failure",
          missing.ok and bool(missing.error), missing.summary())

    # -- tolerance ---------------------------------------------------------
    print("\nTolerance")
    near = spec.check(
        {"constraints": {"num_holes": 4, "hole_diameter": 6.002}},
        as_step(plate(4), "near"))
    check("a hole within tolerance still matches", near.ok, near.summary())
    check("0.25mm is the floor for a small nominal",
          spec._near(6.2, 6.0) and not spec._near(6.4, 6.0))
    check("a large nominal gets a proportional tolerance",
          spec._near(504.0, 500.0) and not spec._near(507.0, 500.0))

    # -- garbage in the plan -----------------------------------------------
    print("\nA malformed plan cannot crash the check")
    for label, plan in (
        ("nulls", {"constraints": {"num_holes": None, "hole_diameter": None}}),
        ("strings", {"constraints": {"num_holes": "four"}}),
        ("negative", {"constraints": {"num_holes": -2}}),
        ("fractional count", {"constraints": {"num_holes": 2.5}}),
        ("infinite", {"constraints": {"hole_diameter": float("inf")}}),
        ("wrong shape", {"dimensions": {"overall_bbox": [80, 60, 8]}}),
        ("not a dict", []),
    ):
        try:
            result = spec.check(plan, as_step(plate(4), "junk"))
            check(f"{label} is ignored rather than fatal", result.ok,
                  result.summary())
        except Exception as exc:                       # noqa: BLE001
            check(f"{label} is ignored rather than fatal", False,
                  f"{type(exc).__name__}: {exc}")

    # -- measurement accuracy ----------------------------------------------
    print("\nMeasurements agree with arithmetic")
    measured = spec.measure_step(as_step(plate(4), "measure"))
    expect = 80 * 60 * 8 - 4 * math.pi * 3 ** 2 * 8
    check("volume matches the closed form",
          abs(measured["volume"] - expect) < 0.5,
          f"{measured['volume']:.1f} vs {expect:.1f}")
    check("bounding box matches",
          all(abs(measured["bbox"][k] - v) < 1e-6
              for k, v in (("xlen", 80), ("ylen", 60), ("zlen", 8))))
    check("the solid reports watertight", measured["is_valid"])

    shutil.rmtree(work, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
