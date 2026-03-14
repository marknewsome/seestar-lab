# Seestar Lab

**A local web app for your Seestar S50 data**

---

## What is Seestar Lab?

- A self-hosted web application that runs entirely on your own machine
- No cloud accounts, no subscriptions, no data leaves your network
- Point it at your Seestar data directory and open a browser
- Automatically discovers sessions, matches catalog objects, and drives processing pipelines
- Built on Python / Flask — runs on Windows, macOS, and Linux

---

## Session Browser

- Scans your data directory and presents every observation session as a card
- Each card shows a thumbnail (stacked output, enhanced JPEG, or video cover frame)
- Hover to zoom; click to open a full-screen image gallery
- Sessions are matched against the **Messier** and **Caldwell** catalogs automatically
- Bingo-card views show which objects you have captured across your full history
- Calendar activity heatmap displays daily sub counts or session counts

---

## Sub-Frame Stacking

- One-click pipeline for `_sub` session folders containing raw `.fit` sub-frames
- 7-stage pipeline runs entirely on CPU — no GPU required:
  1. Quality selection (Laplacian sharpness scoring)
  2. ECC image registration
  3. Sigma-clip mean stack
  4. Background gradient removal
  5. Auto-crop of alignment artefacts
  6. Auto-stretch (percentile clip + gamma)
  7. Bilateral denoise + unsharp mask
- Live progress via Server-Sent Events; stacked JPEG appears as the session thumbnail immediately

---

## Transit Detection

- Background-subtraction + blob-tracking pipeline finds aircraft, birds, and the ISS crossing the solar or lunar disk
- Drift compensation corrects Seestar servo jitter before differencing frames
- Weighted confidence score (linearity, velocity, duration) filters false positives
- Optional **YOLOv8n** second-stage validation confirms visually recognisable aircraft and birds
- Detected events cross-referenced against the **OpenSky Network ADS-B** feed for aircraft ID
- Padded MP4 clips with burned UTC timestamps, hero-frame thumbnails, and JSON sidecars saved automatically

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
