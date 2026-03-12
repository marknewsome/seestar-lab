#!/usr/bin/env python3
"""
Seestar Lab — Comet animation and stacking processor.

════════════════════════════════════════════════════════════════════════════════
OUTPUTS  (all written to COMET_DIR)
════════════════════════════════════════════════════════════════════════════════

  comet_stars_fixed.mp4     Stars-aligned animation.  Background stars are
                            fixed; the comet nucleus drifts across the field,
                            showing its real sky motion over days/weeks.

  comet_nucleus_fixed.mp4   Nucleus-centred animation.  The comet head stays
                            locked in the frame; background stars trail behind
                            it.  Coma and inner tail structure are easy to
                            compare frame-to-frame.

  comet_stack.jpg           Mean composite of all star-aligned frames.  Stars
                            are sharp; the comet traces a smeared arc.  Useful
                            as a reference field image and context view.

  comet_nucleus_stack.jpg   Mean composite of all nucleus-aligned frames.  Tail
                            and coma flux co-add coherently across sessions,
                            revealing faint structure not visible in any single
                            frame.  Satellite/aircraft trail pixels are excluded
                            per-frame via connected-component trail detection so
                            they do not contaminate the stack.  Sky background
                            is subtracted using only covered pixels.

  comet_ls.jpg              Larson–Sekanina rotational-gradient image of the
                            nucleus-aligned deep stack.  Symmetric coma
                            brightness cancels; jets, shells, and asymmetric
                            structure appear as light/dark features.

  comet_portrait.jpg        Auto-composed portrait crop from the nucleus stack.
                            The tail direction is estimated from flux asymmetry;
                            the image is rotated so the tail points upward and
                            cropped to a 2:3 portrait with the nucleus placed in
                            the lower-centre third.  Comet name and date range
                            are overlaid.

  comet_track.jpg           Single reference frame with the smoothed nucleus
                            path drawn as a coloured polyline, showing the
                            comet's trajectory across the field.

  comet_alignment.json      JSON cache of per-frame similarity transforms and
                            detected nucleus positions.  Allows re-rendering
                            with different stretch / crop parameters without
                            re-running the slow alignment passes.

  _frames/frame_NNNN.jpg    Individual annotated frames for the in-app frame
                            browser: stars-fixed view with nucleus marker
                            overlay, used by the "Fix nucleus" UI workflow.

════════════════════════════════════════════════════════════════════════════════
PIPELINE  (5 passes)
════════════════════════════════════════════════════════════════════════════════

Pass 1 — Star alignment
  Each FITS frame is aligned to the highest-sub-count reference frame using
  a 2-D similarity transform (translation + rotation + uniform scale, 4 DOF).

  Primary method — astroalign:
    Detects point sources via sigma-thresholded convolution, builds a
    triangle-invariant hash index, and finds a geometrically consistent match
    set via a RANSAC-like consensus step.  Robust to field overlap as low as
    ~30 % and to the comet moving within the frame.

  Fallback method — OpenCV ORB:
    Extracts 2 000 binary BRIEF descriptors, matches them with brute-force
    Hamming distance + cross-check, then fits an affine-partial transform via
    RANSAC.  Scale is validated to [0.85, 1.15] to reject bogus matches.
    Used when star density is too low or the fields overlap insufficiently
    for astroalign's triangle hashing.

  Frames where both methods fail are marked unaligned (None transform).  They
  are still included in the nucleus-fixed output using raw-frame coordinates.

  Transforms are stored in comet_alignment.json.  Subsequent runs load the
  cache unless --no-cache is passed.

Pass 2 — Nucleus detection
  The comet nucleus is located in each ORIGINAL (pre-aligned) frame, then the
  detected pixel position is transformed into reference-frame coordinates.

  Detection algorithm (_find_nucleus_in_frame):
    1. Restrict the search to a circular region of radius ~40 % of frame size
       around either the user hint offset or the previous frame's result
       (rolling hint), or the frame centre for the first frame.
    2. Subtract a large-σ Gaussian (σ=60 px) background estimate from the
       search region to remove the sky gradient and sensor amp glow.
    3. Compute a diffuseness score:  large_blur² / (small_blur + ε)
       where large_blur (σ=25) and small_blur (σ=4) are Gaussian blurs of
       the background-subtracted residual.
       A point star loses brightness rapidly as σ grows; an extended coma
       remains relatively bright at large σ.  The ratio therefore rewards
       the broad fuzzy coma while penalising sharp stars.
    4. Find the peak of the score map, then centroid within a 40-px window
       around it for sub-pixel stability.

  User hint:
    --nucleus-hint-x/y provide fractional (0–1) frame coordinates clicked in
    the UI.  These are converted to a pixel offset from the reference frame
    centre and applied uniformly to every frame's search origin.  This works
    because the Seestar re-centres on the comet each session, so the nucleus
    is always near raw-frame centre; a fixed offset from centre is the right
    model for correcting systematic detection error.

  Position smoothing:
    Raw centroid detections jitter a few pixels frame-to-frame due to seeing
    and the diffuseness-score peak wandering within the coma.  A Gaussian
    temporal smooth (σ=3 frames, reflected-pad at boundaries) removes this
    high-frequency noise while preserving the real multi-day comet drift.

Pass 3 — Stars-fixed animation  (sub-passes 3a / 3b / 3c)

  3a — Union canvas
    Transforms the four corners of every frame into reference-frame pixel
    space and computes the bounding box of all resulting points.  This defines
    a canvas large enough to hold every aligned frame without clipping content.

  3b — Fill composite
    All frames are warped onto the union canvas with BORDER_CONSTANT (zero
    fill) and accumulated into a weighted mean.  The resulting composite
    contains real sky signal for every canvas pixel covered by at least one
    frame.  Pixels never covered by any frame (rare corner slivers) are
    inpainted with cv2.INPAINT_TELEA.

    A static alpha map is built from the per-pixel coverage fraction, Gaussian-
    blurred (σ=40 px) to create a soft vignette at the frame boundaries.
    This map is computed ONCE and applied identically to every output frame so
    the canvas edge never shifts between frames (which would appear as a
    dancing border in the animation).

  3c — Output frames
    Each frame is warped onto the union canvas, then blended with the fill
    composite:  pixel = frame × α + composite × (1−α).
    INTER_LINEAR warping avoids ringing near the zero-fill boundary.
    Each frame is display-stretched, noise-reduced, labelled, and saved as a
    JPEG review frame with the detected nucleus position annotated.

Pass 4 — Nucleus-fixed animation
  For each frame with a valid nucleus position, a composite affine transform
  is built that applies the star-alignment transform AND translates the
  nucleus to the reference frame's nucleus position in a single warpAffine.
  The resulting aligned_rgb is then:
    • Accumulated into stack_sum for the deep stack.
    • Cropped to a NUCLEUS_CROP_PX square window around the nucleus.
    • Display-stretched, noise-reduced, and labelled for the animation.

  Deep stack sky subtraction:
    The nucleus-aligned mean stack contains large zero-fill borders (the
    BORDER_CONSTANT fill for each frame's alignment shift).  Standard
    percentile sky estimation over the whole canvas would see mostly black
    pixels and estimate sky ≈ 0, leaving the real sky level in the image.
    Instead, sky is estimated only from covered (non-zero) pixels, subtracted
    per-channel, then the image is passed to _stretch with sky_pct=0.

Pass 5 — Track composite
  The reference frame is stretched and the smoothed nucleus path is drawn as
  a colour-graduated polyline, giving a single image that shows the full
  comet trajectory across the field over the observation period.

════════════════════════════════════════════════════════════════════════════════
DISPLAY STRETCH  (_stretch)
════════════════════════════════════════════════════════════════════════════════

  Applied independently to every output image/frame:
    1. Per-channel sky subtraction at STRETCH_SKY_PCT percentile (default 25).
    2. Divide by the STRETCH_HIGH_PCT percentile of luminance (default 99.8),
       mapping the bright-but-not-saturated region to white.
    3. Gamma correction: output = input^STRETCH_GAMMA (default 0.5),
       which boosts faint coma and tail signal without blowing out the nucleus.
    4. Clip to [0, 1].

════════════════════════════════════════════════════════════════════════════════
FITS COMPATIBILITY
════════════════════════════════════════════════════════════════════════════════

  Handles two common Seestar FITS layouts:
    • 2-D Bayer mono sub  — (H, W) uint16 with BAYERPAT header → debayered to
      (H, W, 3) RGB float32.  Supports RGGB, BGGR, GBRG, GRBG patterns.
    • Pre-debayered stack — (3, H, W) uint16 → transposed to (H, W, 3).
  BZERO / BSCALE are applied in both cases.
  Sub-count (nsubs) is parsed from the Seestar filename convention
  "Stacked_N_*.fit"; individual subs default to nsubs=1.

════════════════════════════════════════════════════════════════════════════════
CLI FLAGS
════════════════════════════════════════════════════════════════════════════════

  COMET_DIR             Directory containing .fit files (positional, optional).
  --no-cache            Redo star alignment even if comet_alignment.json exists.
  --redetect-nucleus    Re-run nucleus detection using cached star transforms.
  --nucleus-hint-x X    User-corrected nucleus X as fraction of frame width (0–1).
  --nucleus-hint-y Y    User-corrected nucleus Y as fraction of frame height (0–1).
  --files-json JSON     JSON array of absolute .fit paths; bypasses directory glob.
  --fps N               Animation frame rate (default 10).
  --gamma G             Stretch gamma, <1 brightens faint features (default 0.5).
  --crop PX             Nucleus-fixed crop window side length (default 700).
  --sky-pct P           Sky background percentile for stretch (default 25).
  --high-pct P          White-point percentile for stretch (default 99.8).
  --noise N             Bilateral noise reduction strength, 0=off (default 0).
  --width PX            Output frame width in pixels (default 1080).
  --max-frames N        Subsample to at most N frames (default 300, 0=unlimited).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import astroalign as aa
import cv2
import numpy as np
from astropy.io import fits

# ── Paths ──────────────────────────────────────────────────────────────────────

COMET_DIR   = "/mnt/d/xfer/C-2025 A6_sub"
OUT_DIR     = COMET_DIR
CACHE_JSON  = os.path.join(OUT_DIR, "comet_alignment.json")
STARS_OUT   = os.path.join(OUT_DIR, "comet_stars_fixed.mp4")
NUCLEUS_OUT = os.path.join(OUT_DIR, "comet_nucleus_fixed.mp4")
TRACK_OUT   = os.path.join(OUT_DIR, "comet_track.jpg")

# ── Tunable parameters ─────────────────────────────────────────────────────────

FPS               = 10      # animation frame rate
OUTPUT_WIDTH      = 1080    # output frame width (height scaled proportionally)
STRETCH_SKY_PCT   = 25.0    # percentile for sky background estimate
STRETCH_HIGH_PCT  = 99.8    # percentile for white point (per-frame)
STRETCH_GAMMA     = 0.5     # gamma applied after linear stretch (< 1 boosts dim features)
NUCLEUS_CROP_PX   = 700     # side length (px) of the comet-fixed crop window
AA_SIGMA          = 5.0     # source-detection sigma for astroalign
AA_MAX_PTS        = 60      # max control points for astroalign
MIN_SUBS          = 1       # skip frames with fewer stacked subs than this
MAX_FRAMES        = 300     # subsample to at most this many frames (0 = no limit)
NOISE_LEVEL       = 0       # bilateral noise reduction strength 0=off, 1–5=increasing
MAX_GAP_MULT      = 4.0     # VFR: cap large gaps at this multiple of the median (0=no cap)
TITLE_SECS        = 2.5     # duration of the title card at the start of each animation

# ── FITS loading ───────────────────────────────────────────────────────────────

_BAYER_CODES = {
    "BGGR": cv2.COLOR_BayerBG2RGB,
    "GBRG": cv2.COLOR_BayerGB2RGB,
    "GRBG": cv2.COLOR_BayerGR2RGB,
    "RGGB": cv2.COLOR_BayerRG2RGB,
}


def _fits_dims(path: str) -> tuple[int, int]:
    """Return (height, width) from FITS header without loading pixel data."""
    with fits.open(path, memmap=False) as hdul:
        hdr = hdul[0].header
    return int(hdr["NAXIS2"]), int(hdr["NAXIS1"])


def _load_fits_meta(path: str) -> dict:
    """Read FITS headers only — no pixel data loaded."""
    with fits.open(path, memmap=False) as hdul:
        hdr = hdul[0].header
    stem  = Path(path).stem
    parts = stem.split("_")
    nsubs = 1
    if parts[0].lower() == "stacked":
        try:
            nsubs = int(parts[1])
        except (IndexError, ValueError):
            pass
    return {
        "date_obs": hdr.get("DATE-OBS", ""),
        "exptime":  float(hdr.get("EXPTIME", 0)),
        "nsubs":    nsubs,
    }


def _load_fits(path: str) -> tuple[np.ndarray, dict]:
    """Load a Seestar FITS frame → (H, W, 3) float32, plus metadata.

    Handles both:
      • Individual Bayer subs  — (H, W) uint16 with BAYERPAT header → debayer
      • Pre-debayered stacks   — (3, H, W) uint16 → transpose to (H, W, 3)
    """
    with fits.open(path) as hdul:
        hdr  = hdul[0].header
        raw  = hdul[0].data          # uint16, shape varies

    bzero  = float(hdr.get("BZERO",  0))
    bscale = float(hdr.get("BSCALE", 1))
    bayer  = hdr.get("BAYERPAT", "")

    if raw.ndim == 2 and bayer:
        # Raw Bayer mono sub — debayer to (H, W, 3) RGB uint16
        code = _BAYER_CODES.get(bayer.upper(), cv2.COLOR_BayerGR2RGB)
        rgb16 = cv2.cvtColor(raw.astype(np.uint16), code)   # (H, W, 3) uint16
        data  = rgb16.astype(np.float32) * bscale + bzero
    elif raw.ndim == 3 and raw.shape[0] == 3:
        # Pre-debayered RGB stack: (3, H, W) → (H, W, 3)
        data = np.transpose(raw, (1, 2, 0)).astype(np.float32) * bscale + bzero
    else:
        data = raw.astype(np.float32) * bscale + bzero

    # Parse nsubs: "Stacked_N_..." → N; individual subs → 1
    stem  = Path(path).stem
    parts = stem.split("_")
    nsubs = 1
    if parts[0].lower() == "stacked":
        try:
            nsubs = int(parts[1])
        except (IndexError, ValueError):
            pass

    meta = {
        "date_obs": hdr.get("DATE-OBS", ""),
        "exptime":  float(hdr.get("EXPTIME", 0)),
        "nsubs":    nsubs,
    }
    return data, meta


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Convert (H,W,3) float to (H,W) luminance."""
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def _stretch(rgb: np.ndarray,
             sky_pct:  Optional[float] = None,
             high_pct: Optional[float] = None,
             gamma:    Optional[float] = None,
             stat_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Per-frame display stretch:
      1. Subtract per-channel sky background (low percentile).
      2. Scale so the high percentile of luminance maps to 1.
      3. Apply gamma to bring up faint coma / tail.
      4. Clip to [0, 1].

    sky_pct/high_pct/gamma override the module globals when provided.
    stat_mask — optional boolean (H,W) array; when given, sky and high
      percentiles are computed only from masked pixels.  Use this when
      the image contains composite-fill areas with different brightness
      (e.g. the stars-fixed canvas), so only the real frame pixels drive
      the calibration.
    """
    s_pct = sky_pct  if sky_pct  is not None else STRETCH_SKY_PCT
    h_pct = high_pct if high_pct is not None else STRETCH_HIGH_PCT
    g     = gamma    if gamma    is not None else STRETCH_GAMMA

    out = rgb.copy()
    for c in range(3):
        vals = out[..., c][stat_mask] if stat_mask is not None else out[..., c].ravel()
        sky  = np.percentile(vals, s_pct)
        out[..., c] = out[..., c] - sky

    lum = _luminance(out)
    if stat_mask is not None:
        lum_vals = lum[stat_mask]
    else:
        lum_vals = lum[lum > 0]
    high = np.percentile(lum_vals[lum_vals > 0], h_pct) if np.any(lum_vals > 0) else 1.0
    if high > 0:
        out = out / high

    out = np.power(np.clip(out, 0, 1), g)
    return np.clip(out, 0, 1)


def _apply_noise(bgr: np.ndarray, noise: int = 0) -> np.ndarray:
    """Bilateral noise reduction. noise=0 → no-op; 1–5 → increasing strength."""
    if noise <= 0:
        return bgr
    sigma = float(10 + noise * 10)   # 20 … 60 for levels 1–5
    return cv2.bilateralFilter(bgr, 9, sigmaColor=sigma, sigmaSpace=sigma)


def _to_bgr8(rgb_float: np.ndarray, width: int, sharpen: bool = False) -> np.ndarray:
    """(H,W,3) float [0,1] → BGR uint8, resized to output_width.

    sharpen=True applies a mild unsharp mask — useful for the nucleus-fixed
    crop which is at native resolution (no downscale sharpening effect).
    """
    h, w = rgb_float.shape[:2]
    height = int(h * width / w)
    bgr = cv2.cvtColor((rgb_float * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    bgr = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_LANCZOS4)
    if sharpen:
        blur   = cv2.GaussianBlur(bgr, (0, 0), sigmaX=1.2)
        bgr    = cv2.addWeighted(bgr, 1.6, blur, -0.6, 0)
    return bgr


# ── Star alignment ─────────────────────────────────────────────────────────────

def _orb_align(src_lum: np.ndarray, ref_lum: np.ndarray
               ) -> Optional[tuple[float,float,float,float]]:
    """
    Fallback star alignment using OpenCV ORB feature matching.
    Returns (tx, ty, rot_deg, scale) or None if insufficient matches.
    """
    def _to8(img):
        lo, hi = np.percentile(img, 0.5), np.percentile(img, 99.5)
        if hi <= lo: return None
        return np.clip((img - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)

    s8, r8 = _to8(src_lum), _to8(ref_lum)
    if s8 is None or r8 is None:
        return None

    orb = cv2.ORB_create(nfeatures=2000)
    kp1, des1 = orb.detectAndCompute(s8, None)
    kp2, des2 = orb.detectAndCompute(r8, None)
    if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    matches = sorted(matches, key=lambda m: m.distance)[:80]
    if len(matches) < 8:
        return None

    src_pts = np.float32([kp1[m.queryIdx].pt for m in matches]).reshape(-1,1,2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in matches]).reshape(-1,1,2)
    M, mask = cv2.estimateAffinePartial2D(src_pts, dst_pts,
                                          method=cv2.RANSAC,
                                          ransacReprojThreshold=3.0)
    if M is None or mask.sum() < 6:
        return None

    tx    = float(M[0, 2])
    ty    = float(M[1, 2])
    rot   = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    scale = float(np.sqrt(M[0, 0]**2 + M[1, 0]**2))
    if not (0.85 < scale < 1.15):
        return None  # bogus scale — likely false feature matches
    return tx, ty, rot, scale


def _align_stars(
    files: list[str],
    ref_idx: int,
) -> list[Optional[tuple[float, float, float, float]]]:
    """
    Compute per-frame similarity transforms relative to the reference frame.
    Tries astroalign first; falls back to ORB; marks unaligned as None.

    Returns transforms — list of (tx, ty, rot, scale) or None per frame.
    No pixel data is retained in memory between frames.
    """
    ref_data, _ = _load_fits(files[ref_idx])
    ref_lum = _luminance(ref_data)
    del ref_data  # free immediately

    transforms = [None] * len(files)
    transforms[ref_idx] = (0.0, 0.0, 0.0, 1.0)

    for i, f in enumerate(files):
        if i == ref_idx:
            continue
        src_data, _ = _load_fits(f)
        src_lum = _luminance(src_data)
        del src_data
        tx = ty = rot = 0.0; scale = 1.0; method = "none"

        # Try 1: astroalign (best for overlapping star fields)
        try:
            transform, _ = aa.find_transform(
                src_lum, ref_lum,
                detection_sigma=AA_SIGMA,
                max_control_points=AA_MAX_PTS,
            )
            m     = transform.params
            tx    = float(m[0, 2]); ty    = float(m[1, 2])
            rot   = float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))
            scale = float(np.sqrt(m[0, 0]**2 + m[1, 0]**2))
            method = "astroalign"
        except Exception:
            pass

        # Try 2: ORB fallback
        if method == "none":
            res = _orb_align(src_lum, ref_lum)
            if res:
                tx, ty, rot, scale = res
                method = "ORB"

        if method != "none":
            transforms[i] = (tx, ty, rot, scale)
            print(f"    [{i+1:2d}] {method:12s}  tx={tx:+6.1f} ty={ty:+6.1f} "
                  f"rot={rot:+5.2f}° scale={scale:.4f}", flush=True)
        else:
            transforms[i] = None
            print(f"    [{i+1:2d}] unaligned    (no star overlap — comet-fixed only)",
                  flush=True)

    return transforms


# ── Comet nucleus detection ─────────────────────────────────────────────────────

def _find_nucleus_in_frame(lum: np.ndarray,
                            hint_x: Optional[float] = None,
                            hint_y: Optional[float] = None,
                            search_r: int = 400) -> Optional[tuple[float, float]]:
    """
    Find the comet nucleus in a single luminance frame.

    The Seestar tracks the comet each session, so the nucleus is always near
    the frame centre.  We search within search_r pixels of the hint position
    (default: frame centre) for the brightest diffuse blob.
    """
    h, w = lum.shape
    cx = int(hint_x) if hint_x is not None else w // 2
    cy = int(hint_y) if hint_y is not None else h // 2

    # Restrict to search region
    x1 = max(0, cx - search_r); x2 = min(w, cx + search_r)
    y1 = max(0, cy - search_r); y2 = min(h, cy + search_r)
    roi = lum[y1:y2, x1:x2].astype(np.float32)

    # Background subtract: remove large-scale gradient
    bg = cv2.GaussianBlur(roi, (0, 0), sigmaX=60)
    residual = np.clip(roi - bg * 0.95, 0, None)

    # Diffuseness score: large_blur^2 / small_blur.
    # A point star loses peak rapidly as sigma grows; an extended coma
    # stays relatively bright.  The ratio heavily penalises point sources
    # and rewards the broad fuzzy coma we're looking for.
    small = cv2.GaussianBlur(residual, (0, 0), sigmaX=4)
    large = cv2.GaussianBlur(residual, (0, 0), sigmaX=25)
    eps   = float(np.percentile(residual, 99)) * 0.05 + 1.0
    score = (large ** 2) / (small + eps)
    _, _, _, max_loc = cv2.minMaxLoc(score)

    # Centroid within a window around the peak to reduce per-frame jitter.
    # A single argmax pixel is noisy; the weighted centroid of the score peak
    # is stable to sub-pixel accuracy even when the coma centre shifts slightly.
    CENTROID_R = 40
    px, py = max_loc
    wx1 = max(0, px - CENTROID_R); wx2 = min(score.shape[1], px + CENTROID_R + 1)
    wy1 = max(0, py - CENTROID_R); wy2 = min(score.shape[0], py + CENTROID_R + 1)
    window = score[wy1:wy2, wx1:wx2]
    total  = float(window.sum())
    if total > 0:
        ys, xs = np.mgrid[wy1:wy2, wx1:wx2]
        cx_sub = float((xs * window).sum() / total)
        cy_sub = float((ys * window).sum() / total)
    else:
        cx_sub, cy_sub = float(px), float(py)

    return (float(x1 + cx_sub), float(y1 + cy_sub))


def _find_nucleus(
    files: list[str],
    transforms: list[Optional[tuple[float, float, float, float]]],
    nucleus_hint_aligned: Optional[tuple[float, float]] = None,
    ref_dims: Optional[tuple[int, int]] = None,
) -> list[Optional[tuple[float, float]]]:
    """
    Detect the comet nucleus in each ORIGINAL (unaligned) frame, then transform
    each detected position into the aligned (reference) frame coordinate system.

    nucleus_hint_aligned — optional (x, y) in the ALIGNED reference-frame pixel
        coordinate space (i.e. where the user clicked in the annotated-frame
        viewer).  Converted ONCE to a raw-space offset from frame centre and
        applied uniformly to every frame.

        Why not per-frame inverse transforms?  The Seestar re-points to the comet
        each session, so the nucleus is always near raw-frame centre.  The aligned
        nucleus position changes frame-to-frame (that's the stars-fixed animation),
        so applying frame i's inverse transform to the reference-frame click gives
        the wrong raw-space position for frame i.  The correct model is: the user
        is telling us the nucleus is δ pixels away from raw-frame centre; apply
        that same δ to every frame.

    When no user hint is provided a rolling hint is used instead: the detected
    raw-frame position from the previous frame seeds the next frame's search,
    tracking slight comet drift within a session.
    """
    # Pre-compute fixed raw-space offset from frame centre.
    # Reference frame has identity transform → aligned ≈ raw for that frame,
    # so the click in aligned space directly gives the raw-space offset from centre.
    raw_hint_offset: Optional[tuple[float, float]] = None
    if nucleus_hint_aligned is not None and ref_dims is not None:
        ax, ay = nucleus_hint_aligned
        rw, rh = ref_dims
        raw_hint_offset = (ax - rw / 2.0, ay - rh / 2.0)

    positions = []
    last_orig_pos: Optional[tuple[float, float]] = None

    for i, f in enumerate(files):
        src_data, _ = _load_fits(f)
        src_lum = _luminance(src_data)
        h, w = src_lum.shape
        search_r = int(min(h, w) * 0.4)

        # ── Determine search hint ─────────────────────────────────────────────
        if raw_hint_offset is not None:
            # Same offset from centre applied to every frame.
            dx, dy = raw_hint_offset
            hx, hy = w / 2.0 + dx, h / 2.0 + dy
        elif last_orig_pos is not None:
            # Rolling hint: previous frame's detected raw position (no user hint).
            hx, hy = last_orig_pos
        else:
            hx, hy = float(w // 2), float(h // 2)

        pos_orig = _find_nucleus_in_frame(src_lum, hint_x=hx, hint_y=hy,
                                          search_r=search_r)
        if pos_orig is None:
            positions.append(None)
            last_orig_pos = None
            continue

        last_orig_pos = pos_orig
        ox, oy = pos_orig
        t = transforms[i]
        if t is not None:
            # Transform the original-frame nucleus position into aligned coords:
            #   aligned_pt = R_scale * orig_pt + translation
            tx, ty, rot_deg, scale = t
            rad = np.radians(rot_deg)
            c, s = np.cos(rad), np.sin(rad)
            ax_out = scale * c * ox - scale * s * oy + tx
            ay_out = scale * s * ox + scale * c * oy + ty
            positions.append((float(ax_out), float(ay_out)))
        else:
            # No star alignment for this frame — use original-frame position
            positions.append((float(ox), float(oy)))

    return positions


# ── Build one animation ─────────────────────────────────────────────────────────

def _make_title_frame(w: int, h: int,
                      comet_name: str,
                      anim_label: str,
                      date_range: str) -> np.ndarray:
    """Render a title-card BGR frame for the start of each animation.

    Scales all text sizes relative to a 1080-wide reference so the card looks
    correct at any output resolution.
    """
    # Subtle top-lit dark background
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        v = int(18 * (1.0 - y / h))
        frame[y, :] = (v, v, v)

    font  = cv2.FONT_HERSHEY_DUPLEX
    cx    = w // 2
    scale = w / 1080.0

    def _put(text: str, cy: int, fs: float, color: tuple, thickness: int = 1) -> None:
        fs_s = fs * scale
        th   = max(1, round(thickness * scale))
        (tw, _), _ = cv2.getTextSize(text, font, fs_s, th)
        x = cx - tw // 2
        cv2.putText(frame, text, (x + 1, cy + 1), font, fs_s,
                    (0, 0, 0), th + 2, cv2.LINE_AA)
        cv2.putText(frame, text, (x, cy), font, fs_s, color, th, cv2.LINE_AA)

    # Comet name — large, accent cyan
    _put(comet_name, int(h * 0.33), 1.0, (100, 220, 255), 2)

    # Thin divider line
    lw = int(w * 0.36)
    ly = int(h * 0.45)
    cv2.line(frame, (cx - lw // 2, ly), (cx + lw // 2, ly),
             (40, 90, 60), max(1, round(scale)))

    # Animation type — medium, light
    _put(anim_label, int(h * 0.57), 0.78, (210, 210, 210), 1)

    # Date range — small, muted
    _put(date_range, int(h * 0.70), 0.52, (115, 115, 115), 1)

    # Bottom-right watermark
    wm    = "Seestar Lab"
    wm_fs = 0.42 * scale
    (ww, _), _ = cv2.getTextSize(wm, font, wm_fs, 1)
    cv2.putText(frame, wm,
                (w - ww - int(18 * scale), h - int(18 * scale)),
                font, wm_fs, (55, 55, 55), 1, cv2.LINE_AA)

    return frame


def _prepend_title(frames: list[np.ndarray],
                   durations: Optional[list[float]],
                   comet_name: str,
                   anim_label: str,
                   date_range: str) -> tuple[list[np.ndarray], Optional[list[float]]]:
    """Prepend a title card to frames (and matching duration entry if VFR)."""
    if not frames:
        return frames, durations
    h, w = frames[0].shape[:2]
    title = _make_title_frame(w, h, comet_name, anim_label, date_range)
    if durations is not None:
        return [title] + frames, [TITLE_SECS] + durations
    # CFR: repeat the title frame for TITLE_SECS worth of frames
    n_title = max(1, round(TITLE_SECS * FPS))
    return [title] * n_title + frames, None


def _compute_frame_durations(
    metas: list[dict],
    fps: float,
    max_gap_mult: float,
) -> list[float]:
    """
    Compute per-frame display durations (seconds) proportional to real timestamps.

    The median inter-frame gap maps to the nominal 1/fps duration.  Larger gaps
    are scaled up proportionally so the animation plays faster through dense
    sequences and lingers at real observing gaps.  max_gap_mult caps the ratio
    so a multi-day cloud-out doesn't freeze the animation (0 = no cap).
    """
    from datetime import datetime as _dt
    nominal = 1.0 / fps
    n = len(metas)
    if n < 2:
        return [nominal] * n

    timestamps: list[Optional[float]] = []
    for m in metas:
        try:
            ts = _dt.fromisoformat(m["date_obs"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, AttributeError):
            ts = None
        timestamps.append(ts)

    # Collect valid gaps to find the median reference cadence
    gaps = []
    for i in range(n - 1):
        t0, t1 = timestamps[i], timestamps[i + 1]
        if t0 is not None and t1 is not None and t1 > t0:
            gaps.append(t1 - t0)

    if not gaps:
        return [nominal] * n

    median_gap = float(np.median(gaps))
    if median_gap <= 0:
        return [nominal] * n

    durations = []
    for i in range(n):
        if i < n - 1:
            t0, t1 = timestamps[i], timestamps[i + 1]
            if t0 is not None and t1 is not None and t1 > t0:
                ratio = (t1 - t0) / median_gap
                if max_gap_mult > 0:
                    ratio = min(ratio, max_gap_mult)
                dur = ratio * nominal
            else:
                dur = nominal
        else:
            dur = nominal          # last frame gets nominal duration
        durations.append(max(dur, nominal * 0.1))  # floor at 10 % of nominal

    return durations


def _write_video(frames_bgr: list[np.ndarray], path: str,
                 durations: Optional[list[float]] = None) -> None:
    """Write a list of BGR uint8 frames to an MP4 via ffmpeg.

    durations: per-frame display time in seconds.  When provided the output
    uses variable frame rate (ffconcat demuxer) so real observing gaps are
    represented proportionally in playback speed.  None → uniform FPS.
    """
    if not frames_bgr:
        return

    ffbin = "/usr/bin/ffmpeg" if os.path.isfile("/usr/bin/ffmpeg") else shutil.which("ffmpeg")

    # ── VFR path ───────────────────────────────────────────────────────────────
    if durations is not None and ffbin:
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="comet_vfr_")
        try:
            concat_path = os.path.join(tmp_dir, "frames.txt")
            with open(concat_path, "w") as fh:
                fh.write("ffconcat version 1.0\n")
                for j, (frame, dur) in enumerate(zip(frames_bgr, durations)):
                    jpg = os.path.join(tmp_dir, f"f{j:05d}.jpg")
                    cv2.imwrite(jpg, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    fh.write(f"file '{jpg}'\nduration {dur:.6f}\n")
                # Repeat last entry without duration so ffmpeg flushes it
                last = os.path.join(tmp_dir, f"f{len(frames_bgr)-1:05d}.jpg")
                fh.write(f"file '{last}'\n")

            tmp_out = path + ".vfr.tmp.mp4"
            r = subprocess.run(
                [ffbin, "-y", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", concat_path,
                 "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                 "-pix_fmt", "yuv420p", tmp_out],
                capture_output=True,
            )
            if r.returncode == 0:
                os.replace(tmp_out, path)
                return
            print(f"  VFR encode warning: {r.stderr.decode(errors='replace')[:200]}", flush=True)
            # Fall through to CFR below
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── CFR path (fallback or when durations=None) ─────────────────────────────
    h, w = frames_bgr[0].shape[:2]
    raw  = path + ".raw.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(raw, fourcc, FPS, (w, h))
    for f in frames_bgr:
        writer.write(f)
    writer.release()

    if ffbin:
        tmp = path + ".tmp.mp4"
        r = subprocess.run(
            [ffbin, "-y", "-loglevel", "error", "-i", raw,
             "-c:v", "libx264", "-crf", "18", "-preset", "slow",
             "-pix_fmt", "yuv420p", tmp],
            capture_output=True,
        )
        if r.returncode == 0:
            os.replace(tmp, path)
            os.remove(raw)
            return
    os.rename(raw, path)


def _overlay_label(img: np.ndarray, text: str) -> np.ndarray:
    out = img.copy()
    cv2.putText(out, text, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(out, text, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def _draw_nucleus_marker(img: np.ndarray, px: int, py: int,
                          label: str = "nucleus") -> None:
    """Draw a prominent nucleus annotation circle with crosshairs and text label."""
    color_outer = (0, 200, 255)   # cyan-yellow
    color_inner = (0, 255, 255)   # bright cyan
    r = 22
    # Shadow ring for contrast on bright backgrounds
    cv2.circle(img, (px, py), r + 2, (0, 0, 0), 3, cv2.LINE_AA)
    # Main circle
    cv2.circle(img, (px, py), r, color_outer, 2, cv2.LINE_AA)
    # Crosshair lines (gap in the centre so nucleus is visible)
    gap = 6
    for dx, dy in [(r + 8, 0), (-(r + 8), 0), (0, r + 8), (0, -(r + 8))]:
        x0, y0 = px + (gap if dx > 0 else -gap if dx < 0 else 0), \
                  py + (gap if dy > 0 else -gap if dy < 0 else 0)
        cv2.line(img, (x0, y0), (px + dx, py + dy), (0, 0, 0), 3, cv2.LINE_AA)
        cv2.line(img, (x0, y0), (px + dx, py + dy), color_inner, 1, cv2.LINE_AA)
    # Small fill dot at centre
    cv2.circle(img, (px, py), 3, color_inner, -1, cv2.LINE_AA)
    # Text label
    tx, ty = px + r + 6, py - 6
    cv2.putText(img, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                0.45, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                0.45, color_inner, 1, cv2.LINE_AA)


# ── Satellite trail detection ─────────────────────────────────────────────────

def _detect_trail_mask(
    rgb_float: np.ndarray,
    sigma_thresh: float = 6.0,
    min_length:   int   = 60,
    fill_thresh:  float = 0.25,
    dilation:     int   = 12,
) -> np.ndarray:
    """
    Return a boolean mask of satellite/aircraft trail pixels (True = contaminated).

    Algorithm:
      1. Compute luminance; estimate background with a large Gaussian blur.
      2. Compute residual = lum − background; threshold at sigma_thresh × MAD-σ
         to find anomalously bright features.
      3. Find connected components.  A component is classified as a trail if:
           max(bbox_width, bbox_height) ≥ min_length   (it is long)
           AND  area / (bbox_w × bbox_h)  ≤ fill_thresh  (it is sparse/thin)
         Stars are small and compact (fill ≈ 1); satellite trails are long and
         thin (fill ≈ 0.05–0.15).  Point sources and small clumps of bright
         stars are therefore ignored even if they exceed the brightness threshold.
      4. Dilate the trail mask by `dilation` pixels to cover PSF wings.

    Zero-fill border pixels (from warpAffine BORDER_CONSTANT) are naturally
    excluded because their luminance is 0 and they never exceed the threshold.
    """
    lum = _luminance(rgb_float)
    bg  = cv2.GaussianBlur(lum, (0, 0), sigmaX=25.0)
    residual = np.clip(lum.astype(np.float32) - bg, 0.0, None)

    # Robust σ estimate via MAD (resistant to stars and bright sky)
    med   = float(np.median(residual))
    mad   = float(np.median(np.abs(residual - med)))
    sigma = max(mad * 1.4826, 1e-9)

    binary = (residual > sigma_thresh * sigma).astype(np.uint8)

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8)

    trail_mask = np.zeros_like(binary, dtype=np.uint8)
    for lab in range(1, n_labels):
        bbox_w = int(stats[lab, cv2.CC_STAT_WIDTH])
        bbox_h = int(stats[lab, cv2.CC_STAT_HEIGHT])
        area   = int(stats[lab, cv2.CC_STAT_AREA])
        max_dim = max(bbox_w, bbox_h)
        fill    = area / max(bbox_w * bbox_h, 1)
        if max_dim >= min_length and fill <= fill_thresh:
            trail_mask[labels == lab] = 1

    if trail_mask.any():
        k    = dilation * 2 + 1
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        trail_mask = cv2.dilate(trail_mask, kern)

    return trail_mask.astype(bool)


# ── Larson-Sekanina filter ────────────────────────────────────────────────────

def _larson_sekanina(
    rgb_float: np.ndarray,
    cx: float,
    cy: float,
    angle_deg: float = 15.0,
) -> np.ndarray:
    """
    Rotational gradient filter (Larson–Sekanina).

    Subtracts a copy of the image rotated by `angle_deg` around the nucleus
    centre from the original.  Symmetric coma brightness cancels out; any
    asymmetric structure (jets, shells, fan tail) is left as signed residuals.

    The result is rescaled so the median of the bright pixels sits at 0.5 on a
    [0, 1] float32 output, giving a mid-gray background with light/dark
    features visible.  The nucleus core (r < 6 px in the output-scale image) is
    replaced with a neutral 0.5 disk so the extreme central peak does not clip
    the colour stretch.

    Returns float32 [0, 1] RGB, same shape as input.
    """
    h, w = rgb_float.shape[:2]
    # Rotate a copy around the nucleus centre
    M = cv2.getRotationMatrix2D((cx, cy), angle_deg, 1.0)
    rotated = cv2.warpAffine(rgb_float, M, (w, h),
                             flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REFLECT_101)
    diff = rgb_float.astype(np.float32) - rotated.astype(np.float32)

    # Coverage mask — avoid black border artefacts from warpAffine
    ones = np.ones((h, w), dtype=np.float32)
    cov  = cv2.warpAffine(ones, M, (w, h),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cov > 0.5

    # Normalise: shift so median of in-bounds pixels = 0.5
    in_vals = diff[mask]
    if in_vals.size == 0:
        return np.clip(diff * 0.5 + 0.5, 0, 1).astype(np.float32)
    med = float(np.median(in_vals))
    # Scale so ±2×IQR spans the [0,1] range
    q25, q75 = np.percentile(in_vals, [25, 75])
    iqr = max(float(q75 - q25), 1e-6)
    out = np.clip((diff - med) / (4 * iqr) + 0.5, 0.0, 1.0).astype(np.float32)
    # Zero-fill border pixels
    out[~mask] = 0.5

    # Neutralise nucleus core to avoid clipping the display stretch
    yy, xx = np.ogrid[:h, :w]
    core = (xx - cx) ** 2 + (yy - cy) ** 2 < 6 ** 2
    out[core] = 0.5

    return out


# ── Tail direction detection ──────────────────────────────────────────────────

def _find_tail_direction(
    nucleus_stack: np.ndarray,
    cx: float,
    cy: float,
) -> float:
    """
    Estimate the tail direction from the nucleus-aligned mean stack.

    Returns the angle **towards which the tail points** measured clockwise from
    up (north) in image coordinates, in degrees [0, 360).

    Algorithm:
      1. Work on the luminance channel.
      2. Mask out the bright nucleus core (r < 12 px).
      3. Compute a weighted centroid of pixel brightness in a 250-px annulus
         around the nucleus.  The centroid should lie in the direction of the
         coma/tail since the tail contributes more flux on one side.
      4. The tail direction is the angle from the nucleus to that centroid.
    """
    h, w = nucleus_stack.shape[:2]
    lum = _luminance(nucleus_stack)                     # float32 [0,1]

    yy, xx = np.ogrid[:h, :w]
    r2 = (xx - cx) ** 2 + (yy - cy) ** 2

    inner_r = 12.0
    outer_r = min(cx, cy, w - cx, h - cy) * 0.85       # stay inside image
    outer_r = max(outer_r, inner_r + 20)

    ring = (r2 > inner_r ** 2) & (r2 < outer_r ** 2)

    weights = lum * ring.astype(np.float32)
    total = weights.sum()
    if total < 1e-9:
        return 0.0  # can't determine — default "up"

    # Weighted centroid
    wx = float((weights * xx).sum() / total)
    wy = float((weights * yy).sum() / total)

    # Angle from nucleus to centroid, clockwise from up
    dx = wx - cx
    dy = wy - cy                # positive = down in image coords
    angle = float(np.degrees(np.arctan2(dx, -dy))) % 360.0
    return angle


# ── Comet portrait composer ───────────────────────────────────────────────────

def _comet_portrait(
    nucleus_stack: np.ndarray,
    cx: float,
    cy: float,
    comet_name: str,
    date_range: str,
    output_width: int,
) -> np.ndarray:
    """
    Auto-compose a portrait-format comet image.

    Steps:
      1. Detect tail direction; rotate the stack so the tail points upward.
      2. Pad the rotated image so a 2:3 (portrait) crop centred on the nucleus
         never goes out of bounds.
      3. Crop to 2:3 with the nucleus placed in the lower-centre third.
      4. Stretch and resize to `output_width`.
      5. Overlay comet name and date text.

    Returns a uint8 BGR image.
    """
    h, w = nucleus_stack.shape[:2]
    tail_ang = _find_tail_direction(nucleus_stack, cx, cy)

    # We want the tail to point UP, so rotate by -tail_ang
    # (clockwise positive in image coords means negative getRotationMatrix2D)
    rot_angle = -tail_ang
    M = cv2.getRotationMatrix2D((cx, cy), rot_angle, 1.0)

    # Pad before rotating so the comet stays inside the canvas
    pad = max(h, w)
    padded = cv2.copyMakeBorder(nucleus_stack.astype(np.float32),
                                pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=0)
    ph, pw = padded.shape[:2]
    pcx, pcy = cx + pad, cy + pad
    Mp = cv2.getRotationMatrix2D((pcx, pcy), rot_angle, 1.0)
    rotated = cv2.warpAffine(padded, Mp, (pw, ph),
                             flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # Portrait crop: width = crop_w, height = crop_h = 1.5 × crop_w
    # Nucleus sits at the lower-centre third (vertically 2/3 down)
    crop_w = min(w, h * 2 // 3)
    crop_h = crop_w * 3 // 2

    # Nucleus x-centre, nucleus placed at 2/3 height from top
    x1 = int(pcx - crop_w // 2)
    y1 = int(pcy - crop_h * 2 // 3)
    x2 = x1 + crop_w
    y2 = y1 + crop_h

    # Clamp to padded image bounds (should always fit because of pad above)
    x1 = max(0, min(pw - crop_w, x1))
    y1 = max(0, min(ph - crop_h, y1))
    x2 = x1 + crop_w
    y2 = y1 + crop_h
    crop = rotated[y1:y2, x1:x2]

    # Sky-subtract using covered pixels only (same logic as nucleus stack)
    covered = crop.sum(axis=2) > 0
    if covered.any():
        for c in range(3):
            ch  = crop[..., c]
            sky = np.percentile(ch[covered], STRETCH_SKY_PCT)
            crop[..., c] = np.where(covered, np.clip(ch - sky, 0, None), 0.0)

    bgr = _to_bgr8(_stretch(crop, sky_pct=0.0), output_width)

    # Label: comet name (large) + date range (small) at bottom
    bh, bw = bgr.shape[:2]
    font       = cv2.FONT_HERSHEY_DUPLEX
    font_scale = bw / 900.0
    y_name     = bh - int(50 * font_scale) - 4
    y_date     = bh - int(22 * font_scale) - 4

    for text, ypos, scale_mul, thickness in [
        (comet_name, y_name, 1.0,  2),
        (date_range, y_date, 0.55, 1),
    ]:
        fs = font_scale * scale_mul
        # shadow
        cv2.putText(bgr, text, (12, ypos), font, fs, (0, 0, 0), thickness + 2, cv2.LINE_AA)
        # white text
        cv2.putText(bgr, text, (12, ypos), font, fs, (220, 220, 220), thickness, cv2.LINE_AA)

    # Small watermark
    wm = "Seestar Lab"
    wm_scale = font_scale * 0.38
    (wm_w, _), _ = cv2.getTextSize(wm, font, wm_scale, 1)
    cv2.putText(bgr, wm, (bw - wm_w - 8, bh - 6), font,
                wm_scale, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.putText(bgr, wm, (bw - wm_w - 8, bh - 6), font,
                wm_scale, (120, 120, 120), 1, cv2.LINE_AA)

    return bgr


# ── Nucleus position smoothing ─────────────────────────────────────────────────

def _smooth_nucleus_positions(
    nucleus_pos: list[Optional[tuple[float, float]]],
    sigma: float = 3.0,
) -> list[Optional[tuple[float, float]]]:
    """
    Temporally smooth nucleus positions with a Gaussian kernel.

    Frame-to-frame jitter (from the diffuseness-score peak wandering slightly
    within the coma) makes the nucleus-fixed crop window bounce visibly.
    A Gaussian smooth with σ ≈ 3 frames removes high-frequency noise while
    preserving the real multi-day drift of the comet across the field.

    Frames without a detected nucleus (None) are skipped; surrounding valid
    positions are reflected-padded before convolution so edge frames are not
    pulled toward zero.
    """
    if not any(p is not None for p in nucleus_pos):
        return nucleus_pos

    n = len(nucleus_pos)
    valid_idx = [i for i, p in enumerate(nucleus_pos) if p is not None]

    # Build dense arrays with linear interpolation across gaps
    xs = np.full(n, np.nan)
    ys = np.full(n, np.nan)
    for i in valid_idx:
        xs[i], ys[i] = nucleus_pos[i]  # type: ignore[misc]

    if len(valid_idx) > 1:
        vi = np.array(valid_idx)
        # Interpolate interior gaps
        xs[vi[0]:vi[-1] + 1] = np.interp(
            np.arange(vi[0], vi[-1] + 1), vi, xs[vi])
        ys[vi[0]:vi[-1] + 1] = np.interp(
            np.arange(vi[0], vi[-1] + 1), vi, ys[vi])
    # Constant-fill any leading/trailing NaN so reflect-pad doesn't spread them
    vi = np.array(valid_idx)
    xs[:vi[0]]      = xs[vi[0]]
    xs[vi[-1] + 1:] = xs[vi[-1]]
    ys[:vi[0]]      = ys[vi[0]]
    ys[vi[-1] + 1:] = ys[vi[-1]]

    # Build Gaussian kernel
    ksize = max(3, int(sigma * 4) | 1)   # odd, at least 3
    half  = ksize // 2
    k     = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2)
    k    /= k.sum()

    def _smooth(arr: np.ndarray) -> np.ndarray:
        padded = np.pad(arr, half, mode="reflect")
        return np.convolve(padded, k, mode="valid")

    xs_sm = _smooth(xs)
    ys_sm = _smooth(ys)

    return [
        (float(xs_sm[i]), float(ys_sm[i])) if nucleus_pos[i] is not None else None
        for i in range(n)
    ]


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("comet_dir", nargs="?", default=COMET_DIR)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--redetect-nucleus", action="store_true",
                    help="Re-run nucleus detection using cached star alignment")
    ap.add_argument("--nucleus-hint-x", type=float, default=-1.0,
                    help="User-corrected nucleus X position as fraction of frame width (0–1)")
    ap.add_argument("--nucleus-hint-y", type=float, default=-1.0,
                    help="User-corrected nucleus Y position as fraction of frame height (0–1)")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                    help="Subsample to at most N frames (0 = no limit)")
    ap.add_argument("--files-json", default=None,
                    help="JSON array of absolute .fit paths; skips directory glob")
    ap.add_argument("--fps",      type=int,   default=None, help="Override FPS")
    ap.add_argument("--gamma",    type=float, default=None, help="Override stretch gamma")
    ap.add_argument("--crop",     type=int,   default=None, help="Override nucleus crop px")
    ap.add_argument("--sky-pct",  type=float, default=None, help="Override sky background percentile")
    ap.add_argument("--high-pct", type=float, default=None, help="Override white-point percentile")
    ap.add_argument("--noise",        type=int,   default=None, help="Noise reduction level 0–5")
    ap.add_argument("--width",        type=int,   default=None, help="Output frame width in pixels")
    ap.add_argument("--max-gap-mult", type=float, default=None,
                    help="VFR gap cap: max inter-frame gap as multiple of median (0=no cap, default 4)")
    ap.add_argument("--no-vfr",       action="store_true",
                    help="Disable variable frame rate; use uniform FPS instead")
    args = ap.parse_args()

    # Apply CLI overrides to module globals so helper functions pick them up
    global FPS, STRETCH_GAMMA, NUCLEUS_CROP_PX, STRETCH_SKY_PCT, STRETCH_HIGH_PCT, NOISE_LEVEL, MAX_GAP_MULT
    if args.fps          is not None: FPS              = args.fps
    if args.gamma        is not None: STRETCH_GAMMA    = args.gamma
    if args.crop         is not None: NUCLEUS_CROP_PX  = args.crop
    if args.sky_pct      is not None: STRETCH_SKY_PCT  = args.sky_pct
    if args.high_pct     is not None: STRETCH_HIGH_PCT = args.high_pct
    if args.noise        is not None: NOISE_LEVEL      = args.noise
    if args.width        is not None: OUTPUT_WIDTH     = args.width
    if args.max_gap_mult is not None: MAX_GAP_MULT     = args.max_gap_mult
    use_vfr = not args.no_vfr

    comet_dir   = args.comet_dir
    cache_json  = os.path.join(comet_dir, "comet_alignment.json")
    stars_out   = os.path.join(comet_dir, "comet_stars_fixed.mp4")
    nucleus_out = os.path.join(comet_dir, "comet_nucleus_fixed.mp4")
    track_out   = os.path.join(comet_dir, "comet_track.jpg")

    # ── Gather and sort files ──────────────────────────────────────────────────
    if args.files_json:
        all_fits = [Path(p) for p in json.loads(args.files_json)]
    else:
        all_fits = sorted(Path(comet_dir).glob("*.fit"))

    entries = []
    for f in all_fits:
        meta = _load_fits_meta(str(f))
        if meta["nsubs"] < MIN_SUBS:
            continue
        entries.append((meta["date_obs"], str(f), meta))
    entries.sort(key=lambda e: e[0])

    # Subsample evenly if too many frames
    max_frames = args.max_frames
    if max_frames > 0 and len(entries) > max_frames:
        step = len(entries) / max_frames
        entries = [entries[int(i * step)] for i in range(max_frames)]
        print(f"Subsampled to {len(entries)} frames (--max-frames {max_frames})")

    files = [e[1] for e in entries]
    metas = [e[2] for e in entries]
    n     = len(files)

    # ── Load user frame-rejection list ────────────────────────────────────────
    # rejected_indices is a list of 0-based frame indices the user marked in the
    # wizard's frame browser.  It is persisted in comet_alignment.json so it
    # survives re-renders.  We read it BEFORE checking cache_ok so that even a
    # full re-alignment (--no-cache) still respects the rejections.
    rejected_indices: set = set()
    if os.path.isfile(cache_json):
        try:
            with open(cache_json) as fh:
                _rj = json.load(fh)
            rejected_indices = set(int(x) for x in _rj.get("rejected_indices", []))
        except Exception:
            pass
    rejected   = [i in rejected_indices for i in range(n)]
    n_rejected = sum(rejected)

    print("─" * 60)
    print(f"Comet Processor  —  {Path(comet_dir).name}")
    rej_note = f"  ({n_rejected} rejected)" if n_rejected else ""
    print(f"Frames : {n}{rej_note}  ({entries[0][0][:10]} → {entries[-1][0][:10]})")
    if n_rejected:
        names = ", ".join(os.path.basename(files[i]) for i in sorted(rejected_indices))
        print(f"Rejected: {names}")
    print("─" * 60)

    if n - n_rejected < 2:
        sys.exit("Need at least 2 non-rejected frames.")

    # Pick reference: highest sub count among non-rejected frames
    ref_idx = max((i for i in range(n) if not rejected[i]),
                  key=lambda i: metas[i]["nsubs"])
    print(f"Reference frame: [{ref_idx}]  {metas[ref_idx]['date_obs'][:16]}"
          f"  ({metas[ref_idx]['nsubs']} subs)\n")

    # ── Pass 1: Star alignment ─────────────────────────────────────────────────
    nucleus_hint_provided = (0.0 <= args.nucleus_hint_x <= 1.0 and
                             0.0 <= args.nucleus_hint_y <= 1.0)
    cache_ok = os.path.isfile(cache_json) and not args.no_cache
    transforms:  list = []
    nucleus_pos: list = []

    if cache_ok:
        with open(cache_json) as fh:
            cache = json.load(fh)
        # Invalidate cache if it was built from a different number of frames
        if len(cache.get("transforms", [])) != n:
            print(f"[Pass 1] Cache frame count mismatch ({len(cache.get('transforms',[]))} "
                  f"vs {n}) — ignoring cache, re-aligning …")
            cache_ok = False
        else:
            transforms = [tuple(t) if t else None for t in cache["transforms"]]
            if not nucleus_hint_provided and not args.redetect_nucleus:
                print("[Pass 1] Loading cached alignment …")
                nucleus_pos = [tuple(p) if p else None for p in cache["nucleus_pos"]]
            elif args.redetect_nucleus:
                print("[Pass 1] Loading cached star alignment "
                      "(nucleus will be re-detected) …")
            else:
                print("[Pass 1] Loading cached star alignment "
                      "(nucleus will be re-detected with user hint) …")

    if not cache_ok:
        print("[Pass 1] Star alignment (astroalign) …")
        transforms = _align_stars(files, ref_idx)

    # Reference frame dimensions (needed for canvas computation and warpAffine)
    ref_data, _ = _load_fits(files[ref_idx])
    ref_h, ref_w = _luminance(ref_data).shape
    del ref_data

    # Compute canvas offset now (only depends on transforms + file dims).
    # Needed to correctly convert the user's canvas-space hint click to
    # raw-frame coordinates before Pass 2.
    _all_cx: list[float] = [0.0, float(ref_w), 0.0, float(ref_w)]
    _all_cy: list[float] = [0.0, 0.0, float(ref_h), float(ref_h)]
    for _i, _f in enumerate(files):
        if _i == ref_idx:
            continue
        _t = transforms[_i]
        _fh, _fw = _fits_dims(_f)
        _src_corners = [(0.0, 0.0), (float(_fw), 0.0),
                        (0.0, float(_fh)), (float(_fw), float(_fh))]
        if _t is None:
            for _cx_s, _cy_s in _src_corners:
                _all_cx.append(_cx_s); _all_cy.append(_cy_s)
        else:
            _tx, _ty, _rot_deg, _scale = _t
            _rad = np.radians(_rot_deg)
            _c, _s = np.cos(_rad), np.sin(_rad)
            for _cx_s, _cy_s in _src_corners:
                _all_cx.append(_scale * _c * _cx_s - _scale * _s * _cy_s + _tx)
                _all_cy.append(_scale * _s * _cx_s + _scale * _c * _cy_s + _ty)
    _canvas_off_x = int(np.floor(min(_all_cx)))
    _canvas_off_y = int(np.floor(min(_all_cy)))
    _canvas_w     = int(np.ceil(max(_all_cx))) - _canvas_off_x
    _canvas_h     = int(np.ceil(max(_all_cy))) - _canvas_off_y

    # ── Pass 2: Nucleus detection ──────────────────────────────────────────────
    if not nucleus_pos:   # empty when cache missed or user hint supplied
        # Build hint in reference-frame pixel coords from user fractional input.
        # The user clicked at fraction (fx, fy) of the canvas-space annotated JPEG.
        # Canvas pixel = (fx * canvas_w, fy * canvas_h).
        # Reference-frame pixel = canvas pixel + (off_x, off_y)
        # (the ref frame is shifted by (-off_x, -off_y) into the canvas).
        nucleus_hint_aligned = None
        ref_dims_for_hint    = None
        if nucleus_hint_provided:
            hx_canvas = args.nucleus_hint_x * _canvas_w
            hy_canvas = args.nucleus_hint_y * _canvas_h
            nucleus_hint_aligned = (hx_canvas + _canvas_off_x,
                                    hy_canvas + _canvas_off_y)
            ref_dims_for_hint    = (ref_w, ref_h)
            print(f"\n[Pass 2] Detecting nucleus (user hint: "
                  f"{nucleus_hint_aligned[0]:.0f}, {nucleus_hint_aligned[1]:.0f}) …")
        else:
            print("\n[Pass 2] Detecting comet nucleus …")

        nucleus_pos = _find_nucleus(files, transforms,
                                    nucleus_hint_aligned=nucleus_hint_aligned,
                                    ref_dims=ref_dims_for_hint)
        for i, pos in enumerate(nucleus_pos):
            if pos:
                print(f"    [{i+1:2d}] nucleus at ({pos[0]:.1f}, {pos[1]:.1f})")
            else:
                print(f"    [{i+1:2d}] nucleus not found")

        with open(cache_json, "w") as fh:
            json.dump({
                "transforms":       [list(t) if t else None for t in transforms],
                "nucleus_pos":      [list(p) if p else None for p in nucleus_pos],
                "rejected_indices": sorted(rejected_indices),
            }, fh, indent=2)
        print(f"  Alignment cached → {cache_json}")

    # Temporally smooth nucleus positions before rendering.
    # Raw centroid detections still jitter a few pixels frame-to-frame;
    # a σ=3-frame Gaussian averages over seeing fluctuations while preserving
    # the real multi-day comet drift.  Smoothed positions are used for both
    # the crop-window placement (nucleus-fixed) and the frame annotations.
    nucleus_pos = _smooth_nucleus_positions(nucleus_pos, sigma=3.0)
    print("  Nucleus positions smoothed (σ=3 frames)")

    # ── Pass 3: Stars-fixed animation ─────────────────────────────────────────
    print("\n[Pass 3] Building stars-fixed animation …")
    frames_dir = os.path.join(comet_dir, "_frames")
    os.makedirs(frames_dir, exist_ok=True)

    # ── 3a: Compute union canvas ───────────────────────────────────────────────
    # Find the bounding box (in reference-frame pixel coords) that contains all
    # source frames after alignment.  This gives a canvas larger than any single
    # frame so no content is ever cropped.
    print("  [3a] Computing union canvas …")
    all_cx: list[float] = [0.0, float(ref_w), 0.0, float(ref_w)]
    all_cy: list[float] = [0.0, 0.0, float(ref_h), float(ref_h)]
    for i, f in enumerate(files):
        if i == ref_idx:
            continue
        t = transforms[i]
        fh, fw = _fits_dims(f)   # header-only read — fast
        src_corners = [(0.0, 0.0), (float(fw), 0.0),
                       (0.0, float(fh)), (float(fw), float(fh))]
        if t is None:
            # No star alignment: frame occupies the same pixel space as ref
            for cx_s, cy_s in src_corners:
                all_cx.append(cx_s); all_cy.append(cy_s)
        else:
            tx, ty, rot_deg, scale = t
            rad = np.radians(rot_deg)
            c, s = np.cos(rad), np.sin(rad)
            for cx_s, cy_s in src_corners:
                cx_r = scale * c * cx_s - scale * s * cy_s + tx
                cy_r = scale * s * cx_s + scale * c * cy_s + ty
                all_cx.append(cx_r); all_cy.append(cy_r)

    off_x = int(np.floor(min(all_cx)))
    off_y = int(np.floor(min(all_cy)))
    canvas_w = int(np.ceil(max(all_cx))) - off_x
    canvas_h = int(np.ceil(max(all_cy))) - off_y
    print(f"    Canvas: {canvas_w}×{canvas_h}  "
          f"(ref: {ref_w}×{ref_h}, offset: {off_x:+d},{off_y:+d})")

    def _make_M(idx: int) -> np.ndarray:
        """2×3 affine matrix that warps frame idx into the union canvas."""
        t = transforms[idx]
        if t is not None and idx != ref_idx:
            tx, ty, rot_deg, scale = t
            rad = np.radians(rot_deg)
            c, s = np.cos(rad), np.sin(rad)
            return np.float32([[scale * c, -scale * s, tx - off_x],
                               [scale * s,  scale * c, ty - off_y]])
        return np.float32([[1.0, 0.0, float(-off_x)],
                           [0.0, 1.0, float(-off_y)]])

    # ── 3b: Build composite for gap-fill ──────────────────────────────────────
    # Average all warped frames in raw float space.  Wherever a given output
    # frame has no data (black warpAffine border), we substitute the composite
    # pixel — which is real sky from other sessions that covered that area.
    print("  [3b] Building fill composite …")
    comp_sum   = np.zeros((canvas_h, canvas_w, 3), dtype=np.float64)
    comp_count = np.zeros((canvas_h, canvas_w),    dtype=np.int32)
    raw_count  = np.zeros((canvas_h, canvas_w),    dtype=np.int32)  # unmasked, for static_alpha
    for i, f in enumerate(files):
        if rejected[i]:
            print(f"    [{i+1:2d}/{n}]  REJECTED — skipped", flush=True)
            continue
        src_data, _ = _load_fits(f)
        warped = cv2.warpAffine(src_data.astype(np.float32), _make_M(i),
                                (canvas_w, canvas_h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        del src_data
        covered = warped.sum(axis=2) > 0
        trail   = _detect_trail_mask(warped)
        good    = covered & ~trail
        comp_sum[good]   += warped[good].astype(np.float64)
        comp_count[good] += 1
        raw_count[covered] += 1
        n_trail = int(trail[covered].sum())
        del warped
        suffix = f"  trail: {n_trail} px masked" if n_trail else ""
        print(f"    [{i+1:2d}/{n}]{suffix}", flush=True)

    composite = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    any_cov = comp_count > 0
    composite[any_cov] = (comp_sum[any_cov] /
                          comp_count[any_cov, np.newaxis]).astype(np.float32)
    del comp_sum

    # Inpaint the rare pixels that no frame ever covered (tiny corner slivers)
    never_cov = ~any_cov
    if never_cov.any():
        n_px = int(never_cov.sum())
        print(f"    Inpainting {n_px} px not covered by any frame …", flush=True)
        max_val = float(composite.max()) or 1.0
        comp8 = np.clip(composite / max_val * 255, 0, 255).astype(np.uint8)
        comp8_bgr = cv2.cvtColor(comp8, cv2.COLOR_RGB2BGR)
        filled8 = cv2.inpaint(comp8_bgr, never_cov.astype(np.uint8),
                              15, cv2.INPAINT_TELEA)
        filled_rgb = cv2.cvtColor(filled8, cv2.COLOR_BGR2RGB)
        composite[never_cov] = (filled_rgb.astype(np.float32) *
                                max_val / 255.0)[never_cov]
    del any_cov, never_cov

    # Save composite stack JPEG — mean of all aligned frames, stretched.
    # No annotations; stars are sharp, comet trail is smeared along its path.
    stack_out = os.path.join(comet_dir, "comet_stack.jpg")
    stack_bgr = _to_bgr8(_stretch(composite), OUTPUT_WIDTH)
    cv2.imwrite(stack_out, stack_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"  Composite stack → {stack_out}")

    # Build a STATIC per-pixel blend weight from coverage fraction.
    # The boundary between "real frame data" and "composite fill" shifts to a
    # different position each frame (because the coverage extent varies with
    # each frame's alignment transform), making it look like the canvas edge is
    # bouncing.  Computing alpha ONCE from the total coverage count and applying
    # the same map to every frame gives a fixed vignette that never moves.
    #
    # Gaussian blur on the coverage fraction softens the transition zone so the
    # edge fades smoothly rather than stepping from real data to composite.
    coverage_frac = raw_count.astype(np.float32) / float(max(n - n_rejected, 1))
    static_alpha_2d = cv2.GaussianBlur(coverage_frac, (0, 0), sigmaX=40.0)
    static_alpha = np.clip(static_alpha_2d, 0.0, 1.0)[:, :, np.newaxis]
    del comp_count, raw_count, coverage_frac, static_alpha_2d

    # ── 3c: Build output frames ────────────────────────────────────────────────
    print("  [3c] Building frames …")
    active_metas_3c  = [m for m, r in zip(metas, rejected) if not r]
    stars_durations  = (_compute_frame_durations(active_metas_3c, FPS, MAX_GAP_MULT)
                        if use_vfr else None)
    stars_frames = []
    for i, (f, meta) in enumerate(zip(files, metas)):
        src_data, _ = _load_fits(f)
        warped = cv2.warpAffine(src_data.astype(np.float32), _make_M(i),
                                (canvas_w, canvas_h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        del src_data

        # Blend with composite so canvas-edge vignette is stable every frame.
        covered_mask = warped.sum(axis=2) > 0
        blended  = warped * static_alpha + composite * (1.0 - static_alpha)
        stretched = _stretch(blended, stat_mask=covered_mask)
        bgr       = _apply_noise(_to_bgr8(stretched, OUTPUT_WIDTH), NOISE_LEVEL)

        dt   = meta["date_obs"][:16].replace("T", "  ")
        nsub = meta["nsubs"]
        lbl  = f"{dt} UTC   ({nsub} sub{'s' if nsub != 1 else ''} x {meta['exptime']:.0f}s)"
        bgr  = _overlay_label(bgr, lbl)

        # Frame review JPEG — always written (including rejected frames so the
        # strip stays complete and the user can see what they rejected).
        frame_bgr = bgr.copy()
        pos = nucleus_pos[i]
        if pos:
            out_h, out_w = frame_bgr.shape[:2]
            scale_x = out_w / canvas_w
            scale_y = out_h / canvas_h
            px = int((pos[0] - off_x) * scale_x)
            py = int((pos[1] - off_y) * scale_y)
            _draw_nucleus_marker(frame_bgr, px, py)
        cv2.imwrite(
            os.path.join(frames_dir, f"frame_{i:04d}.jpg"),
            frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85],
        )

        if rejected[i]:
            print(f"    [{i+1:2d}/{n}] {meta['date_obs'][:16]}  REJECTED", flush=True)
            continue

        stars_frames.append(bgr)
        print(f"    [{i+1:2d}/{n}] {meta['date_obs'][:16]}", flush=True)

    date_range   = (f"{active_metas_3c[0]['date_obs'][:10]}  →  "
                    f"{active_metas_3c[-1]['date_obs'][:10]}")
    comet_name   = Path(comet_dir).name
    stars_frames, stars_durations = _prepend_title(
        stars_frames, stars_durations, comet_name, "Stars Fixed", date_range)
    _write_video(stars_frames, stars_out, durations=stars_durations)
    vfr_note = f"  (VFR, gap cap {MAX_GAP_MULT}×)" if stars_durations else ""
    print(f"  Stars-fixed → {stars_out}{vfr_note}")
    print(f"  Annotated frames → {frames_dir}/  ({n} files, {n_rejected} rejected)")

    # ── Pass 4: Nucleus-fixed animation ───────────────────────────────────────
    print("\n[Pass 4] Building nucleus-fixed animation …")
    valid_pos = [(i, p) for i, p in enumerate(nucleus_pos) if p is not None]
    if not valid_pos:
        print("  No nucleus positions found — skipping comet-fixed animation.")
    else:
        # Reference nucleus position: use the reference frame's nucleus
        ref_pos = nucleus_pos[ref_idx] or valid_pos[0][1]
        ref_nx, ref_ny = ref_pos

        nucleus_frames = []
        nucleus_metas  = []   # metas for frames that make it into the animation
        stack_sum    = np.zeros((ref_h, ref_w, 3), dtype=np.float64)
        stack_count  = np.zeros((ref_h, ref_w),    dtype=np.int32)
        stack_frames = 0      # number of frames that contributed any pixels
        for i, (f, meta) in enumerate(zip(files, metas)):
            if rejected[i]:
                print(f"    [{i+1:2d}/{n}] {meta['date_obs'][:16]}  REJECTED — skipped",
                      flush=True)
                continue
            pos = nucleus_pos[i]
            if pos is None:
                print(f"    [{i+1:2d}/{n}] no nucleus — skipping", flush=True)
                continue

            src_data, _ = _load_fits(f)

            # Translation to put nucleus at ref position, then star-align transform
            nx, ny = pos
            # Compose: first star-align, then translate so comet goes to ref_pos
            t = transforms[i]
            if t is not None and i != ref_idx:
                tx, ty, rot_deg, scale = t
                rad = np.radians(rot_deg)
                c, s = np.cos(rad), np.sin(rad)
                # Combined: star align + nucleus centering translation
                dtx = ref_nx - nx
                dty = ref_ny - ny
                M = np.float32([[scale*c, -scale*s, tx + dtx],
                                [scale*s,  scale*c, ty + dty]])
            else:
                dtx = ref_nx - nx
                dty = ref_ny - ny
                M = np.float32([[1, 0, dtx],
                                [0, 1, dty]])

            aligned_rgb = cv2.warpAffine(
                src_data.astype(np.float32), M, (ref_w, ref_h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=0)

            # Accumulate nucleus-aligned full frame for deep stack
            # Exclude satellite/aircraft trail pixels via per-pixel count array.
            trail   = _detect_trail_mask(aligned_rgb)
            covered = aligned_rgb.sum(axis=2) > 0
            good    = covered & ~trail
            stack_sum[good]   += aligned_rgb[good].astype(np.float64)
            stack_count[good] += 1
            stack_frames += 1
            n_trail = int(trail[covered].sum())

            # Crop to nucleus neighbourhood
            cx, cy = int(ref_nx), int(ref_ny)
            half   = NUCLEUS_CROP_PX // 2
            x1, y1 = max(0, cx - half), max(0, cy - half)
            x2, y2 = min(ref_w, cx + half), min(ref_h, cy + half)
            crop   = aligned_rgb[y1:y2, x1:x2]

            stretched = _stretch(crop)
            crop_w    = min(OUTPUT_WIDTH, NUCLEUS_CROP_PX)
            bgr       = _apply_noise(_to_bgr8(stretched, crop_w, sharpen=True), NOISE_LEVEL)

            dt   = meta["date_obs"][:16].replace("T", "  ")
            nsub = meta["nsubs"]
            lbl  = f"{dt} UTC   ({nsub} sub{'s' if nsub != 1 else ''} x {meta['exptime']:.0f}s)"
            bgr  = _overlay_label(bgr, lbl)
            nucleus_frames.append(bgr)
            nucleus_metas.append(meta)
            trail_note = f"  trail: {n_trail} px masked" if n_trail else ""
            print(f"    [{i+1:2d}/{n}] {meta['date_obs'][:16]}{trail_note}", flush=True)

        nucleus_durations = _compute_frame_durations(nucleus_metas, FPS, MAX_GAP_MULT) if use_vfr else None
        nuc_date_range    = (f"{nucleus_metas[0]['date_obs'][:10]}  →  "
                             f"{nucleus_metas[-1]['date_obs'][:10]}")
        nucleus_frames, nucleus_durations = _prepend_title(
            nucleus_frames, nucleus_durations, comet_name, "Nucleus Fixed", nuc_date_range)
        _write_video(nucleus_frames, nucleus_out, durations=nucleus_durations)
        vfr_note = f"  (VFR, gap cap {MAX_GAP_MULT}×)" if nucleus_durations else ""
        print(f"  Nucleus-fixed → {nucleus_out}{vfr_note}")

        # Save nucleus-aligned mean stack
        nucleus_stack_out = os.path.join(comet_dir, "comet_nucleus_stack.jpg")
        if stack_frames > 0:
            # Per-pixel mean: divide only where at least one frame contributed.
            any_cov    = stack_count > 0
            mean_stack = np.zeros((ref_h, ref_w, 3), dtype=np.float32)
            mean_stack[any_cov] = (
                stack_sum[any_cov] / stack_count[any_cov, np.newaxis]
            ).astype(np.float32)
            # Sky-subtract using only covered (non-black) pixels so that the
            # large zero-fill borders from BORDER_CONSTANT shifts don't push
            # the percentile estimate to zero and leave the sky level gray.
            if any_cov.any():
                for c in range(3):
                    ch  = mean_stack[..., c]
                    sky = np.percentile(ch[any_cov], STRETCH_SKY_PCT)
                    mean_stack[..., c] = np.where(any_cov, np.clip(ch - sky, 0, None), 0.0)
            stack_bgr  = _to_bgr8(_stretch(mean_stack, sky_pct=0.0), OUTPUT_WIDTH)
            cv2.imwrite(nucleus_stack_out, stack_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            print(f"  Nucleus stack  → {nucleus_stack_out}  ({stack_frames} frames)")

            # Larson-Sekanina filter on full-res float stack
            ls_out = os.path.join(comet_dir, "comet_ls.jpg")
            stack_rgb = mean_stack.copy()   # float32, sky-subtracted
            ls_rgb = _larson_sekanina(stack_rgb, float(ref_nx), float(ref_ny))
            ls_bgr = _to_bgr8(ls_rgb, OUTPUT_WIDTH)
            cv2.imwrite(ls_out, ls_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            print(f"  Larson-Sekanina → {ls_out}")

            # Comet portrait
            portrait_out = os.path.join(comet_dir, "comet_portrait.jpg")
            stack_for_portrait = mean_stack.copy()
            portrait_bgr = _comet_portrait(
                stack_for_portrait, float(ref_nx), float(ref_ny),
                comet_name, nuc_date_range, OUTPUT_WIDTH)
            cv2.imwrite(portrait_out, portrait_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            print(f"  Portrait        → {portrait_out}")

    # ── Pass 5: Track composite ────────────────────────────────────────────────
    print("\n[Pass 5] Building comet track composite …")
    ref_data, _ = _load_fits(files[ref_idx])
    ref_stretched = _stretch(ref_data.astype(np.float32))
    track_bgr     = _to_bgr8(ref_stretched, OUTPUT_WIDTH)
    scale_x = OUTPUT_WIDTH / ref_w
    scale_y = track_bgr.shape[0] / ref_h

    prev_pt = None
    for i, pos in enumerate(nucleus_pos):
        if pos is None or rejected[i]:
            continue
        px = int(pos[0] * scale_x)
        py = int(pos[1] * scale_y)
        color = (
            int(255 * i / max(n - 1, 1)),
            100,
            int(255 * (1 - i / max(n - 1, 1))),
        )
        if prev_pt:
            cv2.line(track_bgr, prev_pt, (px, py), color, 2)
        cv2.circle(track_bgr, (px, py), 5, color, -1)
        dt_lbl = metas[i]["date_obs"][5:10]   # "MM-DD"
        cv2.putText(track_bgr, dt_lbl, (px + 7, py + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)
        prev_pt = (px, py)

    cv2.imwrite(track_out, track_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"  Track composite → {track_out}")

    print("\n" + "─" * 60)
    print("Done.")
    print(f"  Stars-fixed  : {stars_out}")
    print(f"  Nucleus-fixed: {nucleus_out}")
    print(f"  Track map    : {track_out}")
    print("─" * 60)


if __name__ == "__main__":
    main()
