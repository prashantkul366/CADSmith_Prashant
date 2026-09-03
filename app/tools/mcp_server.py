"""Expose CADSmith to any agent that speaks MCP.

The app already generates, edits, measures and exports parts over HTTP.  This
is an adapter, not a second implementation: every tool here drives the running
server, so the kernel, the five agents, the standard-parts catalogue and the
kernel-measured specification checks behave exactly as they do in the browser.

What that buys is the thing the web app cannot do - putting verified CAD
inside somebody else's agent.  An engineer working in Claude Code can ask for
a bracket, have it measured against the plan that asked for it, and get a STEP
file back, without leaving the tool they are already in.

Run it against a server you have already started:

    .venv/bin/python -m app.tools.mcp_server

``CADSMITH_URL`` points it somewhere other than http://127.0.0.1:8000.

Registering it with Claude Code:

    claude mcp add cadsmith -- /path/to/.venv/bin/python -m app.tools.mcp_server
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.getenv("CADSMITH_URL", "http://127.0.0.1:8000").rstrip("/")

#: A run with vision judging and two refinement passes can take minutes on a
#: self-hosted model, and the caller is an agent that will wait.
POLL_TIMEOUT = float(os.getenv("CADSMITH_MCP_TIMEOUT", "900"))
POLL_EVERY = 2.0

server = MCPServer(
    name="cadsmith",
    title="CADSmith",
    instructions=(
        "Generates and edits real parametric CAD geometry. Parts are built by "
        "the OpenCASCADE kernel, measured against the plan that asked for "
        "them, and exported as STEP or STL.\n\n"
        "Read the `spec` field on any result before trusting it: it reports "
        "what the kernel measured, and `spec.ok == false` means the part does "
        "not match what was asked for, whatever the description says. "
        "`converged == false` means the same. Do not describe a part as "
        "correct when its measurements disagree.\n\n"
        "Standard hardware - ISO fasteners, bearings, gears, timing pulleys, "
        "o-rings, dowel pins - should go through find_standard_part, which is "
        "exact and needs no model call. Use generate_part for custom geometry."
    ),
)


# ---------------------------------------------------------------------------
# Talking to the app
# ---------------------------------------------------------------------------

class AppUnavailable(RuntimeError):
    """The CADSmith server is not answering."""


def _request(method: str, path: str, payload: Optional[dict] = None,
             timeout: float = 60.0) -> Any:
    try:
        response = httpx.request(method, BASE_URL + path, json=payload,
                                 timeout=timeout)
    except httpx.HTTPError as exc:
        raise AppUnavailable(
            f"No CADSmith server at {BASE_URL} ({exc}). Start one with "
            f"./app/run_app.sh, or set CADSMITH_URL."
        ) from exc
    if response.status_code >= 400:
        detail = response.text[:400]
        try:
            detail = response.json().get("detail", detail)
        except Exception:                              # noqa: BLE001
            pass
        raise RuntimeError(f"CADSmith refused the request: {detail}")
    return response.json() if response.content else {}


def _await_job(job_id: str) -> dict:
    """Block until a job stops running, then return it."""
    started = time.time()
    while time.time() - started < POLL_TIMEOUT:
        job = _request("GET", f"/api/jobs/{job_id}")["job"]
        if job["status"] in ("done", "error"):
            return job
        time.sleep(POLL_EVERY)
    raise RuntimeError(
        f"Job {job_id} was still running after {POLL_TIMEOUT:.0f}s. It may "
        f"still finish; check it with get_part."
    )


def _digest(job: dict) -> dict:
    """The part of a job an agent needs, without the transcript.

    Deliberately leads with whether the result can be trusted rather than
    with the geometry, because a caller that reads only the first field
    should still get the important part.
    """
    versions = job.get("versions") or []
    latest = versions[-1] if versions else {}
    geometry = latest.get("geometry") or {}
    spec = latest.get("spec") or None

    trusted = bool(job.get("converged")) and (spec is None or spec.get("ok"))
    summary = {
        "part_id": job.get("id"),
        "trustworthy": trusted,
        "converged": bool(job.get("converged")),
        "status": job.get("status"),
        "source": job.get("source"),
        "prompt": job.get("prompt"),
        "versions": len(versions),
    }
    if job.get("error"):
        summary["error"] = job["error"]

    if geometry:
        box = geometry.get("bounding_box") or {}
        summary["measured"] = {
            "volume_mm3": round(geometry.get("volume", 0.0), 3),
            "bbox_mm": [round(box.get(k, 0.0), 3)
                        for k in ("xlen", "ylen", "zlen")],
            "faces": geometry.get("num_faces"),
            "edges": geometry.get("num_edges"),
            "watertight": geometry.get("is_valid"),
        }
    if spec:
        summary["spec"] = {
            "ok": spec.get("ok"),
            "holes": (spec.get("measured") or {}).get("holes"),
            "checks": [
                {"what": c["label"], "wanted": c["expected"],
                 "measured": c["actual"], "passed": c["passed"],
                 "blocking": c["hard"]}
                for c in spec.get("checks", [])
            ],
        }
    if latest.get("judge_feedback"):
        summary["judge_said"] = latest["judge_feedback"][:600]
    if job.get("tokens"):
        summary["tokens"] = job["tokens"]
    summary["code"] = _code_of(job.get("id"), latest.get("iteration"))
    return summary


def _code_of(job_id: Optional[str], iteration: Optional[int]) -> str:
    if job_id is None or iteration is None:
        return ""
    try:
        response = httpx.get(
            f"{BASE_URL}/api/jobs/{job_id}/v/{iteration}/code.py", timeout=30)
        return response.text if response.status_code < 400 else ""
    except httpx.HTTPError:
        return ""


def _backend() -> dict:
    """A backend the app can actually run the agents on, with its models.

    An agent calling this server should not have to know how the app is
    configured, so this is discovered rather than assumed. Some providers -
    a self-hosted endpoint, say - carry no default model names, in which case
    whatever the endpoint reports serving is used.
    """
    try:
        # ?models=true asks each ready provider what it actually
        # serves, rather than trusting a default name that a
        # self-hosted endpoint will not have.
        providers = _request("GET", "/api/providers?models=true",
                             timeout=30)
    except Exception:                                  # noqa: BLE001
        return {}
    rows = providers.get("providers", providers) if isinstance(
        providers, dict) else providers
    for row in rows or []:
        if not row.get("ready"):
            continue
        offered = row.get("models") or []
        options = {"provider": row.get("id")}
        generation = row.get("default_generation_model") or (
            offered[0] if offered else "")
        judge = row.get("default_judge_model") or generation
        if generation:
            options["generation_model"] = generation
            options["judge_model"] = judge
        return options
    return {}


def _reportable(fn):
    """Return failures as readable JSON instead of an opaque tool error.

    The SDK reports an uncaught exception to the client as "Error executing
    tool <name>", which tells the calling agent nothing it can act on. A
    missing backend, an unknown part id and a kernel failure need different
    responses, so each has to survive the trip.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            return _json({"error": str(exc),
                          "error_type": type(exc).__name__})

    return wrapper


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@server.tool(
    title="Generate a part",
    description=(
        "Build a part from a description and measure it against the plan. "
        "Returns the CadQuery source, the kernel's measurements, and whether "
        "the result can be trusted. Describe shape, sizes and features; state "
        "dimensions in millimetres."
    ),
)
@_reportable
def generate_part(
    description: str,
    max_iterations: int = 2,
    use_catalog: bool = True,
    use_vision: bool = True,
) -> str:
    """Generate a part and return its measurements.

    Args:
        description: The part to build, with dimensions.
        max_iterations: Refinement passes after the first attempt.
        use_catalog: Answer a standard-hardware request from the verified
            catalogue instead of generating it. Exact, and no model call.
        use_vision: Show the Judge a three-view render as well as the metrics.
    """
    options: dict = {
        "max_iterations": max(0, min(int(max_iterations), 5)),
        "use_catalog": bool(use_catalog),
        "use_vision": bool(use_vision),
    }
    backend = _backend()
    if not backend:
        raise RuntimeError(
            "No model backend is configured on the CADSmith server, so the "
            "five agents cannot run. Standard hardware still works through "
            "find_standard_part. To generate custom geometry, configure a "
            "provider in the app.")
    options.update(backend)
    job = _request("POST", "/api/jobs",
                   {"prompt": description, "options": options})["job"]
    return _json(_digest(_await_job(job["id"])))


