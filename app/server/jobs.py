"""Job registry: runs the CADSmith pipeline off the request thread.

A generation run is far too slow to serve from a request handler - a complex
part is minutes of LLM calls and CAD kernel work.  Jobs are therefore queued
onto a worker thread and observed through the event stream, while artifacts
land in a per-job directory the HTTP layer can serve directly.

Concurrency is deliberately one job at a time.  ``autofab.agents`` accumulates
token usage in module-level counters, so overlapping runs would report each
other's spend; serialising also keeps a demo within sane API rate limits.
Queued jobs wait rather than fail.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import catalog_run
from .edits import apply_changes, describe, plan_edit
from .events import (
    EventSink,
    PHASE_EDIT,
    PHASE_JOB,
    STATUS_FAILED,
    STATUS_INFO,
    STATUS_OK,
    STATUS_STARTED,
)
from . import budget as budget_mod
from .providers import DEFAULT_PROVIDER, LLMConfig, resolve
from .replay import clone_run, is_replayable, replay_into
from .instrument import (
    PipelineMessage,
    InstrumentedExecutor,
    InstrumentedPipeline,
    InstrumentedValidator,
    RunContext,
    install_agent_hooks,
    set_context,
)

def _llm_problems(job: "Job") -> list[str]:
    from .providers import problems

    return problems(job.options.llm_config())


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

PART_NAME = "part"


@dataclass
class JobOptions:
    """Run parameters exposed to the client.

    ``max_iterations`` defaults below the pipeline's own default of 5: an
    interactive demo values responsiveness, and the published benchmark runs
    remain available through the original scripts.
    """

    max_iterations: int = 3
    max_error_retries: int = 3
    use_vision: bool = True
    #: Give the Planner the published dimensions for whatever the request
    #: names. Off reproduces the pipeline as published, so the two settings
    #: are a live ablation rather than a preference.
    ground_dimensions: bool = True
    #: Answer an unambiguous standard-part request from the catalogue instead
    #: of generating it. Off sends everything through the five agents.
    use_catalog: bool = True
    #: Tokens this run may spend before it is stopped. Every refinement round
    #: is a paid call and the vision Judge sends an image each time, so a loop
    #: that is not converging is a bill rather than a slow run.
    token_budget: int = budget_mod.DEFAULT_BUDGET
    provider: str = DEFAULT_PROVIDER
    generation_model: str = ""
    judge_model: str = ""

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "JobOptions":
        raw = raw or {}
        return cls(
            max_iterations=max(0, min(8, int(raw.get("max_iterations", 3)))),
            max_error_retries=max(0, min(5, int(raw.get("max_error_retries", 3)))),
            use_vision=bool(raw.get("use_vision", True)),
            ground_dimensions=bool(raw.get("ground_dimensions", True)),
            token_budget=max(0, int(raw.get("token_budget")
                                    or budget_mod.DEFAULT_BUDGET)),
            use_catalog=bool(raw.get("use_catalog", True)),
            provider=str(raw.get("provider") or DEFAULT_PROVIDER),
            generation_model=str(raw.get("generation_model") or ""),
            judge_model=str(raw.get("judge_model") or ""),
        )

    def llm_config(self) -> LLMConfig:
        """Resolve the backend for this job, keys included."""
        return resolve(
            provider_id=self.provider,
            generation_model=self.generation_model,
            judge_model=self.judge_model,
            judge_vision=self.use_vision,
        )


@dataclass
class Job:
    id: str
    prompt: str
    options: JobOptions
    directory: Path
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    converged: bool = False
    design_plan: dict = field(default_factory=dict)
    versions: list[dict] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    llm_calls: int = 0
    source: str = "live"
    replay_of: Optional[str] = None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "converged": self.converged,
            "design_plan": self.design_plan,
            "versions": self.versions,
            "tokens": self.tokens,
            "llm_calls": self.llm_calls,
            "options": asdict(self.options),
            "source": self.source,
            "replay_of": self.replay_of,
        }


def _reverted_version(job, new_code: str, base_index: int):
    """The earlier version this code reproduces, if it reproduces one.

    Returns the iteration number of an existing version whose code the
    Refiner has handed back verbatim, ignoring the one it was asked to
    edit; ``None`` when the code is genuinely new. Whitespace is
    normalised so a reformatted copy still counts as the same code.
    """
    def norm(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.strip().splitlines())

    candidate = norm(new_code)
    for version in job.versions:
        index = version.get("iteration")
        if index == base_index:
            continue
        path = job.directory / f"v{index}" / "code.py"
        try:
            if norm(path.read_text()) == candidate:
                return index
        except OSError:
            continue
    return None


class JobManager:
    """Owns job lifecycle, worker thread and on-disk layout."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._sinks: dict[str, EventSink] = {}
        self._contexts: dict[str, RunContext] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cadsmith")
        install_agent_hooks()

    # -- accessors ----------------------------------------------------------

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def sink(self, job_id: str) -> Optional[EventSink]:
        with self._lock:
            return self._sinks.get(job_id)

    def context(self, job_id: str) -> Optional[RunContext]:
        with self._lock:
            return self._contexts.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.summary() for j in jobs]

    def artifact_path(self, job_id: str, version: int, filename: str) -> Optional[Path]:
        """Resolve a version artifact, refusing anything outside the job dir."""
        job = self.get(job_id)
        if job is None:
            return None
        base = (job.directory / f"v{int(version)}").resolve()
        try:
            target = (base / filename).resolve()
            target.relative_to(job.directory.resolve())
        except (ValueError, OSError):
            return None
        return target if target.is_file() else None

    # -- lifecycle ----------------------------------------------------------

    def create(self, prompt: str, options: JobOptions) -> Job:
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        directory = self.runs_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)

        job = Job(id=job_id, prompt=prompt, options=options, directory=directory)
        sink = EventSink(path=directory / "events.jsonl")
        ctx = RunContext(sink=sink, job_dir=directory, part_name=PART_NAME)

        with self._lock:
            self._jobs[job_id] = job
            self._sinks[job_id] = sink
            self._contexts[job_id] = ctx

        self._write_meta(job)
        sink.emit(PHASE_JOB, STATUS_QUEUED, "Queued.", prompt=prompt,
                  options=asdict(options))
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        sink = self.sink(job.id)
        ctx = self.context(job.id)
        assert sink is not None and ctx is not None

        ctx.llm = job.options.llm_config()
        ctx.ground_dimensions = job.options.ground_dimensions
        ctx.budget = budget_mod.Budget(limit=job.options.token_budget)
        set_context(ctx)
        job.status = STATUS_RUNNING
        job.started_at = time.time()
        sink.emit(PHASE_JOB, STATUS_STARTED, "Pipeline started.",
                  llm=ctx.llm.redacted())
        if ctx.llm.generation_model == ctx.llm.judge_model:
            # The pipeline judges with a stronger, separate model on purpose;
            # one model grading its own work is the bias that design avoids.
            sink.emit(
                PHASE_JOB, STATUS_INFO,
                f"Generation and judging both use {ctx.llm.judge_model}, so "
                f"the Judge is grading its own work. Pick a different judge "
                f"model for an independent check.")

        try:
            from autofab import agents

            agents.reset_token_usage()

            # An unambiguous standard part has nothing for five agents to work
            # out: the dimensions come from the standard and a model can only
            # introduce error. The router refuses anything ambiguous and
            # verifies whatever it does return, so reaching here means a sound
            # solid is already in hand. Anything unexpected falls through to
            # the pipeline rather than failing the run.
            routed = None
            if job.options.use_catalog:
                try:
                    routed = catalog_run.find(job.prompt)
                except Exception as error:
                    sink.emit(PHASE_JOB, STATUS_INFO,
                              f"Catalogue lookup skipped: {error}")
            if routed is not None:
                try:
                    catalog_run.serve(ctx, routed, job.directory / "work")
                    # The job itself is catalogue-sourced, not just its
                    # version - otherwise History shows it as an ordinary
                    # converged run and the provenance is lost.
                    job.source = "catalog"
                    job.converged = True
                    job.design_plan = {}
                    job.llm_calls = 0
                    job.tokens = agents.get_token_usage()
                    job.versions = list(ctx.versions)
                    job.status = STATUS_DONE
                    job.finished_at = time.time()
                    self._write_meta(job)
                    sink.emit(PHASE_JOB, STATUS_OK, "Served from the catalogue.",
                              converged=True, tokens=job.tokens,
                              llm_calls=0, source="catalog")
                    return
                except Exception as error:
                    sink.emit(
                        PHASE_JOB, STATUS_INFO,
                        f"The catalogue part would not build here, so the "
                        f"pipeline will generate it instead: {error}")
                    ctx.source = "pipeline"
                    ctx.versions.clear()

            pipeline = InstrumentedPipeline(
                output_dir=str(job.directory / "work"),
                max_error_retries=job.options.max_error_retries,
                max_refinement_iterations=job.options.max_iterations,
                verbose=False,
                use_vision=job.options.use_vision,
            )
            result = pipeline.run(job.prompt, name=PART_NAME)

            job.converged = result.converged
            job.design_plan = result.design_plan or {}
            job.llm_calls = result.total_llm_calls
            job.tokens = agents.get_token_usage()
            job.versions = list(ctx.versions)
            job.status = STATUS_DONE

            try:
                (job.directory / "result.json").write_text(
                    json.dumps(result.to_dict(), indent=2)
                )
            except OSError:
                pass

            sink.emit(
                PHASE_JOB,
                STATUS_OK,
                "Converged." if result.converged else
                "Finished without converging - showing the best attempt.",
                converged=result.converged,
                iterations=len(result.iterations),
                llm_calls=result.total_llm_calls,
                tokens=job.tokens,
                spend=budget_mod.summary(job.tokens, ctx.budget),
                total_ms=result.total_time_ms,
            )
        except Exception as exc:  # surfaced to the client, never swallowed
            job.status = STATUS_ERROR
            job.error = (str(exc) if isinstance(
                exc, (PipelineMessage, budget_mod.BudgetExceeded))
                else f"{type(exc).__name__}: {exc}")
            job.versions = list(ctx.versions)
            job.tokens = agents.get_token_usage()
            sink.emit(PHASE_JOB, STATUS_FAILED, job.error,
                      tokens=job.tokens,
                      spend=budget_mod.summary(job.tokens, ctx.budget),
                      traceback=traceback.format_exc()[-4000:])
        finally:
            job.finished_at = time.time()
            set_context(None)
            sink.close()
            self._write_meta(job)

    # -- natural-language edits ---------------------------------------------

    def submit_edit(self, job: Job, instruction: str,
                    base_version: Optional[int] = None) -> None:
        """Queue an edit against ``base_version``, or the latest version.

        The base is explicit because the viewer can be showing an earlier
        attempt: editing must change what the person is looking at, not
        whatever happens to be newest.
        """
        sink = self.sink(job.id)
        if sink is not None:
            sink.emit(PHASE_JOB, STATUS_QUEUED, "Edit queued.",
                      instruction=instruction, base_version=base_version)
        job.status = STATUS_QUEUED
        self._pool.submit(self._run_edit, job, instruction, base_version)

    def _run_edit(self, job: Job, instruction: str,
                  base_version: Optional[int] = None) -> None:
        """Apply an edit, by parameter patch where possible or the Refiner.

        The two paths differ in more than speed.  A patch changes a number the
        script already declares, so the kernel alone can confirm the result.
        The Refiner writes new code, so the full validation - vision Judge
        included - is worth its cost.
        """
        sink = self.sink(job.id)
        ctx = self.context(job.id)
        if sink is None:
            return
        if ctx is None:
            # Never fail silently here: the client is waiting on the stream.
            job.status = STATUS_DONE
            job.finished_at = time.time()
            sink.emit(PHASE_JOB, STATUS_FAILED,
                      "That run cannot be edited in this session.", edit=True)
            return

        ctx.llm = job.options.llm_config()
        ctx.ground_dimensions = job.options.ground_dimensions
        ctx.budget = budget_mod.Budget(limit=job.options.token_budget)
        set_context(ctx)
        job.status = STATUS_RUNNING
        started = time.time()

        try:
            from autofab import agents

            agents.reset_token_usage()
            if base_version is None:
                previous = job.versions[-1]
            else:
                previous = next(
                    (v for v in job.versions
                     if v.get("iteration") == base_version),
                    job.versions[-1])
            source_dir = job.directory / f"v{previous['iteration']}"
            code = (source_dir / "code.py").read_text()
            next_index = max(v["iteration"] for v in job.versions) + 1

            plan = plan_edit(code, instruction)
            if plan.possible:
                new_code = apply_changes(code, plan.changes)
                ctx.method = "parameter patch"
                ctx.changes = [c.to_dict() for c in plan.changes]
                sink.emit(PHASE_EDIT, STATUS_OK, describe(plan.changes),
                          method=ctx.method, instruction=instruction,
                          changes=ctx.changes,
                          base_version=previous["iteration"])
            elif plan.guidance:
                # The reason already says what to do, so offering the Refiner
                # would be misleading - this is not a structural change and
                # no agent is needed to resolve it.
                job.status = STATUS_DONE
                sink.emit(PHASE_JOB, STATUS_FAILED,
                          f"Not applied - {plan.reason}", edit=True)
                return
            elif _llm_problems(job):
                # The patch path needs no model, so it stays available with no
                # provider configured; this one does not.
                job.status = STATUS_DONE
                sink.emit(
                    PHASE_JOB, STATUS_FAILED,
                    f"That is not a parameter change ({plan.reason}), so it "
                    f"needs the Refiner agent - but no model backend is "
                    f"available: {_llm_problems(job)[0]} Try naming a "
                    f"dimension the script declares.",
                    edit=True)
                return
            else:
                ctx.method = "refiner agent"
                ctx.changes = []
                sink.emit(PHASE_EDIT, STATUS_INFO,
                          f"Not a parameter change ({plan.reason}) - "
                          f"asking the Refiner agent.",
                          method=ctx.method, instruction=instruction,
                          reason=plan.reason)
                new_code = agents.refine_geometry(
                    code, instruction, job.design_plan, job.prompt)
                reverted = _reverted_version(job, new_code,
                                             previous["iteration"])
                if reverted is not None:
                    # The Refiner is given the current code and the original
                    # prompt. A smaller model sometimes answers from the
                    # prompt alone and hands back an earlier version whole,
                    # which would silently undo every edit since. Recording
                    # it would also leave the next edit building on the
                    # reverted code, so stop and keep what the person has.
                    job.status = STATUS_DONE
                    sink.emit(
                        PHASE_JOB, STATUS_FAILED,
                        f"Not applied - the Refiner returned version "
                        f"{reverted} unchanged instead of editing version "
                        f"{previous['iteration']}, which would have undone "
                        f"your earlier edits. Version "
                        f"{previous['iteration']} is still the latest. Try "
                        f"naming the dimension you want changed.",
                        edit=True)
                    return

            ctx.source = "edit"
            ctx.instruction = instruction

            executor = InstrumentedExecutor(
                output_dir=str(job.directory / "work"), timeout_seconds=60)
            part = f"{PART_NAME}_iter{next_index}"
            result = executor.execute(new_code, name=part)

            # Only the agent path gets error repair: it wrote the code, so a
            # failure is its to fix.  A failed patch means the requested value
            # is not buildable, which the person needs to be told, not hidden.
            retries = 0
            while (not result.success and ctx.method == "refiner agent"
                   and retries < job.options.max_error_retries):
                retries += 1
                new_code = agents.fix_error(new_code, result.error, job.design_plan)
                result = executor.execute(new_code, name=part)

            if not result.success:
                job.status = STATUS_DONE
                sink.emit(
                    PHASE_JOB, STATUS_FAILED,
                    f"That change could not be built: {result.error_type}. "
                    f"The previous version is unchanged.",
                    error=(result.error or "")[-2000:], edit=True)
                return

            validator = InstrumentedValidator()
            if ctx.method == "refiner agent":
                render = str(job.directory / "work" / f"{part}_render.png")
                validator.validate(
                    result.geometry_json,
                    code=new_code,
                    prompt=f"{job.prompt}\n\nRequested change: {instruction}",
                    stl_path=result.stl_path if job.options.use_vision else "",
                    render_save_path=render if job.options.use_vision else "",
                )
            else:
                # No prompt or code, so autofab.validator runs its kernel
                # checks and skips the Judge (validator.py:150).
                validator.validate(result.geometry_json)

            job.versions = list(ctx.versions)
            job.tokens = agents.get_token_usage()
            job.status = STATUS_DONE
            sink.emit(PHASE_JOB, STATUS_OK, "Edit applied.",
                      edit=True, method=ctx.method,
                      total_ms=(time.time() - started) * 1000,
                      tokens=job.tokens, iterations=len(job.versions))
        except Exception as exc:
            job.status = STATUS_DONE
            sink.emit(PHASE_JOB, STATUS_FAILED,
                      f"The edit failed: {type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc()[-4000:], edit=True)
        finally:
            # Every terminal path of an edit lands here, including the early
            # returns for a refused or unbuildable change. Without this the
            # job kept reporting the *original* run's finish time, so an API
            # client polling finished_at could not tell an edit had happened
            # at all - the browser only got away with it because it follows
            # the event stream instead.
            job.finished_at = time.time()
            ctx.source, ctx.method = "pipeline", ""
            ctx.instruction, ctx.changes = "", []
            set_context(None)
            self._write_meta(job)

    # -- replay --------------------------------------------------------------

    def can_replay(self, job: Job) -> bool:
        return is_replayable(job.directory)

    def create_replay(self, source: Job, speed: float = 6.0) -> Job:
        """Register a replay of a recorded run and queue it.

        The artifacts are copied rather than referenced, so the replay is a
        self-contained run in its own right - it survives the original being
        deleted, and it cannot mutate what it was made from.
        """
        job_id, directory = clone_run(source.directory, self.runs_dir)

        job = Job(
            id=job_id,
            prompt=source.prompt,
            options=source.options,
            directory=directory,
            design_plan=source.design_plan,
            converged=source.converged,
            versions=list(source.versions),
            source="replay",
            replay_of=source.id,
        )
        sink = EventSink(path=directory / "events.jsonl")
        ctx = RunContext(sink=sink, job_dir=directory, part_name=PART_NAME)

        with self._lock:
            self._jobs[job_id] = job
            self._sinks[job_id] = sink
            self._contexts[job_id] = ctx

        self._write_meta(job)
        sink.emit(PHASE_JOB, STATUS_QUEUED,
                  f"Replaying a recorded run ({source.id}).",
                  prompt=source.prompt, replay_of=source.id, replayed=True)
        self._pool.submit(self._run_replay, job, source.directory, speed)
        return job

    def _run_replay(self, job: Job, source_dir: Path, speed: float) -> None:
        sink = self.sink(job.id)
        if sink is None:
            return
        job.status = STATUS_RUNNING
        job.started_at = time.time()
        try:
            replay_into(sink, source_dir, speed=speed)
            job.status = STATUS_DONE
            sink.emit(PHASE_JOB, STATUS_OK, "Replay finished.",
                      converged=job.converged, replayed=True,
                      iterations=len(job.versions))
        except Exception as exc:
            job.status = STATUS_ERROR
            job.error = (str(exc) if isinstance(exc, PipelineMessage)
                         else f"{type(exc).__name__}: {exc}")
            sink.emit(PHASE_JOB, STATUS_FAILED, job.error, replayed=True)
        finally:
            job.finished_at = time.time()
            sink.close()
            self._write_meta(job)

    def _write_meta(self, job: Job) -> None:
        try:
            (job.directory / "meta.json").write_text(
                json.dumps(job.summary(), indent=2, default=str)
            )
        except OSError:
            pass

    # -- restoring past runs -------------------------------------------------

    def load_from_disk(self) -> int:
        """Re-register finished runs found on disk (survives a server restart)."""
        restored = 0
        for directory in sorted(self.runs_dir.iterdir()):
            meta_file = directory / "meta.json"
            if not directory.is_dir() or not meta_file.is_file():
                continue
            with self._lock:
                if directory.name in self._jobs:
                    continue
            try:
                meta = json.loads(meta_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            job = Job(
                id=meta.get("id", directory.name),
                prompt=meta.get("prompt", ""),
                options=JobOptions.from_dict(meta.get("options")),
                directory=directory,
                status=meta.get("status", STATUS_DONE),
                created_at=meta.get("created_at", 0.0),
                started_at=meta.get("started_at"),
                finished_at=meta.get("finished_at"),
                error=meta.get("error"),
                converged=bool(meta.get("converged")),
                design_plan=meta.get("design_plan") or {},
                versions=meta.get("versions") or [],
                tokens=meta.get("tokens") or {},
                llm_calls=meta.get("llm_calls", 0),
                source=meta.get("source", "live"),
                replay_of=meta.get("replay_of"),
            )
            # A job interrupted by a restart can never resume; mark it honestly.
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.status = STATUS_ERROR
                job.error = "Interrupted by a server restart."

            # A restored run must stay editable, which needs both a writable
            # log and a context carrying the versions it already has.
            sink = EventSink.from_file(directory / "events.jsonl",
                                       keep_appending=True)
            ctx = RunContext(sink=sink, job_dir=directory, part_name=PART_NAME)
            ctx.versions = list(job.versions)
            if job.versions:
                ctx.iteration = max(v.get("iteration", 0) for v in job.versions)

            with self._lock:
                self._jobs[job.id] = job
                self._sinks[job.id] = sink
                self._contexts[job.id] = ctx
            restored += 1
        return restored
