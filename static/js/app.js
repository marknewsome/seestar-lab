'use strict';

// ── Shutdown ───────────────────────────────────────────────────────────────────
async function shutdownApp() {
  if (!confirm('Stop the Seestar Lab server?')) return;
  await fetch('/api/shutdown', { method: 'POST' });
  document.body.innerHTML = '<div style="padding:2rem;font-family:monospace;color:#aaa">Server stopped. You can close this tab.</div>';
}

// ── State ─────────────────────────────────────────────────────────────────────
const sessions          = {};   // object_name → session dict  (source of truth)
const stackData         = {};   // object_name → stack job dict
let activeFilter    = 'all';
let evtSource       = null;
let dbLoaded        = false; // true once we've received the initial DB flush

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
      loadStackData().then(() => {
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

    case 'stack_queued':
      stackData[ev.session_name] = Object.assign(stackData[ev.session_name] || {}, {
        session_name: ev.session_name,
        status: 'pending',
        pct: 0,
        stage: 'Queued…',
        frames_total: ev.fits_count || 0,
        frames_accepted: 0,
      });
      _refreshStackFooter(ev.session_name);
      break;

    case 'stack_progress':
      handleStackProgress(ev);
      break;

    case 'stack_done':
      handleStackDone(ev);
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
    if (el) {
      // Don't rebuild if the user is typing in the notes textarea —
      // just patch the rating dot and notes value quietly instead.
      const textarea = el.querySelector('.session-notes');
      if (textarea && document.activeElement === textarea) {
        const dot = el.querySelector('.rating-dot');
        if (dot) {
          dot.dataset.rating = session.user_rating ?? 'none';
          dot.title = session.user_rating ? RATING_TIPS[session.user_rating] : 'Not rated — click to rate';
        }
      } else {
        el.outerHTML = buildCard(session);
      }
    }
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

async function loadStackData() {
  try {
    const res = await fetch('/api/stack/status');
    if (!res.ok) return;
    Object.assign(stackData, await res.json());
  } catch { /* non-fatal */ }
}

function handleStackProgress(ev) {
  const sn = ev.session_name;
  const update = {
    session_name:    sn,
    status:          ev.status || 'running',
    pct:             ev.pct,
    stage:           ev.stage,
    frames_accepted: ev.frames_accepted,
    frames_total:    ev.frames_total,
  };
  if (ev.status === 'error') update.error_msg = ev.stage;
  stackData[sn] = Object.assign(stackData[sn] || {}, update);
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
    max_frames:      ev.max_frames || stackData[sn]?.max_frames || 500,
  });
  _refreshStackFooter(sn);
}

