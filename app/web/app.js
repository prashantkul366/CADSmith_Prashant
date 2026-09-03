"use strict";
/* ═══════════════════════════════════════════════════════════════════════
   CADSmith — application logic.

   Every panel on screen is fed by the backend: the Design Plan is the
   Planner agent's JSON, the code is what the Coder wrote, the dimensions
   are measured by the OpenCASCADE kernel, and the verdict is the Opus
   Judge's.  Nothing is simulated, and progress is driven by events the
   pipeline actually emitted rather than by timers.
   ═══════════════════════════════════════════════════════════════════════ */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
// Escapes quotes too: this output is used inside attributes (data-prompt),
// where an unescaped quote would end the attribute early.
const esc = s => String(s ?? "").replace(/&/g, "&amp;")
  .replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const fmt = n => (n === null || n === undefined || Number.isNaN(n)) ? "—"
  : (Number.isInteger(n) ? String(n) : (Math.round(n * 100) / 100).toString());

const S = {
  jobId: null,
  versions: [],      // one entry per pipeline iteration or applied edit
  selected: -1,
  health: null,
  busy: false,
  stream: null,
  designPlan: null,
  converged: false,
  replay: false,
  seq: 0,          // next unseen event sequence number
  providers: [],
  provider: null,
  editing: false,
};

/* ── the five stages shown while a run is in flight ─────────────────── */
const STAGES = [
  { key: "plan",    label: "Planning the part" },
  { key: "code",    label: "Writing CadQuery" },
  { key: "execute", label: "Building the solid" },
  { key: "judge",   label: "Validating geometry" },
  { key: "done",    label: "Ready" },
];

/* ═══════════════════════ helpers ═══════════════════════ */

let toastTimer;
function toast(message, icon) {
  const el = $("#toast");
  el.innerHTML = (icon ||
    `<svg class="icn" viewBox="0 0 24 24" style="color:var(--valid)"><path d="M20 6 9 17l-5-5"/></svg>`)
    + `<span>${esc(message)}</span>`;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 2600);
}

function warnToast(message) {
  toast(message, `<svg class="icn" viewBox="0 0 24 24" style="color:var(--warn)"><path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>`);
}

/* One pass over the raw source, escaping each segment as it is emitted.
   Chained .replace() calls cannot be used here: the first pass injects
   markup, and a later pass then matches inside it - the string rule would
   see the class="c" of a comment span and wrap it, corrupting the output. */
