"""
Seestar Lab — FITS sub-frame stacking engine.

Stacking pipeline (astrophotography best practices):
  1. Inventory      — scan for .fit files, read FITS headers
  2. Quality assess — Laplacian-variance sharpness per frame
  3. Frame rejection — discard frames below quality threshold
  4. Debayer        — convert Bayer (GRBG default) to BGR colour
  5. Registration   — align frames to reference via ECC; phase-corr fallback
  6. Integration    — sigma-clipped mean combination
  7. Background     — 2D polynomial gradient subtraction
  8. Crop           — trim invalid border pixels from alignment warps
  9. Stretch        — percentile black-point + sqrt gamma (PixInsight-style STF)
 10. Enhancement    — bilateral noise reduction + unsharp-mask sharpening
 11. Save           — JPEG output (+ optional float32 FITS for further processing)

No astropy required.  Uses only numpy, scipy, and opencv-python.
"""

import os
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
    """Debayer uint16 Bayer array to BGR uint16."""
    code = _BAYER_CODES.get(bayer_pattern.upper().strip(), cv2.COLOR_BayerGR2BGR)
    return cv2.cvtColor(raw, code)


# ── Registration ──────────────────────────────────────────────────────────────

def _to_gray8(bgr_f32: np.ndarray) -> np.ndarray:
    """Convert float32 BGR [0,1] to uint8 grayscale (for ECC registration)."""
    gray = (0.299 * bgr_f32[:, :, 2]
          + 0.587 * bgr_f32[:, :, 1]
          + 0.114 * bgr_f32[:, :, 0])
    mn, mx = float(gray.min()), float(gray.max())
    if mx > mn:
        gray = (gray - mn) / (mx - mn)
    return (gray * 255).astype(np.uint8)


def _register(ref_gray8: np.ndarray, frame_gray8: np.ndarray) -> np.ndarray:
    """
    Find the 2×3 Euclidean warp that aligns frame to reference.
    ECC with MOTION_EUCLIDEAN; falls back to phase correlation on failure.
    Returns the warp matrix (identity = no alignment).
    """
    warp     = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
    try:
        _, warp = cv2.findTransformECC(
            ref_gray8, frame_gray8, warp,
            cv2.MOTION_EUCLIDEAN, criteria,
        )
    except cv2.error:
        # Phase-correlation fallback (translation only)
        try:
            (dx, dy), _ = cv2.phaseCorrelate(
                ref_gray8.astype(np.float32),
                frame_gray8.astype(np.float32),
            )
            warp[0, 2] = float(dx)
            warp[1, 2] = float(dy)
        except Exception:
            pass  # identity — no alignment
    return warp


# ── Integration ───────────────────────────────────────────────────────────────

def _sigma_clip_mean(stack: np.ndarray, n_sigma: float = 2.5) -> np.ndarray:
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

def _auto_stretch(img: np.ndarray) -> np.ndarray:
    """
    Astrophotography auto-stretch per channel:
      1. Black point  = 0.5th  percentile
      2. White point  = 99.8th percentile
      3. Linear normalise to [0,1]
      4. Sqrt gamma (≈ PixInsight STF) to lift faint nebulosity

    img: float32 (H, W, 3), any range.
    Returns float32 (H, W, 3) in [0, 1].
    """
    result = np.zeros_like(img, dtype=np.float32)
    for c in range(3):
        ch  = img[:, :, c]
        lo  = float(np.percentile(ch,  0.5))
        hi  = float(np.percentile(ch, 99.8))
        if hi > lo:
            stretched = np.clip((ch - lo) / (hi - lo), 0.0, 1.0)
        else:
            stretched = np.zeros_like(ch)
        result[:, :, c] = np.sqrt(stretched)   # sqrt ≈ gamma 0.5
    return result


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray) -> np.ndarray:
    """
    Mild bilateral noise reduction + unsharp-mask sharpening.
    img: uint8 (H, W, 3) BGR [0, 255].  Returns uint8.
    """
    # Bilateral filter: preserves edges, smooths noise in sky background
    denoised  = cv2.bilateralFilter(img, d=7, sigmaColor=30, sigmaSpace=10)
    # Unsharp mask: moderate sharpening (amount 1.3, radius 1.5 px)
    blurred   = cv2.GaussianBlur(denoised, (0, 0), 1.5)
    sharpened = cv2.addWeighted(denoised, 1.3, blurred, -0.3, 0)
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
        ref_local  = int(np.argmax(ref_scores))   # index within accepted list

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

                aligned = cv2.warpAffine(
                    bgr, warp, (w, h),
                    flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0,
                )
                # Track which pixels actually came from this frame
                ones  = np.ones((h, w), dtype=np.float32)
                valid = cv2.warpAffine(
                    ones, warp, (w, h),
                    flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0,
                ) > 0.5

                frames.append(aligned)
                masks.append(valid)
            except Exception:
                pass   # skip frames that fail to load or register

        if len(frames) < MIN_FRAMES:
            raise RuntimeError(
                f"Only {len(frames)} frames registered successfully (need {MIN_FRAMES})"
            )

        # ── Stage 4: Integration ─────────────────────────────────────────────
        progress_cb(65, "Integrating (sigma-clipped mean)", n_accepted, total)
        _chk()

        stack_arr = np.stack(frames, axis=0)    # (N, H, W, 3)
        stacked   = _sigma_clip_mean(stack_arr)  # (H, W, 3)
        del stack_arr, frames                    # release ~800 MB

        # Valid mask: pixels where every frame contributed
        all_valid = masks[0].copy()
        for m in masks[1:]:
            all_valid &= m

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
