"""Check that natural-language edits map to the right parameter, or refuse.

The point of these cases is the refusals as much as the successes: a
mis-mapped edit silently changes the wrong dimension, so anything ambiguous
must fall through to the Refiner agent instead of guessing.

Run:  .venv/bin/python -m app.tests.test_edits
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server.edits import apply_changes, describe, parameters, plan_edit

CODE = """import cadquery as cq

base_length = 100.0
base_width = 60.0
base_thickness = 10.0
support_height = 45.0
support_thickness = 8.0
hole_diameter = 8.0
hole_count = 4

result = cq.Workplane('XY').box(base_length, base_width, base_thickness)
for i in range(hole_count):
    spacing = 12.0
    result = result.faces('>Z').workplane().hole(hole_diameter)
"""


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def test_units() -> None:
    """A centimetre is ten millimetres, and the script is written in mm.

    "make it 1.5cm thick" used to set thickness to 1.5 - out by a factor of
    ten, silently, and looking for all the world like it had worked.
    """
    print("\nUnits are converted, not ignored")
    code = ("import cadquery as cq\n"
            "thickness = 10.0\n"
            "width = 40.0\n"
            "result = cq.Workplane('XY').box(width, width, thickness)\n")
    cases = [
        ("make it 15mm thick", 15.0),
        ("make it 1.5cm thick", 15.0),
        ("make it 2 cm thick", 20.0),
        ("make it 1 inch thick", 25.4),
        ("make it 0.5 inches thick", 12.7),
        ("make it 15 millimetres thick", 15.0),
        ("set thickness to 12", 12.0),        # no unit: millimetres
    ]
    for instruction, expected in cases:
        plan = plan_edit(code, instruction)
        got = plan.changes[0].new if plan.changes else None
        check(f"'{instruction}'", got is not None
              and abs(got - expected) < 1e-6,
              f"{got} (want {expected})")

    # "in" as a preposition must not become inches - a factor of 25 in the
    # wrong direction is worse than not supporting the unit at all.
    plan = plan_edit(code, "make it 3 in total, set thickness to 20")
    check("'3 in total' is not three inches",
          plan.changes and abs(plan.changes[0].new - 20.0) < 1e-6,
          str(plan.changes[0].new if plan.changes else plan.reason))


def test_impossible_values() -> None:
    print("\nValues the kernel cannot hold are refused")
    code = "import cadquery as cq\nthickness = 10.0\nresult = cq.Workplane('XY').box(5,5,thickness)\n"
    for instruction, why in [
        ("set thickness to 0.0000001", "below the kernel's tolerance"),
        ("set thickness to 1e400", "not a finite number"),
        ("set thickness to -5", "negative"),
    ]:
        plan = plan_edit(code, instruction)
        check(f"'{instruction}' refused ({why})", not plan.changes,
              plan.reason[:60] if not plan.changes else str(plan.changes))

    # ...but scientific notation that IS a number must be read properly.
    plan = plan_edit(code, "set thickness to 1e3")
    check("'1e3' is a thousand, not one",
          plan.changes and plan.changes[0].new == 1000.0,
          str(plan.changes[0].new if plan.changes else plan.reason))


def main() -> int:
    test_units()
    test_impossible_values()
    print("\nParameter extraction")
    found = parameters(CODE)
    check("top-level parameters found", len(found) == 7, str(sorted(found)))
    check("indented locals are ignored", "spacing" not in found)
    check("integers recognised as integers", found["hole_count"].is_integer)
    check("floats recognised as floats", not found["base_length"].is_integer)

    print("\nEdits that should map")
    cases = [
        ("make the base 15mm thick", "base_thickness", 15.0),
        ("set support height to 60", "support_height", 60.0),
        ("change the hole diameter to 6.5mm", "hole_diameter", 6.5),
        ("base length should be 120mm", "base_length", 120.0),
        ("increase support height by 10", "support_height", 55.0),
        ("add two more holes", "hole_count", 6.0),
    ]
    for instruction, expected_name, expected_value in cases:
        plan = plan_edit(CODE, instruction)
        ok = (plan.possible and plan.changes[0].name == expected_name
              and abs(plan.changes[0].new - expected_value) < 1e-9)
        check(f'"{instruction}"', ok,
              describe(plan.changes) if plan.possible else f"refused: {plan.reason}")

    print("\nEdits that must fall through to the agent")
    refusals = [
        ("add a reinforcing gusset between the walls", "shape"),
        ("make it stronger", "value"),
        ("round the outer edges", "shape"),
        ("set the flange diameter to 90mm", "no flange"),
        ("make the base thickness 0", "would set"),
        ("set base thickness to 10", "already"),
    ]
    for instruction, expected_reason in refusals:
        plan = plan_edit(CODE, instruction)
        check(f'"{instruction}"', not plan.possible and expected_reason in plan.reason,
              plan.reason or f"WRONGLY APPLIED: {describe(plan.changes)}")

    print("\nAmbiguity is refused, not guessed")
    ambiguous = plan_edit(CODE, "make the thickness 12mm")
    check('"make the thickness 12mm" is ambiguous',
          not ambiguous.possible and "ambiguous" in ambiguous.reason,
          ambiguous.reason or describe(ambiguous.changes))

    print("\nApplying a change")
    plan = plan_edit(CODE, "make the base 15mm thick")
    updated = apply_changes(CODE, plan.changes)
    check("assignment rewritten", "base_thickness = 15.0" in updated)
    check("other parameters untouched", "base_length = 100.0" in updated
          and "hole_count = 4" in updated)
    check("line count unchanged",
          len(updated.splitlines()) == len(CODE.splitlines()))
    check("still valid Python", _compiles(updated))

    integer_plan = plan_edit(CODE, "add two more holes")
    integer_code = apply_changes(CODE, integer_plan.changes)
    check("integer parameters stay integers", "hole_count = 6" in integer_code,
          next(l for l in integer_code.splitlines() if "hole_count =" in l))

    comment_code = "thickness = 4.0  # mm, per the drawing\n"
    commented = apply_changes(comment_code, plan_edit(comment_code,
                              "make the thickness 9mm").changes)
    check("trailing comment preserved",
          commented.strip() == "thickness = 9.0  # mm, per the drawing",
          commented.strip())

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _compiles(code: str) -> bool:
    try:
        compile(code, "<edited>", "exec")
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
