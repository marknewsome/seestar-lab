#!/usr/bin/env python3
"""
Experiment: deterministic astrophotography stacking pipeline.

Steps:
  1. Load all subs (no alteration)
  2. Star alignment — astroalign, sharpest sub as reference, translation+rotation only
  3. Subframe weighting — FWHM, background level, eccentricity via SEP
  4. Weighted sigma-clip stack — sigma_low=2.0, sigma_high=3.0
  5. Background extraction — coarse grid 2D polynomial subtraction
  6. Color calibration — background neutralisation + star white balance
  7. arcsinh stretch — gentle, preserves star colour
  8. Save final JPEG + linear float32 FITS
"""

import os
import sys
import glob
import warnings
import numpy as np
import cv2

# ── Paths ─────────────────────────────────────────────────────────────────────

SUBS_DIR   = "/mnt/d/xfer/NGC 6946_sub"
OUT_DIR    = "/mnt/d/xfer/NGC 6946"
OUT_FINAL  = os.path.join(OUT_DIR, "NGC6946_experiment_final.jpg")
OUT_LINEAR = os.path.join(OUT_DIR, "NGC6946_experiment_linear.fits")
MAX_FRAMES = 119   # match Seestar's 119-frame stack for direct comparison

BAYER_PATTERN = 'GRBG'   # Seestar S50 default

# ── FITS reader (no astropy dependency for reading) ───────────────────────────

def _parse_fits_header(raw: bytes) -> dict:
    header: dict = {}
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
        raw = f.read(n_bytes)

    dtype_map = {16: '>i2', 32: '>i4', -32: '>f4', -64: '>f8'}
    dtype = dtype_map.get(bitpix)
    if dtype is None:
        raise ValueError(f"Unsupported BITPIX={bitpix}")
    arr = np.frombuffer(raw, dtype=dtype).reshape(naxis2, naxis1).astype(np.float32)
    physical = arr * bscale + bzero
    return np.clip(physical, 0, 65535).astype(np.uint16), header


# ── Debayer ───────────────────────────────────────────────────────────────────

_BAYER_CODES = {
    'RGGB': cv2.COLOR_BayerRG2BGR,
    'BGGR': cv2.COLOR_BayerBG2BGR,
    'GRBG': cv2.COLOR_BayerGR2BGR,
    'GBRG': cv2.COLOR_BayerGB2BGR,
}


def _debayer(raw: np.ndarray, pattern: str = 'GRBG') -> np.ndarray:
    code = _BAYER_CODES.get(pattern.upper().strip(), cv2.COLOR_BayerGR2BGR)
    return cv2.cvtColor(raw, code)


# ── Sharpness (for reference frame selection) ─────────────────────────────────

def _sharpness(raw_bayer: np.ndarray) -> float:
    h, w   = raw_bayer.shape
    cy, cx = h // 2, w // 2
    qh, qw = h // 4, w // 4
    crop   = raw_bayer[cy - qh:cy + qh, cx - qw:cx + qw]
    lap    = cv2.Laplacian(crop.astype(np.float32), cv2.CV_32F)
    return float(lap.var())


# ── Registration ──────────────────────────────────────────────────────────────

def _to_gray8(bgr_f32: np.ndarray) -> np.ndarray:
    gray = (0.299 * bgr_f32[:, :, 2]
          + 0.587 * bgr_f32[:, :, 1]
          + 0.114 * bgr_f32[:, :, 0])
    lo, hi = float(np.percentile(gray, 0.5)), float(np.percentile(gray, 99.5))
    if hi > lo:
        gray = np.clip((gray - lo) / (hi - lo), 0.0, 1.0)
    gray8 = (gray * 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray8)


def _warp_is_sane(warp: np.ndarray, max_shift: float = 50.0, max_rot_deg: float = 2.0) -> bool:
    dx  = float(warp[0, 2])
    dy  = float(warp[1, 2])
    rot = abs(float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))))
    return abs(dx) <= max_shift and abs(dy) <= max_shift and rot <= max_rot_deg