function _refreshStackFooter(sessionName) {
  const card = document.getElementById(cardId(sessionName));
  if (!card) return;
  const footer = card.querySelector('.stack-footer');
  if (footer) footer.outerHTML = buildStackFooter(sessionName);
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

  const visible = Object.values(sessions).filter(passesFilter);

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

  if (activeFilter === 'all') {
    // Group by catalog prefix (M, NGC, IC, C, …); non-catalog items sort last
    const OTHER = '\uFFFF';
    const groups = {};
    visible.forEach(s => {
      const m = s.object_name.match(/^([A-Za-z]+)\s*\d/);
      const prefix = m ? m[1].toUpperCase() : OTHER;
      (groups[prefix] = groups[prefix] || []).push(s);
    });

    // Within each group sort numerically; _sub follows its base session
    Object.values(groups).forEach(arr => {
      arr.sort((a, b) => {
        const na = parseInt(a.object_name.replace(/\D/g, ''), 10) || 0;
        const nb = parseInt(b.object_name.replace(/\D/g, ''), 10) || 0;
        if (na !== nb) return na - nb;
        return (a.object_name.endsWith('_sub') ? 1 : 0) - (b.object_name.endsWith('_sub') ? 1 : 0);
      });
    });

    // Sort groups alphabetically; non-catalog group goes last
    const sortedPrefixes = Object.keys(groups).sort((a, b) => a.localeCompare(b));

    let html = '';
    sortedPrefixes.forEach(prefix => {
      const label = prefix === OTHER ? 'Other' : prefix;
      html += `<div class="catalog-group-header">${label}</div>`;
      html += groups[prefix].map(buildCard).join('');
    });
    grid.innerHTML = html;
  } else {
    visible.sort((a, b) => {
      if (isCatalogFilter) {
        const na = parseInt(a.object_name.replace(/\D/g, ''), 10);
        const nb = parseInt(b.object_name.replace(/\D/g, ''), 10);
        if (na !== nb) return na - nb;
        return (a.object_name.endsWith('_sub') ? 1 : 0) - (b.object_name.endsWith('_sub') ? 1 : 0);
      }
      const da = a.dates[a.dates.length - 1] || '';
      const db = b.dates[b.dates.length - 1] || '';
      return db.localeCompare(da);
    });
    grid.innerHTML = visible.map(buildCard).join('');
  }

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

const RATING_CYCLE = [null, 'satisfied', 'want_more', 'priority'];
const RATING_TIPS  = { satisfied: 'Satisfied', want_more: 'Want more time', priority: 'Priority re-image' };

async function saveSessionNotes(sessionName, text) {
  if (sessions[sessionName]) sessions[sessionName].notes = text || null;
  try {
    await fetch(`/api/session/${encodeURIComponent(sessionName)}/notes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: text }),
    });
  } catch { /* ignore */ }
}

async function cycleSessionRating(sessionName) {
  const s = sessions[sessionName];
  if (!s) return;
  const cur = s.user_rating ?? null;
  const idx = RATING_CYCLE.indexOf(cur);
  const next = RATING_CYCLE[(idx + 1) % RATING_CYCLE.length];
  s.user_rating = next;
  // Update dot immediately (optimistic)
  const dot = document.querySelector(`.rating-dot[data-session="${CSS.escape(sessionName)}"]`);
  if (dot) {
    dot.dataset.rating = next ?? 'none';
    dot.title = next ? RATING_TIPS[next] : 'Not rated — click to rate';
  }
  try {
    await fetch(`/api/session/${encodeURIComponent(sessionName)}/rate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: next }),
    });
  } catch { /* ignore */ }
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
  const hasThumbnail = !imgs && s.thumbnail;
  const imgUrl0 = imgs
    ? `/api/image?path=${encodeURIComponent(imgs[0])}`
    : (hasThumbnail ? `/api/thumbnail/${encodeURIComponent(s.object_name)}` : null);
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
        const pinned    = s.pinned_thumbnail ? ' thumb-pinned' : '';
        const pickBtn   = hasThumbnail
          ? `<button class="thumb-pick-btn" title="Choose thumbnail"
               onclick="openThumbPicker('${s.object_name.replace(/'/g,"\\'")}',event)">⊞</button>`
          : '';
        return `<div class="card-thumb-wrap"${imgsAttr}>
          <img class="card-thumb${imgs ? ' thumb-clickable' : ''}${pinned}" src="${imgUrl0}"
               alt="${esc(s.object_name)} preview" ${clickAttr}
               onerror="this.closest('.card-thumb-wrap').style.display='none'">
          ${arrows}
          ${pickBtn}
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

  // Comets get their own footer; exclude from stack to avoid false _sub match
  const isSubSession  = !isComet && s.object_name.endsWith('_sub') && s.num_subs > 0;
  const stackFooter   = isSubSession ? buildStackFooter(s.object_name) : '';
  const cometFooter   = isComet ? buildCometFooter(s) : '';

  // Placeholder filled async by loadCometInfo()
  const cometInfoRow  = isComet
    ? `<div class="comet-fullname-row" id="comet-info-${cardId(s.object_name).slice(5)}"></div>`
    : '';

  const ratingVal = s.user_rating ?? null;
  const ratingTip = ratingVal ? RATING_TIPS[ratingVal] : 'Not rated — click to rate';
  const ratingDot = `<span class="rating-dot"
      data-rating="${ratingVal ?? 'none'}"
      data-session="${esc(s.object_name)}"
      title="${esc(ratingTip)}"
      onclick="cycleSessionRating('${s.object_name.replace(/'/g, "\\'")}')"></span>`;

  const safeObjName = s.object_name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const notesHtml = `<textarea class="session-notes" rows="2"
      placeholder="Observing notes (conditions, issues, goals…)"
      onblur="saveSessionNotes('${safeObjName}', this.value)"
      onkeydown="if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();this.blur();}"
      >${esc(s.notes ?? '')}</textarea>`;

  return `
    <div class="session-card" id="${cardId(s.object_name)}">
      ${thumbHtml}
      <div class="card-header">
        <div class="object-name">${esc(s.object_name)}</div>
        ${badge}
        ${ratingDot}
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
      ${notesHtml}
      ${stackFooter}
      ${cometFooter}
    </div>`;
}

