"""A scripted stand-in for the Anthropic client, for testing without a key.

Every agent in ``autofab.agents`` reaches the network through one function,
``_get_client()``.  Patching just that leaves the real agent bodies running -
prompt assembly, RAG retrieval from KB1/KB2, markdown fence stripping, JSON
parsing, and (for the Judge) the actual VTK three-view render.  Only the HTTP
call is replaced, so a test exercises everything except the model itself.

Responses are dispatched on the agent's system prompt and served in order,
letting a test script a specific run: "this Coder output, then a Judge
rejection, then that Refiner output, then a Judge pass".
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass
class _Usage:
    input_tokens: int = 1200
    output_tokens: int = 400


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)


# Distinctive phrases from each agent's system prompt in autofab/agents.py.
_ROUTES = [
    ("planner", "You are the Planner Agent"),
    ("coder", "You are the Coder Agent"),
    ("error_refiner", "You are the Error Refiner Agent"),
    ("judge", "You are the Validator Agent"),
    ("refiner", "You are the Refiner Agent"),
]


class FakeClaude:
    """Serves canned responses per agent role, in order.

    Args:
        plan: The design plan the Planner returns.
        code: Successive Coder/Refiner outputs.  The first is the Coder's;
            each later one is served to the next Refiner call.
        verdicts: Successive Judge verdicts as ``(passed, feedback)``.
        error_fixes: Successive Error Refiner outputs.
    """

    def __init__(
        self,
        plan: dict,
        code: list[str],
        verdicts: list[tuple[bool, str]],
        error_fixes: Optional[list[str]] = None,
    ):
        self.plan = plan
        self.code = list(code)
        self.verdicts = list(verdicts)
        self.error_fixes = list(error_fixes or [])
        self.calls: list[str] = []

    # -- anthropic client surface -------------------------------------------

    @property
    def messages(self) -> "FakeClaude":
        return self

    def create(self, *, system: str = "", **_: Any) -> _Response:
        role = self._route(system)
        self.calls.append(role)
        return _Response(content=[_Block(text=self._respond(role))])

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _route(system: str) -> str:
        for name, marker in _ROUTES:
            if marker in system:
                return name
        raise AssertionError(f"Unroutable system prompt: {system[:120]!r}")

    def _respond(self, role: str) -> str:
        if role == "planner":
            return json.dumps(self.plan)
        if role == "judge":
            passed, feedback = (
                self.verdicts.pop(0) if self.verdicts else (True, "All constraints met.")
            )
            return json.dumps({"passed": passed, "feedback": feedback})
        if role == "error_refiner":
            if not self.error_fixes:
                raise AssertionError("Error Refiner called but no fix was scripted")
            return self.error_fixes.pop(0)
        # coder / refiner both draw from the code queue
        if not self.code:
            raise AssertionError(f"{role} called but no code was scripted")
        return self.code.pop(0)


@contextlib.contextmanager
def patched(fake: FakeClaude) -> Iterator[FakeClaude]:
    """Route every agent in ``autofab.agents`` through ``fake``."""
    from autofab import agents

    original = agents._get_client
    agents._get_client = lambda: fake  # type: ignore[assignment]
    try:
        yield fake
    finally:
        agents._get_client = original  # type: ignore[assignment]
