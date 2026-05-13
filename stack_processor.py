"""
Seestar Lab — FITS sub-frame stacking engine (v2).

Pipeline:
  1. SSD copy          — all source frames copied to a local temp dir before any
                         reads begin.  Both passes work entirely from local storage;
                         the source drive (spinning or network) is never touched again.
  2. Sharpness scan    — light sequential pass on the temp copies: read raw Bayer,
                         compute Laplacian variance, no debayer.
  3. Frame selection   — reject below 40 % of median sharpness; keep top max_frames
                         by score.  Files re-sorted to original on-disk order so
                         pass 2 is as sequential as possible.
  4. Registration pass — heavy pass on accepted files only: read FITS, debayer,
                         normalise sky background to reference, align (astroalign →
                         ECC → phase-correlation), measure SEP frame metrics.
  4. Integration       — weighted sigma-clipped mean (chunked, memory-efficient).
  5. Upsample          — 2× Lanczos-4 to match Seestar output resolution.
  6. Background        — 2D polynomial gradient subtraction (16×16 grid).
  7. Crop              — trim invalid border pixels from alignment warps.
  8. Colour calibration— background neutralise + star white-balance (SEP).
  9. Stretch           — PixInsight-style MTF auto-stretch per channel.
 10. Enhancement       — NLM denoising + unsharp-mask sharpening.
 11. Save              — linear float32 FITS (if astropy present) + JPEG.

Drive I/O strategy
------------------
All source files are copied to a local temp dir (tempfile.mkdtemp) before either
pass begins.  The source drive (spinning or network mount) is read exactly once,
sequentially, during the copy.  Every subsequent operation — quality scan, frame
selection, alignment, integration — reads from local SSD.

The copy covers all frames so the quality scan is also fast; previously the scan
ran directly on the source drive which caused a seek per file on spinning media.
Only the top-max_frames selected files are loaded in pass 2, so the unselected
temp copies are never read again (they are cleaned up in the finally block).
"""

import os
import re
import shutil
import tempfile
import warnings
import time
import numpy as np
import cv2
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Optional


# ── Constants ──────────────────────────────────────────────────────────────────

FITS_EXT          = {'.fit', '.fits', '.fts'}
MIN_FRAMES        = 3      # refuse to stack fewer than this many accepted frames
QUALITY_THRESHOLD = 0.40   # reject frames below this fraction of median sharpness
DEFAULT_MAX_FRAMES = 500   # keep only this many best frames (quality-ranked)
DRIZZLE_SCALE     = 2      # Lanczos upsample factor (matches Seestar's output size)


class StackCancelled(RuntimeError):
    """Raised when a cancel callback signals the job should stop."""


# ── Minimal FITS reader ────────────────────────────────────────────────────────

def _parse_fits_header(raw: bytes) -> dict:
    """Parse raw FITS header bytes into a plain dict of str→str."""
    header: dict[str, str] = {}
    for i in range(0, len(raw), 80):
        card = raw[i:i + 80].decode('ascii', errors='replace')
        key  = card[:8].strip()
        if key == 'END':
            break
        if len(card) > 9 and card[8] == '=':
            raw_val = card[10:].split('/', 1)[0].strip()
            header[key] = raw_val.strip("'").strip()
    return header


def _read_fits(path: str) -> tuple[np.ndarray, dict]:
    """
    Read a single-HDU FITS file.
    Returns (uint16 array shaped (height, width), header dict).
    Supports BITPIX 16, 32, −32, −64.
    """
    with open(path, 'rb') as f:
        raw_header = b''
        found_end  = False
        while not found_end:
            block = f.read(2880)
            if not block:
                break
            raw_header += block
            for i in range(0, len(block), 80):
                if block[i:i + 3] == b'END':
                    found_end = True
                    break

        header = _parse_fits_header(raw_header)
        naxis1 = int(header.get('NAXIS1', 0))
        naxis2 = int(header.get('NAXIS2', 0))
        bitpix = int(header.get('BITPIX', 16))
        bzero  = float(header.get('BZERO',  0))
        bscale = float(header.get('BSCALE', 1))

        n_bytes = naxis1 * naxis2 * abs(bitpix) // 8
        raw     = f.read(n_bytes)

    dtype_map = {16: '>i2', 32: '>i4', -32: '>f4', -64: '>f8'}
    dtype = dtype_map.get(bitpix)
    if dtype is None:
        raise ValueError(f"Unsupported FITS BITPIX={bitpix} in {path}")

    arr      = np.frombuffer(raw, dtype=dtype).reshape(naxis2, naxis1).astype(np.float32)
    physical = arr * bscale + bzero
    return np.clip(physical, 0, 65535).astype(np.uint16), header


# ── Quality assessment ─────────────────────────────────────────────────────────

def _sharpness(raw_bayer: np.ndarray) -> float:
    """Laplacian-variance sharpness on the centre quarter of a Bayer frame."""
    h, w   = raw_bayer.shape
    cy, cx = h // 2, w // 2
    qh, qw = h // 4, w // 4
    crop   = raw_bayer[cy - qh: cy + qh, cx - qw: cx + qw]
    lap    = cv2.Laplacian(crop.astype(np.float32), cv2.CV_32F)
    return float(lap.var())


# ── Debayer ───────────────────────────────────────────────────────────────────

_BAYER_CODES = {
    'RGGB': cv2.COLOR_BayerRG2BGR,
    'BGGR': cv2.COLOR_BayerBG2BGR,
    'GRBG': cv2.COLOR_BayerGR2BGR,
    'GBRG': cv2.COLOR_BayerGB2BGR,
}


def _debayer(raw: np.ndarray, bayer_pattern: str = 'GRBG') -> np.ndarray:
    """Debayer uint16 Bayer array to BGR uint16 (bilinear, full 16-bit precision)."""
    code = _BAYER_CODES.get(bayer_pattern.upper().strip(), cv2.COLOR_BayerGR2BGR)
    return cv2.cvtColor(raw, code)


# ── Background measurement ────────────────────────────────────────────────────

def _sky_background(bgr_f32: np.ndarray) -> float:
    """10th-percentile of green channel (non-zero pixels) as sky background proxy."""
    green   = bgr_f32[:, :, 1].ravel()
    nonzero = green[green > 0.001]
    if nonzero.size == 0:
        return float(np.percentile(green, 10))
    return float(np.percentile(nonzero, 10))


# ── Registration ──────────────────────────────────────────────────────────────

def _to_gray8(bgr_f32: np.ndarray) -> np.ndarray:
    """Float32 BGR [0,1] → uint8 grayscale with CLAHE contrast boost for registration."""
    gray = (0.299 * bgr_f32[:, :, 2]
          + 0.587 * bgr_f32[:, :, 1]
          + 0.114 * bgr_f32[:, :, 0])
    lo, hi = float(np.percentile(gray, 0.5)), float(np.percentile(gray, 99.5))
    if hi > lo:
        gray = np.clip((gray - lo) / (hi - lo), 0.0, 1.0)
    gray8 = (gray * 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray8)


def _lum_for_registration(lum: np.ndarray) -> np.ndarray:
    """
    Background-subtract luminance before passing to astroalign.
    Removes extended nebula/galaxy glow that overwhelms star detection.
    The original frame data is never modified — this copy is only for
    deriving the alignment transform.
    """
    try:
        import sep
        lum64 = np.ascontiguousarray(lum.astype(np.float64))
        bkg   = sep.Background(lum64, bw=64, bh=64, fw=3, fh=3)
        return np.clip(lum64 - bkg.back(), 0.0, None).astype(np.float32)
    except Exception:
        return lum


