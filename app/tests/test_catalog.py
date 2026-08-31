"""The parametric hardware catalogue: does it build, and is it really editable?

Runs the real OpenCASCADE kernel throughout - no mocking - and checks each
generated part against the dimension table it came from, because a part that
builds cleanly to the wrong size is the failure mode that matters here.

The last group is the one that justifies generating hardware instead of
downloading it: the emitted source has to survive the app's own parameter
editor. A downloaded STEP would fail every one of those checks.

    .venv/bin/python -m app.tests.test_catalog
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cadquery as cq  # noqa: E402

from app.catalog import parts, standards  # noqa: E402
from app.server import edits  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def build(code: str):
    namespace: dict = {}
    exec(compile(code, "<catalog>", "exec"), namespace)
    result = namespace["result"]
    return result.val() if hasattr(result, "val") else result


def near(a: float, b: float, tol: float = 0.05) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------

def test_tables_are_sane() -> None:
    print("\nThe tables agree with themselves")
    bad = [key for key, spec in standards.ISO_4762.items()
           if spec.head_diameter <= standards.THREADS[key].diameter]
    check("every cap screw head is wider than its thread", not bad, str(bad))

    bad = [key for key, spec in standards.ISO_4762.items()
           if spec.socket_across_flats >= spec.head_diameter]
    check("every hex socket fits inside its head", not bad, str(bad))

    bad = [key for key, thread in standards.THREADS.items()
           if not (thread.tapping_drill < thread.diameter
                   < thread.clearance_close <= thread.clearance_normal)]
    check("tapping < nominal < close fit <= normal fit", not bad, str(bad))

    bad = [key for key, spec in standards.ISO_7089.items()
           if spec.inner_diameter <= standards.THREADS[key].diameter
           or spec.outer_diameter <= spec.inner_diameter]
    check("every washer clears its screw and has a rim", not bad, str(bad))

    bad = [key for key, spec in standards.BEARINGS.items()
           if spec.bore >= spec.outer_diameter or spec.width <= 0]
    check("every bearing bore is inside its outer diameter", not bad, str(bad))


def test_every_part_builds() -> None:
    print("\nEvery part builds as one watertight solid")
    made = []
    for size in standards.ISO_4762:
        made.append(parts.socket_head_cap_screw(size, 30))
    for size in standards.ISO_4014:
        made.append(parts.hex_bolt(size, 40))
    for size in standards.ISO_4032:
        made.append(parts.hex_nut(size))
    for size in standards.ISO_7089:
        made.append(parts.flat_washer(size))
    for designation in standards.BEARINGS:
        made.append(parts.ball_bearing(designation))
    for cord in standards.O_RING_CORDS:
        made.append(parts.o_ring(20.0, cord))
    for diameter in (3.0, 6.0, 10.0, 16.0):
        made.append(parts.dowel_pin(diameter, diameter * 4))

    broken, unwatertight, multi = [], [], []
    for part in made:
        try:
            solid = build(part.code)
        except Exception as error:
            broken.append(f"{part.id}: {type(error).__name__}")
            continue
        if not solid.isValid():
            unwatertight.append(part.id)
        if len(solid.Solids()) != 1:
            multi.append(f"{part.id}={len(solid.Solids())}")

    check(f"all {len(made)} parts execute", not broken, "; ".join(broken[:3]))
    check("all are watertight", not unwatertight, "; ".join(unwatertight[:3]))
    check("all are a single solid", not multi, "; ".join(multi[:3]))


def test_dimensions_match_the_standard() -> None:
    print("\nWhat the kernel measures is what the standard says")
    for size in ("M4", "M8", "M16"):
        spec = standards.ISO_4762[size]
        solid = build(parts.socket_head_cap_screw(size, 30).code)
        box = solid.BoundingBox()
        check(f"{size} cap screw head is {spec.head_diameter}mm across",
              near(box.xlen, spec.head_diameter, 0.2), f"{box.xlen:.2f}")
        check(f"{size} cap screw bearing face is the origin",
              near(box.zmin, -30.0, 0.01), f"zmin {box.zmin:.2f}")

    for size in ("M5", "M10", "M20"):
        spec = standards.ISO_4032[size]
        solid = build(parts.hex_nut(size).code)
        box = solid.BoundingBox()
        across_corners = spec.across_flats * 2 / math.sqrt(3)
        check(f"{size} nut measures {spec.across_flats}mm across flats",
              spec.across_flats - 0.3 <= max(box.xlen, box.ylen)
              <= across_corners + 0.3,
              f"{max(box.xlen, box.ylen):.2f}")
        check(f"{size} nut is {spec.height}mm thick",
              near(box.zlen, spec.height, 0.01), f"{box.zlen:.2f}")

    for size in ("M6", "M12"):
        spec = standards.ISO_7089[size]
        solid = build(parts.flat_washer(size).code)
        ideal = (math.pi / 4 * (spec.outer_diameter ** 2
                                - spec.inner_diameter ** 2) * spec.thickness)
        check(f"{size} washer volume matches the annulus",
              near(solid.Volume(), ideal, ideal * 0.01),
              f"{solid.Volume():.1f} vs {ideal:.1f}")

    for designation in ("608", "6203", "6305"):
        spec = standards.BEARINGS[designation]
        solid = build(parts.ball_bearing(designation).code)
        box = solid.BoundingBox()
        check(f"{designation} envelope is {spec.outer_diameter}x{spec.width}",
              near(box.xlen, spec.outer_diameter, 0.01)
              and near(box.zlen, spec.width, 0.01),
              f"{box.xlen:.2f} x {box.zlen:.2f}")

    # Pappus's theorem is an independent check on the torus: a mistake in the
    # mean radius would still build and still look like an O-ring.
    for inner, cord in ((20.0, 2.5), (50.0, 3.53)):
        solid = build(parts.o_ring(inner, cord).code)
        ideal = 2 * math.pi ** 2 * ((inner + cord) / 2) * (cord / 2) ** 2
        check(f"O-ring {inner}x{cord} matches Pappus",
              near(solid.Volume(), ideal, ideal * 0.01),
              f"{solid.Volume():.1f} vs {ideal:.1f}")


def test_hole_helpers() -> None:
    print("\nThe hole a fastener actually needs")
    check("an M8 screw needs a 9mm normal clearance hole",
          standards.clearance_hole("M8") == 9.0)
    check("an M8 close fit is 8.4mm",
          standards.clearance_hole("M8", "close") == 8.4)
    check("an M8 coarse thread is tapped 6.8mm",
          standards.tapping_drill("M8") == 6.8)
    diameter, depth = standards.counterbore("M8")
    check("an M8 counterbore clears a 13mm head",
          diameter > standards.ISO_4762["M8"].head_diameter
          and depth == standards.ISO_4762["M8"].head_height,
          f"{diameter} x {depth}")
    check("sizes are accepted however they are written",
          standards.clearance_hole("m8") == standards.clearance_hole("M8")
          == standards.clearance_hole("8"))


def test_selection() -> None:
    print("\nPicking a catalogue part out of a request")
    hits = {
        "M8x30 socket head cap screw": "iso4762_m8x30.0",
        "an M6 x 20 SHCS": "iso4762_m6x20.0",
        "M10 hex bolt 40mm long": "iso4014_m10x40.0",
        "M8 hex nut": "iso4032_m8",
        "M12 washer": "iso7089_m12",
        "a 6203 bearing": "bearing_6203",
        "o-ring 20 x 2.5": "oring_20.0x2.5",
        "dowel pin 6 x 20": "dowel_6.0x20.0",
    }
    for text, expected in hits.items():
        found = parts.select(text)
        check(f"'{text}' is a catalogue part",
              found is not None and found.id == expected,
              found.id if found else "no match")

    # The dangerous direction: a custom part that merely mentions hardware
    # must still be generated, never substituted.
    misses = [
        "a bracket with four M8 clearance holes",
        "a motor mount plate using M6 screws",
        "a bearing housing for a 6203",
        "a pillow block for a 25mm shaft",
        "an adapter plate with M10 holes",
        "M99 bolt",
    ]
    for text in misses:
        found = parts.select(text)
        check(f"'{text[:40]}' stays a custom part",
              found is None, found.id if found else "")


def test_the_generated_code_is_editable() -> None:
    """The whole reason for generating rather than importing a STEP."""
    print("\nThe emitted source survives the app's own parameter editor")
    screw = parts.socket_head_cap_screw("M8", 30)

    found = edits.parameters(screw.code)
    check("the editor can read its parameters",
          {"thread_diameter", "length", "head_diameter",
           "head_height"} <= set(found),
          ", ".join(sorted(found)))

    check("no dimension is mistaken for a count",
          not any(p.is_integer for p in found.values()),
          ", ".join(n for n, p in found.items() if p.is_integer))

    plan = edits.plan_edit(screw.code, "make the length 45mm")
    check("a length change is understood",
          bool(plan.changes)
          and plan.changes[0].name == "length"
          and plan.changes[0].new == 45.0,
          plan.changes[0].name if plan.changes else plan.reason)

    patched = edits.apply_changes(screw.code, plan.changes)
    solid = build(patched)
    box = solid.BoundingBox()
    check("and the kernel rebuilds it at the new length",
          near(box.zlen, standards.ISO_4762["M8"].head_height + 45.0, 0.2),
          f"{box.zlen:.2f}mm overall")

    # A downloaded STEP would refuse every one of these. This one cannot even
    # be asked the question, which is exactly the point.
    original = build(screw.code)
    check("the edit actually changed the geometry",
          not near(original.Volume(), solid.Volume(), 1.0),
          f"{original.Volume():.0f} -> {solid.Volume():.0f} mm3")


def main() -> int:
    test_tables_are_sane()
    test_every_part_builds()
    test_dimensions_match_the_standard()
    test_hole_helpers()
    test_selection()
    test_the_generated_code_is_editable()

    print("\n" + "=" * 58)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:6])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
