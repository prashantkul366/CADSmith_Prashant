"""Preflight check: prove every layer works before starting the app.

Runs the same operations the pipeline does, in the order it does them, but
with short timeouts and one thing at a time - so a failure names the layer
that is broken instead of appearing as a run that hangs on "Planning the
part".

The model checks are the ones that usually matter.  A backend can be
reachable and still be unusable: rate-limited, too slow, unable to accept the
rendered image the Judge needs, or unwilling to answer in the strict JSON the
Planner parses.  Each of those is checked separately.

Usage:
    python -m app.tools.doctor
    python -m app.tools.doctor --provider custom \
        --generation-model minimax/minimax-m3:free \
        --judge-model minimax/minimax-m3:free
    python -m app.tools.doctor --skip-models        # offline checks only
    python -m app.tools.doctor --timeout 45         # per model call

No key is ever printed: only whether one is set, and its last four characters.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 1x1 PNG, used only if a real render could not be produced.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
    "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")
if os.name == "nt" and not os.getenv("WT_SESSION"):
    GREEN = RED = YELLOW = DIM = RESET = ""   # older consoles show the codes

_failures: list[str] = []
_warnings: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}PASS{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def warn(label: str, detail: str = "", fix: str = "") -> None:
    print(f"  {YELLOW}WARN{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    if fix:
        print(f"        {DIM}-> {fix}{RESET}")
    _warnings.append(label)


def fail(label: str, detail: str = "", fix: str = "") -> None:
    print(f"  {RED}FAIL{RESET}  {label}" + (f"  {detail}" if detail else ""))
    if fix:
        print(f"        -> {fix}")
    _failures.append(label)


# ---------------------------------------------------------------------------


def check_python() -> None:
    section("Interpreter")
    version = sys.version_info
    detail = f"{platform.python_version()} on {platform.system()}"
    if version < (3, 10):
        fail("Python version", detail, "CadQuery needs 3.10 or newer; 3.11 is tested.")
    else:
        ok("Python version", detail)
    ok("running from", str(ROOT))


def check_packages() -> None:
    section("Packages")
    required = [
        ("cadquery", "the CAD kernel"),
        ("vtk", "three-view rendering for the Judge"),
        ("trimesh", "benchmark metrics"),
        ("scipy", "benchmark metrics"),
        ("numpy", ""),
        ("fastapi", "web server"),
        ("uvicorn", "web server"),
        ("httpx", "non-Anthropic providers"),
        ("anthropic", "the Anthropic provider"),
        ("dotenv", ".env loading"),
    ]
    missing = []
    for name, why in required:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "") or getattr(
                module, "VTK_VERSION", "")
            ok(name, str(version))
        except Exception as exc:
            missing.append(name)
            fail(name, f"{type(exc).__name__}: {exc}", why)
    if missing:
        print(f"\n        -> pip install -r app{os.sep}requirements-app.txt")


def check_cad_kernel() -> None:
    section("CAD kernel")
    try:
        from autofab.executor import Executor
    except Exception as exc:
        fail("import autofab.executor", str(exc))
        return

    work = Path(tempfile.mkdtemp(prefix="cadsmith_doctor_"))
    code = ("import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10, 5).faces('>Z').hole(4)\n")
    started = time.time()
    try:
        result = Executor(output_dir=str(work), timeout_seconds=120).execute(
            code, name="doctor")
    except Exception as exc:
        fail("execute a script", f"{type(exc).__name__}: {exc}")
        return

    if not result.success:
        fail("execute a script", (result.error or "")[-300:],
             "CadQuery is installed but cannot build geometry.")
        return

    geometry = result.geometry_json or {}
    bbox = geometry.get("bounding_box", {})
    correct = (abs(bbox.get("xlen", 0) - 20) < 1e-6
               and abs(bbox.get("ylen", 0) - 10) < 1e-6
               and abs(bbox.get("zlen", 0) - 5) < 1e-6)
    if correct and geometry.get("is_valid"):
        ok("build and measure a solid",
           f"20x10x5mm, watertight, {(time.time() - started) * 1000:.0f}ms")
    else:
        fail("build and measure a solid", json.dumps(bbox))

    for label, path in (("STL export", result.stl_path),
                        ("STEP export", result.step_path)):
        if path and Path(path).exists() and Path(path).stat().st_size > 0:
            ok(label, f"{Path(path).stat().st_size:,} bytes")
        else:
            fail(label, "file missing or empty")

    globals()["_stl_path"] = result.stl_path


def check_rendering() -> None:
    section("Rendering (the vision Judge's three views)")
    stl = globals().get("_stl_path")
    if not stl or not Path(stl).exists():
        warn("render", "skipped - no STL from the kernel check")
        return
    try:
        from autofab.render import render_stl_to_png
    except Exception as exc:
        fail("import autofab.render", str(exc))
        return

    png = Path(tempfile.mkdtemp(prefix="cadsmith_doctor_")) / "render.png"
    started = time.time()
    try:
        render_stl_to_png(stl, str(png))
    except Exception as exc:
        fail("offscreen render", f"{type(exc).__name__}: {exc}",
             "On headless Linux: sudo apt-get install libosmesa6. "
             "Without this the Judge runs on kernel metrics alone.")
        return

    if png.exists() and png.stat().st_size > 1000:
        ok("offscreen render",
           f"{png.stat().st_size:,} bytes, {(time.time() - started) * 1000:.0f}ms")
        globals()["_render_png"] = png.read_bytes()
    else:
        fail("offscreen render", "produced no image")


def check_configuration(args) -> dict:
    section("Configuration")
    try:
        from dotenv import load_dotenv

        env_file = ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            ok(".env found", str(env_file))
        else:
            warn(".env not found", str(env_file),
                 "Fine if you set variables another way.")
    except Exception:
        warn("python-dotenv unavailable", "", "Environment variables still work.")

    interesting = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CADSMITH_LLM_API_KEY",
                   "CADSMITH_LLM_BASE_URL", "OLLAMA_BASE_URL",
                   "LMSTUDIO_BASE_URL", "CADSMITH_LLM_TIMEOUT"]
    for name in interesting:
        value = os.getenv(name)
        if not value:
            continue
        if name.endswith("KEY"):
            ok(name, f"set, {len(value)} chars, ends ...{value[-4:]}")
        else:
            ok(name, value)

    try:
        from app.server import providers
    except Exception as exc:
        fail("import app.server.providers", str(exc))
        return {}

    # Only the provider you actually selected can be a problem; the others
    # being unconfigured is the normal case, not a caveat.
    print()
    for entry in providers.status():
        if entry["ready"]:
            ok(f"provider {entry['id']}", f"ready - {entry['base_url'] or 'SDK'}")
        else:
            print(f"  {DIM}....  provider {entry['id']}  not configured{RESET}")

    config = providers.resolve(
        provider_id=args.provider,
        generation_model=args.generation_model,
        judge_model=args.judge_model,
    )
    issues = providers.problems(config)
    print()
    if issues:
        for issue in issues:
            fail(f"selected provider '{args.provider}'", issue)
    else:
        ok(f"selected provider '{args.provider}'",
           f"gen={config.generation_model} judge={config.judge_model}")
        if config.generation_model == config.judge_model:
            warn("one model for both roles", config.judge_model,
                 "The Judge grades its own work. Works, but it is not an "
                 "independent check.")
    return {"config": config, "providers": providers}


def _chat(config, model: str, messages: list, timeout: float,
          max_tokens: int = 32) -> tuple[bool, str, float, str]:
    """One raw chat call. Returns (ok, text, seconds, error)."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    started = time.time()
    try:
        response = httpx.post(
            f"{config.base_url}/chat/completions", headers=headers,
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": 0},
            timeout=timeout)
    except httpx.TimeoutException:
        return False, "", time.time() - started, f"no response within {timeout:.0f}s"
    except httpx.HTTPError as exc:
        return False, "", time.time() - started, f"{type(exc).__name__}: {exc}"

    elapsed = time.time() - started
    if response.status_code >= 400:
        return False, "", elapsed, f"HTTP {response.status_code}: {response.text[:200]}"

    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        return False, "", elapsed, f"no choices: {json.dumps(payload)[:200]}"
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    if not text.strip() and message.get("reasoning"):
        return False, "", elapsed, ("empty content, reasoning-only reply - this "
                                    "model returns its answer in a 'reasoning' "
                                    "field the pipeline does not read")
    return True, text, elapsed, ""


