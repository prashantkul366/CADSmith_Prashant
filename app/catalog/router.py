"""Decide whether a request is a standard part, and which one.

The whole catalogue meets the pipeline here.  Three rules govern it:

**Only an unambiguous request routes.**  "An M8x30 socket head cap screw" is
a standard part.  "A bracket that takes four M8 screws" is a custom bracket
that happens to mention one, and must still be generated - substituting the
screw would hand back confidently wrong hardware.  When in doubt, return
None and let the pipeline do its job; a slower correct answer beats a fast
wrong one.

**Nothing is served without being built first.**  Every candidate goes
through ``verify.check``.  This is not belt-and-braces: cq_warehouse 0.8.0
returns washers as non-closed shells with twice the correct volume, and a
part like that reaching the Judge would be reported as a broken solid and
blamed on the pipeline. A part that fails verification is dropped and the
request falls through to the model.

**Missing libraries degrade, they do not break.**  cq_gears and cq_warehouse
are optional git dependencies; without them the catalogue is smaller and
everything still runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from app.catalog import japanese, library, parts, standards, verify
from app.catalog.parts import CatalogPart

# A standard part named inside a bigger noun is a component of a custom part,
# not the deliverable. Shared with parts.select, and extended for the
# families the libraries add.
_CUSTOM_CONTEXT = parts._CUSTOM_CONTEXT + (
    "gearbox", "gear box", "reducer", "drivetrain", "transmission",
    "assembly", "for a gear", "gear train", "tensioner", "idler bracket",
)

_TEETH = re.compile(r"(\d+)\s*(?:-)?\s*(?:tooth|teeth|t\b)", re.I)
_MODULE = re.compile(r"\bmod(?:ule)?\.?\s*(\d+(?:\.\d+)?)", re.I)
_HELIX = re.compile(r"(\d+(?:\.\d+)?)\s*(?:deg|degree)", re.I)
_FACE_WIDTH = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:wide|face|thick)", re.I)
_BORE = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*bore", re.I)
_WIRE = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*wire", re.I)
_OD = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:od|outside diameter|outer diameter)",
                 re.I)
_FREE_LENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:free length|long|length)",
                          re.I)
_COILS = re.compile(r"(\d+(?:\.\d+)?)\s*coils?", re.I)
_BELT = re.compile(r"\b(gt2|gt3|htd\s*5m|htd5m|htd|t5|t10)\b", re.I)

_GEAR_WORDS = ("gear", "pinion", "sprocket", "cog")


@dataclass
class Routed:
    """A catalogue part that has been built and checked."""
    part: CatalogPart
    report: verify.Report
    source: str          # which backend produced it


def _number(pattern, text, default=None, cast=float):
    found = pattern.search(text)
    return cast(found.group(1)) if found else default


def _gear(text: str) -> Optional[CatalogPart]:
    """A gear, if the request names one specifically enough."""
    lowered = text.lower()
    if not any(word in lowered for word in _GEAR_WORDS):
        return None
    if not library.HAVE_GEARS and "sprocket" not in lowered:
        return None

    teeth = _number(_TEETH, text, cast=int)
    module = _number(_MODULE, text)
    face_width = _number(_FACE_WIDTH, text)
    bore = _number(_BORE, text)

    if "sprocket" in lowered:
        if not library.HAVE_WAREHOUSE or teeth is None:
            return None
        return library.sprocket(teeth=teeth)

    # A rack has no tooth count, so it is allowed through without one.
    if "rack" in lowered:
        return library.rack_gear(module=module or 2.0)

    if teeth is None:
        # "a spur gear" with no tooth count is under-specified. The Planner
        # can pick sensible numbers; the catalogue should not guess them.
        return None

    if "bevel" in lowered:
        return library.bevel_gear(module=module or 2.0, teeth=teeth,
                                  face_width=face_width or 8.0)
    if "herringbone" in lowered or "double helical" in lowered:
        return library.herringbone_gear(module=module or 2.0, teeth=teeth,
                                        face_width=face_width or 14.0,
                                        bore=bore or 8.0)
    if "ring gear" in lowered or "internal gear" in lowered or "annulus" in lowered:
        return library.ring_gear(module=module or 2.0, teeth=teeth,
                                 face_width=face_width or 10.0)
    if "helical" in lowered:
        angle = _number(_HELIX, text, 20.0)
        return library.spur_gear(module=module or 2.0, teeth=teeth,
                                 face_width=face_width or 12.0,
                                 bore=bore or 8.0, helix_angle=angle)
    if "spur" in lowered or "gear" in lowered or "pinion" in lowered:
        return library.spur_gear(module=module or 2.0, teeth=teeth,
                                 face_width=face_width or 10.0,
                                 bore=bore or 8.0)
    return None


def _spring(text: str) -> Optional[CatalogPart]:
    lowered = text.lower()
    if "spring" not in lowered:
        return None
    if "compression" not in lowered and "helical" not in lowered:
        # Extension and torsion springs need ends this builder does not make.
        return None
    wire = _number(_WIRE, text)
    outer = _number(_OD, text)
    if wire is None or outer is None:
        return None
    return parts.compression_spring(
        wire_diameter=wire, outer_diameter=outer,
        free_length=_number(_FREE_LENGTH, text, 50.0),
        coils=_number(_COILS, text, 8.0))


def _pulley(text: str) -> Optional[CatalogPart]:
    lowered = text.lower()
    if "pulley" not in lowered and "sheave" not in lowered:
        return None
    belt = _BELT.search(text)
    if not belt:
        return None  # a plain "pulley" is a V-belt or flat pulley, not this
    teeth = _number(_TEETH, text, cast=int)
    if teeth is None:
        return None
    key = belt.group(1).upper().replace(" ", "").replace("-", "")
    if key == "HTD":
        key = "HTD5M"
    if key not in parts.BELT_PROFILES:
        return None
    return parts.timing_pulley(
        teeth=teeth, belt=key,
        face_width=_number(_FACE_WIDTH, text, 7.0),
        bore=_number(_BORE, text, 5.0))


def _fastener(text: str) -> Optional[CatalogPart]:
    """Screws and nuts, preferring cq_warehouse for its far wider coverage.

    It carries 12 head types across M1.6-M64 where parts.py has two across
    M2-M24, so it leads. Falling back keeps the app working uninstalled.
    """
    lowered = text.lower()
    if not library.HAVE_WAREHOUSE:
        return parts.select(text)

    size_match = parts._SIZE.search(text)
    if not size_match:
        return parts.select(text)
    try:
        size = standards._normalise(size_match.group(1))
    except KeyError:
        return None
    if size not in standards.ISO_4762:
        return parts.select(text)

    threaded = "thread" in lowered and "no thread" not in lowered
    length = (_number(parts._LENGTH, text)
              or _number(parts._LENGTH_WORDS, text))

    if "nut" in lowered:
        for word, kind in (("flange", "hex_flange"), ("square", "square"),
                           ("cap nut", "domed_cap"), ("dome", "domed_cap"),
                           ("heat set", "heat_set"), ("heat-set", "heat_set")):
            if word in lowered:
                return library.nut(size, kind=kind, threaded=threaded)
        return library.nut(size, kind="hex", threaded=threaded)

    if "screw" in lowered or "bolt" in lowered:
        length = length or parts.size_lengths(size)
        for word, kind in (("countersunk", "countersunk"),
                           ("countersink", "countersunk"),
                           ("flat head", "countersunk"),
                           ("button", "button_head"),
                           ("cheese", "cheese_head"),
                           ("pan head", "pan_head"),
                           ("set screw", "set_screw"),
                           ("grub", "set_screw"),
                           ("hex head", "hex_head"),
                           ("hex bolt", "hex_head")):
            if word in lowered:
                return library.screw(size, length, kind=kind, threaded=threaded)
        if any(w in lowered for w in
               ("cap screw", "socket head", "shcs", "allen", "hex socket")):
            return library.screw(size, length, kind="socket_head",
                                 threaded=threaded)
        # Unqualified: a cap screw is the common case in machine design.
        return library.screw(size, length, kind="socket_head",
                             threaded=threaded)

    return parts.select(text)


def select(text: str) -> Optional[Routed]:
    """The standard part this request asks for, built and checked.

    Returns None for anything custom, anything under-specified, and anything
    that fails verification.
    """
    # The backends are part of the cache key, not just the text: what the
    # catalogue can answer depends on which libraries are loaded, and keying
    # on text alone returned a cq_gears part after cq_gears was switched off.
    found = _select_cached(text, library.HAVE_GEARS, library.HAVE_WAREHOUSE)
    if found is not None or not japanese.has_japanese(text):
        return found

    # Every pattern below reads English, so a Japanese request matches none of
    # them and a Japanese user loses the catalogue entirely. Rewriting the
    # request into the vocabulary those patterns already speak recovers it.
    # Second, never first: an English request cannot reach this, and a
    # Japanese one that already matched is not rewritten either, so the
    # rewrite can only add matches - it can never change an existing one.
    return _select_cached(japanese.to_english(text),
                          library.HAVE_GEARS, library.HAVE_WAREHOUSE)


@lru_cache(maxsize=64)
def _select_cached(text: str, have_gears: bool,
                   have_warehouse: bool) -> Optional[Routed]:
    """Cached: the request handler asks whether the catalogue can answer
    before accepting a job, and the worker asks again when it runs it.
    Building a sprocket twice for one request is pure waste."""
    lowered = text.lower()
    if any(word in lowered for word in _CUSTOM_CONTEXT):
        return None

    for finder in (_gear, _pulley, _spring, _fastener, parts.select):
        try:
            candidate = finder(text)
        except Exception:
            continue  # a bad parse must never take the request down
        if candidate is None:
            continue
        report = verify.check(candidate)
        if not report.ok:
            # Built and unsound: drop it rather than serving it. The request
            # falls through to the model, which is slower but not wrong.
            continue
        return Routed(part=candidate, report=report,
                      source=_provenance(candidate))
    return None


def _provenance(part: CatalogPart) -> str:
    """Which backend actually built this, read from what the code imports.

    Taking the label from whichever finder matched was wrong: _fastener
    hands washer requests to parts.py, and they were still being reported as
    coming from cq_warehouse. The emitted code cannot misreport itself.
    """
    if "cq_gears" in part.code:
        return "cq_gears"
    if "cq_warehouse" in part.code:
        return "cq_warehouse"
    return "cadsmith"


def describe() -> dict:
    """What the catalogue can serve right now, for the health panel."""
    backends = library.available()
    families = ["washers", "o-rings", "dowel pins", "bearings",
                "compression springs", "timing pulleys"]
    if backends["cq_gears"]:
        families += ["spur/helical gears", "herringbone", "ring", "rack",
                     "bevel"]
    if backends["cq_warehouse"]:
        families += ["screws (12 heads)", "nuts (5 types)", "sprockets"]
    else:
        families += ["screws (2 heads)", "hex nuts"]
    return {"backends": backends, "families": families}
