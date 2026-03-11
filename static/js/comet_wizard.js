"use strict";

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  directory:       "",
  allFiles:        [],   // [{path, filename, date_obs, exptime, nsubs, thumb_url, sessionIdx}]
  sessions:        [],   // [{label, indices:[]}]
  rejected:        new Set(),
  lastClickedIdx:  null, // for shift+click range
  jobId:           null,
  pollTimer:       null,
  elapsedTimer:    null,
  renderStartTime: null,
  logLines:        0,
  refPath:         null, // path of the reference frame (highest nsubs) for preview
  previewDebounce: null,
  nucleusHint:     null, // {x, y} fractional (0..1) — user-corrected nucleus position
  frameRejections: new Set(), // set of frame indices (numbers) rejected in the frame browser
  frameDir:        null,      // dir used by the frame browser (for saving rejections)
};

// ── Session detection ─────────────────────────────────────────────────────────

const SESSION_GAP_MS = 5 * 3600 * 1000;   // 5-hour gap = new session

function detectSessions(files) {
  const sessions = [];
  let current    = [];
  files.forEach((f, i) => {
    if (i === 0) {
      current.push(i);
    } else {
      const prev = new Date(files[i - 1].date_obs);
      const curr = new Date(f.date_obs);
      if ((curr - prev) > SESSION_GAP_MS) {
        sessions.push(current);
        current = [i];
      } else {
        current.push(i);
      }
    }
    f.sessionIdx = sessions.length; // 0-based while building
  });
  if (current.length) sessions.push(current);

  // Re-label sessionIdx now that we know the final count
  sessions.forEach((indices, si) => indices.forEach(i => { files[i].sessionIdx = si; }));

  return sessions.map((indices, si) => {
    const first = files[indices[0]].date_obs;
    // Format as "Oct 06" from ISO date
    const d    = new Date(first);
    const mon  = d.toLocaleString("en-US", {month: "short", timeZone: "UTC"});
    const day  = String(d.getUTCDate()).padStart(2, "0");
    return { label: `Night ${si + 1} · ${mon} ${day}`, indices };
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const $  = id => document.getElementById(id);
const qs = sel => document.querySelector(sel);

function showSection(n) {
  document.querySelectorAll(".wizard-section").forEach(s => s.classList.remove("active"));
  $(`step-${n}`).classList.add("active");
  document.querySelectorAll(".wizard-step").forEach((dot, i) => {
    dot.classList.toggle("active",   i + 1 === n);
    dot.classList.toggle("complete", i + 1 <  n);
  });
}

function fmtDate(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

function updateBulkSummary() {
  const total = state.allFiles.length;
  const kept  = total - state.rejected.size;
  $("bulk-summary").textContent = `${kept} of ${total} selected`;
  $("to-step-2").disabled = kept < 2;
}

// ── Step 1: Comet discovery ───────────────────────────────────────────────────

$("discover-btn").addEventListener("click", () => loadDiscovery());

async function loadDiscovery() {
  const root   = $("discover-root").value.trim() || $("discover-root").placeholder;
  const picker = $("comet-picker");
  picker.innerHTML = '<span class="picker-loading">Scanning…</span>';
  picker.style.display = "block";

  try {
    const res  = await fetch(`/api/comet/discover?root=${encodeURIComponent(root)}`);
    const data = await res.json();
    if (!res.ok) {
      picker.innerHTML = `<span class="picker-error">${data.error}</span>`;
      return;
    }
    if (!data.comets.length) {
      picker.innerHTML = '<span class="picker-empty">No comet directories found under this root.</span>';
      return;
    }
    picker.innerHTML = "";
    data.comets.forEach(c => {
      const row = document.createElement("button");
      row.className = "comet-pick-row";
      row.innerHTML =
        `<span class="pick-name">${c.name}</span>` +
        `<span class="pick-meta">${c.fits_count > 0 ? c.fits_count + " subs" : "no subs"}</span>`;
      row.addEventListener("click", () => {
        $("comet-dir").value = c.path;
        picker.style.display = "none";
        // Auto-scan immediately
        $("scan-btn").click();
      });
      picker.appendChild(row);
    });
  } catch (err) {
    picker.innerHTML = `<span class="picker-error">Network error: ${err}</span>`;
  }
}

// ── Step 1: Scan ──────────────────────────────────────────────────────────────

$("scan-btn").addEventListener("click", async () => {
  const dir = $("comet-dir").value.trim();
  if (!dir) return;

  $("scan-btn").disabled = true;
  $("scan-btn").textContent = "Scanning…";
  $("scan-status").style.display = "block";
  $("scan-status").textContent = "Reading FITS headers…";
  $("thumb-grid").innerHTML = "";
  $("bulk-controls").style.display = "none";
  state.allFiles  = [];
  state.rejected  = new Set();

  try {
    const res  = await fetch("/api/comet/scan", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({directory: dir}),
    });
    const data = await res.json();
    if (!res.ok) {
      $("scan-status").textContent = `Error: ${data.error}`;
      return;
    }

    state.directory      = data.directory;
    state.allFiles       = data.files;
    state.lastClickedIdx = null;
    state.sessions       = detectSessions(state.allFiles);

    // Pick reference frame (highest nsubs) for live preview
    state.refPath = null;
    if (state.allFiles.length > 0) {
      let best = state.allFiles[0];
      state.allFiles.forEach(f => { if (f.nsubs > best.nsubs) best = f; });
      state.refPath = best.path;
    }
    $("scan-status").textContent =
      `Found ${data.count} sub${data.count !== 1 ? "s" : ""}` +
      ` across ${state.sessions.length} night${state.sessions.length !== 1 ? "s" : ""}` +
      ` — thumbnails loading…`;
    renderSessionBar();
    renderGrid();
    $("bulk-controls").style.display = "flex";
    updateBulkSummary();
  } catch (err) {
    $("scan-status").textContent = `Network error: ${err}`;
  } finally {
    $("scan-btn").disabled = false;
    $("scan-btn").textContent = "Scan";
  }
});

// ── Session bar ───────────────────────────────────────────────────────────────

function renderSessionBar() {
  const bar = $("session-bar");
  if (state.sessions.length <= 1) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  bar.innerHTML = "<span class='session-bar-label'>Nights:</span>";
  state.sessions.forEach((sess, si) => {
    const btn = document.createElement("button");
    btn.className   = "btn btn-sm session-btn";
    btn.dataset.si  = si;
    btn.textContent = `${sess.label} (${sess.indices.length})`;
    btn.addEventListener("click", () => toggleSession(si));
    bar.appendChild(btn);
  });
  updateSessionButtons();
}

function sessionState(si) {
  // "kept" | "rejected" | "mixed"
  const indices = state.sessions[si].indices;
  const nRej    = indices.filter(i => state.rejected.has(state.allFiles[i].path)).length;
  if (nRej === 0)              return "kept";
  if (nRej === indices.length) return "rejected";
  return "mixed";
}

function toggleSession(si) {
  const st      = sessionState(si);
  const indices = state.sessions[si].indices;
  // any rejected → keep all; all kept → reject all
  if (st === "kept") {
    indices.forEach(i => state.rejected.add(state.allFiles[i].path));
  } else {
    indices.forEach(i => state.rejected.delete(state.allFiles[i].path));
  }
  syncCardClasses();
  updateSessionButtons();
  updateBulkSummary();
}

function updateSessionButtons() {
  document.querySelectorAll(".session-btn").forEach(btn => {
    const si = parseInt(btn.dataset.si);
    const st = sessionState(si);
    btn.classList.toggle("session-kept",     st === "kept");
    btn.classList.toggle("session-rejected", st === "rejected");
    btn.classList.toggle("session-mixed",    st === "mixed");
  });
}

function syncCardClasses() {
  document.querySelectorAll(".comet-thumb-card").forEach(card => {
    const idx  = parseInt(card.dataset.idx);
    const path = state.allFiles[idx].path;
    card.classList.toggle("rejected", state.rejected.has(path));
  });
}

// ── Thumbnail grid ────────────────────────────────────────────────────────────

function renderGrid() {
  const grid = $("thumb-grid");
  grid.innerHTML = "";
  state.allFiles.forEach((f, idx) => {
    const card = document.createElement("div");
    card.className = "comet-thumb-card";
    card.dataset.idx = idx;

    // Session colour stripe
    const stripe = document.createElement("div");
    stripe.className = `thumb-session-stripe session-color-${f.sessionIdx % 6}`;

    const img = document.createElement("img");
    img.className = "comet-thumb-img";
    img.loading   = "lazy";
    img.src       = f.thumb_url;
    img.alt       = f.filename;

    const overlay = document.createElement("div");
    overlay.className = "thumb-reject-overlay";
    overlay.textContent = "✕";

    const badge = document.createElement("div");
    badge.className = "thumb-number-badge";
    badge.textContent = idx + 1;

    const meta = document.createElement("div");
    meta.className = "thumb-meta";
    meta.innerHTML =
      `<span class="thumb-date">${fmtDate(f.date_obs)}</span>` +
      `<span class="thumb-info">${f.exptime}s · ${f.nsubs} sub${f.nsubs !== 1 ? "s" : ""}</span>`;

    card.append(stripe, img, badge, overlay, meta);
    card.addEventListener("click", e => onCardClick(e, idx, card));
    grid.appendChild(card);
  });
}

function onCardClick(e, idx, card) {
  if (e.shiftKey && state.lastClickedIdx !== null && state.lastClickedIdx !== idx) {
    // Range action: match what a plain click on this card would do
    const willReject = !state.rejected.has(state.allFiles[idx].path);
    const lo = Math.min(state.lastClickedIdx, idx);
    const hi = Math.max(state.lastClickedIdx, idx);
    for (let i = lo; i <= hi; i++) {
      const p = state.allFiles[i].path;
      if (willReject) state.rejected.add(p);
      else            state.rejected.delete(p);
    }
    syncCardClasses();
  } else {
    // Plain toggle
    const path = state.allFiles[idx].path;
    if (state.rejected.has(path)) {
      state.rejected.delete(path);
      card.classList.remove("rejected");
    } else {
      state.rejected.add(path);
      card.classList.add("rejected");
    }
    state.lastClickedIdx = idx;
  }
  updateSessionButtons();
  updateBulkSummary();
}

$("keep-all-btn").addEventListener("click", () => {
  state.rejected.clear();
  syncCardClasses();
  updateSessionButtons();
  updateBulkSummary();
});

$("reject-all-btn").addEventListener("click", () => {
  state.allFiles.forEach(f => state.rejected.add(f.path));
  syncCardClasses();
  updateSessionButtons();
  updateBulkSummary();
});

// ── Step 2: Parameters ────────────────────────────────────────────────────────

// Preview helpers
function schedulePreview() {
  if (state.previewDebounce) clearTimeout(state.previewDebounce);
  state.previewDebounce = setTimeout(fetchPreview, 400);
}

async function fetchPreview() {
  if (!state.refPath) return;
  $("preview-spinner").style.display = "inline";
  try {
    const res = await fetch("/api/comet/preview-frame", {
      method:  "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        path:     state.refPath,
        sky_pct:  parseFloat($("p-sky").value),
        high_pct: parseFloat($("p-high").value),
        gamma:    parseFloat($("p-gamma").value),
        noise:    parseInt($("p-noise").value),
        width:    640,
      }),
    });
    if (res.ok) {
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const img  = $("preview-img");
      if (img._prevUrl) URL.revokeObjectURL(img._prevUrl);
      img._prevUrl = url;
      img.src = url;
      $("preview-pane").style.display = "block";
    }
  } catch (_) {}
  finally {
    $("preview-spinner").style.display = "none";
  }
}

[["p-fps",       "p-fps-val",       v => v,                         false],
 ["p-gamma",     "p-gamma-val",     v => parseFloat(v).toFixed(2),  true],
 ["p-crop",      "p-crop-val",      v => v,                         false],
 ["p-maxframes", "p-maxframes-val", v => v,                         false],
 ["p-sky",       "p-sky-val",       v => v,                         true],
 ["p-high",      "p-high-val",      v => parseFloat(v).toFixed(1),  true],
 ["p-noise",     "p-noise-val",     v => v,                         true],
].forEach(([id, valId, fmt, preview]) => {
  const el  = $(id);
  const out = $(valId);
  out.textContent = fmt(el.value);
  el.addEventListener("input", () => {
    out.textContent = fmt(el.value);
    if (preview) schedulePreview();
  });
});

$("back-to-1").addEventListener("click", () => showSection(1));

$("to-step-2").addEventListener("click", () => {
  showSection(2);
  if (state.refPath) fetchPreview();
});

$("to-step-3").addEventListener("click", () => {
  const kept    = state.allFiles.filter(f => !state.rejected.has(f.path));
  const summary = `${kept.length} subs · FPS ${$("p-fps").value} · ` +
                  `γ=${$("p-gamma").value} · sky=${$("p-sky").value}% · ` +
                  `white=${$("p-high").value}% · noise=${$("p-noise").value} · ` +
                  `crop ${$("p-crop").value}px · max ${$("p-maxframes").value} frames · ` +
                  `${$("p-width").value}px wide`;
  $("render-summary").textContent = summary;
  $("render-results").style.display  = "none";
  $("render-log-wrap").style.display = "none";
  $("render-progress-wrap").style.display = "none";
  $("render-message").textContent = "";
  $("start-render-btn").style.display  = "inline-flex";
  $("cancel-render-btn").style.display = "none";
  state.logLines = 0;
  showSection(3);
});

// ── Step 3: Render ────────────────────────────────────────────────────────────

$("back-to-2").addEventListener("click", () => {
  stopPolling();
  showSection(2);
});

$("start-render-btn").addEventListener("click", startRender);

$("cancel-render-btn").addEventListener("click", async () => {
  if (!state.jobId) return;
  await fetch("/api/comet/cancel", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({job_id: state.jobId}),
  });
  stopPolling();
  $("render-message").textContent = "Cancelled.";
  $("cancel-render-btn").style.display = "none";
  $("start-render-btn").style.display  = "inline-flex";
});

