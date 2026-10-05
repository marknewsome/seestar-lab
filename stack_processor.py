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
  4. Siril stack       — preferred path when Siril CLI is installed: RCD demosaic
                         (convert -debayer), 2-pass registration, FWHM-weighted
                         winsorized sigma-clip stack with channel equalisation.
                         Steps 4–5 below are the fallback when Siril is missing.
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

import gc
import json
import logging
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

# Siril's multi-file `convert` opens every frame at once and aborts with
# "Max number of opened files (8192) is larger than required number of images"
# past this count.  `-fitseq` would avoid it but its register step is broken
# in Siril 1.4.3 (see the note in _siril_full_stack), so this is a hard
# ceiling.  Clamp here rather than discovering it after the multi-hour copy
# and scoring phases have already run.
SIRIL_MAX_OPEN_FILES = 8192
SIRIL_FRAME_LIMIT    = 8000   # headroom under the ceiling for Siril's own temps


class StackCancelled(RuntimeError):
    """Raised when a cancel callback signals the job should stop."""


def _kill_windows_siril() -> None:
    """Kill any siril-cli.exe left running on the Windows side.

    Siril is invoked through WSL interop, so Python's subprocess timeout only
    terminates the local /init shim; the Windows process survives and keeps
    writing into the work dir we are about to delete.
    """
    import subprocess, logging
    try:
        subprocess.run(
            ["/mnt/c/Windows/System32/taskkill.exe", "/IM", "siril-cli.exe", "/F"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        logging.warning(f"Could not kill orphaned siril-cli.exe: {exc}")


def _siril_error_lines(stdout: str, context: int = 12) -> str:
    """Pull the actual error lines out of Siril's very chatty stdout.

    Siril logs a line per frame ("HDU 1234: type=0, ..."), so a plain tail of
    the output buries the real failure under thousands of progress lines.
    """
    lines = stdout.splitlines()
    hits  = [i for i, ln in enumerate(lines)
             if any(k in ln for k in ('rror', 'ailed', 'Could not', 'Warning:',
                                      'not found', 'Exiting'))]
    if not hits:
        return f"stdout (tail): {stdout[-800:]}\n"
    keep, seen = [], set()
    for i in hits[-context:]:
        for j in range(max(0, i - 1), min(len(lines), i + 2)):
            if j not in seen:
                seen.add(j)
                keep.append(lines[j])
    return "stdout (error lines):\n" + "\n".join(keep) + "\n"


def _copy_file_no_sendfile(src: str, dst: str, bufsize: int = 4 * 1024 * 1024) -> None:
    """Chunked read/write copy, bypassing os.sendfile() and file metadata.

    shutil.copy2/copyfile use sendfile() on Linux, which has been observed to
    raise ENOMEM ("Cannot allocate memory") when either side of the copy is a
    WSL2 9p/drvfs mount (e.g. a Windows drive under /mnt/<letter>) — a known
    category of WSL2 9p driver issue, not an actual low-memory condition.
    Deliberately skips shutil.copystat(): chmod/utime on a 9p-mounted
    destination can raise PermissionError, and these are throwaway working
    copies that don't need preserved permissions or timestamps.
    """
    with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
        while True:
            buf = fsrc.read(bufsize)
            if not buf:
                break
            fdst.write(buf)


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


def _read_fits_header(path: str) -> dict:
    """Read only a FITS file's header, without loading the pixel payload.

    Much cheaper than _read_fits() when only metadata (e.g. CCD-TEMP,
    DATE-OBS) is needed — avoids reading/allocating the ~4MB pixel array
    per frame, which matters when sampling many files across a session.
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
    return _parse_fits_header(raw_header)


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


def score_session_frames(
    fits_files:  list[str],
    progress_cb: Optional[Callable[[int, str], None]] = None,
    cancel_cb:   Optional[Callable[[], bool]] = None,
) -> list[dict]:
    """
    Score every frame in a session with the same Stage A / Stage B metrics the
    real stacking pipeline uses, without copying to local disk or stacking
    anything.  Read-only pass over the source files — safe to run concurrently
    with an actual stack job (though I/O will contend on spinning/network
    drives).

    Returns one dict per input file, in the same order as fits_files:
      {file, sharpness, stage_a_pass, fwhm, eccentricity, star_count, snr,
       score, error}
    'error' is set (and other fields are None/0) if the file couldn't be read
    or scored.  Frames are NOT capped by max_frames or min_quality here — this
    is the full distribution so the caller can pick those cutoffs visually.
    """

    def _chk():
        if cancel_cb and cancel_cb():
            raise StackCancelled("Scoring cancelled")

    total = len(fits_files)
    results: list[dict] = []
    sharpness: list[float] = []
    bayer_pattern = 'GRBG'

    for i, fpath in enumerate(fits_files):
        _chk()
        row = {
            'file': os.path.basename(fpath), 'sharpness': 0.0,
            'stage_a_pass': False, 'fwhm': None, 'eccentricity': None,
            'star_count': 0, 'snr': None, 'score': 0.0, 'error': None,
        }
        try:
            raw, hdr = _read_fits(fpath)
            if i == 0:
                bayer_pattern = hdr.get('BAYERPAT', 'GRBG').strip("'").strip()
            row['sharpness'] = _sharpness(raw)
        except Exception as exc:
            row['error'] = str(exc) or type(exc).__name__
        results.append(row)
        sharpness.append(row['sharpness'] if row['error'] is None else 0.0)
        if progress_cb:
            progress_cb(int(60 * (i + 1) / total), f"Stage A: {i + 1}/{total}")

    positive  = [s for s in sharpness if s > 0]
    threshold = float(np.median(positive)) * QUALITY_THRESHOLD if positive else 0.0

    for i, row in enumerate(results):
        if row['error'] is None:
            row['stage_a_pass'] = sharpness[i] >= threshold

    stage_a_indices = [i for i, row in enumerate(results) if row['stage_a_pass']]
    for bi, i in enumerate(stage_a_indices):
        _chk()
        row = results[i]
        try:
            raw, _ = _read_fits(fits_files[i])
            bgr     = _debayer(raw, bayer_pattern).astype(np.float32) / 65535.0
            metrics = _frame_metrics(bgr)
            row['fwhm']         = metrics['fwhm']
            row['eccentricity'] = metrics['eccentricity']
            row['star_count']   = metrics['star_count']
            row['snr']          = metrics['snr']
            row['score']        = _quality_score(metrics)
        except Exception as exc:
            row['error'] = str(exc) or type(exc).__name__
        if progress_cb:
            progress_cb(60 + int(40 * (bi + 1) / max(len(stage_a_indices), 1)),
                        f"Stage B: {bi + 1}/{len(stage_a_indices)}")

    return results


# ── Sensor temperature survey ──────────────────────────────────────────────────

# Seestar has no active sensor cooling; CCD-TEMP (recorded per-frame) drives
# the stacked-image noise floor directly — a session captured near 9°C
# reproducibly stacks far cleaner than one captured near 20-30°C. See
# STACKING_RECIPE.md for the measurements behind this.
_TEMP_WARN_C = 18.0   # mean CCD-TEMP above this: flag as likely-noisy in the UI

_DATE_RE = re.compile(r'(\d{8})-\d{6}')


def survey_session_temps(
    fits_files:  list[str],
    per_night_sample: int = 8,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> dict:
    """
    Group a session's FITS files by capture night (from the YYYYMMDD in each
    filename) and sample CCD-TEMP from a handful of frames per night —
    header-only reads, no pixel data, so this stays fast even on
    multi-thousand-frame sessions.

    Returns {
      'nights': [{'date': 'YYYY-MM-DD', 'frame_count': int,
                  'temp_mean': float, 'temp_min': float, 'temp_max': float,
                  'warn': bool}, ...]  (sorted oldest to newest),
      'overall_mean': float | None,
    }
    Nights with no readable CCD-TEMP are omitted from 'nights' but still
    counted in the session; frame_count reflects ALL files that date, not
    just the sampled ones used for the temperature reading.
    """
    by_date: dict[str, list[str]] = {}
    for fpath in fits_files:
        m = _DATE_RE.search(os.path.basename(fpath))
        date_key = m.group(1) if m else 'unknown'
        by_date.setdefault(date_key, []).append(fpath)

    dates = sorted(by_date.keys())
    nights = []
    all_temps: list[float] = []

    for i, date_key in enumerate(dates):
        files = by_date[date_key]
        # evenly spaced sample across this night's files
        n = min(per_night_sample, len(files))
        idxs = sorted(set(int(j * (len(files) - 1) / max(n - 1, 1)) for j in range(n)))
        temps: list[float] = []
        for idx in idxs:
            try:
                hdr = _read_fits_header(files[idx])
                t = hdr.get('CCD-TEMP')
                if t is not None:
                    temps.append(float(t))
            except Exception:
                pass

        if temps:
            mean_t = float(np.mean(temps))
            nights.append({
                'date':        f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                                if date_key != 'unknown' else 'unknown',
                'frame_count': len(files),
                'temp_mean':   round(mean_t, 1),
                'temp_min':    round(min(temps), 1),
                'temp_max':    round(max(temps), 1),
                'warn':        mean_t > _TEMP_WARN_C,
            })
            all_temps.extend(temps)

        if progress_cb:
            progress_cb(int(100 * (i + 1) / len(dates)),
                        f"Sampling night {i + 1}/{len(dates)}: {date_key}")

    return {
        'nights':       nights,
        'overall_mean': round(float(np.mean(all_temps)), 1) if all_temps else None,
    }


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

def _subtract_background(img: np.ndarray, mesh_scale: int = 20) -> np.ndarray:
    """
    SEP sigma-clipped mesh background subtraction.

    mesh_scale controls the box size: bw = w // mesh_scale.  Higher values
    give a coarser mesh (fewer, larger cells) — better for large galaxies like
    M101 where fine cells would sample interarm regions as sky.  Lower values
    give a finer mesh — better for compact objects with strong gradients.
    Recommended range: 8 (very coarse, large galaxy) to 40 (fine, small nebula).

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
        if mesh_scale <= 0:
            return result   # caller requested no background subtraction
        bw = max(w // mesh_scale, 32)
        bh = max(h // mesh_scale, 32)
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


def _chroma_smooth(img: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    """
    Lab-space chroma smoothing on a linear float32 BGR image in [0, 1].

    Smooths only the a and b (colour) channels, leaving luminance untouched.
    Applied on linear data before stretching so the noise model is Gaussian
    and uniform — the stretch would otherwise nonlinearly amplify residual
    chroma speckle into the visible mottled colour pattern.
    """
    # Normalise to [0,1] for Lab conversion regardless of input ADU range,
    # then restore original scale afterward so only a/b (colour) are changed.
    img_f   = np.clip(img, 0.0, None).astype(np.float32)
    scale   = float(np.percentile(img_f, 99.99)) if img_f.max() > 0 else 1.0
    scale   = max(scale, 1e-6)
    img_n   = np.clip(img_f / scale, 0.0, 1.0)
    lab     = cv2.cvtColor(img_n, cv2.COLOR_BGR2Lab)
    l, a, b = cv2.split(lab)
    k    = max(int(sigma * 6) | 1, 3)
    a_sm = cv2.GaussianBlur(a, (k, k), sigma)
    b_sm = cv2.GaussianBlur(b, (k, k), sigma)
    result_n = cv2.cvtColor(cv2.merge([l, a_sm, b_sm]), cv2.COLOR_Lab2BGR)
    return (result_n * scale).astype(np.float32)


def _scnr_green(img: np.ndarray) -> np.ndarray:
    """
    Average-neutral Subtractive Chromatic Noise Reduction for the green channel.
    OSC Bayer sensors have 2× as many green photosites as red or blue, so the
    integrated stack always has excess green noise. This clips green to the mean
    of R and B wherever it exceeds that.

    Average-neutral rather than the maximum-neutral variant (clip to max(R, B)),
    which leaves a residual imbalance the stretch then amplifies into visible
    teal star halos: max(R, B) still lets G sit well above the *weaker* channel,
    and a linked autostretch — while preserving linear channel ratios — expands
    small absolute channel gaps by orders of magnitude in the midtones, so a
    G/R of ~1.13 in linear data becomes several 8-bit levels of saturation. The
    effect scales with how much the stretch lifts the image (measured on IC 434:
    teal 1.15% at Siril targetbg 0.05 rising to 4.79% at 0.30), which is why it
    cannot be corrected by a global per-channel scale — the residual is
    brightness-dependent (G/R 0.77 in midtones vs 1.21 in the bright band).

    Measured on IC 434 via teal_halo_metric.py: 2.321% -> 0.142%, with no
    regression on clean targets (M 27, M 81, M 13, IC 5146 all <= 0.072%) and
    no loss of genuine colour on M 27, a real teal planetary nebula (nebula
    G 220.2 -> 218.5, median saturation 6.0 -> 5.0).
    """
    result = img.copy()
    result[:, :, 1] = np.minimum(img[:, :, 1],
                                 (img[:, :, 2] + img[:, :, 0]) / 2.0)
    return result


def _reduce_stars(img: np.ndarray, amount: float = 0.5, max_radius: int = 25) -> np.ndarray:
    """
    Shrink star PSFs on an already-stretched float32 BGR image in [0, 1] so
    faint nebulosity reads more clearly without a dense star field competing
    for attention. Detects real stars with SEP (same tool already used for
    frame quality scoring and colour calibration elsewhere in this file) and
    erodes each one toward the local background — this transforms genuine
    star pixels from the stack, it does not delete, replace, or synthesise
    anything.

    amount: 0 = no change, 1 = maximum shrink (roughly halves apparent
    radius for a typical star). max_radius: stars larger than this many
    pixels (e.g. an oversaturated primary) are skipped — very large blobs
    are usually a nebula core or a badly saturated star, not a point source,
    and eroding them tends to look like a hole rather than a smaller star.
    """
    if amount <= 0:
        return img

    luma = (0.114 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.299 * img[:, :, 2]).astype(np.float64)
    luma = np.ascontiguousarray(luma)
    try:
        import sep
        # Dense star fields (e.g. Milky Way star-cloud targets like the Veil
        # Nebula) can trip SEP's default 300,000-pixel active-object limit at
        # thresh=5.0, since nearly every pixel is "above threshold" somewhere
        # in a crowded frame. Raise it generously rather than let extract()
        # raise and silently no-op the whole function on exactly the fields
        # that most need star reduction — confirmed hitting the default limit
        # on C34's 663-sub field, 2026-09-17.
        sep.set_extract_pixstack(2_000_000)
        bkg  = sep.Background(luma)
        data = np.ascontiguousarray((luma - bkg.back()).astype(np.float64))
        objs = sep.extract(data, thresh=5.0, err=bkg.globalrms, minarea=5)
    except Exception:
        return img
    if len(objs) == 0:
        return img

    h, w = luma.shape
    # Build one mask: for each detected star, a filled circle a bit larger
    # than its own SEP semi-major axis — this is the region eroded toward
    # background. Skipping objects bigger than max_radius leaves nebula
    # cores / very large saturated blobs untouched.
    star_mask = np.zeros((h, w), dtype=np.uint8)
    for o in objs:
        r = float(max(o['a'], o['b'])) * 2.2
        if r > max_radius or r < 1.0:
            continue
        cv2.circle(star_mask, (int(round(o['x'])), int(round(o['y']))), int(round(r)), 255, -1)
    if not np.any(star_mask):
        return img

    # Erosion kernel scales with `amount`; min-filtering each channel inside
    # the star mask pulls the bright core inward toward the surrounding
    # (dimmer) pixels, which reads as a smaller, tighter star.
    k = max(3, int(round(amount * 7)) | 1)  # odd kernel size, grows with amount
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    eroded = np.empty_like(img)
    for c in range(3):
        eroded[:, :, c] = cv2.erode(img[:, :, c], kernel)

    # Feather the mask edge so the transition isn't a hard circle boundary.
    mask_f = cv2.GaussianBlur(star_mask.astype(np.float32) / 255.0, (0, 0), sigmaX=1.5)
    mask_f = np.clip(mask_f * amount, 0.0, 1.0)[:, :, None]

    return img * (1.0 - mask_f) + eroded * mask_f


# ── Star/nebula separation (full removal + single-sub star layer) ────────────
#
# _reduce_stars() above (erosion) works for moderately star-rich targets but
# does not meaningfully declutter a Milky-Way-saturated field — shrinking
# each star's radius a little does nothing when star COUNT, not individual
# star size, is the dominant visual problem. Confirmed 2026-09-19 on SH2-142
# (dense Cygnus star cloud): star_reduce=0.9 produced no visible change.
#
# This is the heavier alternative validated as a prototype 2026-09-17/18:
# remove stars from the deep stack entirely (inpaint), stretch the starless
# nebula aggressively with no stars left to blow out, and separately pull a
# star layer from ONE registered single sub (far less accumulated signal
# than the deep stack, so its own stars are much less saturated) to recombine.

_STARLESS_MAX_RADIUS = 12    # px; excludes large bright non-stellar blobs (nebula cores)
_STARLESS_MAX_ELONG  = 2.5   # a/b ratio; real stars are round, nebula wisps are elongated
_STARLESS_MASK_MULT  = 1.5   # mask radius as a multiple of each star's SEP a/b.
                             # Was 2.5, which is fine on a sparse field but on a
                             # dense one (SH2-142, ~9.7k stars) masked 25.3% of
                             # the frame and merged neighbouring circles into
                             # large contiguous holes — 790 blobs bigger than 3x
                             # a lone circle, the largest 90x — which the fill
                             # cannot handle convincingly, reading as halos and
                             # softness. At 1.5 the same field masks 14.8% with
                             # a largest blob of 902px (vs 4514px).
_STARLESS_FILL_K     = 21    # median kernel for filling masked star positions;
                             # must comfortably exceed the largest mask radius
                             # so the window still sees unmasked background.
_STAR_LAYER_MAX_RADIUS = 10
_STAR_LAYER_MAX_ELONG  = 2.0


def _detect_point_stars(luma: np.ndarray, max_radius: float, max_elong: float,
                        require_clean_flag: bool = False):
    """
    SEP-detect compact, round, isolated sources on a float32/float64
    luminance array — i.e. real point-source stars, excluding large bright
    blobs (nebula cores/knots) and elongated features (wisps, diffraction
    spikes). Returns the filtered SEP object array.

    require_clean_flag additionally drops SEP's flagged (blended/saturated/
    edge) detections — useful for a single sub's star layer, where a
    contaminated detection would otherwise leak nebula-core fragments into
    the star mask (confirmed necessary on M42's Trapezium, 2026-09-17).
    """
    import sep
    luma64 = np.ascontiguousarray(luma.astype(np.float64))
    sep.set_extract_pixstack(2_000_000)
    bkg = sep.Background(luma64)
    sub = np.ascontiguousarray((luma64 - bkg.back()).astype(np.float64))
    objs = sep.extract(sub, thresh=5.0, err=bkg.globalrms, minarea=5)

    keep = []
    for o in objs:
        if require_clean_flag and o['flag'] != 0:
            continue
        a, b = float(o['a']), float(o['b'])
        r = max(a, b)
        if r > max_radius or r < 1.0:
            continue
        if b > 0 and (a / b) > max_elong:
            continue
        keep.append(o)
    return keep


def _build_starless_layer(img: np.ndarray) -> np.ndarray:
    """
    Remove point-source stars from a calibrated linear BGR image via
    SEP detection + OpenCV inpainting, leaving nebula/galaxy structure
    (including bright compact features like an emission-nebula core)
    intact. The size/elongation filter in _detect_point_stars is what
    keeps a bright nebula core from being detected as one giant "star"
    and inpainted away — confirmed necessary on M42's Trapezium
    (measured ~210px semi-major axis vs ~5-10px for real stars).
    """
    luma = 0.114 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.299 * img[:, :, 2]
    stars = _detect_point_stars(luma, _STARLESS_MAX_RADIUS, _STARLESS_MAX_ELONG)

    h, w = luma.shape
    mask = np.zeros((h, w), dtype=np.uint8)
    for o in stars:
        r = max(int(round(max(float(o['a']), float(o['b'])) * _STARLESS_MASK_MULT)), 2)
        cv2.circle(mask, (int(round(o['x'])), int(round(o['y']))), r, 255, -1)
    if not np.any(mask):
        return img.copy()

    starless = img.copy()
    for c in range(3):
        ch = img[:, :, c]
        scale = max(float(np.percentile(ch, 99.5)), 1e-8)
        # sqrt companding before the uint8 round-trip cv2.inpaint forces on us.
        # Linear astro data is extremely bottom-heavy — SH2-142's median sits at
        # 0.3% of the p99.5 scale, i.e. 8-bit level 0.83 — so quantising it
        # linearly sent 54.9% of pixels to zero and gave a 100% median relative
        # error, erasing exactly the faint nebulosity this function exists to
        # preserve. Companding drops that to 0.8% zeros and 5.5% median error.
        # Pixels above the scale clip either way, but those are the star cores
        # being inpainted away, so the clipping is harmless.
        norm = np.clip(ch / scale, 0.0, 1.0)
        ch_u8 = np.clip(np.sqrt(norm) * 255.0, 0, 255).astype(np.uint8)
        # A wide median fill rather than cv2.inpaint. Both TELEA and NS
        # propagate inward from the mask boundary, and on a dense field those
        # boundary pixels still sit in star halo, so each hole fills far too
        # bright and reads as a ring/donut: measured against the local
        # background on SH2-142, TELEA lands at 2.40x and NS at 3.02x (NS is
        # worse, despite being the obvious alternative to try), while a median
        # lands at 1.11x. A median ignores bright outliers instead of
        # propagating them, which is exactly the right behaviour for stars.
        filled_u8 = cv2.medianBlur(ch_u8, _STARLESS_FILL_K)
        filled = (filled_u8.astype(np.float32) / 255.0) ** 2 * scale
        # Keep the original float pixels everywhere the mask didn't touch.
        # The fill returns a whole new image, so taking it wholesale would push
        # the uint8 round-trip's residual error into untouched nebulosity
        # (measured 6.4% median change outside the mask on SH2-142) — the exact
        # "slight overall softness vs the plain stack" this function was
        # reported for. Only masked pixels have anything to gain from it.
        starless[:, :, c] = np.where(mask > 0, filled, ch)
    return starless


def _register_to(source: np.ndarray, target_luma: np.ndarray) -> Optional[np.ndarray]:
    """
    Register a calibrated linear BGR `source` image onto `target_luma`'s
    frame via astroalign (star-pattern matching), warping all 3 channels
    with the same transform. Returns None if no reliable transform is
    found (e.g. too few matching stars).

    Do NOT assume a single sub is already pixel-aligned to a deep stack's
    frame just because Siril picked it as the registration reference —
    the deep stack's own border-crop and background-mesh subtraction shift
    the effective coordinate origin. Confirmed on M42: a 24x196px offset
    between the "reference" sub and the deep stack before registration,
    reduced to 1x0px after. Always register explicitly.
    """
    import astroalign as aa
    src_luma = (0.114 * source[:, :, 0] + 0.587 * source[:, :, 1]
              + 0.299 * source[:, :, 2]).astype(np.float32)
    try:
        transform, _ = aa.find_transform(
            src_luma, target_luma, detection_sigma=5, max_control_points=60,
        )
    except Exception:
        return None

    # aa.apply_transform's output takes TARGET's shape, not source's — the two
    # can legitimately differ (e.g. a light sub-stack's own border-crop trims
    # a different row/column count than the deep stack's crop did, even from
    # the same source data). Allocate against target_luma, not source, or a
    # shape mismatch throws when writing per-channel results.
    aligned = np.zeros((target_luma.shape[0], target_luma.shape[1], 3), dtype=source.dtype)
    for c in range(3):
        aligned_ch, _footprint = aa.apply_transform(transform, source[:, :, c], target_luma)
        aligned[:, :, c] = aligned_ch
    return aligned


def _extract_star_layer(aligned_sub: np.ndarray, exclude_xy: Optional[tuple] = None,
                        exclude_radius: float = 80.0) -> np.ndarray:
    """
    Isolate just the point-source stars from a registered single-sub image
    (already in the deep stack's coordinate frame — see _register_to),
    zeroing everything else. exclude_xy/exclude_radius optionally excludes
    a known bright-core region (e.g. an emission nebula's brightest knot)
    from star detection as a belt-and-braces backstop on top of the
    size/elongation/flag filters — confirmed useful on M42's Trapezium,
    which produced a few small "clean" detections even after filtering.
    """
    luma = (0.114 * aligned_sub[:, :, 0] + 0.587 * aligned_sub[:, :, 1]
          + 0.299 * aligned_sub[:, :, 2])
    stars = _detect_point_stars(luma, _STAR_LAYER_MAX_RADIUS, _STAR_LAYER_MAX_ELONG,
                                require_clean_flag=True)

    h, w = luma.shape
    mask = np.zeros((h, w), dtype=np.float32)
    for o in stars:
        if exclude_xy is not None:
            dx = float(o['x']) - exclude_xy[0]
            dy = float(o['y']) - exclude_xy[1]
            if (dx * dx + dy * dy) ** 0.5 < exclude_radius:
                continue
        r = max(max(float(o['a']), float(o['b'])) * 3.0, 3.0)
        cv2.circle(mask, (int(round(o['x'])), int(round(o['y']))), int(round(r)), 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=1.5)
    return aligned_sub * mask[:, :, None]


def _starless_blend(deep_calibrated: np.ndarray, star_sub_path,
                    bayer_pattern: str = 'GRBG',
                    progress_cb: Optional[Callable] = None) -> np.ndarray:
    """
    Full star/nebula separation pipeline: build a starless layer from the
    deep (calibrated, linear) stack, register+extract a star layer from a
    lighter-SNR source, and recombine via per-channel max (screen-like
    blend that keeps whichever layer is brighter at each pixel — the
    starless layer everywhere except at star positions, where the aligned
    star retains its own, less-saturated brightness).

    star_sub_path: EITHER a single raw CFA .fit path (str) — cheap, but
    only detects a single sub's own star population, which on a dense
    field can be far fewer than the deep stack resolves (confirmed on
    SH2-142: a single sub found ~1,400-2,400 stars vs the 320-frame deep
    stack's 9,624 — restoring under a fifth of the field's actual stars)
    — OR a list of raw CFA .fit paths (list[str]), which are lightly
    stacked via Siril first (10-30 frames recommended: enough SNR to
    detect a field's real star population without accumulating anywhere
    near the deep stack's own saturation). Falls back to the unmodified
    deep stack if registration, stacking, or star detection fails.
    """
    deep_luma = (0.114 * deep_calibrated[:, :, 0] + 0.587 * deep_calibrated[:, :, 1]
               + 0.299 * deep_calibrated[:, :, 2]).astype(np.float32)
    if progress_cb is None:
        progress_cb = lambda p, msg, *a: None

    try:
        if isinstance(star_sub_path, (list, tuple)):
            # Light sub-stack: reuse _siril_full_stack for register+stack+
            # calibrate on a small frame count, then read its own output.
            tmp_fits = os.path.join(
                tempfile.gettempdir(), f"starless_substack_{int(time.time())}.fits")
            hdr0 = _read_fits_header(star_sub_path[0])
            bp = str(hdr0.get('BAYERPAT', bayer_pattern)).strip() or bayer_pattern
            ok = _siril_full_stack(
                list(star_sub_path), bp, tmp_fits,
                progress_cb=lambda p, msg, *_a: progress_cb(int(p * 0.5), msg),
            )
            if not ok:
                logging.warning("_starless_blend: light sub-stack failed, using deep stack as-is")
                return deep_calibrated
            from astropy.io import fits as _fits
            with _fits.open(tmp_fits) as h:
                sub_data = h[0].data.astype(np.float32)
            os.unlink(tmp_fits)
            linear_sidecar = str(Path(tmp_fits).with_name(Path(tmp_fits).stem + '_linear.fits'))
            if os.path.isfile(linear_sidecar):
                os.unlink(linear_sidecar)
            sub_cal = sub_data[::-1].transpose(1, 2, 0).copy()
        else:
            raw, hdr = _read_fits(star_sub_path)
            bp = str(hdr.get('BAYERPAT', bayer_pattern)).strip() or bayer_pattern
            sub_bgr = _debayer(raw, bp).astype(np.float32) / 65535.0
            sub_cal = _subtract_background(sub_bgr, mesh_scale=8)
            sub_cal = np.clip(sub_cal, 0.0, None)
            sub_cal = _color_calibrate(sub_cal)
            sub_cal = _scnr_green(sub_cal)

        progress_cb(60, "Registering star source")
        aligned = _register_to(sub_cal, deep_luma)
        if aligned is None:
            logging.warning("_starless_blend: registration failed, using deep stack as-is")
            return deep_calibrated

        progress_cb(75, "Extracting star layer")
        core_y, core_x = np.unravel_index(np.argmax(deep_luma), deep_luma.shape)
        star_layer = _extract_star_layer(aligned, exclude_xy=(core_x, core_y))
        progress_cb(90, "Building starless nebula layer")
        starless = _build_starless_layer(deep_calibrated)
        return np.maximum(starless, star_layer)
    except Exception as exc:
        logging.warning(f"_starless_blend failed ({exc}); using deep stack as-is")
        return deep_calibrated


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


# ── Stretch / denoise tuning constants ───────────────────────────────────────
# Change here; values flow automatically into both the pipeline and the run log.

_STRETCH_Q          = 8.0   # asinh stretch aggressiveness (5=gentle, 8=aggressive)
_STRETCH_BLACK_PCT  = 40    # percentile used as black point (50=median; lower reveals fainter structure)
_STRETCH_WHITE_PCT  = 99.9  # percentile used as white reference (lower = brighter core, more star clipping)
_CORE_PROTECT       = False # re-stretch clipped-white pixels with a gentler curve instead of flat white
_CORE_PCT           = 99.95 # luminance percentile above which a pixel is considered "clipped core", not just a bright star
_CORE_Q             = 2.0   # asinh Q for the core's own curve (lower = gentler = more headroom before it also clips)
_LUMA_BLUR_K    = 9     # luma Gaussian kernel size (must be odd)
_LUMA_BLUR_SIG  = 2     # luma Gaussian sigma
_CHROMA_BLUR_K  = 31    # chroma Gaussian kernel size (must be odd)
_CHROMA_BLUR_SIG = 10   # chroma Gaussian sigma
_UNSHARP_SIG    = 1.5   # unsharp mask blur sigma
_UNSHARP_GAIN   = 1.35  # unsharp mask blend weight (1 + gain blends in sharpened detail)
_SATURATION     = 1.0   # colour saturation multiplier (1.0 = unchanged)
_STAR_REDUCE       = 0.0  # 0 = off, up to 1.0 = maximum star shrink (see _reduce_stars)
_STAR_REDUCE_MAXR  = 25    # px; stars larger than this are skipped (nebula cores, saturated primaries)


def _boost_saturation(img: np.ndarray, saturation: float) -> np.ndarray:
    """
    Scale colour saturation of a stretched float32 BGR image in [0, 1].

    Works in HSV so hue is untouched; V is untouched so star cores and the
    nebula luminance keep their stretch.  Saturation is applied after the
    stretch — boosting in linear space would amplify chroma noise that the
    stretch then exaggerates.
    """
    if abs(saturation - 1.0) < 1e-3:
        return img
    hsv = cv2.cvtColor(np.clip(img, 0.0, 1.0), cv2.COLOR_BGR2HSV)
    hsv[..., 1] = np.clip(hsv[..., 1] * saturation, 0.0, 1.0)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

_STF_MIDTONE_TARGET = 0.12


def _auto_stretch(img: np.ndarray,
                  Q: float          = _STRETCH_Q,
                  black_pct: float  = _STRETCH_BLACK_PCT,
                  white_pct: float  = _STRETCH_WHITE_PCT,
                  core_protect: bool = _CORE_PROTECT,
                  core_pct:     float = _CORE_PCT,
                  core_Q:       float = _CORE_Q) -> np.ndarray:
    """
    Per-channel asinh stretch for preview JPEGs.

    black_pct percentile → black point; white_pct percentile → white reference.
    Q controls shadow aggressiveness: 5 = gentle, 8 = moderate, 15 = aggressive.
    Lower white_pct brightens the core (at the cost of more star clipping).

    core_protect: for targets with a small, very bright core well above
    white_pct (e.g. M42's Trapezium) the normal curve clips the whole core
    to flat white with no structure. When enabled, pixels above core_pct on
    the RAW LINEAR luminance (not the stretched result — arcsinh saturates
    near 1.0, which makes "quite bright" and "genuinely blown out" pixels
    indistinguishable post-stretch) that also sit in a spatially large
    clipped region (not an isolated star PSF — see below) are re-stretched
    with their own curve, using black/white points local to that region
    (not the frame-wide black_pct, which is tuned for the faint nebula and
    is far too low for the core's own brightness range), then feathered
    back in via a distance-transform mask so there's no hard seam.

    The connected-component + morphological-opening step is what keeps
    ordinary stars out of the mask (a compact nebula core is dozens of
    pixels across at typical plate scales; a star's saturated peak is a
    handful of pixels and gets opened away), so per-star halos don't appear.
    """
    result = np.zeros_like(img, dtype=np.float32)
    denom  = float(np.arcsinh(Q))
    for c in range(3):
        ch   = img[:, :, c]
        lo   = float(np.percentile(ch, black_pct))
        hi   = float(np.percentile(ch, white_pct))
        span = max(hi - lo, 1e-10)
        linear = np.clip((ch - lo) / span, 0.0, None)
        result[:, :, c] = np.clip(np.arcsinh(linear * Q) / denom, 0.0, 1.0)

    if not core_protect:
        return result

    # Identify clipped pixels on the RAW linear luminance, not the post-arcsinh
    # result — arcsinh saturates hard near 1.0, so "quite bright" and "actually
    # blown out" both read back as ~1.0 in the stretched result and become
    # indistinguishable. On the linear data, core_pct (e.g. 99.95) reliably
    # isolates only the genuinely brightest handful of pixels.
    linear_luma = 0.114 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.299 * img[:, :, 2]
    core_thr = float(np.percentile(linear_luma, core_pct))
    clipped = linear_luma >= core_thr
    if not np.any(clipped):
        return result

    # Morphological opening removes small blobs (star PSFs, typically a few
    # pixels to ~10px across even when saturated) while leaving large
    # contiguous clipped regions (a nebula core spans dozens+ pixels) intact.
    # This is what keeps the effect off ordinary stars without needing a
    # separate per-star exclusion list.
    kernel = np.ones((9, 9), np.uint8)
    opened = cv2.morphologyEx(clipped.astype(np.uint8) * 255,
                              cv2.MORPH_OPEN, kernel)
    if not np.any(opened):
        return result

    # Feather the mask edge with a distance transform so the blend fades in
    # smoothly over ~15px rather than at a hard pixel boundary.
    dist = cv2.distanceTransform(opened, cv2.DIST_L2, 5)
    feather_px = 15.0
    mask = np.clip(dist / feather_px, 0.0, 1.0).astype(np.float32)

    # Core curve gets its OWN black and white points from the masked region's
    # own linear values — the frame-wide black_pct is tuned for the faint
    # nebula and is far too low for this much brighter region (using it here
    # was the bug that crushed the core toward black). Low/high percentiles
    # *within the region* instead span the core's own actual brightness range.
    core_denom = float(np.arcsinh(core_Q))
    core_result = np.zeros_like(img, dtype=np.float32)
    region = mask > 0
    for c in range(3):
        ch = img[:, :, c]
        vals = ch[region]
        lo = float(np.percentile(vals, 1.0))
        hi = float(np.percentile(vals, 99.9))
        span = max(hi - lo, 1e-10)
        linear = np.clip((ch - lo) / span, 0.0, None)
        core_result[:, :, c] = np.clip(np.arcsinh(linear * core_Q) / core_denom, 0.0, 1.0)

    mask3 = mask[:, :, None]
    return result * (1.0 - mask3) + core_result * mask3


# ── Siril CLI post-processing ─────────────────────────────────────────────────

SIRIL_CLI          = "/mnt/c/Program Files/Siril/bin/siril-cli.exe"
SIRIL_WIN_WORK_BASE = "/mnt/g/Temp"   # Windows-accessible temp root for Siril jobs


_CORE_BLOWN_MIN_AREA = 2000   # px; largest contiguous near-white region, after
                              # eroding away isolated star cores, that marks a
                              # target as having a blown-out core. Measured on
                              # the archive at the default targetbg: bright-core
                              # targets (M 27, M 43, M 13, M 81, IC 434) land at
                              # 4,939-33,252px, faint extended ones (Veil,
                              # SH2-142, M 33, IC 5146, M 36) at 2-1,412px.
_CORE_RESCUE_TARGETBG = 0.05  # targetbg to re-render with when a core is blown


def _blown_core_area(jpeg_path: str) -> int:
    """
    Largest contiguous near-white region in a rendered JPEG, after eroding away
    isolated star cores. Distinguishes "a bright compact object clipped to a
    flat white blob" from "a normal field with bright stars in it" — plain
    percent-above-threshold does not, because a dense star field scores just as
    high as a blown nebula core.
    """
    img = cv2.imread(jpeg_path)
    if img is None:
        return 0
    lum = (0.299 * img[:, :, 2] + 0.587 * img[:, :, 1]
           + 0.114 * img[:, :, 0]).astype(np.float32)
    hot = (lum > 235).astype(np.uint8)
    eroded = cv2.erode(hot, np.ones((7, 7), np.uint8))
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(eroded, 8)
    return int(stats[1:, cv2.CC_STAT_AREA].max()) if n > 1 else 0


def _siril_postprocess(fits_path: str, jpeg_path: str,
                       progress_cb: Optional[Callable] = None,
                       shadowsclip: float = -2.00,
                       targetbg:    float = 0.15,
                       chroma_k:    int   = _CHROMA_BLUR_K,
                       chroma_sig:  float = _CHROMA_BLUR_SIG,
                       core_rescue: bool  = True) -> bool:
    """
    Call the Windows Siril CLI to produce a finished JPEG from a linear FITS.

    Pipeline: autostretch → save JPEG.  Background extraction and GraXpert
    denoising are applied upstream (in _siril_full_stack) on the linear FITS
    before this function is called.  Falls back silently to our own preview
    pipeline if Siril is not installed or the script fails.

    shadowsclip/targetbg are Siril's own autostretch controls (sigma units
    from the histogram peak / target background level — see the defaults'
    rationale below); chroma_k/chroma_sig control the post-stretch chroma
    denoise, matching the Python pipeline's equivalent knob so the Wizard's
    sliders mean the same thing regardless of which stretch path ran.

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

    # Green suppression before the stretch. The delivered linear FITS is left
    # untouched (users open it in Siril/PixInsight and expect raw linear data),
    # so SCNR is applied to a temporary copy that only feeds the preview. It has
    # to happen pre-stretch: the residual green is tiny in linear data and only
    # becomes visible because the autostretch amplifies it (see _scnr_green).
    # Previously SCNR ran only in the callers' not-siril_ok fallback branches,
    # so every preview rendered by this function — the normal path — skipped it.
    orig_fits_path = fits_path   # before the SCNR temp swap below, for retries
    scnr_fits = None
    try:
        from astropy.io import fits as _fits
        with _fits.open(fits_path) as _h:
            _d = _h[0].data
        if _d is not None and _d.ndim == 3 and _d.shape[0] == 3:
            _bgr = np.stack([_d[2], _d[1], _d[0]], axis=2).astype(np.float32)
            scnr_fits = str(Path(fits_path).with_name(
                Path(fits_path).stem + '_scnr_tmp.fits'))
            if _write_fits(scnr_fits, _scnr_green(_bgr), Path(fits_path).stem):
                fits_path = scnr_fits
            else:
                scnr_fits = None
    except Exception as exc:
        logging.warning(f"SCNR before Siril preview failed, using FITS as-is: {exc}")
        scnr_fits = None

    fits_win = to_win(fits_path)
    jpeg_win = to_win(os.path.splitext(jpeg_path)[0])  # Siril appends .jpg itself

    # autostretch args: [-linked] [shadowsclip [targetbg]].
    # -linked stretches all channels together (no colour shift after the
    # stack's -rgb_equal).  Target background 0.15 instead of the 0.25
    # default — the default lifts the sky noise floor well into view
    # (Seestar's own JPEGs sit around 0.16).
    #
    # shadowsclip -2.00 (not the more aggressive -2.80): shadowsclip is in
    # sigma units from the histogram peak, so a steeper (more negative)
    # value plus a low targetbg means a steeper shadow-region stretch curve
    # that visibly amplifies real, small residual noise. Confirmed via A/B
    # test on a known-clean linear FITS (2026-09-15, IC 434/Horsehead,
    # GraXpert-denoised background measured sigma~8-14): -2.80 produced
    # sigma~28-34 in the final JPEG (visibly grainy, ~3x the target), while
    # -2.00 produced sigma~12-13, matching a known-good manual reference
    # ("clubtalk" export). See project-ngc5907-first-deep-pool-stack-attempt
    # memory for the full parameter sweep (-2.80/-2.00/-1.50/-1.00/-0.50
    # shadowsclip x 0.15/0.20/0.25/0.30 targetbg).
    script = (
        'requires 1.2.0\n'
        f'load "{fits_win}"\n'
        f'autostretch -linked {shadowsclip:.2f} {targetbg:.2f}\n'
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

        # Bright-core rescue. targetbg 0.15 is tuned for faint extended targets
        # and is right for most of the archive, but on a target with a compact
        # bright core (planetary nebula, globular, emission-nebula knot) it
        # pushes the whole object against white: M 27's nebula body lands with
        # its middle 50% inside 24 of 255 levels and 46.5% of it above 240,
        # which is the long-standing "core clips to a flat white blob" problem.
        # Nothing is clipped in the linear data — it is purely where the stretch
        # puts it — so re-rendering darker recovers the structure: M 27 goes to
        # IQR 56 with 0.1% blown (from 25.5%), and background sigma drops too.
        # Detect rather than guess, since lowering it globally measurably dims
        # faint targets like the Veil, where 0.15 is the better result.
        if core_rescue and targetbg > _CORE_RESCUE_TARGETBG:
            if _blown_core_area(jpeg_path) > _CORE_BLOWN_MIN_AREA:
                progress_cb(50, "Bright core detected — re-rendering darker")
                # orig_fits_path, not fits_path: by here fits_path points at
                # this call's SCNR temp copy, which the finally below deletes.
                return _siril_postprocess(
                    orig_fits_path, jpeg_path, progress_cb,
                    shadowsclip=shadowsclip, targetbg=_CORE_RESCUE_TARGETBG,
                    chroma_k=chroma_k, chroma_sig=chroma_sig,
                    core_rescue=False,
                )

        # Chroma-only denoise on the stretched JPEG. Siril's autostretch
        # path has no color noise reduction of its own (unlike the Python
        # fallback pipeline's _denoise_sharpen); on warm-sensor sessions
        # residual per-channel noise reads visually as color speckle even
        # though luminance noise (the dominant, largely capture-limited
        # component) is unaffected. Confirmed 2026-09-16 on NGC 7000
        # (20.5C session): Cr/Cb std roughly halved (7.0/6.3 -> 3.1/2.4),
        # luma essentially unchanged (46.85 -> 46.82) — real, visible
        # reduction in color mottling without touching brightness noise.
        try:
            img = cv2.imread(jpeg_path)
            if img is not None:
                ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
                y, cr, cb = cv2.split(ycrcb)
                cr = cv2.GaussianBlur(cr, (chroma_k, chroma_k), chroma_sig)
                cb = cv2.GaussianBlur(cb, (chroma_k, chroma_k), chroma_sig)
                out = cv2.cvtColor(cv2.merge([y, cr, cb]), cv2.COLOR_YCrCb2BGR)
                cv2.imwrite(jpeg_path, out, [cv2.IMWRITE_JPEG_QUALITY, 95])
        except Exception as exc:
            import logging
            logging.warning(f"Chroma denoise on Siril preview failed: {exc}")

        return True

    except Exception as exc:
        import logging
        logging.warning(f"Siril post-processing failed: {exc}")
        try:
            os.unlink(script_path)
        except Exception:
            pass
        return False

    finally:
        if scnr_fits:
            try:
                os.unlink(scnr_fits)
            except OSError:
                pass


# ── Siril full pipeline (register + stack + preview) ─────────────────────────

def _siril_full_stack(
    selected_files: list[str],
    bayer_pattern: str,
    output_fits: str,
    progress_cb: Optional[Callable] = None,
    bg_mesh_scale: int = 20,
    _extra_stats: Optional[dict] = None,
) -> bool:
    """
    Use Siril CLI for RCD demosaic + 2-pass registration + weighted sigma-clip
    stacking, producing a 3-channel linear color FITS at output_fits.

    Siril pipeline:
      convert light -debayer        → RCD demosaic of the raw CFA frames.
                                       RCD preserves ~12 % more star sharpness
                                       than a bilinear debayer and avoids its
                                       correlated chroma speckle.  (Bayer
                                       drizzle was also evaluated: same
                                       sharpness, but ~2× noisier at typical
                                       frame counts because each colour plane
                                       only gets ¼ of the samples per frame.)
      register light_ -2pass        → compute transforms + FWHM stats only;
                                       picks the best frame as reference
      seqapplyreg light_            → apply transforms (framing=max)
      stack r_light_ rej 3 3        → winsorized sigma-clip integration,
                                       additive+scale norm, FWHM-weighted,
                                       per-channel background equalisation
                                       saves stacked.fit (3-ch float32 RGB)

    Python post-step: read stacked.fit → border crop → sky subtract → denoise
    → save 3-channel float32 FITS to output_fits.

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

        # Pre-flight disk-space check.
        # Raw CFA input (2 B/px) + Siril's debayered RGB copy (uint16, 6 B/px)
        # + registered float32 RGB frames (12 B/px).
        n = len(selected_files)
        if n > 0:
            import shutil as _shutil
            sample_raw, sample_hdr = _read_fits(selected_files[0])
            h_s, w_s = sample_raw.shape[:2]
            input_bytes  = h_s * w_s * 2 * n              # uint16 CFA input
            debayer_bytes = h_s * w_s * 3 * 2 * n         # uint16 RGB (convert)
            reg_bytes    = h_s * w_s * 3 * 4 * n          # float32 registered RGB
            needed_bytes = (input_bytes + debayer_bytes + reg_bytes) * 1.15
            free_bytes   = _shutil.disk_usage(SIRIL_WIN_WORK_BASE).free
            if needed_bytes > free_bytes:
                needed_gb = needed_bytes / 1024**3
                free_gb   = free_bytes   / 1024**3
                logging.warning(
                    f"Siril full stack: insufficient disk space — "
                    f"need {needed_gb:.1f} GB, have {free_gb:.1f} GB free on "
                    f"{SIRIL_WIN_WORK_BASE}.  Reduce max_frames or free disk space."
                )
                progress_cb(0,
                    f"Disk space error: need {needed_gb:.1f} GB, "
                    f"only {free_gb:.1f} GB free — reduce max_frames or free space on C:"
                )
                return False
            sample_has_bayerpat = bool(str(sample_hdr.get('BAYERPAT', '')).strip())

        # Copy raw CFA frames into the work dir.  No Python debayer — Siril's
        # 'convert -debayer' (RCD) does it better than OpenCV's bilinear, and
        # the Seestar headers already carry BAYERPAT so a byte-for-byte copy
        # keeps everything Siril needs.  If a header lacks BAYERPAT, rewrite
        # via astropy adding the pattern detected earlier.
        progress_cb(0, f"Siril: copying {n} CFA frames to work dir…")
        for i, src in enumerate(selected_files):
            dst = os.path.join(work_dir, f"raw_{i:05d}.fit")
            if sample_has_bayerpat:
                _copy_file_no_sendfile(src, dst)
            else:
                from astropy.io import fits as _fits
                raw, _ = _read_fits(src)
                hdu = _fits.PrimaryHDU(raw.astype(np.uint16))
                hdu.header['BAYERPAT'] = bayer_pattern
                hdu.writeto(dst, overwrite=True)
                del raw, hdu
            if (i + 1) % 50 == 0 or i + 1 == n:
                progress_cb(
                    int(15 * (i + 1) / n),
                    f"Siril: copied {i + 1}/{n} frames",
                )

        work_win = to_win(work_dir)

        # Demosaic, register, and stack.
        #   convert       RCD-debayers every raw_ frame into the light fitseq
        #                 (single-file sequence; -fitseq avoids Siril's "too
        #                 many open files" error at large frame counts, and
        #                 changes the sequence name to "light" with no
        #                 trailing underscore, unlike multi-file sequences)
        #   -2pass        registration computes transforms + per-frame FWHM
        #                 without writing frames, and picks the best-quality
        #                 frame as the reference automatically
        #   seqapplyreg   applies the transforms in the reference frame's
        #                 footprint (default framing=current).  framing=max
        #                 must NOT be used here: Seestar is alt-az, so
        #                 multi-night sessions carry large field rotation and
        #                 the union canvas becomes mostly empty border, which
        #                 also skews the stretch statistics downstream.
        #                 Partial-coverage edges are IQR-cropped in Python.
        #   stack         winsorized 3σ rejection, additive+scale normalisation,
        #                 frames weighted by registration FWHM, channel
        #                 backgrounds equalised, output rescaled to [0,1]
        # NOTE: -fitseq is intentionally NOT used here despite handling >8192
        # frames without hitting Siril's open-file ceiling. Confirmed via a
        # controlled A/B test (2026-09-14) that -fitseq's register step fails
        # on EVERY frame with "Numerical overflow during type conversion" /
        # "Could not load frame N" — reproduced on plain, ordinary frames with
        # no WCS/header irregularities, and with -debayer removed entirely.
        # The old multi-file convert/register (no -fitseq) succeeded 10/10 on
        # the identical input. This is a Siril 1.4.3 fitseq+register bug, not
        # a data problem — so max_frames must stay under Siril's open-file
        # limit (8192 on this system) rather than relying on -fitseq to scale
        # past it.
        script = (
            f'requires 1.4.0\n'
            f'cd "{work_win}"\n'
            f'setext fit\n'
            f'convert light -debayer\n'
            f'register light_ -2pass\n'
            f'seqapplyreg light_\n'
            f'stack r_light_ rej 3 3 -norm=addscale -output_norm -rgb_equal '
            f'-weight=wfwhm -out=stacked\n'
        )

        script_path = os.path.join(work_dir, "stack.ssf")
        with open(script_path, 'w') as f:
            f.write(script)

        logging.info(f"Siril full stack: {n} frames  work={work_dir}")
        progress_cb(20, f"Siril: registering and stacking {n} frames…")

        # Registration + stacking time scales with frame count; a flat 2h cap
        # (fine up to ~2000 frames) starves deep pools like multi-night SN
        # watches. A 2s/frame budget proved too tight in practice — a
        # 7500-frame run (NGC 5907, 2026-09-14) hit the resulting 15000s
        # (4h10m) ceiling with convert+register+seqapplyreg already complete
        # and stack still running. Budget 6s/frame with a 2h floor, capped
        # at 16h.
        siril_timeout = max(7200, min(n * 6, 57600))
        try:
            proc = subprocess.run(
                [SIRIL_CLI, "-s", to_win(script_path)],
                capture_output=True, text=True, timeout=siril_timeout,
            )
        except subprocess.TimeoutExpired:
            # Killing the subprocess only reaps the WSL /init interop shim —
            # the real siril-cli.exe keeps running on the Windows side and
            # would go on writing into work_dir while the finally block below
            # deletes it, and would contend with the next attempt for the
            # same scratch drive.  Reap it properly.
            _kill_windows_siril()
            raise

        if proc.returncode != 0:
            logging.warning(
                f"Siril full stack exit {proc.returncode}\n"
                f"{_siril_error_lines(proc.stdout)}"
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
            g_fits = data[1]   # green plane; astropy returns NumPy order (row 0 = top)
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

            # Apply column crop: a column is invalid only if >5% of its rows
            # are zero (Siril registration border), not just a single zero pixel.
            lum_cols = bgr.sum(axis=2)
            valid_cols = np.where((lum_cols > 0).mean(axis=0) > 0.95)[0]
            c0 = int(valid_cols[0])  if valid_cols.size else 0
            c1 = int(valid_cols[-1]) + 1 if valid_cols.size else bgr.shape[1]

            bgr = bgr[bot_crop:top_crop, c0:c1]

            # Save pre-background-subtraction linear FITS so re-render can
            # re-apply background subtraction with a different mesh scale
            # without re-running the full alignment stack.
            linear_fits = str(Path(output_fits).with_name(
                Path(output_fits).stem + '_linear.fits'))
            _write_fits(linear_fits, bgr, Path(output_fits).stem + '_linear')

            # Per-channel SEP background subtraction: fits a sigma-clipped 2D mesh
            # to each channel independently, equalising sky levels across R/G/B and
            # removing vignetting gradients without treating nebula/galaxy as sky.
            bgr = _subtract_background(bgr, mesh_scale=bg_mesh_scale)
            bgr = np.clip(bgr, 0.0, None)

            # Star colour calibration (SEP aperture photometry white-balance).
            # Siril's -rgb_equal only equalises the SKY background across
            # channels; it does not correct per-star colour, so a genuine
            # Bayer-response imbalance in the source frames (observed as a
            # green/teal tint on bright stars) passes through uncorrected.
            # Reuses the same SEP-based star white-balance as the pure-Python
            # fallback pipeline, applied here so the Siril path gets it too.
            bgr = _color_calibrate(bgr)

            # GraXpert AI denoising on the linear image (before any stretch).
            # Linear data has Gaussian noise characteristics; denoising here gives
            # the model clean signal to work with rather than nonlinearly amplified
            # shadow noise.  Falls back silently if GraXpert is unavailable.
            progress_cb(97, "AI denoising (GraXpert)")
            _gx_status: list = []
            bgr = _graxpert_denoise(bgr, strength=1.0, _status_out=_gx_status)
            if _extra_stats is not None:
                _extra_stats['graxpert_status'] = _gx_status[0] if _gx_status else 'not run'

            _write_fits(output_fits, bgr, Path(output_fits).stem)
        except Exception as exc:
            logging.warning(f"Siril stack: FITS-read/write failed: {exc}")
            return False

        progress_cb(100, "Siril stacking complete")
        return True

    except subprocess.TimeoutExpired:
        logging.warning(
            f"Siril full stack timed out after {siril_timeout}s "
            f"({siril_timeout / 3600:.1f} h) on {n} frames — the Windows "
            f"siril-cli.exe has been killed and the work dir will be removed. "
            f"Raise the per-frame budget in _siril_full_stack or lower "
            f"max_frames if this recurs."
        )
        progress_cb(0, f"Siril timed out after {siril_timeout / 3600:.1f} h "
                       f"on {n} frames")
        return False
    except Exception as exc:
        logging.warning(f"Siril full stack error: {exc}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── AI denoising (GraXpert) ───────────────────────────────────────────────────

def _graxpert_denoise(
    img_bgr: np.ndarray,
    strength: float = 1.0,
    _status_out: Optional[list] = None,
) -> np.ndarray:
    """
    GraXpert AI denoising on a linear float32 BGR image in [0, 1].
    Downloads the ONNX model on first call; uses CUDA if available.
    Falls back silently to the original image on any error.

    If _status_out is a list, a single human-readable status string is
    appended to it so callers can include the outcome in a run log.

    Monkey-patches graxpert.ai_model_handling.run_in_process to a no-op so
    that inference runs in-process rather than in a forked child.  GraXpert
    forks to guard against ROCm crashes, but the fork corrupts the CUDA
    context on WSL2 (CUDA is not fork-safe), causing GPU=-1 failures.
    Running in-process is safe for CUDA/TensorRT providers.
    """
    def _record(s):
        if _status_out is not None:
            _status_out.append(s)

    try:
        # The CUDA runtime libs (cublas, cudnn) are installed as pip wheels
        # under site-packages/nvidia/, which is not on the system loader path.
        # preload_dlls() (onnxruntime ≥ 1.21) dlopens them from the wheels so
        # the CUDAExecutionProvider can initialise; without it onnxruntime
        # silently falls back to CPU on WSL2.
        try:
            import onnxruntime as _ort
            if hasattr(_ort, "preload_dlls"):
                _ort.preload_dlls()
        except Exception:
            pass

        import graxpert.ai_model_handling as _gxh
        import graxpert.denoising as _gxd
        from graxpert.denoising import denoise as _gx_denoise
        from graxpert.ai_model_handling import (
            denoise_ai_models_dir, ai_model_path_from_version,
            download_version, latest_version, list_local_versions,
        )
        from graxpert.s3_secrets import denoise_bucket_name

        # GraXpert caches the denoised tile output in a MODULE-LEVEL global
        # (graxpert.denoising.cached_denoised_image) and only clears it via
        # its own GUI eventbus (AppEvents.LOAD_IMAGE_REQUEST etc), which this
        # headless pipeline never fires. Across a long-running server process
        # that calls denoise() for multiple different images, the second and
        # later calls reuse the FIRST image's cached tiles regardless of the
        # new image's shape — confirmed by a live "operands could not be
        # broadcast together with shapes (1920,1080,3) (1893,1080,3) ..."
        # failure (2026-09-15) where a Horsehead run silently fell back to
        # the un-denoised image because a prior NGC 5907 run's differently-
        # shaped result was still cached. Reset it ourselves before every
        # call so each image gets a fresh denoise pass.
        _gxd.reset_cached_denoised_image(None)

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
            logging.warning("GraXpert denoise returned None — using original image")
            _record(f"failed (returned None)")
            return img_bgr
        logging.info(f"GraXpert denoise: OK (strength={strength}, model={ai_version})")
        _record(f"OK  (strength={strength}, model={ai_version})")
        return result[:, :, ::-1].astype(np.float32)   # RGB → BGR
    except Exception as exc:
        logging.warning(f"GraXpert denoise failed — using original image: {exc}")
        _record(f"failed ({exc})")
        return img_bgr


# ── Enhancement ───────────────────────────────────────────────────────────────

def _denoise_sharpen(img: np.ndarray,
                     luma_k:      int   = _LUMA_BLUR_K,
                     luma_sig:    float = _LUMA_BLUR_SIG,
                     chroma_k:    int   = _CHROMA_BLUR_K,
                     chroma_sig:  float = _CHROMA_BLUR_SIG,
                     unsharp_gain: float = _UNSHARP_GAIN,
                     unsharp_sig:  float = _UNSHARP_SIG) -> np.ndarray:
    """Chroma Gaussian + unsharp-mask sharpening on a stretched uint8 BGR image."""
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycrcb)

    y_dn  = cv2.GaussianBlur(y,  (luma_k,   luma_k),   luma_sig)
    cr_dn = cv2.GaussianBlur(cr, (chroma_k, chroma_k), chroma_sig)
    cb_dn = cv2.GaussianBlur(cb, (chroma_k, chroma_k), chroma_sig)

    denoised  = cv2.cvtColor(cv2.merge([y_dn, cr_dn, cb_dn]), cv2.COLOR_YCrCb2BGR)
    blurred   = cv2.GaussianBlur(denoised, (0, 0), unsharp_sig)
    return cv2.addWeighted(denoised, unsharp_gain, blurred, -(unsharp_gain - 1), 0)


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
        f"  Quality floor cut   : {stats.get('floor_rejected', 0)}  (min_quality={stats.get('min_quality', 0.0):.2f}, floor score={stats.get('floor_score', 0.0):.2f})",
        f"  Score range         : {stats.get('worst_score', '?')} – {stats.get('best_score', '?')}",
        f"  Alignment accepted  : {aligned}  ({align_rate} of selected)",
        f"  Alignment rejected  : {rejected_align}",
        f"",
        f"Registration",
        f"  Reference frame  : {stats.get('ref_frame', 'unknown')}",
        f"  Bayer pattern    : {stats.get('bayer_pattern', 'unknown')}",
        f"",
        f"Post-processing",
        f"  bg_mesh_scale    : {stats.get('bg_mesh_scale', 20)}  (0 = skip background subtraction)",
        f"  min_quality      : {stats.get('min_quality', 0.0):.2f}  (0 = off; 0.5 = keep top half by score)",
        f"  GraXpert denoise : {stats.get('graxpert_status', 'not run')}",
        f"  stretch_Q        : {_STRETCH_Q}  (black point={_STRETCH_BLACK_PCT}th pct)",
        f"  luma_blur        : {_LUMA_BLUR_K}×{_LUMA_BLUR_K}  sigma={_LUMA_BLUR_SIG}",
        f"  chroma_blur      : {_CHROMA_BLUR_K}×{_CHROMA_BLUR_K}  sigma={_CHROMA_BLUR_SIG}",
        f"  unsharp          : gain={_UNSHARP_GAIN}  sigma={_UNSHARP_SIG}",
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
        fits_files:    list[str],
        output_path:   str,
        progress_cb:   Callable[[int, str, int, int], None],
        cancel_cb:     Optional[Callable[[], bool]] = None,
        max_frames:    int = DEFAULT_MAX_FRAMES,
        use_cache:     bool = False,
        bg_mesh_scale: int = 20,
        min_quality:   float = 0.0,
    ) -> dict:

        def _chk():
            if cancel_cb and cancel_cb():
                raise StackCancelled("Stacking cancelled")

        # Clamp before the copy/scoring phases so an over-large request fails
        # here instead of after ~1.5 h of work, when Siril's convert aborts.
        if os.path.isfile(SIRIL_CLI) and max_frames > SIRIL_FRAME_LIMIT:
            logging.warning(
                f"max_frames={max_frames} exceeds the Siril open-file limit "
                f"({SIRIL_MAX_OPEN_FILES}); clamping to {SIRIL_FRAME_LIMIT}"
            )
            max_frames = SIRIL_FRAME_LIMIT

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
            'bg_mesh_scale':  bg_mesh_scale,
            'min_quality':    min_quality,
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
                    _copy_file_no_sendfile(src, dst)
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

            # Rank by combined quality score (best first)
            all_scores = [_quality_score(stage_b_metrics[k]) for k in range(n_stage_a)]
            scored = sorted(range(n_stage_a), key=lambda k: all_scores[k], reverse=True)

            # Score-relative quality floor: drop frames below min_quality × best score.
            # This cuts poor-condition frames even when max_frames would include them.
            best_score  = all_scores[scored[0]] if scored else 0.0
            floor_score = best_score * max(min_quality, 0.0)
            scored_floor = [k for k in scored if all_scores[k] >= floor_score]
            n_floor_rejected = len(scored) - len(scored_floor)

            # Hard cap at max_frames
            scored = scored_floor[:max_frames]

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

            run_stats['stage_b_pass']      = len(stage_a_idx)
            run_stats['selected']          = n_selected
            run_stats['floor_rejected']    = n_floor_rejected
            run_stats['best_score']        = round(best_score, 2)
            run_stats['worst_score']       = round(all_scores[scored[-1]] if scored else 0.0, 2)
            run_stats['floor_score']       = round(floor_score, 2)

            # Persist the selected-frame list (basenames + scores) so a later
            # Siril-stage failure can be retried without redoing Stage A/B
            # scoring — a distinct, much cheaper cache than the full-pool
            # copy cache above, since the selection itself is expensive to
            # recompute but tiny to store.
            try:
                os.makedirs(cache_dir, exist_ok=True)
                selection_path = os.path.join(cache_dir, 'last_selection.json')
                with open(selection_path, 'w') as f:
                    json.dump({
                        'max_frames':  max_frames,
                        'min_quality': min_quality,
                        'selected':    [
                            {'file': os.path.basename(selected_files[j]),
                             'score': round(all_scores[scored[j]], 2)}
                            for j in range(n_selected)
                        ],
                    }, f, indent=2)
            except OSError:
                pass

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
            # SIRIL PATH — let Siril handle demosaic, registration, stacking
            #
            # Siril is a well-tested astronomical image processor.  We hand it
            # the quality-filtered frame list and it does:
            #   convert -debayer → RCD demosaic of the raw CFA frames
            #   register -2pass  → transforms + FWHM stats, best ref frame
            #   seqapplyreg      → apply transforms (framing=max)
            #   stack rej        → FWHM-weighted winsorized sigma-clip with
            #                      additive+scale norm and channel equalisation
            # then Python crops, subtracts background, denoises, and
            # _siril_postprocess renders the preview JPEG.
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
            siril_installed = os.path.isfile(SIRIL_CLI)
            _siril_extra: dict = {}
            siril_stack_ok  = _siril_full_stack(
                selected_files, bayer_pattern, fits_path,
                progress_cb=lambda p, msg, *_a: progress_cb(
                    37 + int(p * 0.50), msg, n_selected, total
                ),
                bg_mesh_scale=bg_mesh_scale,
                _extra_stats=_siril_extra,
            )
            run_stats.update(_siril_extra)
            if siril_installed and not siril_stack_ok:
                # Siril is present but the run failed (disk space, script error,
                # etc.).  The error was already surfaced via progress_cb — raise
                # now so the job is marked as failed rather than silently falling
                # through to the Python pipeline.
                raise RuntimeError(
                    "Siril stacking failed — check the Stack Queue log for details"
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
                        cv2.imwrite(str(output_path), preview_u8[::-1],
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

            # Siril not installed — fall through to built-in pipeline
            progress_cb(37, "Siril not installed; using built-in pipeline",
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
                    str(output_path), preview_u8[::-1],
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
                     progress_cb:   Optional[Callable] = None,
                     bg_mesh_scale: int   = 20,
                     stretch_q:     float = _STRETCH_Q,
                     black_pct:     float = _STRETCH_BLACK_PCT,
                     white_pct:     float = _STRETCH_WHITE_PCT,
                     luma_k:        int   = _LUMA_BLUR_K,
                     luma_sig:      float = _LUMA_BLUR_SIG,
                     chroma_k:      int   = _CHROMA_BLUR_K,
                     chroma_sig:    float = _CHROMA_BLUR_SIG,
                     unsharp_gain:  float = _UNSHARP_GAIN,
                     unsharp_sig:   float = _UNSHARP_SIG,
                     saturation:    float = _SATURATION,
                     core_protect:  bool  = _CORE_PROTECT,
                     core_pct:      float = _CORE_PCT,
                     core_Q:        float = _CORE_Q,
                     star_reduce:      float = _STAR_REDUCE,
                     star_reduce_maxr: int   = _STAR_REDUCE_MAXR,
                     starless_blend:      bool = False,
                     starless_star_sub                = None) -> str:
    """
    Regenerate the preview JPEG from an already-stacked FITS file without
    re-running frame alignment.  All post-processing parameters are tunable.
    Returns the path of the written JPEG.

    starless_blend: use full star/nebula separation (see _starless_blend)
    instead of stretching the deep stack directly — for star-saturated
    fields (e.g. Milky Way star clouds) where _reduce_stars's erosion
    approach doesn't meaningfully declutter the field (confirmed on
    SH2-142, 2026-09-19: star_reduce=0.9 produced no visible change).
    Requires starless_star_sub: EITHER a single raw CFA .fit path (str)
    — cheap, but on a dense field only restores a fraction of the deep
    stack's actual star population (confirmed on SH2-142: a single sub
    found ~1,400-2,400 stars vs the deep stack's 9,624) — OR a list of
    raw CFA .fit paths (list[str]) to lightly sub-stack first for much
    better star-detection SNR without the deep stack's own saturation
    (10-30 frames recommended). Mutually exclusive with star_reduce/
    core_protect in practice, though not enforced here.
    """
    if progress_cb is None:
        progress_cb = lambda p, msg, *a: None

    # If a pre-background-subtraction linear FITS exists (saved during the
    # original stack), use it so we can re-apply bg subtraction with the new
    # mesh scale.  Falls back to the processed FITS for older stacks.
    # Accept either the stacked FITS or its _linear sidecar. Passing the
    # sidecar directly used to append _linear a second time, so has_linear
    # came out False and the whole calibration block below was silently
    # skipped — raw data went straight to the stretch, which looks like a
    # severe green cast in the output.
    if Path(fits_path).stem.endswith('_linear'):
        linear_fits = fits_path
    else:
        linear_fits = str(Path(fits_path).with_name(
            Path(fits_path).stem + '_linear.fits'))
    source_fits = linear_fits if os.path.isfile(linear_fits) else fits_path
    has_linear  = os.path.isfile(linear_fits)

    progress_cb(5, "Loading FITS", 0, 0)
    try:
        from astropy.io import fits as _fits
        with _fits.open(source_fits) as hdul:
            data = hdul[0].data.astype(np.float32)
    except Exception as e:
        raise RuntimeError(f"Cannot read FITS: {e}")

    if data.ndim == 3 and data.shape[0] == 3:
        bgr = data[::-1].transpose(1, 2, 0).copy()
    elif data.ndim == 3 and data.shape[2] == 3:
        bgr = data[:, :, ::-1].copy()
    else:
        raise RuntimeError(f"Unexpected FITS shape: {data.shape}")

    os.makedirs(os.path.dirname(os.path.abspath(jpeg_path)), exist_ok=True)

    # Keep a copy of the previous render so the wizard can offer before/after comparison
    prev_path = str(Path(jpeg_path).with_name(Path(jpeg_path).stem + '_prev.jpg'))
    if os.path.isfile(jpeg_path):
        try:
            shutil.copy2(jpeg_path, prev_path)
        except OSError:
            pass

    if has_linear:
        progress_cb(10, "Background subtraction", 0, 0)
        bgr = _subtract_background(bgr, mesh_scale=bg_mesh_scale)
        bgr = np.clip(bgr, 0.0, None)

        progress_cb(25, "Colour calibration", 0, 0)
        bgr = _color_calibrate(bgr)

        progress_cb(30, "AI denoising (GraXpert)", 0, 0)
        bgr = _graxpert_denoise(bgr, strength=1.0)

        progress_cb(60, "SCNR green suppression", 0, 0)
        bgr = _scnr_green(bgr)

        if starless_blend and starless_star_sub:
            progress_cb(65, "Star/nebula separation", 0, 0)
            bgr = _starless_blend(
                bgr, starless_star_sub,
                progress_cb=lambda p, msg, *_a: progress_cb(65 + int(p * 0.05), msg, 0, 0),
            )

        # Prefer Siril's own autostretch (histogram-transform-function based)
        # over the Python asinh curve — it is the same stretch the ORIGINAL
        # full-stack job uses (_siril_full_stack -> _siril_postprocess), and
        # confirmed materially better at preserving faint, low-contrast
        # structure (e.g. filamentary targets like the Veil Nebula: Siril
        # recovered detail the Python asinh curve nearly erased, 2026-09-17).
        # core_protect is Python-only (no Siril equivalent), so fall back to
        # the Python path whenever it's requested; also fall back whenever
        # Siril itself is unavailable or the CLI call fails.
        siril_ok = False
        if not core_protect:
            # Map the Wizard's Python-stretch sliders onto Siril's own
            # autostretch controls so they stay meaningful on this path
            # instead of silently doing nothing. Approximate, not exact —
            # the two curves aren't the same shape — but keeps the sliders
            # live rather than dead controls whenever Siril runs.
            #   targetbg    ~ black_pct's 0-90 range  -> Siril's 0.05-0.30
            #   shadowsclip ~ stretch_q's 1-30 range   -> Siril's -0.5..-2.8
            # white_pct has no Siril equivalent (autostretch has no white-
            # point percentile control) and is ignored on this path.
            targetbg    = 0.05 + (max(0.0, min(90.0, black_pct)) / 90.0) * 0.25
            shadowsclip = -0.5 - (max(1.0, min(30.0, stretch_q)) - 1.0) / 29.0 * 2.3

            tmp_fits = str(Path(jpeg_path).with_name(
                Path(jpeg_path).stem + '_rerender_tmp.fits'))
            try:
                _write_fits(tmp_fits, bgr, 'rerender_tmp')
                progress_cb(70, "Siril autostretch", 0, 0)
                siril_ok = _siril_postprocess(
                    tmp_fits, jpeg_path,
                    progress_cb=lambda p, msg, *_a: progress_cb(70 + int(p * 0.15), msg, 0, 0),
                    shadowsclip=shadowsclip, targetbg=targetbg,
                    chroma_k=chroma_k, chroma_sig=chroma_sig,
                )
            finally:
                try:
                    os.unlink(tmp_fits)
                except OSError:
                    pass

            if siril_ok and star_reduce > 0:
                # Siril already wrote the JPEG; star reduction needs to run
                # on those pixels, so reload, apply, and re-save.
                progress_cb(90, "Reducing stars", 0, 0)
                img = cv2.imread(jpeg_path)
                if img is not None:
                    preview = _reduce_stars(img[::-1].astype(np.float32) / 255.0,
                                            amount=star_reduce, max_radius=star_reduce_maxr)
                    preview_u8 = np.clip(preview * 255.0, 0, 255).astype(np.uint8)
                    cv2.imwrite(jpeg_path, preview_u8[::-1], [cv2.IMWRITE_JPEG_QUALITY, 95])

        if not siril_ok:
            progress_cb(75, "Auto-stretch", 0, 0)
            preview = _auto_stretch(bgr, Q=stretch_q, black_pct=black_pct, white_pct=white_pct,
                                    core_protect=core_protect, core_pct=core_pct, core_Q=core_Q)
            preview = _boost_saturation(preview, saturation)

            if star_reduce > 0:
                progress_cb(82, "Reducing stars", 0, 0)
                preview = _reduce_stars(preview, amount=star_reduce, max_radius=star_reduce_maxr)

            progress_cb(88, "Noise reduction and sharpening", 0, 0)
            preview_u8 = (preview * 255).astype(np.uint8)
            preview_u8 = _denoise_sharpen(preview_u8,
                                          luma_k=luma_k, luma_sig=luma_sig,
                                          chroma_k=chroma_k, chroma_sig=chroma_sig,
                                          unsharp_gain=unsharp_gain, unsharp_sig=unsharp_sig)

            progress_cb(97, "Saving JPEG", 0, 0)
            ok = cv2.imwrite(jpeg_path, preview_u8[::-1], [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise RuntimeError(f"Failed to write JPEG to {jpeg_path}")
        progress_cb(100, "Done", 0, 0)
        return jpeg_path

    # No linear FITS — older stack.  Try Siril postprocess on the processed FITS.
    progress_cb(10, "Post-processing (Siril)", 0, 0)
    siril_ok = _siril_postprocess(fits_path, jpeg_path,
                                   progress_cb=lambda p, msg, *a: progress_cb(
                                       10 + int(p * 0.85), msg, 0, 0))
    if siril_ok:
        progress_cb(100, "Done", 0, 0)
        return jpeg_path

    # Fallback: our own pipeline on the processed FITS
    progress_cb(20, "Colour calibration", 0, 0)
    bgr = _color_calibrate(bgr)

    progress_cb(40, "AI denoising (GraXpert)", 0, 0)
    bgr = _graxpert_denoise(bgr, strength=1.0)

    progress_cb(70, "SCNR green suppression", 0, 0)
    bgr = _scnr_green(bgr)

    progress_cb(80, "Auto-stretch", 0, 0)
    preview = _auto_stretch(bgr, Q=stretch_q, black_pct=black_pct)
    preview = _boost_saturation(preview, saturation)

    progress_cb(90, "Noise reduction and sharpening", 0, 0)
    preview_u8 = (preview * 255).astype(np.uint8)
    preview_u8 = _denoise_sharpen(preview_u8,
                                  luma_k=luma_k, luma_sig=luma_sig,
                                  chroma_k=chroma_k, chroma_sig=chroma_sig,
                                  unsharp_gain=unsharp_gain, unsharp_sig=unsharp_sig)

    progress_cb(97, "Saving JPEG", 0, 0)
    ok = cv2.imwrite(jpeg_path, preview_u8[::-1], [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError(f"Failed to write JPEG to {jpeg_path}")

    progress_cb(100, "Done", 0, 0)
    return jpeg_path
