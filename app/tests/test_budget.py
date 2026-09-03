"""The per-run token ceiling, and that it really stops a run.

A guard that reports a limit but does not enforce it is worse than none: it
reads as protection while the meter keeps running. So the important case here
is not the arithmetic, it is that a pipeline whose model never converges stops
making calls once the allowance is gone - checked against the real pipeline
with a scripted model, not against a mock of the guard.

Run:  .venv/bin/python -m app.tests.test_budget
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server import budget  # noqa: E402
from app.server.events import EventSink  # noqa: E402
from app.server.instrument import (  # noqa: E402
    InstrumentedPipeline,
    RunContext,
    install_agent_hooks,
    set_context,
)
from app.tests.fake_claude import FakeClaude, patched  # noqa: E402

PROMPT = "A flat washer: outer diameter 20mm, inner diameter 10.5mm, 2mm thick."
PLAN = {
    "description": "A flat washer",
    "components": ["washer"],
    "dimensions": {"overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2}},
    "constraints": {"num_holes": 1, "hole_diameter": 10.5},
}
CODE = """import cadquery as cq

outer_diameter = 20.0
bore = 10.5
thickness = 2.0

result = (cq.Workplane("XY").circle(outer_diameter / 2)
          .circle(bore / 2).extrude(thickness))
"""


class Spender(FakeClaude):
    """Answers every agent, and always rejects, so the loop never converges.

    Reports a large token count per call so the ceiling is reached in a few
    turns rather than hundreds.
    """

    def __init__(self, per_call: int = 20_000):
        super().__init__(plan=PLAN, code=[CODE] * 200,
                         verdicts=[(False, "Not yet.")] * 200)
        self.per_call = per_call

    def create(self, *, system: str = "", **kwargs):
        response = super().create(system=system, **kwargs)
        response.usage.input_tokens = self.per_call
        response.usage.output_tokens = 0
        return response


def main() -> int:
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="cadsmith_budget_"))

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # -- arithmetic --------------------------------------------------------
    print("\nCounting what a run has spent")
    spent = {"input_tokens": 30_000, "output_tokens": 2_000, "calls": 7}
    small = budget.Budget(limit=50_000)
    check("input and output are counted together",
          small.spent(spent) == 32_000, str(small.spent(spent)))
    check("remaining is what is left", small.remaining(spent) == 18_000)
    check("a run inside its allowance is not exhausted",
          not small.exhausted(spent))
    check("and check() lets it through", small.check(spent) is None)

    over = budget.Budget(limit=10_000)
    check("a run past its allowance is exhausted", over.exhausted(spent))
    try:
        over.check(spent)
        check("and check() stops it", False, "no error raised")
    except budget.BudgetExceeded as exc:
        check("and check() stops it", True)
        check("the message says what was spent and how to raise it",
              "32,000 tokens" in str(exc)
              and "CADSMITH_TOKEN_BUDGET" in str(exc), str(exc)[:120])
        check("and it is recorded on the budget", bool(over.stopped))

    check("a limit of zero disables the ceiling",
          not budget.Budget(limit=0).exhausted(spent))
    check("an empty usage dict is not a spend",
          budget.Budget(limit=10).spent({}) == 0)

    # -- money, only when the operator supplies the rates -------------------
    print("\nCost is only estimated from rates you supply")
    for name in budget.RATE_ENV:
        os.environ.pop(name, None)
    check("no rates configured means no estimate",
          budget.estimate(spent) is None)
    check("and none is reported", "estimated_cost" not in budget.summary(spent))

    os.environ[budget.RATE_ENV[0]] = "3.0"
    os.environ[budget.RATE_ENV[1]] = "15.0"
    try:
        cost = budget.estimate(spent)
        check("with rates, the estimate is input x rate + output x rate",
              cost is not None and abs(cost - (30_000 / 1e6 * 3.0
                                               + 2_000 / 1e6 * 15.0)) < 1e-9,
              f"{cost}")
        check("and it reaches the summary",
              "estimated_cost" in budget.summary(spent))
        os.environ[budget.RATE_ENV[0]] = "not a number"
        check("a malformed rate is ignored rather than fatal",
              budget.estimate(spent) is None)
    finally:
        for name in budget.RATE_ENV:
            os.environ.pop(name, None)

    print("\nThe summary carries what the client needs")
    report = budget.summary(spent, budget.Budget(limit=50_000))
    for key in ("input_tokens", "output_tokens", "calls", "total_tokens",
                "budget", "remaining"):
        check(f"summary has {key}", key in report)
    check("a stopped run says so in the summary",
          "stopped" in budget.summary(spent, over))

    # -- the case that matters ---------------------------------------------
    print("\nA pipeline that will not converge stops spending")
    install_agent_hooks()
    job_dir = work / "job"
    job_dir.mkdir(parents=True)
    sink = EventSink(path=job_dir / "events.jsonl")
    ctx = RunContext(sink=sink, job_dir=job_dir, part_name="washer")
    ctx.budget = budget.Budget(limit=60_000)      # three calls at 20k each
    set_context(ctx)

    from autofab import agents

    agents.reset_token_usage()
    pipeline = InstrumentedPipeline(
        output_dir=str(job_dir / "work"), max_error_retries=1,
        # Left high on purpose: without a budget this would run all of them.
        max_refinement_iterations=40, verbose=False, use_vision=False)

    stopped = ""
    try:
        with patched(Spender(per_call=20_000)):
            pipeline.run(PROMPT, name="washer")
        check("the run was stopped", False, "it ran to completion")
    except budget.BudgetExceeded as exc:
        stopped = str(exc)
        check("the run was stopped", True)
    except Exception as exc:                           # noqa: BLE001
        check("the run was stopped", False,
              f"stopped with {type(exc).__name__}: {exc}")

    usage = agents.get_token_usage()
    check("it stopped near the ceiling, not far past it",
          usage["input_tokens"] <= 60_000 + 20_000,
          f"{usage['input_tokens']:,} tokens over {usage['calls']} calls")
    check("which is far fewer calls than 40 iterations would take",
          usage["calls"] < 10, f"{usage['calls']} calls")
    check("and the reason is a sentence, not a stack trace",
          "budget" in stopped and "Traceback" not in stopped, stopped[:100])

    print("\nA generous budget does not interfere")
    agents.reset_token_usage()
    sink2 = EventSink(path=job_dir / "events2.jsonl")
    ctx2 = RunContext(sink=sink2, job_dir=job_dir, part_name="washer")
    ctx2.budget = budget.Budget(limit=budget.DEFAULT_BUDGET)
    set_context(ctx2)
    pipeline2 = InstrumentedPipeline(
        output_dir=str(job_dir / "work2"), max_error_retries=1,
        max_refinement_iterations=1, verbose=False, use_vision=False)
    with patched(FakeClaude(plan=PLAN, code=[CODE],
                            verdicts=[(True, "All constraints met.")])):
        result = pipeline2.run(PROMPT, name="washer")
    check("an ordinary run finishes untouched", result.converged)
    check("well inside the default allowance",
          agents.get_token_usage()["input_tokens"] < budget.DEFAULT_BUDGET,
          f"{agents.get_token_usage()['input_tokens']:,} of "
          f"{budget.DEFAULT_BUDGET:,}")

    set_context(None)
    shutil.rmtree(work, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