@server.tool(
    title="Edit a part",
    description=(
        "Change an existing part in natural language. A request naming a "
        "dimension the script declares - \"make it 20mm thick\" - is applied "
        "by rewriting that value and rebuilding, with no model call. Anything "
        "structural goes to the Refiner agent. The reply says which happened."
    ),
)
@_reportable
def edit_part(part_id: str, instruction: str) -> str:
    """Apply one change to a part built earlier.

    Args:
        part_id: The id returned by generate_part.
        instruction: The change, e.g. "make it 20mm thick".
    """
    _request("POST", f"/api/jobs/{part_id}/edit", {"instruction": instruction})
    job = _await_job(part_id)
    result = _digest(job)
    versions = job.get("versions") or []
    if versions:
        result["edit_method"] = versions[-1].get("method") or "unchanged"
        result["changes"] = versions[-1].get("changes") or []
    return _json(result)


@server.tool(
    title="Measure a part",
    description=(
        "Read the kernel's measurements of a part already built: volume, "
        "bounding box, face and edge counts, watertightness, and the "
        "diameter of every hole found in the solid."
    ),
)
@_reportable
def measure_part(part_id: str) -> str:
    """Measure a part without changing it.

    Args:
        part_id: The id returned by generate_part.
    """
    return _json(_digest(_request("GET", f"/api/jobs/{part_id}")["job"]))


