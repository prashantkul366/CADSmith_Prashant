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
from typing import Any, Optional

from .events import (
    EventSink,
    PHASE_JOB,
    STATUS_FAILED,
    STATUS_INFO,
    STATUS_OK,
    STATUS_STARTED,
)
from .instrument import (
    InstrumentedPipeline,
    RunContext,
    install_agent_hooks,
    set_context,
)

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

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "JobOptions":
        raw = raw or {}
        return cls(
            max_iterations=max(0, min(8, int(raw.get("max_iterations", 3)))),
            max_error_retries=max(0, min(5, int(raw.get("max_error_retries", 3)))),
            use_vision=bool(raw.get("use_vision", True)),
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
        }


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

        set_context(ctx)
        job.status = STATUS_RUNNING
        job.started_at = time.time()
        sink.emit(PHASE_JOB, STATUS_STARTED, "Pipeline started.")

        try:
            from autofab import agents

            agents.reset_token_usage()
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
                total_ms=result.total_time_ms,
            )
        except Exception as exc:  # surfaced to the client, never swallowed
            job.status = STATUS_ERROR
            job.error = f"{type(exc).__name__}: {exc}"
            job.versions = list(ctx.versions)
            sink.emit(PHASE_JOB, STATUS_FAILED, job.error,
                      traceback=traceback.format_exc()[-4000:])
        finally:
            job.finished_at = time.time()
            set_context(None)
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
            )
            # A job interrupted by a restart can never resume; mark it honestly.
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.status = STATUS_ERROR
                job.error = "Interrupted by a server restart."

            sink = EventSink.from_file(directory / "events.jsonl")
            with self._lock:
                self._jobs[job.id] = job
                self._sinks[job.id] = sink
            restored += 1
        return restored
