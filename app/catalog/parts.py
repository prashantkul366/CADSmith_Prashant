"""Standard hardware, generated parametrically as CadQuery source.

The point of generating rather than importing: what comes out is *code*, with
the standard's dimensions as named assignments at the top.  So the app's code
panel shows real numbers, the parameter-patch editor can rewrite them, and
the Refiner can restructure the part - none of which is possible with a
downloaded STEP, which is a frozen shape with no history and no parameters.

    from app.catalog import parts
    screw = parts.socket_head_cap_screw("M8", 30)
    print(screw.code)        # standalone CadQuery, assigns `result`

Threads are not modelled.  A swept helix on a single M8x30 measured 283x the
build time and 224x the STEP size of a plain shank (2.5s and 1.3MB for one
screw), which is why every production CAD library shows fasteners with plain
shanks too.  Pass ``threaded=True`` if you want the real helix for a hero
render and can pay for it.

Origins are chosen so the part lands where it belongs when translated to a
feature: screws and bolts sit with the *bearing face under the head* at
z=0, everything else on its own base at z=0, axis along +Z.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.catalog import standards


@dataclass(frozen=True)
class CatalogPart:
    id: str
    title: str
    standard: str
    code: str
    parameters: dict[str, float] = field(default_factory=dict)


def _n(value: float) -> str:
    """Format a dimension, always with a decimal point.

    Every number here is a length, and the app's parameter editor treats an
    assignment with no decimal point as a count and rounds edits to whole
    numbers - which would quietly turn an 8.4mm clearance hole into 8mm.
    """
    text = f"{float(value):g}"
    return text if "." in text or "e" in text else text + ".0"


def _across_corners(across_flats: float) -> float:
    """CadQuery's polygon() takes the circumscribed circle diameter."""
    return across_flats * 2.0 / 3.0 ** 0.5


_THREAD_SNIPPET = """
# A real helical thread. Expensive - see the note in app/catalog/parts.py.
helix = cq.Wire.makeHelix(pitch=pitch, height=length, radius=thread_diameter / 2.0)
crest = (
    cq.Workplane('XZ').center(thread_diameter / 2.0, 0)
    .polyline([(0, -pitch / 2.0), (0, pitch / 2.0), (-thread_depth, 0)])
    .close()
)
result = result.union(crest.sweep(cq.Workplane(helix), isFrenet=True))
"""


def socket_head_cap_screw(size: str = "M8", length: float = 30.0,
                          threaded: bool = False) -> CatalogPart:
    """ISO 4762 socket head cap screw - the workhorse of machine design."""
    key = standards._normalise(size)
    spec = standards.ISO_4762[key]
    thread = standards.THREADS[key]
    # ISO 4762 tables a minimum key engagement; a little over half the head
    # height is the usual simplification and always clears the minimum.
    socket_depth = round(spec.head_height * 0.55, 2)

    core = f"""import cadquery as cq

# {key} x {_n(length)} socket head cap screw, ISO 4762
# The bearing face under the head sits at z = 0, so translating this to a
# hole position drops the screw straight into place.
thread_diameter = {_n(thread.diameter)}
length = {_n(length)}                 # under the head, as the standard measures it
pitch = {_n(thread.pitch)}
head_diameter = {_n(spec.head_diameter)}
head_height = {_n(spec.head_height)}
socket_across_flats = {_n(spec.socket_across_flats)}
socket_depth = {_n(socket_depth)}
thread_depth = {_n(round(thread.pitch * 0.54, 3))}

# Shank, hanging below the bearing face
result = cq.Workplane('XY').circle(thread_diameter / 2.0).extrude(-length)

# Head, with the small chamfer the standard puts on its top edge
head = (
    cq.Workplane('XY').circle(head_diameter / 2.0).extrude(head_height)
    .edges('>Z').chamfer(head_diameter * 0.03)
)
result = result.union(head)

# Hex socket for the key
socket = (
    cq.Workplane('XY').workplane(offset=head_height)
    .polygon(6, {_n(round(_across_corners(spec.socket_across_flats), 4))})
    .extrude(-socket_depth)
)
result = result.cut(socket)
"""
    return CatalogPart(
        id=f"iso4762_{key.lower()}x{_n(length)}",
        title=f"Socket head cap screw {key} x {_n(length)}",
        standard="ISO 4762",
        code=core + (_THREAD_SNIPPET if threaded else ""),
        parameters={"thread_diameter": thread.diameter, "length": length,
                    "head_diameter": spec.head_diameter,
                    "head_height": spec.head_height},
    )


