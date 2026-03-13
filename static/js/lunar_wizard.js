'use strict';

// ── State ──────────────────────────────────────────────────────────────────────

let _lwMode       = 'phase';   // 'phase' | 'single'
let _lwSessions   = [];        // [{object_name, paths, dates, num_videos, illum_pct, age_days, phase_name}, ...]
let _lwFiles      = [];        // single-session file list [{path, name, date, duration_s, selected}]
let _lwDir        = '';
let _lwJobId      = null;
let _lwPollTimer  = null;
let _lwStartTime  = null;
let _lwElapsedTimer = null;

const _LW_STORE_KEY = 'seestar_lunar_job';

function _saveJob(jobId, mode) {
  try { localStorage.setItem(_LW_STORE_KEY, JSON.stringify({jobId, mode, t: Date.now()})); } catch (_) {}
}
function _saveDone(outputs, mode) {
  try { localStorage.setItem(_LW_STORE_KEY, JSON.stringify({done: true, outputs, mode, t: Date.now()})); } catch (_) {}
}
function _clearJob() {
  try { localStorage.removeItem(_LW_STORE_KEY); } catch (_) {}
}

// ── Reconnect to any in-progress job from a previous page load ────────────────
(async function _maybeReconnect() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(_LW_STORE_KEY) || 'null'); } catch (_) { return; }
  if (!saved) return;
  if (Date.now() - (saved.t || 0) > 30 * 24 * 3600 * 1000) { _clearJob(); return; }

  // Completed results saved locally — show without hitting the API
  if (saved.done) {
    _lwMode = saved.mode || 'phase';
    document.getElementById('lw-phase-panel').style.display  = 'none';
    document.getElementById('lw-single-panel').style.display = 'none';
    document.getElementById('lw-render-panel').style.display = '';
    document.getElementById('lw-render-title').textContent   =
      _lwMode === 'phase' ? 'Phase Sequence Results' : 'Single Session Results';
    document.getElementById('lw-progress-wrap').style.display = 'none';
    document.getElementById('lw-log-header').style.display    = 'none';
    _showResults(saved.outputs || {});
    return;
  }

  if (!saved.jobId) return;
  // In-progress job — probe server (4 h staleness cutoff for running jobs)
  if (Date.now() - (saved.t || 0) > 4 * 3600 * 1000) { _clearJob(); return; }

  try {
    const res  = await fetch(`/api/lunar/status?job_id=${saved.jobId}`);
    const data = await res.json();
    if (data.error || data.status === 'error') { _clearJob(); return; }

    _lwJobId    = saved.jobId;
    _lwMode     = saved.mode || 'phase';

    document.getElementById('lw-phase-panel').style.display  = 'none';
    document.getElementById('lw-single-panel').style.display = 'none';
    document.getElementById('lw-render-panel').style.display = '';
    document.getElementById('lw-render-title').textContent   =
      _lwMode === 'phase' ? 'Rendering Phase Sequence…' : 'Rendering Single Session…';

    if (data.status === 'done') {
      _saveDone(data.outputs || {}, _lwMode);
      document.getElementById('lw-progress-wrap').style.display = 'none';
      document.getElementById('lw-log-header').style.display    = 'none';
      _showResults(data.outputs || {});
      return;
    }
    _lwStartTime = saved.t;
    _startElapsedClock();
    document.getElementById('lw-log-header').style.display = '';
    _updateProgress(data);   // show current message/log immediately
    _pollStatus();
  } catch (_) {}
})();

// ── Init ────────────────────────────────────────────────────────────────────────

// Pre-populate date range with last 30 days
(function _initDateRange() {
  const now = new Date();
  const to  = now.toISOString().slice(0, 10);
  now.setDate(now.getDate() - 30);
  const from = now.toISOString().slice(0, 10);
  document.getElementById('lw-from-date').value = from;
  document.getElementById('lw-to-date').value   = to;
})();

