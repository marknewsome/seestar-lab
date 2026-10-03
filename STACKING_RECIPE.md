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

**"Just use the coldest night" does not automatically help — tested and
measured, not just assumed (M81, 2026-09-30).** M81's pool spans 6 nights;
the coldest (Feb 4, 10.9°C) had only 20 frames, far too few for a deep
stack on its own. The two nights with real depth were both warm (May 8:
583 frames/20.4°C, May 9: 541 frames/23.6°C). Tried restricting to just
the cooler of the two (May 8, 500 frames after the usual cap) versus the
full 1000-frame mixed-night pool the normal pipeline selected (which
necessarily pulls in some May 9 frames too). Measured corner (empty-sky)
noise directly rather than eyeballing it:

| Stack | Frames | chroma(r−g) | chroma(b−g) | luma σ |
|---|---|---|---|---|
| Full pool (mixed nights, normal `max_frames=1000`) | 1000 | 5.78 | 5.65 | 43.56 |
| May 8 only (cooler night, capped) | 500 | 6.32 | 6.38 | 48.90 |

**The restricted-to-cooler-night stack was measurably WORSE, not better.**
Halving the frame count (SNR ∝ √N) cost more than the ~3°C average
temperature improvement gained — the noise floor is dominated by both
factors, not sensor temperature alone, and on this particular pool the
frame-count penalty outweighed the temperature benefit. Lesson: don't
assume restricting to a cooler subset helps without checking whether that
subset actually has comparable depth to the full pool — if the coldest
night(s) can't support a similarly-sized stack, the full mixed-night pool
may well be the better choice despite its higher average temperature.

**Counter-example where the cooler subset DID win (IC 5146, 2026-10-01).**
Same experiment, different outcome, because the depth trade-off was much
less severe here. IC 5146's pool spans 9 nights; the normal pipeline's
quality-ranked 1000-frame selection pulled heavily from several warm nights
(mean sampled CCD-TEMP ≈ 20.1°C, range 14.4–25.0°C across the pool) and
produced a result where the Cocoon Nebula's own structure barely resolved
at all — just a faint reddish blob, no internal detail, plus a dark
wedge-shaped crop artifact in one corner. But unlike M81, this pool has a
genuinely substantial cooler pair of nights: Oct 14 (169 frames, 16.6°C) +
Oct 15 (131 frames, 15.8°C) = 300 frames at ~16.2°C average — a much
smaller frame-count cut (1000→295 accepted) than M81's 1000→500 halving,
because the baseline here didn't need anywhere near 1000 frames of mixed-
quality data to begin with.

| Stack | Frames | chroma(r−g) | chroma(b−g) | luma σ |
|---|---|---|---|---|
| Full pool (mixed nights, `max_frames=1000`) | 1000 | 7.34 | 5.58 | 47.73 |
| Oct 14+15 only (cooler nights) | 295 | 6.44 | 5.23 | 51.31 |

Luma noise alone is a mixed signal (slightly higher on the restricted
stack), but chroma noise improved and — more importantly — the Cocoon's
actual structure (dark absorption lane, embedded star cluster, real color
gradation) only became visible in the restricted stack; the corner crop
artifact also disappeared. Kept the Oct 14+15-only result as the
reference. **Combined lesson from both experiments: whether restricting to
a cooler subset helps depends on how much depth that subset actually has
relative to what the full pool would otherwise use — there's no universal
answer, measure (and look at) both before deciding.**

## Chroma denoising on the Siril autostretch path

Siril's own `autostretch` path has no color-noise reduction — on a
warm-sensor session, residual per-channel noise reads visually as colorful
speckle even though the underlying luminance noise (the larger, genuinely
capture-limited component) is separate. The fix: Gaussian blur on the Cr/Cb
channels in 8-bit YCrCb space, applied to the final stretched JPEG,
measurably reduces chroma noise (roughly halved to more than halved
depending on how noisy the source was) without materially affecting
luminance — confirmed on two independent targets.