def _register(ref_gray8: np.ndarray, frame_gray8: np.ndarray) -> np.ndarray | None:
    """Return 2×3 Euclidean warp (WARP_INVERSE_MAP) or None."""
    # 1. astroalign (star triangles — best quality)
    try:
        import astroalign as aa
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            _, tf = aa.find_transform(frame_gray8, ref_gray8)
        params  = tf.params
        warp_aa = np.array([
            [params[0, 0], params[0, 1], params[0, 2]],
            [params[1, 0], params[1, 1], params[1, 2]],
        ], dtype=np.float32)
        if _warp_is_sane(warp_aa):
            return warp_aa
    except Exception:
        pass

    # 2. ECC fallback
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

    # 3. Phase correlation (translation only)
    try:
        (dx, dy), _ = cv2.phaseCorrelate(
            ref_gray8.astype(np.float32),
            frame_gray8.astype(np.float32),
        )
        fb       = np.eye(2, 3, dtype=np.float32)
        fb[0, 2] = float(dx)
        fb[1, 2] = float(dy)
        if _warp_is_sane(fb):
            return fb
    except Exception:
        pass

    return None


# ── SEP-based frame metrics ───────────────────────────────────────────────────

def _frame_metrics(bgr_f32: np.ndarray) -> dict:
    """
    Use SEP (Source Extractor Python) to measure per-frame quality:
      - fwhm:       median FWHM of detected stars (pixels)
      - eccentricity: median eccentricity (0=round, 1=line)
      - background: median sky background level

    Returns a dict with those three keys.
    Falls back to simple Laplacian-based metrics if SEP fails.
    """
    try:
        import sep
        # Work on the green channel (best S/N for a GRBG Bayer pattern)
        green = bgr_f32[:, :, 1].astype(np.float64)
        green = np.ascontiguousarray(green)

        bkg  = sep.Background(green)
        data = green - bkg.back()

        objects = sep.extract(data, thresh=5.0, err=bkg.globalrms, minarea=9)

        if len(objects) < 5:
            raise RuntimeError("too few stars")

        # Filter: flag==0 (clean detections), reasonable size
        mask = (objects['flag'] == 0) & (objects['a'] > 0.5) & (objects['b'] > 0.5)
        obj  = objects[mask]

        if len(obj) < 5:
            obj = objects   # lenient: use all

        # FWHM from second-moment axes (Gaussian approximation)
        fwhm = 2.355 * np.sqrt(obj['a'] * obj['b'])   # geometric mean
        ecc  = np.sqrt(np.maximum(1.0 - (obj['b'] / np.maximum(obj['a'], 1e-6))**2, 0.0))

        return {
            'fwhm':         float(np.median(fwhm)),
            'eccentricity': float(np.median(ecc)),
            'background':   float(bkg.globalback),
        }

    except Exception as e:
        # Fallback: no SEP or too few stars
        green = bgr_f32[:, :, 1]
        return {
            'fwhm':         10.0,   # neutral penalty
            'eccentricity': 0.5,
            'background':   float(np.percentile(green, 10)),
        }


def _compute_weight(metrics: dict) -> float:
    """
    Linear combination of quality scores.
      w = (1/FWHM²) × (1/(1+ecc)) × (1/(1+bg))
    All terms normalised to [0,1] across the ensemble after collection.
    Returns raw un-normalised weight here; caller normalises.
    """
    fwhm = max(metrics['fwhm'], 0.5)
    ecc  = max(metrics['eccentricity'], 0.0)
    bg   = max(metrics['background'], 0.0)
    return (1.0 / fwhm**2) * (1.0 / (1.0 + ecc)) * (1.0 / (1.0 + bg * 0.001))


# ── Weighted sigma-clipped integration ────────────────────────────────────────

