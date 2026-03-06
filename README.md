# Seestar Lab

A local web application for browsing, cataloging, and analyzing observation data from a
[Seestar S50](https://www.zwoastro.com/product/seestar/) smart telescope.  It automatically
discovers session folders, matches objects against the Messier and Caldwell catalogs, runs a
computer-vision pipeline to detect aircraft, birds, and satellites transiting the solar or
lunar disk, and stacks raw FITS sub-frames into publication-quality images.

---

## Features

| Feature | Description |
|---|---|
| **Session browser** | Scans the data directory and displays every observation session as a card with thumbnail, dates, sub-count, and video hours |
| **Session thumbnails** | Best-quality image from each session (enhanced JPEG, stacked output, or cover frame) is shown on the card; hover-zooms to a larger view |
| **Sub-frame stacking** | One-click pipeline stacks raw `.fit` sub-frames: quality selection, ECC alignment, sigma-clip mean, background gradient removal, auto-crop, colour stretch, denoising, and sharpening |
| **Catalog matching** | Messier and Caldwell bingo-card views show which objects have been captured |
| **Transit detection** | Background-subtraction + blob-tracking pipeline finds transiting objects in solar and lunar videos; clips and thumbnails are saved automatically |
| **Transit tab indicators** | Solar and Lunar filter tabs on the Transits page show a pulsing amber dot while detection is actively running for that category |
| **YOLO validation** | Optional second-stage YOLOv8n inference on the hero frame confirms visually recognisable aircraft or birds; unconfirmed events are still shown but can be filtered |
| **Aircraft lookup** | Detected events are cross-referenced against the OpenSky Network ADS-B feed to identify the aircraft |
| **Live updates** | A Server-Sent Events stream pushes progress to the browser in real time — no polling, no page reloads |

---

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH` (for H.264 transcoding and cover-art embedding)
- OpenCV, Flask, scipy — see `requirements.txt`

```
pip install -r requirements.txt
```

`ultralytics` (YOLOv8) is an **optional** dependency.  If it is not installed the
first-stage CV detector works as normal and the YOLO filter toggle is hidden in the UI.
Model weights (`yolov8n.pt`, ~6 MB) are downloaded automatically by ultralytics on first
use and cached in `~/.cache/ultralytics/`.

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
app.py               Flask routes, SSE broadcaster, transit job queue, stack job queue
scanner.py           Filesystem crawler; builds and diffs session records
db.py                SQLite persistence (sessions, video jobs, transit events, stack jobs)
stack_processor.py   Sub-frame stacking pipeline (registration, sigma-clip, stretch, denoise)
transit_detector.py  Computer-vision transit-detection pipeline
yolo_validator.py    Optional YOLOv8n second-stage confirmation (soft dependency)
aircraft_lookup.py   OpenSky REST API integration
catalogs.py          Messier / Caldwell catalog data and DSO type/group mappings
object_catalog.py    Object-type detection (solar/lunar/planet/comet/messier/…) and descriptions
static/js/app.js     Sessions-page UI; SSE client; transit controls; stack controls
static/js/catalog.js Messier / Caldwell bingo-card pages
static/js/transits.js Transit gallery page; running-indicator logic
templates/           Jinja2 HTML templates
```

### Data flow

```
Filesystem
  └─ scanner.py ──► db.sessions ──► SSE ──► browser (app.js)
                                              │
                         User clicks "Detect" │      User clicks "Stack"
                                              ▼              ▼
                       app.py ──► _transit_queue     _stack_queue (thread)
                                       │                     │
                              [copy to local SSD]   stack_processor.py
                                       │                     │
                              transit_detector.py    sigma-clip stack
                                       │                     │
                              yolo_validator.py     stretch + denoise
                                (optional)                   │
                                       │            db.stack_jobs ──► SSE ──► browser
                              db.transit_events ──► SSE ──► browser
                                       │
                              aircraft_lookup.py (OpenSky)
```

**Local SSD caching** — if the source video is on a different storage device from the
system temp directory (e.g. a spinning external drive), the app copies the file to a local
temp path before detection and deletes the copy when done.  This eliminates random-seek
latency on spinning media without changing where clips are written.

### Session scanning

The scanner walks the data directory tree looking for leaf folders that contain FITS or
video files.  It uses directory `mtime` to skip unchanged subtrees on subsequent runs
(differential scan).  For each changed object it rebuilds a merged session record from all
associated directories, picks the best thumbnail, sums sub-counts and video durations, and
upserts the record into SQLite.

macOS resource-fork files (`._filename`) are ignored during enumeration and purged from the
database on startup.

---

## Sub-Frame Stacking

Session folders whose name ends with `_sub` (e.g. `M51_sub`) contain raw `.fit` sub-frames
from the Seestar's individual exposures.  Click the **Stack** button on those cards to run
the stacking pipeline.  Progress and stage are reported live via SSE; on completion the
stacked JPEG is saved and displayed as the session thumbnail.

### Pipeline stages

| # | Stage | Details |
|---|---|---|
| 1 | **Quality selection** | Laplacian-variance sharpness scored on the centre quarter of each frame; frames below 40 % of the median score are rejected.  Minimum 3 accepted frames required. |
| 2 | **ECC registration** | `cv2.findTransformECC` with `MOTION_EUCLIDEAN` aligns each frame to the reference (sharpest accepted frame).  Falls back to phase correlation if ECC fails. |
| 3 | **Sigma-clip mean stack** | Frames are stacked into a 3-D array; per-pixel MAD-based sigma clipping (σ = 2.5) rejects hot pixels, cosmic rays, and satellite trails before taking the mean. |
| 4 | **Background subtraction** | An 8 × 8 grid samples 20th-percentile pixel values across the image; a degree-2 2-D polynomial is fit to the grid and subtracted to remove gradient vignetting. |
| 5 | **Auto-crop** | The valid-pixel overlap mask is computed from all alignment transforms; a tight bounding rectangle (12 px margin) removes the dark rotation artefact borders. |
| 6 | **Auto-stretch** | Percentile black/white point clipping followed by a √γ stretch mimics PixInsight's Screen Transfer Function for natural colour rendition. |
| 7 | **Denoise + sharpen** | `cv2.bilateralFilter` smooths noise while preserving edges; a weighted unsharp mask enhances fine detail. |

Output is written to `seestar_stacked.jpg` inside the session's output directory and
automatically registered as the session thumbnail — visible immediately without a rescan.

### Re-running

A **Re-run** button replaces the Stack button once a job has completed or failed, allowing
re-stacking (e.g. after adjusting quality parameters).

---

## Transit Detection

Transit detection runs entirely on the CPU using OpenCV.  No GPU is required.  The optional
YOLO second stage uses a small pretrained model (YOLOv8n, ~6 MB) and runs on CPU.

### Triggering detection

Click **Detect Transits** on any Solar or Lunar session card.  The app enumerates every
video file in the session's directories and queues one job per file.  A thread pool
processes up to **3 videos concurrently** (OpenCV decode and NumPy both release the GIL,
so threading gives real parallelism without multiprocessing overhead).  The queue can be
paused, cancelled, or force-rerun with **↻ Re-detect**.

### Output files

For each detected event three files are written to `SEESTAR_OUTPUT_DIR`:

| File | Contents |
|---|---|
| `{stem}_t01_plane_0.87.mp4` | Padded clip (±5 s of context) transcoded to H.264 with UTC timestamp burned on each frame; hero-frame JPEG embedded as cover art |
| `{stem}_t01_plane_0.87_thumb.jpg` | Hero-frame JPEG (the track point closest to disk centre) |
| `{stem}_t01_plane_0.87.json` | Full metadata sidecar (all `TransitEvent` fields, video metadata, `detected_at`) |

The clip filename encodes: original video stem · event index · first-stage label ·
confidence (e.g. `0.87` = 87 %).

---

### Algorithm

The pipeline runs in six sequential steps.  See `DETECTION_PIPELINE.md` for a visual
flowchart.

#### Step 1 — Temporal median background

Thirty frames are sampled evenly across the video in a single forward pass.
`cap.grab()` advances the stream without a full pixel decode; only the 30 keeper frames
pay the decompression cost.  The 30 grayscale frames are stacked and reduced to a
pixel-wise median image.

Stationary features — sunspots, lunar craters, surface detail — are baked into this
background and cancel out when subtracted.  Moving objects (aircraft, birds, ISS) leave
a clean residual.

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

1. **Drift compensation** — The Seestar's alt-az mount makes slow servo corrections that
   shift crater edges and solar-surface features against the static background, generating
   spurious linear blobs that can mimic transiting objects.  Before differencing, a
   256 × 256 pixel crop centred on the disk is phase-correlated between the current frame
   and the background to measure sub-pixel translational drift `(dx, dy)`.  If the shift
   exceeds 0.3 px (the sub-pixel noise floor) the background is realigned with an affine
   warp before differencing.  Corrections larger than 8 px are clamped (likely a re-point
   or tracking failure, not a smooth correction).  A texture gate skips phase correlation
   on featureless crops — the dark side of a crescent moon or a smooth solar disk with no
   sunspots — where the output would be unreliable.

2. **Difference** — `abs(gray_frame − aligned_background)` pixel by pixel.
3. **Threshold** — pixels differing by more than `DIFF_THRESH` (12 DN solar, 8 DN lunar)
   become foreground.
4. **Morphological open** — a 3×3 elliptical kernel removes single-pixel noise.
5. **Disk mask** — pixels outside the disk circle are zeroed.
6. **Connected components** — each remaining blob is measured: area, centroid, aspect ratio.

**Dominant-blob detection** (large-aircraft guard)

After computing all blob areas the code checks whether one blob overwhelmingly dominates
the frame before applying any size cap:

- If `largest / second_largest ≥ 20×` → a single giant object (e.g. a nearby Cessna)
  occupies the frame.  Keep only that blob and pass it to the tracker regardless of size.
- Else if `largest / second_largest ≥ 10×` **and** `largest ≥ 0.3 % of disk area` (solar
  only) → a moderately large aircraft whose absolute size rules out ordinary shimmer.
  Keep only that blob.
- Otherwise → apply the normal per-blob size cap (`MAX_BLOB_FRAC × disk_area`), then
  check the remaining blob count.

If more than `MAX_BLOBS_PER_FRAME` blobs survive after the dominance check the frame is
treated as camera shake or atmospheric seeing noise and skipped.  Active tracks have their
gap counter incremented; tracks that miss more than `MAX_GAP_FRAMES` (4) consecutive frames
are finalised.

**Track association**

Each frame's surviving blobs are matched to active tracks using a nearest-centroid rule
with linear extrapolation: the expected next position of a track is predicted from its last
two points, and the closest unmatched blob within 12 % of the disk radius is assigned to
it.  Unmatched blobs start new tracks.

#### Step 4 — Track scoring and classification

Finalised tracks pass through a gauntlet of rejection filters (cheapest first), then a
weighted confidence score.

**Rejection filters**

| # | Filter | Solar | Lunar | Notes |
|---|---|---|---|---|
| 1 | Minimum track length | 8 pts | 7 pts | R² over < 7 points is statistically unreliable |
| 2 | Minimum displacement | 5 % of disk radius | same | Rejects stationary residuals |
| 3 | Minimum velocity | 3.0 %Ø/s | 2.0 %Ø/s | Sunspots drift at ~0.04 %Ø/day — five orders of magnitude below threshold |
| 4 | Cloud-wisp guard | vel < 10 %Ø/s: reject if mean blob > 0.6 % disk area **or** duration > 5 s | — | Diffuse cloud wisps produce larger blobs than compact bird silhouettes at the same velocity; very long slow transits are clouds, not birds |
| 5 | Fill fraction | ≥ 0.40 | ≥ 0.50 | `n_points ÷ (frame_end − frame_start + 1)` — rejects erratic shimmer tracks that accumulate the minimum point count through large gaps |
| 6 | R² floor | ≥ 0.70 | ≥ 0.60 | Hard linearity floor; cloud wisps and seeing-shimmer cluster below this.  ISS candidates (vel > 40 %Ø/s **and** R² > 0.95) are exempt |
| 7 | Perimeter proximity | ≥ 0.50 | ≥ 0.60 | `max(radial_start, radial_end) ÷ disk_radius` — blobs that materialise mid-disk cannot be real transits |
| 8 | Confidence score | ≥ 0.75 | ≥ 0.60 | Weighted sum below (see table) |

**Confidence score weights**

| Criterion | Weight | Notes |
|---|---|---|
| **Linearity (R²)** | 50 % | Scipy linear regression; axis chosen by larger variance; degenerate vertical lines return R² = 1 |
| **Velocity** | 30 % | Optimal 3–50 %Ø/s scores 1.0; tapers outside that range |
| **Duration** | 20 % | 0.1–30 s scores 1.0; outside that range scores 0.3 |

**Classification heuristics**

| Label | Condition |
|---|---|
| `iss` | velocity > 40 %Ø/s **and** R² > 0.97 |
| `plane` | R² ≥ 0.90 **and** velocity ≥ 3 %Ø/s |
| `bird` | velocity < 8 %Ø/s |
| `unknown` | everything else |

If more than 30 events survive for a single video the entire result set is discarded as
almost certainly false positives (the shake filter was insufficient for that video).

#### Step 5 — Clip extraction

For each accepted event the code seeks to `frame_start − pad` (5 s of context) and reads
forward through `frame_end + pad` in a single pass.  A UTC timestamp parsed from the
filename (`YYYY-MM-DD-HHMMSS`) is burned onto every frame.  The hero frame (the track
point closest to the disk centre) is captured during this same pass — no second seek is
needed.

After writing the raw `mp4v` clip, `ffmpeg` transcodes it to H.264 (`libx264 -crf 23
-preset fast`) and embeds the hero JPEG as cover art so file browsers show a preview.  A
JSON sidecar with the complete `TransitEvent` fields is written alongside each clip.

#### Step 6 — YOLO second-stage validation (optional)

If `ultralytics` is installed, YOLOv8n inference is run on the hero-frame JPEG for each
event.  Only two COCO classes are checked: `airplane` (4) and `bird` (14).  The result is
stored as `yolo_label` / `yolo_confidence` on the event.

In the UI, confirmed events show a **✓ airplane** or **✓ bird** badge on the pill.  A
"confirmed only" toggle on each session card hides all first-stage detections that YOLO did
not confirm.

---

### Per-type parameter table

| Parameter | Solar | Lunar | Purpose |
|---|---|---|---|
| `diff_thresh` | 12 | 8 | Foreground threshold (DN) — moon is dimmer |
| `min_track_frames` | 8 | 7 | Minimum track length for reliable R² |
| `min_vel_pct` | 3.0 | 2.0 | Minimum speed (%Ø/s) |
| `min_confidence` | 0.75 | 0.60 | Weighted score cutoff |
| `min_linearity` | 0.70 | 0.60 | Hard R² floor (cloud wisps and seeing shimmer cluster below this) |
| `min_fill_frac` | 0.40 | 0.50 | Minimum fraction of spanned frames with a blob |
| `min_perimeter_frac` | 0.50 | 0.60 | One track end must reach this fraction of disk radius from edge |
| `shake_hot_frac` | 0.015 | 0.04 | Fraction of disk pixels lit up that signals a shake frame |
| `max_blobs_per_frame` | 5 | 10 | Blob-count threshold beyond which a frame is skipped |
| `max_blob_frac` | 0.02 | 0.95 | Normal per-blob size cap (dominant-blob bypass overrides this) |
| cloud-wisp blob threshold | 0.006 (0.6 % disk area) | — | Mean blob size above which a slow solar track is rejected as a cloud wisp |
| cloud-wisp duration cap | 5.0 s | — | Maximum duration for slow (< 10 %Ø/s) solar events |

**Drift-compensation constants** (solar and lunar share the same values):

| Constant | Value | Purpose |
|---|---|---|
| `DRIFT_CROP` | 256 px | Side of the square disk crop used for phase correlation |
| `DRIFT_MAX` | 8.0 px | Clamp on accepted shift; larger values treated as re-points and ignored |
| `DRIFT_MIN` | 0.3 px | Sub-pixel noise floor; shifts below this threshold are ignored |

All three filters (`min_linearity`, `min_fill_frac`, `min_perimeter_frac`) are now active
for both solar and lunar, with solar thresholds calibrated slightly tighter to reject the
cloud-wisp and atmospheric-shimmer false positives that are common in solar videos.  The
solar-specific cloud-wisp guard (mean blob area + duration cap) has no lunar equivalent
because the moon's limb contrast and cooler imaging conditions do not produce the same
thin-cloud artefacts.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Sessions index page |
| `GET` | `/catalog/messier` | Messier bingo-card page |
| `GET` | `/catalog/caldwell` | Caldwell bingo-card page |
| `GET` | `/transits` | Transit gallery page (all events across sessions) |
| `GET` | `/api/events` | SSE stream (sessions, progress, transit events, stack progress) |
| `GET` | `/api/sessions` | JSON: all sessions |
| `GET` | `/api/catalog/<type>` | JSON: Messier or Caldwell catalog with capture status |
| `GET` | `/api/thumbnail/<name>` | Resized session thumbnail JPEG |
| `GET` | `/api/status` | JSON: scan state, last scan time, session count |
| `POST` | `/api/scan` | Start scan — body: `{"force": bool}` |
| `POST` | `/api/transit/detect` | Queue transit detection — body: `{"session_name": str, "force": bool}` |
| `GET` | `/api/transit/all` | JSON: all video jobs and detected events grouped by session |
| `GET` | `/api/transit/gallery` | JSON: all transit events as a flat list (newest first) |
| `GET` | `/api/transit/clip/<id>` | Stream MP4 clip for event |
| `GET` | `/api/transit/thumb/<id>` | Serve hero-frame JPEG for event |
| `GET` | `/api/transit/running` | JSON: `{"types": [...]}` — video types with actively running jobs |
| `POST` | `/api/transit/pause` | Pause transit worker |
| `POST` | `/api/transit/resume` | Resume transit worker |
| `POST` | `/api/transit/cancel` | Cancel jobs — body: `{"session_name": str}` or `{"all": true}` |
| `POST` | `/api/stack/start` | Queue sub-frame stacking — body: `{"session_name": str, "force": bool}` |
| `GET` | `/api/stack/status` | JSON: all stack job statuses keyed by session name |
| `GET` | `/api/stack/image/<session_name>` | Serve full-size stacked JPEG |

### SSE event types

| Type | Payload fields | When sent |
|---|---|---|
| `session` | full session dict | New or updated session discovered |
| `session_removed` | `object_name` | Session deleted from DB |
| `db_loaded` | — | Initial DB flush to new SSE client complete |
| `progress` | `message` | Scan progress update |
| `complete` | `changed`, `total` | Scan finished |
| `transit_progress` | `session_name`, `video_path`, `video_type`, `status`, `pct`, `message` | Per-frame detection progress (`video_type`: `"solar"` or `"lunar"`) |
| `transit_done` | `session_name`, `video_path`, `video_type`, `events[]` | Detection finished for one video |
| `transit_queue_state` | `paused`, `cancel_all` | Queue paused, resumed, or cancelled |
| `stack_progress` | `session_name`, `status`, `pct`, `stage`, `frames_total`, `frames_accepted` | Stacking pipeline progress |
| `stack_done` | `session_name`, `status`, `frames_total`, `frames_accepted`, `output_path` | Stacking complete (or failed) |

---

## Database

SQLite at `seestar-lab.db` in the project root.  The schema is created automatically on
startup; new columns are added with `ALTER TABLE` for backwards compatibility.

| Table | Purpose |
|---|---|
| `sessions` | One row per observation object (M42, Solar, etc.) |
| `scanned_dirs` | Directory paths + mtimes for differential scanning |
| `meta` | Key-value store (last scan time, data dir) |
| `video_jobs` | One row per video file queued for transit detection |
| `transit_events` | One row per detected transit event; includes `yolo_label` and `yolo_confidence` |
| `stack_jobs` | One row per sub-frame stacking job; tracks status, progress percentage, pipeline stage, frame counts, and output path |