def hex_bolt(size: str = "M8", length: float = 30.0) -> CatalogPart:
    """ISO 4014 hex head bolt."""
    key = standards._normalise(size)
    spec = standards.ISO_4014[key]
    thread = standards.THREADS[key]

    code = f"""import cadquery as cq

# {key} x {_n(length)} hex head bolt, ISO 4014
# Bearing face under the head at z = 0.
thread_diameter = {_n(thread.diameter)}
length = {_n(length)}
head_across_flats = {_n(spec.across_flats)}
head_height = {_n(spec.height)}

result = cq.Workplane('XY').circle(thread_diameter / 2.0).extrude(-length)

head = (
    cq.Workplane('XY')
    .polygon(6, {_n(round(_across_corners(spec.across_flats), 4))})
    .extrude(head_height)
)
result = result.union(head)

# Chamfer the corners off the top of the head, as forging leaves them
result = result.faces('>Z').chamfer(head_across_flats * 0.06)
"""
    return CatalogPart(
        id=f"iso4014_{key.lower()}x{_n(length)}",
        title=f"Hex head bolt {key} x {_n(length)}",
        standard="ISO 4014", code=code,
        parameters={"thread_diameter": thread.diameter, "length": length,
                    "head_across_flats": spec.across_flats,
                    "head_height": spec.height},
    )


def hex_nut(size: str = "M8") -> CatalogPart:
    """ISO 4032 hex nut.

    The bore is the nominal diameter rather than the thread's minor
    diameter, so a nut and its screw sit tangent in an assembly instead of
    interfering. That is the usual simplification for unthreaded hardware.
    """
    key = standards._normalise(size)
    spec = standards.ISO_4032[key]
    thread = standards.THREADS[key]

    code = f"""import cadquery as cq

# {key} hex nut, ISO 4032
across_flats = {_n(spec.across_flats)}
thickness = {_n(spec.height)}
bore_diameter = {_n(thread.diameter)}

result = (
    cq.Workplane('XY')
    .polygon(6, {_n(round(_across_corners(spec.across_flats), 4))})
    .extrude(thickness)
)

# Both faces are chamfered on a real nut
result = result.faces('>Z').chamfer(across_flats * 0.05)
result = result.faces('<Z').chamfer(across_flats * 0.05)

result = (
    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')
    .circle(bore_diameter / 2.0).cutThruAll()
)
"""
    return CatalogPart(
        id=f"iso4032_{key.lower()}", title=f"Hex nut {key}",
        standard="ISO 4032", code=code,
        parameters={"across_flats": spec.across_flats,
                    "thickness": spec.height,
                    "bore_diameter": thread.diameter},
    )


def flat_washer(size: str = "M8") -> CatalogPart:
    """ISO 7089 plain washer."""
    key = standards._normalise(size)
    spec = standards.ISO_7089[key]

    code = f"""import cadquery as cq

# {key} plain washer, ISO 7089
inner_diameter = {_n(spec.inner_diameter)}
outer_diameter = {_n(spec.outer_diameter)}
thickness = {_n(spec.thickness)}

result = (
    cq.Workplane('XY')
    .circle(outer_diameter / 2.0)
    .circle(inner_diameter / 2.0)
    .extrude(thickness)
)
"""
    return CatalogPart(
        id=f"iso7089_{key.lower()}", title=f"Plain washer {key}",
        standard="ISO 7089", code=code,
        parameters={"inner_diameter": spec.inner_diameter,
                    "outer_diameter": spec.outer_diameter,
                    "thickness": spec.thickness},
    )


