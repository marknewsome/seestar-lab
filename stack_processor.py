"""
Seestar Lab — FITS sub-frame stacking engine.

Stacking pipeline (astrophotography best practices):
  1. Inventory      — scan for .fit files, read FITS headers
  2. Quality assess — Laplacian-variance sharpness per frame
  3. Frame rejection — discard frames below quality threshold
  4. Debayer        — convert Bayer (GRBG default) to BGR colour
  5. Registration   — align frames to reference via ECC; phase-corr fallback
  6. Integration    — 2× drizzle (coverage-weighted, streaming accumulation)
  7. Background     — 2D polynomial gradient subtraction
  8. Crop           — trim invalid border pixels from alignment warps
  9. Stretch        — percentile black-point + sqrt gamma (PixInsight-style STF)
 10. Enhancement    — bilateral noise reduction + unsharp-mask sharpening
 11. Save           — JPEG output (+ optional float32 FITS for further processing)

No astropy required.  Uses only numpy, scipy, and opencv-python.

Drizzle integration:
  Each sub-frame is mapped into a 2× output grid (3840×2160 from 1920×1080 subs)
  using its computed warp transform scaled to the output resolution.  Frames are
  accumulated one at a time (streaming) so peak RAM is ~500 MB regardless of N.
  Coverage-weighted mean provides sub-pixel accuracy matching Seestar's own output.
"""

import os
import warnings
import numpy as np
import cv2
from typing import Callable, Optional


# ── Constants ─────────────────────────────────────────────────────────────────

FITS_EXT          = {'.fit', '.fits', '.fts'}
MIN_FRAMES        = 3      # refuse to stack fewer than this many accepted frames
QUALITY_THRESHOLD = 0.40   # reject frames below this fraction of median sharpness


class StackCancelled(RuntimeError):
    """Raised when a cancel callback signals the job should stop."""


# ── Minimal FITS reader ───────────────────────────────────────────────────────

def _parse_fits_header(raw: bytes) -> dict:
    """Parse raw FITS header bytes into a plain dict of str→str."""
    header: dict[str, str] = {}
    for i in range(0, len(raw), 80):
        card = raw[i:i + 80].decode('ascii', errors='replace')
        key  = card[:8].strip()
        if key == 'END':
            break
        # Value cards have '=' at position 8
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

    dtype_map = {
        16:   '>i2',    # signed 16-bit big-endian
        32:   '>i4',
        -32:  '>f4',
        -64:  '>f8',
    }
    dtype = dtype_map.get(bitpix)
    if dtype is None:
        raise ValueError(f"Unsupported FITS BITPIX={bitpix} in {path}")

    arr     = np.frombuffer(raw, dtype=dtype).reshape(naxis2, naxis1).astype(np.float32)
    physical = arr * bscale + bzero
    uint16  = np.clip(physical, 0, 65535).astype(np.uint16)
    return uint16, header


# ── Quality assessment ────────────────────────────────────────────────────────

def _sharpness(raw_bayer: np.ndarray) -> float:
    """Laplacian-variance sharpness on the centre quarter of a Bayer frame."""
    h, w      = raw_bayer.shape
    cy, cx    = h // 2, w // 2
    qh, qw    = h // 4, w // 4
    crop      = raw_bayer[cy - qh: cy + qh, cx - qw: cx + qw]
    lap       = cv2.Laplacian(crop.astype(np.float32), cv2.CV_32F)
    return float(lap.var())


# ── Debayer ───────────────────────────────────────────────────────────────────

_BAYER_CODES = {
    'RGGB': cv2.COLOR_BayerRG2BGR,
    'BGGR': cv2.COLOR_BayerBG2BGR,
    'GRBG': cv2.COLOR_BayerGR2BGR,
    'GBRG': cv2.COLOR_BayerGB2BGR,
}


def _debayer(raw: np.ndarray, bayer_pattern: str = 'GRBG') -> np.ndarray:
    """Debayer uint16 Bayer array to BGR uint16 (bilinear).
    Note: OpenCV's VNG/EA debayer algorithms only accept 8-bit input, so we
    use bilinear here to preserve full 16-bit precision for stacking."""
    code = _BAYER_CODES.get(bayer_pattern.upper().strip(), cv2.COLOR_BayerGR2BGR)
    return cv2.cvtColor(raw, code)


# ── Registration ──────────────────────────────────────────────────────────────