def _weighted_sigma_clip(
    stack: np.ndarray,
    weights: np.ndarray,
    sigma_low: float = 2.0,
    sigma_high: float = 3.0,
    n_iter: int = 3,
    chunk_rows: int = 100,
) -> np.ndarray:
    """
    Weighted sigma-clipped mean — chunked iterative implementation.

    Avoids ever sorting the full (N, H, W, C) array (which requires 3× the
    stack size in sort-index memory).  Instead, processes `chunk_rows` rows at
    a time using iterative weighted-mean + weighted-stddev clipping:

      1. Compute weighted mean of the chunk
      2. Compute weighted σ (standard deviation)
      3. Reject pixels outside [μ − σ_low·σ, μ + σ_high·σ]
      4. Recompute weighted mean from survivors
      Repeat n_iter times.  Falls back to plain weighted mean where all clipped.

    Peak extra RAM per chunk: ≈ 2 × chunk_size (mean array + diff array).

    stack:   float32 (N, H, W, C)
    weights: float32 (N,)
    """
    N, H, W, C = stack.shape
    w = (weights / weights.sum()).astype(np.float32)   # normalised
    result = np.empty((H, W, C), dtype=np.float32)

    for r0 in range(0, H, chunk_rows):
        r1    = min(r0 + chunk_rows, H)
        chunk = stack[:, r0:r1, :, :]          # (N, rH, W, C) — view, no copy
        rH    = r1 - r0

        # Broadcast weights: (N, 1, 1, 1)
        wc = w[:, np.newaxis, np.newaxis, np.newaxis]

        # Initial weighted mean
        mu = (chunk * wc).sum(axis=0)          # (rH, W, C)

        for _ in range(n_iter):
            diff  = chunk - mu[np.newaxis]
            # Weighted variance
            wvar  = ((diff ** 2) * wc).sum(axis=0)
            sigma = np.sqrt(np.maximum(wvar, 1e-12))

            lo    = mu - sigma_low  * sigma
            hi    = mu + sigma_high * sigma

            valid = (chunk >= lo[np.newaxis]) & (chunk <= hi[np.newaxis])
            w_sel = np.where(valid, wc, 0.0)   # (N, rH, W, C)
            w_sum = w_sel.sum(axis=0)           # (rH, W, C)

            new_mu = (chunk * w_sel).sum(axis=0) / np.maximum(w_sum, 1e-12)

            # Fallback: use plain weighted mean where all frames clipped
            all_clip = (w_sum < 1e-12)
            if all_clip.any():
                new_mu = np.where(all_clip, mu, new_mu)

            mu = new_mu

        result[r0:r1] = mu

    return result


# ── Background extraction ─────────────────────────────────────────────────────