**Resolved (2026-09-16): re-validated on both targets, calibration stage kept
on.** The regression reported above (background noise doubling near the M101
galaxy with the full background-subtract/color-calibrate/GraXpert stage vs.
a bypass) was traced to an accidental comparison methodology — a one-off
`_siril_postprocess()` call directly against `_linear.fits`, outside the
normal job flow — not a real defect in the calibration stage itself.
Re-running both targets through the normal, unconditional full pipeline
(`rerender_preview`'s `has_linear` path: `_subtract_background` →
`_color_calibrate` → `_graxpert_denoise` → `_scnr_green` → stretch →
chroma-blur denoise/sharpen — see `stack_processor.py`) produced good,
approved results on both a warm-sensor target (M101, `save/
M101_1000frames_2026-09-15_chromafix.jpg`) and a cold-sensor target (NGC 7000,
`save/NGC7000_700frames_2026-09-16_chromafix.jpg`). The pipeline does not need
per-target branching around this stage; keep calibration unconditional.

A follow-up attempt the same day to fix the separate green-tint issue (below)
by changing `_color_calibrate`'s star white-balance made M101 look as bad as
the no-chroma-blur version, and was reverted rather than merged — the
`_fixedpipeline_test.jpg` files left in each target's output folder are that
rejected experiment, not an improvement; disregard them.

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

### Investigated 2026-10-03: the suggested fix above is a dead end — ruled out

Followed the "tune `_color_calibrate`'s reference-star selection" suggestion
on a fresh IC 434 stack (500 frames) plus 10 other targets' linear FITS.
**It does not fix the tint. Do not retry this approach.**

What was tested: the `a < 5.0` size filter was suspected of excluding bright
bloomed stars from the white-balance sample. It is not the problem — it
excludes only 3 of 268 detections on IC 434. The *real* defect in the
selection is different: the sample is dominated by near-noise detections
(median flux 0.011; only 8 of 246 above flux 1.0), and since the top-200 cut
(246 -> 200) culls almost nothing, the **median** that sets the correction is
decided by the faint tail rather than by real stars. Adding a flux floor
genuinely fixes *that* — measured on the linear intermediate it halved mean
deviation from neutral across 11 targets (0.096 -> 0.048), and on the two
worst targets (IC 434, M 43) bright-core G/R went 1.45 -> ~1.05.

**But that improvement does not reach the output, and the change made the
visible result worse.** Re-rendering end-to-end through `rerender_preview`
and measuring the star *halos* (3-7px annulus, where the tint actually lives
— the very cores clip to white and measure neutral, which is why the original
whole-pixel measurement understated it) showed median G-R moving from -5.11
to **+1.00**, i.e. halos became *more* green-leaning. Confirmed visually, not
just numerically. The change was reverted and not committed.

Why the intermediate improves but the output doesn't: after
`_color_calibrate` the bright pixels are already G/R = 0.77 (green well
*below* red) — there is no green excess left in the linear data to correct.
`_scnr_green` then clips G to max(R,B), so G provably cannot exceed both
other channels at that point either. The green therefore originates
**downstream of calibration**, in the stretch stage. Note the default path
is Siril `autostretch -linked` (see `_siril_postprocess`); a linked stretch
preserves channel ratios and so cannot flip G from below-R to above-R, but it
does expand small absolute channel gaps by orders of magnitude in the
mid-tones — a 0.0003 linear gap becomes many 8-bit levels. Via the Python
`_auto_stretch` fallback path G stays below R (G-R = -0.029) all the way to
the final image, which further localises the problem to the Siril stretch
path rather than to colour calibration.

**Next investigator: start at the stretch stage, not `_color_calibrate`.**
The open question is why the stretched output shows green halos when its input
has none.

**Correction (same day, after finding the `rerender_preview` `_linear.fits`
bug — see the bright-core section below):** the claim above that this was
"localised to the Siril stretch path" is NOT established. That conclusion came
from comparing renders where the Siril path had silently fallen back to Python
(Siril cannot write to a Linux-only scratch dir) and where calibration had been
silently skipped. Re-measured correctly, both stretch paths behave sanely and
IC 434's star-halo mean G-R is *negative* in both the shipped render (-5.11)
and a fresh correct one (-7.34) — i.e. green sits below red on that metric.

**But the teal halos are still plainly visible in the image**, so the user's
original observation stands. The effect is a hue/saturation phenomenon, not a
mean-channel-difference one — which is why both the halo-annulus G-R metric and
a median-hue metric (median sat only 14/255, hues scattered 16°-333°) fail to
capture it.

### There is now a metric: `teal_halo_metric.py` (2026-10-03)

