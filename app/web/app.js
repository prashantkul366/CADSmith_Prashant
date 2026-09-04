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
/* Keys, not labels: the strip is redrawn from these on every event and on
   every language change, so the text has to be looked up at draw time. */
const STAGES = ["plan", "code", "execute", "judge", "done"];

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
  S.codeLines = code ? code.split("\n").length : 0;
  $("#codeStat").textContent = S.codeLines
    ? t("code.stat", { n: S.codeLines }) : t("code.empty");
}

/* ═══════════════════════ health ═══════════════════════ */

async function loadHealth() {
  const chip = $("#healthChip");
  try {
    S.health = await API.health();
  } catch (_) {
    chip.className = "health bad";
    chip.querySelector("span").textContent = t("health.unreachable");
    return;
  }

  const checks = S.health.checks;
  const canGenerate = S.health.can_generate;
  chip.className = "health " + (S.health.ok ? "ok" : (canGenerate ? "warn" : "bad"));
  chip.querySelector("span").textContent =
    S.health.ok ? t("health.ready")
    : canGenerate ? t("health.degraded") : t("health.notready");

  $("#diagRows").innerHTML = Object.entries(checks).map(([name, check]) => {
    // The row name is interface text; the detail beside it quotes the
    // environment - a version string, a package name, a certificate path -
    // and is shown exactly as reported. The backend row is the exception:
    // it is a sentence about what to do next, so it is translated.
    const label = I18N.has("diag." + name)
      ? t("diag." + name) : name.replace(/_/g, " ");
    const detail = (name === "model_backend" && !check.ok)
      ? t("banner.backend") : check.detail;
    return `
    <div class="drow">
      <span class="dot ${check.ok ? "ok" : "bad"}"></span>
      <b>${esc(label)}</b>
      <span>${esc(detail)}</span>
    </div>`;
  }).join("");

  if (!checks.model_backend.ok) {
    $("#keyBanner").hidden = false;
    const catalogue = checks.catalog && checks.catalog.ok;
    $("#keyBannerText").textContent =
      t(catalogue ? "banner.catalog" : "banner.nobackend");
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
  try { S.examples = await API.examples(); } catch (_) { return; }
  renderExamples();
}

/* The prompt a sample inserts is the prompt it shows. Where a translation
   exists it is a term-for-term rendering of the same benchmark entry - every
   dimension and axis carried across - so the part is the same part in either
   language. Where one does not, the English prompt is shown unchanged rather
   than a machine rendering of it. */
function samplePrompt(example) {
  const key = "sample." + example.id;
  return I18N.has(key) ? t(key) : example.prompt;
}

function renderExamples() {
  const examples = S.examples || [];
  $("#samples").innerHTML = examples.map(e => {
    const prompt = samplePrompt(e);
    const tier = String(e.tier || "").toLowerCase() === "demo"
      ? t("tier.demo") : String(e.tier || "").toUpperCase();
    return `
    <button class="sample" data-prompt="${esc(prompt)}">
      <b>${esc(e.id.toUpperCase())} · ${esc(tier)}</b>
      <small>${esc(prompt.length > 120 ? prompt.slice(0, 120) + "…" : prompt)}</small>
    </button>`;
  }).join("");
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

/* Canonical ids, translated only when drawn. The counters are keyed by id,
   so a language change re-labels the strip without losing the split. */
const AGENT_IDS = {
  plan: "planner", code: "coder", error_fix: "errorfix",
  judge: "judge", refine: "refiner",
};
const AGENT_ORDER = ["planner", "coder", "errorfix", "judge", "refiner"];
const agentLabel = id => I18N.has("agent." + id) ? t("agent." + id) : id;

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

  const name = AGENT_IDS[phase] || phase;
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
      strip.innerHTML = `<span class="unone">${esc(t("usage.free"))}</span>`;
    } else {
      strip.hidden = true;
    }
    return;
  }

  const parts = AGENT_ORDER
    .filter(name => byAgent[name] && (byAgent[name].input + byAgent[name].output))
    .map(name => {
      const a = byAgent[name];
      return `<span class="uagent"><b>${compact(a.input + a.output)}</b>`
           + `<span>${esc(agentLabel(name))}${a.calls > 1 ? ` ×${a.calls}` : ""}</span></span>`;
    });

  // On a metered backend the useful number is not just what this run spent
  // but how close it came to the ceiling that stops it, so show both once
  // the run is a meaningful way through its allowance.
  const cap = S.spend && S.spend.budget;
  const share = cap ? total / cap : 0;
  const budgetNote = share > 0.25
    ? `<span class="ubudget${share > 0.8 ? " near" : ""}">`
      + `${esc(t("usage.budget", { pct: Math.round(share * 100),
                                  cap: compact(cap) }))}</span>`
    : "";
  const costNote = S.spend && S.spend.estimated_cost !== undefined
    ? `<span class="ucost">≈ $${S.spend.estimated_cost.toFixed(4)}</span>` : "";

  strip.hidden = false;
  strip.innerHTML =
    `<span class="utot">${esc(t("usage.total", {
        total: total.toLocaleString(),
        in: seen.input.toLocaleString(),
        out: seen.output.toLocaleString(),
        calls: I18N.plural("usage.calls", "usage.calls.pl", seen.calls),
      }))}</span>`
    + costNote + budgetNote + parts.join("");
}

