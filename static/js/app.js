'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const sessions          = {};   // object_name → session dict  (source of truth)
const transitData       = {};   // object_name → {video_jobs: [...], events: [...]}
const yoloConfirmedOnly = {};   // object_name → bool (confirmed-only filter per card)
let activeFilter    = 'all';
let evtSource       = null;
let dbLoaded        = false; // true once we've received the initial DB flush
let transitPaused   = false; // mirrors server pause state

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeFilter = btn.dataset.type;
      applyFilter();
    });
  });
  openEventStream();
});

// ── SSE connection ────────────────────────────────────────────────────────────
function openEventStream() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/events');
  setStatus('Connecting…', true);

  evtSource.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); }
    catch (err) { console.error('SSE parse error', err); }
  };

  evtSource.onerror = () => {
    setStatus('Connection lost — reconnecting…', false);
    evtSource.close();
    setTimeout(openEventStream, 4000);
  };
}

// ── Event dispatcher ──────────────────────────────────────────────────────────
function handleEvent(ev) {
  switch (ev.type) {

    case 'session':
      upsertSession(ev.data);
      break;

    case 'session_removed':
      removeSession(ev.object_name);
      break;

    case 'db_loaded':
      dbLoaded = true;
      // Load transit data then render everything once both are ready
      loadTransitData().then(() => {
        applyFilter();
        updateFilterCounts();
        updateSummary();
      });
      if (ev.count === 0) {
        setStatus('Database empty — running first scan…', true);
        showEmptyGrid('Running first scan — cards will appear as they are found…');
      } else {
        setStatus(
          `${ev.count} session${ev.count !== 1 ? 's' : ''} loaded from database`, false
        );
      }
      break;

    case 'progress':
      setStatus(ev.message, true);
      // pct present → determined bar; absent → indeterminate shimmer
      updateProgressBar(typeof ev.pct === 'number' ? ev.pct : null);
      break;

    case 'complete': {
      const msg = ev.changed > 0
        ? `Scan complete — ${ev.changed} session${ev.changed !== 1 ? 's' : ''} updated`
        : 'Scan complete — index is current';
      setStatus(msg, false);
      setScanButtons(false);
      updateSummary();
      updateFilterCounts();
      updateProgressBar(100);
      setTimeout(hideProgressBar, 900);
      break;
    }

    case 'error':
      setStatus('⚠ ' + ev.message, false);
      setScanButtons(false);
      hideProgressBar();
      break;

    case 'transit_progress':
      handleTransitProgress(ev);
      break;

    case 'transit_done':
      handleTransitDone(ev);
      break;

    case 'transit_queue_state':
      transitPaused = !!ev.paused;
      updateTransitQueueControls();
      // If a cancel was for all, refresh affected footers
      if (ev.cancel_all) {
        document.querySelectorAll('.transit-footer').forEach(el => {
          const card = el.closest('.session-card');
          if (!card) return;
          const name = Object.keys(sessions).find(n => cardId(n) === card.id);
          if (name) el.outerHTML = buildTransitFooter(name);
        });
      }
      break;
  }
}

// ── Session management ────────────────────────────────────────────────────────
function upsertSession(session) {
  const name  = session.object_name;
  const isNew = !(name in sessions);
  sessions[name] = session;

  // While the initial DB flush is still streaming, accumulate silently.
  // applyFilter() is called once on db_loaded for a single efficient render.
  if (!dbLoaded) return;

  if (isNew) {
    if (passesFilter(session)) {
      clearEmptyState();
      document.getElementById('sessions-grid')
        .insertAdjacentHTML('afterbegin', buildCard(session));
    }
    updateFilterCounts();
  } else {
    // Update existing card in place (diff-scan found changes)
    const el = document.getElementById(cardId(name));
    if (el) el.outerHTML = buildCard(session);
  }
  updateSummary();
}

