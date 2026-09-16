# OSC Sub-Frame Stacking Recipe (Siril 1.4+)

A validated pipeline for stacking one-shot-color (OSC/Bayer) FITS sub-frames —
e.g. Seestar S50/S30 `_sub` output — into a clean linear color stack and a
publication-quality JPEG.  Developed and A/B-tested in Seestar Lab against
real data (M27, 750 × 10 s subs across three nights); every design decision
below carries the measurement that justified it.

This document is self-contained: hand it to any developer (or AI assistant)
to reimplement the pipeline in another project.

---

## Requirements

- **Siril ≥ 1.4** with CLI (`siril-cli`).  The recipe depends on 1.4 features:
  `-2pass` registration, `seqapplyreg`, `-weight=wfwhm`, `-output_norm`,
  `-rgb_equal`.
- Input: raw **CFA (un-debayered)** FITS subs, uint16, with `BAYERPAT` in the
  header (Seestar writes `GRBG`).  Light-polluted, uncalibrated subs are fine —
  sigma rejection and background extraction handle the rest.
- Optional Python post-processing: `astropy`, `opencv`, `sep`, GraXpert
  (ONNX denoise model).

## Pipeline overview

```
quality filter (Python)  →  Siril: demosaic + register + stack  →  Python post
```

### Stage 1 — Frame quality selection (Python, optional but recommended)

1. **Sharpness scan**: read each sub as raw Bayer uint16; score =
   Laplacian variance of the center quarter.  Reject frames below
   40 % of the median score (kills clouds, trailing, wind shake).
2. **Star metrics** (SEP / Source Extractor): per-frame FWHM, eccentricity,
   star count, SNR.  Combined score = `(stars × SNR) / FWHM`.
3. Keep the top `max_frames` by score (≥ 500 recommended; SNR scales √N).

### Stage 2 — Siril script (the core)

Copy the selected subs **byte-for-byte** (no Python debayer!) into a work dir
as `raw_00000.fit`, `raw_00001.fit`, … then run:

```
requires 1.4.0
cd "<work_dir>"
setext fit
convert light -debayer
register light_ -2pass
seqapplyreg light_
stack r_light_ rej 3 3 -norm=addscale -output_norm -rgb_equal -weight=wfwhm -out=stacked
```

What each line buys you (with the A/B evidence):

| Command | Why |
|---|---|
| `convert light -debayer` | Siril's **RCD demosaic**. vs OpenCV bilinear debayer: star FWHM improved 2.60 → 2.31 px (~12 % sharper) and the correlated chroma speckle that bilinear bakes into every frame is gone. |
| `register light_ -2pass` | Computes transforms + per-frame FWHM stats **without writing frames**, and auto-selects the best-quality frame as reference (single-pass registration uses the first frame). |
| `seqapplyreg light_` | Applies transforms in the **reference frame's footprint** (default `-framing=current`). Do **NOT** use `-framing=max` for alt-az mounts: multi-night sessions carry large field rotation, and the union canvas ends up mostly empty border (measured 31 % empty on a 3-night M27 set), which also poisons downstream stretch statistics. |
| `stack … rej 3 3` | Winsorized sigma clipping (3σ/3σ) — removes hot pixels, cosmic rays, satellite trails. |
| `-norm=addscale` | Additive + scaling input normalisation across frames (required for multi-night sky-level differences). |
| `-weight=wfwhm` | Frames weighted by registration FWHM — poor-seeing subs contribute less instead of equally. |
| `-rgb_equal` | Equalises channel backgrounds (OSC color cast). |
| `-output_norm` | Rescales output to [0, 1] float32. |

Output: `stacked.fit` — 3-channel float32 RGB, linear.

