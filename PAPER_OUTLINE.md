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
- Reject below 40 % of median sharpness; rank survivors by combined score; cap at max_frames

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

### 3.6 Results

- M 101 (Pinwheel Galaxy): 500 sub-frames, 10 s each = 83 min total integration
- Pipeline runtime: ~31 min end-to-end on WSL2 with CUDA-accelerated GraXpert denoising
- Output: spiral arms clearly resolved, star colours preserved, clean background
- SNR discussion: 500 vs. 1000 vs. 1500 frames; diminishing returns curve; quality-filter
  effect (adding frames includes lower-quality subs)

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

### 6.1 Observing Planner

- Rise/set times and altitude curves using astropy for configured observer lat/lon

### 6.2 Live RTSP Capture

- MJPEG proxy (Flask) for in-browser stream view; independent ffmpeg -c copy recording

### 6.3 Catalog Scoreboards and Poster Printing

- Bingo-card grids for Messier/Caldwell; 13×19" print-optimised poster layout

---

## 7. Discussion

- Lessons from WSL2 integration: wslpath conversion, Windows temp dir, GPU access via CUDA
- Siril as a stacking engine: advantages (battle-tested algorithms, GPU-optional) vs. coupling
  (CLI API stability, version-specific sequence naming)
- Future work:
  - Checkpoint/resume for stacking (resume after crash mid-session)
  - GraXpert AI denoising as the default JPEG step (already implemented as fallback)
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
