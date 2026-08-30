"""Makes the stock CADSmith pipeline observable - without modifying it.

Everything under ``autofab/`` is left byte-for-byte as published, so
``run.py`` and both benchmark scripts stay reproducible.  Observability is
achieved from the outside, in three ways:

1. ``Pipeline`` builds its executor and validator as plain instance
   attributes, so :class:`InstrumentedPipeline` swaps in subclasses after
   ``super().__init__()`` runs.
2. ``pipeline.py`` and ``validator.py`` both reach the agents through the
   *module object* (``from . import agents`` then ``agents.plan(...)``), so
   wrapping the module's function attributes intercepts every LLM call.
3. The current job is carried in a :class:`~contextvars.ContextVar`, so the
   wrappers are installed once, globally, yet stay correctly scoped when
   several jobs run on different worker threads.  With no context set the
   wrappers are pass-throughs, so importing this module never changes
   behaviour for anything outside the web app.

The iteration number is recovered from the ``name`` argument the pipeline
passes to the executor (``f"{name}_iter{iteration}"``) rather than by
scraping log strings, which keeps the coupling to a documented call
signature instead of to human-readable text.
"""

from __future__ import annotations

import contextvars
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from autofab.executor import Executor, ExecutionResult
from autofab.pipeline import Pipeline
from autofab.validator import Validator, ValidationReport

from .events import (
    EventSink,
    PHASE_CODE,
    PHASE_ERROR_FIX,
    PHASE_EXECUTE,
    PHASE_JUDGE,
    PHASE_LOG,
    PHASE_PLAN,
    PHASE_REFINE,
    PHASE_RENDER,
    PHASE_VERSION,
    STATUS_FAILED,
    STATUS_INFO,
    STATUS_OK,
    STATUS_STARTED,
)

_ITER_RE = re.compile(r"_iter(\d+)$")


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    """Per-job state shared by the instrumented components.

    One of these is active per worker thread, published through a ContextVar.
    """

    sink: EventSink
    job_dir: Path
    part_name: str
    iteration: int = 0
    #: Set when the Judge call itself raised.  ``autofab.validator`` records
    #: that case as a *passing* check (validator.py:174), which would let an
    #: API error silently converge a part.  The app surfaces it instead.
    judge_error: Optional[str] = None
    versions: list[dict] = field(default_factory=list)
    #: Provenance stamped onto the next published version.
    source: str = "pipeline"
    method: str = ""
    instruction: str = ""
    changes: list[dict] = field(default_factory=list)

    def version_dir(self, iteration: Optional[int] = None) -> Path:
        d = self.job_dir / f"v{self.iteration if iteration is None else iteration}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def emit(self, phase: str, status: str, message: str = "", **data: Any):
        return self.sink.emit(phase, status, message, **data)


_current: contextvars.ContextVar[Optional[RunContext]] = contextvars.ContextVar(
    "cadsmith_run_context", default=None
)


def set_context(ctx: Optional[RunContext]) -> None:
    """Bind a run context to the calling thread."""
    _current.set(ctx)


def get_context() -> Optional[RunContext]:
    return _current.get()


def _emit(phase: str, status: str, message: str = "", **data: Any) -> None:
    ctx = _current.get()
    if ctx is not None:
        ctx.emit(phase, status, message, **data)


def _token_usage() -> dict:
    """Live token counters from the agents module (best effort)."""
    try:
        from autofab import agents

        return agents.get_token_usage()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Agent hooks
# ---------------------------------------------------------------------------

_HOOKS_INSTALLED = False