def check_models(args, resolved: dict) -> None:
    section("Model backend")
    if args.skip_models:
        warn("model checks", "skipped (--skip-models)")
        return
    config = resolved.get("config")
    providers = resolved.get("providers")
    if config is None or providers.problems(config):
        warn("model checks", "skipped - provider is not configured")
        return

    if config.kind == "anthropic":
        _check_anthropic(config, args)
        return

    # 1. Can we reach it at all?
    models = providers.list_models(config.provider, timeout=args.timeout)
    if models:
        ok("reachable", f"{len(models)} models offered")
        for role, name in (("generation", config.generation_model),
                           ("judge", config.judge_model)):
            if name not in models:
                warn(f"{role} model not in the list", name,
                     "It may still work; the list can be partial.")
    else:
        warn("model list", "empty or unavailable",
             "Not fatal, but suggests the endpoint or key may be wrong.")

    # 2. Does the generation model actually answer, and how fast?
    good, text, seconds, error = _chat(
        config, config.generation_model,
        [{"role": "user", "content": "Reply with exactly: ok"}], args.timeout)
    if good:
        ok("generation model answers", f"{seconds:.1f}s, {text.strip()[:20]!r}")
        if seconds > 25:
            warn("generation model is slow", f"{seconds:.1f}s for a trivial reply",
                 "A full part needs 3-6 calls, so expect minutes.")
    else:
        fail("generation model answers", error, _advice(error))
        return

    # 3. Strict JSON - the Planner parses the reply with json.loads.
    good, text, seconds, error = _chat(
        config, config.generation_model,
        [{"role": "system", "content": "Output ONLY valid JSON, no other text."},
         {"role": "user", "content": 'Return {"ok": true} and nothing else.'}],
        args.timeout, max_tokens=200)
    if not good:
        fail("strict JSON reply", error)
    else:
        from app.server.providers import repair_json

        try:
            json.loads(text)
            ok("strict JSON reply", "clean")
        except Exception:
            try:
                json.loads(repair_json(text))
                warn("strict JSON reply", "wrapped in prose",
                     "The app extracts the object, so this is handled.")
            except Exception:
                fail("strict JSON reply", repr(text[:120]),
                     "The Planner will fail on this model. Try another.")

    # 4. Vision - the Judge sends the three-view render.
    image = globals().get("_render_png") or _TINY_PNG
    encoded = base64.standard_b64encode(image).decode()
    good, text, seconds, error = _chat(
        config, config.judge_model,
        [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            {"type": "text", "text": "Reply with exactly: seen"},
        ]}], args.timeout)
    size_note = f"{len(encoded) / 1024:.0f}KB payload, {seconds:.1f}s"
    if good:
        ok("judge model accepts images", size_note)
    else:
        warn("judge model accepts images", error[:160],
             "The Judge will fall back to kernel metrics only, which is the "
             "paper's no-vision ablation. Switch off Vision Judge, or pick a "
             "vision-capable model.")


