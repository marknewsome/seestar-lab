'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

let _swDir      = '';
let _swFiles    = [];   // [{path, name, date, duration_s, selected}]
let _swJobId    = null;
let _swPollTimer = null;
let _swStartTime = null;
let _swElapsedTimer = null;

const _SW_LAST_DIR_KEY = 'seestar_solar_last_dir';

function _swStoreKey(dir) { return 'seestar_solar:' + (dir || _swDir); }

function _loadSaved(dir) {
  try {
    const s = JSON.parse(localStorage.getItem(_swStoreKey(dir)) || 'null');
    if (!s) return null;
    if (Date.now() - (s.t || 0) > 30 * 24 * 3600 * 1000) { _clearJob(dir); return null; }
    return s;
  } catch (_) { return null; }
}
function _saveJob(jobId) {
  try {
    localStorage.setItem(_swStoreKey(), JSON.stringify({jobId, t: Date.now()}));
    localStorage.setItem(_SW_LAST_DIR_KEY, _swDir);
  } catch (_) {}
}
function _saveDone(outputs) {
  try {
    localStorage.setItem(_swStoreKey(), JSON.stringify({done: true, outputs, t: Date.now()}));
    localStorage.setItem(_SW_LAST_DIR_KEY, _swDir);
  } catch (_) {}
}
function _clearJob(dir) {
  try { localStorage.removeItem(_swStoreKey(dir)); } catch (_) {}
}

// ── Reconnect to any in-progress job from a previous page load ────────────────
(async function _maybeReconnect() {
  let lastDir;
  try { lastDir = localStorage.getItem(_SW_LAST_DIR_KEY); } catch (_) { return; }
  if (!lastDir) return;
  const saved = _loadSaved(lastDir);
  if (!saved) return;

  _swDir = lastDir;
  document.getElementById('sw-dir-input').value = lastDir;

  // Completed results saved locally — show without hitting the API
  if (saved.done) {
    _gotoStep(3);
    document.getElementById('sw-progress-wrap').style.display = 'none';
    document.getElementById('sw-log-header').style.display    = 'none';
    _showResults(saved.outputs || {});
    return;
  }

  if (!saved.jobId) return;
  // In-progress job — probe server (4 h staleness cutoff for running jobs)
  if (Date.now() - (saved.t || 0) > 4 * 3600 * 1000) { _clearJob(); return; }

  try {
    const res  = await fetch(`/api/solar/status?job_id=${saved.jobId}`);
    const data = await res.json();
    if (data.error || data.status === 'error') { _clearJob(); return; }
    if (data.status === 'done') {
      _saveDone(data.outputs || {});
      _gotoStep(3);
      document.getElementById('sw-progress-wrap').style.display = 'none';
      _showResults(data.outputs || {});
      return;
    }
    // Still running — reconnect
    _swJobId     = saved.jobId;
    _swStartTime = saved.t;  // elapsed clock from original start
    _gotoStep(3);
    _startElapsedClock();
    document.getElementById('sw-log-header').style.display = '';
    _updateProgress(data);   // show current message/log immediately
    _pollStatus();
  } catch (_) {}
})();

// ── Init ──────────────────────────────────────────────────────────────────────

// Populate sessions quick-pick from known solar sessions
(async function _loadSolarSessions() {
  try {
    const res  = await fetch('/api/sessions');
    const data = await res.json();
    const solar = (data || []).filter(s => s.object_type === 'solar' || s.object_type === 'Solar');
    if (!solar.length) return;

    const hint = document.getElementById('sw-sessions-hint');
    const list = document.getElementById('sw-sessions-list');
    hint.style.display = 'block';

    solar.forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm sw-session-btn';
      const dirs = (s.paths || []).join(', ');
      btn.textContent = s.object_name;
      btn.title = dirs;
      btn.addEventListener('click', () => {
        const dir = (s.paths || [])[0] || '';
        if (dir) {
          document.getElementById('sw-dir-input').value = dir;
          _swDir = dir;
          _scanDirectory(dir);
        }
      });
      list.appendChild(btn);
    });
  } catch (_) {}
})();

// ── Directory scan ────────────────────────────────────────────────────────────

document.getElementById('sw-scan-btn').addEventListener('click', () => {
  const dir = document.getElementById('sw-dir-input').value.trim();
  if (dir) _scanDirectory(dir);
});

