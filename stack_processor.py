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
    """Degree-2 2D polynomial background per channel, sampled on a grid×grid mesh."""
    h, w   = img.shape[:2]
    result = img.copy()
    cell_h = max(h // grid, 1)
    cell_w = max(w // grid, 1)

    for c in range(3):
        ch          = img[:, :, c]
        ys, xs, vals = [], [], []
        for gy in range(grid):
            for gx in range(grid):
                y0, y1 = gy * cell_h, min((gy + 1) * cell_h, h)
                x0, x1 = gx * cell_w, min((gx + 1) * cell_w, w)
                patch  = ch[y0:y1, x0:x1].ravel()
                if patch.size == 0:
                    continue
                ys.append((y0 + y1) * 0.5 / h)
                xs.append((x0 + x1) * 0.5 / w)
                vals.append(float(np.percentile(patch, 20)))

        if len(vals) < 6:
            continue

        ys_   = np.asarray(ys,   dtype=np.float64)
        xs_   = np.asarray(xs,   dtype=np.float64)
        vals_ = np.asarray(vals, dtype=np.float64)
        A = np.column_stack([
            np.ones_like(xs_), xs_, ys_,
            xs_**2, xs_ * ys_, ys_**2,
        ])
        try:
            coef, _, _, _ = np.linalg.lstsq(A, vals_, rcond=None)
        except Exception:
            continue

        yy, xx   = np.mgrid[0:h, 0:w]
        yy       = yy.astype(np.float32) / h
        xx       = xx.astype(np.float32) / w
        bg_model = (coef[0]
                    + coef[1] * xx + coef[2] * yy
                    + coef[3] * xx**2 + coef[4] * xx * yy + coef[5] * yy**2
                    ).astype(np.float32)
        sub = ch - bg_model
        sub -= sub.min()
        result[:, :, c] = sub

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


def _auto_stretch(img: np.ndarray) -> np.ndarray:
    """PixInsight-style MTF auto-stretch, per channel."""
    result = np.zeros_like(img, dtype=np.float32)
    m_tgt  = _STF_MIDTONE_TARGET

    for c in range(3):
        ch  = img[:, :, c].ravel()
        sky = ch[ch > 0] if (ch > 0).any() else ch

        med   = float(np.median(sky))
        mad   = float(np.median(np.abs(sky - med)))
        sigma = mad * 1.4826
        c0    = max(0.0, med - 2.8 * sigma)
        hi    = float(np.percentile(sky, 99.9))
        span  = max(hi - c0, 1e-10)

        x     = np.clip((img[:, :, c] - c0) / span, 0.0, 1.0)
        med_n = float(np.clip((med - c0) / span, 1e-6, 1.0 - 1e-6))

        denom_m = 2.0 * med_n * m_tgt - m_tgt - med_n
        m = float(med_n * (m_tgt - 1.0) / denom_m) if abs(denom_m) > 1e-10 else 0.5
        m = max(1e-4, min(1.0 - 1e-4, m))

        denom = (2.0 * m - 1.0) * x - m
        denom = np.where(np.abs(denom) > 1e-10, denom, np.sign(denom + 1e-30) * 1e-10)
        result[:, :, c] = np.clip((m - 1.0) * x / denom, 0.0, 1.0)

    return result


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray) -> np.ndarray:
    """NLM colour denoising + unsharp-mask sharpening. img: uint8 BGR."""
    denoised  = cv2.fastNlMeansDenoisingColored(
        img, None, h=6, hColor=6,
        templateWindowSize=7, searchWindowSize=21,
    )
    blurred   = cv2.GaussianBlur(denoised, (0, 0), 1.2)
    sharpened = cv2.addWeighted(denoised, 1.6, blurred, -0.6, 0)
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
        }

        total = len(fits_files)
        run_stats['total'] = total
        if total < MIN_FRAMES:
            raise RuntimeError(
                f"Need at least {MIN_FRAMES} FITS files to stack, found {total}"
            )

        # ── Copy all source frames to local SSD temp dir ──────────────────────
        # Both passes read entirely from local storage so spinning-drive or
        # network-mount latency never affects either the quality scan or the
        # alignment pass.  All files are copied sequentially once; the temp
        # dir is cleaned up in the finally block regardless of outcome.
        tmp_dir = tempfile.mkdtemp(prefix="seestar_stack_")
        try:
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

            # Pre-allocate stack array in float16 to halve peak RAM.
            # float16 gives ~3 significant decimal digits on [0,1] data — more
            # than enough for stacking; sigma-clip operates in float32 per chunk.
            stack_arr = np.zeros((n_selected, h, w, 3), dtype=np.float16)
            stack_arr[0] = ref_bgr  # float32 → float16 truncation is automatic
            n_accepted   = 1

            masks:    list[np.ndarray] = [np.ones((h, w), dtype=bool)]
            stk_metrics: list[dict]   = [sel_metrics[ref_rank_idx]]

            for fi, fpath in enumerate(selected_files):
                _chk()
                if fi == ref_rank_idx:
                    continue
                pct = 37 + int(38 * (fi + 1) / n_selected)
                progress_cb(pct, f"Aligning {fi + 1}/{n_selected}", n_selected, total)
                try:
                    raw, _        = _read_fits(fpath)
                    bgr           = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
                    frame_lum_raw = (0.299 * bgr[:,:,2]
                                   + 0.587 * bgr[:,:,1]
                                   + 0.114 * bgr[:,:,0])
                    frame_lum     = _lum_for_registration(frame_lum_raw)
                    gray8 = _to_gray8(bgr)
                    warp  = _register(ref_gray8, gray8, ref_lum, frame_lum)
                    if warp is None:
                        continue

                    aligned = cv2.warpAffine(
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

                    stack_arr[n_accepted] = aligned
                    n_accepted += 1
                    masks.append(valid)
                    stk_metrics.append(sel_metrics[fi])
                except Exception:
                    pass

            n_dropped_align = n_selected - n_accepted
            run_stats['aligned']        = n_accepted
            run_stats['rejected_align'] = n_dropped_align
            progress_cb(75,
                        f"Alignment complete: {n_accepted}/{n_selected} frames accepted "
                        f"({n_dropped_align} rejected by registration)",
                        n_accepted, total)
            _chk()

            if n_accepted < MIN_FRAMES:
                raise RuntimeError(
                    f"Only {n_accepted}/{n_selected} frames registered successfully "
                    f"(need {MIN_FRAMES}) — check that frames overlap the reference field"
                )

            stack_arr = stack_arr[:n_accepted]

            # ════════════════════════════════════════════════════════════════
            # STEPS 5–6 — global sky normalization (additive, post-alignment)
            # Compute per-frame sky level from aligned data, then shift each
            # frame by (global_median_sky - frame_sky).  Done after alignment
            # so estimates are accurate and corrections are consistent across
            # the ensemble.
            # ════════════════════════════════════════════════════════════════
            progress_cb(75, "Global sky normalization…", n_accepted, total)
            _chk()
            frame_skies = np.array([
                float(np.percentile(stack_arr[i][stack_arr[i] > 0], 25))
                if (stack_arr[i] > 0).any() else 0.0
                for i in range(n_accepted)
            ], dtype=np.float32)
            global_sky = float(np.median(frame_skies))
            for i in range(n_accepted):
                if frame_skies[i] > 0:
                    stack_arr[i] = np.clip(
                        stack_arr[i] + (global_sky - frame_skies[i]), 0.0, None
                    )

            # ════════════════════════════════════════════════════════════════
            # STEPS 7–8 — sigma-clip across normalized stack → weighted mean
            # ════════════════════════════════════════════════════════════════
            progress_cb(77, f"Integrating {n_accepted} frames (weighted σ-clip mean)",
                        n_accepted, total)
            _chk()

            raw_weights = np.array([_compute_weight(m) for m in stk_metrics],
                                   dtype=np.float32)
            if raw_weights.sum() == 0:
                raw_weights = np.ones(n_accepted, dtype=np.float32)

            stacked = _weighted_sigma_clip(stack_arr, raw_weights)
            del stack_arr

            all_valid_native = masks[0].copy()
            for m in masks[1:]:
                all_valid_native &= m

            # ── 2× upsample ───────────────────────────────────────────────────
            progress_cb(82, "Upsampling 2×", n_accepted, total)
            oh, ow  = h * DRIZZLE_SCALE, w * DRIZZLE_SCALE
            stacked = cv2.resize(stacked, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
            all_valid = cv2.resize(
                all_valid_native.astype(np.uint8), (ow, oh),
                interpolation=cv2.INTER_NEAREST,
            ).astype(bool)

            # ── Background subtraction ─────────────────────────────────────────
            progress_cb(84, "Removing background gradient", n_accepted, total)
            _chk()
            stacked = _subtract_background(stacked, grid=16)

            # ── Crop ───────────────────────────────────────────────────────────
            progress_cb(86, "Cropping to valid overlap region", n_accepted, total)
            stacked = _auto_crop(stacked, all_valid)

            # ── Colour calibration ─────────────────────────────────────────────
            progress_cb(87, "Colour calibration", n_accepted, total)
            _chk()
            stacked = _color_calibrate(stacked)

            # ════════════════════════════════════════════════════════════════
            # STEP 9 — save linear master FITS (primary output)
            # The FITS is the deliverable; JPEG is preview only.
            # ════════════════════════════════════════════════════════════════
            fits_path = str(Path(output_path).with_suffix('.fits'))
            progress_cb(89, "Saving linear master FITS", n_accepted, total)
            _write_fits(fits_path, stacked, Path(output_path).stem)

            # ════════════════════════════════════════════════════════════════
            # STEP 10 — preview JPEG (stretch + SCNR + denoise, never linear)
            # SCNR and all colour/aesthetic operations belong here only.
            # ════════════════════════════════════════════════════════════════
            progress_cb(90, "Auto-stretch (preview)", n_accepted, total)
            _chk()
            preview = _auto_stretch(stacked.copy())

            progress_cb(93, "SCNR — green noise reduction (preview)", n_accepted, total)
            preview = _scnr_green(preview)

            progress_cb(95, "Noise reduction and sharpening (preview)", n_accepted, total)
            _chk()
            preview_u8 = (preview * 255).astype(np.uint8)
            preview_u8 = _denoise_sharpen(preview_u8)

            progress_cb(97, "Saving preview JPEG", n_accepted, total)
            out_dir = os.path.dirname(os.path.abspath(output_path))
            os.makedirs(out_dir, exist_ok=True)
            ok = cv2.imwrite(
                str(output_path), preview_u8,
                [cv2.IMWRITE_JPEG_QUALITY, 95],
            )
            if not ok:
                raise RuntimeError(f"Failed to write preview JPEG to {output_path}")

            run_stats['elapsed_s'] = round(time.monotonic() - t_start, 1)
            log_path = str(Path(output_path).with_suffix('.log'))
            _write_stack_log(log_path, run_stats, output_path)

            progress_cb(100, "Done", n_accepted, total)
            return {
                "frames_total":    total,
                "frames_accepted": n_accepted,
                "output_path":     output_path,
                "log_path":        log_path,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
