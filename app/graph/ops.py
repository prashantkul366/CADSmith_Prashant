"""The operation vocabulary a part can be described in.

Every failure this app has produced on a real model came from the same place:
the model reasoning about coordinates.  Holes placed at (70, 50) on a
workplane centred at the origin, so three of four fell off an 80x60 plate and
were never cut.  A ``.translate((0, 0, 0))`` that did nothing, leaving two arms
crossed at the origin instead of meeting as an L.  A hexagon built across
corners when the prompt said across the flats.

None of those are expressible here.  A position is a named parameter measured
from a face's own centre, and where the frame sits in kernel space is the
compiler's business.  ``across_flats`` is the parameter's name, so the term
cannot be misread.  There is no free-form transform to emit.

Each operation declares its parameters, what extents it produces, what would
make it impossible, and how to write it as CadQuery.  The extents are what let
a hole be checked against the stock it is cut from before the kernel runs at
all - which is milliseconds, against seconds for a build and minutes for a
refinement round that discovers the same thing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: Anything smaller than this is not a dimension the kernel can build.
MIN_SIZE = 1e-3


class GraphError(ValueError):
    """The graph does not describe a buildable part."""


@dataclass
class Node:
    id: str
    type: str
    params: dict
    parent: Optional[str] = None

    def get(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    def number(self, name: str, default: Optional[float] = None) -> float:
        raw = self.params.get(name, default)
        if raw is None:
            raise GraphError(f"{self.id}: '{name}' is required.")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise GraphError(
                f"{self.id}: '{name}' must be a number, got {raw!r}.") from None
        if not math.isfinite(value):
            raise GraphError(f"{self.id}: '{name}' is not a finite number.")
        return value

    def size(self, name: str, default: Optional[float] = None) -> float:
        value = self.number(name, default)
        if value < MIN_SIZE:
            raise GraphError(
                f"{self.id}: '{name}' is {value:g}mm, which is too small to "
                f"build. Dimensions are in millimetres.")
        return value


@dataclass
class Ctx:
    """What an operation knows about the solid it is acting on.

    ``extents`` is the parent's overall size, for checking. ``vars`` is the
    *names* the parent declared those extents under, so a derived position can
    be written as arithmetic on them rather than baked to a literal. That is
    the difference between a script that happens to be right and one that
    stays right when a dimension changes.
    """

    extents: Optional[tuple] = None
    vars: Optional[tuple] = None      # variable names for (x, y, z)


@dataclass
class Op:
    """One operation: what it needs, what it makes, how it is written."""

    summary: str
    #: Parameter names that must be present.
    required: tuple[str, ...] = ()
    #: Whether this operation modifies an earlier node rather than starting one.
    needs_parent: bool = False
    #: (node, extents) -> the extents of the result, or None if unchanged.
    extents: Optional[Callable[[Node, tuple], Optional[tuple]]] = None
    #: (node, ctx) -> reasons this cannot be built.
    check: Optional[Callable[[Node, "Ctx"], list[str]]] = None
    #: (node, ctx) -> CadQuery lines.
    emit: Callable[[Node, "Ctx"], list[str]] = field(
        default=lambda node, ctx: [])
    #: Variable names this operation declares for its (x, y, z) extents.
    names: Optional[Callable[[Node], tuple]] = None
    #: Parameters that must be finite numbers whenever they are present.
    #: Checked while parsing, so a graph carrying "wide" where a width
    #: belongs is refused before anything tries to build it.
    numeric: tuple[str, ...] = ()


def _n(value: float) -> str:
    """A dimension, always with a decimal point.

    The app's parameter editor treats an assignment with no decimal point as a
    count and rounds edits to whole numbers, which would quietly turn an 8.4mm
    clearance hole into 8mm.
    """
    text = f"{float(value):g}"
    return text if ("." in text or "e" in text) else text + ".0"


# ---------------------------------------------------------------------------
# Solids
# ---------------------------------------------------------------------------

def _box_emit(node: Node, _ctx: "Ctx") -> list[str]:
    x, y, z = node.size("x"), node.size("y"), node.size("z")
    return [
        f"length = {_n(x)}",
        f"width = {_n(y)}",
        f"thickness = {_n(z)}",
        "",
        'result = cq.Workplane("XY").box(length, width, thickness)',
    ]


def _cylinder_emit(node: Node, _ctx: "Ctx") -> list[str]:
    return [
        f"diameter = {_n(node.size('diameter'))}",
        f"height = {_n(node.size('height'))}",
        "",
        'result = cq.Workplane("XY").circle(diameter / 2).extrude(height)',
    ]


def _prism_emit(node: Node, _ctx: "Ctx") -> list[str]:
    sides = int(node.number("sides"))
    height = node.size("height")
    across_flats = node.get("across_flats")
    if across_flats is not None:
        # CadQuery's polygon() takes the circumscribed diameter - across the
        # corners. A prompt that says "across the flats" means the other one,
        # and a model asked to write this directly has been observed getting
        # it backwards. Converting here means the distinction is made once.
        flats = node.size("across_flats")
        corners = flats / math.cos(math.pi / sides)
        head = [f"across_flats = {_n(flats)}",
                f"sides = {sides}",
                "# polygon() takes the across-corners diameter",
                "across_corners = across_flats / math.cos(math.pi / sides)"]
        diameter = "across_corners"
    else:
        corners = node.size("across_corners")
        head = [f"across_corners = {_n(corners)}", f"sides = {sides}"]
        diameter = "across_corners"
    return head + [
        f"height = {_n(height)}",
        "",
        f'result = (cq.Workplane("XY")',
        f"          .polygon(sides, {diameter})",
        "          .extrude(height))",
    ]


def _prism_extents(node: Node, _parent: tuple) -> tuple:
    sides = int(node.number("sides"))
    if node.get("across_flats") is not None:
        corners = node.size("across_flats") / math.cos(math.pi / sides)
    else:
        corners = node.size("across_corners")
    return (corners, corners, node.size("height"))


def _prism_check(node: Node, _ctx: "Ctx") -> list[str]:
    sides = node.number("sides")
    if sides != int(sides) or sides < 3:
        return [f"{node.id}: a prism needs at least 3 whole sides, "
                f"got {sides:g}."]
    if (node.get("across_flats") is None) == (
            node.get("across_corners") is None):
        return [f"{node.id}: give exactly one of across_flats or "
                f"across_corners, so the size cannot be misread."]
    return []


# ---------------------------------------------------------------------------
# Holes
# ---------------------------------------------------------------------------

_FACES = {"top": '">Z"', "bottom": '"<Z"',
          "front": '"<Y"', "back": '">Y"',
          "left": '"<X"', "right": '">X"'}

#: Which two extents of the parent lie in the plane of each face, and which is
#: the depth through it.
_FACE_PLANE = {"top": (0, 1, 2), "bottom": (0, 1, 2),
               "front": (0, 2, 1), "back": (0, 2, 1),
               "left": (1, 2, 0), "right": (1, 2, 0)}


def _positions(node: Node, parent: Optional[tuple]) -> list[tuple[float, float]]:
    """Where the holes go, in the face's own frame, measured from its centre.

    The model never states a kernel coordinate. It says "10mm in from each
    corner", or gives offsets from the middle of the face, and the arithmetic
    that turns either into a position on a centred workplane happens here.
    """
    face = str(node.get("face", "top"))
    pattern = str(node.get("pattern", "at"))

    if pattern == "at":
        # Models write a single hole several equally sensible ways: a list of
        # positions, one position, a bare x and y, or - for a bore on the
        # axis - nothing at all. Refusing three of those four buys nothing;
        # what matters is that whatever is given is measured from the centre
        # of the face, which no spelling of it can change.
        raw = node.get("positions")
        if raw is None and node.get("position") is not None:
            raw = [node.get("position")]
        if raw is None and (node.get("x") is not None
                            or node.get("y") is not None):
            raw = [[node.get("x", 0.0), node.get("y", 0.0)]]
        if raw is None:
            return [(0.0, 0.0)]        # a hole with no position is central
        if not isinstance(raw, (list, tuple)) or not raw:
            raise GraphError(
                f"{node.id}: 'positions' must be a non-empty list, each entry "
                f"[x, y] measured from the centre of the {face} face.")
        out: list[tuple[float, float]] = []
        for entry in raw:
            if isinstance(entry, dict):
                # {"x": ..., "y": ...} is as clear as [x, y] and models write
                # it about as often.
                entry = [entry.get("x", 0.0), entry.get("y", 0.0)]
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise GraphError(
                    f"{node.id}: every position must be [x, y] measured from "
                    f"the centre of the {face} face, got {entry!r}.")
            try:
                out.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                raise GraphError(
                    f"{node.id}: position {entry!r} is not two numbers."
                ) from None
        return out

    if parent is None:
        raise GraphError(
            f"{node.id}: pattern {pattern!r} is measured from the part it cuts "
            f"into, so it needs a parent.")
    u, v, _ = _FACE_PLANE.get(face, (0, 1, 2))
    half_u, half_v = parent[u] / 2, parent[v] / 2

    if pattern == "rect_corners":
        inset_u = node.size("inset_x")
        inset_v = node.size("inset_y", inset_u)
        return [(su * (half_u - inset_u), sv * (half_v - inset_v))
                for su in (-1, 1) for sv in (-1, 1)]

    if pattern == "linear":
        count = int(node.number("count"))
        spacing = node.size("spacing")
        along_y = str(node.get("along", "x")).lower() == "y"
        start = -(count - 1) * spacing / 2
        return [((0.0, start + i * spacing) if along_y
                 else (start + i * spacing, 0.0)) for i in range(count)]

    if pattern == "circular":
        count = int(node.number("count"))
        radius = node.size("bolt_circle_diameter") / 2
        start = math.radians(float(node.get("start_angle", 0.0) or 0.0))
        return [(radius * math.cos(start + 2 * math.pi * i / count),
                 radius * math.sin(start + 2 * math.pi * i / count))
                for i in range(count)]

    raise GraphError(
        f"{node.id}: unknown pattern {pattern!r}. Use at, rect_corners, "
        f"linear or circular.")


def _hole_check(node: Node, ctx: "Ctx") -> list[str]:
    parent = ctx.extents
    face = str(node.get("face", "top"))
    if face not in _FACES:
        return [f"{node.id}: face must be one of "
                f"{', '.join(sorted(_FACES))}, got {face!r}."]
    if str(node.get("pattern", "at")) in ("linear", "circular"):
        count = node.number("count")
        if count != int(count) or count < 1:
            return [f"{node.id}: count must be a whole number of at least 1."]

    problems: list[str] = []
    radius = node.size("diameter") / 2
    points = _positions(node, parent)

    if parent is not None:
        u, v, depth_axis = _FACE_PLANE.get(face, (0, 1, 2))
        half_u, half_v = parent[u] / 2, parent[v] / 2
        for x, y in points:
            # This is the check that would have caught the plate: the model
            # asked for holes at (70, 50) on a part spanning +/-40 by +/-30,
            # so three of the four were cut in empty space and the part came
            # back with one hole, which the vision Judge then passed.
            if abs(x) + radius > half_u + 1e-9 or abs(y) + radius > half_v + 1e-9:
                problems.append(
                    f"{node.id}: a hole at ({x:g}, {y:g}) with diameter "
                    f"{radius * 2:g} does not fit on a face "
                    f"{parent[u]:g} x {parent[v]:g}. Positions are measured "
                    f"from the centre of the face.")
                break
        depth = node.get("depth")
        if depth is not None and node.size("depth") > parent[depth_axis] + 1e-9:
            problems.append(
                f"{node.id}: a {node.size('depth'):g}mm deep hole is deeper "
                f"than the {parent[depth_axis]:g}mm it is cut into. Leave "
                f"depth out for a through hole.")

    seen: set[tuple[float, float]] = set()
    for point in points:
        key = (round(point[0], 6), round(point[1], 6))
        if key in seen:
            problems.append(
                f"{node.id}: two holes at the same place ({key[0]:g}, "
                f"{key[1]:g}).")
            break
        seen.add(key)
    return problems


def _hole_emit(node: Node, ctx: "Ctx") -> list[str]:
    """Write the holes, keeping derived positions as arithmetic.

    A pattern expressed against the parent's size - four holes 10mm in from
    the corners - is emitted as an expression over the variables the parent
    declared, not as the four points that arithmetic happens to produce now.
    So widening the plate moves the holes with it, which is the whole reason
    to have a graph rather than a snapshot of one.
    """
    face_key = str(node.get("face", "top"))
    face = _FACES[face_key]
    pattern = str(node.get("pattern", "at"))
    prefix = node.id if "hole" in node.id.lower() else f"{node.id}_hole"

    lines = [f"{prefix}_diameter = {_n(node.size('diameter'))}"]
    depth = node.get("depth")
    if depth is not None:
        lines.append(f"{prefix}_depth = {_n(node.size('depth'))}")

    u_axis, v_axis, _ = _FACE_PLANE.get(face_key, (0, 1, 2))
    names = ctx.vars
    half_u = f"{names[u_axis]} / 2" if names else None
    half_v = f"{names[v_axis]} / 2" if names else None

    if pattern == "rect_corners" and half_u:
        inset_u = node.size("inset_x")
        inset_v = node.size("inset_y", inset_u)
        lines += [f"{prefix}_inset_x = {_n(inset_u)}",
                  f"{prefix}_inset_y = {_n(inset_v)}", ""]
        points = (f"[(sx * ({half_u} - {prefix}_inset_x), "
                  f"sy * ({half_v} - {prefix}_inset_y))"
                  f" for sx in (-1, 1) for sy in (-1, 1)]")
    elif pattern == "circular":
        count = int(node.number("count"))
        lines += [f"{prefix}_count = {count}",
                  f"{prefix}_pcd = {_n(node.size('bolt_circle_diameter'))}",
                  f"{prefix}_start = {_n(float(node.get('start_angle', 0) or 0))}",
                  ""]
        points = (f"[({prefix}_pcd / 2 * math.cos(math.radians({prefix}_start)"
                  f" + 2 * math.pi * i / {prefix}_count),"
                  f" {prefix}_pcd / 2 * math.sin(math.radians({prefix}_start)"
                  f" + 2 * math.pi * i / {prefix}_count))"
                  f" for i in range({prefix}_count)]")
    elif pattern == "linear":
        count = int(node.number("count"))
        along_y = str(node.get("along", "x")).lower() == "y"
        lines += [f"{prefix}_count = {count}",
                  f"{prefix}_spacing = {_n(node.size('spacing'))}", ""]
        step = (f"-({prefix}_count - 1) * {prefix}_spacing / 2"
                f" + i * {prefix}_spacing")
        points = (f"[(0.0, {step}) for i in range({prefix}_count)]" if along_y
                  else f"[({step}, 0.0) for i in range({prefix}_count)]")
    else:
        # Positions given outright stay literal: they were stated, not derived.
        lines.append("")
        points = "[" + ", ".join(
            f"({_n(x)}, {_n(y)})" for x, y in _positions(node, ctx.extents)
        ) + "]"

    call = (f".hole({prefix}_diameter)" if depth is None
            else f".hole({prefix}_diameter, {prefix}_depth)")
    lines += [
        f"result = (result.faces({face})",
        "          .workplane()",
        f"          .pushPoints({points})",
        f"          {call})",
    ]
    return lines


# ---------------------------------------------------------------------------
# Edge treatments and shelling
# ---------------------------------------------------------------------------

_EDGES = {"all": "", "vertical": '"|Z"', "top": '">Z"', "bottom": '"<Z"',
          "horizontal": '"#Z"'}


def _edge_check(node: Node, ctx: "Ctx") -> list[str]:
    parent = ctx.extents
    which = str(node.get("edges", "all"))
    if which not in _EDGES:
        return [f"{node.id}: edges must be one of "
                f"{', '.join(sorted(_EDGES))}, got {which!r}."]
    key = "radius" if node.type == "fillet" else "distance"
    size = node.size(key)
    if parent is not None and size * 2 > min(parent) + 1e-9:
        return [f"{node.id}: a {size:g}mm {key} does not fit on a part whose "
                f"smallest dimension is {min(parent):g}mm."]
    return []


def _edge_emit(node: Node, _ctx: "Ctx") -> list[str]:
    which = _EDGES[str(node.get("edges", "all"))]
    key = "radius" if node.type == "fillet" else "distance"
    name = f"{node.id}_{key}"
    selector = f".edges({which})" if which else ".edges()"
    return [
        f"{name} = {_n(node.size(key))}",
        "",
        f"result = result{selector}.{node.type}({name})",
    ]


def _shell_check(node: Node, ctx: "Ctx") -> list[str]:
    parent = ctx.extents
    thickness = node.size("thickness")
    face = str(node.get("open_face", "top"))
    if face not in _FACES:
        return [f"{node.id}: open_face must be one of "
                f"{', '.join(sorted(_FACES))}, got {face!r}."]
    if parent is not None and thickness * 2 >= min(parent):
        return [f"{node.id}: a {thickness:g}mm wall leaves nothing hollow in a "
                f"part {min(parent):g}mm across."]
    return []


def _shell_emit(node: Node, _ctx: "Ctx") -> list[str]:
    face = _FACES[str(node.get("open_face", "top"))]
    return [
        f"wall = {_n(node.size('thickness'))}",
        "",
        f"result = result.faces({face}).shell(-wall)",
    ]


REGISTRY: dict[str, Op] = {
    "box": Op(
        summary="A rectangular block, centred on the origin.",
        required=("x", "y", "z"),
        extents=lambda n, _p: (n.size("x"), n.size("y"), n.size("z")),
        names=lambda _n: ("length", "width", "thickness"),
        emit=_box_emit,
        numeric=("x", "y", "z"),
    ),
    "cylinder": Op(
        summary="A cylinder standing on Z.",
        required=("diameter", "height"),
        extents=lambda n, _p: (n.size("diameter"), n.size("diameter"),
                               n.size("height")),
        names=lambda _n: ("diameter", "diameter", "height"),
        emit=_cylinder_emit,
        numeric=("diameter", "height"),
    ),
    "prism": Op(
        summary="A regular prism. Give across_flats or across_corners.",
        required=("sides", "height"),
        extents=_prism_extents,
        names=lambda _n: ("across_corners", "across_corners",
                          "height"),
        check=_prism_check,
        emit=_prism_emit,
        numeric=("sides", "height", "across_flats", "across_corners"),
    ),
    "hole": Op(
        summary=("Holes through or into a face. Positions are measured from "
                 "the centre of that face."),
        required=("diameter",),
        needs_parent=True,
        check=_hole_check,
        emit=_hole_emit,
        numeric=("diameter", "depth", "inset_x", "inset_y", "count",
                 "spacing", "bolt_circle_diameter", "start_angle", "x", "y"),
    ),
    "fillet": Op(
        summary="Round the selected edges.",
        required=("radius",),
        needs_parent=True,
        check=_edge_check,
        emit=_edge_emit,
        numeric=("radius",),
    ),
    "chamfer": Op(
        summary="Chamfer the selected edges.",
        required=("distance",),
        needs_parent=True,
        check=_edge_check,
        emit=_edge_emit,
        numeric=("distance",),
    ),
    "shell": Op(
        summary="Hollow the part, opening one face.",
        required=("thickness",),
        needs_parent=True,
        check=_shell_check,
        emit=_shell_emit,
        numeric=("thickness",),
    ),
    "raw_script": Op(
        summary=("An escape hatch for geometry the vocabulary cannot express. "
                 "The script must assign to `result`, and nothing above can "
                 "check it."),
        required=("code",),
        emit=lambda n, _c: str(n.get("code", "")).splitlines(),
    ),
}
