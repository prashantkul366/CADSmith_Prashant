"""Standard parts backed by cq_gears and cq_warehouse.

Two Apache-2.0 libraries do a better job than hand-written geometry for the
families they cover, so those families come from them:

    cq_gears       spur, helical, herringbone, ring, rack and bevel gears -
                   real involute flanks, which is exactly the geometry a
                   language model reliably gets wrong.
    cq_warehouse   12 screw head types and 7 nut types across M1.6-M64,
                   sprockets, roller chain and ISO threads.

Both are optional.  When neither is installed the catalogue still works from
``parts.py`` alone, with narrower coverage - a demo should not die because a
git dependency moved.

What stays in ``parts.py``, on evidence rather than preference:

    washers    cq_warehouse 0.8.0 returns every washer, in all four
               standards, as a non-closed shell with roughly twice the
               correct volume. Ours is exact and valid.
    bearings   cq_warehouse carries 31 sizes, but only 4 of the 20 ISO 15
               designations people actually ask for; it has no 6203.

Emitted code names its dimensions, so the parameter editor can rewrite them
and the Refiner can restructure around them.  The fastener designation is a
string rather than a number, so "make it 40mm long" is patchable but "make it
an M10" is not - that one goes to the Refiner.
"""

from __future__ import annotations

from app.catalog import standards
from app.catalog.parts import CatalogPart, _n

try:  # optional: gears
    import cq_gears  # noqa: F401
    HAVE_GEARS = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_GEARS = False

try:  # optional: fasteners, sprockets, threads
    import cq_warehouse.fastener  # noqa: F401
    HAVE_WAREHOUSE = True
except Exception:  # pragma: no cover
    HAVE_WAREHOUSE = False


class NotAvailable(RuntimeError):
    """The library backing this part is not installed."""


# ---------------------------------------------------------------------------
# Gears
# ---------------------------------------------------------------------------

_GEAR_HEADER = """import cadquery as cq
from cq_gears import {cls}

# {title}
# Pitch diameter is module x teeth = {pitch:g} mm; tip diameter adds two
# modules for the addendum, giving {tip:g} mm.
"""


def _gear(cls, title, params, extra_lines=(), bore=None):
    # Keys starting with an underscore are metadata for the header comment,
    # not constructor arguments - strip them here rather than at every call
    # site, where forgetting once puts `_pitch=40.0` into the emitted code.
    body = ",\n".join(f"    {k}={v}" for k, v in params.items()
                      if not k.startswith("_"))
    code = (_GEAR_HEADER.format(cls=cls, title=title,
                                pitch=params.get("_pitch", 0),
                                tip=params.get("_tip", 0))
            + "\n".join(extra_lines) + "\n\n"
            + f"gear = {cls}(\n{body},\n)\n"
            + "result = cq.Workplane('XY').gear(gear)\n")
    return code


def spur_gear(module: float = 2.0, teeth: int = 20, face_width: float = 10.0,
              bore: float = 8.0, pressure_angle: float = 20.0,
              helix_angle: float = 0.0) -> CatalogPart:
    """An involute spur gear, or a helical one with a helix angle."""
    if not HAVE_GEARS:
        raise NotAvailable("cq_gears is not installed")
    helical = abs(helix_angle) > 1e-9
    pitch_d = module * teeth
    tip_d = pitch_d + 2 * module
    kind = "helical" if helical else "spur"
    title = (f"{kind} gear, module {module:g}, {teeth} teeth"
             + (f", {helix_angle:g} degree helix" if helical else ""))
    lines = [
        f"module = {_n(module)}",
        f"teeth_number = {teeth}",
        f"face_width = {_n(face_width)}",
        f"pressure_angle = {_n(pressure_angle)}",
    ]
    params = {"module": "module", "teeth_number": "teeth_number",
              "width": "face_width", "pressure_angle": "pressure_angle"}
    if helical:
        lines.append(f"helix_angle = {_n(helix_angle)}")
        params["helix_angle"] = "helix_angle"
    if bore:
        lines.append(f"bore_diameter = {_n(bore)}")
        params["bore_d"] = "bore_diameter"
    params["_pitch"], params["_tip"] = pitch_d, tip_d
    code = _gear("SpurGear", title, params, lines)
    return CatalogPart(
        id=f"gear_{kind}_m{module:g}_z{teeth}",
        title=title.capitalize(), standard="ISO 53 involute profile",
        code=code,
        parameters={"module": module, "teeth": teeth,
                    "face_width": face_width, "bore": bore,
                    "pitch_diameter": pitch_d, "tip_diameter": tip_d})


