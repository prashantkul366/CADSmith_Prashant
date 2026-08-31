"""AWS Bedrock as a model backend.

Bedrock is not "an Anthropic key on a different URL". It authenticates with
AWS credentials - a profile, environment variables, an SSO session or an
instance role - resolved by botocore and never passed through this app. The
model ids differ too (an ``anthropic.`` prefix). Both of those are easy to
get wrong in a way that produces a confusing error hours later, so they are
checked here.

What is *not* checked: a real call to Bedrock. That needs an AWS account with
model access, and this test must never spend someone's money or reach for
credentials it was not given. The client is constructed for real, and the
pipeline is driven through a Bedrock-shaped stand-in.

    .venv/bin/python -m app.tests.test_bedrock
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server import providers  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def test_it_is_actually_installable() -> None:
    """The dependency has to be declared, not just imported here.

    This check exists because it broke: splitting the catalogue extras out
    of requirements-app.txt truncated the file from that header onward, and
    took the Bedrock line with it. Everything still worked in a venv that
    already had boto3, and a fresh install silently lost Bedrock entirely -
    which would have surfaced as an obscure ImportError on someone else's
    machine, holding real AWS credentials.
    """
    print("\nThe dependency is declared, not just present here")
    root = Path(__file__).resolve().parents[2]
    declared = (root / "app" / "requirements-app.txt").read_text()
    check("requirements-app.txt declares the Bedrock extra",
          "anthropic[bedrock]" in declared,
          "boto3 comes from it; without it Bedrock cannot authenticate")
    try:
        import boto3  # noqa: F401
        import botocore  # noqa: F401
        ok = True
    except ImportError as error:
        ok, boto3 = False, error
    check("and it is importable in this environment", ok, str(boto3)[:60])


def test_registration() -> None:
    print("\nThe provider is offered")
    spec = providers.BUILTIN.get("bedrock")
    check("Bedrock is a known provider", spec is not None)
    if spec is None:
        return
    check("it is its own kind, not the Anthropic key path",
          spec.kind == "bedrock", spec.kind)
    check("it asks for no API key", not spec.needs_key)
    check("its models carry the Bedrock prefix",
          spec.default_generation_model.startswith("anthropic.")
          and spec.default_judge_model.startswith("anthropic."),
          f"{spec.default_generation_model} / {spec.default_judge_model}")
    check("generation and judging are still different models",
          spec.default_generation_model != spec.default_judge_model)


def test_region_is_required() -> None:
    print("\nA region is required, and named as the problem")
    providers.set_session_aws("bedrock", region="", profile="")
    saved = {name: os.environ.pop(name, None)
             for name in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    try:
        config = providers.resolve("bedrock")
        if config.aws_region:
            print(f"  ....  a region is configured on this machine "
                  f"({config.aws_region}) - skipping the missing-region case")
        else:
            issues = " ".join(providers.problems(config))
            check("no region is reported clearly",
                  "region" in issues.lower(), issues[:90])

        providers.set_session_aws("bedrock", region="us-east-1")
        config = providers.resolve("bedrock")
        check("a region chosen in the app is used",
              config.aws_region == "us-east-1", config.aws_region)

        os.environ["AWS_REGION"] = "eu-west-1"
        providers.set_session_aws("bedrock", region="")
        check("otherwise AWS_REGION is used",
              providers.resolve("bedrock").aws_region == "eu-west-1",
              providers.resolve("bedrock").aws_region)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        providers.set_session_aws("bedrock", region="", profile="")


def test_credentials_are_probed() -> None:
    print("\nCredentials are probed, not assumed")
    found, source = providers._aws_credentials_found(force=True)
    check("the probe answers without raising", isinstance(found, bool),
          f"found={found} source={source!r}")
    check("and names where they came from", bool(source), source)

    # The probe is on the health path, so it must be cheap after the first
    # call - the AWS chain can otherwise wait on instance metadata each time.
    import time
    providers._aws_credentials_found(force=True)
    started = time.time()
    for _ in range(20):
        providers._aws_credentials_found()
    elapsed = (time.time() - started) * 1000
    check("repeated probes are cached", elapsed < 50,
          f"20 calls in {elapsed:.1f}ms")


def test_no_key_field() -> None:
    print("\nIt is never presented as an API key")
    config = providers.resolve("bedrock")
    redacted = config.redacted()
    check("the config reports no key", redacted["has_key"] is False)
    check("it reports the region instead", "aws_region" in redacted)
    check("and no secret is in the redacted view",
          not any(key in redacted for key in
                  ("api_key", "aws_secret_key", "aws_access_key")),
          ", ".join(sorted(redacted)))

    entry = next(p for p in providers.status() if p["id"] == "bedrock")
    check("the picker knows it needs no key", entry["needs_key"] is False)
    check("the picker exposes the region for the form",
          "aws_region" in entry and "aws_profile" in entry)
    check("readiness is not merely 'needs no key'",
          entry["ready"] is (bool(entry["aws_region"])
                             and bool(entry["aws_credentials"])),
          f"ready={entry['ready']} region={entry['aws_region']!r} "
          f"creds={entry['aws_credentials']!r}")


def test_client_construction() -> None:
    print("\nThe client is the real Bedrock one")
    providers.set_session_aws("bedrock", region="us-east-1")
    try:
        config = providers.resolve("bedrock")
        client = providers.build_client(config)
        check("it is wrapped so the model id can be chosen by role",
              type(client).__name__ == "BedrockClient", type(client).__name__)
        check("Mantle is the default path underneath",
              type(client._inner).__name__ == "AnthropicBedrockMantle",
              type(client._inner).__name__)
        check("it speaks the Messages API directly, with no format shim",
              hasattr(client, "messages") and hasattr(client.messages, "create"))

        notes: list[str] = []
        os.environ["CADSMITH_BEDROCK_LEGACY"] = "1"
        legacy = providers.build_client(config, on_note=notes.append)
        check("the legacy InvokeModel path is available",
              type(legacy._inner).__name__ == "AnthropicBedrock",
              type(legacy._inner).__name__)
        check("and says so, rather than switching silently",
              any("legacy" in note.lower() for note in notes),
              notes[0][:60] if notes else "no note")
    finally:
        os.environ.pop("CADSMITH_BEDROCK_LEGACY", None)
        providers.set_session_aws("bedrock", region="", profile="")


def test_pipeline_runs_through_it() -> None:
    """The real agents, real CadQuery, a Bedrock-shaped client."""
    print("\nThe pipeline runs through a Bedrock-shaped client")
    from autofab import agents

    class _Block:
        def __init__(self, text): self.text = text

    class _Usage:
        input_tokens, output_tokens = 120, 40

    class _Response:
        def __init__(self, text):
            self.content, self.usage = [_Block(text)], _Usage()

    seen: list[str] = []

    class _Messages:
        def create(self, **kwargs):
            seen.append(kwargs.get("model", ""))
            return _Response(
                '{"description": "plate", "components": ["plate"], '
                '"dimensions": {"key_dimensions": {"length": 40}}, '
                '"constraints": {}, "acceptance_criteria": {}}')

    class _BedrockClient:
        messages = _Messages()

    providers.set_session_aws("bedrock", region="us-east-1")
    original = agents._get_client
    try:
        # Wrapped exactly as build_client wraps the real SDK client, so the
        # model substitution under test is the one that ships.
        config = providers.resolve("bedrock")
        wrapped = providers.BedrockClient(_BedrockClient(), config)
        agents._get_client = lambda: wrapped

        plan = agents.plan("a 40mm plate")
        check("the Planner returns a plan through it",
              isinstance(plan, dict) and plan.get("description") == "plate",
              str(plan)[:60])

        # The bug this exists for: autofab hardcodes its model ids at the
        # call site, and those are Anthropic-API ids. Sent to Bedrock
        # unchanged, the run dies on the first call naming a model nobody
        # chose.
        check("the hardcoded Anthropic id does not reach Bedrock",
              seen and not seen[0].startswith("claude-sonnet-4-5"),
              seen[0] if seen else "nothing sent")
        check("the configured Bedrock id is sent instead",
              seen and seen[0] == config.generation_model,
              f"{seen[0] if seen else '?'} (want {config.generation_model})")

        seen.clear()
        agents.evaluate_geometry(
            {"volume": 1.0, "bounding_box": {"xlen": 1, "ylen": 1, "zlen": 1}},
            {"dimensions": {}}, "a 40mm plate")
        check("the Judge gets the judge model, not the generation one",
              seen and seen[0] == config.judge_model,
              f"{seen[0] if seen else '?'} (want {config.judge_model})")
    finally:
        agents._get_client = original
        providers.set_session_aws("bedrock", region="", profile="")


def main() -> int:
    test_it_is_actually_installable()
    test_registration()
    test_region_is_required()
    test_credentials_are_probed()
    test_no_key_field()
    test_client_construction()
    test_pipeline_runs_through_it()

    print("\n  Note: no request was made to Bedrock. A real call needs an AWS")
    print("  account with Anthropic model access enabled in that region.")
    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(f"   - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