/* ═══════════════════════ pipeline progress ═══════════════════════ */

function renderStages(activeKey, detail) {
  S.stage = { key: activeKey, detail: detail || "" };
  const activeIndex = STAGES.indexOf(activeKey);
  $("#pipe").innerHTML = STAGES.map((stage, i) => {
    const state = i < activeIndex ? "done" : (i === activeIndex ? "act" : "");
    return `
      <div class="pstep ${state}">
        <div class="pbullet">
          <svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg><i class="pspin"></i>
        </div>
        <div>
          <div class="plabel">${esc(t("stage." + stage))}</div>
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
      renderStages("done", t("detail.fromcatalog", { title: data.title }));
      renderUsage();
      renderPlan(null);
      // No judge ran, so naming one in the Validation header would credit a
      // model that was never called.
      const backend = String(data.backend || "").toUpperCase();
      const label = t("label.catalog", { backend: backend });
      $("#judgeModelLabel").textContent = label;
      $("#genModelLabel").textContent = label;
    }
    return;
  }

  if (phase === "ground") {
    appendLog(t("log.grounding", { message: message }));
    if (data && data.subjects && data.subjects.length) {
      renderStages("plan",
                   t("detail.grounded", { subjects: data.subjects.join(", ") }));
    }
    return;
  }

  const stage = PHASE_STAGE[phase];
  if (stage) {
    let detail = "";
    if (phase === "plan" && status === "started") detail = t("detail.decompose");
    if (phase === "code" && status === "started") detail = t("detail.apidocs");
    if (phase === "code" && status === "ok") detail = t("detail.lines", { n: data.lines });
    if (phase === "execute" && status === "started") detail = t("detail.kernel");
    if (phase === "execute" && status === "failed") detail = t("detail.execfail");
    if (phase === "error_fix" && status === "started") detail = t("detail.errorfix");
    if (phase === "render" && status === "ok") detail = t("detail.render");
    if (phase === "judge" && status === "started") detail = t("detail.judging");
    if (phase === "refine" && status === "started") detail = t("detail.refining");
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
    const label = v.source === "edit" ? t("iter.edit", { n: v.iteration })
      : v.source === "catalog" ? t("iter.catalog")
      : t("iter.iteration", { n: v.iteration });
    const thumb = v.has_render
      ? `<img src="${API.artifact(S.jobId, v.iteration, "render.png")}" alt="" />`
      : "";
    return `<div class="iter ${i === S.selected ? "sel" : ""}" data-i="${i}">
        ${thumb}
        <div class="ilabel"><i class="${kind}"></i>${label}</div>
      </div>`;
  }).join("");
  const hint = S.versions.length > 1
    ? `<span class="ihint">${esc(t("iter.compare", { n: S.versions.length }))}</span>`
    : "";
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
    $("#planBody").innerHTML = `<div class="await">${esc(t("plan.catalog", {
      title: S.catalog.title, standard: S.catalog.standard }))}</div>`;
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
    ${rows ? `<div class="eyebrow" style="margin-bottom:6px">${
      esc(t("plan.dimensions"))}</div>${rows}` : ""}
    ${bbox.xlen ? `<div class="dim"><span>${esc(t("plan.bbox"))}</span><b>${
      fmt(bbox.xlen)} × ${fmt(bbox.ylen)} × ${fmt(bbox.zlen)}<u>mm</u></b></div>` : ""}
    ${constraintTags ? `<div class="eyebrow" style="margin:12px 0 6px">${
      esc(t("plan.constraints"))}</div>
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
    version.source === "edit" ? t("facts.updated")
    : version.source === "catalog" ? t("facts.standard")
    : failedSpec ? t("facts.wrong", { label: specLabel(failedSpec),
                                      actual: failedSpec.actual,
                                      expected: failedSpec.expected })
    : t(version.passed ? "facts.validated" : "facts.unvalidated");
  $("#mfacts").innerHTML = [
    [t("facts.bbox"), `${fmt(bbox.xlen)}×${fmt(bbox.ylen)}×${fmt(bbox.zlen)}`],
    [t("facts.volume"), fmt(Math.round(geometry.volume || 0))],
    [t("facts.faces"), geometry.num_faces],
    [t("facts.edges"), geometry.num_edges],
    [t("facts.solid"), t(geometry.is_valid ? "facts.watertight" : "facts.invalid")],
  ].map(([label, value]) =>
    `<div class="fact"><b>${esc(String(value ?? "—"))}</b><span>${esc(label)}</span></div>`
  ).join("");

  const icon = $("#mIcon");
  if (icon) icon.style.color = version.passed ? "var(--valid)" : "var(--warn)";
}

/* A measured check names itself by a stable key, so its label is looked up
   here rather than taken from the server's English. The server text is the
   fallback: a check this build has never seen still reads as something. */
function specLabel(check) {
  const advisory = check.hard === false;
  const key = "spec." + check.key + (advisory ? ".advisory" : "");
  if (I18N.has(key)) return t(key);
  if (I18N.has("spec." + check.key)) return t("spec." + check.key);
  return check.label;
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
      <span class="speclabel">${esc(specLabel(c))}</span>
      <span class="specval">${esc(c.actual)}</span>
      <span class="specwant">${esc(t("spec.wanted", { expected: c.expected }))}</span>
    </div>`;
  }).join("");
  return `<div class="eyebrow" style="margin:14px 0 6px">${esc(t("spec.heading"))}</div>
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
      const named = failed.map(c => t("val.refused.item", {
        label: specLabel(c), actual: c.actual, expected: c.expected })).join("; ");
      heading = t("val.refused");
      body = t(version.judge_passed ? "val.refused.judgepassed"
                                    : "val.refused.measured")
        + named + t("val.refused.tail")
        + (version.judge_passed || !version.judge_feedback ? ""
           : t("val.refused.judgetoo", { feedback: version.judge_feedback }));
    } else {
      heading = t(passed ? "val.accepted" : "val.rejected");
      body = version.judge_feedback || version.feedback_text || "";
    }
    const judgeModel = (S.judgeModel || "").toUpperCase() || t("val.src.judge");
    attribution = judgeModel + " · "
      + t(version.has_render ? "val.src.render" : "val.src.metrics");
  } else if (version.source === "catalog") {
    // No agent produced this, so there is nothing for a Judge to have
    // accepted. Say where it came from instead of implying a verdict.
    const cat = version.catalog || {};
    heading = t("val.catalog.heading");
    body = t("val.catalog.body", {
      title: cat.title || t("val.catalog.thispart"),
      standard: cat.standard || t("val.catalog.itsstandard") });
    attribution = t("val.src.catalog", {
      backend: String(cat.backend || "cadsmith").toUpperCase() });
  } else {
    heading = t(passed ? "val.rebuilt" : "val.rebuilt.failed");
    body = passed ? t("val.rebuilt.body")
                  : (version.feedback_text || t("val.rebuilt.body.failed"));
    attribution = t("val.src.kernel");
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
      <div class="eyebrow" style="margin-bottom:6px">${esc(t("val.sawheading"))}</div>
      <img class="rthumb" id="rthumb" src="${renderUrl}" alt="${esc(t("val.renderalt"))}" />
      <div class="rcap">${esc(t("val.sawcaption"))}</div>` : ""}`;

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
  if (!prompt) { warnToast(t("run.needprompt")); return; }
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
    `<div class="await" data-i18n="plan.planning">${esc(t("plan.planning"))}</div>`;
  $("#valBody").innerHTML =
    `<div class="await" data-i18n="val.waiting">${esc(t("val.waiting"))}</div>`;
  sheetSvg = null;
  $("#drawBtn").disabled = true;
  $("#cmdIn").disabled = true;
  $("#applyBtn").disabled = true;
  showOverlay("pipe");
  renderStages("plan", t("detail.sending"));

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
      warnToast(t("run.lostconn"));
    },
  });
}

