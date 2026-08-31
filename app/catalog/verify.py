"""Build a catalogue part and check it is sound before anyone relies on it.

This exists because of a specific failure.  cq_warehouse 0.8.0 returns every
washer, in all four standards, as a non-closed shell with roughly twice the
correct volume - ``isValid()`` is False and the volume is wrong by 2x.  A
part like that reaching the Judge would be reported as a broken solid and
blamed on the pipeline, when the fault is in the catalogue.

So nothing is served from the catalogue without passing through here first:

    the code executes, and assigns ``result``
    the result is a single solid, unless it is honestly an assembly
    the solid is valid and closed
    it has real volume and a sensible bounding box

A part that fails is never served.  The router falls through to the model
instead, which is slower but not wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Report:
    ok: bool
    part_id: str
    problems: list[str] = field(default_factory=list)
    volume: float = 0.0
    bbox: tuple[float, float, float] = (0.0, 0.0, 0.0)
    num_solids: int = 0
    num_faces: int = 0
    is_valid: bool = False
    build_ms: float = 0.0
    solid: Optional[Any] = None

    def summary(self) -> str:
        if self.ok:
            x, y, z = self.bbox
            return (f"{x:.1f}x{y:.1f}x{z:.1f} mm, {self.volume:.0f} mm3, "
                    f"{self.num_faces} faces, {self.build_ms:.0f} ms")
        return "; ".join(self.problems)


def build(code: str, timeout_note: str = "") -> tuple[Any, float]:
    """Execute a part's source and return its solid, plus build time."""
    namespace: dict[str, Any] = {}
    started = time.time()
    exec(compile(code, "<catalog-part>", "exec"), namespace)
    elapsed = (time.time() - started) * 1000
    if "result" not in namespace:
        raise KeyError("the script did not assign `result`")
    result = namespace["result"]
    solid = result.val() if hasattr(result, "val") else result
    return solid, elapsed


def check(part, allow_multi_solid: bool = False) -> Report:
    """Everything that must hold before a part is served."""
    report = Report(ok=False, part_id=getattr(part, "id", "?"))
    try:
        solid, report.build_ms = build(part.code)
    except Exception as error:
        report.problems.append(f"{type(error).__name__}: {error}")
        return report

    try:
        report.num_solids = len(solid.Solids())
        report.num_faces = len(solid.Faces())
        report.volume = solid.Volume()
        box = solid.BoundingBox()
        report.bbox = (box.xlen, box.ylen, box.zlen)
        report.is_valid = solid.isValid()
    except Exception as error:
        report.problems.append(f"could not measure it: {type(error).__name__}: {error}")
        return report

    if not report.is_valid:
        report.problems.append("the solid is not valid (OCCT rejects it)")
    if report.num_solids == 0:
        report.problems.append("no solid was produced")
    if report.num_solids > 1 and not allow_multi_solid:
        report.problems.append(
            f"{report.num_solids} disconnected solids - a single part should "
            f"be one body")
    if report.volume <= 0:
        report.problems.append("zero or negative volume")
    if min(report.bbox) <= 0:
        report.problems.append(f"degenerate bounding box {report.bbox}")

    report.solid = solid
    report.ok = not report.problems
    return report


def expect_volume(report: Report, expected: float, tolerance_pct: float = 2.0
                  ) -> Optional[str]:
    """Compare against an analytically known volume.

    The washer bug was not caught by validity alone - a shell can be invalid
    and still measure *something*. Two-times-wrong needs a number to compare
    against, so parts with a closed-form volume carry one.
    """
    if expected <= 0:
        return None
    error_pct = abs(report.volume - expected) / expected * 100.0
    if error_pct > tolerance_pct:
        return (f"volume {report.volume:.1f} mm3 differs from the exact "
                f"{expected:.1f} mm3 by {error_pct:.1f}%")
    return None
