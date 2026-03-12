'use strict';

// ── Init ──────────────────────────────────────────────────────────────────────

const _params  = new URLSearchParams(window.location.search);
const _dir     = _params.get('dir') || '';
const _present = _params.has('present');

let _frameList = [];
let _frameIdx  = 0;

if (_present) enterPresent();

// Set comet name from the last path segment of the directory
const _cometName = _dir.split('/').filter(Boolean).pop() || 'Comet Results';
document.getElementById('cr-title').textContent = _cometName;
document.title = _cometName + ' — Seestar Lab';

// Back-to-wizard link preserves dir
const wizLink = document.getElementById('cr-wizard-link');
if (_dir) wizLink.href = `/comet?dir=${encodeURIComponent(_dir)}`;

// Present toggle
document.getElementById('cr-present-btn').addEventListener('click', () => {
  const url = new URL(window.location.href);
  url.searchParams.set('present', '1');
  window.location.href = url.toString();
});

function exitPresent() {
  const url = new URL(window.location.href);
  url.searchParams.delete('present');
  window.location.href = url.toString();
}

function enterPresent() {
  document.documentElement.classList.add('present-mode');
}

// ── Load results ──────────────────────────────────────────────────────────────

async function loadResults() {
  if (!_dir) {
    showError('No directory specified. Add ?dir=… to the URL.');
    return;
  }
  try {
    const res  = await fetch(`/api/comet/check?dir=${encodeURIComponent(_dir)}`);
    const data = await res.json();
    if (data.error) { showError(data.error); return; }
    render(data.outputs || {});
  } catch (e) {
    showError('Network error: ' + e);
  }
}

function outputUrl(path, mtime) {
  const v = mtime ? `&v=${mtime}` : '';
  return `/api/comet/output?path=${encodeURIComponent(path)}${v}`;
}

function render(o) {
  document.getElementById('cr-loading').style.display = 'none';
  document.getElementById('cr-body').style.display    = 'block';

  // ── Animations ─────────────────────────────────────────────────────────────
  const animGrid = document.getElementById('cr-animations-grid');
  if (o.stars_mp4)   animGrid.appendChild(makeVideoCard('⭐ Stars fixed',    o.stars_mp4));
  if (o.nucleus_mp4) animGrid.appendChild(makeVideoCard('☄ Nucleus fixed',  o.nucleus_mp4));
  if (animGrid.children.length)
    document.getElementById('cr-animations-section').style.display = 'block';

  // ── Stacks ─────────────────────────────────────────────────────────────────
  const stackGrid = document.getElementById('cr-stacks-grid');
  if (o.portrait_jpg)      stackGrid.appendChild(makeImageCard('🎨 Comet portrait',              o.portrait_jpg));
  if (o.stack_jpg)         stackGrid.appendChild(makeImageCard('🖼 Stars-aligned composite',     o.stack_jpg));
  if (o.nucleus_stack_jpg) stackGrid.appendChild(makeImageCard('☄ Nucleus-aligned deep stack',  o.nucleus_stack_jpg));
  if (o.ls_jpg)            stackGrid.appendChild(makeImageCard('🔬 Larson-Sekanina filter',      o.ls_jpg));
  if (o.track_jpg)         stackGrid.appendChild(makeImageCard('📍 Nucleus track',              o.track_jpg));
  if (stackGrid.children.length)
    document.getElementById('cr-stacks-section').style.display = 'block';

  // ── Frame browser ───────────────────────────────────────────────────────────
  if (o.frame_count > 0 && o.frame_dir) {
    document.getElementById('cr-frames-section').style.display = 'block';
    document.getElementById('cr-frames-label').textContent =
      `🎞 Annotated frames — ${o.frame_count} frames`;
    loadFrameStrip(o.frame_dir);
  }
}

// ── Card builders ─────────────────────────────────────────────────────────────

function makeVideoCard(label, path) {
  const url  = outputUrl(path);
  const card = document.createElement('div');
  card.className = 'cr-card';

  const title = document.createElement('div');
  title.className = 'cr-card-title';
  title.textContent = label;

  const video = document.createElement('video');
  video.className = 'cr-video';
  video.src       = url;
  video.controls  = true;
  video.loop      = true;
  video.muted     = true;
  video.preload   = 'metadata';

  const dl = makeDownloadLink(url, path);
  card.append(title, video, dl);
  return card;
}

