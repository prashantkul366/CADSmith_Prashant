"""Standard dimensions, retrieved for whatever a request mentions.

The Planner is the one agent with no retrieval at all.  KB1 gives the Coder
CadQuery API docs and KB2 gives the Error Refiner error fixes, but the agent
that decides every target dimension gets the bare prompt and is told, in its
own system prompt, to "estimate reasonable engineering dimensions" when the
request does not state them.  Estimating is exactly where a model invents a
22mm pilot bore for a NEMA 23 that needs 38.1mm.

This is the missing third knowledge base: not geometry, not API docs, just
the numbers, pulled from ``standards.py`` for the things a request actually
names.  A request that names nothing standard gets nothing injected.

Retrieval is conservative on purpose.  Wrong facts are worse than none: they
would be handed to the Planner as authoritative and propagate through the
plan, the code and the Judge's acceptance criteria together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.catalog import standards

# At most this many subjects, so a request naming a dozen sizes cannot bury
# the actual request under reference material.
MAX_FACTS = 6

_THREAD_RE = re.compile(r"\bM\s?(\d+(?:\.\d+)?)(?![\d.])", re.I)
_BEARING_RE = re.compile(r"\b(6\d{3})\b")
_NEMA_RE = re.compile(r"\bNEMA\s*(\d{1,2})\b", re.I)

# Words that mean the request involves a fastener even without a size given.
_FASTENER_WORDS = ("bolt", "screw", "cap head", "socket head", "shcs",
                   "tapped", "threaded hole", "clearance hole", "counterbore",
                   "countersink", "nut", "washer", "stud")


@dataclass(frozen=True)
class Fact:
    subject: str
    standard: str
    lines: list[str]

    def render(self) -> str:
        body = "\n".join(f"  - {line}" for line in self.lines)
        return f"{self.subject} ({self.standard})\n{body}"


def _thread_fact(size: str) -> Fact:
    thread = standards.THREADS[size]
    lines = [
        f"nominal diameter {thread.diameter} mm, coarse pitch {thread.pitch} mm",
        f"clearance hole {thread.clearance_close} mm close fit, "
        f"{thread.clearance_normal} mm normal fit",
        f"tapping drill {thread.tapping_drill} mm for a coarse thread",
    ]
    screw = standards.ISO_4762.get(size)
    if screw:
        bore, depth = standards.counterbore(size)
        lines.append(
            f"socket head cap screw: head {screw.head_diameter} mm diameter x "
            f"{screw.head_height} mm high, {screw.socket_across_flats} mm key")
        lines.append(
            f"counterbore to bury that head: {bore} mm diameter x {depth} mm deep")
    nut = standards.ISO_4032.get(size)
    if nut:
        lines.append(f"hex nut: {nut.across_flats} mm across flats, "
                     f"{nut.height} mm thick")
    washer = standards.ISO_7089.get(size)
    if washer:
        lines.append(
            f"plain washer: {washer.inner_diameter} mm bore, "
            f"{washer.outer_diameter} mm outside, {washer.thickness} mm thick")
    return Fact(f"{size} coarse thread and its hardware",
                "ISO 261, ISO 273, ISO 4762, ISO 4032, ISO 7089", lines)


def _bearing_fact(designation: str) -> Fact:
    spec = standards.BEARINGS[designation]
    return Fact(
        f"{designation} deep groove ball bearing", "ISO 15",
        [f"bore {spec.bore} mm, outside diameter {spec.outer_diameter} mm, "
         f"width {spec.width} mm",
         "a housing bore for the outer ring is normally H7; a shaft for a "
         "rotating inner ring is normally k6",
         f"a housing must be at least {spec.outer_diameter} mm bore to accept "
         f"it, and the shoulder that locates it must not foul the outer ring"],
    )


def _nema_fact(frame: str) -> Fact:
    spec = standards.NEMA_FRAMES[frame]
    return Fact(
        f"{frame} stepper motor frame", "NEMA ICS 16",
        [f"body {spec.frame_size} mm square",
         f"mounting holes on a {spec.bolt_pattern} mm square pattern",
         f"pilot boss {spec.pilot_diameter} mm diameter - a mounting plate "
         f"must bore at least this to seat flat",
         f"shaft {spec.shaft_diameter} mm diameter",
         f"mounting screws are normally {spec.mounting_screw}"],
    )


def facts_for(text: str) -> list[Fact]:
    """Every standard fact the request implicates, most specific first."""
    found: list[Fact] = []
    seen: set[str] = set()

    def add(fact: Fact) -> None:
        if fact.subject not in seen:
            seen.add(fact.subject)
            found.append(fact)

    for match in _NEMA_RE.finditer(text):
        key = f"NEMA {match.group(1)}"
        if key in standards.NEMA_FRAMES:
            add(_nema_fact(key))

    for match in _BEARING_RE.finditer(text):
        if match.group(1) in standards.BEARINGS:
            add(_bearing_fact(match.group(1)))

    for match in _THREAD_RE.finditer(text):
        try:
            add(_thread_fact(standards._normalise(match.group(1))))
        except KeyError:
            continue  # M99 and the like: say nothing rather than guess

    # A NEMA frame implies its mounting screw even when the request never
    # names a size, which is the usual way these prompts are written.
    for fact in list(found):
        if "stepper motor frame" in fact.subject:
            frame = fact.subject.rsplit(" stepper", 1)[0]
            size = standards.NEMA_FRAMES[frame].mounting_screw
            add(_thread_fact(standards._normalise(size)))

    return found[:MAX_FACTS]


def as_prompt_block(facts: list[Fact]) -> str:
    """The text appended to the Planner's request.

    Deliberately defers to the request: the Planner's own system prompt tells
    it to honour explicit dimensions, and a reference table must not override
    someone who asked for something non-standard on purpose.
    """
    if not facts:
        return ""
    body = "\n\n".join(fact.render() for fact in facts)
    return (
        "REFERENCE DIMENSIONS\n"
        "These are from the published standards and are exact. Use them "
        "rather than estimating, and carry them into key_dimensions and "
        "constraints so the generated part actually fits. Where the request "
        "states a dimension explicitly, the request wins.\n\n"
        f"{body}"
    )


def summary(facts: list[Fact]) -> str:
    """One line naming what was retrieved, for the event log and the UI."""
    if not facts:
        return "no standard dimensions applied"
    return "grounded in " + ", ".join(fact.subject for fact in facts)


def ground(text: str) -> tuple[str, list[Fact]]:
    """The request with its reference block appended, and what was found."""
    facts = facts_for(text)
    if not facts:
        return text, []
    return f"{text}\n\n{as_prompt_block(facts)}", facts
