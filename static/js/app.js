'use strict';

// ── Shutdown ───────────────────────────────────────────────────────────────────
async function shutdownApp() {
  if (!confirm('Stop the Seestar Lab server?')) return;
  await fetch('/api/shutdown', { method: 'POST' });
  document.body.innerHTML = '<div style="padding:2rem;font-family:monospace;color:#aaa">Server stopped. You can close this tab.</div>';
}

// ── State ─────────────────────────────────────────────────────────────────────
const sessions          = {};   // object_name → session dict  (source of truth)
const transitData       = {};   // object_name → {video_jobs: [...], events: [...]}
const stackData         = {};   // object_name → stack job dict
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
      // Load transit + stack data then render everything once both are ready
      Promise.all([loadTransitData(), loadStackData()]).then(() => {
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

    case 'stack_progress':
      handleStackProgress(ev);
      break;

    case 'stack_done':
      handleStackDone(ev);
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

async function loadStackData() {
  try {
    const res = await fetch('/api/stack/status');
    if (!res.ok) return;
    Object.assign(stackData, await res.json());
  } catch { /* non-fatal */ }
}

function handleStackProgress(ev) {
  const sn = ev.session_name;
  stackData[sn] = Object.assign(stackData[sn] || {}, {
    session_name:    sn,
    status:          ev.status || 'running',
    pct:             ev.pct,
    stage:           ev.stage,
    frames_accepted: ev.frames_accepted,
    frames_total:    ev.frames_total,
  });
  _refreshStackFooter(sn);
}

function handleStackDone(ev) {
  const sn = ev.session_name;
  stackData[sn] = Object.assign(stackData[sn] || {}, {
    session_name:    sn,
    status:          'done',
    pct:             100,
    stage:           'Done',
    frames_accepted: ev.frames_accepted,
    frames_total:    ev.frames_total,
    output_path:     ev.output_path,
  });
  _refreshStackFooter(sn);
}

function _refreshStackFooter(sessionName) {
  const card = document.getElementById(cardId(sessionName));
  if (!card) return;
  const footer = card.querySelector('.stack-footer');
  if (footer) footer.outerHTML = buildStackFooter(sessionName);
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
  const isCatalogFilter = activeFilter === 'messier' || activeFilter === 'caldwell'
                       || activeFilter === 'dso'     || activeFilter === 'unknown';

  const visible = Object.values(sessions)
    .filter(passesFilter)
    .sort((a, b) => {
      if (isCatalogFilter) {
        const na = parseInt(a.object_name.replace(/\D/g, ''), 10);
        const nb = parseInt(b.object_name.replace(/\D/g, ''), 10);
        return na - nb;
      }
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
  visible.filter(s => s.object_type === 'comet').forEach(s => loadCometInfo(s.object_name));

  // Wire "View N images" buttons (data-attribute avoids inline JS quoting issues)
  grid.querySelectorAll('.btn-view-images').forEach(btn => {
    btn.addEventListener('click', () => {
      const imgs = JSON.parse(btn.dataset.images);
      openLightbox(imgs, 0);
    });
  });
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

  // Thumbnail — shown for any session that has a preview image on disk.
  // For stacked comet sessions with multiple images, add prev/next arrows.
  // For stacked comet sessions: use image_files list (≥1 triggers lightbox,
  // >1 also shows prev/next arrows). For everything else use the thumbnail API.
  const imgs    = (s.image_files && s.image_files.length >= 1) ? s.image_files : null;
  const hasArrows = imgs && imgs.length > 1;
  const imgUrl0 = imgs
    ? `/api/image?path=${encodeURIComponent(imgs[0])}`
    : (s.thumbnail ? `/api/thumbnail/${encodeURIComponent(s.object_name)}` : null);
  const thumbHtml = imgUrl0
    ? (() => {
        const imgsAttr = imgs
          ? ` data-images='${JSON.stringify(imgs).replace(/'/g,"&#39;")}' data-idx="0"` : '';
        const arrows   = hasArrows ? `
          <button class="thumb-arrow thumb-prev" onclick="thumbStep(this,-1,event)" title="Previous">&#8249;</button>
          <button class="thumb-arrow thumb-next" onclick="thumbStep(this,+1,event)" title="Next">&#8250;</button>
          <span class="thumb-counter">1 / ${imgs.length}</span>` : '';
        const clickAttr = imgs
          ? `onclick="openLightbox(JSON.parse(this.closest('.card-thumb-wrap').dataset.images),+this.closest('.card-thumb-wrap').dataset.idx)"`
          : '';
        return `<div class="card-thumb-wrap"${imgsAttr}>
          <img class="card-thumb${imgs ? ' thumb-clickable' : ''}" src="${imgUrl0}"
               alt="${esc(s.object_name)} preview" ${clickAttr}
               onerror="this.closest('.card-thumb-wrap').style.display='none'">
          ${arrows}
        </div>`;
      })()
    : '';

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

  const isComet       = s.object_type === 'comet';
  const isTransitType = s.object_type === 'solar' || s.object_type === 'lunar';
  const transitFooter = isTransitType ? buildTransitFooter(s.object_name) : '';

  // Comets get their own footer; exclude from stack to avoid false _sub match
  const isSubSession  = !isComet && s.object_name.endsWith('_sub') && s.num_subs > 0;
  const stackFooter   = isSubSession ? buildStackFooter(s.object_name) : '';
  const cometFooter   = isComet ? buildCometFooter(s) : '';

  // Placeholder filled async by loadCometInfo()
  const cometInfoRow  = isComet
    ? `<div class="comet-fullname-row" id="comet-info-${cardId(s.object_name).slice(5)}"></div>`
    : '';

  return `
    <div class="session-card" id="${cardId(s.object_name)}">
      ${thumbHtml}
      <div class="card-header">
        <div class="object-name">${esc(s.object_name)}</div>
        ${badge}
      </div>
      ${cometInfoRow}
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
      ${stackFooter}
      ${cometFooter}
    </div>`;
}

// ── Stack footer ──────────────────────────────────────────────────────────────

async function queueStack(sessionName, force = false) {
  const btn = document.getElementById(`stack-btn-${cardId(sessionName).slice(5)}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Queuing…'; }
  try {
    const res  = await fetch('/api/stack/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ session_name: sessionName, force }),
    });
    const body = await res.json();
    if (!res.ok) {
      alert(`Stack error: ${body.error || res.status}`);
      if (btn) { btn.disabled = false; btn.textContent = 'Stack'; }
      return;
    }
    // Optimistically set state so the footer re-renders immediately
    stackData[sessionName] = Object.assign(stackData[sessionName] || {}, {
      session_name: sessionName,
      status: 'pending',
      pct: 0,
      stage: 'Queued…',
      frames_total: body.fits_count || 0,
      frames_accepted: 0,
    });
    _refreshStackFooter(sessionName);
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Stack'; }
  }
}

function buildStackFooter(sessionName) {
  const job     = stackData[sessionName];
  const sn_js   = sessionName.replace(/'/g, "\\'");
  const idSuffix = sessionName.replace(/[^a-z0-9]/gi, '_');
  const status  = job?.status;
  const isActive = status === 'pending' || status === 'running';
  const isDone   = status === 'done';
  const isError  = status === 'error';

  // Progress bar row
  let progressRow = '';
  if (isActive) {
    const pct   = job.pct || 0;
    const stage = esc(job.stage || 'Working…');
    const counts = (job.frames_total > 0)
      ? ` · ${job.frames_accepted || 0}/${job.frames_total} frames`
      : '';
    progressRow = `
      <div class="stack-progress-wrap">
        <div class="stack-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="stack-stage">${stage}${esc(counts)} ${pct}%</div>`;
  }

  // Result thumbnail + view link
  let resultRow = '';
  if (isDone && job.output_path) {
    resultRow = `
      <div class="stack-result">
        <img class="stack-result-thumb"
             src="/api/stack/image/${encodeURIComponent(sessionName)}"
             alt="Stacked result"
             onclick="window.open('/api/stack/image/${encodeURIComponent(sessionName)}','_blank')"
             title="Click to view full size">
        <a class="stack-view-link"
           href="/api/stack/image/${encodeURIComponent(sessionName)}"
           target="_blank">View full size</a>
      </div>`;
  }

  // Error message
  const errorRow = isError
    ? `<div class="stack-error">Error: ${esc(job.error_msg || 'unknown')}</div>`
    : '';

  // Buttons
  const stackBtn = (!isActive)
    ? `<button id="stack-btn-${idSuffix}" class="btn-stack"
         onclick="queueStack('${sn_js}')">Stack</button>`
    : `<button id="stack-btn-${idSuffix}" class="btn-stack" disabled>Stacking…</button>`;

  const restackBtn = (isDone || isError)
    ? `<button class="btn-stack-rerun"
         onclick="queueStack('${sn_js}', true)"
         title="Re-stack with current settings">↻ Re-stack</button>`
    : '';

  const frameInfo = isDone
    ? `<span class="stack-frame-info">${job.frames_accepted}/${job.frames_total} frames used</span>`
    : '';

  return `<div class="stack-footer">
    <div class="stack-header-row">
      <span class="stack-label">Stacking</span>
      ${frameInfo}
      <div class="stack-btn-group">
        ${restackBtn}
        ${stackBtn}
      </div>
    </div>
    ${progressRow}
    ${resultRow}
    ${errorRow}
  </div>`;
}

// ── Comet footer ──────────────────────────────────────────────────────────────

function buildCometFooter(s) {
  const animations  = s.animations || {};
  const isProcessed = !!(animations.stars_mp4 || animations.nucleus_mp4 ||
                         animations.track_jpg  || animations.stack_jpg);
  const isSub       = s.object_name.endsWith('_sub');

  // Only _sub sessions have FITS subs that the wizard can process
  const wizardBtn = isSub
    ? (() => {
        const paths  = s.paths || [];
        const dir    = animations.anim_dir || (paths.length > 0 ? paths[0] : '');
        const url    = dir ? `/comet?dir=${encodeURIComponent(dir)}` : '/comet';
        return `<a class="btn-comet-wizard" href="${url}">Open in Wizard →</a>`;
      })()
    : '';

  const statusChip = isSub
    ? (isProcessed
        ? '<span class="comet-status-chip processed">✓ Animations ready</span>'
        : '<span class="comet-status-chip pending">Not yet processed</span>')
    : '<span class="comet-status-chip stacked">Stacked images</span>';

  // For non-_sub sessions: "View N images" button that opens the lightbox.
  // Images are stored in a data-attribute; the click handler reads it at runtime.
  const imgs = s.image_files && s.image_files.length > 0 ? s.image_files : null;
  const viewBtn = (!isSub && imgs)
    ? `<button class="btn-comet-wizard btn-view-images"
               data-images="${esc(JSON.stringify(imgs))}">
         View ${imgs.length} image${imgs.length !== 1 ? 's' : ''} →
       </button>`
    : '';

  return `<div class="comet-footer">
    <div class="comet-footer-row">
      ${statusChip}
      ${wizardBtn}${viewBtn}
    </div>
  </div>`;
}

const _cometInfoCache = {};

async function loadCometInfo(name) {
  const idSuffix = name.replace(/[^a-z0-9]/gi, '_');
  const el = document.getElementById(`comet-info-${idSuffix}`);
  if (!el) return;

  if (_cometInfoCache[name] !== undefined) {
    _renderCometInfo(el, _cometInfoCache[name]);
    return;
  }
  try {
    const res  = await fetch(`/api/comet/info?name=${encodeURIComponent(name)}`);
    const data = await res.json();
    _cometInfoCache[name] = data;
    _renderCometInfo(el, data);
  } catch (_) {}
}

function _renderCometInfo(el, data) {
  if (!data || !data.fullname) return;
  let html = `<span class="comet-fullname">${esc(data.fullname)}</span>`;
  if (data.orbit_class) {
    html += ` <span class="comet-orbit-class">${esc(data.orbit_class)}</span>`;
  }
  el.innerHTML = html;
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

  // Compute position counters for the progress label "5/125 · …"
  // With concurrent workers multiple jobs can be 'running' at once; each gets
  // its own sequential position (doneCount+1, doneCount+2, …) rather than all
  // sharing the same number.
  const totalJobs = jobs.length;
  const doneCount = jobs.filter(j => j.status === 'done' || j.status === 'cancelled' || j.status === 'error').length;

  // --- job progress rows (skip done/cancelled/pending — only show active/errored) ---
  let jobRows = '';
  let runningIdx = 0;
  for (const j of jobs) {
    if (j.status === 'done' || j.status === 'cancelled' || j.status === 'pending') continue;
    const pctBar = j.status === 'running'
      ? `<div class="transit-progress-wrap"><div class="transit-progress-fill" style="width:${j.pct}%"></div></div>`
      : '';
    let posLabel = '';
    if (j.status === 'running' && totalJobs > 1) {
      runningIdx++;
      posLabel = `${doneCount + runningIdx}/${totalJobs} · `;
    }
    const statusLabel = j.status === 'error'
      ? `<span class="transit-status error">✗ ${esc(j.error_msg || 'error')}</span>`
      : `<span class="transit-status running">${posLabel}${esc(j.message || j.status)} ${j.pct}%</span>`;
    jobRows += `<div class="transit-job-row">${esc(j.basename)} — ${statusLabel}${pctBar}</div>`;
  }

  // --- YOLO confirmed-only toggle ---
  const yoloActive    = yoloConfirmedOnly[sessionName] ?? true;
  const displayEvents = yoloActive
    ? events.filter(ev => ev.yolo_label != null)
    : events;

  const yoloToggle = `<label class="yolo-filter-label">
       <input type="checkbox" ${yoloActive ? 'checked' : ''}
              onchange="setYoloFilter('${sn_js}', this.checked)">
       confirmed only
     </label>`;

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

// ── Multi-image thumb navigation ──────────────────────────────────────────────

function thumbStep(btn, delta, event) {
  event?.stopPropagation();
  const wrap  = btn.closest('.card-thumb-wrap');
  const imgs  = JSON.parse(wrap.dataset.images);
  let   idx   = (+wrap.dataset.idx + delta + imgs.length) % imgs.length;
  wrap.dataset.idx = idx;
  wrap.querySelector('.card-thumb').src = `/api/image?path=${encodeURIComponent(imgs[idx])}`;
  wrap.querySelector('.thumb-counter').textContent = `${idx + 1} / ${imgs.length}`;
}

// ── Lightbox ──────────────────────────────────────────────────────────────────

let _lbImages = [];
let _lbIdx    = 0;

function _ensureLightbox() {
  if (document.getElementById('lightbox')) return;
  const lb = document.createElement('div');
  lb.id = 'lightbox';
  lb.innerHTML = `
    <div id="lb-backdrop"></div>
    <div id="lb-shell">
      <button id="lb-close" title="Close (Esc)">✕</button>
      <button id="lb-prev"  title="Previous (←)">&#8249;</button>
      <img    id="lb-img"   alt="">
      <button id="lb-next"  title="Next (→)">&#8250;</button>
      <div id="lb-footer">
        <span id="lb-counter"></span>
        <span id="lb-filename"></span>
        <a    id="lb-download" download title="Download">⬇</a>
      </div>
    </div>`;
  document.body.appendChild(lb);

  document.getElementById('lb-backdrop').addEventListener('click', closeLightbox);
  document.getElementById('lb-close').addEventListener('click', closeLightbox);
  document.getElementById('lb-prev').addEventListener('click', () => _lbNav(-1));
  document.getElementById('lb-next').addEventListener('click', () => _lbNav(+1));

  document.addEventListener('keydown', e => {
    if (!document.getElementById('lightbox').classList.contains('open')) return;
    if (e.key === 'Escape')     closeLightbox();
    if (e.key === 'ArrowLeft')  _lbNav(-1);
    if (e.key === 'ArrowRight') _lbNav(+1);
  });
}

function openLightbox(images, startIdx) {
  _ensureLightbox();
  _lbImages = images;
  _lbIdx    = startIdx ?? 0;
  _lbShow();
  document.getElementById('lightbox').classList.add('open');
}

function closeLightbox() {
  document.getElementById('lightbox')?.classList.remove('open');
}

function _lbNav(delta) {
  _lbIdx = (_lbIdx + delta + _lbImages.length) % _lbImages.length;
  _lbShow();
}

function _lbShow() {
  const path = _lbImages[_lbIdx];
  const url  = `/api/image?path=${encodeURIComponent(path)}`;
  const name = path.split(/[\\/]/).pop();
  document.getElementById('lb-img').src        = url;
  document.getElementById('lb-counter').textContent =
    _lbImages.length > 1 ? `${_lbIdx + 1} / ${_lbImages.length}` : '';
  document.getElementById('lb-filename').textContent = name;
  const dl = document.getElementById('lb-download');
  dl.href     = url;
  dl.download = name;
  document.getElementById('lb-prev').style.display = _lbImages.length > 1 ? '' : 'none';
  document.getElementById('lb-next').style.display = _lbImages.length > 1 ? '' : 'none';
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
