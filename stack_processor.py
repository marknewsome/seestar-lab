"""
Seestar Lab — FITS sub-frame stacking engine (v2).

Pipeline:
  1. Quality scan      — Laplacian-variance sharpness; pick reference frame
  2. Registration pass — align each frame to reference; normalise sky background
  3. Frame metrics     — SEP FWHM / eccentricity / background → per-frame weight
  4. Integration       — weighted sigma-clipped mean (chunked, memory-efficient)
  5. Upsample          — 2× Lanczos-4 to match Seestar output resolution
  6. Background        — 2D polynomial gradient subtraction (16×16 grid)
  7. Crop              — trim invalid border pixels from alignment warps
  8. Colour calibration— background neutralise + star white balance (SEP)
  9. Stretch           — PixInsight-style MTF auto-stretch per channel
 10. Enhancement       — NLM denoising + unsharp-mask sharpening
 11. Save              — linear float32 FITS (optional) + JPEG

v2 improvements over v1:
  - Per-frame sky background normalisation before integration — critical for
    multi-session data; frames from murky nights no longer bias the stack
  - SEP-based FWHM + eccentricity frame weighting; sharper frames contribute more
  - Weighted sigma-clipped mean replaces unweighted mean
  - Colour calibration: background neutralisation + aperture-photometry star
    white balance so galaxy colours are not shifted by the stretch
  - Linear float32 FITS output alongside JPEG for further processing
"""

import os
import warnings
import numpy as np
import cv2
from pathlib import Path
from typing import Callable, Optional


# ── Constants ──────────────────────────────────────────────────────────────────

FITS_EXT          = {'.fit', '.fits', '.fts'}
MIN_FRAMES        = 3      # refuse to stack fewer than this many accepted frames
QUALITY_THRESHOLD = 0.40   # reject frames below this fraction of median sharpness
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


# ── Quality assessment ────────────────────────────────────────────────────────

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
    """
    Estimate sky background level as the 10th percentile of the green channel.
    Uses only non-zero pixels to exclude alignment border padding.
    """
    green = bgr_f32[:, :, 1].ravel()
    nonzero = green[green > 0.001]
    if nonzero.size == 0:
        return float(np.percentile(green, 10))
    return float(np.percentile(nonzero, 10))


# ── Registration ──────────────────────────────────────────────────────────────

def _to_gray8(bgr_f32: np.ndarray) -> np.ndarray:
    """
    Convert float32 BGR [0,1] to uint8 grayscale for registration.
    CLAHE boosts local contrast so faint star fields have enough gradient
    for ECC to converge even when a single hot pixel dominates the range.
    """
    gray = (0.299 * bgr_f32[:, :, 2]
          + 0.587 * bgr_f32[:, :, 1]
          + 0.114 * bgr_f32[:, :, 0])
    lo, hi = float(np.percentile(gray, 0.5)), float(np.percentile(gray, 99.5))
    if hi > lo:
        gray = np.clip((gray - lo) / (hi - lo), 0.0, 1.0)
    gray8 = (gray * 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray8)


_MAX_WARP_SHIFT_PX = 50.0
_MAX_WARP_ROT_DEG  = 2.0


def _warp_is_sane(warp: np.ndarray) -> bool:
    dx  = float(warp[0, 2])
    dy  = float(warp[1, 2])
    rot = abs(float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))))
    return (abs(dx) <= _MAX_WARP_SHIFT_PX
            and abs(dy) <= _MAX_WARP_SHIFT_PX
            and rot     <= _MAX_WARP_ROT_DEG)


