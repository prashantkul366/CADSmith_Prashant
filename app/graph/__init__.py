"""A part described as a graph of typed operations, compiled to CadQuery.

The pipeline's artifact today is a free-form Python script.  That works, and
it is why the app can build anything CadQuery can express, but it gives the
model an unbounded action space in which to make coordinate mistakes that
nothing downstream can see.  A graph closes that space: the model fills in
named parameters, and where those sit in kernel space is decided here, once.

Three properties are worth stating because everything else follows from them.

The graph **compiles to the CadQuery the app already executes**, so the
kernel, the executor, the renderer, the exporters, the drawing sheet, the
catalogue and every existing test keep working untouched.  This is a layer
above what exists, not a replacement for it.

The compiled script **declares its dimensions as named variables**, so the
parameter-patch editor keeps working on graph-built parts exactly as it does
on generated ones - "make it 20mm thick" still rewrites one assignment and
rebuilds in about a second with no model call.

And the graph **can be checked before the kernel runs**.  A hole that falls
off the face it is cut into is arithmetic, and answering it in milliseconds
is better than discovering it after a build, a render and a judging round.

``raw_script`` remains as an escape hatch, so nothing the app can build today
becomes unbuildable.  Its contents cannot be checked, and that is said out
loud rather than hidden.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .ops import REGISTRY, Ctx, GraphError, Node

__all__ = ["Graph", "GraphError", "compile_graph", "parse", "vocabulary"]

#: Guards against a model emitting something enormous or self-referential.
MAX_NODES = 60


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    units: str = "mm"

    @property
    def result(self) -> Node:
        return self.nodes[-1]

    def to_dict(self) -> dict:
        return {
            "units": self.units,
            "ops": [
                {k: v for k, v in
                 (("id", n.id), ("type", n.type), ("parent", n.parent),
                  ("params", n.params))
                 if v is not None}
                for n in self.nodes
            ],
        }


def parse(payload: Any) -> Graph:
    """Read a graph, refusing anything that is not one.

    Errors name the node and say what to do, because they are read by the
    model that produced the graph as much as by a person.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GraphError(f"Not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise GraphError("A graph is an object with an 'ops' list.")

    raw_ops = payload.get("ops")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise GraphError("'ops' must be a non-empty list of operations.")
    if len(raw_ops) > MAX_NODES:
        raise GraphError(f"{len(raw_ops)} operations is more than the "
                         f"{MAX_NODES} this vocabulary supports.")

    nodes: list[Node] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, dict):
            raise GraphError(f"Operation {index} is not an object.")
        kind = raw.get("type")
        if kind not in REGISTRY:
            raise GraphError(
                f"Operation {index}: unknown type {kind!r}. Available: "
                f"{', '.join(sorted(REGISTRY))}.")
        node_id = str(raw.get("id") or f"{kind}_{index}")
        if not node_id.isidentifier():
            raise GraphError(
                f"Operation {index}: id {node_id!r} must be usable as a "
                f"variable name.")
        if node_id in seen:
            raise GraphError(f"Two operations share the id {node_id!r}.")
        seen.add(node_id)

        params = raw.get("params")
        if params is None:
            # Tolerate parameters written flat on the operation, which models
            # do often enough that refusing it would cost more than it buys.
            params = {k: v for k, v in raw.items()
                      if k not in ("id", "type", "parent", "params")}
        if not isinstance(params, dict):
            raise GraphError(f"{node_id}: 'params' must be an object.")

        spec = REGISTRY[kind]
        missing = [p for p in spec.required if p not in params]
        if missing:
            raise GraphError(
                f"{node_id}: {kind} needs {', '.join(missing)}.")

        for name in spec.numeric:
            if name not in params:
                continue
            value = params[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = float(str(value))
                except (TypeError, ValueError):
                    raise GraphError(
                        f"{node_id}: '{name}' must be a number, got "
                        f"{params[name]!r}.") from None
            if not math.isfinite(float(value)):
                raise GraphError(
                    f"{node_id}: '{name}' is not a finite number.")
            params[name] = float(value)

        parent = raw.get("parent")
        if parent is not None and str(parent) not in seen:
            raise GraphError(
                f"{node_id}: parent {parent!r} is not an earlier operation.")
        if spec.needs_parent and parent is None:
            if not nodes:
                raise GraphError(
                    f"{node_id}: {kind} changes an existing solid, so it "
                    f"cannot be the first operation.")
            parent = nodes[-1].id

        nodes.append(Node(id=node_id, type=kind, params=params,
                          parent=str(parent) if parent else None))

    if REGISTRY[nodes[0].type].needs_parent:
        raise GraphError(
            f"The first operation must create a solid, not modify one. "
            f"{nodes[0].type} modifies.")

    units = str(payload.get("units") or "mm").lower()
    if units not in ("mm", "millimetre", "millimeter"):
        raise GraphError(
            f"Dimensions must be in millimetres; got {units!r}.")
    return Graph(nodes=nodes, units="mm")


def _extents(graph: Graph) -> dict[str, Optional[tuple]]:
    """The overall size each node leaves behind, for checking what follows.

    Approximate by design: an operation that only removes material or eases an
    edge leaves the extents alone, which is what a hole needs to know about
    the stock it is cut from. Anything genuinely unknown is None, and every
    check that reads it treats None as "cannot say" rather than "fine".
    """
    known: dict[str, Optional[tuple]] = {}
    for node in graph.nodes:
        spec = REGISTRY[node.type]
        parent = known.get(node.parent) if node.parent else None
        if spec.extents is not None:
            known[node.id] = spec.extents(node, parent)
        elif node.type == "raw_script":
            known[node.id] = None
        else:
            known[node.id] = parent
    return known


def _names(graph: Graph) -> dict[str, Optional[tuple]]:
    """The variable names each node's extents are available under.

    Carried forward through operations that do not resize the solid, so a
    hole cut into a filleted plate still refers to the plate's own length and
    width rather than to numbers copied out of them.
    """
    known: dict[str, Optional[tuple]] = {}
    for node in graph.nodes:
        spec = REGISTRY[node.type]
        if spec.names is not None:
            known[node.id] = spec.names(node)
        elif node.type == "raw_script":
            known[node.id] = None
        else:
            known[node.id] = known.get(node.parent) if node.parent else None
    return known


def check(graph: Graph) -> list[str]:
    """Everything wrong with this graph that arithmetic can establish."""
    problems: list[str] = []
    known, named = _extents(graph), _names(graph)
    for node in graph.nodes:
        spec = REGISTRY[node.type]
        if spec.check is None:
            continue
        ctx = Ctx(extents=known.get(node.parent) if node.parent else None,
                  vars=named.get(node.parent) if node.parent else None)
        try:
            problems.extend(spec.check(node, ctx))
        except GraphError as exc:
            problems.append(str(exc))
    return problems


def compile_graph(graph: Graph, strict: bool = True) -> str:
    """Write the graph out as CadQuery.

    With ``strict``, a graph that fails a static check is refused rather than
    built, since the whole point is to answer in milliseconds what a build,
    a render and a judging round would take minutes to discover.
    """
    if strict:
        problems = check(graph)
        if problems:
            raise GraphError(" ".join(problems))

    known, named = _extents(graph), _names(graph)
    needs_math = any(
        (n.type == "prism" and n.get("across_flats") is not None)
        or (n.type == "hole" and str(n.get("pattern", "at")) == "circular")
        for n in graph.nodes)

    lines = ["import cadquery as cq"]
    if needs_math:
        lines.append("import math")
    lines.append("")

    for index, node in enumerate(graph.nodes):
        spec = REGISTRY[node.type]
        ctx = Ctx(extents=known.get(node.parent) if node.parent else None,
                  vars=named.get(node.parent) if node.parent else None)
        if index:
            lines.append("")
        lines.append(f"# {node.id}: {spec.summary.split('.')[0].lower()}")
        lines.extend(spec.emit(node, ctx))

    return "\n".join(lines).rstrip() + "\n"


def build(payload: Any, strict: bool = True) -> tuple[Graph, str]:
    """Parse, check and compile in one step."""
    graph = parse(payload)
    return graph, compile_graph(graph, strict=strict)


def vocabulary() -> str:
    """The operations, written for the model that has to produce a graph."""
    lines = ["Operations available. Dimensions are millimetres.", ""]
    for name in sorted(REGISTRY):
        spec = REGISTRY[name]
        needs = f" (requires {', '.join(spec.required)})" if spec.required else ""
        lines.append(f"- {name}{needs}: {spec.summary}")
    return "\n".join(lines)
