"""
Seestar Lab — FITS sub-frame stacking engine (v2).

Pipeline:
  1. Sharpness scan    — light sequential pass: read raw Bayer, compute Laplacian
                         variance, no debayer.  Establishes quality ranking.
  2. Frame selection   — reject below 40 % of median sharpness; keep top max_frames
                         by score.  Files re-sorted to original on-disk order so
                         pass 2 is as sequential as possible on spinning drives.
  3. Registration pass — heavy pass on accepted files only: read FITS, debayer,
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
Pass 1 reads the raw Bayer uint16 only (~4 MB/frame), sequentially, in filename
order — no debayer, no caching.  For a 5 000-frame library on a spinning drive
this is one sequential sweep of ~20 GB.

Pass 2 reads only the top-max_frames files (default 500), still in their original
filename order so the heads move forward, never back.  At 500 frames that is
~2 GB of sequential I/O from the source drive; the other 4 500 files are never
touched again.

This replaces the v1 pattern of two full random-access sweeps (sharpness scan
then registration), which caused a head seek per file on each pass.
"""

import os
import shutil
import tempfile
import warnings
import numpy as np
import cv2
from pathlib import Path
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
    2×3 Euclidean warp (WARP_INVERSE_MAP) aligning frame to reference.
    astroalign → ECC → phase-correlation; returns None if all fail.
    """
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
    SEP-based FWHM + eccentricity.  Falls back to neutral values if SEP
    is unavailable or finds too few stars.
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
        }
    except Exception:
        return {'fwhm': 3.0, 'eccentricity': 0.3}


def _compute_weight(metrics: dict) -> float:
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
    Weighted sigma-clipped mean, chunked over rows to bound peak RAM.
    stack: float32 (N, H, W, 3);  weights: float32 (N,).
    Returns float32 (H, W, 3).
    """
    N, H, W, C = stack.shape
    w  = (weights / weights.sum()).astype(np.float32)
    wc = w[:, np.newaxis, np.newaxis, np.newaxis]
    result = np.empty((H, W, C), dtype=np.float32)

    for r0 in range(0, H, chunk_rows):
        r1    = min(r0 + chunk_rows, H)
        chunk = stack[:, r0:r1, :, :]

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

        total = len(fits_files)
        if total < MIN_FRAMES:
            raise RuntimeError(
                f"Need at least {MIN_FRAMES} FITS files to stack, found {total}"
            )

        # ── Pass 1: light sharpness scan (sequential, Bayer only, no debayer) ──
        # Reads each file once to compute Laplacian-variance sharpness.
        # No debayer — we only need the raw pixel gradient for a quality rank.
        # This is the cheapest possible pass: ~4 MB/frame, fully sequential.
        progress_cb(2, f"Scanning {total} frames (pass 1/2 — quality)", 0, total)
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
            progress_cb(2 + int(23 * (i + 1) / total),
                        f"Quality scan: {i + 1}/{total}", 0, total)

        positive = [s for s in sharpness if s > 0]
        if not positive:
            raise RuntimeError("Could not read any FITS frames")

        # Apply quality threshold, then keep only the best max_frames.
        threshold    = float(np.median(positive)) * QUALITY_THRESHOLD
        passing_idx  = [i for i, s in enumerate(sharpness) if s >= threshold]

        # Sort by sharpness descending, cap at max_frames
        passing_idx.sort(key=lambda i: sharpness[i], reverse=True)
        selected_idx = passing_idx[:max_frames]

        # Re-sort to original on-disk order so pass 2 reads are as sequential
        # as possible — the heads move forward, never backward.
        selected_idx.sort()
        selected_files = [fits_files[i] for i in selected_idx]
        n_selected     = len(selected_files)

        if n_selected < MIN_FRAMES:
            raise RuntimeError(
                f"Only {n_selected} frames passed quality selection (need {MIN_FRAMES})"
            )

        dropped = total - n_selected
        progress_cb(25, (f"Selected top {n_selected}/{total} frames"
                         f" (dropped {dropped} below threshold or max_frames cap)"),
                    n_selected, total)
        _chk()

        # ── Pass 2: debayer + register + metrics (selected files only) ─────────
        # Reads only n_selected files (≤ max_frames), in original filename order.
        # For a 5 000-frame library with max_frames=500, this reads 10 % of files.

        # Reference frame: the sharpest among the selected set
        ref_sharp_idx = max(range(n_selected),
                            key=lambda i: sharpness[selected_idx[i]])

        progress_cb(26, "Loading reference frame (pass 2/2)", n_selected, total)
        ref_raw, _ = _read_fits(selected_files[ref_sharp_idx])
        ref_bgr    = _debayer(ref_raw, bayer_pattern).astype(np.float32) / 65535.0
        ref_gray8  = _to_gray8(ref_bgr)
        h, w       = ref_bgr.shape[:2]
        ref_bg     = _sky_background(ref_bgr)

        frames:  list[np.ndarray] = [ref_bgr]
        masks:   list[np.ndarray] = [np.ones((h, w), dtype=bool)]
        metrics: list[dict]       = [_frame_metrics(ref_bgr)]

        for fi, fpath in enumerate(selected_files):
            _chk()
            if fi == ref_sharp_idx:
                continue
            pct = 26 + int(44 * (fi + 1) / n_selected)
            progress_cb(pct, f"Aligning {fi + 1}/{n_selected}", n_selected, total)
            try:
                raw, _   = _read_fits(fpath)
                bgr      = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0

                # Normalise sky background to reference level before alignment.
                # Frames from bright/murky sessions are shifted additively so
                # they don't bias the integrated stack sky level.
                frame_bg = _sky_background(bgr)
                if frame_bg > 0 and ref_bg > 0:
                    bgr = np.clip(bgr + (ref_bg - frame_bg), 0.0, None)

                gray8   = _to_gray8(bgr)
                warp    = _register(ref_gray8, gray8)
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
                f"Only {len(frames)} frames registered successfully (need {MIN_FRAMES})"
            )

        # ── Integration ────────────────────────────────────────────────────────
        progress_cb(70, f"Integrating {len(frames)} frames (weighted σ-clip)",
                    n_selected, total)
        _chk()

        raw_weights = np.array([_compute_weight(m) for m in metrics], dtype=np.float32)
        if raw_weights.sum() == 0:
            raw_weights = np.ones(len(frames), dtype=np.float32)

        stack_arr = np.stack(frames, axis=0)
        del frames

        stacked = _weighted_sigma_clip(stack_arr, raw_weights)
        del stack_arr

        all_valid_native = masks[0].copy()
        for m in masks[1:]:
            all_valid_native &= m

        # ── 2× upsample ───────────────────────────────────────────────────────
        progress_cb(77, "Upsampling 2×", n_selected, total)
        oh, ow  = h * DRIZZLE_SCALE, w * DRIZZLE_SCALE
        stacked = cv2.resize(stacked, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        all_valid = cv2.resize(
            all_valid_native.astype(np.uint8), (ow, oh),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)

        # ── Background subtraction ─────────────────────────────────────────────
        progress_cb(80, "Removing background gradient", n_selected, total)
        _chk()
        stacked = _subtract_background(stacked, grid=16)

        # ── Crop ───────────────────────────────────────────────────────────────
        progress_cb(84, "Cropping to valid overlap region", n_selected, total)
        stacked = _auto_crop(stacked, all_valid)

        # ── Colour calibration ─────────────────────────────────────────────────
        progress_cb(86, "Colour calibration", n_selected, total)
        _chk()
        stacked = _color_calibrate(stacked)

        # ── Save linear FITS ───────────────────────────────────────────────────
        fits_path = str(Path(output_path).with_suffix('.fits'))
        progress_cb(88, "Saving linear FITS", n_selected, total)
        _write_fits(fits_path, stacked, Path(output_path).stem)

        # ── Stretch ────────────────────────────────────────────────────────────
        progress_cb(89, "Auto-stretch", n_selected, total)
        _chk()
        stacked = _auto_stretch(stacked)

        # ── Denoise + sharpen ──────────────────────────────────────────────────
        progress_cb(93, "Noise reduction and sharpening", n_selected, total)
        _chk()
        stacked_u8 = (stacked * 255).astype(np.uint8)
        stacked_u8 = _denoise_sharpen(stacked_u8)

        # ── Save JPEG ──────────────────────────────────────────────────────────
        progress_cb(97, "Saving JPEG", n_selected, total)
        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)
        ok = cv2.imwrite(
            str(output_path), stacked_u8,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        )
        if not ok:
            raise RuntimeError(f"Failed to write JPEG to {output_path}")

        progress_cb(100, "Done", n_selected, total)
        return {
            "frames_total":    total,
            "frames_accepted": n_selected,
            "output_path":     output_path,
        }
