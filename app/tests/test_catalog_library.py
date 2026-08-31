"""The catalogue, end to end: every part builds, and only the right ones route.

Three things this has to establish.

*Every part the catalogue can serve is sound.*  Not "imports fine" - built in
the OpenCASCADE kernel, one valid watertight solid, and where a closed-form
volume exists, within tolerance of it.  This is the test that would have
caught cq_warehouse's washers, which are non-closed shells with twice the
correct volume.

*Routing is conservative.*  A request naming a standard part inside a bigger
one ("a bearing housing for a 6203") is a custom part and must reach the
model.  A wrong substitution here hands back confidently wrong hardware.

*What comes out is still editable.*  The point of generating over importing
is that the result is parametric, so the parameter editor must be able to
find and rewrite a gear's tooth count.

    .venv/bin/python -m app.tests.test_catalog_library
"""

from __future__ import annotations

import math
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

from app.catalog import library, parts, router, standards, verify  # noqa: E402
from app.server import edits  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def build_ok(label: str, part, expected_volume: float | None = None,
             tolerance: float = 2.0) -> verify.Report:
    report = verify.check(part)
    detail = report.summary()
    ok = report.ok
    if ok and expected_volume:
        problem = verify.expect_volume(report, expected_volume, tolerance)
        if problem:
            ok, detail = False, problem
    check(label, ok, detail)
    return report


# ---------------------------------------------------------------------------

def test_gears() -> None:
    print("\nGears (cq_gears)")
    if not library.HAVE_GEARS:
        check("cq_gears is installed", False, "skipping the gear families")
        return
    # The tip diameter of an involute gear is module x teeth + 2 modules.
    for module, teeth in ((2.0, 20), (1.5, 40), (3.0, 12)):
        report = build_ok(f"spur module {module:g}, {teeth} teeth",
                          library.spur_gear(module=module, teeth=teeth))
        if report.ok:
            want = module * teeth + 2 * module
            got = max(report.bbox[0], report.bbox[1])
            check(f"  its tip diameter is m*z+2m = {want:g}",
                  abs(got - want) < 0.15, f"got {got:.2f}")
    build_ok("helical, 20 degree", library.spur_gear(teeth=24, helix_angle=20.0))
    build_ok("herringbone", library.herringbone_gear(teeth=20))
    build_ok("internal ring gear", library.ring_gear(teeth=40))
    build_ok("rack", library.rack_gear())
    build_ok("bevel", library.bevel_gear(teeth=20))


def test_fasteners() -> None:
    print("\nFasteners (cq_warehouse)")
    if not library.HAVE_WAREHOUSE:
        check("cq_warehouse is installed", False, "skipping fasteners")
        return
    for kind in library.SCREW_KINDS:
        build_ok(f"{kind} screw M8x30", library.screw("M8", 30.0, kind=kind))
    for kind in ("hex", "hex_flange", "square", "domed_cap"):
        build_ok(f"{kind} nut M8", library.nut("M8", kind=kind))
    build_ok("M8x30 with a real thread",
             library.screw("M8", 30.0, threaded=True))
    build_ok("16 tooth sprocket", library.sprocket(teeth=16))

    print("\n  every size in our thread table is buildable")
    bad = []
    for size in standards.ISO_4762:
        if size not in standards.THREADS:
            continue
        report = verify.check(library.screw(size, standards.THREADS[
            standards._normalise(size)].diameter * 3))
        if not report.ok:
            bad.append(f"{size}: {report.summary()}")
    check("M2 through M24 all build", not bad, "; ".join(bad[:3]))


