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

macOS and Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r app/requirements-app.txt
```

Windows PowerShell — note `python`, not `python3`, and `Scripts\` rather than
`bin/`:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r app\requirements-app.txt
```

CadQuery 2.8 and VTK install as ordinary pip wheels, so the conda environment
the main README describes is no longer required. Tested on Python 3.11.

**On Windows, keep the checkout out of OneDrive.** `.venv` runs to several
hundred megabytes, and syncing it is slow and can lock files mid-install.
Something like `C:\dev\cadsmith_prashant_demo` avoids it.

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

macOS and Linux:

```bash
./app/run_app.sh              # http://127.0.0.1:8000
PORT=9000 ./app/run_app.sh    # somewhere else
./app/run_app.sh --reload     # reload on source changes
```

Windows PowerShell:

```powershell
.\app\run_app.ps1
.\app\run_app.ps1 -Port 9000
.\app\run_app.ps1 -Reload
```

Both read `.env`, so the health check reports the truth before the first run
starts.

## Behind a corporate proxy

If model calls fail with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
```

your network is inspecting TLS and re-signing it with a company certificate
authority. The OS trusts that CA — which is why your browser works — but
Python ships its own `certifi` bundle that has never seen it.

`truststore` is in `requirements-app.txt` and the app injects it at startup,
so Python uses the OS certificate store instead. If you installed before that
was added:

```powershell
.venv\Scripts\python -m pip install truststore
```

Alternatively, point at an explicit bundle containing your company CA — the
app honours `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` and prefers either over
everything else. `CADSMITH_TRUST_STORE=certifi` opts back out.

**Do not disable certificate verification.** On exactly the networks that
need this fix, accepting any certificate is the wrong response. Nothing here
turns verification off.

The preflight reports which trust source is in use, and the health chip shows
it too.

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
- *Standard dimensions* — whether the Planner is handed the published numbers
  for any standard part the request names. See **Grounding the Planner**
  above.

**Watch it work.** The five stages are driven by real events. Execution errors
and refinement rounds appear as they happen, which is the part worth showing:
the loop is the contribution.

**Reload without losing the run.** The pipeline keeps working server-side
while the browser is away, so a refresh, a closed lid or a stray Cmd-R
reattaches to the run in progress rather than dropping it — the event log is
append-only and replays from where the page left off. A run that finished
while you were away opens on its result.

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
  catalog/
    standards.py   dimensions from ISO 4762/4014/4032/7089/273/2338, ISO 15,
                   and NEMA ICS 16 motor frames
    parts.py       those tables turned into CadQuery source
    grounding.py   retrieval of the numbers for the Planner
  tools/
    seed_demo_run.py   record demo runs without an API key
    mock_provider.py   a local stand-in for a model backend
    mock_parts.py      ten real mechanical parts it serves
    grounding_ab.py    with/without grounding, against a real model
    doctor.py          preflight: can this machine run a demo
  tests/
  runs/         one directory per run: events.jsonl, meta.json, v0/ v1/ …
```

Each version bundle holds `code.py`, `model.stl`, `model.step`,
`geometry.json`, `validation.json`, `render.png` and, once requested,
`drawing.svg`.

three.js is vendored rather than loaded from a CDN: a live demo should not
depend on the network.

## Testing without spending quota

A local OpenAI-compatible server stands in for a real provider, so the whole
pipeline runs — CadQuery builds the geometry, VTK renders it, the loop
refines — with the model replaced by canned replies:

```bash
python -m app.tools.mock_provider              # well behaved
python -m app.tools.mock_provider --fail 429   # rate limited
python -m app.tools.mock_provider --fail badjson   # prose around the JSON
python -m app.tools.mock_provider --fail novision  # refuses images
python -m app.tools.mock_provider --fail empty     # reasoning-only reply
python -m app.tools.mock_provider --fail hang      # never answers
```

Then in the app pick **Custom (OpenAI-compatible)**, base URL
`http://127.0.0.1:8123/v1`, models `mock-coder` and `mock-judge`.

The reply is chosen by what you asked for, from ten parts in
`app/tools/mock_parts.py` — pillow block, L-bracket with a gusset, pipe
flange, hydraulic manifold, NEMA 23 motor mount, V-belt pulley, cover plate
with an O-ring groove, stepped shaft, flanged bushing, flat plate. Things a
CAD engineer actually asks for, rather than the primitives the benchmark
leans on. Vague requests still land somewhere sensible: *"something to hold a
rotating shaft"* produces a pillow block.

Each part carries one plausible mistake in its first attempt — a bore too
small, a gusset missing, six bolt holes where the plan says eight, manifold
ports that stop short of the gallery — so the Judge rejects it in the terms
an engineer would use and the refinement loop is exercised rather than
skipped. Both variants are executed in the kernel before being added: the
flawed one must *build*, or the Error Refiner would catch it as a crash and
the Judge would never weigh in on the geometry.

