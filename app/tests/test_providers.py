"""Check the pipeline against a non-Anthropic backend.

A real HTTP server speaking the OpenAI chat-completions API stands in for
OpenAI or a local Ollama, so the adapter is exercised over the wire: message
translation, the base64 image the Judge sends, token accounting, JSON replies
wrapped in prose, and the retry when a model refuses images.

CadQuery and VTK do real work throughout - only the model is substituted.

Run:  .venv/bin/python -m app.tests.test_providers
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.server import providers  # noqa: E402
from app.server.jobs import (  # noqa: E402
    JobManager, JobOptions, STATUS_DONE, STATUS_ERROR)
from app.server.providers import (  # noqa: E402
    LLMConfig, OpenAICompatibleClient, repair_json)

PROMPT = "A flat washer, 20mm outer diameter, 10.5mm bore, 2mm thick."

PLAN = {
    "description": "Flat washer",
    "components": ["annular disc"],
    "dimensions": {"overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2},
                   "key_dimensions": {"outer_diameter": 20, "thickness": 2}},
    "constraints": {"num_holes": 1},
    "acceptance_criteria": {"volume_error_threshold_pct": 5},
    "notes": "Concentric circles extruded.",
}

CODE = """import cadquery as cq

outer_diameter = 20.0
bore = 10.5
thickness = 2.0

