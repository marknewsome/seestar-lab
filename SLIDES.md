# Seestar Lab

**A local web app for your Seestar S50 data**

---

## What is Seestar Lab?

- A self-hosted web application that runs entirely on your own machine
- No cloud accounts, no subscriptions, no data leaves your network
- Point it at your Seestar data directory and open a browser
- Automatically discovers sessions, matches catalog objects, and drives processing pipelines
- Built on Python / Flask — runs on Windows (WSL), macOS, and Linux

---

## Session Browser

- Scans your data directory and presents every observation session as a card
- Each card shows a thumbnail (stacked output, enhanced JPEG, or video cover frame)
- Hover to zoom; click to open a full-screen image gallery
- Sessions are matched against the **Messier** and **Caldwell** catalogs automatically
- Bingo-card views show which objects you have captured, with type filters and progress bar
- Hover to pick the best thumbnail for any multi-night session — choice survives rescans
- Calendar activity heatmap displays daily sub counts or session counts

---

## Sub-Frame Stacking

- One-click pipeline for `_sub` session folders containing raw `.fit` sub-frames
- Hybrid pipeline: Python for quality selection, **Siril CLI** for registration + stacking:
  1. Laplacian sharpness + SEP (FWHM / eccentricity / SNR) quality scoring
  2. Frame selection — Stage A sharpness floor; Stage B score-relative quality floor (`min_quality`); cap at `max_frames`
  3. Pre-debayer each frame to 3-channel RGB FITS (prevents Bayer-grid registration artefacts)
  4. Siril: star-pattern registration → additive-scale sigma-clip stack (3σ)
  5. IQR-based border crop (detects partial-coverage rows by pixel-to-pixel variance)
  6. SEP per-channel 2D mesh background subtraction — `bg_mesh_scale` tunes cell size (coarse for large galaxies, fine for compact nebulae; 0 = skip)
  7. **GraXpert AI denoising on the linear stack** (before stretching — optimal noise model; outcome logged)
  8. Asinh stretch (Q=6) + YCrCb denoise + unsharp mask → JPEG, vertically flipped to match Seestar orientation
- GPU-accelerated via CUDA; 3000 frames ~87 min on consumer hardware
- **Re-render** reprocesses saved linear FITS without a full restack — try different `bg_mesh_scale` in seconds
- Stack queue at `/stack/jobs` — history, durations, log and image links; live SSE progress

---

## Catalog Poster Printing

- One click opens a full-catalog poster optimised for **13×19" paper** (e.g. Canon Pro 100)
- Landscape layout: 11 columns × 10 rows — fits all 110 Messier or 109 Caldwell objects on one page
- Captured objects show their best thumbnail, label, popular name, and ✓ badge
- Uncaptured objects show a dark muted placeholder — a true visual scoreboard
- Progress header: catalog name · ✓ N / 110 · Seestar Lab · date

---

## Comet Wizard

- Step-by-step pipeline for comet `_sub` folders (individual Seestar FITS stacks)
- Frame selection with per-card toggle and session-night bulk accept/reject
- Live stretch preview updates as you move sliders
- Produces four outputs:
  - **Stars-fixed animation** — comet drifts across a fixed star field
  - **Nucleus-fixed animation** — coma and tail structure accumulate as stars trail
  - **Track composite** — reference frame with colour-coded nucleus path
  - **Annotated frame review** — per-frame nucleus marker for inspection
- Nucleus misdetection correction: click the actual coma in the viewer and re-render

---

## Solar Timelapse Wizard

- Processes a directory of Seestar solar MP4 clips into a disk-normalised VFR timelapse
- 3-pass pipeline in `solar_processor.py`:
  - Pass 1: `HoughCircles` disk detection, Laplacian sharpness scoring — **results cached**
  - Pass 2: Affine normalisation, background subtraction, gamma stretch — **streamed to temp JPEGs one frame at a time** (O(1) RAM)
  - Pass 3: `ffconcat` references the already-written JPEGs directly — no second in-memory copy; temp dir cleaned up on completion
- Re-renders using cached disk data take seconds, not minutes
- Tunable parameters: sample interval, speedup factor, gamma, white point, stabilisation window, min quality

---

## Lunar Timelapse Wizard

- Same wizard flow as the Solar wizard, tailored for lunar sessions
- Three render modes: **Standard**, **Enhanced**, **Surface detail**
- Configurable stretch and quality parameters
- Cancel and back-to-parameters navigation at every step
- Completed results persisted in browser localStorage — no re-render needed on return visits

---

## Live Capture

- RTSP stream viewer and recorder for one or more Seestar streams simultaneously
- Live MJPEG feed served through a Flask proxy — displays in a plain `<img>` tag, ~1–3 s latency
- Independent recording process writes `ffmpeg -c copy` MP4s directly to `SEESTAR_DATA_DIR/captures/`
- Ideal use cases: lunar eclipses, planetary transits, any timed event
- Stream configurations (name + URL) persisted in browser localStorage

---

## Observing Planner

- Visibility planner for DSO and solar-system objects
- Shows rise/set times and altitude curves for your configured observer location
- Highlights optimal observing windows for the current night
- Uses observer coordinates from the `.env` configuration (`OBSERVER_LAT` / `OBSERVER_LON`)

---

## Architecture

- **Flask** web server — lightweight, no async complexity
- **SQLite** database — zero-config, single file, differential session scanning
- **ffmpeg** — H.264 transcoding, cover art embedding, MJPEG proxy, VFR assembly
- **OpenCV** — transit detection, ECC registration, disk detection, blob tracking
- **astropy / scipy** — coordinate calculations, sigma clipping, polynomial fitting
- **astroalign** — star-pattern alignment for comet wizard
- Entirely local — no cloud dependency, no external API required (OpenSky and ADS-B are optional)

---

## Getting Started

```bash
# 1. Install dependencies
pip install -r requirements.txt          # ultralytics optional for YOLO

# 2. Configure
cp .env.example .env                     # or create from scratch
# Set SEESTAR_DATA_DIR and SEESTAR_OUTPUT_DIR at minimum

# 3. Run
python app.py

# 4. Open in browser
http://127.0.0.1:5000
```

- `ffmpeg` must be on your `PATH`
- Python 3.10+ required
- YOLO model weights (~6 MB) download automatically on first use if `ultralytics` is installed