async function startRender() {
  const kept = state.allFiles.filter(f => !state.rejected.has(f.path));
  if (kept.length < 2) {
    if (state.allFiles.length === 0) {
      alert("Still scanning for files — please wait a moment, then try again.");
    } else {
      alert("Select at least 2 subs.");
    }
    return;
  }

  $("start-render-btn").style.display   = "none";
  $("cancel-render-btn").style.display  = "inline-flex";
  $("render-progress-wrap").style.display = "block";
  $("render-log-wrap").style.display    = "block";
  $("render-results").style.display     = "none";
  $("render-log").textContent           = "";
  state.logLines = 0;
  startElapsed();
  setProgress(0, "Starting…");

  const body = {
    directory:  state.directory,
    files:      kept.map(f => f.path),
    fps:        parseInt($("p-fps").value),
    gamma:      parseFloat($("p-gamma").value),
    crop:       parseInt($("p-crop").value),
    max_frames: parseInt($("p-maxframes").value),
    no_cache:         $("p-nocache").checked,
    redetect_nucleus: $("p-redetect").checked,
    sky_pct:    parseFloat($("p-sky").value),
    high_pct:   parseFloat($("p-high").value),
    noise:        parseInt($("p-noise").value),
    width:        parseInt($("p-width").value),
    max_gap_mult: parseFloat($("p-maxgap").value),
  };
  if (state.nucleusHint) {
    body.nucleus_hint_x = state.nucleusHint.x;
    body.nucleus_hint_y = state.nucleusHint.y;
  }

  try {
    const res  = await fetch("/api/comet/render", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      setProgress(0, `Error: ${data.error}`);
      $("cancel-render-btn").style.display = "none";
      $("start-render-btn").style.display  = "inline-flex";
      return;
    }
    state.jobId = data.job_id;
    startPolling();
  } catch (err) {
    setProgress(0, `Network error: ${err}`);
    $("cancel-render-btn").style.display = "none";
    $("start-render-btn").style.display  = "inline-flex";
  }
}