function finishRun(data) {
  S.busy = false;
  forgetRun();
  S.converged = !!data.converged;
  $("#genBtn").disabled = !(S.health && S.health.can_generate);

  if (!S.versions.length) {
    failRun(t("run.nogeometry"));
    return;
  }

  selectVersion(S.versions.length - 1);
  const cost = data.tokens
    ? t("run.tokens", { n: (data.tokens.input_tokens
                            + data.tokens.output_tokens).toLocaleString() })
    : "";
  const seconds = data.total_ms
    ? t("run.seconds", { s: (data.total_ms / 1000).toFixed(1) }) : "";

  if (S.catalog || data.source === "catalog") {
    // Nothing iterated and nothing was spent, so the pipeline's wording
    // does not apply - it rendered as "Converged after undefined iterations".
    toast(t("run.catalogdone", {
      title: S.catalog ? S.catalog.title : t("run.standardpart"),
      seconds: seconds }));
  } else if (S.converged) {
    toast(t("run.converged", {
      n: data.iterations, s: data.iterations === 1 ? "" : "s",
      seconds: seconds, cost: cost }));
  } else {
    warnToast(t("run.notconverged", { n: data.iterations }));
  }
  loadHistory();
}

function failRun(message) {
  S.busy = false;
  forgetRun();
  $("#genBtn").disabled = !(S.health && S.health.can_generate);
  $("#errTitle").textContent = t("err.title");
  $("#errMsg").textContent = message || t("err.unknown");
  $("#errFix").textContent =
    t(S.versions.length ? "err.haveattempt" : "err.checkenv");
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
      ? `<span class="hbadge fail">${esc(t("hist.failed"))}</span>`
      : job.converged ? `<span class="hbadge pass">${esc(t("hist.converged"))}</span>`
      : `<span class="hbadge fail">${esc(t("hist.notconverged"))}</span>`;
    // Provenance is stated, never implied: a replay is a recording, and a
    // fixture had its agent replies scripted rather than generated.
    const origin =
      job.source === "replay" ? `<span class="hbadge replay">${esc(t("hist.replay"))}</span>`
      : job.source === "catalog" ? `<span class="hbadge fixture">${esc(t("hist.catalog"))}</span>`
      : job.source === "fixture" ? `<span class="hbadge fixture">${esc(t("hist.fixture"))}</span>`
      : "";
    return `
      <div class="hrow">
        <button class="hitem" data-job="${esc(job.id)}">
          <div class="hnote">${esc(job.prompt.slice(0, 88))}${job.prompt.length > 88 ? "…" : ""}</div>
          <div class="hmeta">
            ${badge}${origin}
            <span class="hbadge">${esc(t("hist.versions", { n: job.versions.length }))}</span>
            <span class="htime">${esc(when)}</span>
          </div>
        </button>
        <button class="hreplay" data-replay="${esc(job.id)}" title="${esc(t("hist.replaytip"))}">
          <svg viewBox="0 0 24 24"><path d="M6 4l13 8-13 8z"/></svg>
        </button>
      </div>`;
  }).join("") : `<div class="await" style="padding:14px">${esc(t("hist.empty"))}</div>`;

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
    setReplayPill();
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
    toast(t(job.converged ? "hist.loaded.converged"
                          : "hist.loaded.unconverged"));
  } else {
    showOverlay("error");
    $("#errTitle").textContent = t("hist.nogeometry");
    $("#errMsg").textContent = job.error || t("hist.stoppedearly");
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
  toast(t("code.copied"));
};

