"""A local OpenAI-compatible server, for testing the app without spending quota.

Point the Custom provider at this and the whole pipeline runs for real -
CadQuery builds the geometry, VTK renders it, the loop refines - with the
model replaced by canned replies.  Useful when an API quota is exhausted, and
better than a real key for reproducing failures, because the misbehaviour is
deterministic.

    python -m app.tools.mock_provider                     # well-behaved
    python -m app.tools.mock_provider --fail 429          # rate limited
    python -m app.tools.mock_provider --fail badjson      # prose around JSON
    python -m app.tools.mock_provider --fail novision     # refuses images
    python -m app.tools.mock_provider --fail empty        # reasoning-only reply
    python -m app.tools.mock_provider --fail hang         # never answers
    python -m app.tools.mock_provider --delay 5           # slow but working
    python -m app.tools.mock_provider --part v_pulley     # always this part

Then in the app: Provider = Custom (OpenAI-compatible), base URL
http://127.0.0.1:8123/v1, any key or none, and models "mock-coder" and
"mock-judge".

The reply is chosen from app/tools/mock_parts.py by what was asked for, so
"something to hold a rotating shaft" produces a pillow block and "a pulley for
a V belt" produces a pulley - real mechanical parts, not primitives.  Vague
requests still land somewhere sensible rather than dead-ending.

The scripted run is deliberately imperfect: the first attempt carries one
plausible mistake, the Judge rejects it in the terms an engineer would use,
and the Refiner corrects it - so the refinement loop is exercised rather than
skipped.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.tools import mock_parts


class Handler(BaseHTTPRequestHandler):
    fail = "none"
    delay = 0.0
    seen_coder_calls = 0
    part = mock_parts.PARTS[0]
    forced = None

    def log_message(self, *_args):
        return

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [{"id": "mock-coder"}, {"id": "mock-judge"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if Handler.fail == "hang":
            time.sleep(3600)
            return
        if Handler.delay:
            time.sleep(Handler.delay)
        if Handler.fail == "429":
            self._json(429, {"error": {"message": "Rate limit exceeded "
                                                  "(mock provider)"}})
            return
        if Handler.fail == "500":
            self._json(500, {"error": {"message": "Upstream exploded "
                                                  "(mock provider)"}})
            return

        messages = body.get("messages", [])
        has_image = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages)
        if has_image and Handler.fail == "novision":
            self._json(400, {"error": {"message": "This model does not support "
                                                  "image input."}})
            return

        system = next((m.get("content", "") for m in messages
                       if m.get("role") == "system"), "")
        text = self._reply_for(system if isinstance(system, str) else "",
                               self._user_text(messages))

        if Handler.fail == "empty":
            self._json(200, {
                "choices": [{"message": {"role": "assistant", "content": "",
                                         "reasoning": "thought about it"}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 0}})
            return

        self._json(200, {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 420},
        })

    # -- replies ------------------------------------------------------------

    @staticmethod
    def _user_text(messages: list) -> str:
        """The user turn, flattened - it may be a list of content blocks."""
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content
                                if part.get("type") == "text")
        return ""

    def _reply_for(self, system: str, user: str = "") -> str:
        # Every agent but the Judge is handed the original request, so the
        # part is re-chosen per call rather than trusted to stick. The Judge
        # falls back to whatever the Planner picked for this run.
        part = mock_parts.select(user) if user.strip() else Handler.part
        if Handler.forced:
            part = Handler.forced

        if "Planner Agent" in system:
            # A Planner call starts a run, so reset here: otherwise the
            # counter carries over and the second run is accepted on its
            # first attempt, skipping the refinement loop.
            Handler.seen_coder_calls = 0
            Handler.part = part
            # Echo the request into the description, as a real Planner would.
            # A fixed string would make consecutive runs indistinguishable on
            # screen, hiding whether the panel is actually being refreshed.
            plan = json.loads(json.dumps(part.plan))
            # Echo the request only, not the reference dimensions the app may
            # have appended: a real Planner writes its own prose here, so
            # letting the block through would misrepresent what grounding
            # looks like on screen.
            asked = user.split("REFERENCE DIMENSIONS")[0]
            asked = " ".join(asked.split())[:90]
            if asked:
                plan["description"] = f"{part.plan['description']} (asked: {asked})"
            return self._maybe_wrap(json.dumps(plan))
        if "Validator Agent" in system:
            # Reject the first attempt, accept what the Refiner produces.
            passed = Handler.seen_coder_calls > 1
            verdict = Handler.part.accept if passed else Handler.part.reject
            return self._maybe_wrap(json.dumps(
                {"passed": passed, "feedback": verdict}))
        if "Refiner Agent" in system or "Error Refiner" in system:
            Handler.seen_coder_calls += 1
            return part.code_fixed
        Handler.seen_coder_calls += 1
        return part.code_first

    @staticmethod
    def _maybe_wrap(payload: str) -> str:
        if Handler.fail == "badjson":
            return (f"Certainly! Here is the JSON you asked for:\n"
                    f"```json\n{payload}\n```\nLet me know if you need more.")
        return payload

    def _json(self, code: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--fail", default="none",
                        choices=["none", "429", "500", "novision", "badjson",
                                 "empty", "hang"],
                        help="Misbehave in a specific way, to test handling.")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait before each reply.")
    parser.add_argument("--part", default=None,
                        choices=[p.id for p in mock_parts.PARTS],
                        help="Always serve this part, whatever was asked for.")
    args = parser.parse_args()

    Handler.fail = args.fail
    Handler.delay = args.delay
    if args.part:
        Handler.forced = next(p for p in mock_parts.PARTS
                              if p.id == args.part)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock provider on http://127.0.0.1:{args.port}/v1  "
          f"(fail={args.fail}, delay={args.delay}s)")
    print("In the app: Provider = Custom, base URL as above, "
          "models 'mock-coder' and 'mock-judge'.")
    print(f"{len(mock_parts.PARTS)} parts, chosen by prompt: "
          + ", ".join(p.id for p in mock_parts.PARTS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
