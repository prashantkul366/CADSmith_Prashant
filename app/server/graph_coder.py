"""Ask the model for an operation graph, and fall back to a script if it cannot.

The stock Coder writes free-form CadQuery.  That is why the app can build
anything the kernel can express, and also why a model can place four holes at
coordinates that put three of them off the part - nothing between the model
and the kernel is in a position to notice.

This asks for a graph first.  A graph is checked by arithmetic before the
kernel runs, compiles to a script that stays parametric, and cannot express
the coordinate mistakes that produced every wrong part this app has made.

The fallback is the point, not an afterthought.  A vocabulary of eight
operations does not cover everything CadQuery does, and a request it cannot
express must still be built, so anything that fails to parse, fails a static
check or fails to compile goes to the stock Coder untouched.  Which path ran
is reported rather than hidden, so the hit rate is a number the project can
watch rather than a claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from app import graph

#: Two attempts. The first failure is usually a small schema mistake the model
#: can fix when told exactly what was wrong; a second failure means the part
#: probably does not fit the vocabulary, and the script path is the better
#: answer than a third round of the same argument.
MAX_ATTEMPTS = 2

GRAPH_SYSTEM = """You are the Coder Agent. You describe a part as a graph of \
typed CAD operations, which is then compiled to CadQuery.

Reply with JSON only - no prose, no code fences:

{"units": "mm", "ops": [ {"id": "...", "type": "...", "parent": "...", \
"params": {...}}, ... ]}

Rules that matter:
- Every dimension is in millimetres.
- `id` is a short name usable as a Python variable. Later operations refer to \
earlier ones by `parent`; leaving `parent` out means the operation before it.
- The first operation must create a solid (box, cylinder, prism, raw_script).
- Hole positions are measured from the CENTRE of the face they are cut into, \
never from a corner and never in world coordinates. Prefer a pattern over \
listing coordinates: `rect_corners` with `inset_x`/`inset_y` places holes in \
from each corner of the parent, whatever size it is.
- A hexagon "across the flats" uses `across_flats`. Use `across_corners` only \
if the request says across corners. Give exactly one.
- Leave `depth` out of a hole to go all the way through.

%(vocabulary)s

If the part genuinely cannot be described with these operations - a loft, a \
sweep, a revolve, a complex profile - use a single `raw_script` operation \
whose `code` parameter is ordinary CadQuery assigning to `result`. Prefer the \
typed operations wherever they fit: they are checked before building and \
stay editable afterwards."""


@dataclass
class GraphResult:
    code: str
    graph: Optional[dict] = None
    attempts: int = 0
    #: Why the graph path was not used, empty when it was.
    fell_back: str = ""
    #: What kind of failure it was, which decides how loudly to report it:
    #:
    #: ``schema``   - the vocabulary could not express this. Ordinary; the
    #:                script path exists for exactly this case.
    #: ``geometry`` - the graph was well formed and described a part that
    #:                cannot be built: a hole off the edge of the face it is
    #:                cut into, a fillet larger than the stock. That is a
    #:                design error, and falling back to a script means the
    #:                model gets to make the same mistake somewhere nothing
    #:                can see it. Worth saying out loud.
    #: ``call``     - the backend failed.
    kind: str = ""

    @property
    def used_graph(self) -> bool:
        return self.graph is not None

    @property
    def is_design_error(self) -> bool:
        return self.kind == "geometry"


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _payload(reply: str) -> str:
    """The JSON in a reply, whether or not the model wrapped it in prose."""
    text = reply.strip()
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def system_prompt() -> str:
    return GRAPH_SYSTEM % {"vocabulary": graph.vocabulary()}


def generate(design_plan: dict, prompt: str, call, on_note=None) -> GraphResult:
    """Ask for a graph; return compiled code, or say why we fell back.

    ``call(system, user)`` is the model call, injected so this module needs to
    know nothing about which backend is in use.
    """
    user = (f"Original user request: {prompt}\n\n"
            f"Design plan:\n{json.dumps(design_plan, indent=2)}\n\n"
            f"Describe this part as an operation graph.")
    last = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = call(system_prompt(), user)
        except Exception as exc:                       # noqa: BLE001
            # A backend failure is not a reason to abandon the graph path
            # quietly - but it is also not something a retry here will fix.
            return GraphResult(code="", attempts=attempt, kind="call",
                               fell_back=f"the model call failed: {exc}")
        kind = "schema"
        try:
            parsed = graph.parse(_payload(reply))
            # Parsed cleanly, so anything that fails from here is a statement
            # about the geometry rather than about the schema.
            kind = "geometry"
            code = graph.compile_graph(parsed, strict=True)
        except graph.GraphError as exc:
            last = str(exc)
            if on_note:
                on_note(f"graph attempt {attempt} of {MAX_ATTEMPTS}: {last}")
            if attempt == MAX_ATTEMPTS:
                break
            # Hand the exact complaint back. These messages are written to be
            # read by the model as much as by a person, so this is usually
            # enough for it to correct a schema slip on the second go.
            user = (f"{user}\n\nYour previous reply could not be used:\n"
                    f"{last}\n\nReturn a corrected graph, JSON only.")
            continue
        except Exception as exc:                       # noqa: BLE001
            last, kind = f"{type(exc).__name__}: {exc}", "schema"
            break
        return GraphResult(code=code, graph=parsed.to_dict(), attempts=attempt)

    return GraphResult(code="", attempts=MAX_ATTEMPTS, fell_back=last,
                       kind=kind)