def install_agent_hooks() -> None:
    """Wrap the agent entry points so every LLM call emits events.

    Idempotent, and a no-op for callers without an active run context.
    """
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return

    from autofab import agents

    def wrap(fn: Callable, phase: str, describe_in: Callable[..., dict],
             describe_out: Callable[[Any], dict]) -> Callable:
        def inner(*args, **kwargs):
            ctx = _current.get()
            if ctx is None:  # untouched behaviour outside the web app
                return fn(*args, **kwargs)
            try:
                payload = describe_in(*args, **kwargs)
            except Exception:
                payload = {}
            ctx.emit(phase, STATUS_STARTED, **payload)
            started = time.time()
            try:
                out = fn(*args, **kwargs)
            except Exception as exc:
                ctx.emit(
                    phase,
                    STATUS_FAILED,
                    f"{type(exc).__name__}: {exc}",
                    ms=(time.time() - started) * 1000,
                )
                raise
            try:
                out_payload = describe_out(out)
            except Exception:
                out_payload = {}
            ctx.emit(
                phase,
                STATUS_OK,
                ms=(time.time() - started) * 1000,
                tokens=_token_usage(),
                **out_payload,
            )
            return out

        inner.__name__ = getattr(fn, "__name__", "wrapped")
        inner.__doc__ = getattr(fn, "__doc__", None)
        inner._cadsmith_wrapped = True  # type: ignore[attr-defined]
        return inner

    agents.plan = wrap(
        agents.plan,
        PHASE_PLAN,
        lambda prompt, *a, **k: {"prompt": prompt},
        lambda plan: {"design_plan": plan},
    )
    agents.generate_code = wrap(
        agents.generate_code,
        PHASE_CODE,
        lambda design_plan, prompt, *a, **k: {},
        lambda code: {"code": code, "lines": len(code.splitlines())},
    )
    agents.fix_error = wrap(
        agents.fix_error,
        PHASE_ERROR_FIX,
        lambda code, error, design_plan, *a, **k: {"error": error[-1200:]},
        lambda code: {"code": code, "lines": len(code.splitlines())},
    )
    agents.refine_geometry = wrap(
        agents.refine_geometry,
        PHASE_REFINE,
        lambda code, feedback, *a, **k: {"feedback": feedback},
        lambda code: {"code": code, "lines": len(code.splitlines())},
    )

    # The Judge is wrapped separately: its failure mode needs recording on the
    # context so InstrumentedValidator can override the silent pass.
    _orig_evaluate = agents.evaluate_geometry

    def evaluate_geometry(*args, **kwargs):
        ctx = _current.get()
        if ctx is None:
            return _orig_evaluate(*args, **kwargs)
        use_vision = bool(kwargs.get("stl_path") or (len(args) > 3 and args[3]))
        ctx.judge_error = None
        ctx.emit(PHASE_JUDGE, STATUS_STARTED, vision=use_vision)
        started = time.time()
        try:
            out = _orig_evaluate(*args, **kwargs)
        except Exception as exc:
            ctx.judge_error = f"{type(exc).__name__}: {exc}"
            ctx.emit(
                PHASE_JUDGE,
                STATUS_FAILED,
                ctx.judge_error,
                ms=(time.time() - started) * 1000,
            )
            raise
        ctx.emit(
            PHASE_JUDGE,
            STATUS_OK,
            out.get("feedback", ""),
            passed=bool(out.get("passed", False)),
            vision=use_vision,
            ms=(time.time() - started) * 1000,
            tokens=_token_usage(),
        )
        return out

    evaluate_geometry._cadsmith_wrapped = True  # type: ignore[attr-defined]
    agents.evaluate_geometry = evaluate_geometry

    _HOOKS_INSTALLED = True


# ---------------------------------------------------------------------------
# Instrumented components
# ---------------------------------------------------------------------------


class InstrumentedExecutor(Executor):
    """Executor that reports what the CAD kernel did and files the artifacts."""

    def execute(self, cadquery_code: str, name: str = "part") -> ExecutionResult:
        ctx = _current.get()
        if ctx is None:
            return super().execute(cadquery_code, name=name)

        match = _ITER_RE.search(name)
        if match:
            ctx.iteration = int(match.group(1))

        ctx.emit(
            PHASE_EXECUTE,
            STATUS_STARTED,
            iteration=ctx.iteration,
            lines=len(cadquery_code.splitlines()),
        )
        result = super().execute(cadquery_code, name=name)

        if result.success:
            ctx.emit(
                PHASE_EXECUTE,
                STATUS_OK,
                iteration=ctx.iteration,
                ms=result.time_ms,
                geometry=result.geometry_json,
            )
            self._file_artifacts(ctx, name, cadquery_code, result)
        else:
            ctx.emit(
                PHASE_EXECUTE,
                STATUS_FAILED,
                (result.error or "")[-1500:],
                iteration=ctx.iteration,
                error_type=result.error_type,
                ms=result.time_ms,
            )
        return result

    def _file_artifacts(
        self, ctx: RunContext, name: str, code: str, result: ExecutionResult
    ) -> None:
        """Copy this iteration's outputs into a stable, servable bundle."""
        vdir = ctx.version_dir()
        try:
            (vdir / "code.py").write_text(code)
            if result.stl_path and Path(result.stl_path).exists():
                shutil.copy2(result.stl_path, vdir / "model.stl")
            if result.step_path and Path(result.step_path).exists():
                shutil.copy2(result.step_path, vdir / "model.step")
            (vdir / "geometry.json").write_text(
                json.dumps(result.geometry_json, indent=2)
            )
        except OSError as exc:
            ctx.emit(PHASE_EXECUTE, STATUS_INFO, f"Could not file artifacts: {exc}")


