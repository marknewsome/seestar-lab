'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
let allItems      = [];   // full catalog from API
let activeGroup   = 'all';
let capturedOnly  = false;

// ── Boot ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.filter-btn[data-group]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn[data-group]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeGroup = btn.dataset.group;
      applyFilter();
    });
  });

  document.getElementById('captured-only').addEventListener('change', e => {
    capturedOnly = e.target.checked;
    applyFilter();
  });

  loadCatalog();
});

// ── Data loading ───────────────────────────────────────────────────────────────
async function loadCatalog() {
  try {
    const res = await fetch(`/api/catalog/${CATALOG_TYPE}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    allItems = await res.json();
    document.getElementById('catalog-loading').style.display = 'none';
    const grid = document.getElementById('catalog-grid');
    grid.style.display = '';

    // Render all cards once; filter by toggling CSS display only
    allItems.sort((a, b) => {
      const na = parseInt(a.label.replace(/\D/g, ''), 10);
      const nb = parseInt(b.label.replace(/\D/g, ''), 10);
      return na - nb;
    });
    grid.innerHTML = allItems.map(buildCard).join('');

    updateProgress();
    updateFilterCounts();
    applyFilter();
  } catch (err) {
    document.getElementById('catalog-loading').innerHTML = `
      <div class="empty-icon">⚠</div>
      <p>Failed to load catalog: ${esc(err.message)}</p>`;
  }
}

// ── Filter — show/hide without rebuilding DOM ──────────────────────────────────
function applyFilter() {
  let anyVisible = false;
  document.querySelectorAll('.cat-card').forEach(el => {
    const group    = el.dataset.group;
    const captured = el.dataset.captured === '1';
    const show = (!capturedOnly || captured) &&
                 (activeGroup === 'all' || group === activeGroup);
    el.style.display = show ? '' : 'none';
    if (show) anyVisible = true;
  });

  const grid = document.getElementById('catalog-grid');
  let empty = grid.querySelector('.cat-empty-msg');
  if (!anyVisible) {
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'empty-state cat-empty-msg';
      empty.style.gridColumn = '1 / -1';
      empty.innerHTML = '<div class="empty-icon">🔭</div><p>No objects match this filter.</p>';
      grid.appendChild(empty);
    }
  } else if (empty) {
    empty.remove();
  }
}

// ── Progress bar ───────────────────────────────────────────────────────────────
function updateProgress() {
  const captured = allItems.filter(i => i.captured).length;
  const total    = allItems.length;
  const pct      = total ? (captured / total) * 100 : 0;

  document.getElementById('catalog-progress').style.display = '';
  document.getElementById('progress-fill').style.width      = `${pct.toFixed(1)}%`;
  document.getElementById('progress-label').textContent     =
    `${captured} / ${total} captured`;
}

// ── Filter counts ──────────────────────────────────────────────────────────────
function updateFilterCounts() {
  const groupCounts = {};
  allItems.forEach(item => {
    groupCounts[item.dso_group] = (groupCounts[item.dso_group] || 0) + 1;
  });
  const total = allItems.length;

  document.querySelectorAll('.filter-btn[data-group]').forEach(btn => {
    const group = btn.dataset.group;
    const base  = btn.dataset.label;
    const n     = group === 'all' ? total : (groupCounts[group] || 0);
    btn.textContent = n ? `${base} (${n})` : base;
  });
}

// ── Card builder ───────────────────────────────────────────────────────────────
const RATING_CYCLE = [null, 'satisfied', 'want_more', 'priority'];
const RATING_TIPS  = { satisfied: 'Satisfied', want_more: 'Want more time', priority: 'Priority re-image', null: 'Not rated' };

function buildCard(item) {
  const capturedClass = item.captured ? 'captured' : 'uncaptured';
  const sessionName = item.session?.object_name;
  const userRating = item.session?.user_rating ?? null;

  const thumb = item.captured && item.session?.thumbnail
    ? `<div class="cat-thumb" style="position:relative;">
         <img src="/api/thumbnail/${encodeURIComponent(sessionName)}"
              data-session="${esc(sessionName)}"
              alt="${esc(item.label)}" loading="lazy"
              onerror="this.parentElement.innerHTML='<div class=cat-thumb-placeholder>★</div>'" />
         ${buildRatingDot(sessionName, userRating)}
       </div>`
    : `<div class="cat-thumb cat-thumb-placeholder">
         <span class="cat-placeholder-icon">★</span>
       </div>`;

  const popularLine = item.popular_name
    ? `<div class="cat-popular">${esc(item.popular_name)}</div>` : '';

  const refLine = item.ngc_ref
    ? `<div class="cat-ref">${esc(item.ngc_ref)}</div>` : '';

  const dateLine = item.captured && item.session?.dates?.length
    ? `<div class="cat-dates">${item.session.dates.map(d => `<span class="date-chip">${esc(d)}</span>`).join('')}</div>`
    : '';

  const subLine = item.captured && item.session?.num_subs
    ? `<div class="cat-subs">${item.session.num_subs.toLocaleString()} subs · ${esc(item.session.total_size_human)}</div>`
    : '';

  const notesIndicator = item.captured && item.session?.notes
    ? `<div class="cat-notes-snippet" title="${esc(item.session.notes)}">📝 ${esc(item.session.notes)}</div>`
    : '';

  const typePill = `<span class="cat-type-pill cat-type-${esc(item.dso_group)}">${esc(item.dso_type_label)}</span>`;

  return `
    <div class="cat-card ${capturedClass}"
         data-group="${esc(item.dso_group)}"
         data-captured="${item.captured ? 1 : 0}"
         title="${esc(item.label)}${item.popular_name ? ' · ' + item.popular_name : ''}">
      ${thumb}
      <div class="cat-card-body">
        <div class="cat-label">${esc(item.label)}</div>
        ${popularLine}
        ${refLine}
        ${typePill}
        <div class="cat-const">${esc(item.constellation)}</div>
        ${dateLine}
        ${subLine}
        ${notesIndicator}
      </div>
    </div>`;
}

function buildRatingDot(sessionName, rating) {
  const tip = RATING_TIPS[rating] ?? 'Not rated';
  return `<span class="cat-rating-dot"
               data-rating="${rating ?? 'none'}"
               data-session="${esc(sessionName)}"
               title="${esc(tip)}"
               onclick="cycleRating(event, this)"></span>`;
}

async function cycleRating(e, dot) {
  e.stopPropagation();
  const sessionName = dot.dataset.session;
  const cur = dot.dataset.rating === 'none' ? null : dot.dataset.rating;
  const idx = RATING_CYCLE.indexOf(cur);
  const next = RATING_CYCLE[(idx + 1) % RATING_CYCLE.length];
  dot.dataset.rating = next ?? 'none';
  dot.title = RATING_TIPS[next] ?? 'Not rated';
  try {
    await fetch(`/api/session/${encodeURIComponent(sessionName)}/rate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: next }),
    });
  } catch { /* ignore — dot already updated optimistically */ }
}

