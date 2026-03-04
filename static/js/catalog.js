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
      render();
    });
  });

  document.getElementById('captured-only').addEventListener('change', e => {
    capturedOnly = e.target.checked;
    render();
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
    document.getElementById('catalog-grid').style.display    = '';
    updateProgress();
    updateFilterCounts();
    render();
  } catch (err) {
    document.getElementById('catalog-loading').innerHTML = `
      <div class="empty-icon">⚠</div>
      <p>Failed to load catalog: ${esc(err.message)}</p>`;
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

// ── Render ─────────────────────────────────────────────────────────────────────
function render() {
  const visible = allItems.filter(item => {
    if (capturedOnly && !item.captured) return false;
    if (activeGroup !== 'all' && item.dso_group !== activeGroup) return false;
    return true;
  });

  const grid = document.getElementById('catalog-grid');
  if (!visible.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
      <div class="empty-icon">🔭</div>
      <p>No objects match this filter.</p>
    </div>`;
    return;
  }
  grid.innerHTML = visible.map(buildCard).join('');
}

// ── Card builder ───────────────────────────────────────────────────────────────
function buildCard(item) {
  const capturedClass = item.captured ? 'captured' : 'uncaptured';
  const thumb = item.captured && item.session?.thumbnail
    ? `<div class="cat-thumb">
         <img src="/api/thumbnail/${encodeURIComponent(item.session.object_name)}"
              alt="${esc(item.label)}" loading="lazy"
              onerror="this.parentElement.innerHTML='<div class=cat-thumb-placeholder>★</div>'" />
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

  const typePill = `<span class="cat-type-pill cat-type-${esc(item.dso_group)}">${esc(item.dso_type_label)}</span>`;

  return `
    <div class="cat-card ${capturedClass}" title="${esc(item.label)}${item.popular_name ? ' · ' + item.popular_name : ''}">
      ${thumb}
      <div class="cat-card-body">
        <div class="cat-label">${esc(item.label)}</div>
        ${popularLine}
        ${refLine}
        ${typePill}
        <div class="cat-const">${esc(item.constellation)}</div>
        ${dateLine}
        ${subLine}
      </div>
    </div>`;
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
