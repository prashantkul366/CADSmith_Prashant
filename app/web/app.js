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
const esc = s => String(s ?? "").replace(/&/g, "&amp;")
  .replace(/</g, "&lt;").replace(/>/g, "&gt;");
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

  if (!checks.api_key.ok) {
    $("#keyBanner").hidden = false;
    $("#keyBannerText").textContent =
      "No ANTHROPIC_API_KEY, so the agents cannot run. Past runs still replay in full.";
    $("#genBtn").disabled = true;
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

  if (phase === "log") { appendLog(message); return; }

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
  if (phase === "version" && status === "ok") {
    addVersion(data);
  }
  if (phase === "job") {
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
    const label = v.source === "edit" ? `EDIT ${i}` : `ITER ${v.iteration}`;
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
  $("#cmdIn").disabled = !S.health || !S.health.can_generate;
  $("#applyBtn").disabled = $("#cmdIn").disabled;
}

/* ═══════════════════════ panels ═══════════════════════ */

function renderPlan(plan) {
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
  $("#mtitle").textContent = version.source === "edit"
    ? "Model updated" : (version.passed ? "Validated" : "Attempt not yet validated");
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

function renderValidation(version) {
  const passed = version.passed;
  const feedback = version.judge_feedback || version.feedback_text || "";
  const renderUrl = version.has_render
    ? API.artifact(S.jobId, version.iteration, "render.png") : null;

  $("#valBody").innerHTML = `
    <div class="verdict ${passed ? "pass" : "fail"}">
      <svg class="vi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        ${passed ? '<path d="M20 6 9 17l-5-5"/>'
                 : '<path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/>'}
      </svg>
      <div>
        <b>${passed ? "Accepted by the Judge" : "Rejected by the Judge"}</b>
        <p>${esc(feedback)}</p>
        <div class="judge-src">CLAUDE OPUS · ${
          version.has_render ? "KERNEL METRICS + THREE-VIEW RENDER" : "KERNEL METRICS ONLY"}</div>
      </div>
    </div>
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
  S.designPlan = null;
  S.converged = false;
  S.replay = false;
  $("#genBtn").disabled = true;
  $("#iters").innerHTML = "";
  $("#plog").innerHTML = "";
  Viewer.clear();
  setCode("");
  $("#valBody").innerHTML = `<div class="await">Waiting for the first attempt…</div>`;
  showOverlay("pipe");
  renderStages("plan", "Sending the request");

  const options = {
    max_iterations: +$("#optIters").value,
    use_vision: $("#optVision").classList.contains("on"),
  };

  try {
    const job = await API.createJob(prompt, options);
    S.jobId = job.id;
    follow(job.id);
  } catch (error) {
    S.busy = false;
    $("#genBtn").disabled = false;
    failRun(error.message);
  }
}

function follow(jobId) {
  if (S.stream) S.stream.close();
  S.stream = API.stream(jobId, {
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

  if (S.converged) {
    toast(`Converged after ${data.iterations} iteration${
      data.iterations === 1 ? "" : "s"}${seconds}${cost}`);
  } else {
    warnToast(`Stopped after ${data.iterations} iterations without the Judge accepting it — showing the closest attempt.`);
  }
  loadHistory();
}

function failRun(message) {
  S.busy = false;
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
    return `
      <button class="hitem" data-job="${esc(job.id)}">
        <div class="hnote">${esc(job.prompt.slice(0, 92))}${job.prompt.length > 92 ? "…" : ""}</div>
        <div class="hmeta">
          ${badge}
          <span class="hbadge">${job.versions.length} ITER</span>
          <span class="htime">${esc(when)}</span>
        </div>
      </button>`;
  }).join("") : `<div class="await" style="padding:14px">No runs yet.</div>`;

  $$("#hlist .hitem").forEach(item => {
    item.onclick = () => openJob(item.dataset.job);
  });
}

async function openJob(jobId) {
  let state;
  try { state = await API.job(jobId); }
  catch (error) { warnToast(error.message); return; }

  const job = state.job;
  S.jobId = job.id;
  S.versions = job.versions || [];
  S.selected = -1;
  S.designPlan = job.design_plan;
  S.converged = job.converged;
  S.busy = false;

  $("#prompt").value = job.prompt;
  $("#plog").innerHTML = "";
  (state.events || [])
    .filter(e => e.phase === "log")
    .forEach(e => appendLog(e.message));

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
  }
  if (key === "d") $("#drawBtn").click();
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

(async function boot() {
  await loadHealth();
  await loadExamples();
  await loadHistory();
  setCode("");
  Viewer.fit(false);
})();
