'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

let _plObjects   = [];   // full result objects list
let _plFilter    = 'all';
let _plSort      = 'rating';
let _plMinAlt    = 20;
let _plTaskId    = null;
let _plPollTimer = null;

// ── Init ──────────────────────────────────────────────────────────────────────

(function _init() {
  // Pre-fill location from server-injected data
  const loc = (typeof PL_OBS_LOCATION !== 'undefined') ? PL_OBS_LOCATION : {};
  if (loc.lat  != null) document.getElementById('pl-lat').value       = loc.lat;
  if (loc.lon  != null) document.getElementById('pl-lon').value       = loc.lon;
  if (loc.elevation != null) document.getElementById('pl-elevation').value = loc.elevation;
  if (loc.name)         document.getElementById('pl-name').value      = loc.name;

  // Default date = today (local)
  const today = new Date();
  const yyyy  = today.getFullYear();
  const mm    = String(today.getMonth() + 1).padStart(2, '0');
  const dd    = String(today.getDate()).padStart(2, '0');
  document.getElementById('pl-date').value = `${yyyy}-${mm}-${dd}`;

  // Hero star field
  _buildStarField();

  // Wire events
  document.getElementById('pl-plan-btn').addEventListener('click', _plan);
  document.getElementById('pl-gps-btn').addEventListener('click', _useGPS);
  document.getElementById('pl-sort').addEventListener('change', e => {
    _plSort = e.target.value;
    _renderTable();
  });
  document.getElementById('pl-min-alt').addEventListener('change', e => {
    _plMinAlt = +e.target.value || 20;
    _renderTable();
  });
  document.querySelectorAll('.planner-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.planner-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _plFilter = btn.dataset.cat;
      _renderTable();
    });
  });
})();

function _buildStarField() {
  const container = document.getElementById('planner-hero-stars');
  if (!container) return;
  const n = 80;
  for (let i = 0; i < n; i++) {
    const s = document.createElement('div');
    s.className = 'planner-star';
    s.style.cssText = `left:${Math.random()*100}%;top:${Math.random()*100}%;` +
      `opacity:${(Math.random()*0.5+0.2).toFixed(2)};` +
      `width:${(Math.random()*2+1).toFixed(1)}px;` +
      `height:${(Math.random()*2+1).toFixed(1)}px;`;
    container.appendChild(s);
  }
}

// ── GPS ───────────────────────────────────────────────────────────────────────

function _useGPS() {
  const btn = document.getElementById('pl-gps-btn');
  btn.disabled = true;
  btn.textContent = '…';
  if (!navigator.geolocation) {
    _setLocStatus('Geolocation not supported by this browser', true);
    btn.disabled = false; btn.textContent = '⌖ GPS';
    return;
  }
  navigator.geolocation.getCurrentPosition(
    pos => {
      document.getElementById('pl-lat').value       = pos.coords.latitude.toFixed(5);
      document.getElementById('pl-lon').value       = pos.coords.longitude.toFixed(5);
      document.getElementById('pl-elevation').value = Math.round(pos.coords.altitude || 50);
      _setLocStatus('Location set from GPS');
      btn.disabled = false; btn.textContent = '⌖ GPS';
    },
    err => {
      _setLocStatus('GPS error: ' + err.message, true);
      btn.disabled = false; btn.textContent = '⌖ GPS';
    },
    { enableHighAccuracy: false, timeout: 8000 }
  );
}

function _setLocStatus(msg, isError) {
  const el = document.getElementById('pl-loc-status');
  el.textContent = msg;
  el.className   = 'planner-loc-status' + (isError ? ' planner-loc-status-error' : '');
}

// ── Plan ──────────────────────────────────────────────────────────────────────

async function _plan() {
  const lat  = parseFloat(document.getElementById('pl-lat').value);
  const lon  = parseFloat(document.getElementById('pl-lon').value);
  const elev = parseFloat(document.getElementById('pl-elevation').value) || 50;
  const name = document.getElementById('pl-name').value.trim();
  const date = document.getElementById('pl-date').value;

  if (isNaN(lat) || isNaN(lon)) {
    _setLocStatus('Please enter latitude and longitude first.', true);
    return;
  }

  _setLocStatus('');
  _showSpinner('Computing visibility…');
  document.getElementById('planner-results').style.display = 'none';
  document.getElementById('planner-error').style.display   = 'none';

  try {
    const res  = await fetch('/api/planner/tonight', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ lat, lon, elevation: elev, name, date }),
    });
    const data = await res.json();
    if (data.error) { _showError(data.error); return; }
    _plTaskId = data.task_id;
    if (data.cached) {
      _fetchResult();
    } else {
      _pollResult();
    }
  } catch (e) {
    _showError('Network error: ' + e);
  }
}

function _pollResult() {
  if (_plPollTimer) clearTimeout(_plPollTimer);
  _plPollTimer = setTimeout(_fetchResult, 1200);
}

async function _fetchResult() {
  if (!_plTaskId) return;
  try {
    const res  = await fetch(`/api/planner/status?task_id=${_plTaskId}`);
    const data = await res.json();
    if (data.status === 'running') {
      _pollResult();
      return;
    }
    _hideSpinner();
    if (data.status === 'error') {
      _showError(data.error || 'Unknown error');
      return;
    }
    _displayResult(data.result);
  } catch (_) {
    _pollResult();
  }
}

// ── Display ───────────────────────────────────────────────────────────────────

