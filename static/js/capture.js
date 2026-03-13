/* capture.js — Live Capture page logic */

const LS_KEY = "seestar_capture_streams";

// ── Helpers ──────────────────────────────────────────────────────────────────

function loadStreams() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveStreams(streams) {
  localStorage.setItem(LS_KEY, JSON.stringify(streams));
}

function genId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function fmtBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  return (b / (1024 * 1024)).toFixed(1) + " MB";
}

function fmtElapsed(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// ── State ────────────────────────────────────────────────────────────────────

// Per-card runtime state (not persisted): { [streamId]: { recId, recStart, recTimer, connected } }
const cardState = {};

let statusPollTimer = null;

// ── Card rendering ────────────────────────────────────────────────────────────

function renderCard(stream) {
  const { id, name, url } = stream;
  cardState[id] = cardState[id] || { recId: null, recStart: null, recTimer: null, connected: false };

  const card = document.createElement("div");
  card.className = "cap-card";
  card.dataset.streamId = id;
  card.innerHTML = `
    <div class="cap-card-header">
      <input class="cap-name-input" type="text" value="${escHtml(name)}" placeholder="Stream name" spellcheck="false" />
      <button class="cap-remove-btn" title="Remove stream">✕</button>
    </div>
    <div class="cap-url-row">
      <input class="cap-url-input" type="text" value="${escHtml(url)}" placeholder="rtsp://192.168.x.x:port/..." spellcheck="false" autocomplete="off" />
      <button class="cap-connect-btn btn btn-sm">Connect</button>
    </div>
    <div class="cap-view">
      <span class="cap-offline">Not connected</span>
      <img class="cap-img" src="" alt="" style="display:none" />
      <div class="cap-spinner" style="display:none">Loading…</div>
    </div>
    <div class="cap-controls">
      <button class="cap-rec-btn btn btn-sm" disabled>⏺ Record</button>
      <span class="cap-rec-dot" style="display:none"></span>
      <span class="cap-rec-status"></span>
    </div>
    <div class="cap-saved-info" style="display:none"></div>
  `;

  const nameInput = card.querySelector(".cap-name-input");
  const urlInput  = card.querySelector(".cap-url-input");
  const connectBtn = card.querySelector(".cap-connect-btn");
  const removeBtn  = card.querySelector(".cap-remove-btn");
  const imgEl      = card.querySelector(".cap-img");
  const offlineEl  = card.querySelector(".cap-offline");
  const spinnerEl  = card.querySelector(".cap-spinner");
  const recBtn     = card.querySelector(".cap-rec-btn");
  const recDot     = card.querySelector(".cap-rec-dot");
  const recStatus  = card.querySelector(".cap-rec-status");
  const savedInfo  = card.querySelector(".cap-saved-info");

  // ── Save name/url on blur ──────────────────────────────────────────────────
  nameInput.addEventListener("blur", () => {
    const streams = loadStreams();
    const s = streams.find(x => x.id === id);
    if (s) { s.name = nameInput.value.trim() || "Stream"; saveStreams(streams); }
  });
  urlInput.addEventListener("blur", () => {
    const streams = loadStreams();
    const s = streams.find(x => x.id === id);
    if (s) { s.url = urlInput.value.trim(); saveStreams(streams); }
  });

  // ── Connect / Disconnect ───────────────────────────────────────────────────
  connectBtn.addEventListener("click", () => {
    const state = cardState[id];
    if (state.connected) {
      // Disconnect
      imgEl.src = "";
      imgEl.style.display = "none";
      offlineEl.style.display = "";
      spinnerEl.style.display = "none";
      connectBtn.textContent = "Connect";
      recBtn.disabled = true;
      state.connected = false;
    } else {
      // Connect
      const streamUrl = urlInput.value.trim();
      if (!streamUrl) return;
      offlineEl.style.display = "none";
      spinnerEl.style.display = "";
      imgEl.style.display = "none";
      connectBtn.textContent = "Disconnect";
      state.connected = true;

      imgEl.onload = () => {
        spinnerEl.style.display = "none";
        imgEl.style.display = "";
        recBtn.disabled = false;
      };
      imgEl.onerror = () => {
        spinnerEl.style.display = "none";
        offlineEl.textContent = "Stream error";
        offlineEl.style.display = "";
        imgEl.style.display = "none";
        imgEl.src = "";
        connectBtn.textContent = "Connect";
        recBtn.disabled = true;
        state.connected = false;
      };
      imgEl.src = "/api/capture/mjpeg?url=" + encodeURIComponent(streamUrl);
    }
  });

  // ── Record / Stop ──────────────────────────────────────────────────────────
  recBtn.addEventListener("click", async () => {
    const state = cardState[id];
    if (state.recId) {
      // Stop
      recBtn.disabled = true;
      try {
        const res = await fetch("/api/capture/record/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rec_id: state.recId }),
        });
        const data = await res.json();
        clearInterval(state.recTimer);
        state.recId = null;
        state.recStart = null;
        state.recTimer = null;
        recDot.style.display = "none";
        recStatus.textContent = "";
        recBtn.textContent = "⏺ Record";
        recBtn.disabled = false;
        checkStopPolling();

        // Show saved info
        const fname = data.out_path ? data.out_path.split("/").pop() : "";
        const size  = data.size_bytes ? fmtBytes(data.size_bytes) : "";
        savedInfo.innerHTML = `Saved: <strong>${escHtml(fname)}</strong> (${size})
          <a href="/api/solar/output?path=${encodeURIComponent(data.out_path)}" target="_blank">Download</a>`;
        savedInfo.style.display = "";
        showRecordingsSection(data.out_path, fname, size);
      } catch (e) {
        recBtn.disabled = false;
        recStatus.textContent = "Stop failed";
      }
    } else {
      // Start
      const streamUrl = urlInput.value.trim();
      const streamName = nameInput.value.trim() || "capture";
      recBtn.disabled = true;
      try {
        const res = await fetch("/api/capture/record/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rtsp_url: streamUrl, name: streamName }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        state.recId = data.rec_id;
        state.recStart = Date.now();
        recDot.style.display = "";
        recBtn.textContent = "⏹ Stop";
        recBtn.disabled = false;
        savedInfo.style.display = "none";

        // Client-side elapsed clock
        state.recTimer = setInterval(() => {
          const elapsed = (Date.now() - state.recStart) / 1000;
          recStatus.textContent = fmtElapsed(elapsed);
        }, 1000);

        startPolling();
      } catch (e) {
        recBtn.disabled = false;
        recStatus.textContent = "Start failed: " + e.message;
      }
    }
  });

  // ── Remove card ────────────────────────────────────────────────────────────
  removeBtn.addEventListener("click", () => {
    // If connected, disconnect first
    if (cardState[id] && cardState[id].connected) {
      imgEl.src = "";
    }
    const streams = loadStreams().filter(x => x.id !== id);
    saveStreams(streams);
    delete cardState[id];
    card.remove();
  });

  return card;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Recordings section ────────────────────────────────────────────────────────

function showRecordingsSection(outPath, fname, size) {
  const section = document.getElementById("cap-recordings-section");
  const list    = document.getElementById("cap-recordings-list");
  section.style.display = "";

  const item = document.createElement("div");
  item.className = "cap-recording-item";
  item.innerHTML = `<span class="cap-rec-filename">${escHtml(fname)}</span>
    <span class="cap-rec-filesize">(${size})</span>
    <a href="/api/solar/output?path=${encodeURIComponent(outPath)}" target="_blank">Download</a>`;
  list.prepend(item);
}

// ── Status polling ────────────────────────────────────────────────────────────

function anyActiveRec() {
  return Object.values(cardState).some(s => s.recId);
}

function startPolling() {
  if (statusPollTimer) return;
  statusPollTimer = setInterval(pollStatus, 3000);
}

function checkStopPolling() {
  if (!anyActiveRec() && statusPollTimer) {
    clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
}

async function pollStatus() {
  try {
    const res  = await fetch("/api/capture/record/status");
    const list = await res.json();
    // Update size display for each active recording
    for (const rec of list) {
      // Find card for this rec
      for (const [streamId, state] of Object.entries(cardState)) {
        if (state.recId === rec.id) {
          const card = document.querySelector(`.cap-card[data-stream-id="${streamId}"]`);
          if (card) {
            const recStatus = card.querySelector(".cap-rec-status");
            const elapsed = (Date.now() - state.recStart) / 1000;
            recStatus.textContent = `${fmtElapsed(elapsed)} · ${fmtBytes(rec.size_bytes)}`;
          }
        }
      }
    }
  } catch {
    // ignore poll errors
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

function init() {
  // Set output dir label
  document.getElementById("cap-output-dir").textContent = CAP_DATA_DIR + "/captures/";

  // Load and render existing streams
  const streams = loadStreams();
  const grid = document.getElementById("cap-grid");
  for (const stream of streams) {
    grid.appendChild(renderCard(stream));
  }

  // Add stream button
  document.getElementById("cap-add-btn").addEventListener("click", () => {
    const stream = { id: genId(), name: "Seestar", url: "" };
    const streams = loadStreams();
    streams.push(stream);
    saveStreams(streams);
    const card = renderCard(stream);
    grid.appendChild(card);
    card.querySelector(".cap-url-input").focus();
  });
}

document.addEventListener("DOMContentLoaded", init);
