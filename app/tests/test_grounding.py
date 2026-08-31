"""Do the published dimensions actually reach the Planner?

Two things are worth proving and one is worth being honest about.

Provable here: the right facts are retrieved for a request, and the wrapped
``agents.plan`` really does hand them to the model - checked by capturing the
exact text that reaches the LLM boundary, not by trusting the wrapper.  And
that with no run context the prompt is untouched, so ``run.py`` and the
benchmark scripts behave as published.

Not provable here: whether a given model then *uses* the numbers.  That needs
a real key and a real model; ``app/tools/grounding_ab.py`` runs that
comparison when you have one.

    .venv/bin/python -m app.tests.test_grounding
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autofab import agents  # noqa: E402

from app.catalog import grounding, standards  # noqa: E402
from app.server.events import EventSink, PHASE_GROUND  # noqa: E402
from app.server.instrument import (  # noqa: E402
    RunContext, install_agent_hooks, set_context,
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class Captured:
    """Stands in for the model, and records what it was asked."""

    def __init__(self) -> None:
        self.system = ""
        self.user = ""

    def install(self) -> None:
        captured = self

        class _Message:
            def __init__(self, text): self.text = text

        class _Usage:
            input_tokens = 100
            output_tokens = 50

        class _Response:
            def __init__(self, text):
                self.content = [_Message(text)]
                self.usage = _Usage()

        class _Messages:
            def create(self, **kwargs):
                captured.system = kwargs.get("system", "")
                blocks = kwargs["messages"][0]["content"]
                captured.user = (blocks if isinstance(blocks, str)
                                 else " ".join(b.get("text", "") for b in blocks))
                return _Response(
                    '{"description": "captured", "components": ["body"], '
                    '"dimensions": {"key_dimensions": {}}, "constraints": {}, '
                    '"acceptance_criteria": {}}')

        class _Client:
            messages = _Messages()

        agents._get_client = lambda: _Client()


# ---------------------------------------------------------------------------

def test_retrieval() -> None:
    print("\nWhat gets retrieved for a request")
    facts = grounding.facts_for("A NEMA 23 motor mounting plate, 100mm square.")
    subjects = [f.subject for f in facts]
    check("a NEMA 23 request retrieves the frame",
          any("NEMA 23" in s for s in subjects), str(subjects))
    check("and the M5 hardware it mounts with",
          any("M5" in s for s in subjects), str(subjects))
    text = grounding.as_prompt_block(facts)
    check("the pilot bore that is normally got wrong is in there",
          "38.1" in text and "47.14" in text)

    facts = grounding.facts_for("a bracket with four M8 clearance holes")
    check("an M8 request gives the 9.0mm clearance hole",
          "9.0 mm normal fit" in grounding.as_prompt_block(facts))

    facts = grounding.facts_for("a housing for a 6203 bearing")
    check("a 6203 request gives 17/40/12",
          all(v in grounding.as_prompt_block(facts)
              for v in ("17.0", "40.0", "12.0")))

    check("a request naming nothing standard retrieves nothing",
          grounding.facts_for("a pillow block for a 25mm shaft") == [])
    check("an unknown designation is not guessed at",
          grounding.facts_for("an M99 bolt and a 9999 bearing") == [])

    many = grounding.facts_for(
        "M3 M4 M5 M6 M8 M10 M12 M16 M20 screws all at once")
    check("retrieval is capped so it cannot bury the request",
          len(many) <= grounding.MAX_FACTS, f"{len(many)} facts")


def test_reaches_the_model() -> None:
    print("\nWhat actually arrives at the model")
    install_agent_hooks()
    captured = Captured()
    captured.install()

    prompt = "A NEMA 23 motor mounting plate, 100mm square and 8mm thick."

    # 1. No run context: this is run.py and the benchmark. Untouched.
    set_context(None)
    agents.plan(prompt)
    check("with no run context the prompt is passed through unchanged",
          captured.user.strip() == prompt, captured.user[:70])

    # 2. In the app, grounding on.
    sink = EventSink()
    ctx = RunContext(sink=sink, job_dir=Path("/tmp"), part_name="test")
    ctx.ground_dimensions = True
    set_context(ctx)
    agents.plan(prompt)
    check("the original request is still there, first",
          captured.user.startswith(prompt), captured.user[:50])
    check("the reference block is appended",
          "REFERENCE DIMENSIONS" in captured.user)
    check("the model is told the NEMA 23 pilot bore is 38.1mm",
          "38.1" in captured.user)
    check("and the 47.14mm bolt pattern",
          "47.14" in captured.user)
    check("and that an explicit request still wins",
          "the request wins" in captured.user)

    events = [e for e in sink.all() if e.phase == PHASE_GROUND]
    check("the run records what it was grounded in",
          len(events) == 1 and "NEMA 23" in events[0].message,
          events[0].message if events else "no event")

    # 3. In the app, grounding off: the published behaviour, as an ablation.
    sink_off = EventSink()
    ctx_off = RunContext(sink=sink_off, job_dir=Path("/tmp"), part_name="test")
    ctx_off.ground_dimensions = False
    set_context(ctx_off)
    agents.plan(prompt)
    check("turning it off restores the published prompt exactly",
          captured.user.strip() == prompt, captured.user[:70])

    # 4. Nothing standard named: no block, and the run says so.
    sink_none = EventSink()
    ctx_none = RunContext(sink=sink_none, job_dir=Path("/tmp"), part_name="test")
    set_context(ctx_none)
    plain = "a pillow block for a 25mm shaft"
    agents.plan(plain)
    check("a request with nothing standard is left alone",
          captured.user.strip() == plain, captured.user[:70])
    events = [e for e in sink_none.all() if e.phase == PHASE_GROUND]
    check("and the run says nothing was applied",
          len(events) == 1 and "no standard" in events[0].message,
          events[0].message if events else "no event")

    set_context(None)


def test_the_system_prompt_is_untouched() -> None:
    print("\nautofab is not modified")
    check("the Planner's own system prompt is the published one",
          "You are the Planner Agent" in agents.PLANNER_SYSTEM
          and "REFERENCE" not in agents.PLANNER_SYSTEM)
    source = Path(__file__).resolve().parents[2] / "autofab" / "agents.py"
    check("autofab/agents.py knows nothing about the catalogue",
          "catalog" not in source.read_text().lower())


def main() -> int:
    test_retrieval()
    test_reaches_the_model()
    test_the_system_prompt_is_untouched()

    print("\n" + "=" * 58)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:6])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