**Considered and rejected — Bayer drizzle** (`seqapplyreg -drizzle -scale=1.0
-pixfrac=1.0 -kernel=square`): identical star sharpness to RCD (FWHM 2.31 px)
but ~2× noisier per-pixel and 3.7× worse post-stretch chroma noise at
N=40 frames, because each color plane receives only ¼ of the samples per
frame.  Only worth revisiting at very high frame counts (N ≳ 2000) or for
super-resolution (`-scale=2`) on heavily dithered data.

### Stage 3 — Python post-processing (on `stacked.fit`)

In order, all on **linear** data before any stretch:

1. **Border crop** — registration leaves partial-coverage edges.  Per-row IQR
   of the green channel: rows with IQR > 1.5× the image-center median IQR are
   border; trim from top/bottom (scan only the outer third).  Columns: keep
   those where > 95 % of pixels are non-zero.
2. **Save the linear FITS** at this point — re-rendering (new stretch/denoise
   parameters) then takes seconds instead of re-stacking for minutes.
3. **Background extraction** — SEP sigma-clipped 2D mesh per channel
   (mesh ≈ image_width / 20).  Removes gradients/vignetting.  Use a coarser
   mesh for large extended objects so the mesh doesn't eat galaxy/nebula
   signal.
4. **AI denoise on linear data** (GraXpert ONNX or similar).  Linear noise is
   Gaussian-ish — denoising before the stretch works far better than after.
   - *WSL2/onnxruntime gotcha*: CUDA libs installed as pip wheels
     (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) are not on the loader path;
     call `onnxruntime.preload_dlls()` (ORT ≥ 1.21) or onnxruntime silently
     falls back to CPU.
