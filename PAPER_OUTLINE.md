# Paper Outline — Seestar Lab

*Working title: "Seestar Lab: A Local-First Data Pipeline for the ZWO Seestar S50 Smart Telescope"*

Venue target: PASP (Publications of the Astronomical Society of the Pacific) Software Description  
or JOSS (Journal of Open Source Software)

---

## Abstract (target ~200 words)

- Problem: Consumer smart telescopes produce large volumes of FITS sub-frames and MP4 clips
  with minimal post-processing tooling beyond the vendor app
- Contribution: Seestar Lab — open-source, self-hosted web application for end-to-end data
  management and processing of Seestar S50 data
- Key capabilities: session browsing, catalog matching, sub-frame stacking via hybrid
  Python + Siril pipeline, comet animation pipeline, solar/lunar timelapse wizards, live RTSP capture
- Result: production-quality stacked images from 500 sub-frames in ~31 min on consumer hardware;
  comet nucleus detection reliable to 0.5 coma-diameters from bright stars

---

## 1. Introduction

- The smart-telescope market (ZWO Seestar, Unistellar, etc.) has created a new class of
  data-rich amateur imager: hundreds to thousands of FITS subs per session, no manual
  alignment needed, but limited post-processing options
- Existing stacking tools (Siril, PixInsight, DeepSkyStacker) are capable but not
  integrated: no session browser, no catalog matching, no comet-specific workflow
- Gap: no open-source web app that combines browsing, cataloging, stacking, and
  planetary/comet animation in a single local tool
- Seestar Lab fills this gap; runs on Windows/WSL2, macOS, and Linux; zero cloud dependency

---

## 2. System Architecture

### 2.1 Overview

- Flask + SQLite + SSE architecture; single-process, thread-pool job queue
- All processing local; no telemetry, no cloud API
- Data directories scanned differentially (mtime-gated) on startup and on demand

### 2.2 Session Discovery and Catalog Matching

- Directory walker identifies `_sub`, video, and stacked-image sessions
- Sessions matched against Messier (110 objects) and Caldwell (109 objects) catalogs
- Object type inference from directory name patterns (solar, lunar, planetary, comet, DSO)
- SQLite upsert preserves user-editable fields (rating, notes, pinned thumbnail) across rescans

### 2.3 Session Browser UI

- Thumbnail cards with hover-zoom and full-screen lightbox
- Activity calendar heatmap (sub-counts or session counts)
- Bingo-card catalog scoreboards with type filters and progress bar
- 13×19" print-ready catalog poster (Canon Pro 100 target)

---

## 3. Sub-Frame Stacking Pipeline  *(strongest technical section)*

### 3.1 Motivation

- Seestar's on-device stacking runs on all subs regardless of quality (clouds, vibration, airplane trails)
- Python-only pipeline limitations: ECC registration is slow, sigma-clip integration at 500+ frames
  peaks >12 GB RAM in WSL2; our Python stacker had an unresolved multi-batch accumulation bug
- Solution: hybrid pipeline — Python for quality gating, Siril CLI for registration and stacking

### 3.2 Quality Selection

- Laplacian-variance sharpness score on raw Bayer centre-quarter (Stage A)
- SEP (Source Extractor Python) per-frame FWHM, eccentricity, SNR (Stage B)
- Combined score = `(star_count × SNR) / FWHM`; rewards dense, sharp, high-contrast frames
- Stage A: reject below 40 % of median sharpness
- Stage B quality floor: reject frames below `best_score × min_quality` before applying `max_frames` cap
  — discards cloud-degraded or poor-seeing subs that would otherwise dilute the stack
- Survivors sorted by score (best first) and capped at `max_frames`; floor-rejected count logged separately

### 3.3 Pre-Debayering for Color Registration

- Key insight: Siril's sub-pixel star registration shifts frames by fractional pixels
- Shifting a raw GRBG Bayer frame by (dx, dy) misaligns the 2×2 CFA grid between frames
- Averaging misaligned Bayer grids produces systematic purple/green diagonal band artifacts
  at spatial frequency 0.5 cycle/pixel — visible at full scale as a colored mesh
- Fix: debayer each selected frame in Python (uint16 → float32 RGB, shape (3,H,W)) before
  handing to Siril; Siril registers and stacks proper 3-channel colour images