// Populate single-session quick-pick from known lunar sessions in DB
(async function _loadLunarQuickPick() {
  try {
    const res  = await fetch('/api/sessions');
    const data = await res.json();
    const lunar = (data || []).filter(s =>
      s.object_type === 'lunar' || s.object_type === 'Lunar'
    );
    if (!lunar.length) return;
    const hint = document.getElementById('lw-sessions-hint');
    const list = document.getElementById('lw-sessions-quick-list');
    hint.style.display = 'block';
    lunar.forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm lw-session-btn';
      btn.textContent = s.object_name;
      btn.title = (s.paths || []).join(', ');
      btn.addEventListener('click', () => {
        const dir = (s.paths || [])[0] || '';
        if (dir) {
          document.getElementById('lw-dir-input').value = dir;
          _lwDir = dir;
          _scanDirectory(dir);
        }
      });
      list.appendChild(btn);
    });
  } catch (_) {}
})();

// ── Mode tabs ──────────────────────────────────────────────────────────────────

document.getElementById('lw-tab-phase').addEventListener('click', () => _setMode('phase'));
document.getElementById('lw-tab-single').addEventListener('click', () => _setMode('single'));

function _setMode(mode) {
  _lwMode = mode;
  document.getElementById('lw-tab-phase').classList.toggle('active',  mode === 'phase');
  document.getElementById('lw-tab-single').classList.toggle('active', mode === 'single');
  document.getElementById('lw-phase-panel').style.display  = mode === 'phase'  ? '' : 'none';
  document.getElementById('lw-single-panel').style.display = mode === 'single' ? '' : 'none';
  document.getElementById('lw-render-panel').style.display = 'none';
}

// ── Phase Sequence — load sessions ─────────────────────────────────────────────

document.getElementById('lw-load-sessions-btn').addEventListener('click', _loadSessions);

async function _loadSessions() {
  const from = document.getElementById('lw-from-date').value;
  const to   = document.getElementById('lw-to-date').value;
  const status = document.getElementById('lw-sessions-status');
  status.textContent = 'Loading sessions…';
  status.className   = 'lw-scan-status lw-scan-info';
  document.getElementById('lw-sessions-wrap').style.display  = 'none';
  document.getElementById('lw-phase-params').style.display   = 'none';

  try {
    const url = `/api/lunar/sessions?from=${from}&to=${to}`;
    const res  = await fetch(url);
    const data = await res.json();
    if (data.error) { _showSessionsError(data.error); return; }

    _lwSessions = (data.sessions || []).map(s => ({ ...s, selected: true }));
    if (!_lwSessions.length) {
      _showSessionsError('No lunar sessions found in that date range.');
      return;
    }
    status.textContent = '';
    _renderSessionsTable();
    document.getElementById('lw-phase-params').style.display = 'block';
    // Show output dir hint
    document.getElementById('lw-output-note').textContent =
      `Output → ${data.output_dir || ''}`;
  } catch (e) {
    _showSessionsError('Network error: ' + e);
  }
}

function _showSessionsError(msg) {
  const status = document.getElementById('lw-sessions-status');
  status.textContent = '⚠ ' + msg;
  status.className   = 'lw-scan-status lw-scan-error';
}