Counting beats averaging here. A pixel counts as teal if its hue is in the
green-cyan band (140-200°), it is meaningfully saturated (S >= 30) and bright
enough to see (V >= 100). The tool reports two numbers and **both matter**:
`halo%` (teal in a 25px zone around the 60 brightest stars) and `frame%` (teal
over the whole image).

Measuring only the halo zone gives false "clean" verdicts: on a re-rendered
M 43 the contamination sits in the nebulosity rather than ringing stars, and
the star finder found only 7 stars, so halo% read 0.00% on a render whose teal
is obvious. frame% caught it at 2.57%. Distrust halo% whenever stars < ~20.
(The star finder uses an absolute luminance percentile, not SEP's sigma-relative
threshold — renders differ enough in noise that an 8-sigma cut finds zero stars
on M 13 and SH2-158, whose globalrms is ~31.)

**The split is bimodal, not gradual**, so a threshold separates cleanly:

| affected (frame% / halo%) | | clean (frame%) | |
|---|---|---|---|
| M 36 | 4.77 / 6.01 | C 34 - West Veil | 0.267 |
| M 31_mosaic | 3.68 / 3.94 | M 33 | 0.201 |
| M 108 | 3.44 / 8.51 | C 34 | 0.154 |
| M 16_mosaic | 2.76 / 7.46 | M 27 | 0.075 |
| IC 434 | 2.32 / 5.67 | SH2-158 | 0.042 |
| M 20 | 2.23 / 4.45 | M 13 | 0.041 |
| M 1 | 2.17 / 3.53 | M 81 | 0.014 |
| M 43 | 1.91 / 16.18 | IC 5146 | 0.003 |
| NGC 5907 | 1.54 / 2.62 | SH2-142 | 0.000 |
| M 97 | 1.21 / 2.59 | | |
| M 51 | 0.35 / 14.91 | | |

**11 of 20 archived renders are affected** — this is a widespread defect, not an
IC 434 quirk.

**Two hypotheses already ruled out with this tool:**
1. *Stretch path.* Python vs Siril does not correlate — both affected (M 43,
   IC 434) and clean (M 27, M 81, M 13) targets used the Python path.
2. *Stale renders / already fixed.* Re-rendering M 43 (worst affected) with
   current code does **not** fix it: frame% goes 1.91 -> 2.57, slightly worse.

**Bisect results (IC 434, the one modern affected render where every stage ran
correctly — GraXpert OK, stacked 2026-10-03):**

Measuring teal after applying the *same* stretch to each linear stage's output:

| stage | teal% | bright G/R |
|---|---|---|
| raw linear | 0.103 | 1.237 |
| + `_subtract_background` | 0.098 | 1.237 |
| + `_color_calibrate` | 0.098 | 1.063 |
| + `_graxpert_denoise` | 0.099 | 1.128 |
| + `_scnr_green` | 0.099 | 1.128 |

**No linear stage introduces teal** — all sit at ~0.1%, and the complete Python
render (stretch + saturation + denoise/sharpen) finishes at **0.074%, clean**.
The shipped render is 2.321%. So the defect is not in the linear pipeline and
not in the Python render path; it is specific to how the *stacking job* builds
its preview.

**Partial cause found: `_scnr_green` never runs on the Siril preview path.**
`_siril_full_stack` applies `_color_calibrate` and `_graxpert_denoise` before
writing the linear FITS, but **not** `_scnr_green` — even though its own comment
cites the green/teal tint as the reason colour calibration is there. In the
stack job (`STEP 10`), the primary preview is `_siril_postprocess(fits_path,…)`
reading that SCNR-less FITS; `_scnr_green` appears only in the `if not
siril_ok:` fallback. So whenever Siril succeeds — the normal case — the
delivered preview never gets green suppression.

Confirmed by experiment on IC 434's linear FITS:

| input to `_siril_postprocess` | teal% |
|---|---|
| without `_scnr_green` (what the job writes) | 1.482 |
| with `_scnr_green` applied first | 1.023 |

**But this is a contributing factor, not the whole cause.** Adding SCNR helps
(1.48 -> 1.02) yet both stay above the 0.5% clean threshold, and neither
reproduces the shipped 2.321% — so at least one more difference between this
reproduction and the real job remains unaccounted for. **Do not "fix" this by
just inserting `_scnr_green` and declaring victory**; verify with the metric
that the result actually lands in the clean band (<=0.3%, where every known-good
target sits), and account for the gap to 2.321% first.

