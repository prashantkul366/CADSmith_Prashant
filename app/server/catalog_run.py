"""Serve a standard part from the catalogue instead of generating it.

When a request is unambiguously a standard part - "an M8x30 socket head cap
screw", "a 20 tooth spur gear module 2" - there is nothing for five agents to
work out.  The dimensions come from the standard, the geometry is exact, and
a language model can only introduce error.  So the catalogue answers directly.

Two things keep this honest.

*It is never a guess.*  ``router.select`` refuses anything ambiguous,
under-specified, or merely mentioning a standard part inside a custom one,
and it builds and verifies every candidate before returning it.  Whatever
reaches here is already known to be a sound solid.

*It is never disguised as pipeline output.*  The version is stamped
``source="catalog"`` and the UI badges it, because a part that no agent
produced must not be shown as evidence that the agents work.  It carries no
Judge verdict for the same reason - there was no Judge.

The part still runs through the same Executor the pipeline uses, so the
artifacts, the geometry JSON and the exports are identical to any other run;
only the route to the code differs.
"""

from __future__ import annotations

import json
import time

from app.catalog import router
from app.server import i18n
from app.catalog.router import Routed
from app.server.events import (
    PHASE_CATALOG, PHASE_VERSION, STATUS_INFO, STATUS_OK,
)
from app.server.instrument import InstrumentedExecutor, RunContext


def find(prompt: str) -> Routed | None:
    """The standard part this prompt asks for, already built and checked."""
    return router.select(prompt)


def serve(ctx: RunContext, routed: Routed, work_dir) -> dict:
    """Build the catalogue part into this job's artifacts and publish it.

    Returns a summary for the job record. Raises if the part will not execute
    here, which should be impossible - the router built it moments ago - but
    the caller falls back to the pipeline rather than trusting that.
    """
    part = routed.part
    ctx.source = "catalog"
    ctx.method = routed.source
    ctx.iteration = 0

    ctx.emit(
        PHASE_CATALOG, STATUS_OK,
        i18n.t("catalog.served", ctx.lang, title=part.title),
        part_id=part.id, title=part.title, standard=part.standard,
        backend=routed.source, parameters=part.parameters,
        verified=routed.report.summary(),
    )

    started = time.time()
    executor = InstrumentedExecutor(output_dir=str(work_dir))
    result = executor.execute(part.code, name="part_iter0")
    if not result.success:
        raise RuntimeError(
            f"the catalogue part would not build here: {result.error}")

    geometry = result.geometry_json or {}
    version_dir = ctx.version_dir()
    # A catalogue part has no Judge verdict, so validation.json records what
    # was actually checked - the kernel, and the router's own verification -
    # rather than an empty report that reads like a Judge passed it.
    try:
        (version_dir / "validation.json").write_text(json.dumps({
            "source": "catalog",
            "part_id": part.id,
            "standard": part.standard,
            "backend": routed.source,
            "all_passed": True,
            "checks": [
                {"metric": "kernel_valid", "passed": True,
                 "message": "OCCT reports a valid watertight solid."},
                {"metric": "single_solid", "passed": True,
                 "message": f"{routed.report.num_solids} solid."},
                {"metric": "llm_judge", "passed": None,
                 "message": "No Judge: this part was not generated."},
            ],
            "feedback_text": "",
        }, indent=2))
    except OSError:
        pass

    version = {
        "iteration": 0,
        "passed": True,
        "judge_passed": None,
        "judge_feedback": "",
        "feedback_text": "",
        "geometry": geometry,
        "has_render": (version_dir / "render.png").exists(),
        "source": "catalog",
        "method": routed.source,
        "instruction": "",
        "changes": [],
        "catalog": {
            "part_id": part.id,
            "title": part.title,
            "standard": part.standard,
            "backend": routed.source,
            "parameters": part.parameters,
        },
    }
    ctx.versions.append(version)
    ctx.emit(PHASE_VERSION, STATUS_OK, **version)
    ctx.emit(PHASE_CATALOG, STATUS_INFO,
             i18n.t("catalog.built", ctx.lang,
                    ms=f"{(time.time() - started) * 1000:.0f}"))
    return version
