# CADSmith — web application

A working front end for the CADSmith pipeline. Describe a part in English, watch
five agents plan it, write CadQuery, build it in the OpenCASCADE kernel and
judge the result, then inspect the solid, its source, its drawing and every
attempt along the way.

Everything on screen comes from the pipeline. The mesh in the viewer is the STL
the kernel exported, the dimensions are the kernel's own measurements, the code
is what the Coder agent wrote, and the verdict is the Judge's. Nothing is
simulated and no progress bar runs on a timer.

## Nothing in the research code changed

`autofab/`, `scripts/`, `data/`, `run.py` and `requirements.txt` are untouched.
The published pipeline and its benchmark numbers stay reproducible; this app is
a layer around them.

Observability without edits works in three ways: `Pipeline` builds its executor
and validator as instance attributes, so instrumented subclasses are swapped in
after construction; the agents are reached through the module object, so
wrapping its function attributes intercepts every LLM call; and a `ContextVar`
scopes both to the current job. With no context set, the wrappers are
pass-throughs — importing the app changes nothing for `run.py` or the benchmark
scripts.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r app/requirements-app.txt
```

CadQuery 2.8 and VTK install as ordinary pip wheels, so the conda environment
the main README describes is no longer required.

On **headless Linux**, VTK imports fine and then fails at render time with no GL
backend, which silently costs you the vision Judge. Install a software
rasteriser:

```bash
sudo apt-get install libosmesa6
```

macOS and Windows need nothing extra. The app tells you either way — see
Diagnostics below.

Set a key for whichever provider you want (see **Model backends** below):

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
# or
echo "OPENAI_API_KEY=sk-..." > .env
# or run a local model and set nothing at all
```

## Running

```bash
./app/run_app.sh              # http://127.0.0.1:8000
PORT=9000 ./app/run_app.sh    # somewhere else
./app/run_app.sh --reload     # reload on source changes
```

`run_app.sh` reads `.env`, so the health check reports the truth before the
first run starts.

## Model backends

`autofab/agents.py` reaches the network through one function, and every agent
goes through it, so the whole pipeline can be pointed elsewhere without
touching the research code. Pick a provider in the app's left panel.

| Provider | Needs | Notes |
|---|---|---|
| **Anthropic** | `ANTHROPIC_API_KEY` | Default. Uses the real SDK, so this path behaves exactly as the published pipeline does. |
| **OpenAI** | `OPENAI_API_KEY` | |
| **Ollama** | Ollama running | Local Llama, Qwen, Mistral… `OLLAMA_BASE_URL` to move it off `localhost:11434`. |
| **LM Studio** | its local server | `LMSTUDIO_BASE_URL` to relocate. |
| **Custom** | `CADSMITH_LLM_BASE_URL` | Anything OpenAI-compatible: vLLM, llama.cpp, Together, Groq, OpenRouter. `CADSMITH_LLM_API_KEY` if it wants one. |

Everything except Anthropic goes through one OpenAI-compatible adapter, so a
new endpoint usually needs only a base URL.

**Keys** come from `.env`, or you can paste one into the app for the current
server process. A pasted key is held in memory only — never written to disk,
never logged, never sent back to the browser. Restarting clears it.

**Model lists come from the provider**, not from a hardcoded table, so a local
Ollama offers the models you have actually pulled.

**Generation and judging are configured separately**, and may be different
models or different providers — a local Llama writing CadQuery with GPT-4o
judging is a valid setup. The pipeline judges with a stronger, independent
model on purpose; point both roles at the same model and the app says so,
because that reintroduces the self-confirmation the design avoids.

Two things degrade rather than fail. A model that cannot accept images gets
retried without the render, and the Judge is labelled as having run on kernel
metrics alone. A model that wraps its JSON in prose — common with smaller
local ones — has the object extracted, since the Planner and Judge parse
strictly.

Local providers are probed for reachability, so "ready" means something is
actually listening rather than merely that no key is required.

## What you can do without any model backend

The agents need one; the CAD kernel does not. Without one you can still:

- **Replay a recorded run** — the events a real run produced, against the
  artifacts it exported. Real geometry, real source, real Judge text; only the
  pacing differs. Marked `REPLAY` in the UI.
- **Edit by parameter patch** — "make it 15mm thick" rewrites the assignment in
  the generated script and rebuilds it in CadQuery, about a second, no model
  call.