function _renderSessionsTable() {
  const tbody = document.getElementById('lw-sessions-tbody');
  tbody.innerHTML = '';

  _lwSessions.forEach((s, i) => {
    const tr = document.createElement('tr');
    const dates = (s.dates || []).join(', ') || s.date_range || '—';
    const illum = s.illum_pct != null ? `${Math.round(s.illum_pct)}%` : '—';
    const phase = s.phase_name || '—';
    const vids  = s.num_videos || 0;
    tr.innerHTML = `
      <td class="col-check">
        <input type="checkbox" class="lw-sess-check" data-idx="${i}" ${s.selected ? 'checked' : ''} />
      </td>
      <td class="col-date">${_firstDate(s)}</td>
      <td class="col-name">${s.object_name}</td>
      <td class="col-phase">
        <span class="lw-phase-pill" style="${_phasePillStyle(s.illum_pct)}">
          ${phase} ${illum}
        </span>
      </td>
      <td class="col-vids">${vids}</td>`;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('.lw-sess-check').forEach(cb => {
    cb.addEventListener('change', e => {
      _lwSessions[+e.target.dataset.idx].selected = e.target.checked;
    });
  });

  document.getElementById('lw-select-all-sessions').checked = true;
  const n = _lwSessions.length;
  document.getElementById('lw-session-count').textContent =
    `${n} session${n !== 1 ? 's' : ''} found`;
  document.getElementById('lw-sessions-wrap').style.display = 'block';
}

function _firstDate(s) {
  const dates = s.dates || [];
  return dates.length ? dates[0] : (s.date_range || '—');
}

function _phasePillStyle(illum) {
  // amber glow for crescent/gibbous, bright for full
  if (illum == null) return '';
  const p = illum / 100;
  const r = Math.round(180 + p * 75);
  const g = Math.round(140 + p * 60);
  const b = Math.round(60  + p * 30);
  return `background:rgb(${r},${g},${b},0.15);color:rgb(${r},${g},${b})`;
}

document.getElementById('lw-select-all-sessions').addEventListener('change', e => {
  const checked = e.target.checked;
  _lwSessions.forEach(s => s.selected = checked);
  document.querySelectorAll('.lw-sess-check').forEach(cb => cb.checked = checked);
});

// ── Single Session — directory scan ───────────────────────────────────────────

document.getElementById('lw-scan-btn').addEventListener('click', () => {
  const dir = document.getElementById('lw-dir-input').value.trim();
  if (dir) _scanDirectory(dir);
});

document.getElementById('lw-dir-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const dir = document.getElementById('lw-dir-input').value.trim();
    if (dir) _scanDirectory(dir);
  }
});

async function _scanDirectory(dir) {
  _lwDir = dir;
  const status = document.getElementById('lw-scan-status');
  status.textContent = 'Scanning…';
  status.className   = 'lw-scan-status lw-scan-info';
  document.getElementById('lw-files-wrap').style.display = 'none';

  try {
    const res  = await fetch('/api/lunar/scan', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ directory: dir }),
    });
    const data = await res.json();
    if (data.error) { _showScanError(data.error); return; }

    _lwFiles = (data.files || []).map(f => ({ ...f, selected: true }));
    if (!_lwFiles.length) {
      _showScanError('No lunar video files found in that directory.');
      return;
    }
    status.textContent = '';
    _renderFileTable();
  } catch (e) {
    _showScanError('Network error: ' + e);
  }
}

function _showScanError(msg) {
  const status = document.getElementById('lw-scan-status');
  status.textContent = '⚠ ' + msg;
  status.className   = 'lw-scan-status lw-scan-error';
}