def ball_bearing(designation: str = "6203") -> CatalogPart:
    """A deep groove ball bearing, simplified to a single solid.

    A real bearing is several bodies that move relative to each other, which
    is neither watertight nor useful to a kernel check. Catalogues ship a
    'simplified' envelope for exactly this reason: the bore, outside
    diameter and width are exact, so fit and clearance still check out, and
    a shallow recess on each face reads as the ring split.
    """
    key = str(designation).strip()
    if key not in standards.BEARINGS:
        raise KeyError(f"No bearing '{designation}'. "
                       f"Known: {', '.join(standards.BEARINGS)}")
    spec = standards.BEARINGS[key]
    band = (spec.outer_diameter - spec.bore) * 0.25

    code = f"""import cadquery as cq

# {key} deep groove ball bearing (simplified envelope), ISO 15
bore = {_n(spec.bore)}
outer_diameter = {_n(spec.outer_diameter)}
width = {_n(spec.width)}
ring_band = {_n(round(band, 3))}      # radial thickness of each ring
recess_depth = {_n(round(spec.width * 0.12, 3))}

result = (
    cq.Workplane('XY')
    .circle(outer_diameter / 2.0)
    .circle(bore / 2.0)
    .extrude(width)
)

# A shallow annular recess on each face, between the two rings, so the part
# reads as a bearing rather than a plain spacer.
#
# Both tools are extruded upward and overshoot the face they cut. An annular
# profile extruded downward from an offset plane comes out an invalid solid
# in this OCCT build, and cutting with it silently leaves the bearing
# unwatertight.
overshoot = 1.0
for base in (-overshoot, width - recess_depth):
    recess = (
        cq.Workplane('XY').workplane(offset=base)
        .circle(outer_diameter / 2.0 - ring_band)
        .circle(bore / 2.0 + ring_band)
        .extrude(recess_depth + overshoot)
    )
    result = result.cut(recess)
"""
    return CatalogPart(
        id=f"bearing_{key}", title=f"Deep groove ball bearing {key}",
        standard="ISO 15", code=code,
        parameters={"bore": spec.bore,
                    "outer_diameter": spec.outer_diameter,
                    "width": spec.width},
    )


def o_ring(inner_diameter: float = 20.0, cord: float = 2.5) -> CatalogPart:
    """An O-ring, specified the way they are sold: inside diameter x cord."""
    mean = inner_diameter + cord

    code = f"""import cadquery as cq

# O-ring, {_n(inner_diameter)} ID x {_n(cord)} cord
inner_diameter = {_n(inner_diameter)}
cord_diameter = {_n(cord)}
mean_diameter = {_n(mean)}    # ID + cord: the circle the cord centre follows

# revolve() cannot close a full circular profile in this OCCT build - it
# raises StdFail_NotDone - so the torus is constructed directly. The volume
# matches Pappus exactly either way.
result = cq.Workplane(obj=cq.Solid.makeTorus(
    mean_diameter / 2.0, cord_diameter / 2.0))
"""
    return CatalogPart(
        id=f"oring_{_n(inner_diameter)}x{_n(cord)}",
        title=f"O-ring {_n(inner_diameter)} ID x {_n(cord)} cord",
        standard="ISO 3601", code=code,
        parameters={"inner_diameter": inner_diameter, "cord_diameter": cord},
    )


def dowel_pin(diameter: float = 6.0, length: float = 20.0) -> CatalogPart:
    """ISO 2338 parallel dowel pin, with the chamfered ends it is ground with."""
    chamfer = round(min(diameter * 0.12, 0.8), 2)

    code = f"""import cadquery as cq

# Parallel dowel pin, {_n(diameter)} x {_n(length)}, ISO 2338
diameter = {_n(diameter)}
length = {_n(length)}
end_chamfer = {_n(chamfer)}

result = cq.Workplane('XY').circle(diameter / 2.0).extrude(length)
result = result.faces('>Z').chamfer(end_chamfer)
result = result.faces('<Z').chamfer(end_chamfer)
"""
    return CatalogPart(
        id=f"dowel_{_n(diameter)}x{_n(length)}",
        title=f"Dowel pin {_n(diameter)} x {_n(length)}",
        standard="ISO 2338", code=code,
        parameters={"diameter": diameter, "length": length},
    )