Ruled out along the way: GraXpert is not the cause (IC 434 had
`GraXpert denoise : OK` and is still affected). The age correlation is also a
red herring — 8 of the 11 affected renders are from May 2026, predating
GraXpert entirely, so "no GraXpert line" there just means "old render".

## Known limitation: bright-core targets (M42-class) — core still clips to flat white

**Also confirmed on M13 (Hercules Cluster, globular, 500×10s subs, 2026-09-17):**
same failure mode, different target class — a globular cluster's densely
packed core is extremely bright relative to the surrounding field for the
same reason M42's Trapezium is (a small, very bright region inside a
normally-exposed frame), and it clips to a flat white blob with no resolved
stars. A gentler stretch (targetbg/shadowsclip re-tuned toward less
aggressive) was tried via re-render and made the result *worse* — materially
more background grain/noise, purple-tinted sky, and no core recovery — so
the original default stretch is the better result to keep. This is now two
independently confirmed cases (an emission nebula core and a globular
cluster core) of the same structural limitation described below, not an
M42-specific quirk.

**Also confirmed on M27 (Dumbbell Nebula, planetary nebula, 1000×10s subs,
2026-09-29):** the bright central lobes clip to a flat white blob with the
finer bipolar/mottled texture washed out, even though the surrounding
fainter halo and star field render well. Tried `white_pct=97.0` (down from
default 99.9) via re-render — this made it slightly *worse*, not better:
`white_pct` lower means the top-of-range clip point sits at a LOWER raw
value, so more of the frame (not less) gets pushed into full clip. The
default settings' result remains the better one to keep. Third
independently confirmed target class (planetary nebula, alongside emission
nebula and globular cluster) hitting the same structural limitation —
reinforces this is general to any target with a compact bright feature
inside an otherwise normally-exposed frame, not specific to one object
type.

**Counter-example, refining the theory (M81/Bode's Galaxy, 1000×10s subs,
2026-09-30): a bright galaxy bulge does NOT trigger this limitation.**
M81's core is genuinely bright (obviously so even in a single raw sub) but
rendered with real graduated brightness and visible texture at full zoom —
no flat-white clipping. This suggests the limitation is specifically about
*spatial concentration* of brightness (a small, tightly-packed or
point-like bright feature — a planetary nebula's central star/lobes, a
globular cluster's packed core, an emission nebula's brightest knot), not
brightness alone. A galaxy bulge is bright but its brightness falls off
gradually over a much larger area, which is apparently enough for the
single global stretch curve to render cleanly. Worth keeping in mind when
predicting which future targets will hit this limitation: look for a
small, sharply-bounded bright region, not just "this target has a bright
part."

M42 (Orion Nebula, 500×10s subs) initially stacked very poorly with the
frame-wide default parameters (`bg_mesh_scale=20`, `white_pct=99.9`): the
whole frame read as washed-out with heavy color speckle in the nebulosity,
because those defaults were tuned on fainter, more diffuse targets and are
wrong for a target this bright and compact.

**Parameter re-tuning fixed the nebulosity/noise problem completely:**
`bg_mesh_scale=8` (coarse mesh — a bright extended nebula shouldn't have its
own glow subtracted as sky, same reasoning as M101) + `white_pct=98.0`
(down from 99.9) via `rerender_preview` recovered clean wispy structure,
accurate color, and no speckle. Re-render only — no re-stack needed, seconds
not minutes, since `_linear.fits` already existed.

**The Trapezium core itself is still flat white with no structure**, and
this is a *different* problem from the above — not a parameter-tuning
question. M42's core is bright enough, over a small enough area, that no
single global (or even region-local) percentile-based stretch curve can
preserve both it and the faint outer nebulosity simultaneously: whatever
`white_pct` reveals the outer wisps will always clip the much-brighter core
to 1.0 on all channels.

**Three attempts at a masked "highlight recovery" curve (2026-09-16) all
failed, for three different, instructive reasons** — do not re-attempt this
exact approach without addressing all three:

1. *v1 — global luminance-percentile mask, region-local white = absolute max.*
   Produced star-shaped ring halos on ordinary bright stars all over the
   frame (a plain percentile threshold on luminance selects every bright
   star, not just the nebula core — there is no size/shape information in a
   percentile), AND a black hole in the core itself (using the raw pixel
   *maximum* as the local white reference is dominated by single hot-pixel
   outliers, crushing the rest of the masked region toward black by
   comparison).
2. *v2 — masked on stretched (post-arcsinh) luminance instead of raw.*
   Fixed the star-halo problem (morphological opening now correctly
   distinguishes small star PSFs from the one large contiguous core region)
   but the black hole persisted **and got slightly worse**. Root cause:
   `arcsinh` saturates hard near 1.0, so "quite bright" and "genuinely
   blown out" pixels are indistinguishable once you've already stretched —
   masking on the stretched result cannot recover information the stretch
   already destroyed.
3. *v3 — masked on raw linear luminance (correct), but with too strict a
   percentile threshold (99.95) so the mask covered a tiny fraction (~1000
   px) of the actual ~40,000-px blown region.* Widening the mask threshold
   to match `white_pct` (98.0, so the mask actually covers the whole
   visually-clipped area) **brought the black hole straight back** — because
   a mask that's finally the right *size* still spans a huge internal
   brightness range (from "just barely clips the main curve" to "the actual
   Trapezium saturation core"), and percentile-normalizing *that whole
   region* to its own black/white points repeats the exact same failure
   mode as the original frame-wide stretch, just at smaller scale. The
   region's own 99.9th-percentile white reference sits almost at 1.0 (nearly
   the full dynamic range), so most of the region's pixels — which are only
   moderately above the outer threshold, not truly saturated — compute to
   near-zero after the second arcsinh curve.

**Conclusion: any single-pair-of-percentiles stretch (global or region-
local) structurally cannot solve this** — the core spans too many orders of
magnitude internally for one black/white point pair to serve well. A real
fix needs either genuine local/spatially-adaptive tone mapping (e.g. CLAHE
on luminance, applied to the already-stretched result so it locally
re-expands contrast using neighborhood statistics rather than one global
pair of percentiles) or literal star/nebula layer separation (SEP-detect and
mask out stars, stretch the starless nebula and a separate star layer with
independently-tuned curves, recombine) — not attempted yet. The scaffolding
for a masked approach (`_auto_stretch`'s `core_protect`/`core_pct`/`core_Q`
params, `stack_processor.py`) is left in place but **defaults to off**
(`_CORE_PROTECT = False`) since it does not currently produce a usable
result; do not enable it without addressing the above.

### CLAHE attempt (2026-10-03): tried, does not work — four variants all failed

The CLAHE suggestion above was taken up and tested on M27 (1000x10s, confirmed
case of this limitation). **It does not fix the problem. All four variants
failed; no code was changed.**

Useful measurement first, to frame the problem correctly: M27's nebula body
holds **12.1x internal contrast in the linear data but only 1.75x in the
delivered JPEG**. Only 2.3% of body pixels exceed 250 and just 121 pixels
frame-wide are 255-clipped in all channels, so **this is not hard clipping —
it is contrast compression**. The body sits at 0.24-0.97 of the white
reference (i.e. *below* it, not above), and arcsinh at Q=8 maps that 2.4x
input range onto 1.6x output because it is already in the curve's flattening
zone. Q controls faint-lift and bright-contrast with one knob, which is the
structural reason no global Q retune can fix both (consistent with the earlier
gentler-stretch failures).

What was tried:
1. *CLAHE on the final JPEG* (what the note above literally suggested).
   Failed: across clip 1.0-4.0 x grid 4-16, every setting either reduced body
   contrast or barely matched it (1.75x -> 1.24-1.82x) while inflating
   background sigma 1.3-2.9x. By JPEG stage the body is already compressed
   into a narrow bright band, so local equalisation has nothing to work with.
2. *Local-mean normalisation on the linear data* (divide luminance by a
   blurred copy, blend by amount). Failed worse: at amount>=0.5 it erased the
   nebula entirely (no body blob detectable). The object is large relative to
   any sane blur kernel, so the "local mean" is the nebula itself.
3. *Masked CLAHE on stretched luminance*, mask built on linear luminance with
   morphological opening to exclude stars and a feathered blend — explicitly
   designed to avoid all three documented core_protect failure modes. Gave
   only a marginal gain (body contrast 1.63x -> 1.78x) while still raising
   background sigma 1.65x despite the mask covering only ~8.5k px.