def _register(ref_gray8: np.ndarray, frame_gray8: np.ndarray) -> np.ndarray | None:
    """
    Find the 2×3 Euclidean warp aligning frame to reference.
    Tries astroalign → ECC → phase correlation, returns None if all fail.
    """
    # 1. astroalign — star-triangle pattern matching
    try:
        import astroalign as aa
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            _, tf = aa.find_transform(frame_gray8, ref_gray8)
        params = tf.params
        warp_aa = np.array([
            [params[0, 0], params[0, 1], params[0, 2]],
            [params[1, 0], params[1, 1], params[1, 2]],
        ], dtype=np.float32)
        if _warp_is_sane(warp_aa):
            return warp_aa
    except Exception:
        pass

    # 2. ECC image correlation
    warp     = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    try:
        _, warp = cv2.findTransformECC(
            ref_gray8, frame_gray8, warp,
            cv2.MOTION_EUCLIDEAN, criteria,
            inputMask=None, gaussFiltSize=5,
        )
        if _warp_is_sane(warp):
            return warp
    except cv2.error:
        pass

    # 3. Phase correlation — translation only
    try:
        (dx, dy), _ = cv2.phaseCorrelate(
            ref_gray8.astype(np.float32),
            frame_gray8.astype(np.float32),
        )
        fallback       = np.eye(2, 3, dtype=np.float32)
        fallback[0, 2] = float(dx)
        fallback[1, 2] = float(dy)
        if _warp_is_sane(fallback):
            return fallback
    except Exception:
        pass

    return None


# ── Frame quality metrics (SEP-based) ─────────────────────────────────────────