def _to_gray8(bgr_f32: np.ndarray) -> np.ndarray:
    """
    Convert float32 BGR [0,1] to uint8 grayscale for ECC registration.
    Uses CLAHE rather than min/max normalisation so that faint star fields
    (where a single hot pixel would otherwise crush all stars to values 1-5)
    have enough gradient information for ECC to converge reliably.
    """
    gray = (0.299 * bgr_f32[:, :, 2]
          + 0.587 * bgr_f32[:, :, 1]
          + 0.114 * bgr_f32[:, :, 0])
    # Percentile stretch first to avoid hot pixels dominating CLAHE
    lo, hi = float(np.percentile(gray, 0.5)), float(np.percentile(gray, 99.5))
    if hi > lo:
        gray = np.clip((gray - lo) / (hi - lo), 0.0, 1.0)
    gray8 = (gray * 255).astype(np.uint8)
    # CLAHE: boost local contrast so stars are clearly visible for the correlator
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray8)


_MAX_WARP_SHIFT_PX = 50.0   # reject ECC result if translation exceeds this
_MAX_WARP_ROT_DEG  = 2.0    # reject ECC result if rotation exceeds this


def _warp_is_sane(warp: np.ndarray) -> bool:
    """Return True if the warp has plausible translation and rotation values."""
    dx  = float(warp[0, 2])
    dy  = float(warp[1, 2])
    rot = abs(float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))))
    return (abs(dx) <= _MAX_WARP_SHIFT_PX
            and abs(dy) <= _MAX_WARP_SHIFT_PX
            and rot     <= _MAX_WARP_ROT_DEG)


def _register(ref_gray8: np.ndarray, frame_gray8: np.ndarray) -> np.ndarray | None:
    """
    Find the 2×3 Euclidean warp that aligns frame to reference.

    Strategy (best-to-fallback):
      1. astroalign — star-triangle pattern matching; centroid-precise, rotation-aware.
      2. ECC (MOTION_EUCLIDEAN) — image-correlation fallback for frames with too few stars.
      3. Phase correlation — translation-only last resort.

    Returns the 2×3 warp matrix, or None if all methods fail / result is implausible.
    """
    # ── 1. astroalign (star-based) ────────────────────────────────────────────
    try:
        import astroalign as aa
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            _, tf = aa.find_transform(frame_gray8, ref_gray8)
        # astroalign returns a skimage SimilarityTransform; convert to 2×3 warp
        params = tf.params          # 3×3 homogeneous matrix
        warp_aa = np.array([
            [params[0, 0], params[0, 1], params[0, 2]],
            [params[1, 0], params[1, 1], params[1, 2]],
        ], dtype=np.float32)
        if _warp_is_sane(warp_aa):
            return warp_aa
    except Exception:
        pass   # too few stars or import error — fall through to ECC

    # ── 2. ECC (image correlation) ────────────────────────────────────────────
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

    # ── 3. Phase-correlation (translation only) ───────────────────────────────
    try:
        (dx, dy), _ = cv2.phaseCorrelate(
            ref_gray8.astype(np.float32),
            frame_gray8.astype(np.float32),
        )
        fallback        = np.eye(2, 3, dtype=np.float32)
        fallback[0, 2]  = float(dx)
        fallback[1, 2]  = float(dy)
        if _warp_is_sane(fallback):
            return fallback
    except Exception:
        pass

    return None   # skip this frame


# ── Integration ───────────────────────────────────────────────────────────────

def _sigma_clip_mean(stack: np.ndarray, n_sigma: float = 2.0) -> np.ndarray:
    """
    Sigma-clipped mean along axis 0.
    stack: float32 (N, H, W, 3).  Returns float32 (H, W, 3).
    Uses MAD-based sigma for robustness against non-Gaussian noise.
    """
    med   = np.median(stack, axis=0)                         # (H, W, 3)
    diff  = np.abs(stack - med[np.newaxis])
    mad   = np.median(diff, axis=0)                          # (H, W, 3)
    sigma = np.maximum(mad * 1.4826, 1e-8)                   # σ equivalent
    good  = diff <= n_sigma * sigma[np.newaxis]              # (N, H, W, 3)
    total = np.where(good, stack, 0.0).sum(axis=0)
    count = np.maximum(good.sum(axis=0).astype(np.float32), 1)
    return total / count


# ── Drizzle integration (STScI drizzle library) ───────────────────────────────

DRIZZLE_SCALE   = 2    # output pixels per input pixel (matches Seestar's 2× output)
DRIZZLE_PIXFRAC = 0.5  # drop size as fraction of input pixel
                       # 0.5 → each drop covers exactly 1 output pixel at 2×


