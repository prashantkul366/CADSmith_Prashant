"""A hard ceiling on what one run may spend.

The pipeline is a loop: plan, code, execute, judge, refine, repeat.  Every
turn of it is a paid model call, and the vision Judge sends a rendered image
each time, so the input side grows faster than the output side.  On a metered
backend like Bedrock a runaway loop is not a slow run, it is a bill.

So a run carries a token budget.  It is checked before each model call, and
when the next call would exceed it the run stops with an explanation instead
of continuing.  Tokens rather than dollars, for two reasons: tokens are what
the API actually reports, exactly, on every call; and Bedrock is billed by
AWS at AWS's own rates, which this app has no business guessing.

If you want the ceiling expressed in money, give it the rates from your own
AWS pricing page - see ``RATE_ENV``.  Nothing here invents a price.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: Tokens per run, counting input and output together. Generous enough that a
#: normal part - five agents, two refinement rounds, a vision judge each time -
#: finishes well inside it, and low enough that a loop which is not converging
#: stops being expensive. Measured runs on this app land around 40k.
DEFAULT_BUDGET = int(os.getenv("CADSMITH_TOKEN_BUDGET", "250000"))

#: Where per-million-token rates come from, when you want a cost estimate.
#: Deliberately unset by default and deliberately not shipped with values:
#: Claude on Bedrock is partner-operated and priced by AWS, per region and per
#: model, and a number hardcoded here would be a guess presented as a fact.
#: Read them off https://aws.amazon.com/bedrock/pricing/ for your region.
RATE_ENV = ("CADSMITH_INPUT_PER_MTOK", "CADSMITH_OUTPUT_PER_MTOK")


class BudgetExceeded(RuntimeError):
    """The run reached its token ceiling and was stopped."""


@dataclass
class Budget:
    """What a run may spend, and what it has spent so far."""

    limit: int = DEFAULT_BUDGET
    #: Set once the ceiling is hit, so the reason survives into the run record.
    stopped: str = ""

    def spent(self, usage: dict) -> int:
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    def remaining(self, usage: dict) -> int:
        return max(0, self.limit - self.spent(usage))

    def exhausted(self, usage: dict) -> bool:
        return self.limit > 0 and self.spent(usage) >= self.limit

    def check(self, usage: dict) -> None:
        """Raise if this run has already spent its allowance.

        Checked between calls rather than mid-call: a call already in flight
        cannot be un-billed, so the useful moment to stop is before the next
        one starts.
        """
        if not self.exhausted(usage):
            return
        spent = self.spent(usage)
        self.stopped = (
            f"This run reached its {self.limit:,}-token budget after "
            f"{usage.get('calls', 0)} model calls ({spent:,} tokens), so it "
            f"was stopped before spending more. The attempts it did produce "
            f"are still here. Raise CADSMITH_TOKEN_BUDGET if this part "
            f"genuinely needs more, or lower the refinement iterations."
        )
        raise BudgetExceeded(self.stopped)


def rates() -> Optional[tuple[float, float]]:
    """Your own per-million-token rates, if you have supplied them."""
    raw_in, raw_out = (os.getenv(name) for name in RATE_ENV)
    if not raw_in or not raw_out:
        return None
    try:
        return float(raw_in), float(raw_out)
    except ValueError:
        return None


def estimate(usage: dict) -> Optional[float]:
    """What this run cost, in the currency of the rates you supplied.

    ``None`` when no rates are configured, which is the default. An estimate
    nobody asked for, built on a price nobody confirmed, is worse than no
    estimate at all.
    """
    configured = rates()
    if configured is None:
        return None
    per_input, per_output = configured
    return (int(usage.get("input_tokens", 0)) / 1e6 * per_input
            + int(usage.get("output_tokens", 0)) / 1e6 * per_output)


def summary(usage: dict, budget: Optional[Budget] = None) -> dict:
    """Spend so far, for the run record and the client."""
    budget = budget or Budget()
    out = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "calls": int(usage.get("calls", 0)),
        "total_tokens": budget.spent(usage),
        "budget": budget.limit,
        "remaining": budget.remaining(usage),
    }
    cost = estimate(usage)
    if cost is not None:
        out["estimated_cost"] = round(cost, 4)
    if budget.stopped:
        out["stopped"] = budget.stopped
    return out