function removeSession(objectName) {
  delete sessions[objectName];
  const el = document.getElementById(cardId(objectName));
  if (el) el.remove();
  updateSummary();
  updateFilterCounts();
  if (!document.querySelector('.session-card')) {
    showEmptyGrid('No sessions match this filter.');
  }
}

// ── Scan buttons ──────────────────────────────────────────────────────────────
async function triggerScan(force = false) {
  setScanButtons(true);
  setStatus(force ? 'Starting full rescan…' : 'Starting differential scan…', true);
  updateProgressBar(null); // show indeterminate bar immediately
  try {
    const res = await fetch('/api/scan', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ force }),
    });
    if (res.status === 409) {
      setStatus('Scan already running…', true);
      setScanButtons(true);
    }
  } catch {
    setStatus('Failed to start scan', false);
    setScanButtons(false);
    hideProgressBar();
  }
}

function setScanButtons(scanning) {
  document.getElementById('scan-btn').disabled      = scanning;
  document.getElementById('scan-full-btn').disabled = scanning;
}

// ── Transit detection ─────────────────────────────────────────────────────────

async function loadTransitData() {
  try {
    const res = await fetch('/api/transit/all');
    if (!res.ok) return;
    const data = await res.json();
    Object.assign(transitData, data);
  } catch { /* non-fatal */ }
}

function updateTransitQueueControls() {
  const pauseBtn = document.getElementById('transit-pause-btn');
  if (!pauseBtn) return;

  // Tally job statuses across all sessions
  let running = 0, pending = 0, done = 0, errored = 0;
  for (const td of Object.values(transitData)) {
    for (const j of td.video_jobs) {
      if      (j.status === 'running')   running++;
      else if (j.status === 'pending')   pending++;
      else if (j.status === 'done')      done++;
      else if (j.status === 'error')     errored++;
    }
  }

  const hasActive = running > 0 || pending > 0;
  const bar = document.getElementById('transit-queue-bar');
  if (bar) bar.style.display = hasActive ? 'flex' : 'none';

  // Build label: "Transit queue · 1 running · 4 pending · 12 done"
  const label = document.getElementById('transit-queue-label');
  if (label) {
    const parts = [transitPaused ? 'Transit queue (paused)' : 'Transit queue'];
    if (running) parts.push(`${running} running`);
    if (pending) parts.push(`${pending} pending`);
    if (done)    parts.push(`${done} done`);
    if (errored) parts.push(`${errored} error`);
    label.textContent = parts.join(' · ');
  }

  pauseBtn.textContent = transitPaused ? '▶ Resume' : '⏸ Pause';
  pauseBtn.classList.toggle('paused', transitPaused);
}

async function toggleTransitPause() {
  const route = transitPaused ? '/api/transit/resume' : '/api/transit/pause';
  await fetch(route, { method: 'POST' });
}

async function cancelAllTransit() {
  if (!confirm('Cancel all queued transit detection jobs?')) return;
  await fetch('/api/transit/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ all: true }),
  });
}

async function cancelSessionTransit(sessionName) {
  await fetch('/api/transit/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_name: sessionName }),
  });
  // Optimistically mark pending jobs as cancelled in local state
  const td = transitData[sessionName];
  if (td) {
    td.video_jobs.forEach(j => {
      if (j.status === 'pending' || j.status === 'running') j.status = 'cancelled';
    });
  }
  // Refresh card footer
  const card = document.getElementById(cardId(sessionName));
  if (card) {
    const footer = card.querySelector('.transit-footer');
    if (footer) footer.outerHTML = buildTransitFooter(sessionName);
  }
  updateTransitQueueControls();
}