function download(name) {
  const version = S.versions[S.selected];
  if (!version) { warnToast(t("draw.needpart")); return; }
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
  renderProviderOptions();
  const select = $("#optProvider");

  const preferred = S.providers.find(p => p.id === payload.default && p.ready)
    || S.providers.find(p => p.ready)
    || S.providers[0];
  if (preferred) {
    select.value = preferred.id;
    applyProvider(preferred.id);
  }
}

function renderProviderOptions() {
  const select = $("#optProvider");
  const chosen = select.value;
  select.innerHTML = S.providers.map(p =>
    `<option value="${esc(p.id)}">${esc(p.label)}${
      p.ready ? "" : esc(t("prov.needssetup"))}</option>`).join("");
  if (chosen) select.value = chosen;
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
    ? t("ph.modelid.count", { n: models.length }) : t("ph.modelid");
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
  $("#providerKey").placeholder =
    t(provider.needs_key ? "ph.apikey.memory" : "ph.apikey.none");

  setModelLabels($("#optGenModel").value, $("#optJudgeModel").value);
  updateProviderNote();
  updateGenerateAvailability();
}

/* Name the models that actually did the work, rather than the ones the
   pipeline happens to default to. */
function setModelLabels(generation, judge) {
  S.judgeModel = judge || "";
  const short = name => (name || "").split("/").pop().toUpperCase() || "—";
  $("#genModelLabel").textContent =
    t("label.planner.model", { model: short(generation) });
  $("#judgeModelLabel").textContent =
    t("label.judge.model", { model: short(judge) });
}