def test_our_own_parts() -> None:
    print("\nParts we build ourselves")
    # A washer is an annulus, so its volume is exactly computable. This is
    # the check cq_warehouse fails by 125%.
    washer = standards.ISO_7089["M8"]
    want = (math.pi / 4 * (washer.outer_diameter ** 2
                           - washer.inner_diameter ** 2) * washer.thickness)
    build_ok("M8 plain washer, against its exact volume",
             parts.flat_washer("M8"), expected_volume=want, tolerance=0.5)

    build_ok("6203 bearing", parts.ball_bearing("6203"))
    build_ok("O-ring 20 x 2.5", parts.o_ring(20.0, 2.5))
    build_ok("dowel pin 6 x 20", parts.dowel_pin(6.0, 20.0))

    # A swept helix has a closed-form volume too: arc length x section.
    wire, outer, free, coils = 2.0, 20.0, 50.0, 8
    mean_r = (outer - wire) / 2.0
    height = free - wire
    arc = coils * math.hypot(2 * math.pi * mean_r, height / coils)
    build_ok("compression spring, against its exact volume",
             parts.compression_spring(wire, outer, free, coils),
             expected_volume=arc * math.pi / 4 * wire ** 2, tolerance=2.0)

    print("\n  timing pulleys - pitch diameter is teeth x pitch / pi")
    for belt, teeth in (("GT2", 20), ("HTD5M", 24), ("T5", 18), ("T10", 30)):
        profile = parts.BELT_PROFILES[belt]
        report = build_ok(f"{belt} pulley, {teeth} teeth",
                          parts.timing_pulley(teeth, belt, bore=0))
        if report.ok:
            want = teeth * profile.pitch / math.pi - 2 * profile.pitch_line_offset
            got = max(report.bbox[0], report.bbox[1])
            check(f"  {belt} outside diameter",
                  abs(got - want) < 0.5, f"want {want:.2f} got {got:.2f}")


def test_the_guard() -> None:
    print("\nThe validity guard")
    good = verify.check(parts.flat_washer("M8"))
    check("a sound part passes", good.ok, good.summary())

    if library.HAVE_WAREHOUSE:
        # The specific defect this guard exists for.
        broken = parts.CatalogPart(
            id="cq_warehouse_washer", title="", standard="ISO 7089",
            code='import cadquery as cq\n'
                 'from cq_warehouse.fastener import PlainWasher\n'
                 'result = cq.Workplane("XY").add('
                 'PlainWasher(size="M8", fastener_type="iso7089"))\n')
        report = verify.check(broken)
        check("cq_warehouse's broken washer is rejected", not report.ok,
              "; ".join(report.problems)[:70])
        spec = standards.ISO_7089["M8"]
        exact = (math.pi / 4 * (spec.outer_diameter ** 2
                                - spec.inner_diameter ** 2) * spec.thickness)
        check("and its volume error is caught too",
              verify.expect_volume(report, exact) is not None,
              f"{report.volume:.0f} vs {exact:.0f} mm3")
        check("the router never serves it",
              router.select("an M8 plain washer") is None
              or router.select("an M8 plain washer").source == "cadsmith",
              str(getattr(router.select("an M8 plain washer"), "source", None)))

    # A part whose code is broken must be reported, not raise.
    report = verify.check(parts.CatalogPart(
        id="broken", title="", standard="", code="result = undefined_name"))
    check("a part that will not execute is reported, not raised",
          not report.ok and report.problems)
    report = verify.check(parts.CatalogPart(
        id="noresult", title="", standard="", code="x = 1"))
    check("a part that assigns no result is reported",
          not report.ok and "result" in report.problems[0])