4. *Plain gamma on the shipped JPEG*, as a sanity check. Failed; the body
   stays a pale blob.

**Two measurement traps to avoid when working on this** (both cost time here):
- A brightness-thresholded body mask (`lum > 120`) is not comparable across
  renders — a brighter render yields a 4x larger "body" and flatters its own
  contrast ratio. Use a fixed spatial region, or compare renders only at
  matched mean brightness.
- Local-detail rms rises under gamma because gamma amplifies noise too. It
  looked like recovered structure and was not. **Check crops visually; the
  scalar metrics here are all misleading on their own.**

Also worth knowing: a hand-assembled `_subtract_background` ->
`_color_calibrate` -> `_scnr_green` -> `_auto_stretch` sequence produced a
visibly *better* M27 core than the shipped Siril render, which looked like a
promising lead — but re-running the **real** `rerender_preview` with
`core_protect=True` (which forces the Python stretch path) did not reproduce
it: the actual Python-path output is washed out, noisier, and carries a strong
green cast. Do not trust hand-assembled stage sequences as a proxy for the
pipeline; always confirm through `rerender_preview`.

### Investigated 2026-10-03: core-layer premise is false; nothing is clipped

Before building the single-sub core layer described below, its **premise was
tested and found to be false**, which then led to the actual root cause.

**1. Nothing is saturated in the linear data.** The planned fix assumed "500
frames of accumulated signal pushed the core into saturation, so pull the core
from a single, less-saturated sub." Measured on the deep linear stacks:

| target | linear luminance max | px > 0.99 | px > 0.90 |
|---|---|---|---|
| M 27 (nebula body) | **0.0128** (1.3% of full scale, 78x headroom) | 0 | 0 |
| M 13 | 0.9909 | 1 | 19 |
| M 43 | 0.9518 | 0 | 60 |
| IC 434 | 0.9255 | 0 | 51 |

The handful of high pixels are *stars*, not nebula cores. M27's nebula body
peaks at 1.3% of full scale. **There is no clipped information for a
less-saturated core layer to recover, so that approach cannot work** — do not
build it. (The star-layer half of `_starless_blend` remains valid; it solves a
different problem.)

**2. The core blowout is NOT a colour or calibration problem.** A long detour
this session appeared to show a severe green cast (G-R +20.6) on re-rendered
M27 and concluded the bright-core and green-tint issues were the same bug.
**That was wrong, and was entirely an artifact of how `rerender_preview` was
being called** — see the `_linear.fits` bug note below. Re-measured correctly,
a fresh render reproduces the shipped JPEG exactly: **G-R = -1.75 (neutral),
IQR 21.1, median 240.4, bg sigma 49.51.** There is no green cast on this target
and the two issues are unrelated. Disregard any note claiming otherwise.

The core blowout is therefore still exactly what the sections above describe:
a tone/stretch problem on data that has plenty of headroom, with no colour
component and nothing clipped in the linear FITS.

**Bug found and fixed along the way (`rerender_preview` silently skipping all
calibration):** the function derived its linear sidecar as
`<stem>_linear.fits`, so passing `seestar_stacked_linear.fits` directly made it
look for `seestar_stacked_linear_linear.fits`, find nothing, set
`has_linear = False`, and **skip the entire background-subtract / colour-
calibrate / GraXpert / SCNR block** — sending raw data straight to the stretch.
That produces a dramatic green cast (G-R +20.6 vs -1.75) and completely
different tone numbers. The live app was never affected: `app.py` derives
`fits_path` from `output_path` as `seestar_stacked.fits`, the non-linear form.
Only direct/scripted calls passing the sidecar hit it. Now fixed — both forms
are accepted and produce byte-identical output.

**Methodology warning for anyone measuring this pipeline:** always call
`rerender_preview` with the plain `seestar_stacked.fits` path, and write the
output **into the target's own directory**. Siril is a Windows binary; when
`jpeg_path` is somewhere Windows cannot reach (e.g. a Linux-only scratch dir),
`_siril_postprocess` writes its temp FITS next to `jpeg_path`, Siril fails, and
the code silently falls back to the Python stretch — giving numbers that look
like a real difference between stretch paths but are an environment artifact.
Two separate false conclusions this session traced back to exactly that.