`--part v_pulley` forces one regardless of the prompt.

Better than a real key for reproducing a failure, because the misbehaviour is
deterministic.

## Grounding the Planner in real dimensions

The Planner is the only agent with no retrieval of its own. KB1 gives the
Coder CadQuery API docs, KB2 gives the Error Refiner error fixes, and the
agent that decides every target dimension gets the bare prompt — plus a
system prompt telling it to *"estimate reasonable engineering dimensions"*
when the request does not state them. Estimating is where a NEMA 23 acquires
a 22mm pilot bore instead of 38.1mm.

`app/catalog/grounding.py` is the missing third knowledge base: not geometry,
not API docs, just the published numbers for whatever a request names. Ask
for a NEMA 23 plate and the Planner is handed the frame size, the 47.14mm
bolt pattern, the 38.1mm pilot boss, the shaft diameter and the M5 hardware
that goes with it — about 1KB, and under 0.1s per run.

Retrieval is conservative: a request naming nothing standard gets nothing
appended, and an unrecognised designation (`M99`, `9999 bearing`) is never
guessed at. Wrong facts would be worse than none, since they reach the plan,
the code and the Judge's acceptance criteria together.

**It is an ablation, not a setting.** The *Standard dimensions* toggle sits
next to *Vision Judge*; off reproduces the published pipeline exactly. And
like everything else in `instrument.py` it is gated on an active run context,
so `run.py` and the benchmark scripts see the published prompt untouched —
`test_grounding.py` asserts that by capturing the exact text arriving at the
LLM boundary.

**What is and is not proven.** The tests prove the numbers reach the model
and that the toggle works in both directions. They cannot prove a given model
then *uses* them — that needs a real key:

```bash
.venv/bin/python -m app.tools.grounding_ab --provider anthropic
```

runs five prompts with and without grounding, one Planner call per cell, and
scores whether the published value came out in the plan. If a model already
knows the number, grounding buys nothing for that fact and the harness says
so rather than assuming the feature works.

## Standard hardware, generated rather than downloaded

`app/catalog/` builds standard mechanical hardware parametrically: socket
head cap screws (ISO 4762), hex bolts (ISO 4014), hex nuts (ISO 4032), plain
washers (ISO 7089), deep groove ball bearings (ISO 15), O-rings and dowel
pins — 78 verified variants.

```python
from app.catalog import parts, standards

screw = parts.socket_head_cap_screw("M8", 30)
print(screw.code)                      # standalone CadQuery, assigns `result`
standards.clearance_hole("M8")         # 9.0 — the hole it actually needs
standards.counterbore("M8")            # (14.0, 8.0)
```

**Why generate instead of importing a vendor STEP.** A STEP is a frozen
B-rep: no history, no parameters. You can cut it, fillet it and place it, but
you cannot turn an M8 bolt into an M10. What `parts.py` emits is *source*,
with the standard's dimensions as named assignments — so the code panel shows
real numbers, the parameter editor rewrites them, and the Refiner can
restructure the part. `test_catalog.py` proves this by running the app's own
editor over a generated screw and rebuilding it in the kernel.

**Threads are not modelled.** A swept helix on one M8×30 measured 283× the
build time and 224× the STEP size of a plain shank — 2.5s and 1.3MB for a
single screw. Every production CAD library shows plain shanks for the same
reason. `threaded=True` gives you the real helix if you want to pay for it.

**Selection is strict.** `parts.select()` returns a catalogue part only for an
unambiguous designation. A custom part that merely *mentions* hardware — "a
bearing housing for a 6203", "a bracket with four M8 clearance holes" — stays
a custom part and goes to the pipeline, because substituting the component
for the assembly would be silently wrong.

The dimension tables in `standards.py` were transcribed by hand and carry
nominal values only, no tolerances. Check them against a real reference
before anything is manufactured.

## Tests

```bash
.venv/bin/pip install -r app/requirements-dev.txt

.venv/bin/python -m app.tests.test_instrumentation  # pipeline hooks, real kernel
.venv/bin/python -m app.tests.test_server           # HTTP, SSE, artifacts
.venv/bin/python -m app.tests.test_edits            # edit interpretation
.venv/bin/python -m app.tests.test_edit_flow        # both edit paths, real kernel
.venv/bin/python -m app.tests.test_replay           # recorded run fidelity
.venv/bin/python -m app.tests.test_catalog         # standard hardware, real kernel
.venv/bin/python -m app.tests.test_grounding       # what reaches the Planner
.venv/bin/python -m app.tests.test_providers        # non-Anthropic backend, real kernel
.venv/bin/python -m app.tests.ui_check              # real browser, needs a server
.venv/bin/python -m app.tests.ui_generate_check     # a real run in a browser,
                                                    # plus provider failures
.venv/bin/python -m app.tests.ui_parts_check       # real parts, vague to
                                                    # complex; mid-run clicking;
                                                    # stage timings
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