async function queueTransitDetection(sessionName, force = false) {
  const btn = document.getElementById(`transit-btn-${cardId(sessionName).slice(5)}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Queuing…'; }
  try {
    const res = await fetch('/api/transit/detect', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ session_name: sessionName, force }),
    });
    const body = await res.json();
    if (!res.ok) {
      alert(`Transit detection error: ${body.error || res.status}`);
      if (btn) { btn.disabled = false; btn.textContent = 'Detect Transits'; }
      return;
    }
    if (body.queued === 0) {
      if (btn) { btn.disabled = false; btn.textContent = 'No new videos'; }
    } else {
      // Pre-populate transitData so we know total queue size before first SSE arrives.
      // Always upsert — this resets any lingering error entries back to 'pending'.
      if (!transitData[sessionName]) transitData[sessionName] = { video_jobs: [], events: [] };
      const jobs = transitData[sessionName].video_jobs;
      const queuedPaths = new Set(body.videos || []);
      for (const vpath of queuedPaths) {
        const basename = vpath.split('/').pop();
        const pending  = { video_path: vpath, basename, status: 'pending', pct: 0, message: '', error_msg: null };
        const idx = jobs.findIndex(j => j.video_path === vpath);
        if (idx >= 0) jobs[idx] = pending; else jobs.push(pending);
      }
      // On a forced re-detect, immediately remove stale event pills for the
      // re-queued videos so the UI shows a clean slate during detection.
      if (force) {
        transitData[sessionName].events = transitData[sessionName].events
          .filter(e => !queuedPaths.has(e.video_path));
      }
      const card = document.getElementById(cardId(sessionName));
      if (card) {
        const footer = card.querySelector('.transit-footer');
        if (footer) footer.outerHTML = buildTransitFooter(sessionName);
      }
    }
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Detect Transits'; }
  }
}

function handleTransitProgress(ev) {
  const sn = ev.session_name;
  if (!transitData[sn]) transitData[sn] = { video_jobs: [], events: [] };

  const jobs = transitData[sn].video_jobs;
  const idx  = jobs.findIndex(j => j.video_path === ev.video_path);
  const entry = {
    video_path: ev.video_path, basename: ev.video_basename,
    status: ev.status, pct: ev.pct, message: ev.message || '',
    error_msg: ev.status === 'error' ? (ev.message || 'unknown error') : null,
  };
  if (idx >= 0) jobs[idx] = entry; else jobs.push(entry);

  const card = document.getElementById(cardId(sn));
  if (card) {
    const footer = card.querySelector('.transit-footer');
    if (footer) footer.outerHTML = buildTransitFooter(sn);
  }
  updateTransitQueueControls();
}

function handleTransitDone(ev) {
  const sn = ev.session_name;
  if (!transitData[sn]) transitData[sn] = { video_jobs: [], events: [] };

  const jobs = transitData[sn].video_jobs;
  const idx  = jobs.findIndex(j => j.video_path === ev.video_path);
  const entry = { video_path: ev.video_path, basename: ev.video_basename,
                  status: 'done', pct: 100, message: '', error_msg: null };
  if (idx >= 0) jobs[idx] = entry; else jobs.push(entry);

  // Remove any stale events for this video (handles re-detection cleanly).
  transitData[sn].events = transitData[sn].events.filter(e => e.video_path !== ev.video_path);
  transitData[sn].events.push(...ev.events);

  const card = document.getElementById(cardId(sn));
  if (card) {
    const footer = card.querySelector('.transit-footer');
    if (footer) footer.outerHTML = buildTransitFooter(sn);
  }
  updateTransitQueueControls();
}

// ── Progress bar ──────────────────────────────────────────────────────────────
function updateProgressBar(pct) {
  const bar  = document.getElementById('scan-progress-bar');
  const fill = document.getElementById('scan-progress-fill');
  bar.style.display = '';
  if (pct === null || pct === undefined) {
    // Indeterminate: animated sweep
    bar.classList.add('indeterminate');
    fill.style.width = '';
  } else {
    // Determined: grow to pct %
    bar.classList.remove('indeterminate');
    fill.style.width = `${pct}%`;
  }
}

function hideProgressBar() {
  const bar  = document.getElementById('scan-progress-bar');
  const fill = document.getElementById('scan-progress-fill');
  bar.style.display = 'none';
  bar.classList.remove('indeterminate');
  fill.style.width = '0%';
}

// ── Filter ────────────────────────────────────────────────────────────────────
function passesFilter(session) {
  return activeFilter === 'all' || session.object_type === activeFilter;
}

function applyFilter() {
  const visible = Object.values(sessions)
    .filter(passesFilter)
    .sort((a, b) => {
      const da = a.dates[a.dates.length - 1] || '';
      const db = b.dates[b.dates.length - 1] || '';
      return db.localeCompare(da);
    });

  const grid = document.getElementById('sessions-grid');
  if (!visible.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔭</div>
        <p>${Object.keys(sessions).length
          ? 'No sessions match this filter.'
          : 'No sessions found.'}</p>
      </div>`;
    return;
  }
  grid.innerHTML = visible.map(buildCard).join('');
}