// ── Stack footer ──────────────────────────────────────────────────────────────

async function queueStack(sessionName, force = false) {
  const sfx         = cardId(sessionName).slice(5);
  const btn         = document.getElementById(`stack-btn-${sfx}`);
  const mfEl        = document.getElementById(`stack-mf-${sfx}`);
  const cacheEl     = document.getElementById(`stack-cache-${sfx}`);
  const bgEl        = document.getElementById(`stack-bg-${sfx}`);
  const mqEl        = document.getElementById(`stack-mq-${sfx}`);
  const maxFrames   = mfEl   ? (parseInt(mfEl.value,   10) || 500) : 500;
  const useCache    = cacheEl ? cacheEl.checked : false;
  const bgMeshScale = bgEl   ? (parseInt(bgEl.value,   10))        : 20;
  const minQuality  = mqEl   ? (parseFloat(mqEl.value) || 0.0)     : 0.0;
  if (btn) { btn.disabled = true; btn.textContent = 'Queuing…'; }
  try {
    const res  = await fetch('/api/stack/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ session_name: sessionName, force,
                                max_frames: maxFrames, use_cache: useCache,
                                bg_mesh_scale: bgMeshScale,
                                min_quality: minQuality }),
    });
    const body = await res.json();
    if (!res.ok) {
      alert(`Stack error: ${body.error || res.status}`);
      if (btn) { btn.disabled = false; btn.textContent = 'Stack'; }
      return;
    }
    // Optimistically set state so the footer re-renders immediately
    stackData[sessionName] = Object.assign(stackData[sessionName] || {}, {
      session_name:  sessionName,
      status:        'pending',
      pct:           0,
      stage:         'Queued…',
      frames_total:  body.fits_count || 0,
      frames_accepted: 0,
      max_frames:    body.max_frames || maxFrames,
      bg_mesh_scale: bgMeshScale,
      min_quality:   minQuality,
    });
    _refreshStackFooter(sessionName);
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Stack'; }
  }
}

async function cancelStack(sessionName) {
  try {
    await fetch('/api/stack/cancel', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ session_name: sessionName }),
    });
  } catch { /* ignore — server will broadcast cancelled state via SSE */ }
}

