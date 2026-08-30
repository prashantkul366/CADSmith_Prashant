"""HTTP surface for the CADSmith web application.

Serves the single-page frontend, starts pipeline jobs, streams their progress
as Server-Sent Events, and hands back the artifacts the CAD kernel produced.

The event stream is poll-based over the job's append-only log rather than a
push queue: events are low-frequency (a handful per second at most), and
replaying from a sequence number makes reconnects, late arrivals and recorded
runs all the same code path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .drawing import ensure_sheet
from .jobs import JobManager, JobOptions, STATUS_DONE, STATUS_ERROR

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent
WEB_DIR = APP_ROOT / "web"
RUNS_DIR = APP_ROOT / "runs"
DATA_DIR = PROJECT_ROOT / "data" / "dataset_v2"

MEDIA_TYPES = {
    ".stl": "model/stl",
    ".step": "application/step",
    ".py": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}

ALLOWED_ARTIFACTS = {
    "model.stl",
    "model.step",
    "code.py",
    "render.png",
    "geometry.json",
    "validation.json",
    "drawing.svg",
}

# Curated starting prompts.  The tiered ones are the exact benchmark entries
# from data/dataset_v2, so a demo can be checked against a reference part.
EXAMPLE_IDS = ["T1_012", "T2_001", "T2_009", "T3_001", "T3_007"]

FALLBACK_EXAMPLES = [
    {
        "id": "demo_bracket",
        "tier": "demo",
        "prompt": (
            "A mounting bracket with a 100mm x 60mm base plate 10mm thick, two "
            "vertical support walls 45mm tall at each end, and four 8mm "
            "mounting holes in a rectangular pattern on the base."
        ),
    },
    {
        "id": "demo_flange",
        "tier": "demo",
        "prompt": (
            "A pipe flange with 100mm outer diameter, 40mm central bore, 12mm "
            "thick, with six 10mm bolt holes evenly spaced on a 78mm bolt circle."
        ),
    },
]

app = FastAPI(title="CADSmith", docs_url="/api/docs", redoc_url=None)
manager = JobManager(RUNS_DIR)

_render_capability: Optional[dict] = None


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------


def _probe_offscreen_render() -> dict:
    """Check that VTK can actually rasterise without a display.

    Import success is not enough: the pip wheel imports fine on a headless
    box and then fails at render time when no GL backend (EGL/OSMesa) exists.
    The result is cached because the probe costs about a second.
    """
    global _render_capability
    if _render_capability is not None:
        return _render_capability

    try:
        import vtk
    except Exception as exc:
        _render_capability = {"ok": False, "detail": f"vtk not importable: {exc}"}
        return _render_capability

    try:
        source = vtk.vtkCubeSource()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        renderer = vtk.vtkRenderer()
        renderer.AddActor(actor)
        window = vtk.vtkRenderWindow()
        window.SetOffScreenRendering(1)
        window.AddRenderer(renderer)
        window.SetSize(64, 64)
        window.Render()
        grabber = vtk.vtkWindowToImageFilter()
        grabber.SetInput(window)
        grabber.Update()
        dims = grabber.GetOutput().GetDimensions()
        window.Finalize()
        ok = dims[0] > 0 and dims[1] > 0
        _render_capability = {
            "ok": ok,
            "detail": "offscreen rendering available" if ok else
                      "render window produced no pixels",
        }
    except Exception as exc:
        _render_capability = {
            "ok": False,
            "detail": (
                f"{type(exc).__name__}: {exc}. On headless Linux install a "
                "software GL backend (apt-get install libosmesa6)."
            ),
        }
    return _render_capability


def _health() -> dict:
    import os

    checks: dict[str, Any] = {}

    try:
        import cadquery

        checks["cadquery"] = {"ok": True, "detail": f"version {cadquery.__version__}"}
    except Exception as exc:
        checks["cadquery"] = {"ok": False, "detail": str(exc)}

    checks["vision_render"] = _probe_offscreen_render()

    key = os.getenv("ANTHROPIC_API_KEY", "")
    checks["api_key"] = {
        "ok": bool(key),
        "detail": "ANTHROPIC_API_KEY is set" if key else
                  "ANTHROPIC_API_KEY is not set - live generation is unavailable "
                  "(recorded runs still replay).",
    }

    try:
        import trimesh  # noqa: F401
        import scipy  # noqa: F401

        checks["metrics"] = {"ok": True, "detail": "trimesh and scipy available"}
    except Exception as exc:
        checks["metrics"] = {"ok": False, "detail": str(exc)}

    return {
        "ok": all(c["ok"] for c in checks.values()),
        "can_generate": checks["cadquery"]["ok"] and checks["api_key"]["ok"],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse(_health())


@app.get("/api/examples")
def examples() -> JSONResponse:
    """Starting prompts, preferring real benchmark entries when present."""
    found: dict[str, dict] = {}
    for filename in ("t1_primitives.jsonl", "t2_engineering_parts.jsonl",
                     "t3_complex_parts.jsonl"):
        path = DATA_DIR / filename
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("id") in EXAMPLE_IDS:
                found[entry["id"]] = {
                    "id": entry["id"],
                    "tier": entry.get("tier", ""),
                    "prompt": entry.get("prompt", ""),
                }

    ordered = [found[i] for i in EXAMPLE_IDS if i in found]
    return JSONResponse({"examples": ordered + FALLBACK_EXAMPLES})


@app.post("/api/jobs")
async def create_job(request: Request) -> JSONResponse:
    body = await _json_body(request)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="A prompt is required.")
    if len(prompt) > 4000:
        raise HTTPException(status_code=400, detail="Prompt is too long.")

    status = _health()
    if not status["checks"]["cadquery"]["ok"]:
        raise HTTPException(
            status_code=503,
            detail="CadQuery is not available in this environment: "
                   + status["checks"]["cadquery"]["detail"],
        )
    if not status["checks"]["api_key"]["ok"]:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not set, so the agents cannot run.",
        )

    options = JobOptions.from_dict(body.get("options"))
    if options.use_vision and not status["checks"]["vision_render"]["ok"]:
        options.use_vision = False  # degrade rather than fail mid-run

    job = manager.create(prompt, options)
    return JSONResponse({"job": job.summary()}, status_code=201)


@app.post("/api/jobs/{job_id}/edit")
async def edit_job(job_id: str, request: Request) -> JSONResponse:
    """Apply a natural-language change to the job's latest version."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if not job.versions:
        raise HTTPException(
            status_code=409, detail="That run produced nothing to edit.")
    if job.status in ("queued", "running"):
        raise HTTPException(
            status_code=409, detail="That run is still working; wait for it to finish.")

    body = await _json_body(request)
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="An instruction is required.")
    if len(instruction) > 1000:
        raise HTTPException(status_code=400, detail="Instruction is too long.")

    status = _health()
    if not status["checks"]["cadquery"]["ok"]:
        raise HTTPException(
            status_code=503,
            detail="CadQuery is not available, so nothing can be rebuilt.")

    base_version = body.get("version")
    if base_version is not None:
        try:
            base_version = int(base_version)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid version.")
        if not any(v.get("iteration") == base_version for v in job.versions):
            raise HTTPException(status_code=404, detail="No such version.")

    manager.submit_edit(job, instruction, base_version=base_version)
    return JSONResponse({"job": job.summary()}, status_code=202)