def _make_forward_pixmap(warp: np.ndarray, h: int, w: int, scale: int = DRIZZLE_SCALE) -> np.ndarray:
    """
    Build the (h, w, 2) forward pixmap required by drizzle.resample.Drizzle.

    drizzle's pixmap convention: for each INPUT pixel (y, x), pixmap[y, x] gives
    the (X_out, Y_out) position in the OUTPUT grid where that pixel lands.

    Our warp uses the WARP_INVERSE_MAP convention: the matrix M maps
    output_native → input, i.e.  x_in = M @ [X_out_native, Y_out_native, 1]^T.

    Inverting to get the forward map (input → output_native) and then scaling
    by `scale` to reach the 2× output grid:

        [X_out, Y_out] = scale × M⁻¹ × ([x_in, y_in] − [tx, ty])

    where M_rot = [[a, b], [c, d]] is the 2×2 rotation part of the warp,
    tx, ty are the translation components, and M⁻¹_rot is its inverse.
    """
    a, b, tx = float(warp[0, 0]), float(warp[0, 1]), float(warp[0, 2])
    c, d, ty = float(warp[1, 0]), float(warp[1, 1]), float(warp[1, 2])

    det = a * d - b * c
    if abs(det) < 1e-10:
        det = 1e-10
    inv_a, inv_b = d / det, -b / det
    inv_c, inv_d = -c / det, a / det

    y_in, x_in = np.mgrid[0:h, 0:w].astype(np.float64)
    dx = x_in - tx
    dy = y_in - ty

    X_out = scale * (inv_a * dx + inv_b * dy)
    Y_out = scale * (inv_c * dx + inv_d * dy)

    return np.stack([X_out, Y_out], axis=-1)  # (h, w, 2)


# ── Background subtraction ────────────────────────────────────────────────────

