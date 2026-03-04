# Seestar Lab

A local web application for browsing, cataloging, and analyzing observation data from a
[Seestar S50](https://www.zwoastro.com/product/seestar/) smart telescope.  It automatically
discovers session folders, matches objects against the Messier and Caldwell catalogs, and
runs a computer-vision pipeline to detect aircraft, birds, and satellites transiting the
solar or lunar disk.

---

## Features

| Feature | Description |
|---|---|
| **Session browser** | Scans the data directory and displays every observation session as a card with thumbnail, dates, sub-count, and video hours |
| **Catalog matching** | Messier and Caldwell bingo-card views show which objects have been captured |
| **Transit detection** | Background-subtraction + blob-tracking pipeline finds transiting objects in solar and lunar videos; clips and thumbnails are saved automatically |
| **Aircraft lookup** | Detected events are cross-referenced against the OpenSky Network ADS-B feed to identify the aircraft |
| **Live updates** | A Server-Sent Events stream pushes progress to the browser in real time — no polling, no page reloads |

---

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH` (for embedding thumbnail cover-art into clip MP4s)
- OpenCV, Flask, scipy — see `requirements.txt`

```
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root (or set environment variables):

```ini
# Required
SEESTAR_DATA_DIR=/mnt/d/xfer          # Root directory to scan for sessions
SEESTAR_OUTPUT_DIR=/mnt/d/seestar-lab # Where transit clips and metadata are written

# Optional — aircraft lookup via OpenSky Network
OBSERVER_LAT=44.5646
OBSERVER_LON=-123.2620
OPENSKY_USERNAME=your_username
OPENSKY_PASSWORD=your_password
OPENSKY_BBOX_DEG=1.5                  # Search-box half-width in degrees (default 1.5)
```

---

## Running

```bash
python app.py
```

Open `http://127.0.0.1:5000` in a browser.  The app performs a differential filesystem scan
on startup, then idles until the user requests a rescan or transit detection.

---

## Architecture

```
app.py               Flask routes, SSE broadcaster, transit job queue
scanner.py           Filesystem crawler; builds and diffs session records
db.py                SQLite persistence (sessions, video jobs, transit events)
transit_detector.py  Computer-vision transit-detection pipeline
aircraft_lookup.py   OpenSky REST API integration
static/js/app.js     Sessions-page UI; SSE client; transit controls
static/js/catalog.js Messier / Caldwell bingo-card pages
templates/           Jinja2 HTML templates
```

### Data flow

```
Filesystem
  └─ scanner.py ──► db.sessions ──► SSE ──► browser (app.js)
                                              │
                         User clicks "Detect" │
                                              ▼
                       app.py ──► _transit_queue (thread)
                                       │
                              transit_detector.py
                                       │
                              db.transit_events ──► SSE ──► browser
                                       │
                              aircraft_lookup.py (OpenSky)
```

### Session scanning

The scanner walks the data directory tree looking for leaf folders that contain FITS or
video files.  It uses directory `mtime` to skip unchanged subtrees on subsequent runs
(differential scan).  For each changed object it rebuilds a merged session record from all
associated directories, picks the best thumbnail, sums sub-counts and video durations, and
upserts the record into SQLite.

macOS resource-fork files (`._filename`) are ignored during enumeration and purged from the
database on startup.

---

## Transit Detection

Transit detection runs entirely on the CPU using OpenCV.  No GPU or ML model is required.

### Triggering detection

Click **Detect Transits** on any Solar or Lunar session card.  The app enumerates every
video file in the session's directories and queues one job per file.  A single background
worker thread processes them in order (oldest file first).  The queue can be paused,
cancelled, or force-rerun with **↻ Re-detect**.

### Output files

For each detected event three files are written to `SEESTAR_OUTPUT_DIR`:

| File | Contents |
|---|---|
| `{stem}_t01_plane_0.87.mp4` | Padded clip (±3 s of context) with UTC timestamp burned on each frame; hero-frame JPEG embedded as cover art |
| `{stem}_t01_plane_0.87_thumb.jpg` | Hero-frame JPEG (the track point closest to disk center) |
| `{stem}_t01_plane_0.87.json` | Full metadata sidecar (all `TransitEvent` fields, video metadata, detected_at) |

---

### Algorithm

The pipeline runs in five sequential steps.

#### Step 1 — Temporal median background

Thirty frames are sampled evenly across the video and stacked into a pixel-wise median
image.  Stationary features — sunspots, lunar craters, surface detail — are baked into
this background and cancel out when subtracted.  Moving objects (aircraft, birds, ISS)
leave a clean residual.

For short clips where the transiting object appears in many background-sample frames a
"ghost" of the object is partially baked in; this reduces the effective blob contrast but
does not prevent detection for objects large enough to dominate a single frame.

#### Step 2 — Disk detection

The blurred background is passed to OpenCV's `HoughCircles` to find the solar or lunar
disk.  If HoughCircles fails (e.g. on a crescent moon) the code falls back to thresholding
the image, finding the largest bright contour, and fitting a minimum-enclosing circle.  A
final fallback places the disk at the image centre with radius `min(w, h) / 2`.

The detected disk centre and radius are used throughout the rest of the pipeline to
constrain blob search to the disk interior and to express velocities as a fraction of the
disk diameter.

#### Step 3 — Per-frame blob tracking

Each frame is processed in order:

1. **Difference** — `abs(gray_frame − background)` pixel by pixel.
2. **Threshold** — pixels differing by more than `DIFF_THRESH` (12 DN solar, 8 DN lunar)
   become foreground.
3. **Morphological open** — a 3×3 elliptical kernel removes single-pixel noise.
4. **Disk mask** — pixels outside the disk circle are zeroed.
5. **Connected components** — each remaining blob is measured: area, centroid, aspect ratio.

**Dominant-blob detection** (the key false-positive guard)

After computing all blob areas the code checks whether one blob overwhelmingly dominates
the frame before applying any size cap:

- If `largest / second_largest ≥ 20×` → a single giant object (e.g. a nearby Cessna)
  occupies the frame.  Keep only that blob and pass it to the tracker regardless of size.
- Else if `largest / second_largest ≥ 5×` **and** `largest ≥ 0.3 % of disk area` →
  a moderately large aircraft whose ratio isn't extreme but whose absolute size rules out
  ordinary atmospheric shimmer.  Keep only that blob.
- Otherwise → apply the normal per-blob size cap (`MAX_BLOB_FRAC × disk_area`), then
  check the remaining blob count.

If more than `MAX_BLOBS_PER_FRAME` blobs survive after the dominance check the frame is
treated as camera shake or atmospheric seeing noise and skipped.  Active tracks have their
gap counter incremented; tracks that miss more than `MAX_GAP_FRAMES` consecutive frames
are finalised.

**Track association**

Each frame's surviving blobs are matched to active tracks using a nearest-centroid rule
with linear extrapolation: the expected next position of a track is predicted from its last
two points, and the closest unmatched blob within 12 % of the disk radius is assigned to
it.  Unmatched blobs start new tracks.

#### Step 4 — Track scoring and classification

Finalised tracks are scored against three criteria:

| Criterion | Weight | Notes |
|---|---|---|
| **Linearity (R²)** | 50 % | Scipy linear regression of track coordinates; axis chosen by larger variance; degenerate vertical tracks return R² = 1 |
| **Velocity** | 30 % | `(displacement_px / duration_frames × fps) / (2 × disk_radius) × 100`; optimal 3–50 %Ø/s scores 1.0 |
| **Duration** | 20 % | 0.1–30 s scores 1.0; outside that range scores 0.3 |

Tracks are rejected before scoring if:

- Fewer than `MIN_TRACK_FRAMES` points (8 solar, 5 lunar)
- Total displacement < 5 % of disk radius
- Velocity < `MIN_VEL_PCT` %Ø/s (3 % solar, 2 % lunar) — this is the primary sunspot
  residual filter; sunspots drift at ~0.04 %Ø/day, five orders of magnitude below the
  minimum threshold

**Classification heuristics**

| Label | Condition |
|---|---|
| `iss` | velocity > 40 %Ø/s **and** R² > 0.97 |
| `plane` | R² ≥ 0.90 **and** velocity ≥ 3 %Ø/s |
| `bird` | velocity < 8 %Ø/s (slow, erratic path) |
| `unknown` | everything else |

Events with `confidence < MIN_CONFIDENCE` (0.70 solar, 0.60 lunar) are discarded.  If
more than 30 events survive for a single video the entire result set is discarded as
likely false positives.

#### Step 5 — Clip extraction

For each accepted event the code re-reads the original video and writes a padded clip
(±3 s of context by default) using OpenCV's `VideoWriter`.  A UTC timestamp parsed from
the filename (`YYYY-MM-DD-HHMMSS`) is burned onto every frame.  The track point closest
to the disk centre is saved as a JPEG thumbnail, which is then embedded into the MP4
container as cover art via `ffmpeg` so Windows Explorer and macOS Finder show a preview.
A JSON sidecar with the complete `TransitEvent` fields is written alongside each clip.

---

### Per-type parameter table

| Parameter | Solar | Lunar | Purpose |
|---|---|---|---|
| `DIFF_THRESH` | 12 | 8 | Foreground threshold (DN) |
| `MIN_TRACK_FRAMES` | 8 | 5 | Minimum track length |
| `MIN_VEL_PCT` | 3.0 | 2.0 | Minimum speed (%Ø/s) |
| `MIN_CONFIDENCE` | 0.70 | 0.60 | Score cutoff |
| `SHAKE_HOT_FRAC` | 0.015 | 0.04 | Hot-pixel fraction that signals shake (unused in current blob-count logic; retained for reference) |
| `MAX_BLOBS_PER_FRAME` | 5 | 10 | Blob-count shake threshold |
| `MAX_BLOB_FRAC` | 0.02 | 0.95 | Normal per-blob size cap (dominant-blob bypass overrides this) |

Lunar thresholds are more lenient because the moon is dimmer than the sun (lower contrast
silhouettes) and its surface features — craters, maria — generate more residual blobs in
quiet frames due to atmospheric seeing.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Sessions index page |
| `GET` | `/catalog/messier` | Messier bingo-card page |
| `GET` | `/catalog/caldwell` | Caldwell bingo-card page |
| `GET` | `/api/events` | SSE stream (sessions, progress, transit events) |
| `GET` | `/api/sessions` | JSON: all sessions |
| `GET` | `/api/catalog/<type>` | JSON: Messier or Caldwell catalog with capture status |
| `GET` | `/api/thumbnail/<name>` | Resized session thumbnail JPEG |
| `GET` | `/api/status` | JSON: scan state, last scan time, session count |
| `POST` | `/api/scan` | Start scan — body: `{"force": bool}` |
| `POST` | `/api/transit/detect` | Queue transit detection — body: `{"session_name": str, "force": bool}` |
| `GET` | `/api/transit/all` | JSON: all video jobs and detected events |
| `GET` | `/api/transit/clip/<id>` | Stream MP4 clip for event |
| `GET` | `/api/transit/thumb/<id>` | Serve hero-frame JPEG for event |
| `POST` | `/api/transit/pause` | Pause transit worker |
| `POST` | `/api/transit/resume` | Resume transit worker |
| `POST` | `/api/transit/cancel` | Cancel jobs — body: `{"session_name": str}` or `{"all": true}` |

### SSE event types

| Type | Payload fields | When sent |
|---|---|---|
| `session` | full session dict | New or updated session discovered |
| `session_removed` | `object_name` | Session deleted from DB |
| `db_loaded` | — | Initial DB flush to new SSE client complete |
| `progress` | `message` | Scan progress update |
| `complete` | `changed`, `total` | Scan finished |
| `transit_progress` | `session_name`, `video_path`, `status`, `pct`, `message` | Per-frame detection progress |
| `transit_done` | `session_name`, `video_path`, `events[]` | Detection finished for one video |
| `transit_queue_state` | `paused`, `cancel_all` | Queue paused, resumed, or cancelled |

---

## Database

SQLite at `seestar.db` in the project root.  The schema is created automatically on
startup; new columns are added with `ALTER TABLE` for backwards compatibility.

| Table | Purpose |
|---|---|
| `sessions` | One row per observation object (M42, Solar, etc.) |
| `scanned_dirs` | Directory paths + mtimes for differential scanning |
| `meta` | Key-value store (last scan time, data dir) |
| `video_jobs` | One row per video file queued for transit detection |
| `transit_events` | One row per detected transit event |