def _frame_metrics(bgr_f32: np.ndarray) -> dict:
    """
    Measure per-frame quality using SEP (Source Extractor Python).
    Returns {fwhm, eccentricity, background}.
    Falls back to neutral penalty values if SEP is unavailable or finds too few stars.
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
        return {
            'fwhm':         float(np.median(fwhm)),
            'eccentricity': float(np.median(ecc)),
            'background':   float(bkg.globalback),
        }
    except Exception:
        # Neutral fallback — frame gets average weight, not zero
        return {'fwhm': 3.0, 'eccentricity': 0.3, 'background': 0.0}


def _compute_weight(metrics: dict) -> float:
    """
    w = (1/FWHM²) × (1/(1+ecc))
    Background term omitted here — background normalisation handles that separately.
    """
    fwhm = max(metrics['fwhm'], 0.5)
    ecc  = max(metrics['eccentricity'], 0.0)
    return (1.0 / fwhm**2) * (1.0 / (1.0 + ecc))


# ── Weighted sigma-clipped integration ────────────────────────────────────────

def _weighted_sigma_clip(
    stack: np.ndarray,
    weights: np.ndarray,
    sigma_low: float  = 2.0,
    sigma_high: float = 3.0,
    n_iter: int       = 3,
    chunk_rows: int   = 128,
) -> np.ndarray:
    """
    Weighted sigma-clipped mean, chunked over rows for memory efficiency.

    stack:   float32 (N, H, W, 3)
    weights: float32 (N,) — need not be normalised
    Returns  float32 (H, W, 3)

    Peak extra RAM per chunk ≈ 2 × chunk_size; the full (N,H,W,3) stack
    must already be in memory but no additional N-sized temporaries are created.
    """
    N, H, W, C = stack.shape
    w  = (weights / weights.sum()).astype(np.float32)
    wc = w[:, np.newaxis, np.newaxis, np.newaxis]    # (N,1,1,1)
    result = np.empty((H, W, C), dtype=np.float32)

    for r0 in range(0, H, chunk_rows):
        r1    = min(r0 + chunk_rows, H)
        chunk = stack[:, r0:r1, :, :]       # (N, rH, W, C) — view, no copy

        mu = (chunk * wc).sum(axis=0)       # weighted mean

        for _ in range(n_iter):
            diff  = chunk - mu[np.newaxis]
            wvar  = ((diff ** 2) * wc).sum(axis=0)
            sigma = np.sqrt(np.maximum(wvar, 1e-12))

            valid = (chunk >= mu[np.newaxis] - sigma_low  * sigma[np.newaxis]) & \
                    (chunk <= mu[np.newaxis] + sigma_high * sigma[np.newaxis])
            w_sel = np.where(valid, wc, 0.0)
            w_sum = w_sel.sum(axis=0)
            new_mu = (chunk * w_sel).sum(axis=0) / np.maximum(w_sum, 1e-12)
            # Where everything was clipped, keep the plain weighted mean
            mu = np.where(w_sum < 1e-12, mu, new_mu)

        result[r0:r1] = mu

    return result


# ── Background subtraction ────────────────────────────────────────────────────

def _subtract_background(img: np.ndarray, grid: int = 16) -> np.ndarray:
    """
    Fit and subtract a degree-2 2D polynomial background per channel.
    Samples the 20th percentile of each grid cell to avoid stars/galaxy arms.
    A 16×16 grid provides finer spatial resolution than v1's 8×8 while
    still fitting well above the scale of extended emission in most targets.
    """
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
                bg = float(np.percentile(patch, 20))
                ys.append((y0 + y1) * 0.5 / h)
                xs.append((x0 + x1) * 0.5 / w)
                vals.append(bg)

        if len(vals) < 6:
            continue

        ys_   = np.asarray(ys,   dtype=np.float64)
        xs_   = np.asarray(xs,   dtype=np.float64)
        vals_ = np.asarray(vals, dtype=np.float64)
        A     = np.column_stack([
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
        sub -= sub.min()    # floor to 0 without clipping faint detail
        result[:, :, c] = sub

    return result


# ── Colour calibration ────────────────────────────────────────────────────────

def _color_calibrate(img: np.ndarray) -> np.ndarray:
    """
    Two-step colour calibration on a linear float32 (H, W, 3) BGR image.

    Step 1 — Background neutralisation:
      Scale each channel so all three sky backgrounds match.
      Removes colour casts without touching the signal.

    Step 2 — Star white balance via SEP aperture photometry:
      Measure R/G and B/G flux ratios across bright isolated stars.
      Scale R and B so stars appear neutral (white).
      Falls back to step-1 result if SEP is unavailable or too few stars.
    """
    result = img.copy()

    # Step 1: background neutralisation
    bg = np.array([
        float(np.percentile(result[:, :, c][result[:, :, c] > 0], 5))
        for c in range(3)
    ])
    bg_mean = bg.mean()
    for c in range(3):
        if bg[c] > 0:
            result[:, :, c] *= bg_mean / bg[c]

    # Step 2: star white balance
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
                    ch     = np.ascontiguousarray(result[:, :, ci].astype(np.float64))
                    bk     = sep.Background(ch)
                    f, _, _ = sep.sum_circle(ch - bk.back(), [o['x']], [o['y']], 3.0)
                    fluxes.append(float(f[0]))
                b_val, g_val, r_val = fluxes   # OpenCV BGR
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


# ── Crop ──────────────────────────────────────────────────────────────────────

def _auto_crop(img: np.ndarray, valid_mask: np.ndarray, margin: int = 12) -> np.ndarray:
    """Crop img to the rectangle where valid_mask is True, with a safety margin."""
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
    """
    PixInsight-style Midtone Transfer Function (STF) auto-stretch, per channel.
    Black clip at (sky − 2.8σ); MTF midpoint solved so sky median → 0.12.
    """
    result = np.zeros_like(img, dtype=np.float32)
    m_tgt  = _STF_MIDTONE_TARGET

    for c in range(3):
        ch  = img[:, :, c].ravel()
        sky = ch[ch > 0] if (ch > 0).any() else ch

        med   = float(np.median(sky))
        mad   = float(np.median(np.abs(sky - med)))
        sigma = mad * 1.4826

        c0   = max(0.0, med - 2.8 * sigma)
        hi   = float(np.percentile(sky, 99.9))
        span = max(hi - c0, 1e-10)

        x     = np.clip((img[:, :, c] - c0) / span, 0.0, 1.0)
        med_n = float(np.clip((med - c0) / span, 1e-6, 1.0 - 1e-6))

        denom_m = 2.0 * med_n * m_tgt - m_tgt - med_n
        if abs(denom_m) > 1e-10:
            m = float(med_n * (m_tgt - 1.0) / denom_m)
            m = max(1e-4, min(1.0 - 1e-4, m))
        else:
            m = 0.5

        denom = (2.0 * m - 1.0) * x - m
        denom = np.where(np.abs(denom) > 1e-10, denom, np.sign(denom + 1e-30) * 1e-10)
        result[:, :, c] = np.clip((m - 1.0) * x / denom, 0.0, 1.0)

    return result


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray) -> np.ndarray:
    """NLM colour denoising + unsharp-mask sharpening. img: uint8 BGR."""
    denoised = cv2.fastNlMeansDenoisingColored(
        img, None,
        h=6, hColor=6,
        templateWindowSize=7, searchWindowSize=21,
    )
    blurred   = cv2.GaussianBlur(denoised, (0, 0), 1.2)
    sharpened = cv2.addWeighted(denoised, 1.6, blurred, -0.6, 0)
    return sharpened


# ── FITS writer ───────────────────────────────────────────────────────────────

def _write_fits(path: str, data: np.ndarray, object_name: str = '') -> bool:
    """
    Save a linear float32 (H, W, 3) BGR image as a 3-plane FITS (RGB axis order).
    Returns True on success, False if astropy is unavailable.
    """
    try:
        from astropy.io import fits as _fits
        rgb = data[:, :, ::-1].transpose(2, 0, 1).astype(np.float32)
        hdu = _fits.PrimaryHDU(rgb)
        hdu.header['BUNIT']   = 'normalized'
        hdu.header['COLORMD'] = 'RGB'
        hdu.header['OBJECT']  = object_name
        hdu.header['INSTRUME'] = 'Seestar S50'
        hdu.header['CREATOR'] = 'Seestar Lab'
        hdu.writeto(path, overwrite=True)
        return True
    except Exception:
        return False


# ── Main processor ────────────────────────────────────────────────────────────

class StackProcessor:
    """
    Full stacking pipeline for Seestar S50 FITS sub-frames.

    Usage:
        proc   = StackProcessor()
        result = proc.run(fits_files, output_path, progress_cb, cancel_cb)

    progress_cb(pct: int, stage: str, accepted: int, total: int)
    cancel_cb() -> bool   (return True to abort)
    """

    def run(
        self,
        fits_files:  list[str],
        output_path: str,
        progress_cb: Callable[[int, str, int, int], None],
        cancel_cb:   Optional[Callable[[], bool]] = None,
    ) -> dict:

        def _chk():
            if cancel_cb and cancel_cb():
                raise StackCancelled("Stacking cancelled")

        total = len(fits_files)
        if total < MIN_FRAMES:
            raise RuntimeError(
                f"Need at least {MIN_FRAMES} FITS files to stack, found {total}"
            )

        # ── Stage 1: Sharpness scan ────────────────────────────────────────────
        progress_cb(2, f"Scanning {total} frames for quality", 0, total)
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
            progress_cb(2 + int(18 * (i + 1) / total),
                        f"Scanning quality: {i + 1}/{total}", 0, total)

        positive = [s for s in sharpness if s > 0]
        if not positive:
            raise RuntimeError("Could not read any FITS frames")

        threshold    = float(np.median(positive)) * QUALITY_THRESHOLD
        accepted_idx = [i for i, s in enumerate(sharpness) if s >= threshold]
        if len(accepted_idx) < MIN_FRAMES:
            accepted_idx = list(range(len(fits_files)))

        accepted_files = [fits_files[i] for i in accepted_idx]
        n_accepted     = len(accepted_files)
        progress_cb(20, f"Accepted {n_accepted}/{total} frames", n_accepted, total)
        _chk()

        # ── Stage 2: Load reference frame ─────────────────────────────────────
        ref_scores = [sharpness[i] for i in accepted_idx]
        ref_local  = int(np.argmax(ref_scores))

        progress_cb(22, "Loading reference frame", n_accepted, total)
        ref_raw, _ = _read_fits(accepted_files[ref_local])
        ref_bgr    = _debayer(ref_raw, bayer_pattern).astype(np.float32) / 65535.0
        ref_gray8  = _to_gray8(ref_bgr)
        h, w       = ref_bgr.shape[:2]
        ref_bg     = _sky_background(ref_bgr)

        frames:  list[np.ndarray] = [ref_bgr]
        masks:   list[np.ndarray] = [np.ones((h, w), dtype=bool)]
        metrics: list[dict]       = [_frame_metrics(ref_bgr)]

        # ── Stage 3: Register + normalise + measure ────────────────────────────
        for fi, fpath in enumerate(accepted_files):
            _chk()
            if fi == ref_local:
                continue
            pct = 22 + int(43 * (fi + 1) / n_accepted)
            progress_cb(pct, f"Aligning {fi + 1}/{n_accepted}", n_accepted, total)
            try:
                raw, _  = _read_fits(fpath)
                bgr     = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0

                # Background-normalise to reference level before alignment
                frame_bg = _sky_background(bgr)
                if frame_bg > 0 and ref_bg > 0:
                    bgr = np.clip(bgr + (ref_bg - frame_bg), 0.0, None)

                gray8 = _to_gray8(bgr)
                warp  = _register(ref_gray8, gray8)
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

                frames.append(aligned)
                masks.append(valid)
                metrics.append(_frame_metrics(aligned))
            except Exception:
                pass

        if len(frames) < MIN_FRAMES:
            raise RuntimeError(
                f"Only {len(frames)} frames registered (need {MIN_FRAMES})"
            )

        # ── Stage 4: Compute weights + integrate ──────────────────────────────
        progress_cb(65, f"Integrating {len(frames)} frames (weighted σ-clip)", n_accepted, total)
        _chk()

        raw_weights = np.array([_compute_weight(m) for m in metrics], dtype=np.float32)
        if raw_weights.sum() == 0:
            raw_weights = np.ones(len(frames), dtype=np.float32)

        stack_arr = np.stack(frames, axis=0)    # (N, H, W, 3)
        del frames

        stacked = _weighted_sigma_clip(stack_arr, raw_weights)
        del stack_arr

        all_valid_native = masks[0].copy()
        for m in masks[1:]:
            all_valid_native &= m

        # ── Stage 5: 2× upsample ──────────────────────────────────────────────
        progress_cb(72, "Upsampling 2×", n_accepted, total)
        oh, ow  = h * DRIZZLE_SCALE, w * DRIZZLE_SCALE
        stacked = cv2.resize(stacked, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        all_valid = cv2.resize(
            all_valid_native.astype(np.uint8), (ow, oh),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)

        # ── Stage 6: Background subtraction ───────────────────────────────────
        progress_cb(75, "Removing background gradient", n_accepted, total)
        _chk()
        stacked = _subtract_background(stacked, grid=16)

        # ── Stage 7: Crop ─────────────────────────────────────────────────────
        progress_cb(80, "Cropping to valid overlap region", n_accepted, total)
        stacked = _auto_crop(stacked, all_valid)

        # ── Stage 8: Colour calibration ───────────────────────────────────────
        progress_cb(83, "Colour calibration", n_accepted, total)
        _chk()
        stacked = _color_calibrate(stacked)

        # ── Stage 9: Save linear FITS ─────────────────────────────────────────
        fits_path = str(Path(output_path).with_suffix('.fits'))
        progress_cb(87, "Saving linear FITS", n_accepted, total)
        object_name = Path(output_path).stem
        _write_fits(fits_path, stacked, object_name)

        # ── Stage 10: Stretch ─────────────────────────────────────────────────
        progress_cb(88, "Auto-stretch", n_accepted, total)
        _chk()
        stacked = _auto_stretch(stacked)

        # ── Stage 11: Denoise + sharpen ───────────────────────────────────────
        progress_cb(93, "Noise reduction and sharpening", n_accepted, total)
        _chk()
        stacked_u8 = (stacked * 255).astype(np.uint8)
        stacked_u8 = _denoise_sharpen(stacked_u8)

        # ── Stage 12: Save JPEG ───────────────────────────────────────────────
        progress_cb(97, "Saving JPEG", n_accepted, total)
        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)
        ok = cv2.imwrite(
            str(output_path), stacked_u8,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        )
        if not ok:
            raise RuntimeError(f"Failed to write JPEG to {output_path}")

        progress_cb(100, "Done", n_accepted, total)
        return {
            "frames_total":    total,
            "frames_accepted": n_accepted,
            "output_path":     output_path,
        }
