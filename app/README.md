# CADSmith — web application

A working front end for the CADSmith pipeline. Describe a part in English, watch
five agents plan it, write CadQuery, build it in the OpenCASCADE kernel and
judge the result, then inspect the solid, its source, its drawing and every
attempt along the way.

Everything on screen comes from the pipeline. The mesh in the viewer is the STL
the kernel exported, the dimensions are the kernel's own measurements, the code
is what the Coder agent wrote, and the verdict is the Judge's. Nothing is
simulated and no progress bar runs on a timer.

## Licence

`app/` is Apache-2.0 (`app/LICENSE`). The research code — `autofab/`,
`scripts/`, `data/`, `run.py` — is by other authors and carries no licence,
so all rights are reserved there by default. The root `NOTICE` explains the
split and lists third-party components.

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
the main README describes is no longer required. **Python 3.11 or 3.12** —
one catalogue dependency uses an API that Python 3.13 removed.

That base install needs no `git`, on purpose. The gear and wider-fastener
libraries are not published on PyPI and install over git, which a Windows
laptop often does not have — in the same file they would fail the whole
install with a confusing error. They are a separate, optional step:

```powershell
.venv\Scripts\python -m pip install -r app\requirements-catalog.txt
```

That one needs [Git for Windows](https://git-scm.com/download/win). Skip it
and the app still runs; the catalogue just covers fewer families, and the
diagnostics chip tells you so and gives you the command.

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
| **AWS Bedrock** | AWS credentials + a region | **Not an Anthropic API key** — see below. |
| **OpenAI** | `OPENAI_API_KEY` | |
| **Ollama** | Ollama running | Local Llama, Qwen, Mistral… `OLLAMA_BASE_URL` to move it off `localhost:11434`. |
| **LM Studio** | its local server | `LMSTUDIO_BASE_URL` to relocate. |
| **Custom** | `CADSMITH_LLM_BASE_URL` | Anything OpenAI-compatible: vLLM, llama.cpp, Together, Groq, OpenRouter. `CADSMITH_LLM_API_KEY` if it wants one. |

Everything except Anthropic and Bedrock goes through one OpenAI-compatible
adapter, so a new endpoint usually needs only a base URL.

### AWS Bedrock

**Bedrock has no API key.** It authenticates with AWS credentials — a named
profile, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, an SSO session, or an
instance role — resolved by botocore and never passed through this app. The
key field is hidden for this provider; what it needs is a **region**:

```bash
echo "AWS_REGION=us-east-1" >> .env     # or choose one in the app
```

Three things differ from the Anthropic path, and each has bitten someone:

- **Model ids carry an `anthropic.` prefix** (`anthropic.claude-sonnet-5`).
  `autofab/agents.py` hardcodes Anthropic-API ids at its call sites, so the
  app substitutes the configured Bedrock id by role — otherwise the first
  call dies with a validation error naming a model nobody chose.
- **Model access is per-region and must be enabled** in the Bedrock console.
  A working set of credentials in the wrong region gets you nothing.
- **`bedrock:ListFoundationModels` is a separate permission** from
  `bedrock:InvokeModel`. A role that can run the pipeline may not be allowed
  to enumerate models, so the picker falls back to the two defaults rather
  than showing an empty list.

The app reports which credential source answered — `Ready — us-east-1,
credentials from env` — because the usual Bedrock failure is the right code
against the wrong account.

If your account or region is still on the older `bedrock-runtime`
InvokeModel route rather than the Messages endpoint, set
`CADSMITH_BEDROCK_LEGACY=1`. The app says when it takes that path rather
than switching silently.

Bedrock is partner-operated, so [its pricing](https://aws.amazon.com/bedrock/pricing/)
is separate from Anthropic's own.

#### First run with real AWS credentials

In order, because each step fails differently:

```bash
# 1. credentials resolve at all — this is what botocore will use
aws sts get-caller-identity

# 2. the region has Anthropic models enabled for THIS account
aws bedrock list-foundation-models --region us-east-1 \
  --by-provider anthropic --query 'modelSummaries[].modelId' --output table

# 3. the app agrees, and says which credential source answered
AWS_REGION=us-east-1 .venv/bin/python -m app.tools.doctor --provider bedrock
```

The doctor prints `provider bedrock  ready - us-east-1 via <source>`. If the
model list in step 2 is empty, model access has not been granted in the
Bedrock console for that region — credentials are fine and nothing will run.

Set the model ids from step 2 in the app; they carry the `anthropic.` prefix
and may be region-prefixed inference profiles (`us.anthropic....`) depending
on what the account is entitled to.

`bedrock:ListFoundationModels` is a separate permission from
`bedrock:InvokeModel`. Without the first, step 2 and the app's model picker
come back empty while generation still works — the picker falls back to the
two defaults.

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

**Keep editing.** Changes chain: ask for 40 teeth, then module 3, then a
wider face, and each lands on top of the last rather than on the original.
Every step becomes its own version in the timeline, so you can click back to
any of them — and editing while looking at an older version branches from
*that* one, not the newest. Patches and Refiner edits interleave freely: a
patch after a full Refiner rewrite still reads its parameters back out of
code it did not write.

A refused edit changes nothing and consumes no version number, so a
misunderstanding costs a sentence, not your part.

Some dimensions are **derived rather than declared**, and asking for those
directly is refused with the arithmetic rather than guessed at. A gear has
no `diameter` to patch — its outside diameter is `module × teeth + 2 ×
module` — so *"set the diameter to 50mm"* would otherwise land on the one
parameter that happens to contain the word, giving you a 50mm hole in a 96mm
gear. Instead it answers:

> a gear has no diameter to set — its outside diameter is module × teeth +
> 2 × module, currently 2 × 20 + 4 = 44mm. Change the tooth count or the
> module to resize it, or say "bore diameter" if you meant that. For 50mm:
> 23 teeth at module 2 gives 50mm, or module 2.27 at 20 teeth gives exactly
> 50mm.

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
    catalog_run.py serves a standard part instead of generating it
    replay.py      re-emits a recorded run at presentation speed
  web/
    index.html  style.css  app.js  api.js  viewer.js  vendor/three.min.js
  catalog/
    standards.py   dimensions from ISO 4762/4014/4032/7089/273/2338, ISO 15,
                   and NEMA ICS 16 motor frames
    parts.py       those tables turned into CadQuery source
    grounding.py   retrieval of the numbers for the Planner
    library.py     gears, fasteners and sprockets from cq_gears/cq_warehouse
    router.py      is this request a standard part, and which one
    verify.py      build it and check it before anyone relies on it
  tools/
    seed_demo_run.py   record demo runs without an API key
    mock_provider.py   a local stand-in for a model backend
    mock_parts.py      ten real mechanical parts it serves
    grounding_ab.py    with/without grounding, against a real model
    check_standards.py verifies the dimension tables against BOLTS
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

## Standard parts, answered from a catalogue

Ask for *"an M8x30 socket head cap screw"* or *"a 20 tooth spur gear, module
2"* and no agent runs. There is nothing to work out: the dimensions come from
the standard, the geometry is exact, and a model can only introduce error.
The part is built in the kernel and returned in a few seconds, **with no API
key at all**.

### Where the geometry comes from

Two Apache-2.0 libraries cover the families they are better at than
hand-written geometry, pinned to a commit because neither is on PyPI:

| family | source |
|---|---|
| spur, helical, herringbone, ring, rack, bevel gears | `cq_gears` |
| 12 screw head types, 7 nut types, M1.6–M64 | `cq_warehouse` |
| sprockets, roller chain, ISO threads | `cq_warehouse` |
| **washers** | `app/catalog/parts.py` — see below |
| bearings (20 ISO 15 designations) | `app/catalog/parts.py` |
| O-rings, dowel pins | `app/catalog/parts.py` |
| compression springs, GT2/HTD/T5/T10 timing pulleys | `app/catalog/parts.py` |

Both are optional. Without them the catalogue covers fewer families and
everything still runs — a demo should not die because a git dependency moved.

Two things were settled by measurement rather than preference. **Washers come
from `parts.py`** because `cq_warehouse` 0.8.0 returns every washer, in all
four standards, as a non-closed shell with roughly twice the correct volume;
ours matches the analytic volume exactly. And **bearings come from `parts.py`**
because `cq_warehouse` carries 31 sizes but only 4 of the 20 ISO 15
designations people actually ask for — it has no 6203.

### The dimension tables are checked, not trusted

`standards.py` was transcribed by hand. `app/tools/check_standards.py`
compares it against [BOLTS](https://github.com/boltsparts/boltsparts), an
independent open library of technical specifications:

```bash
git clone https://github.com/boltsparts/boltsparts.git /tmp/bolts
.venv/bin/python -m app.tools.check_standards --bolts /tmp/bolts
```

**176 values compared, all agree** — ISO 4762, 4014, 4032, 7089, the ISO 15
bearings and the coarse thread pitches. One difference was investigated and
resolved in our favour: BOLTS gives the ISO 7089 M10 washer bore as 10mm,
but that bore tracks the ISO 273 close-fit series exactly at every other
size, and 10.5mm is the close fit for M10.

Nothing from BOLTS is copied here — its data is LGPL-2.1+ and its tooling
GPL-3.0. The tool reads a checkout you supply. NEMA frames, O-ring cords,
the ISO 273 clearance columns and the belt profiles have no counterpart in
BOLTS and remain transcription only; `standards.py` says which is which.

### Nothing is served unverified

`app/catalog/verify.py` builds every candidate before it is served and checks
that it executes, assigns `result`, is a single valid closed solid, and has
real volume. Parts with a closed-form volume are compared against it, which
is what catches a two-times-wrong washer that validity alone would miss.

A part that fails is dropped and the request falls through to the model —
slower, but not wrong.

### Routing is conservative

*"An M8x30 socket head cap screw"* is a standard part. *"A bracket that takes
four M8 screws"* is a custom bracket that merely mentions one, and still goes
to the five agents. So do *"a bearing housing for a 6203"*, *"a gearbox
housing"*, and *"a spur gear"* with no tooth count — under-specified rather
than standard. A wrong substitution hands back confidently wrong hardware,
so anything ambiguous reaches the model.

### It is never disguised as pipeline output

A part no agent produced must not be shown as evidence that the agents work.
Catalogue runs are badged `CATALOG` in the timeline and in History, the
viewer titles them *Standard part*, the Validation panel names the standard
and says plainly that no model wrote it and no Judge assessed it, and the
model labels read `CATALOGUE · CQ_GEARS` rather than naming a model that was
never called.

### It is still editable

That is the whole reason for generating rather than downloading a STEP. The
emitted source is parametric, so *"make it 40 teeth"* is a parameter patch —
no model call — and the kernel rebuilds a gear whose tip diameter measures
84mm, exactly `module × teeth + 2 × module`. A downloaded solid has no
parameters to patch.

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
.venv/bin/python -m app.tests.test_edit_chain      # editing the same part
                                                    # over and over, real kernel
.venv/bin/python -m app.tests.test_replay           # recorded run fidelity
.venv/bin/python -m app.tests.test_catalog         # standard hardware, real kernel
.venv/bin/python -m app.tests.test_grounding       # what reaches the Planner
.venv/bin/python -m app.tests.test_catalog_library # every catalogue part, real
                                                    # kernel; routing; the guard
.venv/bin/python -m app.tests.ui_catalog_check     # catalogue in a browser,
                                                    # with no API key
.venv/bin/python -m app.tests.ui_edit_check        # a long edit chain in a
                                                    # browser, no API key
.venv/bin/python -m app.tests.ui_stress_check      # drives the app badly on
                                                    # purpose: clicking mid-run,
                                                    # double-firing, reloading
.venv/bin/python -m app.tests.test_providers        # non-Anthropic backend, real kernel
.venv/bin/python -m app.tests.test_bedrock         # AWS Bedrock wiring
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
