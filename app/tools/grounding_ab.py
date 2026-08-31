"""Does grounding actually change what the Planner writes down?

The tests prove the numbers reach the model. They cannot prove the model
uses them - that needs a real model and a real key. This runs the same
prompts twice, once with the reference dimensions and once without, and
reports whether the published value came out in the design plan.

One Planner call per cell, no geometry, so a full sweep is a handful of
calls rather than a full pipeline run.

    .venv/bin/python -m app.tools.grounding_ab --provider anthropic
    .venv/bin/python -m app.tools.grounding_ab --provider custom \
        --base-url https://openrouter.ai/api/v1 --model anthropic/claude-sonnet-4.5

The interesting column is 'ungrounded'. If a model already knows a NEMA 23
takes a 38.1mm pilot bore, grounding buys nothing for that fact and the
honest thing is to say so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autofab import agents  # noqa: E402

from app.catalog import grounding  # noqa: E402
from app.server import providers  # noqa: E402
from app.server import tls  # noqa: E402

# Each case: the request, and the published values a correct plan should
# contain somewhere in its dimensions or constraints.
CASES = [
    ("NEMA 23 motor plate",
     "A mounting plate for a NEMA 23 stepper motor, 100mm square and 8mm "
     "thick. It needs a central pilot bore and the motor's bolt pattern.",
     {"pilot bore 38.1": 38.1, "bolt pattern 47.14": 47.14}),
    ("NEMA 17 motor plate",
     "A mounting plate for a NEMA 17 stepper motor, 60mm square and 6mm "
     "thick, with a pilot bore and the motor's bolt pattern.",
     {"pilot bore 22": 22.0, "bolt pattern 31": 31.0}),
    ("M8 clearance holes",
     "A steel bar 120mm x 30mm x 10mm with four holes for M8 socket head "
     "cap screws to pass through freely.",
     {"clearance hole 9.0": 9.0}),
    ("M8 counterbore",
     "A plate 80mm square and 20mm thick with a counterbored hole so an M8 "
     "socket head cap screw sits flush with the surface.",
     {"counterbore 14": 14.0, "head height 8": 8.0}),
    ("6203 bearing housing",
     "A bearing housing block to take a 6203 deep groove ball bearing, with "
     "a shoulder to locate it.",
     {"bearing OD 40": 40.0, "bearing width 12": 12.0}),
]


def numbers_in(payload) -> set[float]:
    """Every number anywhere in the plan, rounded to two places."""
    found: set[float] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            found.add(round(float(node), 2))
        elif isinstance(node, str):
            import re
            for token in re.findall(r"\d+(?:\.\d+)?", node):
                found.add(round(float(token), 2))

    walk(payload)
    return found


def run_case(prompt: str, grounded: bool) -> tuple[dict | None, str]:
    text = grounding.ground(prompt)[0] if grounded else prompt
    try:
        return agents.plan(text), ""
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="",
                        help="Prefer the provider's environment variable.")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    tls.configure()
    config = providers.resolve(
        provider_id=args.provider,
        generation_model=args.model or "",
        judge_model=args.model or "",
    )
    # resolve() reads keys and base URLs from the environment, as the server
    # does; the flags are an override for a one-off run.
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key
    problems = providers.problems(config)
    if problems:
        print("Cannot run:", " ".join(problems))
        return 2

    # Point the agents at the chosen backend, the same way the app does.
    if args.provider != "anthropic":
        agents._get_client = lambda: providers.build_client(config)

    print(f"\nProvider {config.provider}  model {config.generation_model}")
    print(f"{len(CASES)} cases x 2 = {len(CASES) * 2} Planner calls\n")

    rows, results = [], []
    for label, prompt, expected in CASES:
        row = {"case": label, "expected": expected}
        for grounded in (False, True):
            plan, error = run_case(prompt, grounded)
            key = "grounded" if grounded else "ungrounded"
            if plan is None:
                row[key] = None
                row[key + "_error"] = error
                continue
            seen = numbers_in(plan)
            row[key] = {name: (round(value, 2) in seen)
                        for name, value in expected.items()}
            row[key + "_plan"] = plan
            time.sleep(0.5)
        rows.append(row)

        def render(cell):
            if cell is None:
                return "ERROR"
            return " ".join(("YES" if ok else " no") for ok in cell.values())

        print(f"  {label:<24} ungrounded [{render(row.get('ungrounded'))}]"
              f"   grounded [{render(row.get('grounded'))}]")
        results.append(row)

    hits = {"ungrounded": 0, "grounded": 0}
    total = 0
    for row in rows:
        for key in hits:
            cell = row.get(key)
            if isinstance(cell, dict):
                hits[key] += sum(1 for ok in cell.values() if ok)
        total += len(row["expected"])

    print(f"\n  facts correct, ungrounded: {hits['ungrounded']}/{total}")
    print(f"  facts correct, grounded  : {hits['grounded']}/{total}")
    delta = hits["grounded"] - hits["ungrounded"]
    print(f"  difference               : {delta:+d}")
    if delta <= 0:
        print("\n  Grounding did not help this model on these cases. That is a"
              "\n  real result - report it rather than assuming the feature works.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\n  full plans written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