def _register(ref_gray8: np.ndarray, frame_gray8: np.ndarray,
              ref_lum: np.ndarray | None = None,
              frame_lum: np.ndarray | None = None) -> np.ndarray | None:
    """
    2×3 affine warp (WARP_INVERSE_MAP) aligning frame to reference.

    astroalign (star-pattern matching) → ECC → phase-correlation.

    astroalign handles large inter-session shifts and rotations; its result
    is accepted if it passes guardrails:
      - matrix is finite (no NaN/Inf)
      - scale determinant 0.7–1.4 (non-degenerate; same optics → scale ≈ 1)
      - shift ≤ 60 % of frame dimension (larger = different field, not misaligned)
      - rotation ≤ 45° (larger = camera inverted between sessions, reject)
    ECC and phase-correlation only work for small offsets; their limits are
    kept tight (80 px, 5°) since they fail silently outside that range.
    """
    h, w = ref_gray8.shape[:2]

    # ── astroalign: star-triangle matching ────────────────────────────────────
    try:
        import astroalign as aa
        src_ref   = ref_lum   if ref_lum   is not None else ref_gray8.astype(np.float32) / 255.0
        src_frame = frame_lum if frame_lum is not None else frame_gray8.astype(np.float32) / 255.0
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            _, tf = aa.find_transform(src_frame, src_ref, detection_sigma=3)
        p = tf.params
        warp_aa = np.array([[p[0,0], p[0,1], p[0,2]],
                             [p[1,0], p[1,1], p[1,2]]], dtype=np.float32)
        det = abs(float(warp_aa[0,0] * warp_aa[1,1] - warp_aa[0,1] * warp_aa[1,0]))
        dx  = abs(float(warp_aa[0, 2]))
        dy  = abs(float(warp_aa[1, 2]))
        rot = abs(float(np.degrees(np.arctan2(warp_aa[1, 0], warp_aa[0, 0]))))
        if (np.isfinite(warp_aa).all()
                and 0.7 <= det <= 1.4
                and dx  <= w * 0.6
                and dy  <= h * 0.6
                and rot <= 90.0):
            return warp_aa
    except Exception:
        pass

    # ── ECC: pixel-based, small offsets only ──────────────────────────────────
    warp     = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    try:
        _, warp = cv2.findTransformECC(
            ref_gray8, frame_gray8, warp,
            cv2.MOTION_EUCLIDEAN, criteria,
            inputMask=None, gaussFiltSize=5,
        )
        dx  = abs(float(warp[0, 2]))
        dy  = abs(float(warp[1, 2]))
        rot = abs(float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))))
        if dx <= 80 and dy <= 80 and rot <= 5:
            return warp
    except cv2.error:
        pass

    # ── Phase correlation: translation only, last resort ─────────────────────
    try:
        (dx, dy), _ = cv2.phaseCorrelate(
            ref_gray8.astype(np.float32),
            frame_gray8.astype(np.float32),
        )
        if abs(dx) <= 80 and abs(dy) <= 80:
            fallback       = np.eye(2, 3, dtype=np.float32)
            fallback[0, 2] = float(dx)
            fallback[1, 2] = float(dy)
            return fallback
    except Exception:
        pass

    return None


def _fix_hot_pixels(bgr: np.ndarray, sigma: float = 8.0) -> np.ndarray:
    """
    Replace isolated hot pixels with their 3×3 neighbourhood median.
    A pixel is "hot" if it exceeds the local median by more than sigma × MAD.
    Applied per-frame before alignment so hot pixels don't survive sigma-clip.
    """
    result = bgr.copy()
    for c in range(3):
        ch  = bgr[:, :, c]
        # 3×3 median as local background estimate (fast via medianBlur on uint16)
        ch16  = np.clip(ch * 65535, 0, 65535).astype(np.uint16)
        med16 = cv2.medianBlur(ch16, 3)
        med   = med16.astype(np.float32) / 65535.0
        diff  = ch - med
        noise = float(np.median(np.abs(diff))) * 1.4826
        if noise > 0:
            hot = diff > sigma * noise
            result[:, :, c][hot] = med[hot]
    return result


# ── Frame quality metrics (SEP-based) ─────────────────────────────────────────

_METRICS_FALLBACK = {'fwhm': 3.0, 'eccentricity': 0.3, 'star_count': 0, 'snr': 0.0}


def _frame_metrics(bgr_f32: np.ndarray) -> dict:
    """
    SEP-based FWHM, eccentricity, star count, and SNR.
    Used for both Stage B quality ranking and per-frame stacking weights.
    Falls back to neutral values if SEP is unavailable or finds too few stars.
    """
    try:
        import sep
        green = np.ascontiguousarray(bgr_f32[:, :, 1].astype(np.float64))
        bkg   = sep.Background(green)
        data  = green - bkg.back()
        objs  = sep.extract(data, thresh=5.0, err=bkg.globalrms, minarea=9)

        if len(objs) < 5:
            raise RuntimeError("too few stars")

        mask = (objs['flag'] == 0) & (objs['a'] > 0.5) & (objs['b'] > 0.5)
        obj  = objs[mask] if mask.sum() >= 5 else objs

        fwhm = 2.355 * np.sqrt(obj['a'] * obj['b'])
        ecc  = np.sqrt(np.maximum(1.0 - (obj['b'] / np.maximum(obj['a'], 1e-6))**2, 0.0))
        sky_noise = float(bkg.globalrms) if bkg.globalrms > 0 else 1.0
        snr = float(np.median(obj['flux'])) / sky_noise
        return {
            'fwhm':         float(np.median(fwhm)),
            'eccentricity': float(np.median(ecc)),
            'star_count':   int(len(obj)),
            'snr':          max(snr, 0.0),
        }
    except Exception:
        return dict(_METRICS_FALLBACK)


def _quality_score(m: dict) -> float:
    """Stage B ranking score — higher is better; 0 if no stars detected."""
    stars = m.get('star_count', 0)
    if stars < 5:
        return 0.0
    fwhm = max(m.get('fwhm', 999.0), 0.5)
    snr  = max(m.get('snr',  0.0),   0.0)
    return (stars * snr) / fwhm


def _compute_weight(metrics: dict) -> float:
    fwhm  = max(metrics.get('fwhm', 3.0), 0.5)
    ecc   = max(metrics.get('eccentricity', 0.3), 0.0)
    stars = max(metrics.get('star_count', 1), 1)
    snr   = max(metrics.get('snr', 1.0), 0.1)
    return (stars * snr) / (fwhm**2 * (1.0 + ecc))


# ── Weighted sigma-clipped integration ────────────────────────────────────────

def _weighted_sigma_clip(
    stack: np.ndarray,
    weights: np.ndarray,
    sigma_low: float  = 2.0,
    sigma_high: float = 3.0,
    n_iter: int       = 3,
    chunk_rows: int   = 64,
) -> np.ndarray:
    """
    Weighted sigma-clipped mean, chunked over rows to bound peak RAM.
    stack: float16 or float32 (N, H, W, 3);  weights: float32 (N,).
    Each chunk is upcast to float32 before arithmetic. Returns float32 (H, W, 3).
    """
    N, H, W, C = stack.shape
    w  = (weights / weights.sum()).astype(np.float32)
    wc = w[:, np.newaxis, np.newaxis, np.newaxis]
    result = np.empty((H, W, C), dtype=np.float32)

    for r0 in range(0, H, chunk_rows):
        r1    = min(r0 + chunk_rows, H)
        chunk = stack[:, r0:r1, :, :].astype(np.float32)  # upcast float16 → float32

        mu = (chunk * wc).sum(axis=0)

        for _ in range(n_iter):
            diff  = chunk - mu[np.newaxis]
            wvar  = ((diff ** 2) * wc).sum(axis=0)
            sigma = np.sqrt(np.maximum(wvar, 1e-12))

            valid = (chunk >= mu[np.newaxis] - sigma_low  * sigma[np.newaxis]) & \
                    (chunk <= mu[np.newaxis] + sigma_high * sigma[np.newaxis])
            w_sel = np.where(valid, wc, 0.0)
            w_sum = w_sel.sum(axis=0)
            new_mu = (chunk * w_sel).sum(axis=0) / np.maximum(w_sum, 1e-12)
            mu = np.where(w_sum < 1e-12, mu, new_mu)

        result[r0:r1] = mu

    return result