# ---------------------------------------------------------------------------
# Picking a part out of something someone typed.

# A \b after the digits fails on "M8x30", where a word character follows
# a word character and there is no boundary at all. A negative lookahead
# for more digits is what is actually meant.
# Nouns that mean "this is a part to be designed", not a catalogue lookup.
_CUSTOM_CONTEXT = (
    "housing", "bracket", "mount", "holder", "carrier", "enclosure",
    "manifold", "adapter", "adaptor", "fixture", "jig", "puller",
    "block for", "plate for", "body for", "seat for", "cover for",
)

_SIZE = re.compile(r"\bM\s?(\d+(?:\.\d+)?)(?![\d.])", re.I)
_LENGTH = re.compile(r"\bM\s?\d+(?:\.\d+)?\s*[x×*]\s*(\d+(?:\.\d+)?)", re.I)
_BEARING = re.compile(r"\b(6\d{3}|608)\b")
_ORING = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:id|inside)?\s*[x×]\s*"
                    r"(\d+(?:\.\d+)?)", re.I)
_PIN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×]\s*(\d+(?:\.\d+)?)", re.I)
_LENGTH_WORDS = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s+long", re.I)


def select(text: str) -> CatalogPart | None:
    """The standard part someone asked for, or None if this is a custom part.

    Deliberately strict, unlike the mock provider's forgiving part matcher:
    a wrong guess here silently hands back the wrong hardware, and a custom
    bracket that merely *mentions* an M8 screw must still be generated, not
    substituted. Only an unambiguous designation matches.
    """
    lowered = text.lower()

    # A request naming a standard part inside a bigger noun is asking for the
    # bigger noun: "a bearing housing for a 6203" wants the housing, and the
    # bearing is what goes in it. Handing back the bearing would be silently
    # wrong, so custom context wins over any designation in the text.
    if any(word in lowered for word in _CUSTOM_CONTEXT):
        return None

    if "bearing" in lowered:
        found = _BEARING.search(text)
        if found and found.group(1) in standards.BEARINGS:
            return ball_bearing(found.group(1))
        return None

    if "o-ring" in lowered or "o ring" in lowered or "oring" in lowered:
        found = _ORING.search(text)
        if found:
            return o_ring(float(found.group(1)), float(found.group(2)))
        return None

    if "dowel" in lowered:
        found = _PIN.search(text)
        if found:
            return dowel_pin(float(found.group(1)), float(found.group(2)))
        return None

    size_match = _SIZE.search(text)
    if not size_match:
        return None
    try:
        size = standards._normalise(size_match.group(1))
    except KeyError:
        return None

    length_match = _LENGTH.search(text) or _LENGTH_WORDS.search(text)
    length = float(length_match.group(1)) if length_match else None

    if "washer" in lowered:
        return flat_washer(size) if size in standards.ISO_7089 else None
    if "nut" in lowered:
        return hex_nut(size) if size in standards.ISO_4032 else None
    if any(word in lowered for word in
           ("cap screw", "socket head", "shcs", "allen", "hex socket")):
        if size in standards.ISO_4762:
            return socket_head_cap_screw(size, length or size_lengths(size))
        return None
    if "hex" in lowered and ("bolt" in lowered or "screw" in lowered):
        if size in standards.ISO_4014:
            return hex_bolt(size, length or size_lengths(size))
        return None
    if "bolt" in lowered or "screw" in lowered:
        # Unqualified: a cap screw is the common case in machine design.
        if size in standards.ISO_4762:
            return socket_head_cap_screw(size, length or size_lengths(size))
    return None


def size_lengths(size: str) -> float:
    """A sensible default length when someone names a size but no length."""
    diameter = standards.THREADS[standards._normalise(size)].diameter
    return round(diameter * 3.0, 1)


BUILDERS = {
    "socket_head_cap_screw": socket_head_cap_screw,
    "hex_bolt": hex_bolt,
    "hex_nut": hex_nut,
    "flat_washer": flat_washer,
    "ball_bearing": ball_bearing,
    "o_ring": o_ring,
    "dowel_pin": dowel_pin,
}