const PY_TOKENS = new RegExp([
  /(#[^\n]*)/,                                   // comment
  /("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/,     // string
  /\b(import|from|as|def|for|in|return|if|else|not|and|or|None|True|False|lambda|while|range)\b/,
  /\b(\d+\.?\d*)\b/,                              // number
  /\.([A-Za-z_]\w*)(?=\s*\()/,                    // method call
].map(r => r.source).join("|"), "g");

function highlight(code) {
  const CLASSES = ["c", "s", "k", "n", "f"];
  let out = "", last = 0, match;
  PY_TOKENS.lastIndex = 0;
  while ((match = PY_TOKENS.exec(code)) !== null) {
    out += esc(code.slice(last, match.index));
    const group = CLASSES.findIndex((_, i) => match[i + 1] !== undefined);
    const text = match[group + 1];
    // The method-call rule captures the name but matches the leading dot too.
    if (CLASSES[group] === "f") out += ".";
    out += `<span class="${CLASSES[group]}">${esc(text)}</span>`;
    last = match.index + match[0].length;
  }
  return out + esc(code.slice(last));
}

function setCode(code, highlightKeys) {
  let html = highlight(code || "");
  (highlightKeys || []).forEach(key => {
    html = html.replace(
      new RegExp(`(^|\\n)(\\s*${key}\\s*=[^\\n]*)`, "g"),
      (_m, lead, line) => `${lead}<span class="hot">${line}</span>`);
  });
  $("#hl").innerHTML = html + "\n";
  $("#codeStat").textContent = code
    ? `${code.split("\n").length} LINES · PYTHON` : "— — —";
}

/* ═══════════════════════ health ═══════════════════════ */

async function loadHealth() {
  const chip = $("#healthChip");
  try {
    S.health = await API.health();
  } catch (_) {
    chip.className = "health bad";
    chip.querySelector("span").textContent = "SERVER UNREACHABLE";
    return;
  }

  const checks = S.health.checks;
  const canGenerate = S.health.can_generate;
  chip.className = "health " + (S.health.ok ? "ok" : (canGenerate ? "warn" : "bad"));
  chip.querySelector("span").textContent =
    S.health.ok ? "ALL SYSTEMS READY"
    : canGenerate ? "DEGRADED" : "NOT READY";

  $("#diagRows").innerHTML = Object.entries(checks).map(([name, check]) => `
    <div class="drow">
      <span class="dot ${check.ok ? "ok" : "bad"}"></span>
      <b>${esc(name.replace(/_/g, " "))}</b>
      <span>${esc(check.detail)}</span>
    </div>`).join("");

  if (!checks.model_backend.ok) {
    $("#keyBanner").hidden = false;
    const catalogue = checks.catalog && checks.catalog.ok;
    $("#keyBannerText").textContent = catalogue
      ? "No model backend configured, so the five agents cannot run. Standard "
        + "parts still work — ask for a fastener, bearing, gear, pulley or "
        + "spring and it comes from the catalogue, exactly and instantly. "
        + "Anything custom needs a provider below."
      : "No model backend configured, so the agents cannot run. Choose a "
        + "provider below, or replay a recorded run.";
  } else {
    $("#keyBanner").hidden = true;
  }
  if (!checks.vision_render.ok) {
    const toggle = $("#optVision");
    toggle.classList.remove("on");
    toggle.setAttribute("aria-checked", "false");
    toggle.style.opacity = ".45";
    toggle.style.pointerEvents = "none";
  }
}

/* ═══════════════════════ examples ═══════════════════════ */

async function loadExamples() {
  let examples = [];
  try { examples = await API.examples(); } catch (_) { return; }
  $("#samples").innerHTML = examples.map(e => `
    <button class="sample" data-prompt="${esc(e.prompt)}">
      <b>${esc(e.id.toUpperCase())} · ${esc(e.tier.toUpperCase())}</b>
      <small>${esc(e.prompt.length > 120 ? e.prompt.slice(0, 120) + "…" : e.prompt)}</small>
    </button>`).join("");
  $$("#samples .sample").forEach(button => {
    button.onclick = () => {
      $("#prompt").value = button.dataset.prompt;
      $("#prompt").focus();
    };
  });
}

/* ═══════════════════════ token accounting ═══════════════════════ */

/* Each agent event carries the *cumulative* counters from autofab, so the
   cost of one call is the step between consecutive events. That is what
   makes a per-agent breakdown possible at all - the job record only ever
   holds the run total. */

const AGENT_NAMES = {
  plan: "planner", code: "coder", error_fix: "error refiner",
  judge: "judge", refine: "refiner",
};

function resetUsage() {
  S.usage = { seen: { input: 0, output: 0, calls: 0 }, byAgent: {} };
  const strip = $("#usage");
  if (strip) { strip.hidden = true; strip.innerHTML = ""; }
}

function noteUsage(phase, tokens) {
  if (!tokens || !S.usage) return;
  const input = tokens.input_tokens || 0;
  const output = tokens.output_tokens || 0;
  const calls = tokens.calls || 0;
  const seen = S.usage.seen;

  // Cumulative counters only ever climb; a drop means the run restarted, so
  // rebase rather than recording a negative.
  const step = {
    input: Math.max(0, input - seen.input),
    output: Math.max(0, output - seen.output),
    calls: Math.max(0, calls - seen.calls),
  };
  S.usage.seen = { input, output, calls };

  const name = AGENT_NAMES[phase] || phase;
  const entry = S.usage.byAgent[name] || { input: 0, output: 0, calls: 0 };
  entry.input += step.input;
  entry.output += step.output;
  entry.calls += step.calls;
  S.usage.byAgent[name] = entry;
  renderUsage();
}

const compact = n => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

function renderUsage() {
  const strip = $("#usage");
  if (!strip || !S.usage) return;
  const { seen, byAgent } = S.usage;
  const total = seen.input + seen.output;

  if (!total) {
    // A catalogue part costs nothing, and saying so is the point - it is
    // the difference between the two paths made visible.
    if (S.catalog) {
      strip.hidden = false;
      strip.innerHTML =
        `<span class="unone">No model call — this part came from the `
        + `catalogue, so it cost no tokens.</span>`;
    } else {
      strip.hidden = true;
    }
    return;
  }

  const order = ["planner", "coder", "error refiner", "judge", "refiner"];
  const parts = order
    .filter(name => byAgent[name] && (byAgent[name].input + byAgent[name].output))
    .map(name => {
      const a = byAgent[name];
      return `<span class="uagent"><b>${compact(a.input + a.output)}</b>`
           + `<span>${name}${a.calls > 1 ? ` ×${a.calls}` : ""}</span></span>`;
    });

  // On a metered backend the useful number is not just what this run spent
  // but how close it came to the ceiling that stops it, so show both once
  // the run is a meaningful way through its allowance.
  const cap = S.spend && S.spend.budget;
  const share = cap ? total / cap : 0;
  const budgetNote = share > 0.25
    ? `<span class="ubudget${share > 0.8 ? " near" : ""}">`
      + `${Math.round(share * 100)}% of the ${compact(cap)} budget</span>`
    : "";
  const costNote = S.spend && S.spend.estimated_cost !== undefined
    ? `<span class="ucost">≈ $${S.spend.estimated_cost.toFixed(4)}</span>` : "";

  strip.hidden = false;
  strip.innerHTML =
    `<span class="utot"><b>${total.toLocaleString()}</b> tokens · `
    + `${seen.input.toLocaleString()} in · ${seen.output.toLocaleString()} out · `
    + `${seen.calls} call${seen.calls === 1 ? "" : "s"}</span>`
    + costNote + budgetNote + parts.join("");
}

/* ═══════════════════════ pipeline progress ═══════════════════════ */

function renderStages(activeKey, detail) {
  const activeIndex = STAGES.findIndex(s => s.key === activeKey);
  $("#pipe").innerHTML = STAGES.map((stage, i) => {
    const state = i < activeIndex ? "done" : (i === activeIndex ? "act" : "");
    return `
      <div class="pstep ${state}">
        <div class="pbullet">
          <svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg><i class="pspin"></i>
        </div>
        <div>
          <div class="plabel">${stage.label}</div>
          ${i === activeIndex && detail
            ? `<div class="pdetail">${esc(detail)}</div>` : ""}
        </div>
      </div>${i < STAGES.length - 1 ? '<div class="pline"></div>' : ""}`;
  }).join("");
}

function appendLog(line) {
  const log = $("#plog");
  const row = document.createElement("div");
  row.textContent = line;
  log.appendChild(row);
  while (log.childElementCount > 60) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function showOverlay(which) {
  $("#ovEmpty").hidden = which !== "empty";
  $("#ovPipe").hidden = which !== "pipe";
  $("#ovErr").hidden = which !== "error";
  if (which !== "none") $("#minfo").hidden = true;
}

/* ═══════════════════════ event handling ═══════════════════════ */

const PHASE_STAGE = {
  plan: "plan", code: "code", execute: "execute", error_fix: "execute",
  render: "judge", judge: "judge", refine: "code",
};

function handleEvent(event) {
  const { phase, status, message, data } = event;
  S.seq = Math.max(S.seq, event.seq + 1);

  if (S.editing) { handleEditEvent(event); return; }

  if (phase === "log") { appendLog(message); return; }

  // Grounding happens before the Planner runs. Show it in the run log and on
  // the Planning stage, so what the Planner was told is visible rather than
  // being a silent prompt change.
  // A part that no agent produced must never read as evidence that the
  // agents work, so say plainly where it came from.
  if (phase === "catalog") {
    appendLog(message);
    if (data && data.part_id) {
      S.catalog = data;
      renderStages("done", `${data.title} — from the catalogue`);
      renderUsage();
      renderPlan(null);
      // No judge ran, so naming one in the Validation header would credit a
      // model that was never called.
      const backend = String(data.backend || "").toUpperCase();
      $("#judgeModelLabel").textContent = `CATALOGUE · ${backend}`;
      $("#genModelLabel").textContent = `CATALOGUE · ${backend}`;
    }
    return;
  }

  if (phase === "ground") {
    appendLog(`Standard dimensions: ${message}`);
    if (data && data.subjects && data.subjects.length) {
      renderStages("plan", `Grounded in ${data.subjects.join(", ")}`);
    }
    return;
  }

  const stage = PHASE_STAGE[phase];
  if (stage) {
    let detail = "";
    if (phase === "plan" && status === "started") detail = "Decomposing the request";
    if (phase === "code" && status === "started") detail = "Retrieving CadQuery API docs";
    if (phase === "code" && status === "ok") detail = `${data.lines} lines written`;
    if (phase === "execute" && status === "started") detail = "Running in the OCCT kernel";
    if (phase === "execute" && status === "failed") detail = "Execution failed — repairing";
    if (phase === "error_fix" && status === "started") detail = "Error Refiner is fixing the script";
    if (phase === "render" && status === "ok") detail = "Rendering three views";
    if (phase === "judge" && status === "started") detail = "Opus is inspecting the part";
    if (phase === "refine" && status === "started") detail = "Refiner is correcting the geometry";
    renderStages(stage, detail);
  }

  if (phase === "plan" && status === "ok") {
    S.designPlan = data.design_plan;
    renderPlan(data.design_plan);
  }
  if ((phase === "code" || phase === "refine" || phase === "error_fix")
      && status === "ok") {
    setCode(data.code);
  }
  if (data && data.tokens) noteUsage(phase, data.tokens);

  if (phase === "version" && status === "ok") {
    addVersion(data);
  }
  if (phase === "job") {
    if (status === "started" && data.llm) {
      setModelLabels(data.llm.generation_model, data.llm.judge_model);
    }
    if (data && data.spend) {
      // Arrives with the closing job event, after the last token event has
      // already drawn the strip - so redraw it, or the ceiling and the cost
      // estimate never appear.
      S.spend = data.spend;
      renderUsage();
    }
    if (status === "ok") finishRun(data);
    if (status === "failed") failRun(message);
  }
}

/* ═══════════════════════ versions ═══════════════════════ */

function addVersion(version) {
  const existing = S.versions.findIndex(v => v.iteration === version.iteration);
  if (existing >= 0) S.versions[existing] = version;
  else S.versions.push(version);
  renderIterations();
  selectVersion(S.versions.length - 1, { quiet: true });
}

function renderIterations() {
  if (!S.versions.length) { $("#iters").innerHTML = ""; return; }
  const cards = S.versions.map((v, i) => {
    const kind = v.source === "edit" ? "edit" : (v.passed ? "pass" : "fail");
    const label = v.source === "edit" ? `EDIT ${v.iteration}`
      : v.source === "catalog" ? "CATALOG"
      : `ITER ${v.iteration}`;
    const thumb = v.has_render
      ? `<img src="${API.artifact(S.jobId, v.iteration, "render.png")}" alt="" />`
      : "";
    return `<div class="iter ${i === S.selected ? "sel" : ""}" data-i="${i}">
        ${thumb}
        <div class="ilabel"><i class="${kind}"></i>${label}</div>
      </div>`;
  }).join("");
  const hint = S.versions.length > 1
    ? `<span class="ihint">← ${S.versions.length} attempts · click to compare</span>` : "";
  $("#iters").innerHTML = cards + hint;
  $$("#iters .iter").forEach(card => {
    card.onclick = () => selectVersion(+card.dataset.i);
  });
}

async function selectVersion(index, options) {
  const version = S.versions[index];
  if (!version) return;
  S.selected = index;
  renderIterations();

  try {
    const box = await Viewer.load(
      API.artifact(S.jobId, version.iteration, "model.stl"));
    if (!options || !options.quiet) Viewer.fit(true);
    else Viewer.fit(false);
    showOverlay("none");
    $("#minfo").hidden = false;
  } catch (error) {
    warnToast(error.message);
  }

  try {
    const response = await fetch(
      API.artifact(S.jobId, version.iteration, "code.py"));
    if (response.ok) setCode(await response.text());
  } catch (_) { /* code panel keeps its last content */ }

  renderKernelFacts(version);
  renderValidation(version);
  sheetSvg = null;
  $("#drawBtn").disabled = false;
  // A parameter patch is rebuilt by the kernel with no model call, so editing
  // stays available without an API key; the agent path reports its own need.
  // Not while a run is still going, though: versions land one at a time, and
  // each one re-ran this, so the control read as usable mid-run while
  // applyEdit refused the click - a button that looks live and does nothing.
  const canRebuild = !!(S.health && S.health.checks
                        && S.health.checks.cadquery.ok) && !S.busy;
  $("#cmdIn").disabled = !canRebuild;
  $("#applyBtn").disabled = !canRebuild;
}

/* ═══════════════════════ panels ═══════════════════════ */

function renderPlan(plan) {
  // A catalogue part has no design plan because no Planner ran. Saying so is
  // better than leaving "Planning…" on screen for a finished part.
  if (!plan && S.catalog) {
    $("#planBody").innerHTML =
      `<div class="await">No design plan — ${esc(S.catalog.title)} is `
      + `defined by ${esc(S.catalog.standard)}, so nothing had to be `
      + `worked out.</div>`;
    return;
  }
  if (!plan) return;
  const dimensions = (plan.dimensions && plan.dimensions.key_dimensions) || {};
  const bbox = (plan.dimensions && plan.dimensions.overall_bbox) || {};
  const constraints = plan.constraints || {};

  const rows = Object.entries(dimensions).map(([key, value]) => `
    <div class="dim" data-k="${esc(key)}">
      <span>${esc(key.replace(/_/g, " "))}</span>
      <b>${fmt(value)}<u>mm</u></b>
    </div>`).join("");

  const constraintTags = Object.entries(constraints)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `<span class="tag">${esc(k.replace(/_/g, " "))}: ${esc(fmt(v))}</span>`)
    .join("");

  $("#planBody").innerHTML = `
    <div class="plan-desc">${esc(plan.description || "")}</div>
    ${(plan.components || []).length ? `<div class="plan-tags">${
      plan.components.map(c => `<span class="tag">${esc(c)}</span>`).join("")
    }</div>` : ""}
    ${rows ? `<div class="eyebrow" style="margin-bottom:6px">TARGET DIMENSIONS</div>${rows}` : ""}
    ${bbox.xlen ? `<div class="dim"><span>overall bbox</span><b>${
      fmt(bbox.xlen)} × ${fmt(bbox.ylen)} × ${fmt(bbox.zlen)}<u>mm</u></b></div>` : ""}
    ${constraintTags ? `<div class="eyebrow" style="margin:12px 0 6px">CONSTRAINTS</div>
      <div class="plan-tags">${constraintTags}</div>` : ""}`;
}

function renderKernelFacts(version) {
  const geometry = version.geometry || {};
  const bbox = geometry.bounding_box || {};
  // A version the kernel measured and refused is not "not yet validated" -
  // it was validated, and it failed. Naming the measurement that failed is
  // more use than a generic label.
  const failedSpec = version.spec && version.spec.ok === false
    ? (version.spec.checks || []).find(c => !c.passed && c.hard) : null;
  $("#mtitle").textContent =
    version.source === "edit" ? "Model updated"
    : version.source === "catalog" ? "Standard part"
    : failedSpec ? `Measured wrong: ${failedSpec.label} `
        + `${failedSpec.actual}, wanted ${failedSpec.expected}`
    : (version.passed ? "Validated" : "Attempt not yet validated");
  $("#mfacts").innerHTML = [
    ["bbox mm", `${fmt(bbox.xlen)}×${fmt(bbox.ylen)}×${fmt(bbox.zlen)}`],
    ["volume mm³", fmt(Math.round(geometry.volume || 0))],
    ["faces", geometry.num_faces],
    ["edges", geometry.num_edges],
    ["solid", geometry.is_valid ? "WATERTIGHT" : "INVALID"],
  ].map(([label, value]) =>
    `<div class="fact"><b>${esc(String(value ?? "—"))}</b><span>${label}</span></div>`
  ).join("");

  const icon = $("#mIcon");
  if (icon) icon.style.color = version.passed ? "var(--valid)" : "var(--warn)";
}

function specRows(spec) {
  // The measured checks, shown whether they passed or not: the point of this
  // panel is that a number was read off the solid, not asserted about it.
  if (!spec || spec.error || !spec.checks || !spec.checks.length) return "";
  const rows = spec.checks.map((c) => {
    const state = c.passed ? "ok" : (c.hard ? "bad" : "soft");
    const mark = c.passed ? "&#10003;" : (c.hard ? "&#10007;" : "&#8210;");
    return `<div class="specrow ${state}">
      <span class="specmark">${mark}</span>
      <span class="speclabel">${esc(c.label)}</span>
      <span class="specval">${esc(c.actual)}</span>
      <span class="specwant">wanted ${esc(c.expected)}</span>
    </div>`;
  }).join("");
  return `<div class="eyebrow" style="margin:14px 0 6px">MEASURED BY THE KERNEL</div>
    <div class="specgrid">${rows}</div>`;
}

function renderValidation(version) {
  const passed = version.passed;
  const renderUrl = version.has_render
    ? API.artifact(S.jobId, version.iteration, "render.png") : null;

  // A parameter patch is confirmed by the kernel alone - the Judge is not
  // re-run for it. Saying "accepted by the Judge" there would credit a check
  // that never happened, so the two cases are worded differently.
  const judged = version.judge_passed !== null
                 && version.judge_passed !== undefined;

  const spec = version.spec || null;
  const specFailed = spec && spec.ok === false;

  let heading, body, attribution;
  if (judged) {
    if (specFailed) {
      // A measurement outranks an opinion about the same quantity, so it
      // leads whatever the Judge concluded. Crediting the Judge for a
      // rejection the kernel can prove would also contradict the status
      // bar above, which names the measurement that failed.
      const failed = (spec.checks || []).filter(c => !c.passed && c.hard);
      const named = failed.map(c => `${c.label} measured ${c.actual}, `
        + `wanted ${c.expected}`).join("; ");
      heading = "Refused on measurement";
      body = (version.judge_passed
        ? "The Judge accepted this, but the kernel disagrees: "
        : "The kernel measured this part against the plan: ") + named + ". "
        + "A measurement settles a dimension; an opinion about one does not."
        + (version.judge_passed || !version.judge_feedback ? ""
           : ` The Judge also rejected it: ${version.judge_feedback}`);
    } else {
      heading = passed ? "Accepted by the Judge" : "Rejected by the Judge";
      body = version.judge_feedback || version.feedback_text || "";
    }
    const judgeModel = (S.judgeModel || "").toUpperCase() || "JUDGE MODEL";
    attribution = judgeModel + " · " + (version.has_render
      ? "KERNEL METRICS + THREE-VIEW RENDER" : "KERNEL METRICS ONLY");
  } else if (version.source === "catalog") {
    // No agent produced this, so there is nothing for a Judge to have
    // accepted. Say where it came from instead of implying a verdict.
    const cat = version.catalog || {};
    heading = "Standard part, served from the catalogue";
    body = `${cat.title || "This part"} is defined by ${cat.standard
      || "its standard"}, so its dimensions are exact rather than estimated. `
      + "No model wrote it and no Judge assessed it. OpenCASCADE built it and "
      + "reports a valid watertight solid, and the source is parametric — "
      + "edit it like any other part.";
    attribution = "CATALOGUE · " + String(cat.backend || "cadsmith").toUpperCase()
      + " · NO MODEL CALL";
  } else {
    heading = passed ? "Rebuilt and checked by the kernel"
                     : "The kernel rejected this solid";
    body = passed
      ? "OpenCASCADE rebuilt the solid and reports it valid and watertight. "
        + "The vision Judge was not re-run: a parameter patch changes a value "
        + "the script already declares, not the design."
      : (version.feedback_text || "The rebuilt solid failed the kernel's checks.");
    attribution = "OCCT KERNEL · JUDGE NOT RE-RUN";
  }

  $("#valBody").innerHTML = `
    <div class="verdict ${passed ? "pass" : "fail"}">
      <svg class="vi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        ${passed ? '<path d="M20 6 9 17l-5-5"/>'
                 : '<path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/>'}
      </svg>
      <div>
        <b>${esc(heading)}</b>
        <p>${esc(body)}</p>
        <div class="judge-src">${esc(attribution)}</div>
      </div>
    </div>
    ${specRows(spec)}
    ${renderUrl ? `
      <div class="eyebrow" style="margin-bottom:6px">WHAT THE JUDGE SAW</div>
      <img class="rthumb" id="rthumb" src="${renderUrl}" alt="Three-view render" />
      <div class="rcap">ISOMETRIC · HIGH-ANGLE REAR · FRONT PROFILE</div>` : ""}`;

  const thumb = $("#rthumb");
  if (thumb) {
    thumb.onclick = () => {
      $("#lightboxImg").src = renderUrl;
      $("#lightbox").hidden = false;
    };
  }
}

/* ═══════════════════════ run lifecycle ═══════════════════════ */

async function generate() {
  const prompt = $("#prompt").value.trim();
  if (!prompt) { warnToast("Describe the part first."); return; }
  if (S.busy) return;

  S.busy = true;
  S.versions = [];
  S.selected = -1;
  S.spend = null;
  S.designPlan = null;
  S.converged = false;
  S.replay = false;
  S.catalog = null;
  resetUsage();
  resetPill();
  $("#genBtn").disabled = true;
  $("#iters").innerHTML = "";
  $("#plog").innerHTML = "";
  Viewer.clear();
  setCode("");
  // Everything on the right belongs to the run that is being replaced, so
  // clear it now rather than leaving the previous part's plan and verdict on
  // screen until the new ones arrive.
  $("#planBody").innerHTML =
    `<div class="await">Planning…</div>`;
  $("#valBody").innerHTML = `<div class="await">Waiting for the first attempt…</div>`;
  sheetSvg = null;
  $("#drawBtn").disabled = true;
  $("#cmdIn").disabled = true;
  $("#applyBtn").disabled = true;
  showOverlay("pipe");
  renderStages("plan", "Sending the request");

  const options = {
    max_iterations: +$("#optIters").value,
    use_vision: $("#optVision").classList.contains("on"),
    ground_dimensions: $("#optGround").classList.contains("on"),
    provider: $("#optProvider").value,
    generation_model: $("#optGenModel").value.trim(),
    judge_model: $("#optJudgeModel").value.trim(),
  };

  try {
    const job = await API.createJob(prompt, options);
    S.jobId = job.id;
    S.seq = 0;
    rememberRun(job.id);
    follow(job.id, 0);
  } catch (error) {
    S.busy = false;
    $("#genBtn").disabled = false;
    failRun(error.message);
  }
}

/* A run outlives the page: the pipeline keeps working server-side while the
   browser reloads, sleeps or navigates away. Remember which job was in
   flight so boot() can reattach to it, rather than leaving a finished run
   sitting in History looking like it vanished. sessionStorage can throw
   outright in a locked-down browser, so every touch is guarded. */
const RUNNING_KEY = "cadsmith:running";

function rememberRun(jobId) {
  try { sessionStorage.setItem(RUNNING_KEY, jobId); } catch (e) { /* fine */ }
}

function forgetRun() {
  try { sessionStorage.removeItem(RUNNING_KEY); } catch (e) { /* fine */ }
}

function rememberedRun() {
  try { return sessionStorage.getItem(RUNNING_KEY); } catch (e) { return null; }
}

function follow(jobId, fromSeq) {
  if (S.stream) S.stream.close();
  S.stream = API.stream(jobId, {
    fromSeq: fromSeq || 0,
    onEvent: handleEvent,
    onEnd: () => { S.stream = null; },
    onError: () => {
      S.busy = false;
      $("#genBtn").disabled = false;
      warnToast("Lost the connection to the run.");
    },
  });
}

function finishRun(data) {
  S.busy = false;
  forgetRun();
  S.converged = !!data.converged;
  $("#genBtn").disabled = !(S.health && S.health.can_generate);

  if (!S.versions.length) {
    failRun("The pipeline produced no usable geometry.");
    return;
  }

  selectVersion(S.versions.length - 1);
  const cost = data.tokens
    ? ` · ${(data.tokens.input_tokens + data.tokens.output_tokens).toLocaleString()} tokens`
    : "";
  const seconds = data.total_ms ? ` in ${(data.total_ms / 1000).toFixed(1)}s` : "";

  if (S.catalog || data.source === "catalog") {
    // Nothing iterated and nothing was spent, so the pipeline's wording
    // does not apply - it rendered as "Converged after undefined iterations".
    toast(`${S.catalog ? S.catalog.title : "Standard part"} — from the `
          + `catalogue${seconds}, no model call`);
  } else if (S.converged) {
    toast(`Converged after ${data.iterations} iteration${
      data.iterations === 1 ? "" : "s"}${seconds}${cost}`);
  } else {
    warnToast(`Stopped after ${data.iterations} iterations without the Judge accepting it — showing the closest attempt.`);
  }
  loadHistory();
}

function failRun(message) {
  S.busy = false;
  forgetRun();
  $("#genBtn").disabled = !(S.health && S.health.can_generate);
  $("#errTitle").textContent = "The run could not complete";
  $("#errMsg").textContent = message || "Unknown error.";
  $("#errFix").textContent = S.versions.length
    ? "An earlier attempt is still available below."
    : "Check the environment panel in the header, then try again.";
  $("#errKeep").hidden = !S.versions.length;
  showOverlay("error");
}

/* ═══════════════════════ history ═══════════════════════ */

async function loadHistory() {
  let jobs = [];
  try { jobs = await API.jobs(); } catch (_) { return; }

  $("#hlist").innerHTML = jobs.length ? jobs.map(job => {
    const when = job.created_at
      ? new Date(job.created_at * 1000).toLocaleString() : "";
    const badge = job.status === "error"
      ? `<span class="hbadge fail">FAILED</span>`
      : job.converged ? `<span class="hbadge pass">CONVERGED</span>`
      : `<span class="hbadge fail">NOT CONVERGED</span>`;
    // Provenance is stated, never implied: a replay is a recording, and a
    // fixture had its agent replies scripted rather than generated.
    const origin =
      job.source === "replay" ? `<span class="hbadge replay">REPLAY</span>`
      : job.source === "catalog" ? `<span class="hbadge fixture">CATALOG</span>`
      : job.source === "fixture" ? `<span class="hbadge fixture">FIXTURE</span>`
      : "";
    return `
      <div class="hrow">
        <button class="hitem" data-job="${esc(job.id)}">
          <div class="hnote">${esc(job.prompt.slice(0, 88))}${job.prompt.length > 88 ? "…" : ""}</div>
          <div class="hmeta">
            ${badge}${origin}
            <span class="hbadge">${job.versions.length} VER</span>
            <span class="htime">${esc(when)}</span>
          </div>
        </button>
        <button class="hreplay" data-replay="${esc(job.id)}" title="Replay this run">
          <svg viewBox="0 0 24 24"><path d="M6 4l13 8-13 8z"/></svg>
        </button>
      </div>`;
  }).join("") : `<div class="await" style="padding:14px">No runs yet.</div>`;

  $$("#hlist .hitem").forEach(item => {
    item.onclick = () => openJob(item.dataset.job);
  });
  $$("#hlist .hreplay").forEach(item => {
    item.onclick = () => startReplay(item.dataset.replay);
  });
}

async function openJob(jobId) {
  let state;
  try { state = await API.job(jobId); }
  catch (error) { warnToast(error.message); return; }

  const job = state.job;
  S.replay = job.source === "replay";
  if (S.replay) {
    $("#enginePill").textContent = "REPLAY · recorded run";
    $("#enginePill").classList.add("replaying");
  } else {
    resetPill();
  }
  S.jobId = job.id;
  S.versions = job.versions || [];
  S.selected = -1;
  S.designPlan = job.design_plan;
  S.converged = job.converged;
  S.busy = false;

  $("#prompt").value = job.prompt;
  S.seq = (state.events || []).length;
  $("#plog").innerHTML = "";
  // Replay the token accounting too: the job record keeps only the run
  // total, so the per-agent split has to be rebuilt from the events.
  resetUsage();
  S.catalog = null;
  (state.events || []).forEach(e => {
    if (e.phase === "log") appendLog(e.message);
    if (e.phase === "catalog" && e.data && e.data.part_id) S.catalog = e.data;
    if (e.data && e.data.tokens) noteUsage(e.phase, e.data.tokens);
  });
  renderUsage();

  const started = (state.events || []).find(
    e => e.phase === "job" && e.status === "started" && e.data && e.data.llm);
  if (started) {
    setModelLabels(started.data.llm.generation_model,
                   started.data.llm.judge_model);
  }

  renderPlan(job.design_plan);
  renderIterations();
  $("#hist").classList.remove("open");

  if (S.versions.length) {
    await selectVersion(S.versions.length - 1);
    toast(job.converged ? "Loaded a converged run" : "Loaded an unconverged run");
  } else {
    showOverlay("error");
    $("#errTitle").textContent = "That run produced no geometry";
    $("#errMsg").textContent = job.error || "The pipeline stopped before exporting a solid.";
    $("#errFix").textContent = "";
  }
}

/* ═══════════════════════ wiring ═══════════════════════ */

$("#genBtn").onclick = generate;
$("#prompt").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate();
});

$("#optIters").oninput = e => { $("#optItersOut").value = e.target.value; };
$("#optVision").onclick = () => {
  const on = $("#optVision").classList.toggle("on");
  $("#optVision").setAttribute("aria-checked", String(on));
};
$("#optGround").onclick = () => {
  const on = $("#optGround").classList.toggle("on");
  $("#optGround").setAttribute("aria-checked", String(on));
};

$("#healthChip").onclick = () => {
  const panel = $("#diag");
  panel.hidden = !panel.hidden;
};
document.addEventListener("click", e => {
  if (!$("#diag").hidden && !$("#diag").contains(e.target)
      && e.target.closest("#healthChip") === null) {
    $("#diag").hidden = true;
  }
});

$$(".vt[data-view]").forEach(button => {
  button.onclick = () => {
    $$(".vt[data-view]").forEach(b => b.classList.remove("on"));
    button.classList.add("on");
    $("#spinBtn").classList.remove("on");
    Viewer.view(button.dataset.view, true);
  };
});
$("#fitBtn").onclick = () => Viewer.fit(true);
$("#wireBtn").onclick = () => $("#wireBtn").classList.toggle("on", Viewer.toggleWire());
$("#spinBtn").onclick = () => {
  Viewer.spin = !Viewer.spin;
  $("#spinBtn").classList.toggle("on", Viewer.spin);
};

$("#histBtn").onclick = () => { loadHistory(); $("#hist").classList.add("open"); };
$("#histClose").onclick = () => $("#hist").classList.remove("open");

$("#copyBtn").onclick = async () => {
  const version = S.versions[S.selected];
  if (!version) return;
  const response = await fetch(API.artifact(S.jobId, version.iteration, "code.py"));
  await navigator.clipboard.writeText(await response.text());
  toast("CadQuery source copied");
};

function download(name) {
  const version = S.versions[S.selected];
  if (!version) { warnToast("Generate a part first."); return; }
  const link = document.createElement("a");
  link.href = API.artifact(S.jobId, version.iteration, name);
  link.download = "";
  link.click();
}
$("#dlPy").onclick = () => download("code.py");
$("#dlStep").onclick = () => download("model.step");
$("#dlStl").onclick = () => download("model.stl");

$("#errRetry").onclick = () => generate();
$("#errKeep").onclick = () => {
  if (S.versions.length) selectVersion(S.versions.length - 1);
};

$("#lightbox").onclick = () => { $("#lightbox").hidden = true; };

addEventListener("keydown", e => {
  if (e.target.matches("input,textarea")) return;
  const key = e.key.toLowerCase();
  if (key === "1") Viewer.view("iso", true);
  if (key === "2") Viewer.view("front", true);
  if (key === "3") Viewer.view("top", true);
  if (key === "4") Viewer.view("right", true);
  if (key === "f") Viewer.fit(true);
  if (key === "w") $("#wireBtn").click();
  if (key === "h") $("#histBtn").click();
  if (key === "escape") {
    $("#lightbox").hidden = true;
    $("#diag").hidden = true;
    $("#sheet").classList.remove("on");
    // The history drawer covers the page and swallows clicks, and its only
    // exit was a small x. Escape closes every other overlay; it should
    // close this one too.
    $("#hist").classList.remove("open");
  }
  if (key === "d") $("#drawBtn").click();
});





/* ═══════════════════════ model backend ═══════════════════════ */

/* The pipeline reaches every model through one function in autofab.agents,
   so the whole thing can be pointed at OpenAI, a local Ollama, or anything
   else speaking the same API. Keys entered here are held in server memory
   for this process only — never written to disk and never sent back. */

async function loadProviders() {
  let payload;
  try { payload = await API.providers(true); } catch (_) { return; }

  S.providers = payload.providers || [];
  const select = $("#optProvider");
  select.innerHTML = S.providers.map(p => {
    const state = p.ready ? "" : " — needs setup";
    return `<option value="${esc(p.id)}">${esc(p.label)}${state}</option>`;
  }).join("");

  const preferred = S.providers.find(p => p.id === payload.default && p.ready)
    || S.providers.find(p => p.ready)
    || S.providers[0];
  if (preferred) {
    select.value = preferred.id;
    applyProvider(preferred.id);
  }
}

function currentProvider() {
  return S.providers.find(p => p.id === $("#optProvider").value) || null;
}

function applyProvider(providerId) {
  const provider = S.providers.find(p => p.id === providerId);
  if (!provider) return;
  S.provider = provider;

  const models = provider.models || [];
  $("#genModels").innerHTML = models.map(m => `<option value="${esc(m)}">`).join("");
  $("#judgeModels").innerHTML = $("#genModels").innerHTML;

  // Use the provider's own defaults where it declares them. Where it does
  // not — a gateway offering hundreds of models — leave the field empty and
  // let the datalist suggest, rather than picking an arbitrary first entry
  // and implying it was chosen for the job.
  $("#optGenModel").value = provider.default_generation_model || "";
  $("#optJudgeModel").value = provider.default_judge_model
    || provider.default_generation_model || "";
  $("#optGenModel").placeholder = models.length
    ? `model id (${models.length} available)` : "model id";
  $("#optJudgeModel").placeholder = $("#optGenModel").placeholder;

  const needsSetup = !provider.ready;
  $("#keyRow").hidden = !(needsSetup || provider.key_from_session);
  $("#providerBase").hidden = provider.id !== "custom";
  $("#providerBase").value = provider.base_url || "";

  // Bedrock authenticates with AWS credentials, not an Anthropic key, so
  // showing a key field there would send someone hunting for a key that
  // does not exist. It needs a region; the credentials themselves come from
  // the AWS chain and never pass through this app.
  const bedrock = provider.kind === "bedrock";
  $("#awsRegion").hidden = !bedrock;
  $("#awsProfile").hidden = !bedrock;
  $("#providerKey").hidden = bedrock;
  if (bedrock && !$("#awsRegion").value) {
    $("#awsRegion").value = provider.aws_region || "";
    $("#awsProfile").value = provider.aws_profile || "";
  }
  $("#providerKey").placeholder = provider.needs_key
    ? "API key (memory only)" : "API key (not required)";

  setModelLabels($("#optGenModel").value, $("#optJudgeModel").value);
  updateProviderNote();
  updateGenerateAvailability();
}

/* Name the models that actually did the work, rather than the ones the
   pipeline happens to default to. */
function setModelLabels(generation, judge) {
  S.judgeModel = judge || "";
  const short = name => (name || "").split("/").pop().toUpperCase() || "—";
  $("#genModelLabel").textContent = `PLANNER · ${short(generation)}`;
  $("#judgeModelLabel").textContent = `JUDGE · ${short(judge)}`;
}

function updateProviderNote() {
  const provider = S.provider;
  const note = $("#providerNote");
  if (!provider) { note.textContent = ""; return; }

  const generation = $("#optGenModel").value.trim();
  const judge = $("#optJudgeModel").value.trim();

  if (!provider.ready) {
    note.className = "optnote warn";
    note.textContent = provider.hint + ".";
    return;
  }
  if (!generation || !judge) {
    note.className = "optnote warn";
    note.textContent = "Choose a model for both roles.";
    return;
  }
  if (generation === judge) {
    // The pipeline judges with a separate, stronger model on purpose.
    note.className = "optnote warn";
    note.textContent =
      "Both roles use the same model, so the Judge grades its own work. "
      + "Pick a stronger judge model for an independent check.";
    return;
  }
  note.className = "optnote ok";
  if (provider.kind === "bedrock") {
    // Say where the credentials came from: on Bedrock "ready" can mean an
    // instance role, an SSO session or a profile, and which one it picked
    // is exactly what you need when the wrong account answers.
    note.textContent =
      `Ready — ${provider.aws_region}`
      + (provider.aws_profile ? `, profile ${provider.aws_profile}` : "")
      + (provider.aws_credentials
         ? `, credentials from ${provider.aws_credentials}.` : ".");
    return;
  }
  note.textContent = provider.local
    ? "Running locally — nothing leaves this machine."
    : "Ready.";
}

function updateGenerateAvailability() {
  const provider = S.provider;
  const kernelOk = !!(S.health && S.health.checks
                      && S.health.checks.cadquery.ok);
  const ready = !!(provider && provider.ready
                   && $("#optGenModel").value.trim()
                   && $("#optJudgeModel").value.trim());
  // A standard part is answered from the catalogue with no model call, so
  // the kernel alone is enough to press Generate. A custom prompt without a
  // provider is refused by the server with a clear reason - which is better
  // than greying out the button and leaving someone to guess why.
  const catalogue = !!(S.health && S.health.checks
                       && S.health.checks.catalog
                       && S.health.checks.catalog.ok);
  $("#genBtn").disabled = !(kernelOk && (ready || catalogue));
}

async function saveProviderKey() {
  const provider = currentProvider();
  if (!provider) return;

  const button = $("#saveKeyBtn");
  button.disabled = true;
  try {
    const updated = await API.setProviderKey(
      provider.id, $("#providerKey").value.trim(),
      $("#providerBase").value.trim(),
      $("#awsRegion").value.trim(), $("#awsProfile").value.trim());
    Object.assign(provider, updated);
    $("#providerKey").value = "";
    applyProvider(provider.id);

    const select = $("#optProvider");
    const option = [...select.options].find(o => o.value === provider.id);
    if (option) option.textContent =
      provider.label + (provider.ready ? "" : " — needs setup");

    toast(provider.ready ? `${provider.label} is ready`
                         : `${provider.label} still needs setup`);
    loadHealth();
  } catch (error) {
    warnToast(error.message);
  } finally {
    button.disabled = false;
  }
}

$("#optProvider").onchange = e => applyProvider(e.target.value);
function onModelEdited() {
  setModelLabels($("#optGenModel").value, $("#optJudgeModel").value);
  updateProviderNote();
  updateGenerateAvailability();
}
$("#optGenModel").oninput = onModelEdited;
$("#optJudgeModel").oninput = onModelEdited;
$("#saveKeyBtn").onclick = saveProviderKey;
$("#providerKey").addEventListener("keydown", e => {
  if (e.key === "Enter") saveProviderKey();
});

/* ═══════════════════════ replay ═══════════════════════ */

/* A replay re-emits the events a real run produced, against the artifacts
   that run exported. Nothing is simulated; only the pacing differs, so a
   demo does not depend on the network or on a part converging this time. */
async function startReplay(sourceJobId) {
  if (S.busy) { warnToast("Something is already running."); return; }

  S.busy = true;
  S.replay = true;
  S.versions = [];
  S.selected = -1;
  S.designPlan = null;
  S.seq = 0;
  $("#hist").classList.remove("open");
  $("#iters").innerHTML = "";
  $("#plog").innerHTML = "";
  Viewer.clear();
  setCode("");
  showOverlay("pipe");
  renderStages("plan", "Replaying a recorded run");
  $("#enginePill").textContent = "REPLAY · recorded run";
  $("#enginePill").classList.add("replaying");

  try {
    const job = await API.replay(sourceJobId, 6);
    S.jobId = job.id;
    follow(job.id, 0);
  } catch (error) {
    S.busy = false;
    S.replay = false;
    resetPill();
    failRun(error.message);
  }
}

function resetPill() {
  $("#enginePill").textContent = "Planner · Coder · Executor · Validator · Refiner";
  $("#enginePill").classList.remove("replaying");
}

/* ═══════════════════════ natural-language edits ═══════════════════════ */

/* The steps mirror what actually happens. A parameter patch skips the Judge:
   it changes a number the script already declares, so the kernel alone can
   confirm it. The Refiner writes new code, so the full check is worth it. */
const EDIT_STEPS = [
  { key: "read",     label: "Reading the request" },
  { key: "apply",    label: "Applying the change" },
  { key: "rebuild",  label: "Rebuilding in the kernel" },
  { key: "validate", label: "Validating" },
  { key: "done",     label: "Updated" },
];

function renderEditSteps(activeKey, skipValidate) {
  const steps = skipValidate
    ? EDIT_STEPS.filter(s => s.key !== "validate") : EDIT_STEPS;
  const activeIndex = steps.findIndex(s => s.key === activeKey);
  $("#actSteps").innerHTML = steps.map((step, i) => {
    const state = i < activeIndex ? "done" : (i === activeIndex ? "act" : "");
    return `<div class="ast ${state}"><i></i>${step.label}</div>`
      + (i < steps.length - 1 ? `<span class="arrow">→</span>` : "");
  }).join("");
}

function handleEditEvent(event) {
  const { phase, status, message, data } = event;

  if (phase === "edit") {
    S.editMethod = data.method;
    S.editSkipValidate = data.method === "parameter patch";
    renderEditSteps("apply", S.editSkipValidate);
    if (data.changes && data.changes.length) {
      $("#actDiff").innerHTML = data.changes.map(c =>
        `<span class="diffpill">${esc(c.name.replace(/_/g, " "))}
           <span class="strike">${fmt(c.old)}</span>${fmt(c.new)}</span>`).join("");
    } else {
      $("#actDiff").innerHTML =
        `<span class="diffpill">REFINER AGENT</span>`;
    }
    return;
  }

  if (phase === "refine" && status === "started") {
    renderEditSteps("apply", false);
  }
  if (phase === "execute") {
    renderEditSteps("rebuild", S.editSkipValidate);
  }
  if (phase === "judge") {
    renderEditSteps("validate", false);
  }
  if (phase === "version" && status === "ok") {
    addVersion(data);
  }
  if ((phase === "code" || phase === "refine" || phase === "error_fix")
      && status === "ok") {
    setCode(data.code);
  }
  if (phase === "job" && status === "ok") {
    renderEditSteps("done", S.editSkipValidate);
    setTimeout(() => { $("#act").hidden = true; }, 900);
    finishEdit(true, data);
  }
  if (phase === "job" && status === "failed") {
    $("#act").hidden = true;
    finishEdit(false, data, message);
  }
}

function finishEdit(ok, data, message) {
  S.editing = false;
  S.busy = false;
  $("#applyBtn").disabled = false;
  $("#cmdIn").disabled = false;

  if (!ok) {
    warnToast(message || "The edit could not be applied.");
    return;
  }
  $("#cmdIn").value = "";
  const method = data.method === "parameter patch"
    ? "parameter patch, rebuilt by the kernel"
    : "Refiner agent";
  const seconds = data.total_ms ? ` in ${(data.total_ms / 1000).toFixed(1)}s` : "";
  toast(`Model updated · ${method}${seconds}`);
}

async function applyEdit() {
  const instruction = $("#cmdIn").value.trim();
  if (!instruction) { warnToast("Describe the change first."); return; }
  if (S.busy || !S.jobId || !S.versions.length) return;

  S.busy = true;
  S.editing = true;
  S.editSkipValidate = false;
  $("#applyBtn").disabled = true;
  $("#cmdIn").disabled = true;
  $("#actDiff").innerHTML = "";
  $("#act").hidden = false;
  renderEditSteps("read", false);

  try {
    // Edit the version on screen, which is not always the newest one.
    const base = S.versions[S.selected];
    await API.edit(S.jobId, instruction, base ? base.iteration : undefined);
    follow(S.jobId, S.seq);
  } catch (error) {
    $("#act").hidden = true;
    finishEdit(false, {}, error.message);
  }
}

$("#applyBtn").onclick = applyEdit;
$("#cmdIn").addEventListener("keydown", e => {
  if (e.key === "Enter") applyEdit();
});

/* ═══════════════════════ drawing sheet ═══════════════════════ */

let sheetSvg = null;

async function openDrawing() {
  const version = S.versions[S.selected];
  if (!version) { warnToast("Generate a part first."); return; }

  const button = $("#drawBtn");
  button.disabled = true;
  $("#paper").innerHTML = `<div style="padding:60px;color:#666;font-family:monospace;font-size:12px">Projecting the solid…</div>`;
  $("#sheet").classList.add("on");

  try {
    const url = API.artifact(S.jobId, version.iteration, "drawing.svg");
    const response = await fetch(url);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    sheetSvg = await response.text();
    $("#paper").innerHTML = sheetSvg;
  } catch (error) {
    sheetSvg = null;
    $("#paper").innerHTML =
      `<div style="padding:60px;color:#B00;font-family:monospace;font-size:12px;max-width:640px">`
      + `Could not build the drawing.<br><br>${esc(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
}

/* Rasterise the sheet in the browser. The SVG is self-contained - no external
   references - so it can be drawn straight onto a canvas. */
function exportDrawingPng() {
  if (!sheetSvg) { warnToast("Open a drawing first."); return; }
  const svg = $("#paper").querySelector("svg");
  const width = +svg.getAttribute("width") || 1120;
  const height = +svg.getAttribute("height") || 780;
  const scale = 2;

  const blob = new Blob([sheetSvg], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const image = new Image();

  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = width * scale;
    canvas.height = height * scale;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);

    canvas.toBlob(pngBlob => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(pngBlob);
      link.download = `${S.jobId}_drawing.png`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      toast("Drawing exported as PNG");
    }, "image/png");
  };
  image.onerror = () => {
    URL.revokeObjectURL(url);
    warnToast("Could not rasterise the drawing.");
  };
  image.src = url;
}

$("#drawBtn").onclick = openDrawing;
$("#back3d").onclick = () => $("#sheet").classList.remove("on");
$("#expPng").onclick = exportDrawingPng;

/* ═══════════════════════ boot ═══════════════════════ */

/* Reattach to a run that was still going when the page went away.
   The event log is append-only and replayable from a sequence number, so
   following from 0 rebuilds the whole run through the same handler a live
   run uses - no separate restore path to keep in step. */
async function resumeRun(jobId) {
  if (!jobId) return;
  let state;
  try { state = await API.job(jobId); }
  catch (error) { forgetRun(); return; }

  const job = state.job;
  if (job.finished_at) {
    // It completed while we were away. Show the result rather than the
    // stale progress overlay.
    forgetRun();
    await openJob(jobId);
    toast("That run finished while the page was away");
    return;
  }

  S.busy = true;
  S.jobId = jobId;
  S.seq = 0;
  S.versions = [];
  S.selected = -1;
  S.converged = false;
  $("#prompt").value = job.prompt;
  $("#genBtn").disabled = true;
  $("#drawBtn").disabled = true;
  $("#cmdIn").disabled = true;
  $("#applyBtn").disabled = true;
  $("#iters").innerHTML = "";
  showOverlay("pipe");
  renderStages("plan", "Reattaching to a run already in progress");
  follow(jobId, 0);
  toast("Reattached to a run still in progress");
}

(async function boot() {
  await loadHealth();
  await loadProviders();
  await loadExamples();
  await loadHistory();
  setCode("");
  Viewer.fit(false);
  await resumeRun(rememberedRun());
})();