5. **Stretch** for the JPEG.  If using Siril: `autostretch -linked -2.80 0.15`.
   - `-linked` = one transfer function for all channels (no color shift after
     `-rgb_equal`).
   - Target background **0.15**, not the 0.25 default — the default lifts the
     sky noise floor well into view and reads as "grainy".  (Reference:
     Seestar's own JPEGs sit at ≈ 0.16 background.)
   - Or an asinh/MTF stretch with tunable black/white points for interactive
     tuning.
6. Optional finish: SCNR green suppression, saturation boost (in HSV on the
   *stretched* image — boosting linear amplifies chroma noise), light chroma
   blur, unsharp mask.

## Measured results (M27, 750 × 10 s subs, identical inputs)

Image-center metrics on the final JPEGs (grain = robust σ of high-frequency
residual in sky pixels; chroma = σ(R−G) + σ(B−G)):

| Pipeline | star FWHM | chroma noise | notes |
|---|---|---|---|
| OpenCV bilinear debayer → register → stack (old) | 2.60 px | 26.9 | smooth but soft; color speckle |
| **This recipe** | **2.31 px** | **17.1** | |
| Vendor internal stacker (Seestar, 360 subs) | — | 21.9 | the quality bar |

## Frame count sweet spot (IC 434 / Horsehead, 3,694 subs available)

A/B tested at 500, 1000, and 2000 frames (best-quality-selected via Stage 1
scoring), same pipeline, same night's data:

| max_frames | Result |
|---|---|
| 500 | Clean, low background noise (σ ≈ 11/channel), sharp horsehead silhouette. |
| **1000** | Visibly sharper than 500 at full resolution — more gas-cloud detail around the horsehead — while staying just as clean (σ ≈ 11/channel, identical to 500). **Sweet spot for this dataset.** |
| 2000 | Regressed badly: background noise more than 3× higher (σ ≈ 35/channel) than 500/1000, washed-out/thin nebulosity, an oversized unnatural star-glow blob, and a visible edge/seam artifact. |

**Root cause (resolved 2026-09-11):** the culprit is the `max_frames` cap being
applied *without* a score-relative quality floor. `max_frames` alone always
fills the requested count from the ranked list, even if frame 1500–2000 are
genuinely poor quality (bad seeing, guiding drift, thin cloud) — there's
nothing stopping a weak tail from being forced in just to hit the number. The
fix, already in this pipeline (`min_quality` param, `stack_processor.py`
score-relative floor): reject any frame scoring below `min_quality × best_score`
*before* applying the `max_frames` cap, so a shallow pool can't be padded with
bad frames. This is why 500/1000 (which the 3,694-frame pool could support
cleanly) looked great, while 2000 pulled in frames past where this particular
night's data stayed good.

Takeaway: **`max_frames` and `min_quality` work together, not `max_frames`
alone.** Setting `max_frames` high is only safe when the source pool actually
has that many frames clearing a real quality floor — a deep pool (e.g. months
of accumulated subs on one target) supports a much higher cap safely than a
single night's shoot does. Don't assume more frames helps; check the actual
score distribution first (see **Frame quality report** below) rather than
guessing a cutoff and hoping.

## Frame quality report (verify before you pick max_frames)

Seestar Lab has a dedicated page for this: `/stack/quality/<session_name>`
(linked from the Stack Wizard). It runs Stage 1 scoring (sharpness + SEP
FWHM/eccentricity/star-count/SNR) read-only over every frame in a session —
no copying, no stacking — and shows:

- A histogram of quality scores with a draggable `max_frames` cutoff line,
  so you can see exactly where a quality cliff is *before* committing to a
  stack run (this would have caught the IC 434 2000-frame cliff above ahead
  of time).
- A sortable per-frame table (score, FWHM, eccentricity, star count, SNR,
  sharpness, Stage A pass/fail).

Use it whenever pushing `max_frames` meaningfully above the ~500-1000 range
that's been validated, especially on a new target/dataset.

## Practical notes

- **Disk**: ≈ 20 bytes/pixel × N frames in the work dir (CFA copy 2 B/px +
  Siril debayered uint16 6 B/px + registered float32 12 B/px).  For 1080×1920
  frames: ~42 MB/frame, 21 GB per 500 frames.  Pre-flight check the free
  space and fail fast.
- **Runtime**: ~18–20 min for 750 frames (2.1 MP) on a laptop-class machine;
  Siril registration+stack dominates.
- Run Siril headless: `siril-cli -s script.ssf`; from WSL2 call the Windows
  binary and convert paths with `wslpath -w`.
- Clean the work dir in a `finally:` — registered float32 frames are the bulk
  of the space.
- Filenames must be a Siril sequence (`name_NNNNN.fit`, zero-padded, no
  spaces).

## Frame-count ceiling: Siril's open-file limit, not a soft cap

Siril's multi-file `convert` opens one handle per source frame and aborts
above a hard OS-dependent ceiling — **~8192 on Windows** — with the error
`Max number of opened files (N) is larger than required number of images`.
`-fitseq` (single-file FITS sequence) is documented to avoid this, but as of
**Siril 1.4.3** it is **broken for `register`**: confirmed via a controlled
A/B test (identical 10 source frames) — `-fitseq` failed registration on
every single frame with `FITS error: Numerical overflow during type
conversion`, while the plain multi-file path (`convert light -debayer`,
`register light_ -2pass`) succeeded 10/10 on the same input. This is a real
Siril bug in this version, not a data or pipeline problem — do not use
`-fitseq` until a newer Siril version is verified to have fixed it (1.4.4's
changelog lists "Fixed seq file corruption", untested as of this writing).

**Practical ceiling for this pipeline: `max_frames ≤ ~8000`.** A code-level
clamp should reject/cap requests above this *before* the copy and scoring
phases run (which can take 1+ hour on a large pool) rather than let Siril
fail after the expensive part is already done.

## Sensor temperature: the dominant real-world noise driver

The Seestar S50 has **no active sensor cooling**. Every `.fit` frame records
`CCD-TEMP` in its header — this single field, sampled across a session,
predicts the stacked result's noise floor better than almost anything else
in the pipeline. Confirmed across four independent targets (same validated
pipeline, same day):

| Session mean CCD-TEMP | Stacked JPEG background σ/channel |
|---|---|
| ~9°C  | ~12-13 (clean) |
| ~11°C | ~20-23 |
| ~20-22°C | ~42-49 (visibly grainy) |

This is a roughly monotonic gradient, not a hard threshold — consistent with
CMOS dark current's known temperature dependence (roughly doubles every
6-8°C). **A multi-night session's temperature should be reported per capture
night, not averaged** — a pool spanning months of accumulated subs (e.g. a
supernova-watch target) can have very different nights mixed together, and
one blended average hides exactly the variance that predicts result quality.
Group frames by the date embedded in the filename timestamp and sample
`CCD-TEMP` from a handful of frames per night (header-only read — no need to
load pixel data — keeps this fast even on large pools; ~90s for one session
of 27,850 frames across 88 nights, sampling 8 frames/night).