@app.post("/api/jobs/{job_id}/replay")
async def replay_job(job_id: str, request: Request) -> JSONResponse:
    """Replay a recorded run: its own events, against its own artifacts."""
    source = manager.get(job_id)
    if source is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if not manager.can_replay(source):
        raise HTTPException(
            status_code=409,
            detail="That run has no recorded events or geometry to replay.")

    body = await _json_body(request)
    try:
        speed = float(body.get("speed", 6.0))
    except (TypeError, ValueError):
        speed = 6.0
    speed = max(0.5, min(40.0, speed))

    job = manager.create_replay(source, speed=speed)
    return JSONResponse({"job": job.summary()}, status_code=201)


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    return JSONResponse({"jobs": manager.list()})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    sink = manager.sink(job_id)
    return JSONResponse({
        "job": job.summary(),
        "events": [e.to_dict() for e in (sink.all() if sink else [])],
    })


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request, from_seq: int = 0):
    """Stream a job's events, starting at ``from_seq``.

    Replays history first, then follows.  A client that reloads mid-run passes
    0 and gets the whole story; ``Last-Event-ID`` is honoured for reconnects.
    """
    sink = manager.sink(job_id)
    if sink is None:
        raise HTTPException(status_code=404, detail="No such job.")

    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        from_seq = int(last_event_id) + 1

    async def generate():
        cursor = max(0, from_seq)
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return

            pending = sink.since(cursor)
            for event in pending:
                cursor = event.seq + 1
                yield event.to_sse()

            job = manager.get(job_id)
            finished = job is not None and job.status in (STATUS_DONE, STATUS_ERROR)
            if finished and not sink.since(cursor):
                yield "event: end\ndata: {}\n\n"
                return

            if pending:
                idle = 0.0
            else:
                idle += 0.2
                if idle >= 15.0:  # keep proxies from closing an idle stream
                    idle = 0.0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/jobs/{job_id}/v/{version}/{artifact}")
def get_artifact(job_id: str, version: int, artifact: str):
    if artifact not in ALLOWED_ARTIFACTS:
        raise HTTPException(status_code=404, detail="Unknown artifact.")

    # The drawing sheet is derived from the STEP solid, so it is built on
    # first request and cached beside the other artifacts.
    if artifact == "drawing.svg":
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        version_dir = job.directory / f"v{int(version)}"
        if not version_dir.is_dir():
            raise HTTPException(status_code=404, detail="No such version.")
        try:
            ensure_sheet(version_dir, job.prompt, job.id, int(version))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not build the drawing: {exc}") from exc

    path = manager.artifact_path(job_id, version, artifact)
    if path is None:
        raise HTTPException(status_code=404, detail="Artifact not available.")

    filename = f"{job_id}_v{version}_{artifact}"
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        filename=filename if artifact in ("model.stl", "model.step", "code.py") else None,
    )


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected a JSON body.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return body


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="Frontend is not built.")
    return HTMLResponse(page.read_text())


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    restored = manager.load_from_disk()
    status = _health()
    print(f"CADSmith app ready - {restored} past run(s) restored")
    for name, check in status["checks"].items():
        print(f"  [{'ok ' if check['ok'] else 'MISS'}] {name}: {check['detail']}")