@server.tool(
    title="Check a part against a specification",
    description=(
        "Measure a part and test it against dimensions you state, rather than "
        "against the plan it was generated from. Every claim is settled by "
        "the kernel. Use this to verify a part meets a requirement before "
        "relying on it."
    ),
)
@_reportable
def check_spec(
    part_id: str,
    num_holes: Optional[int] = None,
    hole_diameter_mm: Optional[float] = None,
    bbox_mm: Optional[list[float]] = None,
) -> str:
    """Test a built part against dimensions you supply.

    Args:
        part_id: The id returned by generate_part.
        num_holes: How many holes the part should have.
        hole_diameter_mm: A hole diameter that must be present.
        bbox_mm: Overall size as [x, y, z]; compared as a set of extents, so
            orientation does not matter.
    """
    from app.server import spec as spec_mod

    step = _download(part_id, "model.step")
    plan: dict = {"dimensions": {}, "constraints": {}}
    if bbox_mm and len(bbox_mm) == 3:
        plan["dimensions"]["overall_bbox"] = dict(
            zip(("xlen", "ylen", "zlen"), (float(v) for v in bbox_mm)))
    if num_holes is not None:
        plan["constraints"]["num_holes"] = int(num_holes)
    if hole_diameter_mm is not None:
        plan["constraints"]["hole_diameter"] = float(hole_diameter_mm)

    report = spec_mod.check(plan, step)
    return _json({
        "part_id": part_id,
        "meets_specification": report.ok,
        "summary": report.summary(),
        "measured": report.measured,
        "checks": [
            {"what": c.label, "wanted": c.expected, "measured": c.actual,
             "passed": c.passed, "blocking": c.hard}
            for c in report.checks
        ],
    })


@server.tool(
    title="Export a part",
    description=(
        "Write a built part to a file. STEP is the exchange format every CAD "
        "system reads; STL is for printing and meshing; py is the CadQuery "
        "source, which stays parametric and editable."
    ),
)
@_reportable
def export_part(part_id: str, path: str, fmt: str = "step") -> str:
    """Save a part to disk.

    Args:
        part_id: The id returned by generate_part.
        path: Where to write the file.
        fmt: One of step, stl or py.
    """
    names = {"step": "model.step", "stl": "model.stl", "py": "code.py"}
    key = fmt.lower().lstrip(".")
    if key not in names:
        raise ValueError(f"fmt must be one of {', '.join(sorted(names))}.")

    from pathlib import Path

    data = _download(part_id, names[key], as_bytes=True)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _json({"part_id": part_id, "format": key,
                  "path": str(target), "bytes": len(data)})


@server.tool(
    title="Find a standard part",
    description=(
        "Build a piece of standard hardware from published dimensions - ISO "
        "fasteners, deep groove bearings, involute gears, GT2 and HTD timing "
        "pulleys, o-rings, dowel pins. Exact, immediate, and no model is "
        "involved. Returns nothing if the request is not an unambiguous "
        "standard designation, which means it needs generate_part instead."
    ),
)
@_reportable
def find_standard_part(designation: str) -> str:
    """Look up standard hardware by designation.

    Args:
        designation: e.g. "M8 hex nut ISO 4032", "608 bearing",
            "spur gear 24 teeth module 2", "M10 washer".
    """
    from app.catalog import router

    routed = router.select(designation)
    if routed is None:
        return _json({
            "found": False,
            "reason": ("Not an unambiguous standard part. Give a full "
                       "designation, or use generate_part for custom "
                       "geometry."),
        })
    report = routed.report
    return _json({
        "found": True,
        "title": routed.part.title,
        "standard": routed.part.standard,
        "source": routed.source,
        "parameters": routed.part.parameters,
        "measured": {
            "volume_mm3": round(report.volume, 3),
            "bbox_mm": [round(v, 3) for v in report.bbox],
            "faces": report.num_faces,
            "watertight": report.is_valid,
        },
        "verified": report.ok,
        "code": routed.part.code,
    })


@server.tool(
    title="List parts",
    description="The parts built in this CADSmith instance, newest first.",
)
@_reportable
def list_parts(limit: int = 20) -> str:
    """List recent parts.

    Args:
        limit: How many to return.
    """
    jobs = _request("GET", "/api/jobs")
    rows = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    return _json([
        {"part_id": j.get("id"), "prompt": j.get("prompt"),
         "status": j.get("status"), "converged": j.get("converged"),
         "source": j.get("source")}
        for j in list(rows)[:max(1, min(int(limit), 100))]
    ])


def _download(part_id: str, name: str, as_bytes: bool = False):
    """Fetch an artifact from the newest version of a part."""
    job = _request("GET", f"/api/jobs/{part_id}")["job"]
    versions = job.get("versions") or []
    if not versions:
        raise RuntimeError(f"Part {part_id} produced no geometry.")
    iteration = versions[-1]["iteration"]
    url = f"{BASE_URL}/api/jobs/{part_id}/v/{iteration}/{name}"
    try:
        response = httpx.get(url, timeout=60)
    except httpx.HTTPError as exc:
        raise AppUnavailable(f"Could not fetch {name}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Part {part_id} has no {name}.")
    if as_bytes:
        return response.content

    # spec.check needs a path on disk, so land the bytes in a temp file.
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="cadsmith_mcp_")) / name
    tmp.write_bytes(response.content)
    return tmp


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