- Figure: example of Bayer-grid artifact vs. clean pre-debayered stack

### 3.4 Siril Integration

- Windows Siril CLI called from WSL2 via subprocess with `wslpath -w` path conversion
- Siril script: `convert light → register light_ → stack r_light_ rej 3 3 -norm=addscale`
- Additive-scale normalisation equalises sky background across frames before sigma rejection
- Siril sequence naming conventions documented (v1.4.3 specifics: trailing underscore, `r_` prefix)
- Work directory: `C:\Temp\seestar_siril_{ts}\` — Windows-accessible, cleaned up on completion

### 3.5 IQR Border Crop

- Registration leaves partial-coverage border rows where fewer frames contribute
- Naive luminance-mask crop (min > 0) fails: Siril's additive normalisation gives non-zero
  values to partial-coverage pixels
- Fix: per-row inter-quartile range of the green channel
  - Fully-stacked sky rows: low IQR (uniform sky + read noise)
  - Partial-coverage border rows: high IQR (pixel-to-pixel mosaic variance)
  - Threshold = 1.5 × median IQR of middle third; scan only outer third to avoid galaxy signal
  - FITS row 0 = image bottom — scan from H-1 downward to find top border
- Figure: row-IQR profile illustrating the detection

### 3.6 Per-Channel Background Subtraction

- Goal: equalise R/G/B sky levels and remove smooth vignetting gradients
- Tool: SEP (Source Extractor Python) sigma-clipped background mesh per channel
- `bg_mesh_scale` controls mesh cell size: `bw = image_width // bg_mesh_scale`
  - High values (coarse mesh, e.g. 8): better for large galaxies like M 101 — fine cells
    would sample interarm regions as sky and over-subtract galaxy signal
  - Low values (fine mesh, e.g. 40): better for compact nebulae with strong gradients
  - 0: skip background subtraction entirely (useful for targets that fill the frame)
- Sigma-clipping iteratively rejects bright pixels within each cell — nebulosity and galaxy
  signal are excluded from the sky estimate (unlike percentile-of-cell, which is biased by
  any extended emission filling a large fraction of the frame)
- Applied per-channel independently: corrects both global sky pedestal and inter-channel
  colour balance simultaneously
- Motivation: the original single-scalar 5th-percentile subtract was channel-unaware,
  producing residual blue/green cast (visible in M 20 Trifid comparison)
- Note: GRBG Bayer sensors have 2× green photosites; fine meshes differentially
  over-subtract R and B vs. G, producing green-tinted output after clipping — this is the
  primary motivation for the tunable `bg_mesh_scale` parameter

### 3.7 AI Denoising on Linear Data

- GraXpert ONNX model (v3.x) applied to the linear background-subtracted float32 stack
- Key ordering decision: denoise BEFORE stretching
  - Linear data: read noise is Gaussian, background is flat → optimal conditions for the model
  - Stretched data: nonlinear amplification of shadows transforms Gaussian noise into
    asymmetric, spatially varying noise — model performance degrades
- GPU-accelerated via CUDA (onnxruntime-gpu); strength=1.0 (maximum)
- Falls back silently to undenoised FITS if GraXpert unavailable; outcome (ran / fell back)
  recorded in the plain-text run log alongside frame counts and timing

### 3.8 Results

- M 101 (Pinwheel Galaxy): 3000 sub-frames in ~87 min; 1500 frames in ~1h 6m; 500 frames in ~19 min
- M 20 (Trifid Nebula): 60 frames in ~2 min — both red emission and blue reflection lobes
  visible after per-channel background correction
- Full object set processed in a single session: M 1, 20, 27, 31, 36, 42, 97, 101, 108 — all
  queued sequentially; no interference between jobs
- SNR discussion: 500 vs. 1000 vs. 3000 frames; diminishing returns curve; quality-floor
  effect (min_quality=0.4 rejected 1111/4111 frames in one M 101 run, keeping score range 7442–9569)
- JPEG output vertically flipped to match Seestar app display orientation (FITS row 0 = image bottom)

---

## 4. Comet Processing Pipeline  *(second strongest technical section)*

### 4.1 Nucleus Detection via Diffuseness Scoring