def _subtract_background(img: np.ndarray, grid: int = 16) -> np.ndarray:
    """
    Fit and subtract a degree-2 2D polynomial background per channel.
    Samples the 20th percentile of each grid cell (avoids stars/galaxy).
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
                y0 = gy * cell_h
                y1 = min((gy + 1) * cell_h, h)
                x0 = gx * cell_w
                x1 = min((gx + 1) * cell_w, w)
                patch = ch[y0:y1, x0:x1].ravel()
                if patch.size == 0:
                    continue
                bg = float(np.percentile(patch, 20))
                ys.append((y0 + y1) * 0.5 / h)
                xs.append((x0 + x1) * 0.5 / w)
                vals.append(bg)

        if len(vals) < 6:
            continue

        ys_   = np.array(ys)
        xs_   = np.array(xs)
        vals_ = np.array(vals)
        A     = np.column_stack([
            np.ones_like(xs_), xs_, ys_,
            xs_**2, xs_ * ys_, ys_**2,
        ])
        try:
            coef, _, _, _ = np.linalg.lstsq(A, vals_, rcond=None)
        except Exception:
            continue

        yy, xx = np.mgrid[0:h, 0:w]
        yy     = yy.astype(np.float32) / h
        xx     = xx.astype(np.float32) / w
        bg_model = (coef[0]
                    + coef[1] * xx + coef[2] * yy
                    + coef[3] * xx**2 + coef[4] * xx * yy
                    + coef[5] * yy**2).astype(np.float32)

        sub = ch - bg_model
        sub -= sub.min()    # floor to 0; no hard clipping of faint detail
        result[:, :, c] = sub

    return result


# ── Colour calibration ────────────────────────────────────────────────────────

def _color_calibrate(img: np.ndarray) -> np.ndarray:
    """
    Two-step colour calibration on a linear float32 (H, W, 3) BGR image.

    Step 1 — Background neutralisation:
      Measure the residual sky background per channel and scale each channel
      so that all three backgrounds match.  This removes colour casts in the sky.

    Step 2 — Star white balance:
      Detect bright, isolated star-like sources using SEP.
      Measure aperture photometry for each star in all three channels.
      Compute the median R/G and B/G ratios across the ensemble and scale
      R and B so that stars appear neutral (white).

    Falls back to B = G = R median normalisation if SEP unavailable.
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
            result[:, :, c] = result[:, :, c] * (bg_mean / bg[c])

    # Step 2: star white balance
    try:
        import sep
        lum = (0.299 * result[:, :, 2]
             + 0.587 * result[:, :, 1]
             + 0.114 * result[:, :, 0]).astype(np.float64)
        lum = np.ascontiguousarray(lum)

        bkg     = sep.Background(lum)
        data    = (lum - bkg.back()).astype(np.float64)
        objects = sep.extract(data, thresh=10.0, err=bkg.globalrms, minarea=9)

        if len(objects) >= 10:
            # Keep top 200 stars by flux, isolated (not flagged)
            mask = (objects['flag'] == 0) & (objects['a'] < 5.0)
            obj  = objects[mask]
            if len(obj) > 200:
                flux_order = np.argsort(obj['flux'])[::-1]
                obj = obj[flux_order[:200]]

            r_ratios, b_ratios = [], []
            radius = 3.0   # aperture radius in pixels
            for o in obj:
                fluxes = []
                for c_idx in range(3):
                    ch     = np.ascontiguousarray(result[:, :, c_idx].astype(np.float64))
                    bk_ch  = sep.Background(ch)
                    ch_sub = ch - bk_ch.back()
                    f, _, _ = sep.sum_circle(ch_sub, [o['x']], [o['y']], radius)
                    fluxes.append(float(f[0]))
                b_val, g_val, r_val = fluxes   # OpenCV BGR order
                if g_val > 0:
                    r_ratios.append(r_val / g_val)
                    b_ratios.append(b_val / g_val)

            if len(r_ratios) >= 5:
                r_med = float(np.median(r_ratios))
                b_med = float(np.median(b_ratios))
                # Scale R and B so their median ratio to G becomes 1.0
                if r_med > 0:
                    result[:, :, 2] /= r_med   # R channel (index 2 in BGR)
                if b_med > 0:
                    result[:, :, 0] /= b_med   # B channel (index 0 in BGR)

    except Exception:
        pass   # leave step-1 result unchanged if SEP unavailable

    # Re-floor to 0 after scaling adjustments
    result = np.clip(result, 0.0, None)
    return result


# ── arcsinh stretch ───────────────────────────────────────────────────────────

def _arcsinh_stretch(img: np.ndarray, softening: float = 0.05) -> np.ndarray:
    """
    Lupton et al. (2004) arcsinh stretch.

    Maps linear [0, 1] → display [0, 1]:
        y = arcsinh(x / β) / arcsinh(1 / β)

    where β = softening sets the transition from linear (x << β) to
    logarithmic (x >> β) behaviour.  Smaller β = stronger stretch.

    The black point is set to the sky background (per-channel median) so
    the sky maps near zero on the display.

    img:   float32 (H, W, 3), any range after background subtraction.
    Returns float32 (H, W, 3) in [0, 1].
    """
    result = np.zeros_like(img, dtype=np.float32)

    for c in range(3):
        ch  = img[:, :, c]
        sky = ch[ch > 0] if (ch > 0).any() else ch

        # Sky background = 5th percentile of non-zero pixels
        bg    = float(np.percentile(sky, 5))
        # White point = 99.8th percentile (leaves 0.2% room for stars)
        white = float(np.percentile(sky, 99.8))
        span  = max(white - bg, 1e-10)

        # Normalise to [0, 1] with bg at 0
        x = np.clip((ch - bg) / span, 0.0, None)

        # arcsinh stretch
        beta  = softening
        norm  = float(np.arcsinh(1.0 / beta))
        y     = np.arcsinh(x / beta) / norm

        result[:, :, c] = np.clip(y, 0.0, 1.0)

    return result


# ── Auto crop ────────────────────────────────────────────────────────────────