function setProgress(pct, msg) {
  $("render-progress-fill").style.width = pct + "%";
  $("render-message").textContent       = msg || "";
}

function startElapsed() {
  stopElapsed();
  state.renderStartTime = Date.now();
  const el = $("render-elapsed");
  el.style.display = "inline";
  el.textContent   = "0:00";
  state.elapsedTimer = setInterval(() => {
    const s   = Math.floor((Date.now() - state.renderStartTime) / 1000);
    const m   = Math.floor(s / 60);
    const ss  = String(s % 60).padStart(2, "0");
    el.textContent = `${m}:${ss}`;
  }, 1000);
}

function stopElapsed() {
  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollStatus, 1500);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

async function pollStatus() {
  if (!state.jobId) return;
  try {
    const res  = await fetch(`/api/comet/status?job_id=${state.jobId}`);
    const data = await res.json();

    setProgress(data.pct || 0, data.message || "");

    // Append new log lines
    const logEl    = $("render-log");
    const newLines = (data.log || []).slice(state.logLines);
    if (newLines.length) {
      logEl.textContent += newLines.join("\n") + "\n";
      state.logLines += newLines.length;
      // Auto-scroll
      const wrap = $("render-log-wrap");
      wrap.scrollTop = wrap.scrollHeight;
    }

    if (data.status === "done") {
      stopPolling();
      stopElapsed();
      $("cancel-render-btn").style.display = "none";
      $("start-render-btn").style.display  = "inline-flex";
      state.nucleusHint = null;   // hint was applied; clear it
      // Show "View Results" button linking to the dedicated results page
      const resultsUrl = `/comet/results?dir=${encodeURIComponent(state.directory)}`;
      const vrbtn = $("view-results-btn");
      if (vrbtn) { vrbtn.href = resultsUrl; vrbtn.style.display = "inline-flex"; }
      showResults(data.outputs || {});
    } else if (data.status === "error" || data.status === "cancelled") {
      stopPolling();
      stopElapsed();
      $("cancel-render-btn").style.display = "none";
      $("start-render-btn").style.display  = "inline-flex";
      if (data.status === "error") {
        setProgress(data.pct || 0, `Error: ${data.error || "unknown"}`);
      }
    }
  } catch (_) { /* network glitch — keep polling */ }
}