document.getElementById('sw-dir-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const dir = document.getElementById('sw-dir-input').value.trim();
    if (dir) _scanDirectory(dir);
  }
});

async function _scanDirectory(dir) {
  _swDir = dir;
  const status = document.getElementById('sw-scan-status');
  status.textContent = 'Scanning…';
  status.className   = 'sw-scan-status sw-scan-info';
  document.getElementById('sw-files-wrap').style.display = 'none';

  try {
    const res  = await fetch('/api/solar/scan', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({directory: dir}),
    });
    const data = await res.json();
    if (data.error) { _showScanError(data.error); return; }

    _swFiles = (data.files || []).map(f => ({...f, selected: true}));
    if (!_swFiles.length) {
      _showScanError('No solar video files found in that directory.');
      return;
    }
    status.textContent = '';
    _renderFileTable();

    // Check for a previous completed render for this directory
    const prev = _loadSaved(dir);
    const notice = document.getElementById('sw-prev-render-notice');
    if (prev && prev.done) {
      notice.style.display = 'flex';
      document.getElementById('sw-prev-render-show-btn').onclick = () => {
        notice.style.display = 'none';
        _gotoStep(3);
        document.getElementById('sw-progress-wrap').style.display = 'none';
        document.getElementById('sw-log-header').style.display    = 'none';
        _showResults(prev.outputs || {});
      };
      document.getElementById('sw-prev-render-dismiss-btn').onclick = () => {
        notice.style.display = 'none';
      };
    } else {
      notice.style.display = 'none';
    }
  } catch (e) {
    _showScanError('Network error: ' + e);
  }
}

function _showScanError(msg) {
  const status = document.getElementById('sw-scan-status');
  status.textContent = '⚠ ' + msg;
  status.className   = 'sw-scan-status sw-scan-error';
}

