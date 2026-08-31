"use strict";
/* ═══════════════════════════════════════════════════════════════════════
   API client — every call hits the real CADSmith backend.
   There is no mock path here; if the server cannot do something it says so.
   ═══════════════════════════════════════════════════════════════════════ */

const API = (() => {

  async function json(url, options) {
    const response = await fetch(url, options);
    let body = null;
    try { body = await response.json(); } catch (_) { /* empty or non-JSON */ }
    if (!response.ok) {
      const detail = (body && (body.detail || body.message)) || response.statusText;
      const error = new Error(detail || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  return {
    health:   () => json("/api/health"),
    examples: () => json("/api/examples").then(d => d.examples || []),
    jobs:     () => json("/api/jobs").then(d => d.jobs || []),
    providers: (withModels) =>
      json(`/api/providers${withModels ? "?models=true" : ""}`),

    /* The key is posted once and held in server memory. It is never stored,
       never logged, and never sent back — the reply says only whether the
       provider is usable now. */
    setProviderKey(providerId, apiKey, baseUrl, awsRegion, awsProfile) {
      return json(`/api/providers/${encodeURIComponent(providerId)}/key`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey, base_url: baseUrl,
          aws_region: awsRegion || "", aws_profile: awsProfile || "",
        }),
      }).then(d => d.provider);
    },
    job:      id => json(`/api/jobs/${encodeURIComponent(id)}`),

    createJob(prompt, options) {
      return json("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, options }),
      }).then(d => d.job);
    },

    edit(jobId, instruction, version) {
      return json(`/api/jobs/${encodeURIComponent(jobId)}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction, version }),
      });
    },

    replay(jobId, speed) {
      return json(`/api/jobs/${encodeURIComponent(jobId)}/replay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ speed: speed || 1 }),
      }).then(d => d.job);
    },

    artifact(jobId, version, name) {
      return `/api/jobs/${encodeURIComponent(jobId)}/v/${version}/${name}`;
    },

    /* Follow a job's event stream.
       EventSource replays from the beginning and, on a dropped connection,
       resumes from the last id it saw — so a reload mid-run loses nothing. */
    stream(jobId, { onEvent, onEnd, onError, fromSeq }) {
      // fromSeq skips events already applied, so following a job again after
      // an edit does not replay the original run.
      const source = new EventSource(
        `/api/jobs/${encodeURIComponent(jobId)}/events?from_seq=${fromSeq || 0}`);

      source.onmessage = e => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { return; }
        if (payload && payload.phase && onEvent) onEvent(payload);
      };
      source.addEventListener("end", () => { source.close(); if (onEnd) onEnd(); });
      source.onerror = () => {
        // EventSource reconnects on its own; only a closed socket is fatal.
        if (source.readyState === EventSource.CLOSED && onError) onError();
      };
      return { close: () => source.close() };
    },
  };
})();
