# Seestar Lab

A local web application for browsing, cataloging, and processing observation data from a
[Seestar S50](https://www.zwoastro.com/product/seestar/) smart telescope.  It automatically
discovers session folders, matches objects against the Messier and Caldwell catalogs,
stacks raw FITS sub-frames into publication-quality images, and provides a dedicated
comet-processing wizard that produces star-fixed and comet-fixed animations plus a
track-path composite.

---

## Features

| Feature | Description |
|---|---|
| **Session browser** | Scans the data directory and displays every observation session as a card with thumbnail, dates, sub-count, and video hours |
| **Session thumbnails** | Best-quality image from each session (enhanced JPEG, stacked output, or cover frame) shown on the card; hover-zooms to a larger view; user can pin a preferred thumbnail that survives rescans |
| **Image gallery** | Seestar-stacked JPEGs for non-`_sub` comet sessions are browsable via prev/next arrows on the card thumbnail and a full-screen lightbox |
| **User ratings** | Three-state satisfaction dot on every session card and bingo card: Satisfied (green) / Want more time (amber) / Priority re-image (red). Click to cycle; persists across rescans. "Re-image" filter in the Observing Planner surfaces want-more and priority targets. |
| **Observing notes** | Free-text textarea on each session card for conditions, issues, and goals. Saves automatically on blur or Ctrl+Enter. A truncated snippet with full-text tooltip appears on bingo cards. |
| **Sub-frame stacking** | Hybrid pipeline stacks raw `.fit` sub-frames: sharpness + SEP quality selection in Python, per-frame pre-debayer to 3-channel RGB FITS, then Siril CLI for registration and sigma-clip stacking, followed by IQR border crop and JPEG generation. Configurable frame cap (`max_frames`); cancelable at any point. |
| **Comet wizard** | Step-by-step pipeline for `_sub` comet folders: frame selection, stretch/parameter tuning with live preview, stars-fixed animation, comet-nucleus-fixed animation, track composite, and annotated frame review |
| **Catalog scoreboard** | Messier and Caldwell bingo-card views show which objects have been captured, with progress bar and type filters |
| **Poster printing** | One-click 13×19" landscape poster of the full Messier or Caldwell catalog: captured objects show their thumbnail, uncaptured show a muted placeholder; designed for photo printers |
| **Activity heatmap** | Calendar heatmap showing daily sub counts or session counts across the full observation history |
| **Live updates** | A Server-Sent Events stream pushes progress to the browser in real time — no polling, no page reloads |
| **Solar Timelapse wizard** | 3-step wizard: scan a directory of Seestar solar MP4 clips, tune parameters (sampling, stretch, stabilisation, quality filtering), render a disk-normalised VFR timelapse with title card and portrait. Pass 1 disk-detection results are cached so re-renders are fast. Normalised frames are streamed to disk one at a time — memory usage is O(1) regardless of session length. |
| **Lunar Timelapse wizard** | Step-by-step pipeline for lunar sessions: select videos, choose render mode (Standard / Enhanced / Surface detail), configure stretch and quality parameters, produce an MP4 timelapse with title card. Supports cancel, back-to-parameters, and result persistence. |
| **Live Capture** | RTSP stream viewer and recorder. Add one or more Seestar RTSP URLs; view the live MJPEG feed in the browser; optionally record to `SEESTAR_DATA_DIR/captures/` with auto-named MP4 files. Stream configs are persisted in browser localStorage. |
| **Observing Planner** | Visibility planner for DSO and solar-system objects: shows rise/set times, altitude curves, and optimal observing windows for the configured observer location. |
| **Result persistence** | Completed solar and lunar timelapse results are saved in browser localStorage (keyed by directory, 30-day TTL). Returning to a previously-rendered directory shows the results without re-rendering. A "Forget" button clears the saved state; "Re-render" re-runs Pass 2+3 using the cached disk-detection data. |
| **Stack queue** | `/stack/jobs` shows all stacking jobs: active (running job with live progress bar + queued jobs with pulsing indicator) and history (completed/failed jobs with frame counts, wall-clock duration, and links to the result image and run log). Live-updated via SSE. |

---

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH` (for H.264 transcoding and cover-art embedding)
- OpenCV, Flask, scipy, astropy — see `requirements.txt`

```
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root (or set environment variables):

```ini
# Required
SEESTAR_DATA_DIR=/mnt/d/xfer          # Root directory to scan for sessions
SEESTAR_OUTPUT_DIR=/mnt/d/seestar-lab # Where processed output files are written

# Optional — observer location (used by the Observing Planner)
OBSERVER_LAT=44.5646
OBSERVER_LON=-123.2620
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
app.py               Flask routes, SSE broadcaster, stack job queue, comet job queue
scanner.py           Filesystem crawler; builds and diffs session records
db.py                SQLite persistence (sessions, scanned dirs, stack jobs, meteor impacts)
stack_processor.py   Sub-frame stacking pipeline (quality selection, pre-debayer, Siril registration+stack, IQR crop)
comet_processor.py   Comet animation pipeline (star alignment, nucleus detection, animations, track composite)
catalogs.py          Messier / Caldwell catalog data and DSO type/group mappings
object_catalog.py    Object-type detection (solar/lunar/planet/comet/messier/…) and descriptions
static/js/app.js     Sessions-page UI; SSE client; thumbnail picker; stack controls; lightbox
static/js/comet_wizard.js  Comet wizard multi-step UI; frame grid; preview; job polling; frame browser
static/js/catalog.js       Messier / Caldwell bingo-card pages
solar_processor.py         Solar disk-normalised timelapse pipeline
static/js/solar_wizard.js  Solar timelapse wizard UI — 3-step flow, per-directory localStorage persistence
static/js/lunar_wizard.js  Lunar timelapse wizard UI — mode selection, cancel/back support, result persistence
static/js/capture.js       Live capture page — RTSP stream cards, MJPEG viewer, recording start/stop
templates/                 Jinja2 HTML templates (including poster.html for 13×19" print)
```

### Data flow

```
Filesystem
  └─ scanner.py ──► db.sessions ──► SSE ──► browser (app.js)

                    User clicks "Stack"           User clicks "Render" (Comet)
                           ▼                              ▼
                  _stack_queue (thread)          _comet_jobs (thread)
                           │                              │
                  stack_processor.py            comet_processor.py
                           │                              │
                  sigma-clip stack              star alignment (astroalign)
                  stretch + denoise             nucleus detection
                           │                    stars-fixed animation
                  db.stack_jobs ──► SSE         nucleus-fixed animation
                           │                    track composite
                  job status polling ──► browser frame review JPEGs

                    User clicks "☀ Start Render"       User clicks "⏺ Record"
                              ▼                                ▼
                 app.py ──► _solar_jobs (thread)    _capture_recs (thread)
                                  │                          │
                      solar_processor.py              ffmpeg -c copy
                        Pass 1: HoughCircles              to DATA_DIR/captures/
                        Pass 2: normalise + stretch
                                  │ (stream to tmp JPEGs — O(1) RAM)
                        Pass 3: ffconcat from tmp JPEGs
                                  │ (tmp dir cleaned up on completion)
                       solar_fulldisk.mp4
                       solar_portrait.jpg
                       solar_alignment.json (cache)
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

The pipeline is split between Python (quality selection and pre-debayering) and the
[Siril](https://siril.org/) CLI (registration and stacking).  Siril must be installed on
Windows and reachable at `C:\Program Files\Siril\bin\siril-cli.exe` from WSL2.

| # | Stage | Who | Details |
|---|---|---|---|
| 1 | **Sharpness scan** | Python | Each FITS file is read as raw Bayer uint16. Laplacian-variance sharpness is scored on the centre quarter. |
| 2 | **SEP quality metrics** | Python | Source Extractor Python (SEP) measures FWHM, eccentricity, and SNR per frame. Combined score = `(stars × SNR) / FWHM`. |
| 3 | **Frame selection** | Python | Frames below 40 % of median sharpness are rejected (Stage A). Survivors ranked by combined score; those below `best_score × min_quality` are rejected (Stage B quality floor); remainder capped at `max_frames`. |
| 4 | **Pre-debayer** | Python | Each selected frame is debayered to BGR, converted to float32, transposed to 3-channel (R,G,B) FITS, and written to `C:\Temp\seestar_siril_{ts}\light_NNNNN.fit`. Siril receives proper colour images — no Bayer-grid registration artifacts. |
| 5 | **Convert + Register** | Siril | `convert light -out=pp_light` collects all `light*.fit` files into a Siril sequence. `register light_` computes inter-frame transforms using star-pattern matching. |
| 6 | **Sigma-clip stack** | Siril | `stack r_light_ rej 3 3 -norm=addscale -out=stacked` integrates frames with additive-scale normalisation and 3σ rejection, suppressing hot pixels, cosmic rays, and satellite trails. |
| 7 | **IQR border crop** | Python | Per-row inter-quartile range of the green channel identifies partial-coverage border rows left by registration. Rows with IQR > 1.5 × median IQR of the image centre are trimmed from top and bottom; leftmost/rightmost fully-covered columns are found by luminance mask. |
| 8 | **Background subtraction** | Python | SEP sigma-clipped 2D mesh background subtraction applied per channel. `bg_mesh_scale` controls mesh coarseness: higher = fewer, larger cells (better for large galaxies like M101 where fine cells over-subtract galaxy signal); 0 = skip entirely. |
| 9 | **AI denoising** | GraXpert | GraXpert ONNX model denoises the linear float32 stack before any stretch is applied. Linear data has Gaussian noise characteristics; denoising here gives the model cleaner signal than nonlinearly stretched output would. GPU-accelerated via CUDA when available. Whether GraXpert ran (or fell back) is recorded in the run log. |
| 10 | **Save FITS** | Python | Denoised linear float32 colour FITS written alongside the output directory for later re-rendering without a full restack. |
| 11 | **JPEG preview** | Python | Per-channel asinh stretch (Q=6), YCrCb chroma + luma Gaussian denoise, gentle unsharp mask; JPEG quality 95. Output is vertically flipped to match Seestar app orientation. |

Output is registered as the session thumbnail immediately — visible without a rescan.

Work files (~24 MB × 500 frames ≈ 12 GB) are written to `C:\Temp` and cleaned up on
completion, error, or cancel.

### max_frames and min_quality

`max_frames` (default 500) caps how many frames pass stage 3.  SNR scales as √N, so
returns diminish quickly: 500 → 1000 frames is only +41 % gain.  A practical sweet spot is
**1 500 – 3 000 frames** for a rich session like M 101.

`min_quality` (default 0.0 = off) applies a score-relative floor before the cap: frames
scoring below `best_score × min_quality` are rejected regardless of how many remain.
For example, `min_quality=0.4` keeps only frames that score at least 40 % of the best
frame's score, discarding cloud-degraded or poor-seeing subs even if `max_frames` has not
been reached.  The number of floor-rejected frames appears in the run log.

### Memory usage

The main in-process cost is the pre-debayer loop (one frame at a time — O(1) RAM).  Siril
handles the registration and integration natively, so WSL2 peak RAM is modest compared to
the old Python pipeline.  Disk space for work files is the main constraint: allow ~25 MB per
frame (≈ 12 GB for 500 frames).  If WSL2 memory pressure is observed elsewhere, set a
limit in `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=16GB
```

### Cancel

A **Cancel** button appears while stacking is active.  It signals the pipeline to stop
cleanly after the current frame finishes.  The DB is marked as cancelled and the stack footer
reverts to idle state; no partial output is written.

### Re-stack and Re-render

A **Re-stack** button replaces the Stack button once a job has completed or failed, allowing
re-stacking (e.g. after adjusting `max_frames`, `min_quality`, or `bg_mesh_scale`).

**Re-render** reprocesses the saved linear FITS through background subtraction, GraXpert
denoising, stretch, and sharpening — without rerunning the full alignment stack.  Useful
for tuning `bg_mesh_scale` or comparing stretch settings in seconds rather than hours.

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

## Solar Timelapse Wizard

The Solar Timelapse Wizard processes directories of Seestar solar video clips into a
disk-normalised VFR timelapse and a sharpness-ranked portrait.  Navigate to `/solar`.

### Pipeline passes (`solar_processor.py`)

| Pass | Description |
|---|---|
| 1 | **Disk detection** — Each source video is sampled at ~1 frame/second. Timestamps are parsed from the Seestar filename (`YYYY-MM-DD-HHMMSS`). Each frame is converted to grayscale and `HoughCircles` locates the solar disk. A Laplacian variance score estimates limb sharpness. Frames whose detected radius deviates more than 15 % from the per-video median are rejected. Results cached to `solar_alignment.json`. |
| 2 | **Normalisation** — Each accepted frame is warped to a fixed `out_size × out_size` canvas so the disk fills ~86 % of the frame. Background subtraction and gamma stretch are applied. Each normalised frame is written immediately to a temporary JPEG on disk rather than accumulated in RAM — this keeps peak memory at one frame in flight regardless of how many frames the session contains. |
| 3 | **Assembly** — Frames are sorted by UTC timestamp. VFR display durations are computed from real time gaps divided by the speedup factor, clamped to [1/60, 5] s. A title card (3 s) is prepended. The `ffconcat` demuxer references the already-written Pass 2 JPEGs directly — no second in-memory copy. The final H.264 MP4 and a portrait JPEG (sharpest frame + label overlay) are written to the output directory; the temporary frames are cleaned up automatically. |

### Parameters

| Parameter | Default | Description |
|---|---|---|
| Sample interval | 1.0 s | Seconds between extracted frames per video |
| Speedup | 1800× | 30 real minutes → 1 second of timelapse |
| Gamma | 0.7 | Tone-mapping exponent (lower = brighter/more contrast) |
| White point % | 99.5 | Percentile mapped to white |
| Sky percentile | 5 % | Background sample percentile for subtraction |
| Output size | 1080 px | Square canvas side length |
| Min quality | 5 | Laplacian variance threshold; raise to reject blurry/occluded frames |
| Stabilise | 0 | Gaussian rolling-average window (frames) applied to detected disk-centre positions before normalisation; smooths wind jitter |

Re-rendering with different parameters re-runs only Passes 2 and 3 — Pass 1 is read from the JSON cache, making re-renders take seconds rather than minutes.

---

## Lunar Timelapse Wizard

The Lunar Timelapse Wizard processes directories of Seestar lunar video clips into an MP4
timelapse with a title card.  Navigate to `/lunar`.

The wizard supports selecting videos, choosing a render mode (Standard / Enhanced / Surface
detail), configuring stretch and quality parameters, and producing the timelapse.  Cancel
and back-to-parameters navigation are available at any step.  Completed results are
persisted in browser localStorage so returning to a previously-rendered directory restores
the output without re-rendering.

---

## Live Capture

The Capture page (`/capture`) provides a live RTSP viewer and optional recorder for one or
more Seestar streams.  Useful for watching lunar eclipses, planetary transits, or any event
where you want a persistent recording alongside the live view.

### How it works

- **Live view** — Flask proxies the RTSP stream through `ffmpeg`, re-encoding as MJPEG and
  serving it as `multipart/x-mixed-replace` to a plain `<img>` tag.  No JavaScript library
  is required.  Latency is ~1–3 seconds.
- **Recording** — a separate `ffmpeg -c copy` process writes the stream directly to an MP4
  file in `SEESTAR_DATA_DIR/captures/`, preserving the original H.264 stream without
  re-encoding.  Recording and viewing run independently.
- **Persistence** — stream configurations (name + URL) are saved in browser localStorage.
  Configured streams reappear on the next visit.

Output files are named `{stream_name}_{YYYYMMDD_HHMMSS}.mp4` and land in
`SEESTAR_DATA_DIR/captures/` alongside other raw Seestar data.

---

## Activity Heatmap

`/activity` shows a full-year calendar heatmap of observation history.

- **Subs mode** — cell colour encodes the total number of stacked sub-frames recorded that day
- **Sessions mode** — cell colour encodes the number of distinct observation sessions
- Hover over any cell to see a tooltip with the date, session count, sub count, video duration, and object types observed

---

## Transit Detection

> **Note:** Transit detection (aircraft, birds, ISS crossing the solar/lunar disk) has moved
> to the separate [seestar-transit-finder](../seestar-transit-finder) project, which provides
> a more capable dedicated pipeline.

---

## Lunar Impact Events

The `/impacts` page displays confirmed dark-side flash events (candidate meteor impacts)
detected in lunar video sessions.  Events are stored in the `meteor_impacts` table with
clip path, thumbnail, centroid, peak brightness, and frame timestamps.

---

## API Reference

### Pages

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Sessions browser |
| `GET` | `/catalog/messier` | Messier bingo-card page |
| `GET` | `/catalog/caldwell` | Caldwell bingo-card page |
| `GET` | `/catalog/messier/poster` | Messier 13×19" print poster |
| `GET` | `/catalog/caldwell/poster` | Caldwell 13×19" print poster |
| `GET` | `/activity` | Activity heatmap page |
| `GET` | `/comet` | Comet wizard page |
| `GET` | `/impacts` | Lunar impact events page |
| `GET` | `/solar` | Solar timelapse wizard |
| `GET` | `/lunar` | Lunar timelapse wizard |
| `GET` | `/planner` | Observing planner |
| `GET` | `/capture` | Live RTSP capture page |

### Sessions & scanning

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/sessions` | JSON: all sessions with thumbnails, sub-counts, animation paths, image files |
| `GET` | `/api/thumbnail/<name>` | Resized session thumbnail JPEG |
| `GET` | `/api/image?path=<path>` | Serve any JPG/PNG/TIF resized to 1400 px (used by lightbox) |
| `GET` | `/api/status` | JSON: scan state, last scan time, session count |
| `POST` | `/api/scan` | Start scan — body: `{"force": bool}` |
| `GET` | `/api/events` | SSE stream (sessions, progress, stack progress) |

### Catalog

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/catalog/<type>` | JSON: Messier or Caldwell catalog with capture status |

### Session thumbnails, ratings, and notes

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/session/<name>/images` | JSON: all image files for a session + current pinned thumbnail path |
| `POST` | `/api/session/<name>/pin-thumbnail` | Pin a specific image as the session thumbnail (persists across rescans) |
| `POST` | `/api/session/<name>/rate` | Set user rating — body: `{"rating": "satisfied"|"want_more"|"priority"|null}` |
| `POST` | `/api/session/<name>/notes` | Set observing notes — body: `{"notes": str}` |

### Sub-frame stacking

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/stack/start` | Queue stacking — body: `{"session_name": str, "force": bool, "max_frames": int, "bg_mesh_scale": int, "min_quality": float, "skip_copy": bool}` |
| `POST` | `/api/stack/cancel` | Cancel active job — body: `{"session_name": str}` |
| `POST` | `/api/stack/rerender` | Re-render from saved linear FITS — body: `{"session_name": str, "bg_mesh_scale": int}` |
| `GET` | `/api/stack/status` | JSON: all stack job statuses keyed by session name |
| `GET` | `/api/stack/image/<session_name>` | Serve full-size stacked JPEG |
| `GET` | `/api/stack/log/<session_name>` | Serve plain-text run log |

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

### Solar Timelapse

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/solar/scan` | Scan a directory for solar video files — body: `{"directory": str}`; returns file list with dates and durations |
| `POST` | `/api/solar/render` | Launch render job — body: `{directory, files[], size, sample_interval, speedup, gamma, sky_pct, high_pct, no_cache, stab_window, min_quality}`; returns `job_id` |
| `GET` | `/api/solar/status?job_id=<id>` | JSON: job status, progress pct, log lines, outputs |
| `POST` | `/api/solar/cancel` | Cancel a running job — body: `{"job_id": str}` |
| `GET` | `/api/solar/output?path=<path>` | Serve a solar output file (MP4 or JPEG) by absolute path |

### Lunar Timelapse

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/lunar/scan` | Scan for lunar video sessions |
| `POST` | `/api/lunar/render` | Launch render job |
| `GET` | `/api/lunar/status?job_id=<id>` | Job status |
| `POST` | `/api/lunar/cancel` | Cancel a running job |

### Live Capture

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/capture/mjpeg?url=<rtsp_url>` | MJPEG proxy — streams `multipart/x-mixed-replace` from ffmpeg decoding the RTSP source |
| `POST` | `/api/capture/record/start` | Start recording — body: `{"rtsp_url": str, "name": str}`; returns `{rec_id, out_path}` |
| `POST` | `/api/capture/record/stop` | Stop recording — body: `{"rec_id": str}`; returns `{out_path, size_bytes}` |
| `GET` | `/api/capture/record/status` | JSON: list of active recordings with `{id, name, elapsed_s, size_bytes, out_path}` |

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
| `stack_progress` | `session_name`, `status`, `pct`, `stage`, `frames_total`, `frames_accepted` | Stacking pipeline progress |
| `stack_done` | `session_name`, `status`, `frames_total`, `frames_accepted`, `output_path` | Stacking complete (or failed) |

---

## Testing

Unit tests cover the pure functions in `stack_processor.py` (quality scoring, frame
selection, background subtraction, stretch, SCNR, crop, etc.) using synthetic numpy arrays —
no FITS files or external tools required.

```bash
./run_tests.sh          # activates venv and runs pytest -v
```

41 tests run in under 0.1 s.  See `tests/test_stack_processor.py`.

---

## Database

SQLite at `seestar-lab.db` in the project root.  The schema is created automatically on
startup; new columns are added with `ALTER TABLE` for backwards compatibility.

| Table | Purpose |
|---|---|
| `sessions` | One row per observation object (M42, Solar, etc.); includes `pinned_thumbnail`, `user_rating`, and `notes` columns that survive rescans |
| `scanned_dirs` | Directory paths + mtimes for differential scanning |
| `meta` | Key-value store (last scan time, data dir) |
| `stack_jobs` | One row per sub-frame stacking job; tracks status, progress percentage, pipeline stage, frame counts, and output path |
| `meteor_impacts` | One row per confirmed dark-side lunar flash event (candidate meteor impact) |