def herringbone_gear(module: float = 2.0, teeth: int = 20,
                     face_width: float = 14.0, bore: float = 8.0,
                     helix_angle: float = 25.0) -> CatalogPart:
    if not HAVE_GEARS:
        raise NotAvailable("cq_gears is not installed")
    pitch_d, tip_d = module * teeth, module * teeth + 2 * module
    lines = [f"module = {_n(module)}", f"teeth_number = {teeth}",
             f"face_width = {_n(face_width)}",
             f"helix_angle = {_n(helix_angle)}",
             f"bore_diameter = {_n(bore)}"]
    code = _gear("HerringboneGear",
                 f"herringbone gear, module {module:g}, {teeth} teeth",
                 {"module": "module", "teeth_number": "teeth_number",
                  "width": "face_width", "helix_angle": "helix_angle",
                  "bore_d": "bore_diameter", "_pitch": pitch_d, "_tip": tip_d},
                 lines)
    return CatalogPart(
        id=f"gear_herringbone_m{module:g}_z{teeth}",
        title=f"Herringbone gear, module {module:g}, {teeth} teeth",
        standard="ISO 53 involute profile", code=code,
        parameters={"module": module, "teeth": teeth,
                    "pitch_diameter": pitch_d, "tip_diameter": tip_d})


def ring_gear(module: float = 2.0, teeth: int = 40, face_width: float = 10.0,
              rim_width: float = 4.0) -> CatalogPart:
    """An internal (annulus) gear, as used in a planetary set."""
    if not HAVE_GEARS:
        raise NotAvailable("cq_gears is not installed")
    pitch_d = module * teeth
    lines = [f"module = {_n(module)}", f"teeth_number = {teeth}",
             f"face_width = {_n(face_width)}", f"rim_width = {_n(rim_width)}"]
    code = _gear("RingGear", f"internal ring gear, module {module:g}, "
                             f"{teeth} teeth",
                 {"module": "module", "teeth_number": "teeth_number",
                  "width": "face_width", "rim_width": "rim_width",
                  "_pitch": pitch_d, "_tip": pitch_d - 2 * module}, lines)
    return CatalogPart(
        id=f"gear_ring_m{module:g}_z{teeth}",
        title=f"Internal ring gear, module {module:g}, {teeth} teeth",
        standard="ISO 53 involute profile", code=code,
        parameters={"module": module, "teeth": teeth,
                    "pitch_diameter": pitch_d})


def rack_gear(module: float = 2.0, length: float = 60.0,
              face_width: float = 10.0, height: float = 12.0) -> CatalogPart:
    """The straight-line limit of a gear: a rack."""
    if not HAVE_GEARS:
        raise NotAvailable("cq_gears is not installed")
    lines = [f"module = {_n(module)}", f"rack_length = {_n(length)}",
             f"face_width = {_n(face_width)}", f"rack_height = {_n(height)}"]
    code = _gear("RackGear", f"gear rack, module {module:g}, {length:g} mm long",
                 {"module": "module", "length": "rack_length",
                  "width": "face_width", "height": "rack_height",
                  "_pitch": 0, "_tip": 0}, lines)
    return CatalogPart(
        id=f"gear_rack_m{module:g}_l{length:g}",
        title=f"Gear rack, module {module:g}, {length:g} mm",
        standard="ISO 53 involute profile", code=code,
        parameters={"module": module, "length": length})


def bevel_gear(module: float = 2.0, teeth: int = 20,
               cone_angle: float = 45.0, face_width: float = 8.0
               ) -> CatalogPart:
    """A straight bevel gear, for a right-angle drive."""
    if not HAVE_GEARS:
        raise NotAvailable("cq_gears is not installed")
    pitch_d = module * teeth
    lines = [f"module = {_n(module)}", f"teeth_number = {teeth}",
             f"cone_angle = {_n(cone_angle)}", f"face_width = {_n(face_width)}"]
    code = _gear("BevelGear",
                 f"bevel gear, module {module:g}, {teeth} teeth, "
                 f"{cone_angle:g} degree cone",
                 {"module": "module", "teeth_number": "teeth_number",
                  "cone_angle": "cone_angle", "face_width": "face_width",
                  "_pitch": pitch_d, "_tip": pitch_d + 2 * module}, lines)
    return CatalogPart(
        id=f"gear_bevel_m{module:g}_z{teeth}",
        title=f"Bevel gear, module {module:g}, {teeth} teeth",
        standard="ISO 53 involute profile", code=code,
        parameters={"module": module, "teeth": teeth,
                    "pitch_diameter": pitch_d})


# ---------------------------------------------------------------------------
# Fasteners, from cq_warehouse
# ---------------------------------------------------------------------------

#: Plain-English head names to the cq_warehouse class and its standard.
SCREW_KINDS: dict[str, tuple[str, str]] = {
    "socket_head": ("SocketHeadCapScrew", "iso4762"),
    "hex_head": ("HexHeadScrew", "iso4014"),
    "countersunk": ("CounterSunkScrew", "iso10642"),
    "button_head": ("ButtonHeadScrew", "iso7380_1"),
    "cheese_head": ("CheeseHeadScrew", "iso1207"),
    "pan_head": ("PanHeadScrew", "iso14583"),
    "set_screw": ("SetScrew", "iso4026"),
}

