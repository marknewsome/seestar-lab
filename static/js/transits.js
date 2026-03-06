'use strict';

let allEvents   = [];
let scopeFilter = 'all';
let labelFilter = 'all';
let yoloOnly    = true;

// Tracks which video paths are actively running, mapped to their type
const activeJobs = new Map(); // video_path → video_type

function updateTabIndicators() {
  const runningTypes = new Set(activeJobs.values());
  document.querySelectorAll('#scope-bar .filter-btn[data-scope]').forEach(btn => {
    const scope = btn.dataset.scope;
    btn.classList.toggle('detecting', scope !== 'all' && runningTypes.has(scope));
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Extract YYYY-MM-DD capture date from a video path like
 * …/2025-10-05-224648-Lunar.mp4  →  "2025-10-05"
 * Falls back to the detected_at field if the filename pattern isn't present.
 */
function captureDate(ev) {
  const m = ev.video_path.match(/(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : (ev.detected_at ? ev.detected_at.slice(0, 10) : '');
}

/**
 * Extract HH:MM:SS UTC capture time from filename: …-HHMMSS-….mp4
 */
function captureTime(ev) {
  const m = ev.video_path.match(/\d{4}-\d{2}-\d{2}-(\d{2})(\d{2})(\d{2})/);
  return m ? `${m[1]}:${m[2]}:${m[3]}` : '';
}

function hasAnyYolo() {
  return allEvents.some(ev => ev.yolo_label != null);
}

// ── Card builder ──────────────────────────────────────────────────────────────

function buildCard(ev) {
  const dateStr  = captureDate(ev);
  const timeStr  = captureTime(ev);
  const typeLabel = ev.video_type === 'solar' ? 'Solar' : 'Lunar';
  const pct      = Math.round(ev.confidence * 100);

  const thumb = ev.thumb_path
    ? `<img src="/api/transit/thumb/${ev.id}" alt="transit thumbnail" loading="lazy">`
    : `<div class="gallery-no-thumb">◎</div>`;

  const yoloBadge = ev.yolo_label != null
    ? `<span class="yolo-badge">✓ ${esc(ev.yolo_label)}</span>`
    : '';

  const vel = ev.velocity_pct_per_sec != null
    ? ev.velocity_pct_per_sec.toFixed(1) + ' %Ø/s'
    : '';
  const dur = ev.duration_s != null
    ? ev.duration_s.toFixed(1) + 's'
    : '';
  const stats = [dur, vel].filter(Boolean).join(' · ');

  let acHtml = '';
  if (ev.aircraft_candidates && ev.aircraft_candidates.length > 0) {
    const parts = ev.aircraft_candidates.slice(0, 2).map(c => {
      const bits = [];
      if (c.callsign) bits.push(c.callsign.trim());
      if (c.alt_ft)   bits.push(Math.round(c.alt_ft / 1000) + 'k ft');
      return bits.join(' ');
    });
    acHtml = `<div class="aircraft-hint">✈ ${esc(parts.join('  ·  '))}</div>`;
  }

  const clickAttr = ev.clip_path
    ? `onclick="window.open('/api/transit/clip/${ev.id}','_blank')" role="button" tabindex="0"`
    : '';

  const dateDisplay = dateStr
    ? (timeStr ? `${esc(dateStr)} ${esc(timeStr)} UTC` : esc(dateStr))
    : '';

  return `
    <div class="transit-gallery-card" ${clickAttr}>
      <div class="gallery-card-thumb">${thumb}</div>
      <div class="gallery-card-body">
        <div class="gallery-card-badges">
          <span class="type-badge ${esc(ev.video_type)}">${esc(typeLabel)}</span>
          <span class="transit-event-pill ${esc(ev.label)}">${esc(ev.label)} ${pct}%</span>
          ${yoloBadge}
        </div>
        ${dateDisplay ? `<div class="gallery-card-date">${dateDisplay}</div>` : ''}
        ${stats        ? `<div class="gallery-card-stats">${esc(stats)}</div>` : ''}
        ${acHtml}
      </div>
    </div>`;
}

// ── Filter logic ──────────────────────────────────────────────────────────────

function applyFilters() {
  let visible = allEvents;

  if (scopeFilter !== 'all') {
    visible = visible.filter(ev => ev.video_type === scopeFilter);
  }
  if (labelFilter !== 'all') {
    visible = visible.filter(ev => ev.label === labelFilter);
  }
  if (yoloOnly) {
    visible = visible.filter(ev => ev.yolo_label != null);
  }

  return visible;
}

// ── Render ────────────────────────────────────────────────────────────────────

function render() {
  const visible = applyFilters();
  const grid    = document.getElementById('transit-gallery');
  const summary = document.getElementById('gallery-summary');

  // Gray out the YOLO toggle when no events carry YOLO data
  const wrap = document.getElementById('yolo-toggle-wrap');
  if (hasAnyYolo()) {
    wrap.classList.remove('no-yolo-data');
    wrap.title = 'Only show events visually confirmed by YOLO';
  } else {
    wrap.classList.add('no-yolo-data');
    wrap.title = 'No YOLO-validated events available';
  }

  // Summary line
  const yoloCount  = allEvents.filter(ev => ev.yolo_label != null).length;
  let summaryParts = [`${visible.length.toLocaleString()} event${visible.length !== 1 ? 's' : ''}`];
  if (allEvents.length !== visible.length) {
    summaryParts[0] += ` of ${allEvents.length.toLocaleString()}`;
  }
  if (yoloCount > 0) {
    summaryParts.push(`${yoloCount} YOLO confirmed`);
  }
  summary.textContent = summaryParts.join(' · ');

  if (visible.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="empty-icon">🔭</div>
        <p>No transits match the current filters.</p>
      </div>`;
  } else {
    // Sort visible events by capture date (newest first)
    const sorted = visible.slice().sort((a, b) => {
      const da = captureDate(a) + captureTime(a);
      const db_ = captureDate(b) + captureTime(b);
      return db_ < da ? -1 : db_ > da ? 1 : 0;
    });
    grid.innerHTML = sorted.map(buildCard).join('');
  }
}

// ── Data load ─────────────────────────────────────────────────────────────────

async function loadGallery() {
  try {
    const resp  = await fetch('/api/transit/gallery');
    allEvents   = await resp.json();

    document.getElementById('gallery-loading').style.display = 'none';
    document.getElementById('transit-gallery').style.display = '';
    render();
  } catch (_) {
    document.getElementById('gallery-loading').innerHTML =
      '<div class="empty-icon">⚠</div><p>Failed to load transits.</p>';
  }
}

// ── Event wiring ──────────────────────────────────────────────────────────────

document.querySelectorAll('#scope-bar .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#scope-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    scopeFilter = btn.dataset.scope;
    render();
  });
});

document.querySelectorAll('#label-bar .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#label-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    labelFilter = btn.dataset.label;
    render();
  });
});

document.getElementById('yolo-only').addEventListener('change', e => {
  yoloOnly = e.target.checked;
  render();
});

// ── Running-detection indicators ──────────────────────────────────────────────

async function initRunningIndicators() {
  // Seed initial state from DB (jobs that were running when page loaded)
  try {
    const resp = await fetch('/api/transit/running');
    const data = await resp.json();
    for (const type of (data.types || [])) {
      // Use a sentinel path to mark the type as known-running
      activeJobs.set(`__initial__${type}`, type);
    }
    updateTabIndicators();
  } catch (_) { /* non-critical */ }

  // Subscribe to live updates
  const es = new EventSource('/api/events');
  es.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }

    if (msg.type === 'transit_progress') {
      const vp   = msg.video_path;
      const vt   = msg.video_type;
      const stat = msg.status;
      if (stat === 'running' && vp && vt) {
        // Remove any initial sentinel for this type now we have a real path
        activeJobs.delete(`__initial__${vt}`);
        activeJobs.set(vp, vt);
        updateTabIndicators();
      } else if ((stat === 'cancelled' || stat === 'error') && vp) {
        activeJobs.delete(vp);
        updateTabIndicators();
      }
    } else if (msg.type === 'transit_done') {
      const vp = msg.video_path;
      const vt = msg.video_type;
      if (vp) {
        activeJobs.delete(vp);
        if (vt) activeJobs.delete(`__initial__${vt}`);
        updateTabIndicators();
      }
      // Reload gallery to show the new events
      loadGallery();
    }
  };
}

loadGallery();
initRunningIndicators();