# ── Background subtraction ────────────────────────────────────────────────────

def _subtract_background(img: np.ndarray, grid: int = 16) -> np.ndarray:
    """
    SEP sigma-clipped mesh background subtraction.

    SEP iteratively rejects bright pixels within each cell (sigma-clipping),
    making it robust against extended nebulosity or galaxies that fill a large
    fraction of the frame — unlike a percentile-of-cell approach which treats
    galaxy signal as part of the background.

    Falls back to a simple per-channel median subtraction if SEP is unavailable.
    """
    h, w   = img.shape[:2]
    result = img.copy()

    try:
        import sep
        # Box size: ~1/20 of the frame so the mesh has ~400 cells but each
        # cell is large enough to contain sky even in galaxy-dominated fields.
        bw = max(w // 20, 32)
        bh = max(h // 20, 32)
        for c in range(3):
            data = np.ascontiguousarray(img[:, :, c].astype(np.float64))
            bkg  = sep.Background(data, bw=bw, bh=bh, fw=3, fh=3)
            result[:, :, c] = (data - bkg.back()).astype(np.float32)
        return result
    except Exception:
        pass

    # Fallback: subtract per-channel median (removes pedestal, no gradient fix)
    for c in range(3):
        ch = img[:, :, c]
        result[:, :, c] = ch - float(np.median(ch[ch > 0])) if (ch > 0).any() else ch

    return result


# ── Colour calibration ────────────────────────────────────────────────────────

def _color_calibrate(img: np.ndarray) -> np.ndarray:
    """Background neutralisation + SEP aperture-photometry star white-balance."""
    result = img.copy()

    # Background neutralisation
    bg = np.array([
        float(np.percentile(result[:, :, c][result[:, :, c] > 0], 5))
        for c in range(3)
    ])
    bg_mean = bg.mean()
    for c in range(3):
        if bg[c] > 0:
            result[:, :, c] *= bg_mean / bg[c]

    # Star white-balance
    try:
        import sep
        lum = np.ascontiguousarray(
            (0.299 * result[:, :, 2]
           + 0.587 * result[:, :, 1]
           + 0.114 * result[:, :, 0]).astype(np.float64)
        )
        bkg  = sep.Background(lum)
        data = (lum - bkg.back()).astype(np.float64)
        objs = sep.extract(data, thresh=10.0, err=bkg.globalrms, minarea=9)

        if len(objs) >= 10:
            mask = (objs['flag'] == 0) & (objs['a'] < 5.0)
            obj  = objs[mask]
            if len(obj) > 200:
                obj = obj[np.argsort(obj['flux'])[::-1][:200]]

            r_ratios, b_ratios = [], []
            for o in obj:
                fluxes = []
                for ci in range(3):
                    ch      = np.ascontiguousarray(result[:, :, ci].astype(np.float64))
                    bk      = sep.Background(ch)
                    f, _, _ = sep.sum_circle(ch - bk.back(), [o['x']], [o['y']], 3.0)
                    fluxes.append(float(f[0]))
                b_val, g_val, r_val = fluxes
                if g_val > 0:
                    r_ratios.append(r_val / g_val)
                    b_ratios.append(b_val / g_val)

            if len(r_ratios) >= 5:
                r_med = float(np.median(r_ratios))
                b_med = float(np.median(b_ratios))
                if r_med > 0:
                    result[:, :, 2] /= r_med
                if b_med > 0:
                    result[:, :, 0] /= b_med
    except Exception:
        pass

    return np.clip(result, 0.0, None)


def _scnr_green(img: np.ndarray) -> np.ndarray:
    """
    Maximum-neutral Subtractive Chromatic Noise Reduction for the green channel.
    OSC Bayer sensors have 2× as many green photosites as red or blue, so the
    integrated stack always has excess green noise.  This clips the green channel
    to max(R, B) wherever it exceeds that value — the standard Siril SCNR step.
    """
    result = img.copy()
    result[:, :, 1] = np.minimum(img[:, :, 1],
                                  np.maximum(img[:, :, 2], img[:, :, 0]))
    return result


# ── Crop ──────────────────────────────────────────────────────────────────────

def _auto_crop(img: np.ndarray, valid_mask: np.ndarray, margin: int = 12) -> np.ndarray:
    rows = np.where(np.any(valid_mask, axis=1))[0]
    cols = np.where(np.any(valid_mask, axis=0))[0]
    if not rows.size or not cols.size:
        return img
    r0 = min(int(rows[0])  + margin, img.shape[0] - 1)
    r1 = max(int(rows[-1]) - margin, 0)
    c0 = min(int(cols[0])  + margin, img.shape[1] - 1)
    c1 = max(int(cols[-1]) - margin, 0)
    if r1 <= r0 or c1 <= c0:
        return img
    return img[r0:r1, c0:c1]


# ── Stretch ───────────────────────────────────────────────────────────────────

_STF_MIDTONE_TARGET = 0.12


def _auto_stretch(img: np.ndarray, Q: float = 8.0) -> np.ndarray:
    """
    Per-channel asinh stretch for preview JPEGs.

    Black point = per-channel median (sky background for sky-dominated images).
    arcsinh(x·Q) / arcsinh(Q) is linear near zero so noise excursions just
    above the sky median stay dark, while bright galaxy/star signal is
    compressed logarithmically.  Power-law (gamma) stretches tiny noise
    spikes above sky into visible gray; asinh does not.
    Q controls aggressiveness: 5 = gentle, 8 = moderate, 15 = aggressive.
    """
    result = np.zeros_like(img, dtype=np.float32)
    denom  = float(np.arcsinh(Q))
    for c in range(3):
        ch   = img[:, :, c]
        lo   = float(np.median(ch))            # sky background → black
        hi   = float(np.percentile(ch, 99.9))  # bright stars → white
        span = max(hi - lo, 1e-10)
        linear = np.clip((ch - lo) / span, 0.0, 1.0)
        result[:, :, c] = np.arcsinh(linear * Q) / denom

    return result


# ── Siril CLI post-processing ─────────────────────────────────────────────────

SIRIL_CLI          = "/mnt/c/Program Files/Siril/bin/siril-cli.exe"
SIRIL_WIN_WORK_BASE = "/mnt/c/Temp"   # Windows-accessible temp root for Siril jobs


def _siril_postprocess(fits_path: str, jpeg_path: str,
                       progress_cb: Optional[Callable] = None) -> bool:
    """
    Call the Windows Siril CLI to produce a finished JPEG from a linear FITS.

    Pipeline: autostretch → save JPEG.  Background extraction and GraXpert
    denoising are applied upstream (in _siril_full_stack) on the linear FITS
    before this function is called.  Falls back silently to our own preview
    pipeline if Siril is not installed or the script fails.

    Returns True if Siril succeeded, False if fallback is needed.
    """
    import subprocess, tempfile, shlex

    if progress_cb is None:
        progress_cb = lambda p, msg, *a: None

    if not os.path.isfile(SIRIL_CLI):
        return False

    # wslpath converts Linux paths to Windows UNC / drive paths
    def to_win(path: str) -> str:
        try:
            return subprocess.check_output(
                ["wslpath", "-w", path], text=True
            ).strip()
        except Exception:
            return path

    fits_win = to_win(fits_path)
    jpeg_win = to_win(os.path.splitext(jpeg_path)[0])  # Siril appends .jpg itself

    script = (
        'requires 1.2.0\n'
        f'load "{fits_win}"\n'
        'autostretch\n'
        f'savejpg "{jpeg_win}" 95\n'
    )

    with tempfile.NamedTemporaryFile(suffix='.ssf', mode='w',
                                     delete=False, dir='/tmp') as f:
        f.write(script)
        script_path = f.name

    script_win = to_win(script_path)

    try:
        progress_cb(0, "Siril: background extraction + calibration + stretch")
        result = subprocess.run(
            [SIRIL_CLI, "-s", script_win],
            capture_output=True, text=True, timeout=120,
        )
        os.unlink(script_path)

        if result.returncode != 0:
            import logging
            logging.warning(f"Siril exited {result.returncode}: {result.stderr[:300]}")
            return False

        # Siril writes <name>.jpg — make sure it landed where we expect
        expected = os.path.splitext(jpeg_path)[0] + '.jpg'
        if os.path.isfile(expected) and expected != jpeg_path:
            os.replace(expected, jpeg_path)

        return True

    except Exception as exc:
        import logging
        logging.warning(f"Siril post-processing failed: {exc}")
        try:
            os.unlink(script_path)
        except Exception:
            pass
        return False


# ── Siril full pipeline (register + stack + preview) ─────────────────────────

def _siril_full_stack(
    selected_files: list[str],
    bayer_pattern: str,
    output_fits: str,
    progress_cb: Optional[Callable] = None,
) -> bool:
    """
    Use Siril CLI for CFA registration + sigma-clip stacking, then debayer the
    result in Python to produce a 3-channel linear color FITS at output_fits.

    Siril pipeline (all in CFA/Bayer space):
      convert light -out=pp_light   → CFA frames indexed as light_ sequence
      register light_               → star-pattern alignment (sequence r_light_)
      stack r_light_ rej 3 3        → sigma-clip integration, additive+scale norm
                                       saves stacked.fit (1-ch float32 Bayer)

    Python post-step: read stacked.fit → debayer → sky-pedestal subtract → save
    3-channel float32 FITS to output_fits.

    JPEG generation is NOT done here; call _siril_postprocess or the GraXpert
    pipeline separately on the resulting color FITS.

    Returns True on success, False if Siril is unavailable or the run fails.
    """
    import subprocess, shutil, logging

    if not os.path.isfile(SIRIL_CLI):
        return False

    if progress_cb is None:
        progress_cb = lambda p, msg, *a: None

    def to_win(path: str) -> str:
        try:
            return subprocess.check_output(
                ["wslpath", "-w", path], text=True
            ).strip()
        except Exception:
            return path

    work_dir = os.path.join(SIRIL_WIN_WORK_BASE, f"seestar_siril_{int(time.time())}")

    try:
        os.makedirs(work_dir, exist_ok=True)

        # Debayer each selected frame in Python and write 3-channel float32 FITS.
        # Registering raw CFA Bayer frames creates systematic color cross-talk:
        # sub-pixel shifts misalign the GRBG grid, and averaged shifted Bayer
        # patterns produce the purple/green diagonal band artifact.  Pre-debayering
        # gives Siril proper RGB images so registration and stacking are colour-clean.
        n = len(selected_files)
        progress_cb(0, f"Siril: debayering and writing {n} frames to work dir…")
        try:
            from astropy.io import fits as _fits
        except ImportError:
            logging.warning("astropy not available for pre-debayer write")
            return False

        for i, src in enumerate(selected_files):
            raw, _ = _read_fits(src)
            bgr = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
            rgb = bgr[:, :, ::-1].transpose(2, 0, 1)          # (3, H, W) RGB
            hdu = _fits.PrimaryHDU(rgb.astype(np.float32))
            hdu.header['BUNIT']   = 'normalized'
            hdu.header['COLORMD'] = 'RGB'
            hdu.writeto(os.path.join(work_dir, f"light_{i:05d}.fit"), overwrite=True)
            if (i + 1) % 50 == 0 or i + 1 == n:
                progress_cb(
                    int(20 * (i + 1) / n),
                    f"Siril: debayered {i + 1}/{n} frames",
                )

        work_win = to_win(work_dir)

        # Register and stack pre-debayered RGB FITS.  Siril v1.4 uses r_ prefix.
        script = (
            f'requires 1.2.0\n'
            f'cd "{work_win}"\n'
            f'setext fit\n'
            f'convert light -out=pp_light\n'
            f'register light_\n'
            f'stack r_light_ rej 3 3 -norm=addscale -out=stacked\n'
        )

        script_path = os.path.join(work_dir, "stack.ssf")
        with open(script_path, 'w') as f:
            f.write(script)

        logging.info(f"Siril full stack: {n} frames  work={work_dir}")
        progress_cb(20, f"Siril: registering and stacking {n} frames…")

        proc = subprocess.run(
            [SIRIL_CLI, "-s", to_win(script_path)],
            capture_output=True, text=True, timeout=7200,
        )

        if proc.returncode != 0:
            logging.warning(
                f"Siril full stack exit {proc.returncode}\n"
                f"stdout: {proc.stdout[-1000:]}\n"
                f"stderr: {proc.stderr[-400:]}"
            )
            return False

        stacked_local = os.path.join(work_dir, "stacked.fit")
        if not os.path.isfile(stacked_local):
            logging.warning("Siril stack: stacked.fit not found in work dir")
            return False

        # Read stacked color FITS (already 3-channel — we pre-debayered each frame).
        progress_cb(85, "Processing stacked color image…")
        try:
            from astropy.io import fits as _fits
            with _fits.open(stacked_local) as hdul:
                data = hdul[0].data.astype(np.float32)   # (3, H, W) RGB float32

            if data.ndim == 3 and data.shape[0] == 3:
                bgr = data[::-1].transpose(1, 2, 0).copy()   # RGB→BGR, (H,W,3)
            elif data.ndim == 2:
                # Still mono — fallback debayer (shouldn't happen with pre-debayered input)
                cfa_u16 = np.clip(data * 65535.0, 0, 65535).astype(np.uint16)
                bgr = _debayer(cfa_u16, bayer_pattern).astype(np.float32) / 65535.0
            else:
                logging.warning(f"Siril stack: unexpected FITS shape {data.shape}")
                return False

            # Crop the registration border using per-row IQR of the green channel.
            # Partial-coverage border rows have higher pixel-to-pixel variance than
            # fully-stacked sky rows; galaxy rows also spike but they're in the
            # middle third so we only scan the outer third from each edge.
            # FITS row 0 = bottom of the actual image, so "top border" = high rows.
            g_fits = data[1]   # green plane in FITS orientation (row 0 = image bottom)
            H_f    = g_fits.shape[0]
            row_iqr = (np.percentile(g_fits, 75, axis=1)
                     - np.percentile(g_fits, 25, axis=1))
            mid_iqr   = float(np.median(row_iqr[H_f // 3 : 2 * H_f // 3]))
            border_thr = 1.5 * mid_iqr
            outer      = H_f // 3

            top_crop = H_f
            for i in range(H_f - 1, H_f - outer, -1):
                if row_iqr[i] <= border_thr:
                    top_crop = i + 1
                    break
            bot_crop = 0
            for i in range(0, outer):
                if row_iqr[i] <= border_thr:
                    bot_crop = i
                    break

            # Apply column crop with the same zero-based mask
            lum_cols = bgr.sum(axis=2)
            valid_cols = np.where(lum_cols.min(axis=0) > 0)[0]
            c0 = int(valid_cols[0])  if valid_cols.size else 0
            c1 = int(valid_cols[-1]) + 1 if valid_cols.size else bgr.shape[1]

            bgr = bgr[bot_crop:top_crop, c0:c1]

            # Per-channel SEP background subtraction: fits a sigma-clipped 2D mesh
            # to each channel independently, equalising sky levels across R/G/B and
            # removing vignetting gradients without treating nebula/galaxy as sky.
            bgr = _subtract_background(bgr)
            bgr = np.clip(bgr, 0.0, None)

            # GraXpert AI denoising on the linear image (before any stretch).
            # Linear data has Gaussian noise characteristics; denoising here gives
            # the model clean signal to work with rather than nonlinearly amplified
            # shadow noise.  Falls back silently if GraXpert is unavailable.
            progress_cb(97, "AI denoising (GraXpert)")
            bgr = _graxpert_denoise(bgr, strength=0.8)

            _write_fits(output_fits, bgr, Path(output_fits).stem)
        except Exception as exc:
            logging.warning(f"Siril stack: FITS-read/write failed: {exc}")
            return False

        progress_cb(100, "Siril stacking complete")
        return True

    except subprocess.TimeoutExpired:
        logging.warning("Siril full stack timed out after 2 hours")
        return False
    except Exception as exc:
        logging.warning(f"Siril full stack error: {exc}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── AI denoising (GraXpert) ───────────────────────────────────────────────────

def _graxpert_denoise(img_bgr: np.ndarray, strength: float = 0.8) -> np.ndarray:
    """
    GraXpert AI denoising on a linear float32 BGR image in [0, 1].
    Downloads the ONNX model on first call; uses CUDA if available.
    Falls back silently to the original image on any error.

    Monkey-patches graxpert.ai_model_handling.run_in_process to a no-op so
    that inference runs in-process rather than in a forked child.  GraXpert
    forks to guard against ROCm crashes, but the fork corrupts the CUDA
    context on WSL2 (CUDA is not fork-safe), causing GPU=-1 failures.
    Running in-process is safe for CUDA/TensorRT providers.
    """
    try:
        import graxpert.ai_model_handling as _gxh
        from graxpert.denoising import denoise as _gx_denoise
        from graxpert.ai_model_handling import (
            denoise_ai_models_dir, ai_model_path_from_version,
            download_version, latest_version, list_local_versions,
        )
        from graxpert.s3_secrets import denoise_bucket_name

        # Bypass the fork-based subprocess wrapper — run inference in-process
        # so the CUDA session (created in-process) stays on the same CUDA ctx.
        _orig_run_in_process = _gxh.run_in_process
        _gxh.run_in_process = lambda fn: fn()

        try:
            local = list_local_versions(denoise_ai_models_dir)
            if local:
                ai_version = sorted(local, key=lambda v: v['version'])[-1]['version']
            else:
                ai_version = latest_version(denoise_ai_models_dir, denoise_bucket_name)
                download_version(denoise_ai_models_dir, denoise_bucket_name, ai_version)

            ai_path = ai_model_path_from_version(denoise_ai_models_dir, ai_version)

            # GraXpert expects (H, W, 3) float32 RGB in [0, 1]
            rgb    = np.clip(img_bgr[:, :, ::-1], 0.0, 1.0).astype(np.float32)
            result = _gx_denoise(rgb, ai_path, strength, batch_size=8, ai_gpu_acceleration=True)
        finally:
            _gxh.run_in_process = _orig_run_in_process

        if result is None:
            return img_bgr
        return result[:, :, ::-1].astype(np.float32)   # RGB → BGR
    except Exception:
        return img_bgr


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray) -> np.ndarray:
    """
    Chroma Gaussian + unsharp-mask sharpening.  img: uint8 BGR.

    Runs on the stretch uint8 image to kill residual speckles that GraXpert
    leaves in the linear domain.  Applies moderate luma smoothing + heavy
    chroma smoothing, then a gentle unsharp mask for apparent sharpness.
    """
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycrcb)

    # Luma: gentle smoothing suppresses bright luma speckles in dark sky areas
    y_dn  = cv2.GaussianBlur(y,  (7,  7),  2)

    # Chroma: aggressive blur kills all residual coloured Bayer speckles
    cr_dn = cv2.GaussianBlur(cr, (31, 31), 10)
    cb_dn = cv2.GaussianBlur(cb, (31, 31), 10)

    denoised = cv2.cvtColor(cv2.merge([y_dn, cr_dn, cb_dn]), cv2.COLOR_YCrCb2BGR)

    # Gentle unsharp mask — just enough to restore star and core sharpness
    # without amplifying residual noise.
    blurred   = cv2.GaussianBlur(denoised, (0, 0), 1.5)
    sharpened = cv2.addWeighted(denoised, 1.4, blurred, -0.4, 0)
    return sharpened


# ── FITS writer ───────────────────────────────────────────────────────────────

def _write_fits(path: str, data: np.ndarray, object_name: str = '') -> bool:
    """Save linear float32 BGR image as 3-plane RGB FITS. Returns True on success."""
    try:
        from astropy.io import fits as _fits
        rgb = data[:, :, ::-1].transpose(2, 0, 1).astype(np.float32)
        hdu = _fits.PrimaryHDU(rgb)
        hdu.header['BUNIT']    = 'normalized'
        hdu.header['COLORMD']  = 'RGB'
        hdu.header['OBJECT']   = object_name
        hdu.header['INSTRUME'] = 'Seestar S50'
        hdu.header['CREATOR']  = 'Seestar Lab'
        hdu.writeto(path, overwrite=True)
        return True
    except Exception:
        return False


# ── Log writer ────────────────────────────────────────────────────────────────

def _write_stack_log(log_path: str, stats: dict, output_path: str) -> None:
    """Write a human-readable pipeline run log alongside the FITS output."""
    elapsed = stats.get('elapsed_s', 0.0)
    mins, secs = divmod(int(elapsed), 60)
    elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    total          = stats.get('total', 0)
    stage_a        = stats.get('stage_a_pass', 0)
    stage_b        = stats.get('stage_b_pass', 0)
    selected       = stats.get('selected', 0)
    aligned        = stats.get('aligned', 0)
    rejected_align = stats.get('rejected_align', 0)
    align_rate     = f"{100*aligned/selected:.1f}%" if selected else "n/a"

    lines = [
        f"Seestar Lab — stacking run log",
        f"  Started : {stats.get('started_utc', 'unknown')}",
        f"  Elapsed : {elapsed_str}",
        f"  Output  : {output_path}",
        f"",
        f"Frame counts",
        f"  Input total         : {total}",
        f"  Stage A pass (sharpness)   : {stage_a}  ({100*stage_a/total:.1f}%)"
            if total else f"  Stage A pass (sharpness)   : {stage_a}",
        f"  Stage B pass (SEP metrics) : {stage_b}  ({100*stage_b/total:.1f}%)"
            if total else f"  Stage B pass (SEP metrics) : {stage_b}",
        f"  Selected after cap  : {selected}  (max_frames={stats.get('max_frames', '?')})",
        f"  Alignment accepted  : {aligned}  ({align_rate} of selected)",
        f"  Alignment rejected  : {rejected_align}",
        f"",
        f"Registration",
        f"  Reference frame  : {stats.get('ref_frame', 'unknown')}",
        f"  Bayer pattern    : {stats.get('bayer_pattern', 'unknown')}",
    ]
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception:
        pass


# ── Main processor ────────────────────────────────────────────────────────────

class StackProcessor:
    """
    Full stacking pipeline for Seestar S50 FITS sub-frames.

    Usage:
        proc   = StackProcessor()
        result = proc.run(fits_files, output_path, progress_cb, cancel_cb,
                          max_frames=500)

    progress_cb(pct: int, stage: str, accepted: int, total: int)
    cancel_cb() -> bool   (return True to abort)
    max_frames: keep only this many best-quality frames; set lower to reduce
                RAM and time at the cost of marginally less integration depth.
                500 is a good default — SNR gains above that are √N-limited
                and rarely worth the processing cost.
    """

    def run(
        self,
        fits_files:  list[str],
        output_path: str,
        progress_cb: Callable[[int, str, int, int], None],
        cancel_cb:   Optional[Callable[[], bool]] = None,
        max_frames:  int = DEFAULT_MAX_FRAMES,
        use_cache:   bool = False,
    ) -> dict:

        def _chk():
            if cancel_cb and cancel_cb():
                raise StackCancelled("Stacking cancelled")

        t_start = time.monotonic()
        run_stats: dict = {
            'started_utc':    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'total':          0,
            'stage_a_pass':   0,
            'stage_b_pass':   0,
            'selected':       0,
            'aligned':        0,
            'rejected_align': 0,
            'ref_frame':      '',
            'bayer_pattern':  '',
            'max_frames':     max_frames,
            'elapsed_s':      0.0,
            'used_cache':     False,
        }

        total = len(fits_files)
        run_stats['total'] = total
        if total < MIN_FRAMES:
            raise RuntimeError(
                f"Need at least {MIN_FRAMES} FITS files to stack, found {total}"
            )

        # Persistent frame cache on local SSD — always under tempfile.gettempdir()
        # so it stays on local storage regardless of where the output FITS lives
        # (e.g. network share, external drive).  Keyed by the session directory
        # name so each session gets its own cache.
        session_key = re.sub(r'[^\w\-]', '_', Path(output_path).parent.name)
        cache_dir   = os.path.join(tempfile.gettempdir(),
                                   'seestar_cache', session_key)

        # ── Copy all source frames to local SSD temp dir ──────────────────────
        # If use_cache=True and a valid cache exists, skip the copy and read
        # directly from cache.  Otherwise copy to a temp dir; on success the
        # temp dir is promoted to cache (renamed) so future re-stacks are free.
        tmp_dir = None
        if use_cache and os.path.isdir(cache_dir):
            cached = sorted(
                f for f in os.listdir(cache_dir)
                if Path(f).suffix.lower() in FITS_EXT
            )
            if len(cached) >= MIN_FRAMES:
                fits_files = [os.path.join(cache_dir, f) for f in cached]
                run_stats['used_cache'] = True
                run_stats['total']      = len(fits_files)
                total = len(fits_files)
                progress_cb(10, f"Using {total} cached frames (skipping copy)", 0, total)

        if not run_stats['used_cache']:
            tmp_dir = tempfile.mkdtemp(prefix="seestar_stack_")
        try:
            if not run_stats['used_cache']:
                progress_cb(1, f"Copying {total} frames to local temp dir…", 0, total)
                _chk()
                local_files: list[str] = []
                for ci, src in enumerate(fits_files):
                    _chk()
                    dst = os.path.join(tmp_dir, os.path.basename(src))
                    shutil.copy2(src, dst)
                    local_files.append(dst)
                    if (ci + 1) % 50 == 0 or ci + 1 == total:
                        progress_cb(1 + int(9 * (ci + 1) / total),
                                    f"Copying: {ci + 1}/{total}", 0, total)
                fits_files = local_files

            # ════════════════════════════════════════════════════════════════
            # STAGE A — fast quality filter (Bayer only, no debayer)
            # Reads each file once; computes Laplacian-variance sharpness.
            # Rejects the worst ~40 % before the more expensive Stage B.
            # ════════════════════════════════════════════════════════════════
            progress_cb(10, f"Stage A: scanning {total} frames (Laplacian)", 0, total)
            _chk()

            sharpness:    list[float] = []
            bayer_pattern = 'GRBG'

            for i, fpath in enumerate(fits_files):
                _chk()
                try:
                    raw, hdr = _read_fits(fpath)
                    if i == 0:
                        bayer_pattern = hdr.get('BAYERPAT', 'GRBG').strip("'").strip()
                    sharpness.append(_sharpness(raw))
                except Exception:
                    sharpness.append(0.0)
                progress_cb(10 + int(12 * (i + 1) / total),
                            f"Stage A: {i + 1}/{total}", 0, total)

            positive = [s for s in sharpness if s > 0]
            if not positive:
                raise RuntimeError("Could not read any FITS frames")

            threshold   = float(np.median(positive)) * QUALITY_THRESHOLD
            stage_a_idx = [i for i, s in enumerate(sharpness) if s >= threshold]
            n_stage_a   = len(stage_a_idx)

            if n_stage_a < MIN_FRAMES:
                raise RuntimeError(
                    f"Only {n_stage_a} frames passed Stage A quality filter (need {MIN_FRAMES})"
                )
            run_stats['stage_a_pass']  = n_stage_a
            run_stats['bayer_pattern'] = bayer_pattern
            progress_cb(22, f"Stage A: {n_stage_a}/{total} frames pass "
                            f"(rejected {total - n_stage_a} below sharpness threshold)",
                        n_stage_a, total)
            _chk()

            # ════════════════════════════════════════════════════════════════
            # STAGE B — accurate quality metrics (debayer + SEP)
            # Runs only on Stage A survivors; computes FWHM, eccentricity,
            # star count, and SNR for final ranking and max_frames selection.
            # ════════════════════════════════════════════════════════════════
            progress_cb(22, f"Stage B: computing SEP metrics on {n_stage_a} survivors…",
                        n_stage_a, total)
            _chk()

            stage_b_metrics: list[dict] = []
            for bi, orig_idx in enumerate(stage_a_idx):
                _chk()
                try:
                    raw, _ = _read_fits(fits_files[orig_idx])
                    bgr    = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
                    stage_b_metrics.append(_frame_metrics(bgr))
                except Exception:
                    stage_b_metrics.append(dict(_METRICS_FALLBACK))
                progress_cb(22 + int(14 * (bi + 1) / n_stage_a),
                            f"Stage B: {bi + 1}/{n_stage_a}", n_stage_a, total)

            # Rank by combined quality score; cap to max_frames
            scored = sorted(range(n_stage_a),
                            key=lambda k: _quality_score(stage_b_metrics[k]),
                            reverse=True)
            scored = scored[:max_frames]

            # Re-sort to original on-disk order for sequential pass 2 reads
            scored.sort()
            selected_idx   = [stage_a_idx[k] for k in scored]
            selected_files = [fits_files[i]   for i in selected_idx]
            sel_metrics    = [stage_b_metrics[k] for k in scored]
            n_selected     = len(selected_files)

            if n_selected < MIN_FRAMES:
                raise RuntimeError(
                    f"Only {n_selected} frames passed Stage B quality selection (need {MIN_FRAMES})"
                )

            run_stats['stage_b_pass'] = len(stage_a_idx)
            run_stats['selected']     = n_selected

            # Reference frame: highest Stage B quality score (best FWHM + stars + SNR)
            ref_rank_idx = max(range(n_selected),
                               key=lambda k: _quality_score(sel_metrics[k]))
            ref_frame_name = os.path.basename(selected_files[ref_rank_idx])
            run_stats['ref_frame'] = ref_frame_name

            progress_cb(36, f"Selected {n_selected}/{total} frames "
                            f"(dropped {total - n_selected} total); "
                            f"reference = frame {selected_idx[ref_rank_idx] + 1}",
                        n_selected, total)
            _chk()

            # ════════════════════════════════════════════════════════════════
            # SIRIL PATH — let Siril handle registration, stacking, and preview
            #
            # Siril is a well-tested astronomical image processor.  We hand it
            # the quality-filtered frame list and it does:
            #   convert light → debayer CFA Bayer frames
            #   register      → star-pattern alignment
            #   stack rej     → sigma-clip integration with additive scaling
            #   autostretch   → preview stretch
            #   savejpg       → final JPEG
            #
            # If Siril is unavailable (SIRIL_CLI not found) or the run fails,
            # we fall through to our own Python pipeline below.
            # ════════════════════════════════════════════════════════════════
            fits_path = str(Path(output_path).with_suffix('.fits'))
            out_dir   = os.path.dirname(os.path.abspath(output_path))
            os.makedirs(out_dir, exist_ok=True)

            progress_cb(37,
                        f"Siril: register + stack ({n_selected} frames)…",
                        n_selected, total)
            siril_stack_ok = _siril_full_stack(
                selected_files, bayer_pattern, fits_path,
                progress_cb=lambda p, msg, *_a: progress_cb(
                    37 + int(p * 0.50), msg, n_selected, total
                ),
            )
            if siril_stack_ok:
                run_stats['aligned']        = n_selected
                run_stats['rejected_align'] = 0

                # Generate preview JPEG from the color FITS Siril produced.
                # Try Siril autostretch first; fall back to our own pipeline.
                progress_cb(87, "Post-processing preview…", n_selected, total)
                siril_preview_ok = _siril_postprocess(
                    fits_path, str(output_path),
                    progress_cb=lambda p, msg, *_a: progress_cb(
                        87 + int(p * 0.10), msg, n_selected, total
                    ),
                )
                if not siril_preview_ok:
                    # Fallback: GraXpert + asinh stretch
                    try:
                        from astropy.io import fits as _fits
                        with _fits.open(fits_path) as hdul:
                            d = hdul[0].data.astype(np.float32)
                        bgr_prev = d[::-1].transpose(1, 2, 0).copy() if d.ndim == 3 and d.shape[0] == 3 else d
                        bgr_prev = _graxpert_denoise(bgr_prev, strength=1.0)
                        bgr_prev = _scnr_green(bgr_prev)
                        preview  = _auto_stretch(bgr_prev)
                        preview_u8 = (preview * 255).astype(np.uint8)
                        preview_u8 = _denoise_sharpen(preview_u8)
                        cv2.imwrite(str(output_path), preview_u8,
                                    [cv2.IMWRITE_JPEG_QUALITY, 95])
                    except Exception:
                        pass

                run_stats['elapsed_s'] = round(time.monotonic() - t_start, 1)
                log_path = str(Path(output_path).with_suffix('.log'))
                _write_stack_log(log_path, run_stats, output_path)
                if tmp_dir is not None:
                    try:
                        if os.path.isdir(cache_dir):
                            shutil.rmtree(cache_dir)
                        shutil.move(tmp_dir, cache_dir)
                        tmp_dir = None
                    except Exception:
                        pass
                progress_cb(100, "Done", n_selected, total)
                return {
                    "frames_total":    total,
                    "frames_accepted": n_selected,
                    "output_path":     output_path,
                    "log_path":        log_path,
                }

            # Siril unavailable or failed — fall through to built-in pipeline
            progress_cb(37, "Siril not available; using built-in pipeline",
                        n_selected, total)

            # ════════════════════════════════════════════════════════════════
            # STEP 4 — align remaining frames (astroalign with guardrails)
            # No per-frame normalization here — normalization happens globally
            # after all frames are aligned.
            # ════════════════════════════════════════════════════════════════
            progress_cb(37, "Loading reference frame for alignment…", n_selected, total)
            ref_raw, _ = _read_fits(selected_files[ref_rank_idx])
            ref_bgr    = _debayer(ref_raw, bayer_pattern).astype(np.float32) / 65535.0
            ref_gray8  = _to_gray8(ref_bgr)
            ref_lum_raw = (0.299 * ref_bgr[:,:,2]
                         + 0.587 * ref_bgr[:,:,1]
                         + 0.114 * ref_bgr[:,:,0])
            ref_lum    = _lum_for_registration(ref_lum_raw)
            h, w = ref_bgr.shape[:2]

            # ════════════════════════════════════════════════════════════════
            # STEPS 4–8 — batch align + integrate
            #
            # Process BATCH_SIZE frames at a time so peak RAM is O(BATCH_SIZE)
            # rather than O(n_selected).  Each batch is sigma-clip integrated
            # into a float32 frame; batches are combined via weighted sum
            # (weight = accepted frame count).  Sky normalisation happens
            # per-frame against the reference-frame sky level so it works
            # correctly across batch boundaries.
            # ════════════════════════════════════════════════════════════════
            ref_sky = (float(np.percentile(ref_bgr[ref_bgr > 0], 25))
                       if (ref_bgr > 0).any() else 0.0)

            BATCH_SIZE       = 400
            n_batches        = (n_selected + BATCH_SIZE - 1) // BATCH_SIZE
            weighted_sum     = None          # float32 H×W×3 running accumulator
            n_accepted       = 0
            all_valid_native = np.ones((h, w), dtype=bool)

            for b_idx in range(n_batches):
                batch_start = b_idx * BATCH_SIZE
                batch_end   = min(batch_start + BATCH_SIZE, n_selected)
                batch_fis   = range(batch_start, batch_end)

                batch_arr     = np.zeros((len(batch_fis), h, w, 3), dtype=np.float16)
                batch_valid   = np.ones((h, w), dtype=bool)
                batch_metrics: list[dict] = []
                n_batch       = 0

                for fi in batch_fis:
                    _chk()
                    fpath = selected_files[fi]
                    pct   = 37 + int(38 * (fi + 1) / n_selected)
                    progress_cb(pct,
                                f"Aligning {fi + 1}/{n_selected} "
                                f"(batch {b_idx + 1}/{n_batches})",
                                n_selected, total)
                    try:
                        if fi == ref_rank_idx:
                            bgr   = ref_bgr.copy()
                            valid = np.ones((h, w), dtype=bool)
                        else:
                            raw, _        = _read_fits(fpath)
                            bgr           = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
                            frame_lum_raw = (0.299 * bgr[:, :, 2]
                                           + 0.587 * bgr[:, :, 1]
                                           + 0.114 * bgr[:, :, 0])
                            frame_lum     = _lum_for_registration(frame_lum_raw)
                            gray8 = _to_gray8(bgr)
                            warp  = _register(ref_gray8, gray8, ref_lum, frame_lum)
                            if warp is None:
                                continue

                            bgr   = cv2.warpAffine(
                                bgr, warp, (w, h),
                                flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0,
                            )
                            ones  = np.ones((h, w), dtype=np.float32)
                            valid = cv2.warpAffine(
                                ones, warp, (w, h),
                                flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0,
                            ) > 0.5

                        # Normalise each frame's sky to the reference sky level
                        frame_sky = (float(np.percentile(bgr[bgr > 0], 25))
                                     if (bgr > 0).any() else 0.0)
                        if frame_sky > 0 and ref_sky > 0:
                            bgr = np.clip(bgr + (ref_sky - frame_sky), 0.0, None)

                        batch_arr[n_batch] = bgr
                        n_batch += 1
                        batch_valid  &= valid
                        batch_metrics.append(sel_metrics[fi])
                    except Exception:
                        pass

                if n_batch < 1:
                    continue

                # Sigma-clip integrate this batch → one float32 frame
                progress_cb(
                    37 + int(38 * batch_end / n_selected),
                    f"Integrating batch {b_idx + 1}/{n_batches} ({n_batch} frames)",
                    n_accepted + n_batch, total,
                )
                _chk()
                raw_weights = np.array([_compute_weight(m) for m in batch_metrics],
                                       dtype=np.float32)
                if raw_weights.sum() == 0:
                    raw_weights = np.ones(n_batch, dtype=np.float32)

                batch_result = _weighted_sigma_clip(batch_arr[:n_batch], raw_weights)
                del batch_arr

                if weighted_sum is None:
                    weighted_sum = n_batch * batch_result.astype(np.float32)
                else:
                    weighted_sum += n_batch * batch_result.astype(np.float32)
                n_accepted       += n_batch
                all_valid_native &= batch_valid

            n_dropped_align = n_selected - n_accepted
            run_stats['aligned']        = n_accepted
            run_stats['rejected_align'] = n_dropped_align
            progress_cb(75,
                        f"Alignment complete: {n_accepted}/{n_selected} frames accepted "
                        f"({n_dropped_align} rejected by registration)",
                        n_accepted, total)
            _chk()

            if n_accepted < MIN_FRAMES or weighted_sum is None:
                raise RuntimeError(
                    f"Only {n_accepted}/{n_selected} frames registered successfully "
                    f"(need {MIN_FRAMES}) — check that frames overlap the reference field"
                )

            stacked = weighted_sum / n_accepted
            del weighted_sum

            # ── 2× upsample ───────────────────────────────────────────────────
            progress_cb(82, "Upsampling 2×", n_accepted, total)
            oh, ow  = h * DRIZZLE_SCALE, w * DRIZZLE_SCALE
            stacked = cv2.resize(stacked, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
            all_valid = cv2.resize(
                all_valid_native.astype(np.uint8), (ow, oh),
                interpolation=cv2.INTER_NEAREST,
            ).astype(bool)

            # ── Background subtraction ─────────────────────────────────────────
            # ── Crop ───────────────────────────────────────────────────────────
            # Background subtraction is NOT applied to the stacked image here.
            # For large extended objects (M101, M31, etc.) SEP mesh cells are
            # far smaller than the galaxy disk and would subtract signal as sky.
            # The FITS is delivered as raw linear data; Siril/PixInsight handle
            # background removal better with proper large-object masking.
            # The preview path uses its own pre-crop snapshot (preview_source).
            progress_cb(84, "Cropping to valid overlap region", n_accepted, total)
            preview_source = stacked.copy()
            stacked        = _auto_crop(stacked, all_valid)
            preview_source = _auto_crop(preview_source, all_valid)

            # ── Colour calibration ─────────────────────────────────────────────
            progress_cb(87, "Colour calibration", n_accepted, total)
            _chk()
            stacked        = _color_calibrate(stacked)
            preview_source = _color_calibrate(preview_source)

            # ════════════════════════════════════════════════════════════════
            # STEP 9 — save linear master FITS (primary output)
            # Subtract per-channel sky pedestal (global constant) so sky pixels
            # sit near 0.  This is not background-gradient removal — it's just
            # removing the DC offset so Siril/PixInsight autostretch works
            # correctly.  A scalar subtraction cannot distort the galaxy shape.
            # ════════════════════════════════════════════════════════════════
            fits_path = str(Path(output_path).with_suffix('.fits'))
            progress_cb(89, "Saving linear master FITS", n_accepted, total)
            fits_data = stacked.copy()
            for c in range(3):
                sky = float(np.percentile(fits_data[:, :, c], 5))
                fits_data[:, :, c] = np.clip(fits_data[:, :, c] - sky, 0.0, None)
            _write_fits(fits_path, fits_data, Path(output_path).stem)

            # ════════════════════════════════════════════════════════════════
            # STEP 10 — preview JPEG
            # Try Siril CLI first (background extraction + PCC + autostretch).
            # Fall back to our own GraXpert+asinh pipeline if Siril is absent.
            # ════════════════════════════════════════════════════════════════
            out_dir = os.path.dirname(os.path.abspath(output_path))
            os.makedirs(out_dir, exist_ok=True)

            progress_cb(90, "Post-processing preview (Siril)", n_accepted, total)
            _chk()
            siril_ok = _siril_postprocess(
                fits_path, str(output_path),
                progress_cb=lambda p, msg, *a: progress_cb(
                    90 + int(p * 0.09), msg, n_accepted, total
                ),
            )

            if not siril_ok:
                # Fallback: GraXpert + asinh stretch + chroma denoising
                progress_cb(90, "AI denoising (GraXpert)", n_accepted, total)
                _chk()
                preview_source = _graxpert_denoise(preview_source, strength=1.0)
                preview_source = _scnr_green(preview_source)

                progress_cb(95, "Auto-stretch (preview)", n_accepted, total)
                _chk()
                preview    = _auto_stretch(preview_source)
                preview_u8 = (preview * 255).astype(np.uint8)
                preview_u8 = _denoise_sharpen(preview_u8)

                progress_cb(97, "Saving preview JPEG", n_accepted, total)
                ok = cv2.imwrite(
                    str(output_path), preview_u8,
                    [cv2.IMWRITE_JPEG_QUALITY, 95],
                )
                if not ok:
                    raise RuntimeError(f"Failed to write preview JPEG to {output_path}")

            run_stats['elapsed_s'] = round(time.monotonic() - t_start, 1)
            log_path = str(Path(output_path).with_suffix('.log'))
            _write_stack_log(log_path, run_stats, output_path)

            # Promote temp dir → persistent cache so the next re-stack is free.
            if tmp_dir is not None:
                try:
                    if os.path.isdir(cache_dir):
                        shutil.rmtree(cache_dir)
                    shutil.move(tmp_dir, cache_dir)
                    tmp_dir = None  # transferred; don't delete in finally
                except Exception:
                    pass  # cache promotion failed — not fatal

            progress_cb(100, "Done", n_accepted, total)
            return {
                "frames_total":    total,
                "frames_accepted": n_accepted,
                "output_path":     output_path,
                "log_path":        log_path,
            }
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)


def rerender_preview(fits_path: str, jpeg_path: str,
                     progress_cb: Optional[Callable] = None) -> str:
    """
    Regenerate the preview JPEG from an already-stacked FITS file.

    Applies the full current preview pipeline (GraXpert, SCNR, asinh stretch,
    chroma denoising, unsharp mask) without re-running frame alignment.
    Useful for tuning the preview without an 80-minute re-stack.

    Returns the path of the written JPEG.
    """
    if progress_cb is None:
        progress_cb = lambda p, msg, *a: None

    progress_cb(5, "Loading FITS", 0, 0)
    try:
        from astropy.io import fits as _fits
        with _fits.open(fits_path) as hdul:
            data = hdul[0].data.astype(np.float32)
    except Exception as e:
        raise RuntimeError(f"Cannot read FITS: {e}")

    if data.ndim == 3 and data.shape[0] == 3:
        # FITS plane order is RGB → convert to BGR float32
        bgr = data[::-1].transpose(1, 2, 0).copy()
    elif data.ndim == 3 and data.shape[2] == 3:
        bgr = data[:, :, ::-1].copy()
    else:
        raise RuntimeError(f"Unexpected FITS shape: {data.shape}")

    os.makedirs(os.path.dirname(os.path.abspath(jpeg_path)), exist_ok=True)

    progress_cb(10, "Post-processing (Siril)", 0, 0)
    siril_ok = _siril_postprocess(fits_path, jpeg_path,
                                   progress_cb=lambda p, msg, *a: progress_cb(
                                       10 + int(p * 0.85), msg, 0, 0))
    if siril_ok:
        progress_cb(100, "Done", 0, 0)
        return jpeg_path

    # Fallback: our own pipeline
    progress_cb(20, "Colour calibration", 0, 0)
    bgr = _color_calibrate(bgr)

    progress_cb(40, "AI denoising (GraXpert)", 0, 0)
    bgr = _graxpert_denoise(bgr, strength=1.0)

    progress_cb(70, "SCNR green suppression", 0, 0)
    bgr = _scnr_green(bgr)

    progress_cb(80, "Auto-stretch", 0, 0)
    preview = _auto_stretch(bgr)

    progress_cb(90, "Noise reduction and sharpening", 0, 0)
    preview_u8 = (preview * 255).astype(np.uint8)
    preview_u8 = _denoise_sharpen(preview_u8)

    progress_cb(97, "Saving JPEG", 0, 0)
    ok = cv2.imwrite(jpeg_path, preview_u8, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError(f"Failed to write JPEG to {jpeg_path}")

    progress_cb(100, "Done", 0, 0)
    return jpeg_path