function showResults(outputs) {
  const container = $("result-links");
  container.innerHTML = "";

  // "View full results page" shortcut at top when directory is known
  if (state.directory) {
    const topBar = document.createElement("div");
    topBar.className = "results-topbar";
    const resultsUrl = `/comet/results?dir=${encodeURIComponent(state.directory)}`;
    topBar.innerHTML =
      `<span class="results-topbar-hint">Click a frame below to fix the nucleus or reject it from the render.</span>` +
      `<a class="btn btn-sm" href="${resultsUrl}" target="_blank">⛶ Full results page</a>`;
    container.appendChild(topBar);
  }

  const addVideo = (label, path) => {
    if (!path) return;
    const url  = `/api/comet/output?path=${encodeURIComponent(path)}`;
    const wrap = document.createElement("div");
    wrap.className = "result-card";

    const title = document.createElement("div");
    title.className = "result-card-title";
    title.textContent = label;

    const video = document.createElement("video");
    video.className = "result-video";
    video.src      = url;
    video.controls = true;
    video.loop     = true;
    video.muted    = true;
    video.preload  = "metadata";

    const dl = document.createElement("a");
    dl.className  = "result-dl-link";
    dl.href       = url;
    dl.download   = path.split("/").pop();
    dl.textContent = "⬇ Download";

    wrap.append(title, video, dl);
    container.appendChild(wrap);
  };

  const addImage = (label, path) => {
    if (!path) return;
    const url  = `/api/comet/output?path=${encodeURIComponent(path)}`;
    const wrap = document.createElement("div");
    wrap.className = "result-card";

    const title = document.createElement("div");
    title.className = "result-card-title";
    title.textContent = label;

    const img = document.createElement("img");
    img.className = "result-img";
    img.src = url;
    img.alt = label;

    const dl = document.createElement("a");
    dl.className  = "result-dl-link";
    dl.href       = url;
    dl.download   = path.split("/").pop();
    dl.textContent = "⬇ Download";

    wrap.append(title, img, dl);
    container.appendChild(wrap);
  };

  addVideo("⭐ Stars-fixed animation",    outputs.stars_mp4);
  addVideo("☄ Nucleus-fixed animation",  outputs.nucleus_mp4);


  addImage("🎨 Comet portrait",            outputs.portrait_jpg);
  addImage("🖼 Composite stack",           outputs.stack_jpg);
  addImage("☄ Nucleus-aligned stack",    outputs.nucleus_stack_jpg);
  addImage("🔬 Larson-Sekanina filter",   outputs.ls_jpg);
  addImage("📍 Track composite",          outputs.track_jpg);

  // Frame browser
  if (outputs.frame_count > 0 && outputs.frame_dir) {
    const fbCard = document.createElement("div");
    fbCard.className = "result-card frame-browser-card";

    const fbTitle = document.createElement("div");
    fbTitle.className = "result-card-title frame-browser-title";
    fbTitle.innerHTML =
      `<span>🎞 Frame review — ${outputs.frame_count} annotated frames</span>` +
      `<span class="frame-collapse-arrow">▾</span>`;

    const fbBody = document.createElement("div");
    fbBody.className = "frame-browser-body";

    fbTitle.addEventListener("click", () => {
      const collapsed = fbBody.classList.toggle("collapsed");
      fbTitle.querySelector(".frame-collapse-arrow").textContent = collapsed ? "▸" : "▾";
    });

    // Viewer pane (hidden until a thumb is clicked)
    const viewer = document.createElement("div");
    viewer.className = "frame-viewer";
    viewer.id = "frame-viewer";
    viewer.style.display = "none";

    const imgWrap = document.createElement("div");
    imgWrap.className = "frame-viewer-img-wrap";

    const viewerImg = document.createElement("img");
    viewerImg.className = "frame-viewer-img";
    viewerImg.id = "frame-viewer-img";

    const nucleusMarker = document.createElement("div");
    nucleusMarker.className = "nucleus-hint-marker";
    nucleusMarker.id = "nucleus-hint-marker";
    nucleusMarker.style.display = "none";

    imgWrap.append(viewerImg, nucleusMarker);

    const viewerCaption = document.createElement("div");
    viewerCaption.className = "frame-viewer-caption";
    viewerCaption.id = "frame-viewer-caption";

    const viewerNav = document.createElement("div");
    viewerNav.className = "frame-viewer-nav";
    viewerNav.innerHTML =
      `<button class="btn btn-sm" id="fv-prev">← Prev</button>` +
      `<span id="fv-counter"></span>` +
      `<button class="btn btn-sm" id="fv-next">Next →</button>` +
      `<button class="btn btn-sm btn-warning" id="fv-fix-nucleus" title="Click the actual nucleus position on the image">` +
        `⊕ Fix nucleus</button>` +
      `<button class="btn btn-sm fv-reject-btn" id="fv-reject-frame" title="Exclude this frame from animations and stacks">` +
        `✕ Reject</button>`;

    viewer.append(imgWrap, viewerCaption, viewerNav);

    const strip = document.createElement("div");
    strip.className = "frame-strip";
    strip.id = "frame-strip";

    fbBody.append(viewer, strip);
    fbCard.append(fbTitle, fbBody);
    container.appendChild(fbCard);

    loadFrameStrip(outputs.frame_dir);
  }

  // Nucleus correction banner (shown when a hint has been set)
  const hintBanner = document.createElement("div");
  hintBanner.id = "nucleus-hint-banner";
  hintBanner.className = "nucleus-hint-banner";
  hintBanner.style.display = "none";
  hintBanner.innerHTML =
    `<span id="hint-banner-text">⊕ Nucleus correction set.</span>` +
    `<button class="btn btn-sm btn-primary" id="rerender-btn">Re-render →</button>` +
    `<button class="btn btn-sm" id="clear-hint-btn">Clear correction</button>`;
  container.appendChild(hintBanner);

  document.getElementById("rerender-btn")?.addEventListener("click", () => {
    $("render-results").style.display = "none";
    startRender();
  });
  document.getElementById("clear-hint-btn")?.addEventListener("click", () => {
    state.nucleusHint = null;
    hintBanner.style.display = "none";
  });

  _updateNucleusHintBanner();

  $("render-results").style.display = "block";
  setProgress(100, "Complete!");
}