function _displayResult(r) {
  _plObjects = r.objects || [];

  // Moon card
  const moon = r.moon || {};
  const moonIcon = document.getElementById('pl-moon-icon');
  const illum = moon.illum_pct || 0;
  moonIcon.textContent = illum < 10 ? '🌑' : illum < 30 ? '🌒' : illum < 70 ? '🌓' : illum < 90 ? '🌔' : '🌕';
  document.getElementById('pl-moon-phase').textContent  = moon.phase || '—';
  document.getElementById('pl-moon-detail').textContent =
    `${illum.toFixed(0)}% illuminated · ${moon.age_days?.toFixed(1)} days old · ` +
    `midnight alt ${moon.alt_midnight?.toFixed(0)}°`;

  // Dark window
  const dw = r.dark_window || {};
  if (dw.duration_h) {
    const start = dw.dark_start ? _utcToLocal(dw.dark_start) : '—';
    const end   = dw.dark_end   ? _utcToLocal(dw.dark_end)   : '—';
    document.getElementById('pl-dark-detail').textContent =
      `${start} – ${end} (${dw.duration_h}h)`;
  } else {
    document.getElementById('pl-dark-detail').textContent = 'No astronomical darkness tonight';
  }

  // Counts
  document.getElementById('pl-counts-detail').textContent =
    `${r.total_visible} visible · ${r.never_imaged} never imaged`;

  document.getElementById('planner-results').style.display = 'block';
  _renderTable();
}

function _renderTable() {
  let objs = _plObjects.slice();

  // Filter
  if (_plFilter === 'messier') {
    objs = objs.filter(o => o.id.startsWith('M'));
  } else if (_plFilter === 'caldwell') {
    objs = objs.filter(o => o.id.startsWith('C'));
  } else if (_plFilter === 'new') {
    objs = objs.filter(o => !o.have_data);
  }

  // Min alt
  objs = objs.filter(o => o.peak_alt >= _plMinAlt);

  // Sort
  if (_plSort === 'rating') {
    objs.sort((a, b) => b.rating - a.rating);
  } else if (_plSort === 'alt') {
    objs.sort((a, b) => b.peak_alt - a.peak_alt);
  } else if (_plSort === 'rise') {
    objs.sort((a, b) => a.rise_utc.localeCompare(b.rise_utc));
  } else if (_plSort === 'name') {
    objs.sort((a, b) => a.label.localeCompare(b.label, undefined, {numeric: true}));
  }

  const tbody = document.getElementById('planner-tbody');
  tbody.innerHTML = '';

  if (!objs.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="10" class="pl-empty">No objects match the current filter.</td>`;
    tbody.appendChild(tr);
    return;
  }

  objs.forEach(o => {
    const tr = document.createElement('tr');
    tr.className = o.have_data ? 'pl-row-have' : 'pl-row-new';

    const fovBadge = _fovBadge(o.fov_note);
    const ratingEl = _ratingStars(o.rating);
    const transit  = o.transit_utc ? _utcToLocal(o.transit_utc) : '—';
    const imageEl  = o.have_data
      ? `<span class="pl-have-yes" title="${o.session_dates.join(', ')}">✓ ${o.session_dates.length}</span>`
      : `<span class="pl-have-no">new</span>`;

    tr.innerHTML = `
      <td class="pl-col-id">
        <a class="pl-obj-link" href="/catalog/${o.id.startsWith('M') ? 'messier' : 'caldwell'}"
           title="${o.id}">${o.label}</a>
      </td>
      <td class="pl-col-name">${o.name || '—'}</td>
      <td class="pl-col-type">${o.type_label || o.type || '—'}</td>
      <td class="pl-col-con">${o.constellation || '—'}</td>
      <td class="pl-col-size">${_fmtSize(o.size_arcmin)}${fovBadge}</td>
      <td class="pl-col-alt">${o.peak_alt.toFixed(0)}°</td>
      <td class="pl-col-transit">${transit}</td>
      <td class="pl-col-moon">${o.moon_sep.toFixed(0)}°</td>
      <td class="pl-col-rating">${ratingEl}</td>
      <td class="pl-col-have">${imageEl}</td>`;
    tbody.appendChild(tr);
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _fovBadge(note) {
  if (!note) return '';
  const cls = {mosaic: 'pl-fov-mosaic', large: 'pl-fov-large', good: 'pl-fov-good',
                small: 'pl-fov-small',   tiny:  'pl-fov-tiny'}[note] || '';
  return ` <span class="pl-fov-badge ${cls}">${note}</span>`;
}

function _fmtSize(arcmin) {
  if (arcmin >= 60)  return (arcmin / 60).toFixed(1) + '°';
  return arcmin + '′';
}

function _ratingStars(r) {
  const full  = Math.floor(r);
  const half  = (r - full) >= 0.4 ? 1 : 0;
  const empty = 5 - full - half;
  return '<span class="pl-stars">' +
    '★'.repeat(full) +
    (half ? '½' : '') +
    '<span class="pl-stars-empty">' + '★'.repeat(empty) + '</span>' +
    `</span> <span class="pl-rating-val">${r.toFixed(1)}</span>`;
}

function _utcToLocal(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr.replace(' ', 'T') + 'Z');
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  } catch (_) {
    return isoStr.slice(11, 16);
  }
}

function _showSpinner(msg) {
  document.getElementById('planner-spinner-msg').textContent = msg;
  document.getElementById('planner-spinner-wrap').style.display = '';
}

function _hideSpinner() {
  document.getElementById('planner-spinner-wrap').style.display = 'none';
}

function _showError(msg) {
  _hideSpinner();
  const el = document.getElementById('planner-error');
  el.textContent  = '⚠ ' + msg;
  el.style.display = '';
}
