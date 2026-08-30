"""Integration check of the HTTP surface, against the real CAD kernel.

Drives the server the way the browser does - start a job, follow the SSE
stream, download the artifacts - with only the Anthropic HTTP call faked.
CadQuery and VTK do real work throughout.

Run:  .venv/bin/python -m app.tests.test_server
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Both must be set before app.server.app is imported: the module builds its
# JobManager (and reads the key for its health check) at import time.
_TMP_RUNS = Path(tempfile.mkdtemp(prefix="cadsmith_runs_"))
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used-network-is-faked"

import app.server.app as server  # noqa: E402
from app.server.jobs import JobManager  # noqa: E402
from app.tests.fake_claude import FakeClaude, patched  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

server.manager = JobManager(_TMP_RUNS)

PROMPT = "A flat washer, 20mm outer diameter, 10.5mm bore, 2mm thick."

PLAN = {
    "description": "Flat washer",
    "components": ["annular disc"],
    "dimensions": {
        "overall_bbox": {"xlen": 20, "ylen": 20, "zlen": 2},
        "key_dimensions": {"outer_dia": 20, "bore": 10.5, "thickness": 2},
    },
    "constraints": {"num_holes": 1, "hole_diameter": 10.5},
    "acceptance_criteria": {"volume_error_threshold_pct": 5},
    "notes": "Concentric circles extruded.",
}

CODE = """import cadquery as cq

outer_dia = 20.0
bore = 10.5
thickness = 2.0

result = (
    cq.Workplane('XY')
    .circle(outer_dia / 2.0)
    .circle(bore / 2.0)
    .extrude(thickness)
)
"""


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    fake = FakeClaude(plan=PLAN, code=[CODE], verdicts=[(True, "All constraints met.")])

    with TestClient(server.app) as client, patched(fake):
        print("\nHealth")
        health = client.get("/api/health").json()
        check("cadquery available", health["checks"]["cadquery"]["ok"])
        check("can_generate is true with a key set", health["can_generate"])

        print("\nJob creation")
        bad = client.post("/api/jobs", json={"prompt": "  "})
        check("empty prompt is rejected", bad.status_code == 400,
              f"status {bad.status_code}")

        response = client.post("/api/jobs", json={
            "prompt": PROMPT,
            "options": {"max_iterations": 2, "use_vision": True},
        })
        check("job accepted", response.status_code == 201,
              f"status {response.status_code}")
        job_id = response.json()["job"]["id"]
        print(f"        job id: {job_id}")

        print("\nEvent stream (SSE)")
        events: list[dict] = []
        with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
            check("stream content type", "text/event-stream"
                  in stream.headers.get("content-type", ""))
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if payload:
                        events.append(payload)
                if line.startswith("event: end"):
                    break

        phases = [(e["phase"], e["status"]) for e in events]
        check("stream replayed from the start",
              phases and phases[0][0] == "job", str(phases[:1]))
        check("planner reported", ("plan", "ok") in phases)
        check("coder reported", ("code", "ok") in phases)
        check("kernel execution reported", ("execute", "ok") in phases)
        check("three-view render reported", ("render", "ok") in phases)
        check("judge verdict reported", ("judge", "ok") in phases)
        check("version bundle announced", ("version", "ok") in phases)
        check("stream terminated on completion", ("job", "ok") in phases)

        plan_event = next(e for e in events if e["phase"] == "plan"
                          and e["status"] == "ok")
        check("design plan carried on the event",
              plan_event["data"]["design_plan"]["description"] == "Flat washer")

        exec_event = next(e for e in events if e["phase"] == "execute"
                          and e["status"] == "ok")
        bbox = exec_event["data"]["geometry"]["bounding_box"]
        check("kernel measured a 20mm outer diameter",
              abs(bbox["xlen"] - 20.0) < 1e-6, f"xlen={bbox['xlen']}")
        check("kernel measured 2mm thickness",
              abs(bbox["zlen"] - 2.0) < 1e-6, f"zlen={bbox['zlen']}")

        print("\nJob state")
        state = client.get(f"/api/jobs/{job_id}").json()
        check("job converged", state["job"]["converged"])
        check("token usage recorded", state["job"]["tokens"].get("calls", 0) > 0,
              str(state["job"]["tokens"]))
        check("history replayable after the fact",
              len(state["events"]) == len(events),
              f"{len(state['events'])} vs {len(events)}")

        print("\nArtifacts")
        expected = {
            "model.step": b"ISO-10303",
            "code.py": b"import cadquery",
            "render.png": b"\x89PNG",
        }
        for name, magic in expected.items():
            got = client.get(f"/api/jobs/{job_id}/v/0/{name}")
            ok = got.status_code == 200 and got.content[:len(magic)] == magic
            check(f"v0/{name}", ok,
                  f"status {got.status_code}, {len(got.content)}B")

        # CadQuery writes binary STL: an 80-byte header, a uint32 triangle
        # count, then 50 bytes per triangle.  Check the file is internally
        # consistent rather than sniffing for the ASCII "solid" keyword.
        stl = client.get(f"/api/jobs/{job_id}/v/0/model.stl")
        body = stl.content
        triangles = (struct.unpack("<I", body[80:84])[0]
                     if len(body) >= 84 else 0)
        check("v0/model.stl is a well-formed binary STL",
              stl.status_code == 200 and triangles > 0
              and len(body) == 84 + 50 * triangles,
              f"{triangles} triangles, {len(body)}B")

        geometry = client.get(f"/api/jobs/{job_id}/v/0/geometry.json")
        check("geometry.json is valid and watertight",
              geometry.status_code == 200 and geometry.json()["is_valid"])

        print("\nSafety")
        missing = client.get(f"/api/jobs/{job_id}/v/9/model.stl")
        check("unknown version is 404", missing.status_code == 404)
        traversal = client.get(f"/api/jobs/{job_id}/v/0/../../meta.json")
        check("path traversal refused", traversal.status_code == 404,
              f"status {traversal.status_code}")
        check("unknown job is 404",
              client.get("/api/jobs/nope").status_code == 404)

    shutil.rmtree(_TMP_RUNS, ignore_errors=True)

    print(f"\n{'='*58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