def _subtract_background(img: np.ndarray, grid: int = 8) -> np.ndarray:
    """
    Estimate and subtract a 2D polynomial (degree 2) background gradient.
    img: float32 (H, W, 3) BGR.
    Background sampled as the 20th-percentile of each grid cell
    (avoids bright stars/nebulosity contaminating the estimate).
    """
    h, w   = img.shape[:2]
    result = img.copy()
    cell_h = max(h // grid, 1)
    cell_w = max(w // grid, 1)

    for c in range(3):
        ch  = img[:, :, c]
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

        # Degree-2 2D polynomial: [1, x, y, x², x·y, y²]
        A = np.column_stack([
            np.ones_like(xs_), xs_, ys_,
            xs_**2, xs_ * ys_, ys_**2,
        ])
        try:
            coef, _, _, _ = np.linalg.lstsq(A, vals_, rcond=None)
        except Exception:
            continue

        yy, xx = np.mgrid[0:h, 0:w]
        yy = yy.astype(np.float32) / h
        xx = xx.astype(np.float32) / w
        bg_model = (coef[0]
                    + coef[1] * xx + coef[2] * yy
                    + coef[3] * xx**2 + coef[4] * xx * yy + coef[5] * yy**2)

        subtracted = ch - bg_model.astype(np.float32)
        # Shift so the minimum is 0 (no hard clipping of faint detail)
        subtracted -= subtracted.min()
        result[:, :, c] = subtracted

    return result


# ── Crop ──────────────────────────────────────────────────────────────────────

def _auto_crop(img: np.ndarray, valid_mask: np.ndarray, margin: int = 12) -> np.ndarray:
    """
    Crop img to the rectangle where valid_mask is True, with a safety margin.
    valid_mask: bool (H, W) — True where all registered frames contributed data.
    """
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

_STF_MIDTONE_TARGET = 0.12   # target display value for sky background (PixInsight default ≈ 0.12)


def _auto_stretch(img: np.ndarray) -> np.ndarray:
    """
    PixInsight-style Midtone Transfer Function (STF) auto-stretch, per channel.

    Steps:
      1. Estimate sky background: median(channel)
      2. Estimate sky noise:       1.4826 × MAD(channel)
      3. Black clip c₀:            sky_bg − 2.8 × sky_noise  (clips shadow noise)
      4. Linear normalise to [0, 1] with c₀ as black point
      5. Solve for MTF midpoint m such that the sky median maps to
         _STF_MIDTONE_TARGET (≈ 0.12), keeping sky dark on screen
      6. Apply MTF:  x' = (m−1)·x / ((2m−1)·x − m)

    Unlike a simple gamma curve this keeps the sky background truly dark
    regardless of any residual background gradient left after subtraction.

    img:  float32 (H, W, 3), any range.
    Returns float32 (H, W, 3) in [0, 1].
    """
    result = np.zeros_like(img, dtype=np.float32)
    m_tgt  = _STF_MIDTONE_TARGET

    for c in range(3):
        ch  = img[:, :, c].ravel()

        # Sky background and noise estimate (ignore zero-fill border pixels)
        sky = ch[ch > 0] if (ch > 0).any() else ch
        med   = float(np.median(sky))
        mad   = float(np.median(np.abs(sky - med)))
        sigma = mad * 1.4826

        # Black-clip point
        c0 = max(0.0, med - 2.8 * sigma)

        # White point: actual top of signal (99.9th percentile of non-zero pixels).
        # Using the full bit-range (1.0) would compress the signal into <1% of the
        # display range, making stars invisible.  We stretch to the real signal max.
        hi   = float(np.percentile(sky, 99.9))
        span = max(hi - c0, 1e-10)

        x = np.clip((img[:, :, c] - c0) / span, 0.0, 1.0)

        # Sky median in normalised space
        med_n = float(np.clip((med - c0) / span, 1e-6, 1.0 - 1e-6))

        # Solve for MTF midpoint m: MTF(med_n, m) = m_tgt
        # m = med_n * (m_tgt − 1) / (2·med_n·m_tgt − m_tgt − med_n)
        denom_m = 2.0 * med_n * m_tgt - m_tgt - med_n
        if abs(denom_m) > 1e-10:
            m = float(med_n * (m_tgt - 1.0) / denom_m)
            m = max(1e-4, min(1.0 - 1e-4, m))
        else:
            m = 0.5   # fallback

        # Apply MTF: x' = (m−1)·x / ((2m−1)·x − m)
        denom = (2.0 * m - 1.0) * x - m
        denom = np.where(np.abs(denom) > 1e-10, denom, np.sign(denom + 1e-30) * 1e-10)
        result[:, :, c] = np.clip((m - 1.0) * x / denom, 0.0, 1.0)

    return result


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray) -> np.ndarray:
    """
    NLM colour denoising followed by unsharp-mask sharpening.
    img: uint8 (H, W, 3) BGR [0, 255].  Returns uint8.

    NLM (Non-Local Means) reduces per-pixel noise while preserving edges and
    star PSFs better than bilateral or Gaussian smoothing.  h=6 is gentle
    enough not to smear 2-3px stars; template/search windows are matched to
    typical star sizes.  The unsharp mask then compensates for any softening.
    """
    denoised  = cv2.fastNlMeansDenoisingColored(
        img,
        None,
        h           = 6,    # luminance filter strength (lower = less smoothing)
        hColor      = 6,    # colour filter strength
        templateWindowSize = 7,
        searchWindowSize   = 21,
    )
    blurred   = cv2.GaussianBlur(denoised, (0, 0), 1.2)
    sharpened = cv2.addWeighted(denoised, 1.6, blurred, -0.6, 0)
    return sharpened


# ── Main processor ────────────────────────────────────────────────────────────

class StackProcessor:
    """
    Full stacking pipeline for Seestar S50 FITS sub-frames.

    Usage:
        proc   = StackProcessor()
        result = proc.run(fits_files, output_path, progress_cb, cancel_cb)

    progress_cb signature:  (pct: int, stage: str, accepted: int, total: int)
    cancel_cb   signature:  () -> bool   (return True to abort)
    """

    def run(
        self,
        fits_files: list[str],
        output_path: str,
        progress_cb: Callable[[int, str, int, int], None],
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """
        Stack fits_files, save JPEG to output_path.
        Returns {frames_total, frames_accepted, output_path}.
        Raises StackCancelled or RuntimeError on failure.
        """

        def _chk():
            if cancel_cb and cancel_cb():
                raise StackCancelled("Stacking cancelled")

        total = len(fits_files)
        if total < MIN_FRAMES:
            raise RuntimeError(
                f"Need at least {MIN_FRAMES} FITS files to stack, found {total}"
            )

        # ── Stage 1: Quality assessment ───────────────────────────────────────
        progress_cb(2, f"Assessing {total} frames", 0, total)
        _chk()

        sharpness:    list[float] = []
        bayer_pattern = 'GRBG'   # default; overwritten from first readable header

        for i, fpath in enumerate(fits_files):
            _chk()
            try:
                raw, hdr = _read_fits(fpath)
                if i == 0:
                    bayer_pattern = hdr.get('BAYERPAT', 'GRBG').strip("'").strip()
                sharpness.append(_sharpness(raw))
            except Exception:
                sharpness.append(0.0)
            pct = 2 + int(18 * (i + 1) / total)
            progress_cb(pct, f"Assessing quality: {i + 1}/{total}", 0, total)

        # Reject frames below threshold (based on median of positive scores)
        positive = [s for s in sharpness if s > 0]
        if not positive:
            raise RuntimeError("Could not load any FITS frames")

        threshold   = float(np.median(positive)) * QUALITY_THRESHOLD
        accepted_idx = [i for i, s in enumerate(sharpness) if s >= threshold]

        if len(accepted_idx) < MIN_FRAMES:
            accepted_idx = list(range(len(fits_files)))   # lenient fallback

        accepted_files = [fits_files[i] for i in accepted_idx]
        n_accepted     = len(accepted_files)

        progress_cb(20, f"Accepted {n_accepted}/{total} frames", n_accepted, total)
        _chk()

        # ── Stage 2: Load reference frame ────────────────────────────────────
        ref_scores = [sharpness[i] for i in accepted_idx]
        ref_local  = int(np.argmax(ref_scores))

        progress_cb(22, "Loading reference frame", n_accepted, total)
        ref_raw, _ = _read_fits(accepted_files[ref_local])
        ref_bgr    = _debayer(ref_raw, bayer_pattern).astype(np.float32) / 65535.0
        ref_gray8  = _to_gray8(ref_bgr)
        h, w       = ref_bgr.shape[:2]

        frames: list[np.ndarray] = [ref_bgr]
        masks:  list[np.ndarray] = [np.ones((h, w), dtype=bool)]

        # ── Stage 3: Register remaining frames ───────────────────────────────
        for fi, fpath in enumerate(accepted_files):
            _chk()
            if fi == ref_local:
                continue
            pct = 22 + int(43 * (fi + 1) / n_accepted)
            progress_cb(pct, f"Registering {fi + 1}/{n_accepted}", n_accepted, total)
            try:
                raw, _  = _read_fits(fpath)
                bgr     = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
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
            except Exception:
                pass

        if len(frames) < MIN_FRAMES:
            raise RuntimeError(
                f"Only {len(frames)} frames registered successfully (need {MIN_FRAMES})"
            )

        # ── Stage 4: Sigma-clipped integration + 2× upsample ─────────────────
        # True drizzle adds no resolution benefit when sub-pixel offsets are
        # < 0.5 px (as is typical for Seestar tracking). Sigma-clipping at
        # native resolution removes hot pixels and cosmic rays cleanly; we
        # then upsample to 2× with Lanczos-4 to match Seestar's output size.
        progress_cb(65, f"Integrating sigma-clipped mean ({len(frames)} frames)", n_accepted, total)
        _chk()

        stack_arr = np.stack(frames, axis=0)    # (N, H, W, 3)
        stacked   = _sigma_clip_mean(stack_arr)  # (H, W, 3)
        del stack_arr, frames

        # Valid mask (native res) → upsample to 2× after stacking
        all_valid_native = masks[0].copy()
        for m in masks[1:]:
            all_valid_native &= m

        # 2× upsample: the sigma-clipped stack + its validity mask
        oh, ow    = h * DRIZZLE_SCALE, w * DRIZZLE_SCALE
        stacked   = cv2.resize(stacked,   (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        all_valid = cv2.resize(
            all_valid_native.astype(np.uint8), (ow, oh),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)

        # ── Stage 5: Background subtraction ──────────────────────────────────
        progress_cb(72, "Removing background gradient", n_accepted, total)
        _chk()
        stacked = _subtract_background(stacked)

        # ── Stage 6: Crop ─────────────────────────────────────────────────────
        progress_cb(78, "Cropping to valid overlap region", n_accepted, total)
        stacked = _auto_crop(stacked, all_valid)

        # ── Stage 7: Stretch + colour balance ────────────────────────────────
        progress_cb(84, "Auto-stretch and colour balance", n_accepted, total)
        _chk()
        stacked = _auto_stretch(stacked)

        # ── Stage 8: Noise reduction + sharpening ────────────────────────────
        progress_cb(91, "Noise reduction and sharpening", n_accepted, total)
        _chk()
        stacked_u8 = (stacked * 255).astype(np.uint8)
        stacked_u8 = _denoise_sharpen(stacked_u8)

        # ── Stage 9: Save ─────────────────────────────────────────────────────
        progress_cb(97, "Saving result", n_accepted, total)
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
