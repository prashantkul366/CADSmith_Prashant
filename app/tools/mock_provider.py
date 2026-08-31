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

Then in the app: Provider = Custom (OpenAI-compatible), base URL
http://127.0.0.1:8123/v1, any key or none, and models "mock-coder" and
"mock-judge".

The scripted run is deliberately imperfect: the first attempt comes out too
thick, the Judge rejects it, and the Refiner corrects it - so the refinement
loop is exercised rather than skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAN = {
    "description": "Part planned by the mock provider",
    "components": ["primary body", "through feature"],
    "dimensions": {
        "overall_bbox": {"xlen": 40, "ylen": 30, "zlen": 10},
        "key_dimensions": {"length": 40, "width": 30, "thickness": 10,
                           "hole_diameter": 8},
    },
    "constraints": {"volume_estimate": 11500, "num_holes": 1,
                    "hole_diameter": 8, "symmetry": "mirror about both axes"},
    "acceptance_criteria": {"volume_error_threshold_pct": 5,
                            "bbox_iou_threshold": 0.90},
    "notes": "Scripted reply from app/tools/mock_provider.py.",
}

# First attempt: 20mm thick, which the Judge will reject.
CODE_FIRST = """import cadquery as cq

length = 40.0
width = 30.0
thickness = 20.0
hole_diameter = 8.0

# Plate with a central through hole
result = (
    cq.Workplane('XY')
    .box(length, width, thickness, centered=(True, True, False))
    .faces('>Z')
    .hole(hole_diameter)
)
"""

# After refinement: the thickness the plan actually asked for.
CODE_FIXED = CODE_FIRST.replace("thickness = 20.0", "thickness = 10.0")

REJECT = ("The part measures 20.0mm in Z but the plan specifies a 10mm "
          "thickness, and the front profile view shows it standing far taller "
          "than a plate of this footprint should. Set thickness to 10.0.")
ACCEPT = ("All constraints met. The bounding box is 40.0 x 30.0 x 10.0mm and "
          "the central bore is clearly visible in the high-angle view. The "
          "solid is watertight.")


class Handler(BaseHTTPRequestHandler):
    fail = "none"
    delay = 0.0
    seen_coder_calls = 0

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
        if "Planner Agent" in system:
            # A Planner call starts a run, so reset here: otherwise the
            # counter carries over and the second run is accepted on its
            # first attempt, skipping the refinement loop.
            Handler.seen_coder_calls = 0
            # Echo the request into the description, as a real Planner would.
            # A fixed string would make consecutive runs indistinguishable on
            # screen, hiding whether the panel is actually being refreshed.
            plan = dict(PLAN)
            asked = " ".join(user.split())[:90]
            plan["description"] = (f"Mock plan for: {asked}" if asked
                                   else PLAN["description"])
            return self._maybe_wrap(json.dumps(plan))
        if "Validator Agent" in system:
            # Reject the first attempt, accept what the Refiner produces.
            passed = Handler.seen_coder_calls > 1
            return self._maybe_wrap(json.dumps({
                "passed": passed, "feedback": ACCEPT if passed else REJECT}))
        if "Refiner Agent" in system or "Error Refiner" in system:
            Handler.seen_coder_calls += 1
            return CODE_FIXED
        Handler.seen_coder_calls += 1
        return CODE_FIRST

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
    args = parser.parse_args()

    Handler.fail = args.fail
    Handler.delay = args.delay

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock provider on http://127.0.0.1:{args.port}/v1  "
          f"(fail={args.fail}, delay={args.delay}s)")
    print("In the app: Provider = Custom, base URL as above, "
          "models 'mock-coder' and 'mock-judge'.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