function updateFilterCounts() {
  const counts = {};
  Object.values(sessions).forEach(s => {
    counts[s.object_type] = (counts[s.object_type] || 0) + 1;
  });
  const total = Object.keys(sessions).length;
  document.querySelectorAll('.filter-btn[data-type]').forEach(btn => {
    const type = btn.dataset.type;
    const base = btn.dataset.label;
    if (type === 'all') {
      btn.textContent = total ? `${base} (${total})` : base;
    } else {
      const n = counts[type] || 0;
      btn.textContent = n ? `${base} (${n})` : base;
    }
  });
}

// ── Summary bar ───────────────────────────────────────────────────────────────
function updateSummary() {
  const all = Object.values(sessions);
  if (!all.length) {
    document.getElementById('summary-bar').style.display = 'none';
    return;
  }
  const totalSubs     = all.reduce((s, x) => s + (x.num_subs            || 0), 0);
  const totalVideos   = all.reduce((s, x) => s + (x.num_videos          || 0), 0);
  const totalBytes    = all.reduce((s, x) => s + (x.total_size          || 0), 0);
  const totalVideoSec = all.reduce((s, x) => s + (x.total_video_duration|| 0), 0);

  document.getElementById('stat-sessions').textContent   = all.length.toLocaleString();
  document.getElementById('stat-subs').textContent       = totalSubs.toLocaleString();
  document.getElementById('stat-size').textContent       = humanSize(totalBytes);
  document.getElementById('stat-videos').textContent     = totalVideos.toLocaleString();
  document.getElementById('stat-video-hrs').textContent  = humanDuration(totalVideoSec);
  document.getElementById('summary-bar').style.display   = 'flex';
}

// ── Card rendering ────────────────────────────────────────────────────────────
function cardId(name) {
  return 'card-' + name.replace(/[^a-z0-9]/gi, '_');
}

function buildCard(s) {
  const badge = `<span class="type-badge ${esc(s.object_type)}">${esc(s.type_label)}</span>`;
  const desc  = s.description
    ? `<div class="object-desc">${esc(s.description)}</div>` : '';

  const allDates  = s.dates || [];
  const DATES_MAX = 5;
  const dateChips = allDates.map((d, i) => {
    const extra = i >= DATES_MAX ? ' date-chip-extra' : '';
    return `<span class="date-chip${extra}">${esc(d)}</span>`;
  }).join('');
  const dateToggle = allDates.length > DATES_MAX
    ? `<button class="date-toggle-btn" onclick="toggleDates(this)"
              data-count="${allDates.length - DATES_MAX}">+${allDates.length - DATES_MAX} more</button>`
    : '';

  const subRow = s.num_subs
    ? `<div class="meta-row">
         <span class="meta-icon">📷</span>
         <span>
           <span class="meta-val">${s.num_subs.toLocaleString()}</span> subs
           &nbsp;·&nbsp;
           <span class="meta-val">${esc(s.total_size_human)}</span>
         </span>
       </div>` : '';

  const videoRow = s.num_videos
    ? `<div class="meta-row">
         <span class="meta-icon">🎥</span>
         <span>
           <span class="meta-val">${s.num_videos}</span>
           video${s.num_videos > 1 ? 's' : ''}
         </span>
       </div>` : '';

  const isTransitType = s.object_type === 'solar' || s.object_type === 'lunar';
  const transitFooter = isTransitType ? buildTransitFooter(s.object_name) : '';

  return `
    <div class="session-card" id="${cardId(s.object_name)}">
      <div class="card-header">
        <div class="object-name">${esc(s.object_name)}</div>
        ${badge}
      </div>
      ${desc}
      <hr class="card-divider" />
      <div class="card-meta">
        <div class="meta-row">
          <span class="meta-icon">📅</span>
          <div class="dates-list">${dateChips}${dateToggle}</div>
        </div>
        ${subRow}
        ${videoRow}
      </div>
      ${transitFooter}
    </div>`;
}