def _check_anthropic(config, args) -> None:
    try:
        import anthropic
    except Exception as exc:
        fail("anthropic SDK", str(exc))
        return
    client = anthropic.Anthropic(api_key=config.api_key)
    started = time.time()
    try:
        response = client.messages.create(
            model=config.generation_model, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}])
        ok("generation model answers",
           f"{time.time() - started:.1f}s, {response.content[0].text.strip()[:20]!r}")
    except Exception as exc:
        fail("generation model answers", f"{type(exc).__name__}: {exc}",
             _advice(str(exc)))


def _advice(error: str) -> str:
    lowered = error.lower()
    if "429" in lowered or "rate" in lowered:
        return ("Rate limited. Free-tier limits are per account, not per key - "
                "wait a minute, pick another model, or add credit.")
    if "401" in lowered or "403" in lowered or "auth" in lowered:
        return "The key was rejected. Check it is current and pasted whole."
    if "no response within" in lowered:
        return ("The endpoint accepted the connection but never replied. Check "
                "the base URL, and any corporate proxy or TLS interception.")
    if "404" in lowered:
        return "Model id not found at this endpoint. Check spelling."
    if "connect" in lowered or "resolve" in lowered:
        return "Could not reach the host. Check the base URL and your network."
    return ""


def check_port(args) -> None:
    section("Server port")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            warn(f"port {args.port}", "already in use",
                 "The app may already be running, or use a different --port.")
        else:
            ok(f"port {args.port}", "free")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=os.getenv("CADSMITH_PROVIDER", "custom"))
    parser.add_argument("--generation-model", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Seconds to wait for each model call (default 30). "
                             "The app itself waits far longer.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()

    print("CADSmith preflight")
    print("=" * 58)

    check_python()
    check_packages()
    check_cad_kernel()
    check_rendering()
    resolved = check_configuration(args)
    check_models(args, resolved)
    check_port(args)

    print("\n" + "=" * 58)
    if _failures:
        print(f"{RED}{len(_failures)} problem(s){RESET}: {', '.join(_failures)}")
        print("Fix these before starting the app.")
        return 1
    if _warnings:
        print(f"{YELLOW}Ready, with {len(_warnings)} caveat(s){RESET}: "
              f"{', '.join(_warnings)}")
        return 0
    print(f"{GREEN}Everything checks out.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