NUT_KINDS: dict[str, tuple[str, str]] = {
    "hex": ("HexNut", "iso4032"),
    "hex_flange": ("HexNutWithFlange", "din1665"),
    "square": ("SquareNut", "din557"),
    "domed_cap": ("DomedCapNut", "din1587"),
    "heat_set": ("HeatSetNut", "Hilitchi"),
}


def _thread_designation(size: str) -> str:
    """'M8' -> 'M8-1.25', the size string cq_warehouse expects."""
    spec = standards.THREADS[standards._normalise(size)]
    return f"{spec.designation}-{spec.pitch:g}"


def screw(size: str = "M8", length: float = 30.0,
          kind: str = "socket_head", threaded: bool = False) -> CatalogPart:
    """A standard machine screw, any of seven head styles.

    ``threaded`` models the real helix. It costs about 25x the build time
    (1.7s against 68ms for an M8x30), so it is off by default - the same
    choice every production CAD library makes.
    """
    if not HAVE_WAREHOUSE:
        raise NotAvailable("cq_warehouse is not installed")
    if kind not in SCREW_KINDS:
        raise KeyError(f"Unknown head '{kind}'. Known: {', '.join(SCREW_KINDS)}")
    cls, standard = SCREW_KINDS[kind]
    designation = _thread_designation(size)
    code = f"""import cadquery as cq
from cq_warehouse.fastener import {cls}

# {standard.upper()} {kind.replace('_', ' ')} screw, {size} x {length:g}
size = "{designation}"
length = {_n(length)}
fastener_type = "{standard}"
model_thread = {threaded}

screw = {cls}(
    size=size,
    length=length,
    fastener_type=fastener_type,
    simple=not model_thread,
)
result = cq.Workplane('XY').add(screw)
"""
    return CatalogPart(
        id=f"{standard}_{kind}_{size.lower()}x{length:g}",
        title=f"{standard.upper()} {kind.replace('_', ' ')} screw "
              f"{size} x {length:g}",
        standard=standard.upper(), code=code,
        parameters={"size": size, "length": length, "threaded": threaded})


def nut(size: str = "M8", kind: str = "hex",
        threaded: bool = False) -> CatalogPart:
    if not HAVE_WAREHOUSE:
        raise NotAvailable("cq_warehouse is not installed")
    if kind not in NUT_KINDS:
        raise KeyError(f"Unknown nut '{kind}'. Known: {', '.join(NUT_KINDS)}")
    cls, standard = NUT_KINDS[kind]
    designation = _thread_designation(size)
    code = f"""import cadquery as cq
from cq_warehouse.fastener import {cls}

# {standard.upper()} {kind.replace('_', ' ')} nut, {size}
size = "{designation}"
fastener_type = "{standard}"
model_thread = {threaded}

nut = {cls}(size=size, fastener_type=fastener_type, simple=not model_thread)
result = cq.Workplane('XY').add(nut)
"""
    return CatalogPart(
        id=f"{standard}_{kind}_nut_{size.lower()}",
        title=f"{standard.upper()} {kind.replace('_', ' ')} nut {size}",
        standard=standard.upper(), code=code,
        parameters={"size": size, "threaded": threaded})


def sprocket(teeth: int = 16, chain_pitch: float = 12.7,
             roller_diameter: float = 7.75, thickness: float = 3.0,
             bolt_circle: float = 30.0) -> CatalogPart:
    """A roller-chain sprocket. Defaults are ANSI #40 / ISO 08B."""
    if not HAVE_WAREHOUSE:
        raise NotAvailable("cq_warehouse is not installed")
    code = f"""import cadquery as cq
from cq_warehouse.sprocket import Sprocket

# Roller chain sprocket, {teeth} teeth on {chain_pitch:g} mm pitch
num_teeth = {teeth}
chain_pitch = {_n(chain_pitch)}
roller_diameter = {_n(roller_diameter)}
thickness = {_n(thickness)}
bolt_circle_diameter = {_n(bolt_circle)}

result = cq.Workplane('XY').add(Sprocket(
    num_teeth=num_teeth,
    chain_pitch=chain_pitch,
    roller_diameter=roller_diameter,
    thickness=thickness,
    bolt_circle_diameter=bolt_circle_diameter,
))
"""
    return CatalogPart(
        id=f"sprocket_{teeth}t_p{chain_pitch:g}",
        title=f"Roller chain sprocket, {teeth} teeth, {chain_pitch:g} mm pitch",
        standard="ANSI B29.1 / ISO 606", code=code,
        parameters={"teeth": teeth, "chain_pitch": chain_pitch,
                    "thickness": thickness})


def available() -> dict[str, bool]:
    """Which optional backends are present, for the health panel."""
    return {"cq_gears": HAVE_GEARS, "cq_warehouse": HAVE_WAREHOUSE}
