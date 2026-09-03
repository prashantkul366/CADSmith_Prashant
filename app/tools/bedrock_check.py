"""Check a Bedrock setup before spending anything real on it.

The expensive way to discover that a model id is wrong for your region, or
that your SSO session expired, or that the account has Claude enabled but not
the model you named, is to start a run: five agents, a vision Judge, several
refinement rounds, and a bill for all of it before the failure surfaces.

This does the same discovery for the price of one very small completion.

    .venv/bin/python -m app.tools.bedrock_check
    .venv/bin/python -m app.tools.bedrock_check --region eu-west-1
    .venv/bin/python -m app.tools.bedrock_check --no-call     # never bills

Every step reports separately, so a failure says which one broke rather than
"Bedrock did not work". Nothing here needs an Anthropic key: Bedrock
authenticates with your AWS credentials, resolved by botocore exactly as the
AWS CLI resolves them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server import budget, providers  # noqa: E402

#: The smallest useful request: a handful of input tokens, a two-token answer.
#: Enough to prove the whole path - credentials, region, model access, quota -
#: without being worth worrying about on any price list.
PROBE = [{"role": "user", "content": "Reply with the single word: ready"}]
PROBE_MAX_TOKENS = 8


def line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "  ok ", False: "FAIL ", None: "  -- "}[ok]
    print(f"{mark} {label}" + (f": {detail}" if detail else ""), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default="",
                        help="AWS region; otherwise AWS_REGION or your profile")
    parser.add_argument("--profile", default="",
                        help="AWS named profile; otherwise AWS_PROFILE")
    parser.add_argument("--model", default="",
                        help="Bedrock model id to probe; otherwise the app's "
                             "default generation model")
    parser.add_argument("--no-call", action="store_true",
                        help="check credentials and configuration only, and "
                             "never send a request")
    args = parser.parse_args()

    if args.region:
        os.environ["AWS_REGION"] = args.region
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    spec = providers.BUILTIN.get("bedrock")
    if spec is None:
        line(False, "the app knows about Bedrock")
        return 1

    print("\nConfiguration")
    region = providers._region_for(spec)
    line(bool(region), "region",
         region or "not set - export AWS_REGION or pass --region")
    profile = providers._profile_for(spec)
    line(None, "profile", profile or "none (using the default chain)")

    print("\nCredentials")
    found, where = providers._aws_credentials_found(force=True)
    line(found, "botocore can resolve credentials", where)
    if not found:
        print("\n  Run `aws sso login` (or set AWS_PROFILE) and try again.")
        return 1

    config = providers.resolve("bedrock")
    model = args.model or config.generation_model
    print("\nModels the app will ask for")
    line(None, "generation", config.generation_model)
    line(None, "judge", config.judge_model)
    if not str(model).startswith(("anthropic.", "us.", "eu.", "apac.")):
        line(None, "note",
             f"{model!r} has no Bedrock prefix. Bedrock ids look like "
             f"'anthropic.claude-sonnet-5'; a cross-region inference profile "
             f"looks like 'us.anthropic.claude-sonnet-5'.")

    print("\nSpend guard")
    line(True, "token budget per run", f"{budget.DEFAULT_BUDGET:,} tokens")
    rates = budget.rates()
    line(None, "your per-MTok rates",
         f"in ${rates[0]}, out ${rates[1]}" if rates else
         f"not set - optional, from your AWS pricing page, via "
         f"{' and '.join(budget.RATE_ENV)}")

    if args.no_call:
        print("\nStopping before the probe (--no-call). Nothing was billed.")
        return 0

    print(f"\nProbe ({PROBE_MAX_TOKENS} max output tokens)")
    problems = providers.problems(config)
    if problems:
        for problem in problems:
            line(False, "configuration", problem)
        return 1

    try:
        client = providers.build_client(config)
        response = client.messages.create(
            model=model, max_tokens=PROBE_MAX_TOKENS,
            system="Answer in one word.", messages=PROBE)
    except Exception as exc:                           # noqa: BLE001
        line(False, f"call {model}", f"{type(exc).__name__}: {exc}")
        print("\n  A validation error naming the model usually means the id "
              "\n  is not available in this region, or the account has not "
              "\n  been granted access to it. `aws bedrock "
              "list-foundation-models"
              "\n  --region " + (region or "<region>") + "` lists what you can "
              "actually call;"
              "\n  models needing a cross-region inference profile are listed "
              "by"
              "\n  `aws bedrock list-inference-profiles`.")
        return 1

    text = "".join(getattr(b, "text", "") for b in response.content).strip()
    usage = getattr(response, "usage", None)
    line(True, f"call {model}", f"replied {text!r}")
    if usage is not None:
        spent = {"input_tokens": usage.input_tokens,
                 "output_tokens": usage.output_tokens, "calls": 1}
        line(None, "this probe spent",
             f"{spent['input_tokens']} in, {spent['output_tokens']} out")
        cost = budget.estimate(spent)
        if cost is not None:
            line(None, "at your rates", f"${cost:.6f}")

    print("\nBedrock is reachable and the model answers. "
          "Pick it in the app's provider list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