function _updateNucleusHintBanner() {
  const banner = document.getElementById("nucleus-hint-banner");
  if (!banner) return;
  const hasHint = !!state.nucleusHint;
  const nRej    = state.frameRejections.size;
  const show    = hasHint || nRej > 0;
  banner.style.display = show ? "flex" : "none";
  if (!show) return;
  const parts = [];
  if (hasHint) parts.push("⊕ Nucleus correction set");
  if (nRej > 0) parts.push(`✕ ${nRej} frame${nRej > 1 ? "s" : ""} rejected`);
  const textEl = document.getElementById("hint-banner-text");
  if (textEl) textEl.textContent = parts.join("  ·  ") + ".";
}

// ── Frame browser ─────────────────────────────────────────────────────────────

let _frameList         = [];
let _frameIdx          = 0;
let _nucleusCorrectMode = false;

async function loadFrameStrip(dir) {
  state.frameDir = dir;
  const strip = $("frame-strip");
  if (!strip) return;
  strip.innerHTML = '<span style="color:var(--text-muted);font-size:.8rem;padding:.5rem">Loading…</span>';
  try {
    const res  = await fetch(`/api/comet/frames?dir=${encodeURIComponent(dir)}`);
    const data = await res.json();
    _frameList = data.frames || [];
    strip.innerHTML = "";
    _frameList.forEach((f, i) => {
      const url = `/api/comet/output?path=${encodeURIComponent(f.path)}`;
      const img = document.createElement("img");
      img.className = "frame-strip-thumb";
      img.loading   = "lazy";
      img.src       = url;
      img.title     = f.name;
      img.dataset.idx = i;
      img.addEventListener("click", () => openFrameViewer(i));
      strip.appendChild(img);
    });
    // Load any saved rejections and mark the strip
    await _loadFrameRejections();
  } catch (err) {
    strip.innerHTML = `<span style="color:#fca5a5;font-size:.8rem;padding:.5rem">Error: ${err}</span>`;
  }
}