def _auto_crop(img: np.ndarray, valid_mask: np.ndarray, margin: int = 16) -> tuple[np.ndarray, np.ndarray]:
    rows = np.where(np.any(valid_mask, axis=1))[0]
    cols = np.where(np.any(valid_mask, axis=0))[0]
    if not rows.size or not cols.size:
        return img, valid_mask
    r0 = min(int(rows[0])  + margin, img.shape[0] - 1)
    r1 = max(int(rows[-1]) - margin, 0)
    c0 = min(int(cols[0])  + margin, img.shape[1] - 1)
    c1 = max(int(cols[-1]) - margin, 0)
    if r1 <= r0 or c1 <= c0:
        return img, valid_mask
    return img[r0:r1, c0:c1], valid_mask[r0:r1, c0:c1]


# ── Minimal FITS writer ───────────────────────────────────────────────────────

def _write_fits(path: str, data: np.ndarray) -> None:
    """
    Write a simple 3-plane FITS (NAXIS=3, BITPIX=-32, float32) without astropy.
    data: float32 (H, W, 3) BGR — we save as RGB for FITS convention.
    """
    from astropy.io import fits
    # Convert BGR → RGB, axis order (H, W, 3) → (3, H, W) for FITS
    rgb  = data[:, :, ::-1].transpose(2, 0, 1).astype(np.float32)
    hdu  = fits.PrimaryHDU(rgb)
    hdu.header['BUNIT']   = 'normalized'
    hdu.header['COLORMOD'] = 'RGB'
    hdu.header['OBJECT']  = 'M51'
    hdu.header['INSTRUME'] = 'Seestar S50'
    hdu.writeto(path, overwrite=True)
    print(f"  Saved linear FITS: {path}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # ── Step 1: Load subs ────────────────────────────────────────────────────
    fits_files = sorted(glob.glob(os.path.join(SUBS_DIR, '*.fit'))
                      + glob.glob(os.path.join(SUBS_DIR, '*.fits')))
    if MAX_FRAMES and len(fits_files) > MAX_FRAMES:
        fits_files = fits_files[:MAX_FRAMES]
    total = len(fits_files)
    print(f"[1] Found {total} FITS subs in {SUBS_DIR}")

    # ── Step 2a: Measure sharpness for reference selection ────────────────────
    print("[2] Measuring sharpness — selecting reference frame ...")
    sharpness = []
    bayer_pattern = BAYER_PATTERN
    for i, fpath in enumerate(fits_files):
        try:
            raw, hdr = _read_fits(fpath)
            if i == 0:
                bayer_pattern = hdr.get('BAYERPAT', BAYER_PATTERN).strip("'").strip()
            sharpness.append(_sharpness(raw))
        except Exception as e:
            print(f"  WARNING: could not read {os.path.basename(fpath)}: {e}")
            sharpness.append(0.0)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{total} sharpness measured")

    ref_idx    = int(np.argmax(sharpness))
    ref_sharp  = sharpness[ref_idx]
    print(f"  Bayer pattern: {bayer_pattern}")
    print(f"  Reference frame: #{ref_idx} ({os.path.basename(fits_files[ref_idx])}, sharpness={ref_sharp:.1f})")

    # ── Step 2b: Register all frames to reference ─────────────────────────────
    ref_raw, _ = _read_fits(fits_files[ref_idx])
    ref_bgr    = _debayer(ref_raw, bayer_pattern).astype(np.float32) / 65535.0
    ref_gray8  = _to_gray8(ref_bgr)
    h, w       = ref_bgr.shape[:2]
    print(f"  Frame size: {w}×{h}")

    frames: list[np.ndarray]  = []   # float32 BGR [0,1]
    frame_paths: list[str]    = []
    valid_masks: list[np.ndarray] = []

    print("[2b] Registering frames ...")
    for fi, fpath in enumerate(fits_files):
        try:
            raw, _ = _read_fits(fpath)
            bgr    = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
            if fi == ref_idx:
                frames.append(bgr)
                valid_masks.append(np.ones((h, w), dtype=bool))
                frame_paths.append(fpath)
            else:
                gray8  = _to_gray8(bgr)
                warp   = _register(ref_gray8, gray8)
                if warp is None:
                    print(f"  SKIP: registration failed for frame {fi}")
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
                valid_masks.append(valid)
                frame_paths.append(fpath)
        except Exception as e:
            print(f"  SKIP: frame {fi} error: {e}")
            continue

        if (fi + 1) % 10 == 0:
            print(f"  {fi + 1}/{total} registered ({len(frames)} accepted so far)")

    n_accepted = len(frames)
    print(f"  Registered {n_accepted}/{total} frames")

    if n_accepted < 3:
        sys.exit("ERROR: fewer than 3 frames registered — aborting")

    # ── Step 3: Subframe weighting ────────────────────────────────────────────
    print("[3] Computing subframe weights (FWHM, background, eccentricity) ...")
    raw_weights = []
    metrics_list = []
    for i, (bgr, fpath) in enumerate(zip(frames, frame_paths)):
        m = _frame_metrics(bgr)
        w = _compute_weight(m)
        raw_weights.append(w)
        metrics_list.append(m)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{n_accepted}  FWHM={m['fwhm']:.2f}px  ecc={m['eccentricity']:.3f}  bg={m['background']:.5f}  w={w:.6f}")

    weights = np.array(raw_weights, dtype=np.float32)
    weights /= weights.sum()   # normalise
    print(f"  Weight range: {weights.min():.4f} – {weights.max():.4f}")
    print(f"  Effective N frames (1/sum(w²)): {1.0 / float((weights**2).sum()):.1f}")

    # ── Step 4: Weighted sigma-clip integration ───────────────────────────────
    print(f"[4] Weighted sigma-clip stack (σ_low=2.0, σ_high=3.0, N={n_accepted}) ...")
    stack_arr = np.stack(frames, axis=0)   # (N, H, W, 3)
    del frames

    stacked = _weighted_sigma_clip(stack_arr, weights, sigma_low=2.0, sigma_high=3.0)
    del stack_arr

    print(f"  Stack shape: {stacked.shape}, mean={stacked.mean():.5f}, std={stacked.std():.5f}")

    # Valid-pixel mask: intersection of all valid regions
    all_valid = valid_masks[0].copy()
    for m in valid_masks[1:]:
        all_valid &= m
    coverage = all_valid.mean() * 100
    print(f"  Valid coverage: {coverage:.1f}%")

    # ── Step 5: Background extraction ────────────────────────────────────────
    print("[5] Background extraction (16×16 grid, degree-2 polynomial) ...")
    stacked = _subtract_background(stacked, grid=16)
    print(f"  Post-BG stack: mean={stacked.mean():.5f}, std={stacked.std():.5f}")

    # ── Crop to valid overlap region ──────────────────────────────────────────
    stacked, all_valid = _auto_crop(stacked, all_valid)
    print(f"  After crop: {stacked.shape[1]}×{stacked.shape[0]}")

    # ── Step 6: Colour calibration ────────────────────────────────────────────
    print("[6] Colour calibration (background neutralise + star white balance) ...")
    stacked = _color_calibrate(stacked)

    # ── Save linear FITS ──────────────────────────────────────────────────────
    print("[8a] Saving linear (pre-stretch) FITS ...")
    _write_fits(OUT_LINEAR, stacked)

    # ── Step 7: arcsinh stretch ───────────────────────────────────────────────
    print("[7] arcsinh stretch (softening=0.05) ...")
    stretched = _arcsinh_stretch(stacked, softening=0.05)
    print(f"  Stretched: mean={stretched.mean():.3f}, std={stretched.std():.3f}")

    # ── Step 8b: Save final JPEG ──────────────────────────────────────────────
    print("[8b] Saving final JPEG ...")
    out_u8 = (stretched * 255).clip(0, 255).astype(np.uint8)
    ok = cv2.imwrite(OUT_FINAL, out_u8, [cv2.IMWRITE_JPEG_QUALITY, 97])
    if not ok:
        sys.exit(f"ERROR: failed to write {OUT_FINAL}")
    print(f"  Saved: {OUT_FINAL}  ({out_u8.shape[1]}×{out_u8.shape[0]})")

    print("\nDone.")


if __name__ == '__main__':
    main()
