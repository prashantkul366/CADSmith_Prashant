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

from app.catalog import grounding
from autofab.executor import Executor, ExecutionResult
from autofab.pipeline import Pipeline
from autofab.validator import (
    ValidationCheck,
    ValidationReport,
    Validator,
)

from . import budget as budget_mod
from . import spec
from .providers import LLMConfig, build_client

from .events import (
    EventSink,
    PHASE_CODE,
    PHASE_ERROR_FIX,
    PHASE_EXECUTE,
    PHASE_GROUND,
    PHASE_SPEC,
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


class PipelineMessage(RuntimeError):
    """An error whose text was written for the person reading it.

    Ordinary failures are reported as "TypeError: ..." so the class is
    visible, which is right when it came from the kernel or the network.
    These did not: prefixing one with RuntimeError buries a sentence someone
    can act on under a word that means nothing to them.
    """


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
    #: Which model backend this job runs against. ``None`` means the stock
    #: Anthropic client, exactly as the published pipeline uses.
    llm: Optional[LLMConfig] = None
    #: The last raw text a model returned, kept so a JSON parse failure can
    #: show what actually came back instead of a bare JSONDecodeError.
    last_reply: str = ""
    #: Whether the Planner is given standard dimensions for whatever the
    #: request names. Off reproduces the published behaviour exactly, which
    #: is what makes it an ablation rather than a setting.
    ground_dimensions: bool = True
    #: The plan this run is building to, kept so the built solid can be
    #: measured against the claims that were made about it.
    design_plan: Optional[dict] = None
    #: The most recent kernel-measured specification result.
    spec: Any = None
    #: What this run may spend. Checked before every model call, so a loop
    #: that is not converging stops costing money instead of continuing.
    budget: Optional[budget_mod.Budget] = None
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
                    str(exc) if isinstance(exc, PipelineMessage)
                    else f"{type(exc).__name__}: {exc}",
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

    # The Planner is the only agent with no retrieval of its own, and it is
    # told to "estimate reasonable engineering dimensions" when the request
    # does not state them - which is where a NEMA 23 acquires a 22mm pilot
    # bore. Give it the published numbers for whatever the request names.
    #
    # Gated on the run context, like everything else here: outside the web
    # app this is a pass-through, so run.py and the benchmark scripts see the
    # published behaviour untouched.
    # Every agent reaches the model through _call_claude, so this is the one
    # place the raw reply can be captured - needed to explain a parse failure
    # in terms of what came back rather than where the parser stopped.
    _orig_call = agents._call_claude

    def _call_claude(*args, **kwargs):
        ctx = _current.get()
        if ctx is not None and ctx.budget is not None:
            # Checked before the call, not after: a request already in flight
            # is already billable, so the only useful place to stop is here.
            from autofab import agents as _agents
            ctx.budget.check(_agents.get_token_usage())
        reply = _orig_call(*args, **kwargs)
        if ctx is not None and isinstance(reply, str):
            ctx.last_reply = reply
        return reply

    agents._call_claude = _call_claude

    _plan_inner = agents.plan

    def _refuse_empty_plan(plan) -> None:
        """Stop when the plan describes no part at all.

        A smaller model answers "write me a poem" by filling the schema in
        rather than declining: components empty, every bounding-box extent
        zero, sometimes a note saying outright that this is not a part. The
        pipeline would then spend four more agents and several minutes
        producing a featureless block. Say so now instead.
        """
        if not isinstance(plan, dict):
            return
        components = plan.get("components")
        bbox = ((plan.get("dimensions") or {}).get("overall_bbox") or {})
        extents = [bbox.get(k) for k in ("xlen", "ylen", "zlen")]
        sized = any(isinstance(v, (int, float)) and v > 0 for v in extents)
        if sized or components:
            return
        note = " ".join(str(plan.get("notes") or "").split())[:200]
        raise PipelineMessage(
            "The Planner did not find a part to make in this request: it "
            "returned no components and no overall size. Describe a part - "
            "its shape, size and features."
            + (f' The Planner noted: "{note}"' if note else "")
        )

    def grounded_plan(prompt, *args, **kwargs):
        ctx = _current.get()
        if ctx is None:
            # No run context: the pipeline is being used outside the app.
            return _plan_inner(prompt, *args, **kwargs)
        if not ctx.ground_dimensions:
            # Grounding is a per-run ablation; refusing an empty plan is not,
            # so that check still applies.
            plan = _plan_inner(prompt, *args, **kwargs)
            _refuse_empty_plan(plan)
            ctx.design_plan = plan if isinstance(plan, dict) else None
            return plan
        grounded, facts = grounding.ground(prompt)
        ctx.emit(
            PHASE_GROUND,
            STATUS_OK if facts else STATUS_INFO,
            grounding.summary(facts),
            subjects=[fact.subject for fact in facts],
            standards=[fact.standard for fact in facts],
            added_chars=len(grounded) - len(prompt),
        )
        try:
            plan = _plan_inner(grounded, *args, **kwargs)
        except json.JSONDecodeError:
            # The overwhelmingly common cause is a prompt the model declined
            # or did not read as a part - "write me a poem", an insult, a
            # question. A bare JSONDecodeError makes that look like a fault
            # in the app.
            said = " ".join((ctx.last_reply or "").split())[:220]
            raise PipelineMessage(
                "The Planner replied with prose instead of a design plan, "
                "which usually means the model did not treat this as a "
                "request for a physical part. Describe a part to make - "
                "its shape, size and features."
                + (f' The model said: "{said}"' if said else "")
            ) from None
        _refuse_empty_plan(plan)
        ctx.design_plan = plan if isinstance(plan, dict) else None
        return plan

    agents.plan = wrap(
        grounded_plan,
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

    # Every agent reaches the network through this one function, so replacing
    # it retargets the whole pipeline. With no context - or a context that did
    # not choose a provider - the original is used untouched.
    _orig_get_client = agents._get_client

    def _get_client():
        ctx = _current.get()
        if ctx is None or ctx.llm is None:
            return _orig_get_client()
        return build_client(
            ctx.llm,
            on_note=lambda message: ctx.emit(PHASE_LOG, STATUS_INFO, message),
        )

    _get_client._cadsmith_wrapped = True  # type: ignore[attr-defined]
    agents._get_client = _get_client

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

        self._apply_spec(ctx, report)
        self._publish_version(ctx, report, geometry)
        return report

    @staticmethod
    def _apply_spec(ctx: RunContext, report: ValidationReport) -> None:
        """Settle measurable claims with the kernel, over the Judge's head.

        The Judge has been observed passing a plate with one hole where four
        were asked for, and rejecting a part whose measurements were exactly
        right. Neither verdict survives contact with a measurement, so where
        the plan said something checkable, the measurement decides.
        """
        step = ctx.version_dir() / "model.step"
        if not ctx.design_plan or not step.exists():
            return

        result = spec.check(ctx.design_plan, step)
        ctx.spec = result
        if result.error:
            ctx.emit(PHASE_SPEC, STATUS_INFO, result.summary(),
                     iteration=ctx.iteration)
            return
        if not result.checked:
            return

        for item in result.checks:
            report.checks.append(ValidationCheck(
                metric=f"spec_{item.key}",
                actual=1.0 if item.passed else 0.0,
                target=1.0,
                passed=item.passed or not item.hard,
                message=(f"{item.label}: wanted {item.expected}, "
                         f"measured {item.actual}"),
            ))

        judge_said = next(
            (c.passed for c in report.checks if c.metric == "llm_judge"), None)

        if not result.ok:
            report.all_passed = False
            note = ("The Judge accepted this, but the kernel disagrees. "
                    if judge_said else "")
            report.feedback_text = (
                f"{note}{result.feedback()}\n\n{report.feedback_text}").strip()
            ctx.emit(PHASE_SPEC, STATUS_FAILED, result.summary(),
                     iteration=ctx.iteration, spec=result.to_dict(),
                     overrode_judge=bool(judge_said))
            return

        # Everything measurable is right. That does not by itself overturn a
        # Judge rejection - it may have seen something no assertion covers -
        # but the Refiner is given the measurements so it is not left acting
        # on a description of the part that measurement contradicts.
        ctx.emit(PHASE_SPEC, STATUS_OK, result.summary(),
                 iteration=ctx.iteration, spec=result.to_dict(),
                 disputes_judge=judge_said is False)
        if judge_said is False:
            report.feedback_text = (
                f"{result.feedback()}\n\nEvery measurable claim in the plan "
                f"is met. If you still see a problem, it is in something not "
                f"measured above - do not restate a dimension as wrong when "
                f"it measures correct.\n\n{report.feedback_text}").strip()

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
            # What the kernel measured against the plan, so the client can
            # show the reason a version was refused rather than only that it
            # was - and can show it disagreeing with the Judge.
            "spec": ctx.spec.to_dict() if ctx.spec is not None else None,
        }
        ctx.versions.append(version)
        # Belongs to the version just published, so it must not carry over.
        ctx.spec = None
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