function openFrameViewer(idx) {
  _frameIdx          = idx;
  _nucleusCorrectMode = false;
  const marker = $("nucleus-hint-marker");
  if (marker) marker.style.display = "none";
  const viewer  = $("frame-viewer");
  const img     = $("frame-viewer-img");
  const caption = $("frame-viewer-caption");
  const counter = $("fv-counter");
  const f = _frameList[idx];
  if (!f) return;

  img.src       = `/api/comet/output?path=${encodeURIComponent(f.path)}`;
  img.classList.remove("nucleus-correct-mode");
  const isRejected = state.frameRejections.has(idx);
  viewer.classList.toggle("frame-viewer-rejected", isRejected);
  const rejLabel = isRejected ? "  ✕ REJECTED" : "";
  caption.textContent = f.name.replace(/^frame_\d+\.jpg$/, `Frame ${idx + 1} of ${_frameList.length}`) + rejLabel;
  counter.textContent = `${idx + 1} / ${_frameList.length}`;
  viewer.style.display = "block";

  // Highlight active thumb
  document.querySelectorAll(".frame-strip-thumb").forEach(t =>
    t.classList.toggle("active", parseInt(t.dataset.idx) === idx));

  // Scroll thumb into view
  const thumb = document.querySelector(`.frame-strip-thumb[data-idx="${idx}"]`);
  if (thumb) thumb.scrollIntoView({behavior: "smooth", block: "nearest", inline: "center"});

  $("fv-prev").onclick = () => openFrameViewer(Math.max(0, _frameIdx - 1));
  $("fv-next").onclick = () => openFrameViewer(Math.min(_frameList.length - 1, _frameIdx + 1));

  // Fix-nucleus button
  $("fv-fix-nucleus").onclick = () => {
    _nucleusCorrectMode = true;
    img.classList.add("nucleus-correct-mode");
    $("frame-viewer-caption").textContent = "☞ Click on the actual comet nucleus…  (Esc to cancel)";
  };

  // Reject/restore button — toggles this frame in state.frameRejections
  _updateRejectBtn(idx);
  $("fv-reject-frame").onclick = () => {
    if (state.frameRejections.has(idx)) {
      state.frameRejections.delete(idx);
    } else {
      state.frameRejections.add(idx);
    }
    // Refresh the viewer overlay + caption immediately
    const nowRej = state.frameRejections.has(idx);
    viewer.classList.toggle("frame-viewer-rejected", nowRej);
    const rejLabel = nowRej ? "  ✕ REJECTED" : "";
    caption.textContent =
      f.name.replace(/^frame_\d+\.jpg$/, `Frame ${idx + 1} of ${_frameList.length}`) + rejLabel;
    _updateRejectBtn(idx);
    _syncRejectThumb(idx);
    _saveFrameRejections();
    _updateNucleusHintBanner();
  };

  // Click-to-correct on the frame image
  img._nucleusClickHandler && img.removeEventListener("click", img._nucleusClickHandler);
  img._nucleusClickHandler = (e) => {
    if (!_nucleusCorrectMode) return;
    const rect = img.getBoundingClientRect();
    const fx   = (e.clientX - rect.left)  / rect.width;
    const fy   = (e.clientY - rect.top)   / rect.height;
    state.nucleusHint = {x: fx, y: fy};
    _nucleusCorrectMode = false;
    img.classList.remove("nucleus-correct-mode");
    caption.textContent = `✓ Nucleus correction set at (${(fx*100).toFixed(1)}%, ${(fy*100).toFixed(1)}%)  — re-render to apply`;
    _updateNucleusHintBanner();
    // Show marker at click position
    const marker = $("nucleus-hint-marker");
    if (marker) {
      marker.style.left    = (fx * 100) + "%";
      marker.style.top     = (fy * 100) + "%";
      marker.style.display = "block";
    }
  };
  img.addEventListener("click", img._nucleusClickHandler);
}