result = (
    cq.Workplane('XY')
    .circle(outer_diameter / 2.0)
    .circle(bore / 2.0)
    .extrude(thickness)
)
"""


class FakeOpenAIServer(BaseHTTPRequestHandler):
    """A minimal chat-completions server that records what it was sent."""

    requests: list[dict] = []
    reject_images: bool = False
    wrap_json_in_prose: bool = True

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"data": [{"id": "fake-large"}, {"id": "fake-small"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeOpenAIServer.requests.append(body)

        has_image = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in body.get("messages", []))

        if has_image and FakeOpenAIServer.reject_images:
            self._json(400, {"error": {
                "message": "This model does not support image input."}})
            return

        system = next((m["content"] for m in body.get("messages", [])
                       if m.get("role") == "system"), "")
        text = self._reply_for(system)
        self._json(200, {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 567},
        })

    def _reply_for(self, system: str) -> str:
        if "Planner Agent" in system:
            payload = json.dumps(PLAN)
            return (f"Sure, here is the plan:\n```json\n{payload}\n```\nHope "
                    f"that helps!") if FakeOpenAIServer.wrap_json_in_prose \
                else payload
        if "Validator Agent" in system:
            return json.dumps({"passed": True, "feedback": "All constraints met."})
        return CODE

    def _json(self, code: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    server = HTTPServer(("127.0.0.1", 0), FakeOpenAIServer)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/v1"

    print("\nJSON recovered from a chatty reply")
    check("prose around a fenced object",
          json.loads(repair_json('Sure!\n```json\n{"a": 1}\n```\nDone.'))["a"] == 1)
    check("prose around a bare object",
          json.loads(repair_json('Here you go: {"a": 2} — enjoy'))["a"] == 2)
    check("braces inside strings survive",
          json.loads(repair_json('x {"a": "} not the end", "b": 3} y'))["b"] == 3)
    clean = '{"already": "fine"}'
    check("a clean reply is untouched", repair_json(clean) == clean)
    check("non-JSON is returned as-is", repair_json("no object here") == "no object here")

    print("\nProvider registry")
    providers.set_session_key("custom", api_key="test-key", base_url=base_url)
    entry = next(p for p in providers.status() if p["id"] == "custom")
    check("a session key marks the provider ready", entry["ready"])
    check("the key came from the session", entry["key_from_session"])
    check("no key is ever exposed", "api_key" not in entry and "key" not in entry,
          str(sorted(entry)))
    check("models are read from the provider",
          providers.list_models("custom") == ["fake-large", "fake-small"])

    config = providers.resolve("custom", generation_model="fake-small",
                               judge_model="fake-large")
    check("configuration resolves", not providers.problems(config),
          str(providers.problems(config)))
    check("redaction hides the key",
          "api_key" not in config.redacted() and config.redacted()["has_key"])

    print("\nRunning the pipeline on the fake provider")
    FakeOpenAIServer.requests.clear()
    runs = Path(tempfile.mkdtemp(prefix="cadsmith_providers_"))
    manager = JobManager(runs)
    job = manager.create(PROMPT, JobOptions(
        max_iterations=1, use_vision=True, provider="custom",
        generation_model="fake-small", judge_model="fake-large"))

    deadline = time.time() + 300
    while job.status not in (STATUS_DONE, STATUS_ERROR):
        if time.time() > deadline:
            raise TimeoutError("job did not finish")
        time.sleep(0.3)

    check("job converged", job.converged, job.error or "")
    check("real geometry was built",
          (job.directory / "v0" / "model.stl").exists())
    geometry = json.loads((job.directory / "v0" / "geometry.json").read_text())
    check("the kernel measured the washer",
          abs(geometry["bounding_box"]["xlen"] - 20.0) < 1e-6
          and geometry["is_valid"])

    sent = FakeOpenAIServer.requests
    check("every agent reached the provider", len(sent) >= 3, f"{len(sent)} calls")
    models_used = {r["model"] for r in sent}
    check("generation used the generation model", "fake-small" in models_used)
    check("judging used the judge model", "fake-large" in models_used,
          str(models_used))

    judge_calls = [r for r in sent if r["model"] == "fake-large"]
    judge_content = judge_calls[0]["messages"][-1]["content"]
    check("the render reached the Judge as a data URL",
          isinstance(judge_content, list)
          and any(p.get("type") == "image_url"
                  and p["image_url"]["url"].startswith("data:image/png;base64,")
                  for p in judge_content))
    check("the system prompt is a system message",
          any(m["role"] == "system" for m in judge_calls[0]["messages"]))
    check("token usage was mapped from the OpenAI shape",
          job.tokens.get("input_tokens", 0) >= 1234
          and job.tokens.get("output_tokens", 0) >= 567,
          str(job.tokens))

    print("\nA model that refuses images falls back instead of failing")
    FakeOpenAIServer.requests.clear()
    FakeOpenAIServer.reject_images = True
    notes: list[str] = []
    client = OpenAICompatibleClient(
        LLMConfig(provider="custom", kind="openai_compatible", base_url=base_url,
                  api_key="test-key", generation_model="fake-small",
                  judge_model="fake-large"),
        on_note=notes.append)

    from autofab import agents

    response = client.messages.create(
        model="ignored", max_tokens=1024, system=agents.VALIDATOR_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "aGVsbG8="}},
            {"type": "text", "text": "Evaluate and return JSON."},
        ]}])
    check("the Judge still answered",
          json.loads(response.content[0].text)["passed"] is True)
    check("it retried without the image", len(FakeOpenAIServer.requests) == 2,
          f"{len(FakeOpenAIServer.requests)} attempts")
    check("the fallback was reported, not hidden",
          notes and "kernel metrics alone" in notes[0],
          str(notes))
    FakeOpenAIServer.reject_images = False

    print("\nThe default provider is left alone")
    providers.clear_session_keys()
    default = providers.resolve("anthropic")
    check("still the models the pipeline itself uses",
          default.generation_model == "claude-sonnet-4-5-20250929"
          and default.judge_model == "claude-opus-4-20250514",
          f"{default.generation_model} / {default.judge_model}")
    check("and still the real SDK client",
          type(providers.build_client(
              LLMConfig(provider="anthropic", kind="anthropic", base_url="",
                        api_key="x", generation_model="m", judge_model="m")
          )).__module__.startswith("anthropic"))

    server.shutdown()
    shutil.rmtree(runs, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