## Next attempt at core-blowout: single-sub star layer, not a masked curve

Insight (2026-09-17): comparing our stack against the Seestar app's own
onboard live-stacked result on the same target showed the vendor stacker
does NOT blow out bright stars the way our deep stack does — this is the
standard reason astrophotographers run star removal (StarNet/
StarXTerminator-style tools) before stretching: it sidesteps the dynamic-
range conflict entirely rather than trying to solve it within one curve.

**Why this is a different (and more promising) approach than the abandoned
masked-curve attempts above:** those all tried to solve "one stretch curve,
two dynamic ranges" *within the deep stack itself*. But the deep stack's
stars are the actual problem — 500 frames of accumulated signal makes a
star's peak proportionally far more saturated than in any single sub, while
the faint nebula needs exactly that accumulated depth to be visible at all.
Pulling the star layer from a SINGLE sub (or a very light stack) instead of
the full deep stack sidesteps the conflict rather than fighting it: the
nebula layer gets the full aggressive stretch it needs with no stars in the
way to blow out, and the star layer comes from data that was never so
deeply saturated in the first place.

**Planned pipeline (prototype target: M42, existing data/baseline in
astro/stacks/M42/):**
1. SEP-detect stars on the deep stack's linear data (reuse existing
   detection code — `_reduce_stars`, `_color_calibrate` already do this)
2. Build a starless layer: mask out detected stars from the deep stack,
   inpaint or fill from local background
3. Stretch the starless layer aggressively (current pipeline's approach,
   tuned for faint structure — no core-blowout risk with stars removed)
4. Pick one well-exposed single sub (or a very small/light stack — few
   frames, not the full pool) as the star source; register it to the deep
   stack's frame if needed
5. Detect + isolate just the star layer from that single-sub source,
   stretch it separately with its own (gentler, since less accumulated
   signal) curve
6. Screen/lighten-blend the two layers back together

Once working, expose as a user-selectable Stack Wizard option (a checkbox/
toggle alongside the existing tunable params) rather than an always-on
behavior — this is a bigger structural change than the other tunables, and
different targets may still do better with the existing simple stretch.

### Prototype results (2026-09-17): mechanism validated, core-blowout not yet solved

Built and tested the full pipeline above end-to-end on M42, in stages, each
one exposing a real bug fixed before moving on:

1. **Naive star detection caught the Trapezium itself** as one giant "star"
   (measured `a≈210px` semi-major axis vs ~5-10px for real stars) —
   inpainting it away wiped out the entire nebula core, leaving a flat grey
   blob with visible inpaint sunburst artifacts. Fix: size cap (~10-12px)
   on detected objects before building the star mask.
2. **Size cap alone wasn't enough** — small bright knots *within* the
   crowded Trapezium region still individually passed the size filter.
   Fix: added SEP's `flag == 0` check (rejects blended/contaminated
   detections) plus a roundness check (`a/b < 2.0`, real stars are round,
   nebula-wisp fragments tend to be elongated) plus an explicit spatial
   exclusion circle around the known core coordinates as a belt-and-braces
   backstop.
3. **Assumed Siril's own registration-reference sub was already pixel-
   aligned to the deep stack's frame** (same filename Siril itself picked
   as reference) — wrong. Measured directly: 24×196px offset between the
   deep stack's brightest pixel and the same target's location in the
   "reference" sub. The deep stack's border-crop + background-mesh
   subtraction shift the effective coordinate origin relative to the raw,
   uncropped sub — being *Siril's* registration reference doesn't mean
   pre-aligned to *our* post-processed frame. Fix: real `astroalign`
   registration (`aa.find_transform` + `aa.apply_transform`, same library
   already used in `comet_processor.py`) — found a genuine 13.1° rotation
   + large translation between the two frames; after applying it, the
   brightest-pixel offset dropped to 1×0px.

**With all three fixed, the mechanism works cleanly**: starless deep-stack
layer (full nebula structure, no inpaint artifacts, no core damage) +
properly-registered single-sub star layer (233 clean, correctly-sized,
correctly-positioned stars) blend via `np.maximum` per channel into a
result with rich wispy nebula detail and clean stars, matching the quality
of the best M42 render achieved so far (`_retest_wp98.jpg`).