function _updateRejectBtn(idx) {
  const btn = $("fv-reject-frame");
  if (!btn) return;
  const isRej = state.frameRejections.has(idx);
  btn.textContent = isRej ? "↺ Restore" : "✕ Reject";
  btn.classList.toggle("fv-reject-active", isRej);
}

function _syncRejectThumb(idx) {
  const thumb = document.querySelector(`.frame-strip-thumb[data-idx="${idx}"]`);
  if (thumb) thumb.classList.toggle("frame-rejected", state.frameRejections.has(idx));
}

async function _loadFrameRejections() {
  if (!state.frameDir) return;
  try {
    const res  = await fetch(`/api/comet/rejections?dir=${encodeURIComponent(state.frameDir)}`);
    const data = await res.json();
    state.frameRejections = new Set((data.rejected_indices || []).map(Number));
    // Apply to any strip thumbs already in the DOM
    document.querySelectorAll(".frame-strip-thumb").forEach(t => {
      const i = parseInt(t.dataset.idx);
      t.classList.toggle("frame-rejected", state.frameRejections.has(i));
    });
    _updateNucleusHintBanner();
  } catch (_) {}
}

async function _saveFrameRejections() {
  if (!state.frameDir) return;
  try {
    await fetch("/api/comet/set_rejections", {
      method:  "POST",
      headers: {"Content-Type": "application/json"},
      body:    JSON.stringify({
        dir:              state.frameDir,
        rejected_indices: [...state.frameRejections],
      }),
    });
  } catch (_) {}
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && _nucleusCorrectMode) {
    _nucleusCorrectMode = false;
    const img = $("frame-viewer-img");
    if (img) img.classList.remove("nucleus-correct-mode");
    openFrameViewer(_frameIdx);
  }
});

// ── URL param: ?dir=… pre-fills, auto-scans, and shows existing results ───────

(function () {
  const dir = new URLSearchParams(window.location.search).get("dir");
  if (!dir) return;

  $("comet-dir").value = dir;

  // Check for existing outputs; if found, jump straight to step 3 with the
  // full results UI (frame browser, Fix Nucleus, Reject, Re-render).
  // The scan still runs in the background to populate state.allFiles so that
  // "Re-render →" works without going back to step 1.
  fetch(`/api/comet/check?dir=${encodeURIComponent(dir)}`)
    .then(r => r.json())
    .then(data => {
      const o = data.outputs || {};
      if (o.stars_mp4 || o.nucleus_mp4) {
        state.directory = dir;
        $("render-summary").textContent =
          `Cached results — ${dir.split("/").filter(Boolean).pop()}`;
        $("render-progress-wrap").style.display = "none";
        $("render-log-wrap").style.display      = "none";
        $("start-render-btn").style.display     = "inline-flex";
        $("cancel-render-btn").style.display    = "none";
        showSection(3);
        showResults(o);
      }
    })
    .catch(() => {});

  // Scan in background so state.allFiles is ready if the user re-renders
  setTimeout(() => $("scan-btn").click(), 60);
})()
