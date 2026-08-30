"""Natural-language edits to an already-generated part.

Two paths, chosen by whether the request maps cleanly onto a parameter the
generated script already exposes:

*Parameter patch* - "make it 15mm thick" against a script with a
``thickness = 10.0`` line.  The assignment is rewritten and the script
re-executed, so the geometry is rebuilt by the real kernel in about a second
with no model call.  The Coder agent is instructed to declare parametric
variables at the top of every script (agents.py:130), which is what makes
this reliable enough to try first.

*Refiner agent* - everything else: structural changes, anything touching a
value the script does not name, and any request this module cannot map
confidently.  Ambiguity always falls through to the agent rather than
guessing, because silently changing the wrong dimension is worse than being
slow.

Which path ran is reported to the client, so the distinction is never hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# A top-level parametric assignment: `name = 12.5  # mm`.  Anchored to column
# zero so locals inside functions or loops are left alone.
_ASSIGNMENT = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)(?P<gap>\s*=\s*)(?P<value>-?\d+(?:\.\d+)?)(?P<rest>\s*(?:#.*)?)$"
)

_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)")

# Words a person uses for a dimension, mapped to the tokens that tend to
# appear in generated parameter names.
_SYNONYMS = {
    "thick": "thickness", "thickness": "thickness", "deep": "depth",
    "depth": "depth", "tall": "height", "height": "height", "high": "height",
    "long": "length", "length": "length", "wide": "width", "width": "width",
    "dia": "diameter", "diameter": "diameter", "ø": "diameter",
    "radius": "radius", "rad": "radius", "bore": "bore",
    "hole": "hole", "holes": "hole", "count": "count", "number": "count",
    "fillet": "fillet", "chamfer": "chamfer", "round": "fillet",
    "wall": "wall", "gap": "gap", "spacing": "spacing", "pitch": "pitch",
    "teeth": "teeth", "tooth": "teeth", "module": "module",
    "angle": "angle", "outer": "outer", "inner": "inner", "base": "base",
    "support": "support", "hub": "hub", "flange": "flange", "plate": "plate",
    "shell": "shell", "size": "size",
}

_INCREASE = re.compile(r"\b(add|increase|raise|more|extra|thicken|widen|lengthen|taller|bigger|larger)\b")
_DECREASE = re.compile(r"\b(remove|reduce|decrease|less|fewer|thinner|shorten|shorter|smaller)\b")

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
}

# Features and operations that always mean new geometry.  No amount of
# parameter matching can satisfy these, so they are refused up front.
_SHAPE_ONLY = re.compile(
    r"\b(gusset|rib|slot|boss|pocket|thread|knurl|taper|fillet|chamfer|"
    r"round|rounded|mirror|rotate|revolve|loft|sweep|shell|instead|replace|"
    r"split|attach|introduce)\b"
)

# Verbs that usually mean new geometry, but can legitimately mean "more of a
# thing the script already counts" - resolved once a parameter is chosen.
_MAYBE_STRUCTURAL = re.compile(r"\b(add|remove|delete|cut|drill|move)\b")

_COUNT_TOKENS = {"count", "num", "number", "qty", "teeth"}
_LENGTH_UNIT = re.compile(r"\b(mm|millimet(?:er|re)s?|cm|degrees?|deg)\b|°|⌀")


@dataclass
class Parameter:
    name: str
    value: float
    line: int
    is_integer: bool


@dataclass
class Change:
    name: str
    old: float
    new: float

    def to_dict(self) -> dict:
        return {"name": self.name, "old": self.old, "new": self.new}


@dataclass
class EditPlan:
    """What a parameter patch would do, or why it cannot be done."""
    changes: list[Change]
    reason: str = ""

    @property
    def possible(self) -> bool:
        return bool(self.changes)


def parameters(code: str) -> dict[str, Parameter]:
    """Read the script's top-level numeric parameters."""
    found: dict[str, Parameter] = {}
    for index, line in enumerate(code.splitlines()):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        raw = match.group("value")
        found[match.group("name")] = Parameter(
            name=match.group("name"),
            value=float(raw),
            line=index,
            is_integer="." not in raw,
        )
    return found


def _tokens(name: str) -> set[str]:
    return {part for part in re.split(r"[_\W]+", name.lower()) if part}


def _instruction_tokens(instruction: str) -> set[str]:
    """Words to match against parameter names, plus their canonical forms."""
    words = re.findall(r"[a-zA-Zø]+", instruction.lower())
    expanded = set(words)
    for word in words:
        canonical = _SYNONYMS.get(word)
        if canonical:
            expanded.add(canonical)
    return expanded


def _domain_words(instruction: str) -> set[str]:
    """Only the canonical dimension nouns the instruction actually names.

    Raw words are deliberately excluded: "thick" and "holes" are the same
    concepts as "thickness" and "hole", and treating them as unknown
    qualifiers would reject perfectly ordinary requests.
    """
    return {
        _SYNONYMS[word]
        for word in re.findall(r"[a-zA-Zø]+", instruction.lower())
        if word in _SYNONYMS
    }


