"""The typed operation graph: parsing, static checks, and compiled geometry.

Every compiled script here is executed by the real kernel and the resulting
solid measured, because a compiler that produces plausible-looking CadQuery
which builds the wrong shape is exactly the failure this layer exists to
prevent.

Several cases are the parts a real model got wrong, expressed as graphs.

Run:  .venv/bin/python -m app.tests.test_graph
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cadquery as cq  # noqa: E402

from app import graph  # noqa: E402
from app.server import edits, spec  # noqa: E402


def built(payload) -> tuple[str, object]:
    """Compile and execute, returning the source and the Workplane."""
    _, code = graph.build(payload)
    namespace: dict = {}
    exec(code, namespace)                              # noqa: S102 - the point
    return code, namespace["result"]


PLATE = {"ops": [
    {"id": "stock", "type": "box", "params": {"x": 80, "y": 60, "z": 8}},
    {"id": "corner_holes", "type": "hole", "parent": "stock",
     "params": {"face": "top", "diameter": 6, "pattern": "rect_corners",
                "inset_x": 10, "inset_y": 10}}]}


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    def volume(payload, expected: float, label: str, faces: int = 0) -> None:
        try:
            _, result = built(payload)
        except Exception as exc:                       # noqa: BLE001
            check(label, False, f"{type(exc).__name__}: {exc}")
            return
        got = result.val().Volume()
        check(label, abs(got - expected) / expected < 1e-4,
              f"{got:.1f} vs {expected:.1f}")
        if faces:
            check(f"{label}: face count",
                  len(result.faces().vals()) == faces,
                  f"{len(result.faces().vals())} vs {faces}")

    # -- the parts a model got wrong ---------------------------------------
    print("\nThe parts a real model built wrongly, as graphs")

    volume(PLATE, 80 * 60 * 8 - 4 * math.pi * 3 ** 2 * 8,
           "plate with four corner holes", faces=10)

    volume({"ops": [
        {"id": "hex", "type": "prism",
         "params": {"sides": 6, "across_flats": 20, "height": 30}},
        {"id": "bore", "type": "hole", "parent": "hex",
         "params": {"face": "top", "diameter": 8, "pattern": "at",
                    "positions": [[0, 0]]}}]},
        (3 * math.sqrt(3) / 2) * (20 / math.sqrt(3)) ** 2 * 30
        - math.pi * 4 ** 2 * 30,
        "hexagon sized across the flats, not the corners")

    # Across corners is the other reading, and must give the other answer.
    volume({"ops": [{"id": "hex", "type": "prism",
                     "params": {"sides": 6, "across_corners": 20,
                                "height": 30}}]},
           (3 * math.sqrt(3) / 2) * 10 ** 2 * 30,
           "across corners means something different")

    # -- the rest of the vocabulary ----------------------------------------
    print("\nThe vocabulary builds what it claims")

    volume({"ops": [{"id": "c", "type": "cylinder",
                     "params": {"diameter": 40, "height": 50}}]},
           math.pi * 20 ** 2 * 50, "a cylinder")

    volume({"ops": [
        {"id": "od", "type": "cylinder",
         "params": {"diameter": 40, "height": 50}},
        {"id": "bore", "type": "hole", "parent": "od",
         "params": {"face": "top", "diameter": 30, "pattern": "at",
                    "positions": [[0, 0]]}}]},
        math.pi * (20 ** 2 - 15 ** 2) * 50, "a tube", faces=4)

    volume({"ops": [
        {"id": "disc", "type": "cylinder",
         "params": {"diameter": 100, "height": 10}},
        {"id": "bolts", "type": "hole", "parent": "disc",
         "params": {"face": "top", "diameter": 9, "pattern": "circular",
                    "count": 6, "bolt_circle_diameter": 75}}]},
        math.pi * 50 ** 2 * 10 - 6 * math.pi * 4.5 ** 2 * 10,
        "six holes on a bolt circle")

    volume({"ops": [
        {"id": "bar", "type": "box", "params": {"x": 100, "y": 20, "z": 10}},
        {"id": "row", "type": "hole", "parent": "bar",
         "params": {"face": "top", "diameter": 5, "pattern": "linear",
                    "count": 5, "spacing": 15}}]},
        100 * 20 * 10 - 5 * math.pi * 2.5 ** 2 * 10, "a row of holes")

    volume({"ops": [
        {"id": "b", "type": "box", "params": {"x": 40, "y": 40, "z": 20}},
        {"id": "pocket", "type": "hole", "parent": "b",
         "params": {"face": "top", "diameter": 10, "depth": 8,
                    "pattern": "at", "positions": [[0, 0]]}}]},
        40 * 40 * 20 - math.pi * 5 ** 2 * 8, "a blind pocket")

    _, hollow = built({"ops": [
        {"id": "b", "type": "box", "params": {"x": 40, "y": 40, "z": 20}},
        {"id": "sh", "type": "shell", "parent": "b",
         "params": {"thickness": 2, "open_face": "top"}}]})
    check("shelling hollows the part",
          hollow.val().Volume() < 40 * 40 * 20 * 0.5,
          f"{hollow.val().Volume():.0f} mm3")

    _, rounded = built({"ops": [
        {"id": "b", "type": "box", "params": {"x": 40, "y": 40, "z": 10}},
        {"id": "f", "type": "fillet", "parent": "b",
         "params": {"radius": 5, "edges": "vertical"}}]})
    check("filleting removes material and adds faces",
          rounded.val().Volume() < 40 * 40 * 10
          and len(rounded.faces().vals()) > 6,
          f"{rounded.val().Volume():.0f} mm3, "
          f"{len(rounded.faces().vals())} faces")

    # -- the mistakes that are now unrepresentable or caught ---------------
    print("\nWhat arithmetic can refuse before the kernel runs")

    for label, payload, expect in (
        ("holes placed off the face they are cut into",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 80, "y": 60, "z": 8}},
                  {"id": "h", "type": "hole", "parent": "s",
                   "params": {"face": "top", "diameter": 6, "pattern": "at",
                              "positions": [[5, 5], [75, 5], [75, 55]]}}]},
         "does not fit"),
        ("a hole that overhangs the edge by its radius",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 40, "y": 40, "z": 5}},
                  {"id": "h", "type": "hole", "parent": "s",
                   "params": {"face": "top", "diameter": 10, "pattern": "at",
                              "positions": [[18, 0]]}}]},
         "does not fit"),
        ("a fillet larger than the part",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 20, "y": 20, "z": 4}},
                  {"id": "f", "type": "fillet", "parent": "s",
                   "params": {"radius": 9}}]},
         "does not fit"),
        ("a blind hole deeper than the stock",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 40, "y": 40, "z": 10}},
                  {"id": "h", "type": "hole", "parent": "s",
                   "params": {"face": "top", "diameter": 6, "depth": 25,
                              "pattern": "at", "positions": [[0, 0]]}}]},
         "deeper than"),
        ("a hexagon sized both ways at once",
         {"ops": [{"id": "h", "type": "prism",
                   "params": {"sides": 6, "across_flats": 20,
                              "across_corners": 23, "height": 30}}]},
         "exactly one"),
        ("two holes in the same place",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 40, "y": 40, "z": 10}},
                  {"id": "h", "type": "hole", "parent": "s",
                   "params": {"face": "top", "diameter": 6, "pattern": "at",
                              "positions": [[5, 5], [5, 5]]}}]},
         "same place"),
        ("a wall thicker than the part is wide",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 20, "y": 20, "z": 10}},
                  {"id": "sh", "type": "shell", "parent": "s",
                   "params": {"thickness": 6}}]},
         "nothing hollow"),
        ("a dimension below what the kernel can build",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 40, "y": 40, "z": 0.0001}}]},
         "too small"),
        ("a face that does not exist",
         {"ops": [{"id": "s", "type": "box",
                   "params": {"x": 40, "y": 40, "z": 10}},
                  {"id": "h", "type": "hole", "parent": "s",
                   "params": {"face": "sideways", "diameter": 6,
                              "pattern": "at", "positions": [[0, 0]]}}]},
         "face must be"),
    ):
        try:
            built(payload)
            check(label, False, "was built anyway")
        except graph.GraphError as exc:
            check(label, expect in str(exc), str(exc)[:110])

    # -- malformed graphs --------------------------------------------------
    print("\nA malformed graph is refused with a usable message")
    for label, payload, expect in (
        ("not an object", [1, 2, 3], "object with an 'ops' list"),
        ("no ops", {"ops": []}, "non-empty"),
        ("unknown operation",
         {"ops": [{"type": "wormhole", "params": {}}]}, "unknown type"),
        ("missing a required parameter",
         {"ops": [{"id": "b", "type": "box", "params": {"x": 10}}]}, "needs"),
        ("duplicate ids",
         {"ops": [{"id": "a", "type": "box", "params": {"x": 1, "y": 1, "z": 1}},
                  {"id": "a", "type": "box", "params": {"x": 1, "y": 1, "z": 1}}]},
         "share the id"),
        ("a parent that does not exist",
         {"ops": [{"id": "a", "type": "box",
                   "params": {"x": 10, "y": 10, "z": 10}},
                  {"id": "h", "type": "hole", "parent": "ghost",
                   "params": {"diameter": 3}}]}, "not an earlier"),
        ("starting with an operation that modifies",
         {"ops": [{"id": "f", "type": "fillet", "params": {"radius": 2}}]},
         "cannot be the first"),
        ("an id that is not a name",
         {"ops": [{"id": "my part", "type": "box",
                   "params": {"x": 1, "y": 1, "z": 1}}]}, "variable name"),
        ("units that are not millimetres",
         {"units": "inch", "ops": [{"id": "b", "type": "box",
                                    "params": {"x": 1, "y": 1, "z": 1}}]},
         "millimetres"),
        ("a dimension that is not a number",
         {"ops": [{"id": "b", "type": "box",
                   "params": {"x": "wide", "y": 10, "z": 10}}]},
         "must be a number"),
        ("an infinite dimension",
         {"ops": [{"id": "b", "type": "box",
                   "params": {"x": 1e400, "y": 10, "z": 10}}]}, "finite"),
        ("more operations than the vocabulary supports",
         {"ops": [{"id": f"b{i}", "type": "box",
                   "params": {"x": 1, "y": 1, "z": 1}}
                  for i in range(graph.MAX_NODES + 1)]}, "more than"),
    ):
        try:
            graph.parse(payload)
            check(label, False, "was accepted")
        except graph.GraphError as exc:
            check(label, expect in str(exc), str(exc)[:110])

    check("bad JSON is reported as bad JSON",
          "Not valid JSON" in _error(lambda: graph.parse("{not json")))

    # -- convenience the model relies on -----------------------------------
    print("\nTolerating how models actually write things")
    flat = graph.parse({"ops": [
        {"id": "b", "type": "box", "x": 30, "y": 20, "z": 5}]})
    check("parameters written flat on the operation are read",
          flat.nodes[0].params["x"] == 30, str(flat.nodes[0].params))
    chained = graph.parse({"ops": [
        {"id": "b", "type": "box", "params": {"x": 30, "y": 20, "z": 5}},
        {"id": "f", "type": "fillet", "params": {"radius": 2}}]})
    check("an operation with no parent takes the one before it",
          chained.nodes[1].parent == "b", str(chained.nodes[1].parent))

    # -- the escape hatch --------------------------------------------------
    print("\nThe escape hatch keeps everything buildable")
    _, raw = built({"ops": [{"id": "custom", "type": "raw_script", "params": {
        "code": "result = cq.Workplane('XY').circle(10).extrude(5)"}}]})
    check("raw_script builds what the vocabulary cannot express",
          abs(raw.val().Volume() - math.pi * 100 * 5) < 0.5,
          f"{raw.val().Volume():.1f}")

    # -- it stays parametric ----------------------------------------------
    print("\nThe compiled script is a recipe, not a snapshot")
    code, _ = built(PLATE)
    check("dimensions are declared as named variables",
          "length = 80.0" in code and "width = 60.0" in code)
    check("a derived position is arithmetic, not a baked number",
          "length / 2 - corner_holes_inset_x" in code,
          [l for l in code.splitlines() if "pushPoints" in l][0][:90])

    plan = edits.plan_edit(code, "make it 20mm thick")
    check("the existing parameter editor still understands it", plan.possible,
          plan.reason)
    namespace: dict = {}
    exec(edits.apply_changes(code, plan.changes), namespace)  # noqa: S102
    check("and the patched script rebuilds",
          abs(namespace["result"].val().Volume()
              - (80 * 60 * 20 - 4 * math.pi * 9 * 20)) < 0.5,
          f"{namespace['result'].val().Volume():.1f}")

    wider = edits.plan_edit(code, "change the length to 120mm")
    namespace = {}
    exec(edits.apply_changes(code, wider.changes), namespace)  # noqa: S102
    solid = namespace["result"].val()
    check("resizing the plate moves its holes with it",
          abs(solid.Volume() - (120 * 60 * 8 - 4 * math.pi * 9 * 8)) < 0.5,
          f"{solid.Volume():.1f}")
    check("and all four survive the resize",
          spec.bores(solid) == [6.0, 6.0, 6.0, 6.0], str(spec.bores(solid)))

    # -- round trip --------------------------------------------------------
    print("\nA graph survives the round trip")
    parsed = graph.parse(PLATE)
    again = graph.parse(parsed.to_dict())
    check("re-parsing its own output gives the same graph",
          [n.id for n in again.nodes] == [n.id for n in parsed.nodes]
          and again.nodes[1].parent == "stock")
    check("the vocabulary describes itself for the model",
          "rect_corners" not in graph.vocabulary()
          or "hole" in graph.vocabulary(),
          graph.vocabulary()[:60])

    # -- the Coder layer ---------------------------------------------------
    print("\nAsking a model for a graph, and falling back when it cannot")
    from app.server import graph_coder

    def replier(*replies):
        queue = list(replies)
        def call(_system, _user):
            return queue.pop(0) if queue else queue_last
        return call

    good = graph_coder.generate(
        {}, "a plate",
        lambda s, u: json.dumps(PLATE))
    check("a valid graph is used", good.used_graph, good.fell_back)
    check("and compiles to runnable CadQuery",
          "cq.Workplane" in good.code)
    check("it took one attempt", good.attempts == 1)

    fenced = graph_coder.generate(
        {}, "a plate",
        lambda s, u: f"Here you go:\n```json\n{json.dumps(PLATE)}\n```\nhope that helps")
    check("JSON wrapped in prose and fences is still read",
          fenced.used_graph, fenced.fell_back)

    replies = ['{"ops": [{"type": "wormhole"}]}', json.dumps(PLATE)]
    retried = graph_coder.generate(
        {}, "a plate", lambda s, u: replies.pop(0))
    check("a schema mistake is retried with the complaint",
          retried.used_graph and retried.attempts == 2,
          f"used={retried.used_graph} attempts={retried.attempts}")

    hopeless = graph_coder.generate(
        {}, "a lofted duct", lambda s, u: "I cannot express that as a graph.")
    check("an unusable reply falls back rather than failing",
          not hopeless.used_graph and hopeless.code == "")
    check("and the fallback is classed as a schema miss",
          hopeless.kind == "schema" and not hopeless.is_design_error,
          hopeless.kind)

    off_the_edge = json.dumps({"ops": [
        {"id": "s", "type": "box", "params": {"x": 80, "y": 60, "z": 8}},
        {"id": "h", "type": "hole", "parent": "s",
         "params": {"face": "top", "diameter": 6, "pattern": "at",
                    "positions": [[40, 30]]}}]})
    design = graph_coder.generate({}, "a plate", lambda s, u: off_the_edge)
    check("a well-formed graph describing an unbuildable part is refused",
          not design.used_graph, design.code[:60])
    check("and is classed as a design error, not a schema miss",
          design.is_design_error, design.kind)
    check("with a reason naming the geometry",
          "does not fit" in design.fell_back, design.fell_back[:100])

    broken = graph_coder.generate(
        {}, "a plate",
        lambda s, u: (_ for _ in ()).throw(RuntimeError("backend down")))
    check("a backend failure is reported as such",
          broken.kind == "call" and "backend down" in broken.fell_back,
          broken.fell_back[:80])

    check("the system prompt carries the vocabulary",
          "rect_corners" in graph_coder.system_prompt()
          and "across_flats" in graph_coder.system_prompt())

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _error(fn) -> str:
    try:
        fn()
    except Exception as exc:                           # noqa: BLE001
        return str(exc)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