- Novel heuristic: diffuseness = large_blur² / (small_blur + ε) where blurs are Gaussian
  at two scales (~3 px and ~15 px)
- Stars: large_blur ≈ small_blur → diffuseness ≈ 1
- Comet coma: large_blur >> small_blur → diffuseness >> 1
- Reliable detection even when a star is 5–10× brighter than the coma at pixel level
- Comparison with centroid-only and peak-brightness approaches: both fail on nearby bright stars

### 4.2 Star Alignment and Dual-Output Animations

- astroalign for star-to-star affine transforms between frames
- Stars-fixed: reference transform applied to all frames; comet drifts
- Nucleus-fixed: inverse comet motion applied; stars trail; coma and tail structure accumulate

### 4.3 Nucleus Correction UI

- Click-to-correct: user clicks actual coma position in browser; coordinates sent to server;
  re-render uses corrected nucleus hint without recomputing star alignment

---

## 5. Solar and Lunar Timelapse Wizards

### 5.1 Solar Disk Normalisation

- HoughCircles disk detection with result caching (alignment.json)
- Affine normalisation: disk centre + radius → fixed target in each frame
- O(1) RAM streaming: normalised frames written to temp JPEGs one at a time; no full-session array
- VFR timelapse via ffconcat

### 5.2 Lunar Wizard

- Same wizard flow; three render modes (Standard / Enhanced / Surface detail)
- Background subtraction for limb contrast; configurable stretch

---

## 6. Supporting Features

### 6.1 Stack Queue and Job History

- Serialised single-worker queue: multiple `_sub` sessions can be queued simultaneously
  without interference; jobs run one at a time
- `/stack/jobs` page: live-updated SSE view of active queue and per-session history
  (frame counts, wall-clock duration, log and image links)
- Queued-but-not-running cards show a pulsing amber indicator in the session browser;
  Cancel only exposed for the running job (cancelling a pending job is a no-op)

### 6.2 Unit Testing

- pytest suite covering pure pipeline functions: quality scoring, quality floor selection,
  background subtraction, stretch, SCNR, crop, frame weighting
- Synthetic numpy array fixtures — no FITS files or external tools required; 41 tests in <0.1 s
- Key regressions caught by tests: divide-by-zero in fwhm clamp, quality floor math,
  mesh_scale=0 skip-subtraction path

### 6.3 Observing Planner

- Rise/set times and altitude curves using astropy for configured observer lat/lon

### 6.4 Live RTSP Capture

- MJPEG proxy (Flask) for in-browser stream view; independent ffmpeg -c copy recording

### 6.5 Catalog Scoreboards and Poster Printing

- Bingo-card grids for Messier/Caldwell; 13×19" print-optimised poster layout

---

## 7. Discussion

- Lessons from WSL2 integration: wslpath conversion, Windows temp dir, GPU access via CUDA
- Siril as a stacking engine: advantages (battle-tested algorithms, GPU-optional) vs. coupling
  (CLI API stability, version-specific sequence naming)
- Future work:
  - Checkpoint/resume for stacking (resume after crash mid-session)
  - Plate-solving integration for precise catalog matching
  - Mobile-friendly UI for session browsing

---

## 8. Conclusion

- Seestar Lab provides a complete local-first processing environment for a class of instrument
  that has no equivalent open-source tooling
- Key technical contributions: Bayer-grid pre-debayer for color registration, IQR border crop,
  diffuseness-based comet nucleus detection, O(1)-RAM solar timelapse
- Available at: [GitHub URL TBD]

---

## Figures (planned)

1. Screenshot: session browser with catalog bingo overlay
2. Stacking pipeline flowchart (Python stages + Siril stages)
3. Bayer-grid artifact example vs. clean pre-debayered stack
4. Row-IQR profile for border detection
5. M 101 final stack (500 frames)
6. Comet wizard: diffuseness score heatmap + nucleus detection overlay
7. Stars-fixed vs. nucleus-fixed animation side-by-side frame comparison
8. Solar timelapse wizard screenshot

---

## References (seed list)

- Bertin & Arnouts 1996 — SExtractor
- Siril documentation (siril.org)
- ZWO Seestar S50 product documentation
- astroalign (Beroiz et al.)
- GraXpert (Graxpert.org)
- astropy Collaboration 2022