function _renderFileTable() {
  const tbody = document.getElementById('sw-file-tbody');
  tbody.innerHTML = '';

  _swFiles.forEach((f, i) => {
    const tr   = document.createElement('tr');
    tr.innerHTML = `
      <td class="col-check">
        <input type="checkbox" class="sw-file-check" data-idx="${i}" ${f.selected ? 'checked' : ''} />
      </td>
      <td class="col-name" title="${f.path}">${f.name}</td>
      <td class="col-date">${f.date || '—'}</td>
      <td class="col-dur">${f.duration_s ? _fmtDur(f.duration_s) : '—'}</td>`;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('.sw-file-check').forEach(cb => {
    cb.addEventListener('change', e => {
      _swFiles[+e.target.dataset.idx].selected = e.target.checked;
      _updateToStep2Btn();
    });
  });

  document.getElementById('sw-select-all').checked = true;
  document.getElementById('sw-file-count').textContent =
    `${_swFiles.length} video${_swFiles.length !== 1 ? 's' : ''} found`;
  document.getElementById('sw-files-wrap').style.display = 'block';
  _updateToStep2Btn();
}

document.getElementById('sw-select-all').addEventListener('change', e => {
  const checked = e.target.checked;
  _swFiles.forEach(f => f.selected = checked);
  document.querySelectorAll('.sw-file-check').forEach(cb => cb.checked = checked);
  _updateToStep2Btn();
});

function _updateToStep2Btn() {
  const any = _swFiles.some(f => f.selected);
  document.getElementById('sw-to-step2-btn').disabled = !any;
}

function _fmtDur(s) {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}m ${sec}s`;
}

// ── Step navigation ───────────────────────────────────────────────────────────

document.getElementById('sw-to-step2-btn').addEventListener('click', () => _gotoStep(2));
document.getElementById('sw-back-to-1-btn').addEventListener('click', () => _gotoStep(1));

function _gotoStep(n) {
  [1, 2, 3].forEach(i => {
    document.getElementById(`sw-panel-${i}`).style.display = i === n ? '' : 'none';
    const stepEl = document.getElementById(`sw-step-${i}`);
    stepEl.classList.toggle('active',    i === n);
    stepEl.classList.toggle('completed', i < n);
  });
}

// ── Slider ↔ number sync ──────────────────────────────────────────────────────

[
  ['sw-gamma-range',    'sw-gamma'],
  ['sw-high-pct-range', 'sw-high-pct'],
  ['sw-sky-pct-range',  'sw-sky-pct'],
].forEach(([rangeId, numId]) => {
  const range = document.getElementById(rangeId);
  const num   = document.getElementById(numId);
  range.addEventListener('input', () => { num.value = range.value; });
  num.addEventListener('input',   () => { range.value = num.value; });
});

// ── Render ────────────────────────────────────────────────────────────────────

document.getElementById('sw-render-btn').addEventListener('click', _startRender);
document.getElementById('sw-rerender-btn').addEventListener('click', () => {
  _clearJob();
  _clearResults();
  _gotoStep(2);
});
document.getElementById('sw-forget-btn').addEventListener('click', () => {
  _clearJob();
  _clearResults();
  _gotoStep(1);
});
document.getElementById('sw-cancel-btn').addEventListener('click', _cancelRender);
document.getElementById('sw-back-to-params-btn').addEventListener('click', () => {
  _cancelRender(true);
});

async function _startRender() {
  _clearResults();
  _gotoStep(3);
  _startElapsedClock();
  document.getElementById('sw-log-header').style.display = '';

  const selectedFiles = _swFiles.filter(f => f.selected).map(f => f.path);
  const body = {
    directory:       _swDir,
    files:           selectedFiles,
    size:            +document.getElementById('sw-size').value,
    sample_interval: +document.getElementById('sw-sample-interval').value,
    speedup:         +document.getElementById('sw-speedup').value,
    gamma:           +document.getElementById('sw-gamma').value,
    sky_pct:         +document.getElementById('sw-sky-pct').value,
    high_pct:        +document.getElementById('sw-high-pct').value,
    no_cache:        document.getElementById('sw-no-cache').checked,
    stab_window:     +document.getElementById('sw-stab-window').value,
    min_quality:     +document.getElementById('sw-min-quality').value || undefined,
  };

  try {
    const res  = await fetch('/api/solar/render', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { _showRenderError(data.error); return; }
    _swJobId = data.job_id;
    _saveJob(_swJobId);
    _pollStatus();
  } catch (e) {
    _showRenderError('Network error: ' + e);
  }
}

// ── Polling ───────────────────────────────────────────────────────────────────

function _pollStatus() {
  if (_swPollTimer) clearTimeout(_swPollTimer);
  _swPollTimer = setTimeout(_fetchStatus, 1500);
}

async function _fetchStatus() {
  if (!_swJobId) return;
  try {
    const res  = await fetch(`/api/solar/status?job_id=${_swJobId}`);
    const data = await res.json();
    _updateProgress(data);
    if (data.status === 'running') _pollStatus();
  } catch (_) {
    _pollStatus();
  }
}

function _updateProgress(data) {
  const bar  = document.getElementById('sw-progress-bar');
  const msg  = document.getElementById('sw-progress-msg');
  const wrap = document.getElementById('sw-progress-wrap');
  const log  = document.getElementById('sw-log');

  bar.style.width = (data.pct || 0) + '%';
  msg.textContent = data.message || '';

  // Append log lines
  if (data.log && data.log.length) {
    document.getElementById('sw-log-wrap').style.display = 'block';
    log.textContent = data.log.join('\n');
    log.scrollTop   = log.scrollHeight;
  }

  if (data.status === 'done') {
    _saveDone(data.outputs || {});
    _stopElapsedClock();
    wrap.style.display = 'none';
    _showResults(data.outputs || {});
  } else if (data.status === 'error') {
    _clearJob();
    _stopElapsedClock();
    // Hide cancel/back so error message is clean
    const acts = document.getElementById('sw-render-actions');
    if (acts) acts.style.display = 'none';
    _showRenderError(data.error || 'Unknown error');
  }
}

// ── Results display ───────────────────────────────────────────────────────────

function _showResults(o) {
  const grid  = document.getElementById('sw-result-grid');
  const label = document.getElementById('sw-result-label');

  label.textContent = o.date_label
    ? `☀ ${o.frame_count} frames · ${o.date_label}`
    : `☀ ${o.frame_count || 0} frames`;

  grid.innerHTML = '';

  if (o.timelapse) {
    grid.appendChild(_makeVideoCard('☀ Full-disk timelapse', o.timelapse));
  }
  if (o.portrait) {
    grid.appendChild(_makeImageCard('🖼 Solar portrait', o.portrait));
  }

  document.getElementById('sw-results').style.display = 'block';
}

function _makeVideoCard(label, path) {
  const url  = `/api/solar/output?path=${encodeURIComponent(path)}`;
  const card = document.createElement('div');
  card.className = 'sw-result-card';

  const title = document.createElement('div');
  title.className  = 'sw-card-title';
  title.textContent = label;

  const video = document.createElement('video');
  video.className = 'sw-result-video';
  video.src       = url;
  video.controls  = true;
  video.loop      = true;
  video.muted     = true;
  video.preload   = 'metadata';

  const dl = _makeDownloadLink(url, path);
  card.append(title, video, dl);
  return card;
}

function _makeImageCard(label, path) {
  const url  = `/api/solar/output?path=${encodeURIComponent(path)}`;
  const card = document.createElement('div');
  card.className = 'sw-result-card';

  const title = document.createElement('div');
  title.className   = 'sw-card-title';
  title.textContent = label;

  const img = document.createElement('img');
  img.className = 'sw-result-img';
  img.src       = url;
  img.alt       = label;
  img.loading   = 'lazy';

  const dl = _makeDownloadLink(url, path);
  card.append(title, img, dl);
  return card;
}

function _makeDownloadLink(url, path) {
  const dl = document.createElement('a');
  dl.className   = 'sw-card-dl';
  dl.href        = url;
  dl.download    = path.split('/').pop();
  dl.textContent = '⬇ Download';
  return dl;
}

// ── Cancel ────────────────────────────────────────────────────────────────────

async function _cancelRender(andGoBack) {
  _stopElapsedClock();
  if (_swPollTimer) { clearTimeout(_swPollTimer); _swPollTimer = null; }

  if (_swJobId) {
    try {
      await fetch('/api/solar/cancel', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({job_id: _swJobId}),
      });
    } catch (_) {}
    _swJobId = null;
  }
  _clearJob();

  if (andGoBack) {
    _clearResults();
    _gotoStep(2);
  } else {
    _showRenderError('Render cancelled.');
  }
}

// ── Error / reset helpers ─────────────────────────────────────────────────────

function _showRenderError(msg) {
  const el = document.getElementById('sw-error');
  el.textContent  = '⚠ ' + msg;
  el.style.display = 'block';
  document.getElementById('sw-progress-wrap').style.display = 'none';
}

function _clearResults() {
  document.getElementById('sw-results').style.display    = 'none';
  document.getElementById('sw-error').style.display      = 'none';
  document.getElementById('sw-log-header').style.display = 'none';
  document.getElementById('sw-log-wrap').style.display   = 'none';
  document.getElementById('sw-log').textContent          = '';
  document.getElementById('sw-result-grid').innerHTML    = '';
  document.getElementById('sw-progress-bar').style.width = '0%';
  document.getElementById('sw-progress-msg').textContent = 'Starting…';
  document.getElementById('sw-elapsed').textContent      = '';
  document.getElementById('sw-progress-wrap').style.display = 'block';
  const acts = document.getElementById('sw-render-actions');
  if (acts) acts.style.display = '';
  if (_swPollTimer) { clearTimeout(_swPollTimer); _swPollTimer = null; }
  if (_swElapsedTimer) { clearInterval(_swElapsedTimer); _swElapsedTimer = null; }
  _swJobId = null;
  _swStartTime = null;
}

function _startElapsedClock() {
  _swStartTime = Date.now();
  if (_swElapsedTimer) clearInterval(_swElapsedTimer);
  _swElapsedTimer = setInterval(() => {
    const el = document.getElementById('sw-elapsed');
    if (!el || !_swStartTime) return;
    const s = Math.floor((Date.now() - _swStartTime) / 1000);
    const m = Math.floor(s / 60);
    const sec = s % 60;
    el.textContent = m > 0
      ? `${m}m ${sec.toString().padStart(2, '0')}s elapsed`
      : `${sec}s elapsed`;
  }, 1000);
}

function _stopElapsedClock() {
  if (_swElapsedTimer) { clearInterval(_swElapsedTimer); _swElapsedTimer = null; }
}