/* The provider block's text, without touching its values. applyProvider()
   would do this too, but it also resets the model fields to the provider's
   defaults - which on a language change would silently discard a model id
   someone had typed. */
function refreshProviderText() {
  const provider = S.provider;
  if (!provider) return;
  const models = provider.models || [];
  $("#optGenModel").placeholder = models.length
    ? t("ph.modelid.count", { n: models.length }) : t("ph.modelid");
  $("#optJudgeModel").placeholder = $("#optGenModel").placeholder;
  $("#providerKey").placeholder =
    t(provider.needs_key ? "ph.apikey.memory" : "ph.apikey.none");
  updateProviderNote();
}

/* What a provider that is not ready needs, in the interface's language.
   The server sends the sentence in English and a key beside it; a key this
   build does not know falls back to the sentence, so a provider added later
   still explains itself. */
function providerHint(provider) {
  const key = provider.hint_key;
  const params = provider.hint_params || {};
  if (key && I18N.has(key)) {
    const text = t(key, params);
    // The unreachable case is a prefix - the local server's own instruction
    // still has to follow it.
    if (key === "prov.hint.unreachable") {
      const own = "prov.hint." + (params.id || "");
      return text + (I18N.has(own) ? " " + t(own) : " " + provider.hint);
    }
    return text;
  }
  return provider.hint + ".";
}