There is evidence the vendor's own onboard/live stacking software handles
warm-sensor conditions meaningfully better than a from-scratch pipeline
without deliberate work: comparing this pipeline's output against the
Seestar app's own onboard-stacked JPEG for the same target under similarly
warm conditions showed the vendor output at roughly 1/3 the noise (σ≈14-17
vs σ≈44-49). No dark/bias calibration frames were found saved anywhere
accessible in the raw session data, so the vendor's technique (if it is
darks/bias calibration, which is the most likely explanation) is not
directly reproducible from available data — worth investigating if pursuing
this further, but not yet solved.

## Chroma denoising on the Siril autostretch path

Siril's own `autostretch` path has no color-noise reduction — on a
warm-sensor session, residual per-channel noise reads visually as colorful
speckle even though the underlying luminance noise (the larger, genuinely
capture-limited component) is separate. The fix: Gaussian blur on the Cr/Cb
channels in 8-bit YCrCb space, applied to the final stretched JPEG,
measurably reduces chroma noise (roughly halved to more than halved
depending on how noisy the source was) without materially affecting
luminance — confirmed on two independent targets.

An apparent "color-channel shift" was suspected during initial testing (a
corner patch that should have stayed neutral appeared to gain a green tint
after the blur) but this was a **test-methodology mistake, not a real
effect**: the comparison had accidentally been run against the pipeline's
*pre-calibration* intermediate FITS (`<name>_linear.fits`, written before
`_subtract_background`/`_color_calibrate` run — see "Pipeline overview"
above) instead of the actual, fully-calibrated output FITS the real
pipeline uses. Once corrected to compare against the right file, the
chroma-blur output matched the no-blur baseline's color balance almost
exactly (same channel ordering, sub-1-unit differences) while still
delivering the real chroma-noise reduction. Confirmed via both a synthetic
neutral-image test (blur alone introduces no measurable tint) and the
corrected real-data comparison. **When testing any postprocess-stage
change against saved intermediate files, always verify which file was
actually loaded (log the path, don't assume) — this exact mistake recurred
twice in one investigation before being caught for good.**

## Known issue: green tint on bright stars, separate from the chroma-blur bug

Independently of the chroma-blur dark-background tint above, bright star
pixels can carry a visible green cast even on output generated *before* the
chroma-blur code existed — confirmed on a Horsehead/IC 434 result via direct
measurement: bright (near-saturated) pixels averaged B=229/G=233/R=224 in
the final JPEG, G clearly highest. Checked the linear, already-calibrated
FITS this JPEG was generated from: G is still highest among bright pixels
there too (though by a smaller relative margin), suggesting `_color_calibrate`
(SEP-based star white-balance, intended to correct exactly this — see its
own code comment about a genuine Bayer-response imbalance in the source
frames observed as a green/teal tint on bright stars) is only partially
correcting it, and/or the nonlinear `autostretch` widens whatever residual
imbalance remains, the same way it amplifies noise. Not yet root-caused or
fixed — worth checking whether `_color_calibrate`'s star-detection threshold
or reference-star selection needs tuning, independent of the separate
dark-background chroma-blur issue above. Two distinct known color-accuracy
issues exist in this pipeline as of this writing; do not conflate them when
debugging either one.