- **Open any past run**, compare its iterations, and export STEP/STL/`.py`.
- **Build engineering drawings** from any exported solid.

Seed two demo runs to try this immediately:

```bash
.venv/bin/python -m app.tools.seed_demo_run
```

These have real geometry and real renders, with the agent replies scripted
rather than generated. They are tagged `FIXTURE` in the UI so the distinction
is never hidden. Once you have run the pipeline for real, prefer replaying one
of those.

## Using it

**Generate.** Type a description, or pick a benchmark prompt from
`data/dataset_v2`. Two options matter:

- *Refinement iterations* — how many times the Refiner may correct the geometry
  before the run gives up. Defaults to 3; the pipeline's own default is 5.
- *Vision Judge* — whether the Judge sees the three-view render alongside the
  kernel metrics. Turning it off reproduces the paper's ablation live.

**Watch it work.** The five stages are driven by real events. Execution errors
and refinement rounds appear as they happen, which is the part worth showing:
the loop is the contribution.

**Compare attempts.** Every attempt is a card in the timeline under the viewer.
Click a rejected one to load its geometry, its source and the Judge's reasons
for rejecting it, next to the render the Judge was given.

**Edit.** Type a change in the bar at the bottom. A request naming a parameter
the script declares is patched and rebuilt by the kernel; anything structural
goes to the Refiner agent. The UI says which ran. Ambiguous requests are never
guessed — "make the thickness 12mm" against a script with both
`base_thickness` and `support_thickness` is refused rather than resolved
arbitrarily.

**Drawing.** Front, top, right and isometric views projected from the exported
STEP solid, with hidden lines resolved and all four at a common scale.

**Choose a backend.** Provider, generation model and judge model sit under the
run options. See **Model backends** above.

**Diagnostics.** The chip in the header reports CadQuery, offscreen rendering,
the model backend and the metrics stack. Click it for detail. A demo that will
not work says so before you start.

## Layout

```
app/
  server/
    app.py         HTTP routes, SSE stream, artifact serving, health
    jobs.py        job queue, per-job directories, edits, replays
    events.py      append-only event log, mirrored to events.jsonl
    instrument.py  makes the stock pipeline observable, without editing it
    edits.py       parameter-patch interpretation, with the Refiner as fallback
    providers.py   Anthropic, OpenAI, Ollama and any OpenAI-compatible backend
    drawing.py     orthographic projections composed into a sheet
    replay.py      re-emits a recorded run at presentation speed
  web/
    index.html  style.css  app.js  api.js  viewer.js  vendor/three.min.js
  tools/
    seed_demo_run.py   record demo runs without an API key
  tests/
  runs/         one directory per run: events.jsonl, meta.json, v0/ v1/ …
```

Each version bundle holds `code.py`, `model.stl`, `model.step`,
`geometry.json`, `validation.json`, `render.png` and, once requested,
`drawing.svg`.

three.js is vendored rather than loaded from a CDN: a live demo should not
depend on the network.

## Tests

```bash
.venv/bin/pip install -r app/requirements-dev.txt

.venv/bin/python -m app.tests.test_instrumentation  # pipeline hooks, real kernel
.venv/bin/python -m app.tests.test_server           # HTTP, SSE, artifacts
.venv/bin/python -m app.tests.test_edits            # edit interpretation
.venv/bin/python -m app.tests.test_edit_flow        # both edit paths, real kernel
.venv/bin/python -m app.tests.test_replay           # recorded run fidelity
.venv/bin/python -m app.tests.test_providers        # non-Anthropic backend, real kernel
.venv/bin/python -m app.tests.ui_check              # real browser, needs a server
```

Only the Anthropic HTTP call is faked, by patching `agents._get_client`. The
real agent bodies run, including prompt assembly and RAG retrieval from KB1 and
KB2, and CadQuery and VTK do real work throughout.

## Two bugs left in the research code

Both are one-line fixes, deliberately not applied so `autofab/` stays as
published:

- `autofab/validator.py:174` records a **failed** Judge API call as a *passing*
  check, so a rate limit or network blip silently converges a part. The app
  detects this and reports it as a failure, but `run.py` and the benchmark
  scripts still behave as published.
- `requirements.txt` omits `scipy`, which `autofab/metrics.py:30` imports, so a
  fresh install from that file alone cannot compute CD/F1/IoU.
  `app/requirements-app.txt` adds it.