class InstrumentedValidator(Validator):
    """Validator that publishes the Judge's verdict and the render it saw.

    Also corrects one behaviour for app use: ``autofab.validator`` records a
    *failed* Judge API call as a passing check so it cannot block the research
    benchmark.  In an interactive app that would silently converge a part on a
    rate limit, so the check is flipped back to a failure here.  The core class
    is untouched; only this subclass behaves differently.
    """

    def validate(self, geometry: dict, **kwargs) -> ValidationReport:
        ctx = _current.get()
        if ctx is None:
            return super().validate(geometry, **kwargs)

        render_path = kwargs.get("render_save_path") or ""
        report = super().validate(geometry, **kwargs)

        if render_path and Path(render_path).exists():
            ctx.emit(PHASE_RENDER, STATUS_OK, iteration=ctx.iteration)
            try:
                shutil.copy2(render_path, ctx.version_dir() / "render.png")
            except OSError:
                pass
        elif render_path:
            ctx.emit(
                PHASE_RENDER,
                STATUS_FAILED,
                "Three-view render unavailable - Judge ran without vision.",
                iteration=ctx.iteration,
            )

        if ctx.judge_error:
            self._demote_silent_pass(report, ctx.judge_error)

        self._publish_version(ctx, report, geometry)
        return report

    @staticmethod
    def _demote_silent_pass(report: ValidationReport, error: str) -> None:
        for check in report.checks:
            if check.metric == "llm_judge" and check.passed and "failed" in check.message:
                check.passed = False
                check.message = (
                    f"Judge call failed ({error}). Treated as NOT validated."
                )
        report.all_passed = all(c.passed for c in report.checks)
        if not report.all_passed and "Judge call failed" not in report.feedback_text:
            report.feedback_text = (
                f"Validation could not be completed: {error}\n"
                + report.feedback_text
            )

    @staticmethod
    def _publish_version(
        ctx: RunContext, report: ValidationReport, geometry: dict
    ) -> None:
        """Write validation.json and announce a complete artifact bundle."""
        vdir = ctx.version_dir()
        judge = next((c for c in report.checks if c.metric == "llm_judge"), None)
        try:
            (vdir / "validation.json").write_text(
                json.dumps(report.to_dict(), indent=2)
            )
        except OSError:
            pass

        version = {
            "iteration": ctx.iteration,
            "passed": report.all_passed,
            "judge_passed": bool(judge.passed) if judge else None,
            "judge_feedback": judge.message if judge else "",
            "feedback_text": report.feedback_text,
            "geometry": geometry,
            "has_render": (vdir / "render.png").exists(),
            "source": ctx.source,
            "method": ctx.method,
            "instruction": ctx.instruction,
            "changes": list(ctx.changes),
        }
        ctx.versions.append(version)
        ctx.emit(PHASE_VERSION, STATUS_OK, **version)


class InstrumentedPipeline(Pipeline):
    """Stock :class:`~autofab.pipeline.Pipeline` with observable components.

    Only the two collaborator objects are replaced; ``run()`` itself is the
    unmodified published implementation, so the agent sequence, retry limits
    and convergence rule are exactly those the benchmark measures.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor = InstrumentedExecutor(
            output_dir=str(self.output_dir), timeout_seconds=self.executor.timeout
        )
        self.validator = InstrumentedValidator(
            volume_error_threshold=self.validator.volume_error_threshold,
            bbox_iou_threshold=self.validator.bbox_iou_threshold,
            validity_required=self.validator.validity_required,
        )

    def log(self, msg: str) -> None:
        super().log(msg)
        text = msg.strip()
        if text:
            _emit(PHASE_LOG, STATUS_INFO, text)