def test_routing() -> None:
    print("\nRouting - what is a standard part")
    routes = [
        ("a 20 tooth spur gear, module 2", "gear_spur_m2_z20"),
        ("a module 1.5 helical gear with 30 teeth", "gear_helical_m1.5_z30"),
        ("an internal ring gear with 40 teeth module 2", "gear_ring_m2_z40"),
        ("a bevel gear with 20 teeth module 2", "gear_bevel_m2_z20"),
        ("a 16 tooth sprocket", "sprocket_16t_p12.7"),
        ("an M8x30 socket head cap screw", "iso4762_socket_head_m8x30"),
        ("a countersunk M6 screw 20mm long", "iso10642_countersunk_m6x20"),
        ("an M10 hex nut", "iso4032_hex_nut_m10"),
        ("an M8 flat washer", "iso7089_m8"),
        ("a 6203 bearing", "bearing_6203"),
        ("a compression spring, 2mm wire, 20mm od, 50mm long",
         "spring_d2_od20_l50"),
        ("a GT2 pulley with 20 teeth", "pulley_gt2_20t"),
        ("a T5 timing pulley with 18 teeth", "pulley_t5_18t"),
    ]
    for text, expected in routes:
        result = router.select(text)
        check(f"'{text[:42]}'", result is not None and result.part.id == expected,
              result.part.id if result else "did not route")

    print("\n  and what is not")
    refuse = [
        "a bearing housing for a 6203",
        "a bracket that takes four M8 screws",
        "a gearbox housing",
        "a spur gear",                       # no tooth count: under-specified
        "a motor mount plate",
        "a pulley for a v belt",             # not a timing pulley
        "an extension spring",               # ends this builder does not make
        "a tensioner bracket for a GT2 belt",
        "a plate for an M8 screw",
    ]
    for text in refuse:
        result = router.select(text)
        check(f"'{text[:42]}' reaches the model", result is None,
              result.part.id if result else "")


def test_still_editable() -> None:
    print("\nWhat comes out is still parametric")
    routed = router.select("a 20 tooth spur gear, module 2")
    check("a gear routes", routed is not None)
    if not routed:
        return
    found = edits.parameters(routed.part.code)
    check("the editor sees the gear's parameters",
          "teeth_number" in found and "module" in found,
          ", ".join(sorted(found)[:6]))

    plan = edits.plan_edit(routed.part.code, "make it 40 teeth")
    check("'make it 40 teeth' is a parameter patch",
          bool(plan.changes) and plan.changes[0].name == "teeth_number",
          plan.changes[0].name if plan.changes else plan.reason)

    if plan.changes:
        patched = edits.apply_changes(routed.part.code, plan.changes)
        report = verify.check(parts.CatalogPart(
            id="patched", title="", standard="", code=patched))
        check("the patched gear rebuilds in the kernel", report.ok,
              report.summary())
        if report.ok:
            want = 2.0 * 40 + 2 * 2.0
            got = max(report.bbox[0], report.bbox[1])
            check("and it really has 40 teeth now (tip dia 84mm)",
                  abs(got - want) < 0.2, f"got {got:.2f}")

    screw = router.select("an M8x30 socket head cap screw")
    if screw:
        plan = edits.plan_edit(screw.part.code, "make it 50mm long")
        check("a screw's length is patchable",
              bool(plan.changes) and plan.changes[0].name == "length",
              plan.changes[0].name if plan.changes else plan.reason)


def test_degrades_without_the_libraries() -> None:
    print("\nMissing libraries degrade rather than break")
    real_gears, real_warehouse = library.HAVE_GEARS, library.HAVE_WAREHOUSE
    try:
        library.HAVE_GEARS = library.HAVE_WAREHOUSE = False
        check("a gear request falls through when cq_gears is absent",
              router.select("a 20 tooth spur gear, module 2") is None)
        fallback = router.select("an M8x30 socket head cap screw")
        check("screws still come from parts.py",
              fallback is not None and fallback.report.ok,
              fallback.part.id if fallback else "nothing")
        washer = router.select("an M8 flat washer")
        check("washers are unaffected", washer is not None and washer.report.ok)
        described = router.describe()
        check("the health panel reports the narrower catalogue",
              described["backends"] == {"cq_gears": False, "cq_warehouse": False}
              and "screws (2 heads)" in described["families"])
    finally:
        library.HAVE_GEARS, library.HAVE_WAREHOUSE = real_gears, real_warehouse


def main() -> int:
    test_gears()
    test_fasteners()
    test_our_own_parts()
    test_the_guard()
    test_routing()
    test_still_editable()
    test_degrades_without_the_libraries()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(f"   - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