function makeImageCard(label, path) {
  const url  = outputUrl(path);
  const card = document.createElement('div');
  card.className = 'cr-card';

  const title = document.createElement('div');
  title.className = 'cr-card-title';
  title.textContent = label;

  const img = document.createElement('img');
  img.className = 'cr-img';
  img.src       = url;
  img.alt       = label;
  img.loading   = 'lazy';

  const dl = makeDownloadLink(url, path);
  card.append(title, img, dl);
  return card;
}

function makeDownloadLink(url, path) {
  const dl = document.createElement('a');
  dl.className  = 'cr-dl';
  dl.href       = url;
  dl.download   = path.split('/').pop();
  dl.textContent = '⬇ Download';
  return dl;
}

// ── Frame browser ─────────────────────────────────────────────────────────────

document.getElementById('cr-frames-title').addEventListener('click', () => {
  const body    = document.getElementById('cr-frames-body');
  const arrow   = document.querySelector('#cr-frames-title .frame-collapse-arrow');
  const collapsed = body.classList.toggle('collapsed');
  arrow.textContent = collapsed ? '▸' : '▾';
});

async function loadFrameStrip(dir) {
  const strip = document.getElementById('cr-strip');
  strip.innerHTML = '<span style="color:var(--text-muted);font-size:.8rem;padding:.5rem">Loading…</span>';
  try {
    const res  = await fetch(`/api/comet/frames?dir=${encodeURIComponent(dir)}`);
    const data = await res.json();
    _frameList = data.frames || [];
    strip.innerHTML = '';
    _frameList.forEach((f, i) => {
      const img = document.createElement('img');
      img.className     = 'frame-strip-thumb';
      img.loading       = 'lazy';
      img.src           = outputUrl(f.path, f.mtime);
      img.title         = f.name;
      img.dataset.idx   = i;
      img.addEventListener('click', () => openFrame(i));
      strip.appendChild(img);
    });
  } catch (err) {
    strip.innerHTML = `<span style="color:#fca5a5;font-size:.8rem;padding:.5rem">Error: ${err}</span>`;
  }
}

function openFrame(idx) {
  _frameIdx = idx;
  const f = _frameList[idx];
  if (!f) return;

  const viewer  = document.getElementById('cr-viewer');
  const img     = document.getElementById('cr-viewer-img');
  const caption = document.getElementById('cr-viewer-caption');
  const counter = document.getElementById('cr-counter');

  img.src           = outputUrl(f.path, f.mtime);
  caption.textContent = `Frame ${idx + 1} of ${_frameList.length}`;
  counter.textContent = `${idx + 1} / ${_frameList.length}`;
  viewer.style.display = 'block';

  document.querySelectorAll('.frame-strip-thumb').forEach(t =>
    t.classList.toggle('active', parseInt(t.dataset.idx) === idx));

  const thumb = document.querySelector(`.frame-strip-thumb[data-idx="${idx}"]`);
  if (thumb) thumb.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'center'});

  document.getElementById('cr-prev').onclick =
    () => openFrame(Math.max(0, _frameIdx - 1));
  document.getElementById('cr-next').onclick =
    () => openFrame(Math.min(_frameList.length - 1, _frameIdx + 1));
}

document.addEventListener('keydown', e => {
  if (!document.getElementById('cr-viewer') ||
      document.getElementById('cr-viewer').style.display === 'none') return;
  if (e.key === 'ArrowLeft')  openFrame(Math.max(0, _frameIdx - 1));
  if (e.key === 'ArrowRight') openFrame(Math.min(_frameList.length - 1, _frameIdx + 1));
});

// ── Error display ─────────────────────────────────────────────────────────────

function showError(msg) {
  document.getElementById('cr-loading').style.display = 'none';
  const el = document.getElementById('cr-error');
  el.textContent  = msg;
  el.style.display = 'block';
}

// ── Boot ──────────────────────────────────────────────────────────────────────

loadResults();
