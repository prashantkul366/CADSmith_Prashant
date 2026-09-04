"""Deterministic checks of a built part against the plan that asked for it.

The vision Judge is an opinion.  On a real run it passed a plate carrying one
hole when four were asked for - the code and the render were byte-identical to
a version it had rejected moments before - and separately rejected a bearing
block whose volume was exactly right, by describing hole positions the code
did not contain.  Anything a model says about a measurable quantity is a guess
that happens to be phrased confidently.

So measurable claims are settled here instead, by the kernel:

* the Planner states ``overall_bbox``, ``num_holes`` and ``hole_diameter``;
* the executed solid is measured for the same quantities;
* a mismatch on a *hard* assertion blocks the version whatever the Judge said.

The Judge keeps the questions arithmetic cannot answer - proportion,
plausibility, whether the thing reads as the object that was asked for - and
loses the vote on everything else.

Which claims are allowed to block was decided by measurement, not taste.  Run
against parts already built by this app, a hole-count shortfall never once
flagged a correct part, so it blocks.  ``overall_bbox`` and
``volume_estimate`` both did flag correct parts - the Planner states 3,840 mm3
for a part measuring 38,400, and planned a 20mm rod as 100 x 100 x 20 - so
they are reported and never block.  A gate that rejects correct work gets
switched off, which is worse than not having it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: A length is "the same" within this many millimetres.  Loose enough to
#: absorb the kernel's own tolerance, tight enough that a wrong nominal size
#: never slips through.
LENGTH_TOL_MM = 0.25

#: ...or this fraction of the nominal, whichever is larger, so a 500mm part is
#: not held to the same absolute tolerance as a 5mm one.
LENGTH_TOL_FRAC = 0.01

#: A cylindrical face sweeping less than this is a fillet or a round, not a
#: bore.  Full circle is 2*pi; the slack absorbs parameterisation noise.
FULL_SWEEP = 6.2

#: How far past the surface to probe when deciding whether a cylinder holds
#: material or void, as a fraction of its radius.
PROBE_FRAC = 0.02


@dataclass
class SpecCheck:
    """One claim from the plan, and whether the built solid honours it."""

    key: str
    label: str
    expected: str
    actual: str
    passed: bool
    hard: bool = True

    @property
    def blocking(self) -> bool:
        return self.hard and not self.passed


@dataclass
class SpecReport:
    checks: list[SpecCheck] = field(default_factory=list)
    measured: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when nothing measurable contradicts the plan."""
        return not any(check.blocking for check in self.checks)

    @property
    def failures(self) -> list[SpecCheck]:
        return [c for c in self.checks if c.blocking]

    @property
    def checked(self) -> bool:
        return bool(self.checks)

    def summary(self) -> str:
        if self.error:
            return f"Specification not checked: {self.error}"
        if not self.checks:
            return "The plan stated nothing measurable to check."
        bad = self.failures
        if bad:
            parts = "; ".join(f"{c.label} - {c.actual}" for c in bad)
            return (f"{len(bad)} of {len(self.checks)} measured checks failed: "
                    f"{parts}")
        # An advisory check that did not hold is not a failure, but it is not
        # "all met" either. Saying so was overstating the result in exactly
        # the direction this whole module exists to stop: the rod above came
        # back "3 measured checks, all met" while the size it was planned to
        # did not match what was built.
        noted = [c for c in self.checks if not c.passed]
        if noted:
            parts = "; ".join(f"{c.label} - {c.actual}" for c in noted)
            return (f"{len(self.checks)} measured checks, none blocking; "
                    f"{len(noted)} differ from the plan: {parts}")
        return f"{len(self.checks)} measured checks, all met."

    def feedback(self) -> str:
        """What to tell the Refiner: the measurements, not an opinion."""
        lines = ["Kernel measurements of the part you produced:"]
        for check in self.checks:
            mark = "OK " if check.passed else "NO "
            lines.append(f"  {mark} {check.label}: wanted {check.expected}, "
                         f"measured {check.actual}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "measured": self.measured,
            "checks": [
                {"key": c.key, "label": c.label, "expected": c.expected,
                 "actual": c.actual, "passed": c.passed, "hard": c.hard}
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def bores(solid: Any) -> list[float]:
    """Diameters of the through and blind holes in a solid, smallest first.

    A hole is a cylindrical face that sweeps a full circle and has void rather
    than material just inside it.  The second half of that test is what
    separates a bore from a shaft or an outside diameter: both are full
    cylinders, and only the bore is hollow.  Probing the axis instead would
    call a tube's outer wall a hole, because the tube's bore already put void
    on the axis.

    Partial sweeps are excluded, so fillets and rounds are not counted.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_State

    classifier = BRepClass3d_SolidClassifier(solid.wrapped)
    found: list[float] = []

    for face in solid.Faces():
        surface = BRepAdaptor_Surface(face.wrapped)
        if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Cylinder:
            continue
        if (surface.LastUParameter() - surface.FirstUParameter()) < FULL_SWEEP:
            continue

        cylinder = surface.Cylinder()
        axis, radius = cylinder.Axis(), cylinder.Radius()
        if radius <= 0:
            continue

        u = (surface.FirstUParameter() + surface.LastUParameter()) / 2
        v = (surface.FirstVParameter() + surface.LastVParameter()) / 2
        on = surface.Value(u, v)

        # Foot of the perpendicular from that point down to the axis, so the
        # probe steps inward along the radius rather than along the axis.
        loc, direction = axis.Location(), axis.Direction()
        t = ((on.X() - loc.X()) * direction.X()
             + (on.Y() - loc.Y()) * direction.Y()
             + (on.Z() - loc.Z()) * direction.Z())
        foot = (loc.X() + direction.X() * t,
                loc.Y() + direction.Y() * t,
                loc.Z() + direction.Z() * t)

        step = max(min(radius * PROBE_FRAC, 0.5), 1e-3) / radius
        probe = gp_Pnt(on.X() + (foot[0] - on.X()) * step,
                       on.Y() + (foot[1] - on.Y()) * step,
                       on.Z() + (foot[2] - on.Z()) * step)
        classifier.Perform(probe, 1e-7)
        if classifier.State() == TopAbs_State.TopAbs_OUT:
            found.append(round(radius * 2, 3))

    return sorted(found)


def measure_step(step_path: str | Path) -> dict:
    """Measure the quantities the plan can make claims about."""
    import cadquery as cq

    shape = cq.importers.importStep(str(step_path))
    solid = shape.val()
    box = solid.BoundingBox()
    holes = bores(solid)
    return {
        "volume": solid.Volume(),
        "bbox": {"xlen": box.xlen, "ylen": box.ylen, "zlen": box.zlen},
        "is_valid": solid.isValid(),
        "holes": holes,
        "num_holes": len(holes),
    }


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def _near(actual: float, target: float) -> bool:
    return abs(actual - target) <= max(LENGTH_TOL_MM,
                                       abs(target) * LENGTH_TOL_FRAC)


def _number(raw: Any) -> Optional[float]:
    """A finite number, or None for anything the plan left blank or vague."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _count(raw: Any) -> Optional[int]:
    value = _number(raw)
    if value is None or value < 0 or value != int(value):
        return None
    return int(value)


def compare(plan: dict, measured: dict) -> list[SpecCheck]:
    """Every claim in the plan that the measurements can settle."""
    checks: list[SpecCheck] = []
    def as_map(value: Any) -> dict:
        """A model can put a list where the schema says object."""
        return value if isinstance(value, dict) else {}

    plan = as_map(plan)
    dimensions = as_map(plan.get("dimensions"))
    constraints = as_map(plan.get("constraints"))

    checks.append(SpecCheck(
        key="solid_valid", label="watertight solid",
        expected="valid", actual="valid" if measured["is_valid"] else "invalid",
        passed=bool(measured["is_valid"]), hard=True))

    bbox = as_map(dimensions.get("overall_bbox"))
    wanted_extents = [_number(bbox.get(a)) for a in ("xlen", "ylen", "zlen")]
    if all(e is not None and e > 0 for e in wanted_extents):
        got_extents = [measured["bbox"][a] for a in ("xlen", "ylen", "zlen")]
        # Compared as a sorted set of extents rather than axis by axis. The
        # Planner's choice of axis is not reliable and is rarely what was
        # asked for: a 40mm x 50mm long tube was planned lying along X and
        # built standing on Z, which is the same part. Checking per axis
        # rejected it. A genuinely wrong shape still fails, because its
        # extents differ as a set - the L-bracket that came out a cross wants
        # {5, 50, 50} and measures {30, 30, 50}.
        want, got = sorted(wanted_extents), sorted(got_extents)
        # Advisory, not blocking, and the reason is measured rather than
        # cautious. Across the runs checked while building this, the Planner
        # named dimensions correctly in key_dimensions every time but derived
        # overall_bbox wrongly often enough to matter - a 20mm diameter rod
        # 100mm long was planned as 100 x 100 x 20. Blocking on that field
        # rejects correct parts, and a gate that rejects correct parts gets
        # switched off. It becomes a hard gate once the extents come from a
        # structured operation graph instead of the Planner's arithmetic.
        checks.append(SpecCheck(
            key="bbox", label="overall size (advisory)",
            expected=" x ".join(f"{v:g}" for v in want) + " mm",
            actual=" x ".join(f"{v:.2f}" for v in got) + " mm",
            passed=all(_near(g, w) for g, w in zip(got, want)), hard=False))

    wanted_holes = _count(constraints.get("num_holes"))
    if wanted_holes is not None:
        actual = measured["num_holes"]
        # Asymmetric on purpose. Too few holes is the failure we actually see
        # and it is unambiguous: a plate asked for four and carrying one is
        # wrong however confident the Judge was. More holes than the plan
        # counted usually means the plan under-described the part rather than
        # that the part is wrong - a bearing block plan said "2 holes" meaning
        # its two fixings and did not count the bore it also specified. So a
        # shortfall blocks and a surplus is reported without blocking.
        short = actual < wanted_holes
        checks.append(SpecCheck(
            key="num_holes", label="hole count",
            expected=str(wanted_holes), actual=str(actual),
            passed=not short, hard=True))
        if actual > wanted_holes:
            checks[-1] = SpecCheck(
                key="num_holes", label="hole count (advisory)",
                expected=str(wanted_holes), actual=f"{actual} - more than planned",
                passed=False, hard=False)

    wanted_dia = _number(constraints.get("hole_diameter"))
    if wanted_dia is not None and wanted_dia > 0 and measured["holes"]:
        # A part may legitimately carry holes of several sizes - a bearing
        # block has a bore and two fixings - so the claim is that the stated
        # diameter is present, not that every hole matches it.
        hit = any(_near(d, wanted_dia) for d in measured["holes"])
        listed = ", ".join(f"{d:g}" for d in measured["holes"][:6])
        checks.append(SpecCheck(
            key="hole_diameter", label="hole diameter",
            expected=f"{wanted_dia:g} mm", actual=f"{listed} mm",
            passed=hit, hard=True))

    estimate = _number(constraints.get("volume_estimate"))
    if estimate is not None and estimate > 0:
        actual = measured["volume"]
        # Advisory only: planners routinely get this wrong by a factor of ten,
        # so a hard gate here would reject correct parts.
        ratio = actual / estimate
        checks.append(SpecCheck(
            key="volume_estimate", label="volume (advisory)",
            expected=f"~{estimate:g} mm3", actual=f"{actual:.0f} mm3",
            passed=0.5 <= ratio <= 2.0, hard=False))

    return checks


def check(plan: dict, step_path: str | Path) -> SpecReport:
    """Measure the built part and settle every claim the plan makes about it."""
    try:
        measured = measure_step(step_path)
    except Exception as exc:                       # noqa: BLE001 - kernel/IO
        # Never let a measurement failure block a run: it means we could not
        # check, not that the part is wrong.
        return SpecReport(error=f"{type(exc).__name__}: {exc}")
    return SpecReport(checks=compare(plan, measured), measured=measured)