// ── SSE — live thumbnail updates ───────────────────────────────────────────────
(function () {
  const es = new EventSource('/api/events');
  es.onmessage = e => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.type !== 'session') return;
      const s = ev.data;
      const img = document.querySelector(
        `img[data-session="${CSS.escape(s.object_name)}"]`
      );
      if (img) img.src =
        `/api/thumbnail/${encodeURIComponent(s.object_name)}?_=${Date.now()}`;
      const dot = document.querySelector(
        `.cat-rating-dot[data-session="${CSS.escape(s.object_name)}"]`
      );
      if (dot) {
        const r = s.user_rating ?? null;
        dot.dataset.rating = r ?? 'none';
        dot.title = RATING_TIPS[r] ?? 'Not rated';
      }
      // Sync notes snippet on bingo card
      const card = img?.closest?.('.cat-card');
      if (card) {
        let snippet = card.querySelector('.cat-notes-snippet');
        if (s.notes) {
          if (!snippet) {
            snippet = document.createElement('div');
            snippet.className = 'cat-notes-snippet';
            card.querySelector('.cat-card-body').appendChild(snippet);
          }
          snippet.title = s.notes;
          snippet.textContent = '📝 ' + s.notes;
        } else if (snippet) {
          snippet.remove();
        }
      }
    } catch { /* ignore */ }
  };
})();

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