function updateProviderNote() {
  const provider = S.provider;
  const note = $("#providerNote");
  if (!provider) { note.textContent = ""; return; }

  const generation = $("#optGenModel").value.trim();
  const judge = $("#optJudgeModel").value.trim();

  if (!provider.ready) {
    note.className = "optnote warn";
    note.textContent = providerHint(provider);
    return;
  }
  if (!generation || !judge) {
    note.className = "optnote warn";
    note.textContent = t("prov.bothroles");
    return;
  }
  if (generation === judge) {
    // The pipeline judges with a separate, stronger model on purpose.
    note.className = "optnote warn";
    note.textContent = t("prov.samemodel");
    return;
  }
  note.className = "optnote ok";
  if (provider.kind === "bedrock") {
    // Say where the credentials came from: on Bedrock "ready" can mean an
    // instance role, an SSO session or a profile, and which one it picked
    // is exactly what you need when the wrong account answers.
    note.textContent = t("prov.bedrock", {
      region: provider.aws_region,
      profile: provider.aws_profile
        ? t("prov.bedrock.profile", { profile: provider.aws_profile }) : "",
      credentials: provider.aws_credentials
        ? t("prov.bedrock.creds", { source: provider.aws_credentials }) : ".",
    });
    return;
  }
  note.textContent = t(provider.local ? "prov.local" : "prov.ready");
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

    renderProviderOptions();

    toast(t(provider.ready ? "prov.isready" : "prov.stillneeds",
            { label: provider.label }));
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
  if (S.busy) { warnToast(t("run.busy")); return; }

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
  renderStages("plan", t("detail.replaying"));
  setReplayPill();

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

function setReplayPill() {
  const pill = $("#enginePill");
  pill.textContent = t("hist.replaypill");
  pill.setAttribute("data-i18n", "hist.replaypill");
  pill.classList.add("replaying");
}

function resetPill() {
  const pill = $("#enginePill");
  pill.textContent = t("app.engine");
  pill.setAttribute("data-i18n", "app.engine");
  pill.classList.remove("replaying");
}

/* ═══════════════════════ natural-language edits ═══════════════════════ */

/* The steps mirror what actually happens. A parameter patch skips the Judge:
   it changes a number the script already declares, so the kernel alone can
   confirm it. The Refiner writes new code, so the full check is worth it. */
const EDIT_STEPS = ["read", "apply", "rebuild", "validate", "done"];

function renderEditSteps(activeKey, skipValidate) {
  const steps = skipValidate
    ? EDIT_STEPS.filter(key => key !== "validate") : EDIT_STEPS;
  const activeIndex = steps.indexOf(activeKey);
  $("#actSteps").innerHTML = steps.map((step, i) => {
    const state = i < activeIndex ? "done" : (i === activeIndex ? "act" : "");
    return `<div class="ast ${state}"><i></i>${esc(t("edit.step." + step))}</div>`
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
        `<span class="diffpill">${esc(t("edit.refineragent"))}</span>`;
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
    warnToast(message || t("edit.failed"));
    return;
  }
  $("#cmdIn").value = "";
  const method = t(data.method === "parameter patch"
                   ? "edit.method.patch" : "edit.method.agent");
  const seconds = data.total_ms
    ? t("run.seconds", { s: (data.total_ms / 1000).toFixed(1) }) : "";
  toast(t("edit.done", { method: method, seconds: seconds }));
}

async function applyEdit() {
  const instruction = $("#cmdIn").value.trim();
  if (!instruction) { warnToast(t("edit.needinstruction")); return; }
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
  if (!version) { warnToast(t("draw.needpart")); return; }

  const button = $("#drawBtn");
  button.disabled = true;
  $("#paper").innerHTML = `<div style="padding:60px;color:#666;font-family:monospace;font-size:12px">${esc(t("draw.projecting"))}</div>`;
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
      + `${esc(t("draw.failed"))}<br><br>${esc(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
}

/* Rasterise the sheet in the browser. The SVG is self-contained - no external
   references - so it can be drawn straight onto a canvas. */
function exportDrawingPng() {
  if (!sheetSvg) { warnToast(t("draw.needdrawing")); return; }
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
      toast(t("draw.exported"));
    }, "image/png");
  };
  image.onerror = () => {
    URL.revokeObjectURL(url);
    warnToast(t("draw.rasterfail"));
  };
  image.src = url;
}

$("#drawBtn").onclick = openDrawing;
$("#back3d").onclick = () => $("#sheet").classList.remove("on");
$("#expPng").onclick = exportDrawingPng;

/* ═══════════════════════ reattaching ═══════════════════════ */

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
    toast(t("run.finishedaway"));
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
  renderStages("plan", t("detail.reattach"));
  follow(jobId, 0);
  toast(t("run.reattached"));
}

/* ═══════════════════════ interface language ═══════════════════════ */

/* Two buttons, each written in its own language, so someone who cannot read
   the current interface can still find their way out of it. */
function buildLangSwitch() {
  const box = $("#langSw");
  box.setAttribute("aria-label", t("app.language"));
  box.innerHTML = I18N.LANGS.map(l =>
    `<button class="lang${l.code === I18N.current ? " on" : ""}" `
    + `data-lang="${esc(l.code)}" lang="${esc(l.code)}" `
    + `aria-pressed="${l.code === I18N.current}">${esc(l.label)}</button>`
  ).join("");
  $$("#langSw .lang").forEach(button => {
    button.onclick = () => I18N.set(button.dataset.lang);
  });
}

/* Everything on screen that was written by JavaScript rather than by the
   markup. I18N.apply() has already redrawn the static text by the time this
   runs; these are the panels that hold state, and they are redrawn from that
   state rather than from the DOM, so nothing is translated twice. */
function relocalise() {
  buildLangSwitch();

  const chip = $("#healthChip").querySelector("span");
  if (!S.health) chip.textContent = t("health.unreachable");
  else chip.textContent = S.health.ok ? t("health.ready")
    : S.health.can_generate ? t("health.degraded") : t("health.notready");
  if (S.health && S.health.checks && !S.health.checks.model_backend.ok) {
    const catalogue = S.health.checks.catalog && S.health.checks.catalog.ok;
    $("#keyBannerText").textContent =
      t(catalogue ? "banner.catalog" : "banner.nobackend");
  }

  renderExamples();
  if (S.providers.length) {
    renderProviderOptions();
    refreshProviderText();
  }
  if (S.codeLines !== undefined) {
    $("#codeStat").textContent = S.codeLines
      ? t("code.stat", { n: S.codeLines }) : t("code.empty");
  }
  if (S.stage) renderStages(S.stage.key, S.stage.detail);
  renderUsage();
  renderIterations();

  // A catalogue part names its source in both model labels, and applyProvider
  // has just put the model ids back there. Restore what actually built it.
  if (S.catalog) {
    const label = t("label.catalog",
                    { backend: String(S.catalog.backend || "").toUpperCase() });
    $("#genModelLabel").textContent = label;
    $("#judgeModelLabel").textContent = label;
  }

  if (S.designPlan || S.catalog) renderPlan(S.designPlan);
  const version = S.versions[S.selected];
  if (version) { renderKernelFacts(version); renderValidation(version); }
  if ($("#hist").classList.contains("open")) loadHistory();
}

I18N.onChange(relocalise);

/* ═══════════════════════ boot ═══════════════════════ */

(async function boot() {
  I18N.apply();
  buildLangSwitch();
  await loadHealth();
  await loadProviders();
  await loadExamples();
  await loadHistory();
  setCode("");
  Viewer.fit(false);
  await resumeRun(rememberedRun());
})();