// ── Date chip toggle ──────────────────────────────────────────────────────────
function toggleDates(btn) {
  const list    = btn.closest('.dates-list');
  const extras  = list.querySelectorAll('.date-chip-extra');
  const open    = btn.classList.toggle('open');
  extras.forEach(el => el.style.display = open ? '' : 'none');
  btn.textContent = open ? 'show less' : `+${btn.dataset.count} more`;
}

// ── YOLO filter ───────────────────────────────────────────────────────────────

function setYoloFilter(sessionName, active) {
  yoloConfirmedOnly[sessionName] = active;
  const card = document.getElementById(cardId(sessionName));
  if (card) {
    const footer = card.querySelector('.transit-footer');
    if (footer) footer.outerHTML = buildTransitFooter(sessionName);
  }
}

// ── Transit footer ────────────────────────────────────────────────────────────

function buildTransitFooter(sessionName) {
  const td     = transitData[sessionName] || { video_jobs: [], events: [] };
  const jobs   = td.video_jobs;
  const events = td.events;
  const sn_js  = sessionName.replace(/'/g, "\\'");
  const idSuffix = sessionName.replace(/[^a-z0-9]/gi, '_');

  const hasActive = jobs.some(j => j.status === 'running' || j.status === 'pending');

  // Compute position counter: "5/125" = (done so far + 1) / total
  const totalJobs  = jobs.length;
  const doneCount  = jobs.filter(j => j.status === 'done' || j.status === 'cancelled' || j.status === 'error').length;
  const runningPos = doneCount + 1;

  // --- job progress rows (skip done/cancelled/pending — only show active/errored) ---
  let jobRows = '';
  for (const j of jobs) {
    if (j.status === 'done' || j.status === 'cancelled' || j.status === 'pending') continue;
    const pctBar = j.status === 'running'
      ? `<div class="transit-progress-wrap"><div class="transit-progress-fill" style="width:${j.pct}%"></div></div>`
      : '';
    const posLabel = (j.status === 'running' && totalJobs > 1) ? `${runningPos}/${totalJobs} · ` : '';
    const statusLabel = j.status === 'error'
      ? `<span class="transit-status error">✗ ${esc(j.error_msg || 'error')}</span>`
      : `<span class="transit-status running">${posLabel}${esc(j.message || j.status)} ${j.pct}%</span>`;
    jobRows += `<div class="transit-job-row">${esc(j.basename)} — ${statusLabel}${pctBar}</div>`;
  }

  // --- YOLO confirmed-only toggle ---
  const hasYoloData    = events.some(ev => ev.yolo_label != null);
  const yoloActive     = yoloConfirmedOnly[sessionName] || false;
  const displayEvents  = (hasYoloData && yoloActive)
    ? events.filter(ev => ev.yolo_label != null)
    : events;

  const yoloToggle = hasYoloData
    ? `<label class="yolo-filter-label">
         <input type="checkbox" ${yoloActive ? 'checked' : ''}
                onchange="setYoloFilter('${sn_js}', this.checked)">
         confirmed only
       </label>`
    : '';

  // --- detected event pills ---
  let eventPills = '';
  for (const ev of displayEvents) {
    const pct  = Math.round(ev.confidence * 100);
    const clip = ev.clip_path
      ? ` onclick="window.open('/api/transit/clip/${ev.id}','_blank')" style="cursor:pointer"`
      : '';

    // YOLO confirmation badge
    const yoloBadge = ev.yolo_label != null
      ? `<span class="yolo-badge">✓ ${esc(ev.yolo_label)}</span>`
      : '';

    const pill = `<span class="transit-event-pill ${esc(ev.label)}"${clip}>`
      + `${esc(ev.label)} ${pct}% · ${(ev.duration_s || 0).toFixed(1)}s`
      + yoloBadge
      + `</span>`;

    // Hero-frame thumbnail
    const thumbClick = ev.clip_path
      ? `onclick="window.open('/api/transit/clip/${ev.id}','_blank')"` : '';
    const thumb = ev.thumb_path
      ? `<img class="transit-thumb" src="/api/transit/thumb/${ev.id}"
             alt="${esc(ev.label)} transit" ${thumbClick}>` : '';

    // Aircraft candidates — show up to 3, closest first
    const ac = ev.aircraft_candidates;
    let acHint = '';
    if (Array.isArray(ac) && ac.length > 0) {
      const parts = ac.slice(0, 3).map(a => {
        const cs  = a.callsign || a.icao24 || '?';
        const alt = a.alt_ft != null ? `${a.alt_ft.toLocaleString()}ft` : '';
        return alt ? `${esc(cs)} ${alt}` : esc(cs);
      });
      acHint = `<div class="aircraft-hint">✈ ${parts.join('  ·  ')}</div>`;
    }

    eventPills += `<div class="transit-event-group">${thumb}${pill}${acHint}</div>`;
  }

  const allDone = jobs.length > 0 && jobs.every(j => j.status === 'done' || j.status === 'cancelled' || j.status === 'error');
  const noEvents = allDone && events.length === 0
    ? '<span class="transit-no-events">No transits detected</span>' : '';

  const btnLabel    = hasActive ? 'Detecting…' : 'Detect Transits';
  const btnDisabled = hasActive ? 'disabled' : '';
  const cancelBtn   = hasActive
    ? `<button class="btn-transit-cancel" onclick="cancelSessionTransit('${sn_js}')">✕ Cancel</button>`
    : '';
  // Show Re-detect when all jobs are finished (forces re-run with current params)
  const redetectBtn = (!hasActive && allDone)
    ? `<button class="btn-transit-redetect" onclick="queueTransitDetection('${sn_js}', true)"
              title="Re-run detection with current parameters (force)">↻ Re-detect</button>`
    : '';

  return `<div class="transit-footer">
    <div class="transit-header-row">
      <span class="transit-label">Transit detection</span>
      ${yoloToggle}
      <div class="transit-btn-group">
        ${cancelBtn}
        ${redetectBtn}
        <button id="transit-btn-${idSuffix}" class="btn-transit" ${btnDisabled}
          onclick="queueTransitDetection('${sn_js}')">
          ${btnLabel}
        </button>
      </div>
    </div>
    ${jobRows}
    ${eventPills ? `<div class="transit-events">${eventPills}</div>` : ''}
    ${noEvents}
  </div>`;
}

// ── Grid helpers ──────────────────────────────────────────────────────────────
function showEmptyGrid(msg) {
  document.getElementById('sessions-grid').innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">🔭</div>
      <p>${esc(msg)}</p>
    </div>`;
}

function clearEmptyState() {
  document.querySelector('#sessions-grid .empty-state')?.remove();
}

// ── Status bar ────────────────────────────────────────────────────────────────
function setStatus(msg, running) {
  const el     = document.getElementById('scan-status');
  el.innerHTML = running ? `<span class="spinner"></span>${esc(msg)}` : esc(msg);
  el.className = 'scan-status' + (running ? ' running' : '');
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function humanSize(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(1)} ${units[i]}`;
}

function humanDuration(secs) {
  if (!secs) return '0 min';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m} min`;
}