def _score(parameter: Parameter, words: set[str], counting: bool) -> int:
    """How strongly a parameter name is named by the instruction.

    ``counting`` says the phrasing looks like "two more holes" rather than
    "8mm holes", which is what separates ``hole_count`` from ``hole_diameter``
    when the instruction only says "holes".
    """
    parts = _tokens(parameter.name)
    hits = parts & words
    if not hits:
        return 0
    # Every word of the name being present is a much stronger signal than one
    # of several ("hole diameter" beating "diameter" when both could match).
    score = len(hits) * 2 + (3 if parts <= words else 0)

    is_count = bool(parts & _COUNT_TOKENS)
    if counting and is_count:
        score += 3
    elif not counting and is_count:
        score -= 3
    return score


def _numbers(instruction: str) -> list[float]:
    values = [float(v) for v in _NUMBER.findall(instruction)]
    for word, value in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", instruction.lower()):
            values.append(float(value))
    return values


def plan_edit(code: str, instruction: str) -> EditPlan:
    """Decide whether this instruction is a parameter patch, and what it changes."""
    available = parameters(code)
    if not available:
        return EditPlan([], "the script declares no top-level parameters")

    text = instruction.lower().strip()
    words = _instruction_tokens(text)
    values = _numbers(text)

    shape = _SHAPE_ONLY.search(text)
    if shape:
        return EditPlan([], f"'{shape.group(1)}' asks for a change in shape")

    if not values:
        return EditPlan([], "no target value was given")

    delta = bool(_INCREASE.search(text) or _DECREASE.search(text))
    # "two more holes" counts; "8mm holes" measures.
    counting = delta and not _LENGTH_UNIT.search(text)

    scored = sorted(
        ((p, _score(p, words, counting)) for p in available.values()),
        key=lambda pair: pair[1], reverse=True,
    )
    best, best_score = scored[0]
    if best_score <= 0:
        return EditPlan([], "no parameter matches the words used")

    runner_up = scored[1][1] if len(scored) > 1 else 0
    if runner_up == best_score:
        tied = [p.name for p, s in scored if s == best_score]
        return EditPlan([], f"ambiguous between {' and '.join(tied)}")

    # A qualifier the script does not know about means the instruction is
    # about a feature that does not exist here - "the flange diameter" on a
    # script with only hole_diameter must not quietly resize the holes.
    known = set().union(*(_tokens(p.name) for p in available.values()))
    stray = _domain_words(text) - known
    if stray:
        return EditPlan(
            [], f"the script has no {' or '.join(sorted(stray))} parameter")

    # Adding or removing usually means new geometry, unless it is plainly
    # more of something the script already counts.
    structural = _MAYBE_STRUCTURAL.search(text)
    if structural and not (counting and bool(_tokens(best.name) & _COUNT_TOKENS)):
        return EditPlan([], f"'{structural.group(1)}' asks for a change in shape")

    amount = values[0] if len(values) == 1 else _nearest_number(text, best, values)

    if delta and _DECREASE.search(text):
        new_value = best.value - amount
    elif delta and _INCREASE.search(text):
        new_value = best.value + amount
    else:
        new_value = amount

    if new_value <= 0:
        return EditPlan([], f"that would set {best.name} to {new_value:g}")
    if abs(new_value - best.value) < 1e-9:
        return EditPlan([], f"{best.name} is already {best.value:g}")

    return EditPlan([Change(name=best.name, old=best.value, new=new_value)])


def _nearest_number(text: str, parameter: Parameter, values: list[float]) -> float:
    """With several numbers present, take the one closest to the named token."""
    parts = _tokens(parameter.name)
    anchor = None
    for part in parts:
        match = re.search(rf"\b{re.escape(part)}\b", text)
        if match:
            anchor = match.start()
            break
    if anchor is None:
        return values[-1]

    best, best_distance = values[0], None
    for match in _NUMBER.finditer(text):
        distance = abs(match.start() - anchor)
        if best_distance is None or distance < best_distance:
            best, best_distance = float(match.group(1)), distance
    return best


def apply_changes(code: str, changes: list[Change]) -> str:
    """Rewrite the named assignments, preserving layout and comments."""
    available = parameters(code)
    lines = code.splitlines()

    for change in changes:
        parameter = available.get(change.name)
        if parameter is None:
            continue
        match = _ASSIGNMENT.match(lines[parameter.line])
        if not match:
            continue
        if parameter.is_integer and float(change.new).is_integer():
            rendered = str(int(change.new))
        else:
            rendered = f"{change.new:g}"
            if "." not in rendered and "e" not in rendered:
                rendered += ".0"
        lines[parameter.line] = (
            f"{match.group('name')}{match.group('gap')}{rendered}{match.group('rest')}"
        )

    return "\n".join(lines) + ("\n" if code.endswith("\n") else "")


def describe(changes: list[Change]) -> str:
    return ", ".join(f"{c.name} {c.old:g} → {c.new:g}" for c in changes)