async function rerenderStack(sessionName) {
  const sfx   = sessionName.replace(/[^a-z0-9]/gi, '_');
  const saved = stackInputState[sessionName] || {};
  const body  = {
    bg_mesh_scale: parseInt(document.getElementById('stack-bg-' + sfx)?.value ?? saved.bg ?? 20, 10),
  };
  try {
    await fetch('/api/stack/rerender/' + encodeURIComponent(sessionName), {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
  } catch { /* SSE will report progress */ }
}

// Track which cards have the options panel open (survives footer rebuilds)
const stackOptionsOpen = new Set();

// Persist user-entered input values across footer rebuilds so SSE events
// don't silently reset what the user typed.
const stackInputState = {};
function _saveStackInput(sessionName, key, val) {
  if (!stackInputState[sessionName]) stackInputState[sessionName] = {};
  stackInputState[sessionName][key] = val;
}

function toggleStackOptions(sessionName) {
  if (stackOptionsOpen.has(sessionName)) stackOptionsOpen.delete(sessionName);
  else stackOptionsOpen.add(sessionName);
  _refreshStackFooter(sessionName);
}

function buildStackFooter(sessionName) {
  const job      = stackData[sessionName];
  const sn_js    = sessionName.replace(/'/g, "\\'");
  const sfx      = sessionName.replace(/[^a-z0-9]/gi, '_');
  const status   = job?.status;
  const isQueued  = status === 'pending';
  const isRunning = status === 'running';
  const isActive  = isQueued || isRunning;
  const isDone    = status === 'done';
  const isError   = status === 'error';
  const optOpen   = stackOptionsOpen.has(sessionName);

  // ── Input values: prefer what the user last typed over job-record defaults ─
  const saved = stackInputState[sessionName] || {};
  const mfVal = saved.mf ?? job?.max_frames    ?? 500;
  const bgVal = saved.bg ?? job?.bg_mesh_scale ?? 20;
  const mqVal = saved.mq ?? job?.min_quality   ?? 0.0;

  // ── Primary row controls ──────────────────────────────────────────────────
  const frameInfo = isDone
    ? `<span class="stack-frame-info">${job.frames_accepted}/${job.frames_total} frames</span>`
    : '';

  const mfInput = (!isActive)
    ? `<label class="stack-mf-label" title="Best N frames to use (quality-ranked)">
         <input id="stack-mf-${sfx}" class="stack-mf-input" type="number"
                value="${mfVal}" min="10" max="9999" step="50"
                oninput="_saveStackInput('${sn_js}','mf',this.value)" />
         frames
       </label>`
    : `<span class="stack-mf-label">${job?.frames_accepted || mfVal}/${job?.frames_total || '?'} frames</span>`;

  const gearBtn = (!isActive)
    ? `<button class="btn-stack-gear ${optOpen ? 'active' : ''}"
               onclick="toggleStackOptions('${sn_js}')"
               title="Stacking options">⚙</button>`
    : '';

  const stackBtn = isRunning
    ? `<button id="stack-btn-${sfx}" class="btn-stack" disabled>Stacking…</button>`
    : isQueued
    ? `<button id="stack-btn-${sfx}" class="btn-stack" disabled>Queued…</button>`
    : (!isDone && !isError)
    ? `<button id="stack-btn-${sfx}" class="btn-stack"
               onclick="queueStack('${sn_js}')">Stack</button>`
    : '';

  const rerenderBtn = isDone
    ? `<button class="btn-stack-rerun"
               onclick="rerenderStack('${sn_js}')"
               title="Re-apply background subtraction + AI denoising + stretch">↻ Re-render</button>`
    : '';

  const restackBtn = (isDone || isError)
    ? `<button class="btn-stack-rerun"
               onclick="queueStack('${sn_js}', true)"
               title="Re-stack with current settings">↻ Re-stack</button>`
    : '';

  const cancelBtn = isRunning
    ? `<button class="btn-stack-cancel"
               onclick="cancelStack('${sn_js}')"
               title="Stop stacking after current frame">Cancel</button>`
    : '';

  // ── Options panel (gear toggle) ───────────────────────────────────────────
  const optionsPanel = (!isActive && optOpen) ? `
    <div class="stack-options-panel">
      <label class="stack-mf-label" title="Quality floor: frames scoring below this fraction of the best frame are rejected even if max_frames would include them. 0 = off, 0.5 = top half only.">
        <input id="stack-mq-${sfx}" class="stack-mf-input" type="number"
               value="${mqVal}" min="0" max="0.95" step="0.05"
               oninput="_saveStackInput('${sn_js}','mq',this.value)" />
        min qual
      </label>
      <label class="stack-mf-label" title="Background mesh scale: higher = coarser (large galaxies like M101); lower = finer (compact nebulae). 0 = skip subtraction.">
        <input id="stack-bg-${sfx}" class="stack-mf-input" type="number"
               value="${bgVal}" min="0" max="60" step="2"
               oninput="_saveStackInput('${sn_js}','bg',this.value)" />
        bg scale
      </label>
      ${(isDone || isError) ? `<label class="stack-cache-label" title="Reuse aligned frames from the last run — much faster re-stack">
        <input type="checkbox" id="stack-cache-${sfx}" class="stack-cache-input" checked />
        skip copy
      </label>` : ''}
    </div>` : '';

  // ── Progress / queued indicator ───────────────────────────────────────────
  let progressRow = '';
  if (isRunning) {
    const pct    = job.pct || 0;
    const stage  = esc(job.stage || 'Working…');
    const counts = job.frames_total > 0
      ? ` · ${job.frames_accepted || 0}/${job.frames_total}` : '';
    progressRow = `
      <div class="stack-progress-wrap">
        <div class="stack-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="stack-stage">${stage}${esc(counts)} ${pct}%</div>`;
  } else if (isQueued) {
    progressRow = `<div class="stack-queued-row">⏳ Queued — waiting for active stack to finish</div>`;
  }

  // ── Result thumbnail ──────────────────────────────────────────────────────
  const wizardUrl = `/stack/wizard/${encodeURIComponent(sessionName)}`;
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
        <a class="stack-log-link"
           href="/api/stack/log/${encodeURIComponent(sessionName)}"
           target="_blank">View run log</a>
        <a class="stack-log-link"
           href="${wizardUrl}">Stack Wizard →</a>
      </div>`;
  }

  const errorRow = isError
    ? `<div class="stack-error">Error: ${esc(job.error_msg || 'unknown')}</div>`
    : '';

  const wizardLink = !isActive
    ? `<a class="btn-stack-wizard" href="${wizardUrl}" title="Open full stacking wizard with all controls">Wizard →</a>`
    : '';

  return `<div class="stack-footer">
    <div class="stack-header-row">
      <span class="stack-label">Stacking</span>
      ${frameInfo}
      <div class="stack-btn-group">
        ${mfInput}
        ${gearBtn}
        ${rerenderBtn}
        ${restackBtn}
        ${cancelBtn}
        ${stackBtn}
        ${wizardLink}
      </div>
    </div>
    ${optionsPanel}
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

// ── Thumbnail picker ──────────────────────────────────────────────────────────

function _ensureThumbPicker() {
  if (document.getElementById('thumb-picker')) return;
  const el = document.createElement('div');
  el.id = 'thumb-picker';
  el.innerHTML = `
    <div id="tp-backdrop"></div>
    <div id="tp-panel">
      <div id="tp-header">
        <span id="tp-title">Choose thumbnail</span>
        <button id="tp-close" title="Close (Esc)">✕</button>
      </div>
      <div id="tp-grid"></div>
    </div>`;
  document.body.appendChild(el);
  document.getElementById('tp-backdrop').addEventListener('click', _closeThumbPicker);
  document.getElementById('tp-close').addEventListener('click', _closeThumbPicker);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('thumb-picker').classList.contains('open'))
      _closeThumbPicker();
  });
}

function _closeThumbPicker() {
  document.getElementById('thumb-picker')?.classList.remove('open');
}

async function openThumbPicker(sessionName, event) {
  event?.stopPropagation();
  _ensureThumbPicker();
  const picker = document.getElementById('thumb-picker');
  const grid   = document.getElementById('tp-grid');
  document.getElementById('tp-title').textContent = `Choose thumbnail — ${sessionName}`;
  grid.innerHTML = '<div class="tp-loading">Loading…</div>';
  picker.classList.add('open');

  let images, pinned;
  try {
    const res = await fetch(`/api/session/${encodeURIComponent(sessionName)}/images`);
    if (!res.ok) throw new Error(res.status);
    ({ images, pinned } = await res.json());
  } catch {
    grid.innerHTML = '<div class="tp-loading">Failed to load images.</div>';
    return;
  }

  if (!images.length) {
    grid.innerHTML = '<div class="tp-loading">No images found in this session.</div>';
    return;
  }

  const sn_js = sessionName.replace(/'/g, "\\'");
  grid.innerHTML = images.map(path => {
    const url      = `/api/image?path=${encodeURIComponent(path)}`;
    const fname    = path.split('/').pop();
    const isActive = path === pinned;
    return `<div class="tp-item${isActive ? ' tp-active' : ''}" title="${esc(fname)}">
      <img src="${url}" alt="${esc(fname)}" loading="lazy"
           onerror="this.style.opacity='0.2'">
      <div class="tp-item-overlay">
        <button class="tp-pin-btn" onclick="_pinThumbnail('${sn_js}','${path.replace(/'/g,"\\'")}')">
          ${isActive ? '✓ Pinned' : 'Pin'}
        </button>
      </div>
      ${isActive ? '<div class="tp-check">✓</div>' : ''}
    </div>`;
  }).join('');

  // Clear-pin button at the bottom if one is set
  const clearRow = pinned
    ? `<div id="tp-clear-row">
         <button id="tp-clear-btn" onclick="_pinThumbnail('${sn_js}', null)">
           Clear pin (revert to auto)
         </button>
       </div>`
    : '';
  grid.insertAdjacentHTML('beforeend', clearRow);
}

async function _pinThumbnail(sessionName, path) {
  try {
    await fetch(`/api/session/${encodeURIComponent(sessionName)}/pin-thumbnail`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ path }),
    });
  } catch { return; }

  // Update local state and refresh the card image immediately
  if (sessions[sessionName]) sessions[sessionName].pinned_thumbnail = path;
  const card = document.getElementById(cardId(sessionName));
  if (card) {
    const img = card.querySelector('.card-thumb');
    if (img) {
      img.src = `/api/thumbnail/${encodeURIComponent(sessionName)}?_=${Date.now()}`;
      img.classList.toggle('thumb-pinned', !!path);
    }
  }
  _closeThumbPicker();
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