function _renderFileTable() {
  const tbody = document.getElementById('lw-file-tbody');
  tbody.innerHTML = '';

  _lwFiles.forEach((f, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="col-check">
        <input type="checkbox" class="lw-file-check" data-idx="${i}" ${f.selected ? 'checked' : ''} />
      </td>
      <td class="col-name" title="${f.path}">${f.name}</td>
      <td class="col-date">${f.date || '—'}</td>
      <td class="col-dur">${f.duration_s ? _fmtDur(f.duration_s) : '—'}</td>`;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('.lw-file-check').forEach(cb => {
    cb.addEventListener('change', e => {
      _lwFiles[+e.target.dataset.idx].selected = e.target.checked;
      _updateRenderBtn();
    });
  });

  document.getElementById('lw-select-all-files').checked = true;
  const n = _lwFiles.length;
  document.getElementById('lw-file-count').textContent =
    `${n} video${n !== 1 ? 's' : ''} found`;
  document.getElementById('lw-files-wrap').style.display = 'block';
  _updateRenderBtn();
}

document.getElementById('lw-select-all-files').addEventListener('change', e => {
  const checked = e.target.checked;
  _lwFiles.forEach(f => f.selected = checked);
  document.querySelectorAll('.lw-file-check').forEach(cb => cb.checked = checked);
  _updateRenderBtn();
});

function _updateRenderBtn() {
  const any = _lwFiles.some(f => f.selected);
  document.getElementById('lw-single-render-btn').disabled = !any;
}

function _fmtDur(s) {
  if (s < 60) return `${Math.round(s)}s`;
  const m   = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}m ${sec}s`;
}

// ── Slider ↔ number sync ───────────────────────────────────────────────────────

[
  ['lw-phase-gamma-range',    'lw-phase-gamma'],
  ['lw-phase-high-pct-range', 'lw-phase-high-pct'],
  ['lw-phase-sky-pct-range',  'lw-phase-sky-pct'],
  ['lw-gamma-range',          'lw-gamma'],
  ['lw-high-pct-range',       'lw-high-pct'],
  ['lw-sky-pct-range',        'lw-sky-pct'],
].forEach(([rangeId, numId]) => {
  const range = document.getElementById(rangeId);
  const num   = document.getElementById(numId);
  if (!range || !num) return;
  range.addEventListener('input', () => { num.value = range.value; });
  num.addEventListener('input',   () => { range.value = num.value; });
});

// ── Render ─────────────────────────────────────────────────────────────────────

document.getElementById('lw-phase-render-btn').addEventListener('click', _startPhaseRender);
document.getElementById('lw-single-render-btn').addEventListener('click', _startSingleRender);
document.getElementById('lw-rerender-btn').addEventListener('click', () => {
  _clearJob();
  _clearResults();
  document.getElementById('lw-render-panel').style.display = 'none';
  document.getElementById('lw-phase-panel').style.display  = _lwMode === 'phase'  ? '' : 'none';
  document.getElementById('lw-single-panel').style.display = _lwMode === 'single' ? '' : 'none';
});
document.getElementById('lw-cancel-btn').addEventListener('click', () => _cancelLunarRender(false));
document.getElementById('lw-back-to-params-btn').addEventListener('click', () => _cancelLunarRender(true));

async function _cancelLunarRender(andGoBack) {
  _stopElapsedClock();
  if (_lwPollTimer) { clearTimeout(_lwPollTimer); _lwPollTimer = null; }

  if (_lwJobId) {
    try {
      await fetch('/api/lunar/cancel', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({job_id: _lwJobId}),
      });
    } catch (_) {}
    _lwJobId = null;
  }
  _clearJob();

  if (andGoBack) {
    _clearResults();
    document.getElementById('lw-render-panel').style.display = 'none';
    document.getElementById('lw-phase-panel').style.display  = _lwMode === 'phase'  ? '' : 'none';
    document.getElementById('lw-single-panel').style.display = _lwMode === 'single' ? '' : 'none';
  } else {
    _showRenderError('Render cancelled.');
  }
}

async function _startPhaseRender() {
  const selected = _lwSessions.filter(s => s.selected);
  if (!selected.length) return;

  _clearResults();
  document.getElementById('lw-phase-panel').style.display  = 'none';
  document.getElementById('lw-single-panel').style.display = 'none';
  document.getElementById('lw-render-panel').style.display = '';
  document.getElementById('lw-render-title').textContent   = 'Rendering Phase Sequence…';
  _startElapsedClock();
  document.getElementById('lw-log-header').style.display = '';

  const body = {
    mode:       'phase',
    sessions:   selected.map(s => ({
      name:  s.object_name,
      paths: s.paths || [],
      date:  (s.dates || [])[0] || '',
    })),
    size:       +document.getElementById('lw-phase-size').value,
    gamma:      +document.getElementById('lw-phase-gamma').value,
    sky_pct:    +document.getElementById('lw-phase-sky-pct').value,
    high_pct:   +document.getElementById('lw-phase-high-pct').value,
    frame_hold: +document.getElementById('lw-frame-hold').value,
    no_cache:   document.getElementById('lw-phase-no-cache').checked,
  };

  try {
    const res  = await fetch('/api/lunar/render', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { _showRenderError(data.error); return; }
    _lwJobId = data.job_id;
    _saveJob(_lwJobId, 'phase');
    _pollStatus();
  } catch (e) {
    _showRenderError('Network error: ' + e);
  }
}

async function _startSingleRender() {
  const selectedFiles = _lwFiles.filter(f => f.selected).map(f => f.path);
  if (!selectedFiles.length) return;

  _clearResults();
  document.getElementById('lw-phase-panel').style.display  = 'none';
  document.getElementById('lw-single-panel').style.display = 'none';
  document.getElementById('lw-render-panel').style.display = '';
  document.getElementById('lw-render-title').textContent   = 'Rendering Single Session…';
  _startElapsedClock();
  document.getElementById('lw-log-header').style.display = '';

  const body = {
    mode:            'single',
    directory:       _lwDir,
    files:           selectedFiles,
    size:            +document.getElementById('lw-size').value,
    sample_interval: +document.getElementById('lw-sample-interval').value,
    speedup:         +document.getElementById('lw-speedup').value,
    gamma:           +document.getElementById('lw-gamma').value,
    sky_pct:         +document.getElementById('lw-sky-pct').value,
    high_pct:        +document.getElementById('lw-high-pct').value,
    no_cache:        document.getElementById('lw-no-cache').checked,
  };

  try {
    const res  = await fetch('/api/lunar/render', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { _showRenderError(data.error); return; }
    _lwJobId = data.job_id;
    _saveJob(_lwJobId, 'single');
    _pollStatus();
  } catch (e) {
    _showRenderError('Network error: ' + e);
  }
}

// ── Polling ────────────────────────────────────────────────────────────────────

function _pollStatus() {
  if (_lwPollTimer) clearTimeout(_lwPollTimer);
  _lwPollTimer = setTimeout(_fetchStatus, 1500);
}

async function _fetchStatus() {
  if (!_lwJobId) return;
  try {
    const res  = await fetch(`/api/lunar/status?job_id=${_lwJobId}`);
    const data = await res.json();
    _updateProgress(data);
    if (data.status === 'running') _pollStatus();
  } catch (_) {
    _pollStatus();
  }
}

function _updateProgress(data) {
  const bar  = document.getElementById('lw-progress-bar');
  const msg  = document.getElementById('lw-progress-msg');
  const wrap = document.getElementById('lw-progress-wrap');
  const log  = document.getElementById('lw-log');

  bar.style.width = (data.pct || 0) + '%';
  msg.textContent = data.message || '';

  if (data.log && data.log.length) {
    document.getElementById('lw-log-wrap').style.display = 'block';
    log.textContent = data.log.join('\n');
    log.scrollTop   = log.scrollHeight;
  }

  if (data.status === 'done') {
    _saveDone(data.outputs || {}, _lwMode);
    _stopElapsedClock();
    wrap.style.display = 'none';
    _showResults(data.outputs || {});
  } else if (data.status === 'error') {
    _clearJob();
    _stopElapsedClock();
    const acts = document.getElementById('lw-render-actions');
    if (acts) acts.style.display = 'none';
    _showRenderError(data.error || 'Unknown error');
  }
}

// ── Results display ────────────────────────────────────────────────────────────

function _showResults(o) {
  const grid  = document.getElementById('lw-result-grid');
  const label = document.getElementById('lw-result-label');

  label.textContent = o.date_label
    ? `☽ ${o.frame_count} frames · ${o.date_label}`
    : `☽ ${o.frame_count || 0} frames`;

  grid.innerHTML = '';

  if (o.timelapse) {
    const tl_label = o.mode === 'phase' ? '))) Phase timelapse' : '☽ Session timelapse';
    grid.appendChild(_makeVideoCard(tl_label, o.timelapse));
  }
  if (o.portrait) {
    grid.appendChild(_makeImageCard('🖼 Lunar portrait', o.portrait));
  }
  if (o.mosaic) {
    grid.appendChild(_makeImageCard('⊞ Phase mosaic', o.mosaic));
  }

  document.getElementById('lw-results').style.display = 'block';
}

function _makeVideoCard(label, path) {
  const url  = `/api/lunar/output?path=${encodeURIComponent(path)}`;
  const card = document.createElement('div');
  card.className = 'lw-result-card';

  const title   = document.createElement('div');
  title.className   = 'lw-card-title';
  title.textContent = label;

  const video    = document.createElement('video');
  video.className = 'lw-result-video';
  video.src      = url;
  video.controls = true;
  video.loop     = true;
  video.muted    = true;
  video.preload  = 'metadata';

  const dl = _makeDownloadLink(url, path);
  card.append(title, video, dl);
  return card;
}

function _makeImageCard(label, path) {
  const url  = `/api/lunar/output?path=${encodeURIComponent(path)}`;
  const card = document.createElement('div');
  card.className = 'lw-result-card';

  const title   = document.createElement('div');
  title.className   = 'lw-card-title';
  title.textContent = label;

  const img    = document.createElement('img');
  img.className = 'lw-result-img';
  img.src      = url;
  img.alt      = label;
  img.loading  = 'lazy';

  const dl = _makeDownloadLink(url, path);
  card.append(title, img, dl);
  return card;
}

function _makeDownloadLink(url, path) {
  const dl    = document.createElement('a');
  dl.className   = 'lw-card-dl';
  dl.href        = url;
  dl.download    = path.split('/').pop();
  dl.textContent = '⬇ Download';
  return dl;
}

// ── Elapsed timer ──────────────────────────────────────────────────────────────

function _startElapsedClock() {
  _lwStartTime = Date.now();
  if (_lwElapsedTimer) clearInterval(_lwElapsedTimer);
  _lwElapsedTimer = setInterval(() => {
    const el = document.getElementById('lw-elapsed');
    if (!el || !_lwStartTime) return;
    const s   = Math.floor((Date.now() - _lwStartTime) / 1000);
    const m   = Math.floor(s / 60);
    const sec = s % 60;
    el.textContent = m > 0
      ? `${m}m ${sec.toString().padStart(2, '0')}s elapsed`
      : `${sec}s elapsed`;
  }, 1000);
}

function _stopElapsedClock() {
  if (_lwElapsedTimer) { clearInterval(_lwElapsedTimer); _lwElapsedTimer = null; }
}

// ── Error / reset helpers ──────────────────────────────────────────────────────

function _showRenderError(msg) {
  const el = document.getElementById('lw-error');
  el.textContent   = '⚠ ' + msg;
  el.style.display = 'block';
  document.getElementById('lw-progress-wrap').style.display = 'none';
}

function _clearResults() {
  document.getElementById('lw-results').style.display      = 'none';
  document.getElementById('lw-error').style.display        = 'none';
  document.getElementById('lw-log-header').style.display   = 'none';
  document.getElementById('lw-log-wrap').style.display     = 'none';
  document.getElementById('lw-log').textContent            = '';
  document.getElementById('lw-result-grid').innerHTML      = '';
  document.getElementById('lw-progress-bar').style.width   = '0%';
  document.getElementById('lw-progress-msg').textContent   = 'Starting…';
  document.getElementById('lw-elapsed').textContent        = '';
  document.getElementById('lw-progress-wrap').style.display = 'block';
  const acts = document.getElementById('lw-render-actions');
  if (acts) acts.style.display = '';
  if (_lwPollTimer)    { clearTimeout(_lwPollTimer);   _lwPollTimer  = null; }
  if (_lwElapsedTimer) { clearInterval(_lwElapsedTimer); _lwElapsedTimer = null; }
  _lwJobId    = null;
  _lwStartTime = null;
}
