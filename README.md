# Seestar Lab

A local web application for browsing, cataloging, and analyzing observation data from a
[Seestar S50](https://www.zwoastro.com/product/seestar/) smart telescope.  It automatically
discovers session folders, matches objects against the Messier and Caldwell catalogs, runs a
computer-vision pipeline to detect aircraft, birds, and satellites transiting the solar or
lunar disk, stacks raw FITS sub-frames into publication-quality images, and provides a
dedicated comet-processing wizard that produces star-fixed and comet-fixed animations plus a
track-path composite.

---

## Features

| Feature | Description |
|---|---|
| **Session browser** | Scans the data directory and displays every observation session as a card with thumbnail, dates, sub-count, and video hours |
| **Session thumbnails** | Best-quality image from each session (enhanced JPEG, stacked output, or cover frame) is shown on the card; hover-zooms to a larger view |
| **Image gallery** | Seestar-stacked JPEGs for non-`_sub` comet sessions are browsable via prev/next arrows on the card thumbnail and a full-screen lightbox |
| **Sub-frame stacking** | One-click pipeline stacks raw `.fit` sub-frames: quality selection, ECC alignment, sigma-clip mean, background gradient removal, auto-crop, colour stretch, denoising, and sharpening |
| **Comet wizard** | Step-by-step pipeline for `_sub` comet folders: frame selection, stretch/parameter tuning with live preview, stars-fixed animation, comet-nucleus-fixed animation, track composite, and annotated frame review |
| **Catalog matching** | Messier and Caldwell bingo-card views show which objects have been captured |
| **Transit detection** | Background-subtraction + blob-tracking pipeline finds transiting objects in solar and lunar videos; clips and thumbnails are saved automatically |
| **Transit tab indicators** | Solar and Lunar filter tabs on the Transits page show a pulsing amber dot while detection is actively running for that category |
| **YOLO validation** | Optional second-stage YOLOv8n inference on the hero frame confirms visually recognisable aircraft or birds; unconfirmed events are still shown but can be filtered |
| **Aircraft lookup** | Detected events are cross-referenced against the OpenSky Network ADS-B feed to identify the aircraft |
| **Activity heatmap** | Calendar heatmap showing daily sub counts or session counts across the full observation history |
| **Live updates** | A Server-Sent Events stream pushes progress to the browser in real time — no polling, no page reloads |

---

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH` (for H.264 transcoding and cover-art embedding)
- OpenCV, Flask, scipy, astropy — see `requirements.txt`

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
app.py               Flask routes, SSE broadcaster, transit job queue, stack job queue, comet job queue
scanner.py           Filesystem crawler; builds and diffs session records
db.py                SQLite persistence (sessions, video jobs, transit events, stack jobs)
stack_processor.py   Sub-frame stacking pipeline (registration, sigma-clip, stretch, denoise)
comet_processor.py   Comet animation pipeline (star alignment, nucleus detection, animations, track composite)
transit_detector.py  Computer-vision transit-detection pipeline
yolo_validator.py    Optional YOLOv8n second-stage confirmation (soft dependency)
aircraft_lookup.py   OpenSky REST API integration
catalogs.py          Messier / Caldwell catalog data and DSO type/group mappings
object_catalog.py    Object-type detection (solar/lunar/planet/comet/messier/…) and descriptions
static/js/app.js     Sessions-page UI; SSE client; transit controls; stack controls; lightbox
static/js/comet_wizard.js  Comet wizard multi-step UI; frame grid; preview; job polling; frame browser
static/js/catalog.js Messier / Caldwell bingo-card pages
static/js/transits.js Transit gallery page; running-indicator logic
templates/           Jinja2 HTML templates
```

### Data flow

```
Filesystem
  └─ scanner.py ──► db.sessions ──► SSE ──► browser (app.js)
                                              │
                         User clicks "Detect" │      User clicks "Stack"     User clicks "Render"
                                              ▼              ▼                      ▼
                       app.py ──► _transit_queue     _stack_queue (thread)   _comet_jobs (thread)
                                       │                     │                      │
                              [copy to local SSD]   stack_processor.py    comet_processor.py
                                       │                     │                      │
                              transit_detector.py    sigma-clip stack      star alignment (astroalign)
                                       │                     │              nucleus detection
                              yolo_validator.py     stretch + denoise      stars-fixed animation
                                (optional)                   │              nucleus-fixed animation
                                       │            db.stack_jobs ──► SSE  track composite
                              db.transit_events ──► SSE ──► browser        frame review JPEGs
                                       │                                           │
                              aircraft_lookup.py (OpenSky)            job status polling ──► browser
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

## Comet Wizard

The Comet Wizard processes `_sub` comet folders (containing individual Seestar FITS stacks)
into a set of animations and composites that reveal the comet's motion and structure.  Open
it from a comet session card via **Open in Wizard →**, or navigate directly to `/comet`.

When the wizard detects that a directory has already been processed it shows a green
**"This directory has already been processed — View results →"** banner at the top of
Step 1, allowing you to jump straight to the existing outputs.

### Wizard steps

**Step 1 — Select frames**

- Paste a directory path (or arrive via the session card deep-link `?dir=`)
- **Find comets** button discovers all comet directories under `SEESTAR_DATA_DIR` automatically
- Scan reads FITS headers; each frame is shown as a thumbnail card with its date, exposure, and sub-count
- Click cards to toggle rejection; shift-click for range selection; session-night grouping buttons allow bulk accept/reject
- Tune stretch parameters (sky percentile, white-point, gamma, noise reduction) with a **live preview** rendered from the highest-nsubs frame — the preview updates as you move the sliders
- **Force re-align** checkbox: ignores the `comet_alignment.json` cache and recomputes star alignment and nucleus detection from scratch.  Use this after correcting a nucleus misdetection so the new hint takes effect even if a cache already exists for star transforms.

**Step 2 — Parameters & render**

- Summary of selected frames, FPS, crop window size, and stretch settings
- Elapsed time counter ticks while the job runs
- Progress bar and live log tail from the processor subprocess

**Step 3 — Results**

Four outputs are produced and shown as cards:

| Output | Description |
|---|---|
| `comet_stars_fixed.mp4` | Stars aligned — background stars are fixed, comet nucleus drifts across the field showing its motion over days/weeks.  All frames share the same dimensions (union canvas); border regions not covered by a given frame are filled with real sky averaged from the other frames |
| `comet_nucleus_fixed.mp4` | Nucleus fixed — crop window follows the comet; stars trail behind; coma and tail structure accumulate |
| `comet_stack.jpg` | Composite stack — mean of all star-aligned frames, stretched; stars are sharp, comet is smeared along its path; no annotations |
| `comet_track.jpg` | Track composite — reference frame with nucleus path marked as colour-coded dots |
| `_frames/*.jpg` | Annotated frame review — each frame with nucleus marker (circle + crosshair) overlaid for inspection |

The **Frame review** panel (collapsible) shows all annotated frames in a scrollable filmstrip
with a full-resolution viewer and Prev/Next navigation.

#### Correcting a misdetected nucleus

If the nucleus marker in the frame review is on the wrong object (e.g. a nearby bright star):

1. Click any frame to open it in the viewer
2. Click **⊕ Fix nucleus** (amber button in the viewer nav) — the frame gets a crosshair cursor
3. Click the actual comet coma — the fractional position is stored as `state.nucleusHint`
4. An amber banner confirms the correction: **"Re-render with correction →"**
5. Click it — a new render runs, passing the corrected coordinates to the processor

The correction is applied **independently to every frame** via the inverse similarity
transform for that frame.  This is critical: a position clicked in the aligned-frame viewer
is un-rotated and un-translated back to each raw frame's pixel space before being used as
the search seed.  If a rolling hint were used instead, one bad detection would cascade
forward through every subsequent frame, causing the nucleus-fixed animation to "dance"
between the comet and the star.

---

### Pipeline passes (`comet_processor.py`)

| Pass | Description |
|---|---|
| 1 | **Star alignment** — `astroalign` finds a similarity transform (rotation + scale + translation) between each frame and the reference frame (highest nsubs).  Up to 60 control points; σ = 5.0 source-detection threshold.  Results cached to `comet_alignment.json`. |
| 2 | **Nucleus detection** — diffuseness scoring on each raw (unaligned) frame; positions transformed into aligned coordinates and saved to the same cache. |
| 3a | **Union canvas sizing** — FITS headers are read (no pixel decode) to find each source frame's pixel dimensions; the four corners of every frame are projected through their alignment transform into reference-frame coordinates.  The bounding box of all corners defines an expanded *union canvas* guaranteed to contain every frame without clipping. |
| 3b | **Fill composite** — every frame is warped onto the union canvas at `INTER_LINEAR` quality and accumulated into a running `float64` sum + count array.  The result is `composite = sum / count` — a per-pixel average of all real-sky data from every frame that covered that location.  Corner slivers covered by no frame at all are inpainted with `cv2.INPAINT_TELEA` from their neighbours. |
| 3c | **Stars-fixed animation** — each frame is warped onto the union canvas at `INTER_LANCZOS4` quality; uncovered border pixels are filled from the composite (real sky, real stars) rather than left black; the filled frame is stretched and written to MP4.  Annotated copies (with nucleus marker) written to `_frames/`. |
| 4 | **Nucleus-fixed animation** — each frame translated so the detected nucleus lands at the crop-window centre; written to MP4. |
| 5 | **Track composite** — per-pixel median of all aligned frames forms the star background; nucleus path plotted as cyan dots. |

The cache (`comet_alignment.json`) stores both the per-frame similarity transforms and the
detected nucleus positions.  On subsequent renders with the same frame set only the video
encoding passes need to re-run — star alignment and nucleus detection are skipped.  When a
user nucleus correction is provided the star transforms are still loaded from cache (fast)
but nucleus detection re-runs for every frame with the corrected hint; the cache is then
updated with the new positions.

---

### Nucleus detection — theory of operation

#### Why raw brightness fails

The most obvious approach — find the brightest pixel in the frame — is fooled by any nearby
star that is intrinsically brighter than the comet's nucleus.  Even blurring with a modest
kernel (σ ≈ 12 px) cannot reliably suppress stars that are substantially brighter than the
diffuse coma.

#### Diffuseness score

The detector instead computes a **diffuseness score** that rewards spatially extended
sources over point sources:

```
background  = GaussianBlur(roi, σ=60)          # slow large-scale gradient
residual    = clip(roi − 0.95·background, 0)   # remove sky gradient

small_blur  = GaussianBlur(residual, σ=4)      # ~star-sized kernel
large_blur  = GaussianBlur(residual, σ=25)     # ~coma-sized kernel
ε           = 99th-percentile(residual) × 0.05 + 1.0   # noise floor

score = large_blur² / (small_blur + ε)
```

**Why this works:**

| Source | small_blur peak | large_blur peak | score |
|---|---|---|---|
| Point star (2–5 px FWHM) | high | very low (energy spread over ≈75 px diameter) | **low** |
| Comet coma (50–150 px) | moderate | still high (extended source survives large blur) | **high** |

The comet wins the score even when a nearby star is 5–10× brighter at the pixel level.
The ε floor prevents dark noise patches from achieving spuriously large ratios.

#### Centroiding

After `minMaxLoc` identifies the approximate score peak, a ±40 px window around that peak
is extracted and the **intensity-weighted centroid** is computed:

```
centroid_x = Σ(x · score[x,y]) / Σ score[x,y]   over the window
centroid_y = Σ(y · score[x,y]) / Σ score[x,y]
```

The single brightest pixel in the score map is noisy — atmospheric seeing and sub-count
variation shift the apparent peak by 10–30 px between frames, causing visible left/right
jitter in the nucleus-fixed animation's crop window.  The centroid averages over the entire
coma peak, giving a sub-pixel stable centre that tracks the photometric barycentre of the
coma rather than its noisiest bright speckle.

#### Search region

Detection is constrained to a circle of radius 40 % of `min(H, W)` centred on the raw
frame centre.  The Seestar re-points to the comet at the start of each session, so the
nucleus is always near the frame centre in the raw (unaligned) FITS data regardless of
how different the star backgrounds are between nights.

#### Hint strategies

| Situation | Hint used | Rationale |
|---|---|---|
| No user correction, first frame | Frame centre | Seestar always centres the comet |
| No user correction, subsequent frames | Previous frame's detected position *(rolling hint)* | Tracks slight intra-session drift; keeps the search from wandering to a star that happens to be brighter in that frame |
| User correction provided, any frame | Fixed offset from raw-frame centre, applied identically to every frame | Applied independently per frame — bad detections cannot cascade |

**Rolling hint (no user correction):** After each successful detection the detected raw-frame
position is used as the search centre for the next frame.  This handles cases where the
comet has drifted slightly from the frame centre within a long session.  The risk — that a
single bad detection contaminates all subsequent frames — is accepted as a trade-off because
the Seestar's comet tracking is generally reliable enough to keep the nucleus within the
large search radius.

**Per-frame user hint (correction mode):** The user clicks a position in the *aligned*
annotated-frame viewer.  The correction is expressed as a **fixed offset from raw-frame
centre** and applied uniformly to every frame:

```
δx = click_aligned_x − ref_w / 2
δy = click_aligned_y − ref_h / 2

hint for every frame i:  (frame_w/2 + δx,  frame_h/2 + δy)
```

**Why not per-frame inverse transforms?**  The aligned nucleus position changes
frame-to-frame (the comet moves relative to the stars — that is the whole point of the
stars-fixed animation).  Applying frame i's inverse similarity transform to the
reference-frame click coordinates would map the hint to a *different* raw-space position in
each frame — most of which would be incorrect.

The key insight is that the Seestar re-points to the comet before every session, so the
nucleus is always near raw-frame centre.  The user's correction therefore conveys: *"the
nucleus is δ pixels away from frame centre"* — a spatial offset that is approximately
constant across all frames.  Applying the same (δx, δy) to every frame correctly guides the
detector to the actual coma regardless of which annotated frame the user clicked on.

The reference frame has an identity (or near-identity) transform, so its aligned coordinates
are essentially the same as its raw coordinates — making the δ calculation exact for that
frame and a good approximation for all others.

---

## Activity Heatmap

`/activity` shows a full-year calendar heatmap of observation history.

- **Subs mode** — cell colour encodes the total number of stacked sub-frames recorded that day
- **Sessions mode** — cell colour encodes the number of distinct observation sessions
- Hover over any cell to see a tooltip with the date, session count, sub count, video duration, and object types observed

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

### Pages

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Sessions browser |
| `GET` | `/catalog/messier` | Messier bingo-card page |
| `GET` | `/catalog/caldwell` | Caldwell bingo-card page |
| `GET` | `/transits` | Transit gallery page |
| `GET` | `/activity` | Activity heatmap page |
| `GET` | `/comet` | Comet wizard page |
| `GET` | `/impacts` | Lunar impact events page |

### Sessions & scanning

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/sessions` | JSON: all sessions with thumbnails, sub-counts, animation paths, image files |
| `GET` | `/api/thumbnail/<name>` | Resized session thumbnail JPEG |
| `GET` | `/api/image?path=<path>` | Serve any JPG/PNG/TIF resized to 1400 px (used by lightbox) |
| `GET` | `/api/status` | JSON: scan state, last scan time, session count |
| `POST` | `/api/scan` | Start scan — body: `{"force": bool}` |
| `GET` | `/api/events` | SSE stream (sessions, progress, transit events, stack progress) |

### Catalog

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/catalog/<type>` | JSON: Messier or Caldwell catalog with capture status |

### Transit detection

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/transit/detect` | Queue transit detection — body: `{"session_name": str, "force": bool}` |
| `GET` | `/api/transit/all` | JSON: all video jobs and detected events grouped by session |
| `GET` | `/api/transit/gallery` | JSON: all transit events as a flat list (newest first) |
| `GET` | `/api/transit/clip/<id>` | Stream MP4 clip for event |
| `GET` | `/api/transit/thumb/<id>` | Serve hero-frame JPEG for event |
| `GET` | `/api/transit/running` | JSON: `{"types": [...]}` — video types with actively running jobs |
| `POST` | `/api/transit/pause` | Pause transit worker |
| `POST` | `/api/transit/resume` | Resume transit worker |
| `POST` | `/api/transit/cancel` | Cancel jobs — body: `{"session_name": str}` or `{"all": true}` |

### Sub-frame stacking

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/stack/start` | Queue stacking — body: `{"session_name": str, "force": bool}` |
| `GET` | `/api/stack/status` | JSON: all stack job statuses keyed by session name |
| `GET` | `/api/stack/image/<session_name>` | Serve full-size stacked JPEG |

### Comet wizard

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/comet/scan` | Scan a directory for FITS files — body: `{"directory": str}`; returns file list with headers |
| `POST` | `/api/comet/preview-frame` | Render a single FITS frame with custom stretch params — body: `{path, sky_pct, high_pct, gamma, noise, width}`; returns JPEG |
| `POST` | `/api/comet/render` | Launch processing job — body: `{directory, files[], fps, gamma, sky_pct, high_pct, noise, crop, max_frames, no_cache}`; returns `job_id` |
| `GET` | `/api/comet/status?job_id=<id>` | JSON: job status, progress pct, log lines |
| `POST` | `/api/comet/cancel` | Cancel a running job — body: `{"job_id": str}` |
| `GET` | `/api/comet/check?dir=<path>` | JSON: which outputs exist (`stars_mp4`, `nucleus_mp4`, `track_jpg`, `frame_count`) |
| `GET` | `/api/comet/output?path=<path>` | Serve a comet output file (MP4 or JPEG) by absolute path |
| `GET` | `/api/comet/thumb?path=<path>` | Serve a cached FITS-rendered thumbnail JPEG |
| `GET` | `/api/comet/frames?dir=<path>` | JSON: list of annotated frame JPEGs in `{dir}/_frames/` |
| `GET` | `/api/comet/info?name=<name>` | JSON: JPL SBDB designation and orbit class for a comet name |
| `GET` | `/api/comet/discover` | JSON: all comet session directories found under `SEESTAR_DATA_DIR` |

### Activity

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/activity` | JSON: per-day observation counts (subs, sessions, video seconds, object types) |

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