**Still open: the actual core-blowout goal is NOT yet solved by this.** The
Trapezium was deliberately *excluded* from star detection (correctly — it
isn't a star), so it stays part of the starless nebula layer and still gets
crushed to flat white by the same single global stretch curve problem
diagnosed earlier in this document. This prototype validates the star/
nebula separation *mechanism*, not a fix for the nebula-core brightness
itself.

**Natural next step, using the same machinery just built:** apply the same
single-sub-source trick to the CORE, not just stars — the 500-frame deep
stack's Trapezium is saturated because 500 frames of accumulated signal
pushed it there; a single 10s sub's own core brightness is far less
saturated (same reasoning that motivated pulling stars from one sub in the
first place). Extract just the core region from the same registered single
sub, stretch it with its own gentle curve, and blend it in the same way as
the star layer — rather than trying to solve "one curve, whole dynamic
range" within the deep stack's core pixels the way the three earlier
core_protect attempts did (and failed).

Not yet implemented — next-session work. Prototype scripts are ad-hoc
(scratchpad, not committed) — the working version of this logic still
needs to be written into `stack_processor.py` as reusable functions
(`_extract_stars_from_sub`, `_build_starless_layer`, register + blend) and
wired through `rerender_preview`/the Stack Wizard as a user-selectable
option once the core-brightness piece is also solved, per the user's
request.

## Star/nebula separation ported to stack_processor.py, tested on a dense field (2026-09-19)

Ported the validated M42 prototype into real functions: `_detect_point_stars`,
`_build_starless_layer`, `_register_to`, `_extract_star_layer`,
`_starless_blend`, wired through `rerender_preview`
(`starless_blend`/`starless_star_sub` params) and `/api/stack/rerender`.
`starless_star_sub` accepts either a single raw sub path (cheap, low star
count) or a list of raw sub paths (lightly Siril-stacked first via
`_siril_full_stack` for much better star-detection SNR).

**Two real bugs found and fixed while testing on SH2-142** (a Milky Way
star-cloud field, ~9,624 detected stars — far denser than M42's ~450):

1. *Stale server process.* The running Flask dev server (`debug=False`, no
   reloader) had `stack_processor` imported in memory from BEFORE tonight's
   edits — every rerender call silently ran old code regardless of what was
   on disk. Symptom: results identical to a known-broken earlier attempt no
   matter what was changed. No amount of code fixing helps until the server
   process is actually restarted — remember this for any future live-testing
   session against the running app, not just this feature.
2. *Shape mismatch in `_register_to`.* `aa.apply_transform`'s output takes
   the TARGET's shape, not the source's, but the code allocated `aligned =
   np.zeros_like(source)` — when a light sub-stack's own border-crop trimmed
   a different row count (1919 vs the deep stack's 1920) than the deep
   stack's own crop did, writing per-channel results threw a broadcast
   `ValueError`. Fixed: allocate against `target_luma.shape` instead.

**With both fixed, the pipeline runs to completion without falling back —
but the visual result on this dense field is not clean.** Visible soft
halo/blur artifacts around many stars, and slight overall softness vs the
plain stack. Suspected cause, not yet confirmed: at ~9,624 detected stars
the per-star inpaint-mask circles (radius = 2.5× each star's own SEP a/b)
overlap into much larger contiguous "holes" than on M42's sparser field,
and `cv2.INPAINT_TELEA` filling a large contiguous region convincingly is a
harder problem than filling isolated small circles — the artifact reads
like inpaint-boundary softness bleeding into the surrounding real pixels
where the mask covers a large fraction of a local neighborhood.

**Not yet fixed.** Next steps to try, in likely order of impact:
- Tighten the mask radius multiplier (currently 2.5×) for dense fields, or
  scale it inversely with local star density instead of a fixed constant
- Investigate `cv2.INPAINT_NS` (Navier-Stokes) as an alternative to TELEA —
  different failure characteristics on large contiguous regions
- Consider capping how much of the frame the starless mask is allowed to
  cover before falling back to `_reduce_stars`'s lighter erosion approach
  instead (i.e. pick the technique per-field based on measured star
  density, not a single hardcoded approach)

Restored SH2-142's known-good plain stack (`save/SH2-142_320frames_2026-09-19.jpg`)
after each failed attempt rather than leaving a broken result in place.
