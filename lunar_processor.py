#!/usr/bin/env python3
"""
Seestar Lab — Lunar timelapse processor.

════════════════════════════════════════════════════════════════════════════════
MODES
════════════════════════════════════════════════════════════════════════════════

  phase   Cross-session phase-sequence timelapse.  One best frame per session
          directory, sorted chronologically.  Shows the terminator sweeping
          across the disk over days / weeks / months.

  single  Single-session colour timelapse.  All video files in one directory,
          like the solar pipeline but colour-preserving to show the
          amber→white atmospheric colour shift as the moon rises.

════════════════════════════════════════════════════════════════════════════════
OUTPUTS  (written to --output-dir, or the source directory for single mode)
════════════════════════════════════════════════════════════════════════════════

  lunar_phases.mp4      Phase-sequence VFR timelapse (phase mode)
  lunar_session.mp4     Colour timelapse (single mode)
  lunar_portrait.jpg    Sharpest normalised frame with date / phase overlay
  lunar_mosaic.jpg      Contact-sheet grid of all frames  (phase mode only)
  lunar_alignment.json  Per-session/video disk-detection cache

════════════════════════════════════════════════════════════════════════════════
DISK DETECTION  (two methods, tried in order)
════════════════════════════════════════════════════════════════════════════════

  1. HoughCircles — works reliably for ≥ ~50 % illumination (gibbous / full).

  2. RANSAC algebraic circle fit on bright-limb edge pixels — handles crescent
     and quarter phases where only an arc is visible.  Samples random subsets
     of Canny-edge points from the bright region and fits the full disk circle
     using the standard algebraic linearisation of (x-a)²+(y-b)²=r².
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ── Optional astropy for accurate phase labels ────────────────────────────────
try:
    from astropy.coordinates import get_body, get_sun  # type: ignore
    from astropy.time import Time as _AstroTime         # type: ignore
    _HAVE_ASTROPY = True
except ImportError:
    _HAVE_ASTROPY = False

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False

# ── Tunable constants ─────────────────────────────────────────────────────────

VIDEO_TIMEZONE   = "America/Los_Angeles"
TARGET_R_FRAC    = 0.43          # disk radius / output_size
MAX_RADIUS_DRIFT = 0.20          # looser than solar — phase changes disk shape
DISK_BLUR_K      = 9
MIN_QUALITY      = 6.0           # lower threshold — craters ≠ limb sharpness
MOSAIC_THUMB_PX  = 300           # thumbnail pixel size in mosaic grid

# Filename date pattern: YYYY-MM-DD-HHMMSS
_FNAME_DT_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})')

# Known new moon for age fallback (JD)
_KNOWN_NEW_JD = 2451550.1        # 2000 Jan 6.1 UT
_LUNAR_CYCLE  = 29.530588

VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv"}

# ── Timestamp parser ──────────────────────────────────────────────────────────

def _parse_video_start_utc(video_path: str) -> Optional[datetime]:
    m = _FNAME_DT_RE.search(Path(video_path).stem)
    if not m:
        return None
    if not _HAVE_ZONEINFO:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    try:
        local_tz = _ZoneInfo(VIDEO_TIMEZONE)
        local_dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
            tzinfo=local_tz,
        )
        return local_dt.astimezone(timezone.utc)
    except Exception:
        return None


# ── Circle fitting ────────────────────────────────────────────────────────────

def _fit_circle_algebraic(pts: np.ndarray) -> tuple[float, float, float]:
    """
    Algebraic least-squares circle fit.
    Solves (x-a)²+(y-b)²=r²  linearised as  z = 2ax + 2by + c,  z = x²+y².
    pts: (N, 2) float array of (x, y) edge pixels.
    Returns (cx, cy, r).
    """
    x = pts[:, 0].astype(float)
    y = pts[:, 1].astype(float)
    A = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    b = x ** 2 + y ** 2
    res, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = float(res[0]), float(res[1])
    r2 = float(res[2]) + cx ** 2 + cy ** 2
    if r2 <= 0:
        raise ValueError("Degenerate circle fit")
    return cx, cy, math.sqrt(r2)


def _ransac_circle(
    pts: np.ndarray,
    r_min: float,
    r_max: float,
    n_iter: int = 300,
    inlier_tol: float = 5.0,
) -> Optional[tuple[int, int, int]]:
    """
    RANSAC wrapper around _fit_circle_algebraic.
    Returns (cx, cy, r) in integer pixels, or None.
    """
    if len(pts) < 10:
        return None
    best_cx = best_cy = best_r = None
    best_n = 0
    rng = np.random.default_rng(42)
    sample_k = min(8, len(pts))

    for _ in range(n_iter):
        idx = rng.choice(len(pts), sample_k, replace=False)
        try:
            cx, cy, r = _fit_circle_algebraic(pts[idx].astype(float))
        except Exception:
            continue
        if not (r_min <= r <= r_max):
            continue
        dist = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        n_inl = int(np.sum(dist < inlier_tol))
        if n_inl > best_n:
            best_n = n_inl
            inlier_pts = pts[dist < inlier_tol]
            try:
                cx2, cy2, r2 = _fit_circle_algebraic(inlier_pts.astype(float))
                if r_min <= r2 <= r_max:
                    best_cx, best_cy, best_r = cx2, cy2, r2
                else:
                    best_cx, best_cy, best_r = cx, cy, r
            except Exception:
                best_cx, best_cy, best_r = cx, cy, r

    if best_cx is None:
        return None
    return int(round(best_cx)), int(round(best_cy)), int(round(best_r))


# ── Disk detection ────────────────────────────────────────────────────────────

def _find_disk_lunar(
    gray: np.ndarray,
    r_min: int,
    r_max: int,
) -> Optional[tuple[int, int, int]]:
    """
    Locate the lunar disk in a grayscale frame.
    Returns (cx, cy, r) or None.

    Method 1 — HoughCircles (works for gibbous / full moon)
    Method 2 — RANSAC circle fit on bright-limb Canny edges (crescent / quarter)
    """
    h, w = gray.shape[:2]
    blurred = cv2.GaussianBlur(gray, (DISK_BLUR_K, DISK_BLUR_K), 2)

    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.5,
        minDist=min(h, w) // 2,
        param1=50, param2=25,
        minRadius=r_min, maxRadius=r_max,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    # RANSAC fallback — threshold bright pixels, Canny edges, fit circle
    thresh_val = float(np.percentile(blurred, 72))
    if thresh_val < 5:                  # too dark / no moon visible
        return None
    bright = (blurred > thresh_val).astype(np.uint8) * 255
    edges  = cv2.Canny(bright, 50, 150)
    ys, xs = np.where(edges > 0)
    if len(xs) < 10:
        return None
    pts = np.column_stack([xs, ys])     # (N, 2) in (x, y) order
    return _ransac_circle(pts, r_min, r_max)


def _disk_sharpness(gray: np.ndarray, cx: int, cy: int, r: int) -> float:
    """Laplacian-variance sharpness inside the disk annulus."""
    mask  = np.zeros(gray.shape, dtype=np.uint8)
    inner = max(1, int(r * 0.3))
    outer = max(inner + 1, int(r * 0.90))
    cv2.circle(mask, (cx, cy), outer, 255, -1)
    cv2.circle(mask, (cx, cy), inner,   0, -1)
    roi  = cv2.Laplacian(gray, cv2.CV_64F)
    vals = roi[mask == 255]
    return float(np.var(vals)) if vals.size > 0 else 0.0


# ── Frame normalisation ───────────────────────────────────────────────────────

def _normalise_frame(
    frame_bgr: np.ndarray,
    cx: int, cy: int, r: int,
    out_size: int,
    target_r: int,
) -> np.ndarray:
    scale = target_r / max(r, 1)
    tx    = out_size / 2.0 - scale * cx
    ty    = out_size / 2.0 - scale * cy
    M     = np.array([[scale, 0.0, tx], [0.0, scale, ty]], dtype=np.float32)
    return cv2.warpAffine(frame_bgr, M, (out_size, out_size),
                          flags=cv2.INTER_LANCZOS4,
                          borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(0, 0, 0))


def _stretch_frame(
    frame_bgr: np.ndarray,
    sky_pct:  float,
    high_pct: float,
    gamma:    float,
    out_size: int,
    target_r: int,
) -> np.ndarray:
    """
    Colour-preserving histogram stretch.
    Per-channel sky subtraction keeps hue ratios intact; the white-point
    divisor is derived from luminance so colour temperature is preserved.
    """
    f32 = frame_bgr.astype(np.float32)

    corner_mask = np.ones((out_size, out_size), dtype=np.uint8)
    cx = cy = out_size // 2
    cv2.circle(corner_mask, (cx, cy), int(target_r * 0.97), 0, -1)

    # Per-channel sky subtraction
    for c in range(3):
        ch   = f32[..., c]
        bg   = ch[corner_mask == 1]
        sky  = float(np.percentile(bg, sky_pct)) if bg.size > 0 else 0.0
        f32[..., c] = np.clip(ch - sky, 0.0, None)

    # Luminance white-point — same divisor for all channels → hue preserved
    lum      = 0.299 * f32[..., 2] + 0.587 * f32[..., 1] + 0.114 * f32[..., 0]
    disk_lum = lum[corner_mask == 0]
    hi_val   = float(np.percentile(disk_lum, high_pct)) if disk_lum.size > 0 else 1.0
    if hi_val > 0:
        f32 /= hi_val

    f32 = np.power(np.clip(f32, 0.0, 1.0), gamma)
    return (np.clip(f32, 0.0, 1.0) * 255).astype(np.uint8)


# ── Phase / illumination ──────────────────────────────────────────────────────

def _lunar_illumination(dt_utc: Optional[datetime]) -> tuple[float, float]:
    """
    Returns (illumination_pct 0–100, age_days 0–29.5).
    Uses astropy when available; falls back to a simple periodic formula.
    """
    if dt_utc is None:
        return 0.0, 0.0

    if _HAVE_ASTROPY:
        try:
            t    = _AstroTime(dt_utc)
            moon = get_body("moon", t)
            sun  = get_sun(t)
            elong = float(moon.separation(sun).rad)
            illum = (1.0 - math.cos(elong)) / 2.0 * 100.0
            # Age from a known reference new moon
            age = (t.jd - _KNOWN_NEW_JD) % _LUNAR_CYCLE
            return illum, age
        except Exception:
            pass

    # Fallback: simple approximation accurate to ~1 day
    delta_days = (dt_utc - datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)).total_seconds() / 86400
    age  = delta_days % _LUNAR_CYCLE
    elong = (age / _LUNAR_CYCLE) * 2 * math.pi
    illum = (1.0 - math.cos(elong)) / 2.0 * 100.0
    return illum, age


def _phase_name(age_days: float) -> str:
    if age_days < 1.0 or age_days > 28.5:
        return "New Moon"
    elif age_days < 6.5:
        return "Waxing Crescent"
    elif age_days < 8.0:
        return "First Quarter"
    elif age_days < 13.5:
        return "Waxing Gibbous"
    elif age_days < 15.5:
        return "Full Moon"
    elif age_days < 21.5:
        return "Waning Gibbous"
    elif age_days < 23.0:
        return "Last Quarter"
    else:
        return "Waning Crescent"


# ── Overlay helpers ───────────────────────────────────────────────────────────

def _overlay_label(img: np.ndarray, text: str, pos: str = "bottom") -> np.ndarray:
    out   = img.copy()
    h, w  = out.shape[:2]
    scale = w / 1080.0
    fs    = max(0.5, 1.1 * scale)
    th    = max(1, round(2.0 * scale))
    margin = max(10, int(16 * scale))
    y      = h - margin if pos == "bottom" else max(36, int(40 * scale))
    cv2.putText(out, text, (margin, y), cv2.FONT_HERSHEY_SIMPLEX,
                fs, (0, 0, 0), th + 2, cv2.LINE_AA)
    cv2.putText(out, text, (margin, y), cv2.FONT_HERSHEY_SIMPLEX,
                fs, (255, 255, 255), th, cv2.LINE_AA)
    return out


# ── Mosaic builder ────────────────────────────────────────────────────────────

def _build_mosaic(
    frames: list,
    labels: list,
    thumb_px: int = MOSAIC_THUMB_PX,
) -> Optional[np.ndarray]:
    """Build a contact-sheet grid image from normalised frames."""
    n = len(frames)
    if not n:
        return None
    n_cols = max(1, math.ceil(math.sqrt(n)))
    n_rows = math.ceil(n / n_cols)
    gap    = 6
    cell   = thumb_px + gap
    total_w = n_cols * cell + gap
    total_h = n_rows * cell + gap
    mosaic  = np.zeros((total_h, total_w, 3), dtype=np.uint8)

    for i, (frame, lbl) in enumerate(zip(frames, labels)):
        row  = i // n_cols
        col  = i % n_cols
        x    = gap + col * cell
        y    = gap + row * cell
        thumb = cv2.resize(frame, (thumb_px, thumb_px), interpolation=cv2.INTER_AREA)
        # Small label at bottom of thumb
        fs = max(0.28, 0.33 * thumb_px / 300)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cv2.putText(thumb, lbl, (3 + dx, thumb_px - 5 + dy),
                            cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(thumb, lbl, (3, thumb_px - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, fs, (200, 200, 200), 1, cv2.LINE_AA)
        mosaic[y : y + thumb_px, x : x + thumb_px] = thumb

    return mosaic


# ── Video writer (VFR via ffconcat, CFR fallback) ─────────────────────────────

def _write_mp4(
    path: str,
    frames_bgr: list,
    durations: Optional[list],
    fps_cfr: int = 30,
) -> None:
    if not frames_bgr:
        return
    ffbin = "/usr/bin/ffmpeg" if os.path.isfile("/usr/bin/ffmpeg") else shutil.which("ffmpeg")

    if durations is not None and ffbin:
        tmp_dir = tempfile.mkdtemp(prefix="lunar_vfr_")
        try:
            concat = os.path.join(tmp_dir, "frames.txt")
            with open(concat, "w") as fh:
                fh.write("ffconcat version 1.0\n")
                for j, (frm, dur) in enumerate(zip(frames_bgr, durations)):
                    jpg = os.path.join(tmp_dir, f"f{j:05d}.jpg")
                    cv2.imwrite(jpg, frm, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    fh.write(f"file '{jpg}'\nduration {dur:.6f}\n")
                last = os.path.join(tmp_dir, f"f{len(frames_bgr)-1:05d}.jpg")
                fh.write(f"file '{last}'\n")
            tmp_out = path + ".vfr.tmp.mp4"
            r = subprocess.run(
                [ffbin, "-y", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", concat,
                 "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                 "-pix_fmt", "yuv420p", tmp_out],
                capture_output=True,
            )
            if r.returncode == 0:
                os.replace(tmp_out, path)
                return
            print(f"  VFR encode warning: {r.stderr.decode(errors='replace')[:200]}", flush=True)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # CFR fallback
    h, w   = frames_bgr[0].shape[:2]
    raw    = path + ".raw.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(raw, fourcc, fps_cfr, (w, h))
    for f in frames_bgr:
        writer.write(f)
    writer.release()
    if ffbin:
        tmp = path + ".tmp.mp4"
        r   = subprocess.run(
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


# ── Best-frame picker (used by phase mode) ────────────────────────────────────

def _best_frame_from_video(
    vpath: str,
    r_min: int,
    r_max: int,
    sample_every_s: float = 20.0,
) -> Optional[dict]:
    """
    Sample every ~sample_every_s seconds; return dict with best frame info
    (cx, cy, r, quality, frame_idx, ts_utc) or None if disk not found.
    """
    cap = cv2.VideoCapture(vpath)
    if not cap.isOpened():
        return None
    fps     = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    stride  = max(1, int(fps * sample_every_s))
    start_utc = _parse_video_start_utc(vpath)
    best: Optional[dict] = None

    frame_idx = 0
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        gray   = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        result = _find_disk_lunar(gray, r_min, r_max)
        if result:
            cx, cy, r = result
            q = _disk_sharpness(gray, cx, cy, r)
            if best is None or q > best["quality"]:
                ts = (
                    (start_utc + timedelta(seconds=frame_idx / fps)).isoformat()
                    if start_utc else None
                )
                best = {
                    "video": vpath, "frame_idx": frame_idx,
                    "cx": cx, "cy": cy, "r": r,
                    "quality": q, "ts_utc": ts,
                }
        frame_idx += stride
        if n_total > 0 and frame_idx >= n_total:
            break

    cap.release()
    return best


# ── Phase-sequence pipeline ───────────────────────────────────────────────────

def run_phase(
    sessions:    list,        # [{name, paths:[...], date:"YYYY-MM-DD"}, ...]
    out_dir:     str,
    out_size:    int   = 1080,
    gamma:       float = 0.8,
    sky_pct:     float = 5.0,
    high_pct:    float = 99.5,
    frame_hold:  float = 1.5,  # seconds each frame is displayed
    no_cache:    bool  = False,
    progress_cb        = None,
) -> dict:
    """
    Phase-sequence pipeline (3 passes).
    Returns dict of output paths + metadata.
    """

    def _progress(pct: int, msg: str) -> None:
        print(f"[Pass {1 if pct < 40 else (2 if pct < 75 else 3)}] {msg}", flush=True)
        if progress_cb:
            progress_cb(pct, msg)

    os.makedirs(out_dir, exist_ok=True)
    target_r   = int(out_size * TARGET_R_FRAC)
    r_min      = max(20, int(out_size * 0.12))
    r_max      = int(out_size * 0.52)
    cache_path = os.path.join(out_dir, "lunar_alignment.json")

    # Load cache
    cache: dict = {}
    if not no_cache and os.path.isfile(cache_path):
        try:
            with open(cache_path) as fh:
                cache = json.load(fh)
            print(f"  Loaded alignment cache ({len(cache.get('session_cache', {}))} sessions)", flush=True)
        except Exception:
            cache = {}

    # ── Pass 1 — disk detection per session ──────────────────────────────────
    _progress(0, "Pass 1 — Detecting lunar disk in each session…")

    # Each entry: {session_name, date, best_frame_entry, illum_pct, age_days, phase_lbl}
    session_frames: list[dict] = []

    for si, sess in enumerate(sessions):
        pct_base = int(si / len(sessions) * 37)
        sname = sess.get("name", f"Session {si+1}")
        sdate = sess.get("date", "")
        _progress(pct_base, f"  [{si+1}/{len(sessions)}] {sname}  {sdate}")

        cache_key = json.dumps(sorted(sess.get("paths", [])))
        cached = (cache.get("session_cache") or {}).get(cache_key)

        if cached and not no_cache:
            best_entry = cached
            print(f"    (cached) quality={best_entry.get('quality', 0):.1f}", flush=True)
        else:
            # Collect all video files across all session paths
            video_files: list[str] = []
            for p in sess.get("paths", []):
                if os.path.isdir(p):
                    for fname in sorted(os.listdir(p)):
                        if Path(fname).suffix.lower() in VIDEO_EXT:
                            if not fname.startswith("lunar_"):
                                video_files.append(os.path.join(p, fname))
                elif os.path.isfile(p) and Path(p).suffix.lower() in VIDEO_EXT:
                    video_files.append(p)

            if not video_files:
                print(f"    no video files found — skipping", flush=True)
                continue

            best_entry = None
            for vpath in video_files:
                entry = _best_frame_from_video(vpath, r_min, r_max)
                if entry and (best_entry is None or entry["quality"] > best_entry["quality"]):
                    best_entry = entry

            if best_entry is None:
                print(f"    disk not detected — skipping", flush=True)
                continue
            print(f"    best quality={best_entry['quality']:.1f}  r={best_entry['r']}px", flush=True)

            # Validate radius vs per-session median (single-session: just use the one entry)
            if "video_cache" not in cache:
                cache["video_cache"] = {}
            if "session_cache" not in cache:
                cache["session_cache"] = {}
            cache["session_cache"][cache_key] = best_entry

        # Compute phase from timestamp or supplied date
        ts_utc: Optional[datetime] = None
        if best_entry.get("ts_utc"):
            try:
                ts_utc = datetime.fromisoformat(best_entry["ts_utc"])
            except Exception:
                pass
        if ts_utc is None and sdate:
            try:
                ts_utc = datetime.fromisoformat(sdate + "T00:00:00+00:00")
            except Exception:
                pass

        illum, age = _lunar_illumination(ts_utc)
        pname      = _phase_name(age)
        phase_lbl  = f"{pname}  {illum:.0f}%"
        mosaic_lbl = sdate or (best_entry["ts_utc"] or "")[:10]
        if illum > 0:
            mosaic_lbl += f"  {illum:.0f}%"

        session_frames.append({
            "session_name": sname,
            "date":         sdate,
            "best_entry":   best_entry,
            "illum_pct":    illum,
            "age_days":     age,
            "phase_name":   pname,
            "phase_lbl":    phase_lbl,
            "mosaic_lbl":   mosaic_lbl,
            "ts_utc":       best_entry.get("ts_utc"),
        })

    # Save cache
    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh, indent=2)
    except Exception as exc:
        print(f"  Warning: could not write cache: {exc}", flush=True)

    if not session_frames:
        print("ERROR: no sessions with detected disk.", flush=True)
        sys.exit(1)

    # Sort by timestamp / date
    session_frames.sort(key=lambda s: s.get("ts_utc") or s.get("date") or "")
    print(f"  Total accepted sessions: {len(session_frames)}", flush=True)

    # ── Pass 2 — Normalisation & stretch ─────────────────────────────────────
    _progress(40, "Pass 2 — Normalising and stretching frames…")

    normed_frames:  list[np.ndarray] = []
    mosaic_labels:  list[str]        = []
    portrait_labels: list[str]       = []
    frame_qualities: list[float]     = []

    open_caps: dict[str, cv2.VideoCapture] = {}

    def _get_cap(vp: str) -> cv2.VideoCapture:
        if vp not in open_caps:
            open_caps[vp] = cv2.VideoCapture(vp)
        return open_caps[vp]

    for i, sf in enumerate(session_frames):
        if i % 5 == 0:
            pct = 40 + int(i / len(session_frames) * 33)
            _progress(pct, f"  Normalising frame {i+1}/{len(session_frames)}…")

        e   = sf["best_entry"]
        cap = _get_cap(e["video"])
        cap.set(cv2.CAP_PROP_POS_FRAMES, e["frame_idx"])
        ok, frame = cap.read()
        if not ok:
            continue

        normed    = _normalise_frame(frame, e["cx"], e["cy"], e["r"], out_size, target_r)
        stretched = _stretch_frame(normed, sky_pct, high_pct, gamma, out_size, target_r)
        normed_frames.append(stretched)
        mosaic_labels.append(sf["mosaic_lbl"])
        portrait_labels.append(sf["phase_lbl"])
        frame_qualities.append(e["quality"])

    for cap in open_caps.values():
        cap.release()

    if not normed_frames:
        print("ERROR: no normalised frames produced.", flush=True)
        sys.exit(1)

    _progress(73, f"  Normalised {len(normed_frames)} frames")

    # ── Pass 3 — Portrait + mosaic + timelapse ────────────────────────────────
    _progress(74, "Pass 3 — Assembling outputs…")

    # Portrait: sharpest frame
    best_idx = int(np.argmax(frame_qualities))
    portrait = normed_frames[best_idx].copy()
    date_labels = [sf["date"] for sf in session_frames if sf.get("date")]
    date_range  = (
        date_labels[0] if len(date_labels) == 1
        else (f"{date_labels[0]} – {date_labels[-1]}" if date_labels else "Lunar")
    )
    portrait_text = f"))) {portrait_labels[best_idx]}  ·  {date_range}"
    portrait = _overlay_label(portrait, portrait_text, pos="bottom")
    portrait_path = os.path.join(out_dir, "lunar_portrait.jpg")
    cv2.imwrite(portrait_path, portrait, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"  Wrote portrait → {portrait_path}", flush=True)

    # Mosaic
    mosaic = _build_mosaic(normed_frames, mosaic_labels)
    mosaic_path = None
    if mosaic is not None:
        mosaic_path = os.path.join(out_dir, "lunar_mosaic.jpg")
        cv2.imwrite(mosaic_path, mosaic, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"  Wrote mosaic → {mosaic_path}", flush=True)

    # VFR: fixed frame_hold duration per session frame
    n = len(normed_frames)
    durations = [max(1.0 / 30.0, frame_hold)] * n

    timelapse_path = os.path.join(out_dir, "lunar_phases.mp4")
    _progress(82, f"  Encoding {n} frames → {Path(timelapse_path).name}")
    _write_mp4(timelapse_path, normed_frames, durations)
    print(f"  Wrote timelapse → {timelapse_path}", flush=True)

    _progress(100, "Done.")
    return {
        "timelapse":   timelapse_path if os.path.isfile(timelapse_path) else None,
        "portrait":    portrait_path  if os.path.isfile(portrait_path)  else None,
        "mosaic":      mosaic_path    if mosaic_path and os.path.isfile(mosaic_path) else None,
        "frame_count": n,
        "date_label":  date_range,
    }


# ── Single-session pipeline ───────────────────────────────────────────────────

def run_single(
    video_files:     list[str],
    out_dir:         str,
    out_size:        int   = 1080,
    sample_interval: float = 1.0,
    speedup:         float = 1800.0,
    gamma:           float = 0.8,
    sky_pct:         float = 5.0,
    high_pct:        float = 99.5,
    no_cache:        bool  = False,
    progress_cb             = None,
) -> dict:
    """
    Single-session colour timelapse (mirrors solar pipeline, colour-preserving).
    Returns dict of output paths.
    """

    def _progress(pct: int, msg: str) -> None:
        print(f"[Pass {1 if pct < 40 else (2 if pct < 75 else 3)}] {msg}", flush=True)
        if progress_cb:
            progress_cb(pct, msg)

    os.makedirs(out_dir, exist_ok=True)
    target_r   = int(out_size * TARGET_R_FRAC)
    cache_path = os.path.join(out_dir, "lunar_alignment.json")

    cache: dict = {}
    if not no_cache and os.path.isfile(cache_path):
        try:
            with open(cache_path) as fh:
                cache = json.load(fh)
            print(f"  Loaded alignment cache ({len(cache.get('video_cache', {}))} entries)", flush=True)
        except Exception:
            cache = {}

    # ── Pass 1 ───────────────────────────────────────────────────────────────
    _progress(0, "Pass 1 — Detecting lunar disk in each frame…")

    raw_frames: list[dict] = []
    video_files = sorted(video_files)

    for vi, vpath in enumerate(video_files):
        pct_base = int(vi / len(video_files) * 37)
        _progress(pct_base, f"  [{vi+1}/{len(video_files)}] {Path(vpath).name}")

        cache_key     = vpath
        start_utc     = _parse_video_start_utc(vpath)
        cached_entries = (cache.get("video_cache") or {}).get(cache_key)

        if cached_entries is not None:
            raw_frames.extend(cached_entries)
            print(f"    (cached: {len(cached_entries)} frames)", flush=True)
            continue

        cap = cv2.VideoCapture(vpath)
        if not cap.isOpened():
            print(f"  Warning: cannot open {vpath}", flush=True)
            continue

        fps      = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n_total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        stride   = max(1, int(fps * sample_interval))
        h        = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        w_vid    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        dim      = min(w_vid, h)
        r_min    = max(20, dim // 5)
        r_max    = dim // 2 + 20
        video_entries: list[dict] = []

        frame_idx = 0
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                break
            gray   = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            result = _find_disk_lunar(gray, r_min, r_max)
            if result:
                cx, cy, r = result
                quality   = _disk_sharpness(gray, cx, cy, r)
                offset_s  = frame_idx / fps
                ts_utc    = (
                    (start_utc + timedelta(seconds=offset_s)).isoformat()
                    if start_utc else None
                )
                video_entries.append({
                    "video": vpath, "frame_idx": frame_idx,
                    "ts_utc": ts_utc,
                    "cx": cx, "cy": cy, "r": r, "quality": quality,
                })
            frame_idx += stride
            if n_total > 0 and frame_idx >= n_total:
                break

        cap.release()
        print(f"    detected disk in {len(video_entries)} samples", flush=True)

        if video_entries:
            radii  = [e["r"] for e in video_entries]
            r_med  = float(np.median(radii))
            before = len(video_entries)
            video_entries = [
                e for e in video_entries
                if abs(e["r"] / r_med - 1.0) <= MAX_RADIUS_DRIFT
                   and e["quality"] >= MIN_QUALITY
            ]
            if (before - len(video_entries)):
                print(f"    rejected {before - len(video_entries)} frames", flush=True)

        raw_frames.extend(video_entries)
        if "video_cache" not in cache:
            cache["video_cache"] = {}
        cache["video_cache"][cache_key] = video_entries

    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh, indent=2)
    except Exception as exc:
        print(f"  Warning: could not write cache: {exc}", flush=True)

    if not raw_frames:
        print("ERROR: no frames with detected disk.", flush=True)
        sys.exit(1)

    raw_frames.sort(key=lambda e: (e.get("ts_utc") or "0000", e["video"], e["frame_idx"]))
    print(f"  Total accepted frames: {len(raw_frames)}", flush=True)

    # ── Pass 2 ───────────────────────────────────────────────────────────────
    _progress(40, "Pass 2 — Normalising and stretching frames…")

    normed_frames:  list[np.ndarray] = []
    frame_ts_utc:   list[Optional[str]] = []
    frame_quality:  list[float] = []
    open_caps: dict[str, cv2.VideoCapture] = {}

    def _get_cap(vp: str) -> cv2.VideoCapture:
        if vp not in open_caps:
            open_caps[vp] = cv2.VideoCapture(vp)
        return open_caps[vp]

    # Determine target_r from median detected radius across all frames
    # (adapts to the actual video resolution)
    all_radii = [e["r"] for e in raw_frames]
    median_r  = float(np.median(all_radii))
    # target_r stays fixed at out_size * TARGET_R_FRAC

    for i, entry in enumerate(raw_frames):
        if i % 50 == 0:
            pct = 40 + int(i / len(raw_frames) * 33)
            _progress(pct, f"  Normalising frame {i+1}/{len(raw_frames)}…")

        cap = _get_cap(entry["video"])
        cap.set(cv2.CAP_PROP_POS_FRAMES, entry["frame_idx"])
        ok, frame = cap.read()
        if not ok:
            continue

        normed    = _normalise_frame(frame, entry["cx"], entry["cy"], entry["r"],
                                     out_size, target_r)
        stretched = _stretch_frame(normed, sky_pct, high_pct, gamma, out_size, target_r)
        normed_frames.append(stretched)
        frame_ts_utc.append(entry["ts_utc"])
        frame_quality.append(entry["quality"])

    for cap in open_caps.values():
        cap.release()

    if not normed_frames:
        print("ERROR: no normalised frames produced.", flush=True)
        sys.exit(1)

    _progress(73, f"  Normalised {len(normed_frames)} frames")

    # ── Pass 3 ───────────────────────────────────────────────────────────────
    _progress(74, "Pass 3 — Assembling timelapse…")

    # Portrait
    best_idx  = int(np.argmax(frame_quality))
    portrait  = normed_frames[best_idx].copy()
    ts_valid  = [t for t in frame_ts_utc if t]
    date_label = ts_valid[0][:10] if ts_valid else "Lunar"
    if ts_valid and ts_valid[-1][:10] != ts_valid[0][:10]:
        date_label += f" – {ts_valid[-1][:10]}"

    # Phase label for portrait
    ts_best: Optional[datetime] = None
    if frame_ts_utc[best_idx]:
        try:
            ts_best = datetime.fromisoformat(frame_ts_utc[best_idx])
        except Exception:
            pass
    illum, age = _lunar_illumination(ts_best)
    pname      = _phase_name(age)
    portrait_text = f"))) {pname}  {illum:.0f}%  ·  {date_label}  ·  {len(normed_frames)} frames"
    portrait = _overlay_label(portrait, portrait_text, pos="bottom")
    portrait_path = os.path.join(out_dir, "lunar_portrait.jpg")
    cv2.imwrite(portrait_path, portrait, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"  Wrote portrait → {portrait_path}", flush=True)

    # VFR durations (same as solar)
    n       = len(normed_frames)
    min_dur = 1.0 / 60.0
    max_dur = 5.0
    durations: list[float] = []
    for i in range(n - 1):
        ts_c = frame_ts_utc[i]
        ts_n = frame_ts_utc[i + 1]
        if ts_c and ts_n:
            try:
                dt = (datetime.fromisoformat(ts_n) -
                      datetime.fromisoformat(ts_c)).total_seconds()
            except ValueError:
                dt = sample_interval
        else:
            dt = sample_interval
        durations.append(max(min_dur, min(max_dur, dt / speedup)))
    durations.append(durations[-1] if durations else 1.0 / 30.0)

    timelapse_path = os.path.join(out_dir, "lunar_session.mp4")
    _progress(82, f"  Encoding {n} frames → {Path(timelapse_path).name}")
    _write_mp4(timelapse_path, normed_frames, durations)
    print(f"  Wrote timelapse → {timelapse_path}", flush=True)

    _progress(100, "Done.")
    return {
        "timelapse":   timelapse_path if os.path.isfile(timelapse_path) else None,
        "portrait":    portrait_path  if os.path.isfile(portrait_path)  else None,
        "mosaic":      None,
        "frame_count": n,
        "date_label":  date_label,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seestar lunar timelapse processor")
    sub = p.add_subparsers(dest="mode", required=True)

    # phase subcommand
    ph = sub.add_parser("phase", help="Phase-sequence timelapse (cross-session)")
    ph.add_argument("output_dir", help="Directory to write outputs")
    ph.add_argument("--sessions-json", required=True,
                    help='JSON array of {name, paths:[...], date} objects')
    ph.add_argument("--size",        type=int,   default=1080)
    ph.add_argument("--gamma",       type=float, default=0.8)
    ph.add_argument("--sky-pct",     type=float, default=5.0)
    ph.add_argument("--high-pct",    type=float, default=99.5)
    ph.add_argument("--frame-hold",  type=float, default=1.5,
                    help="Seconds each frame is displayed (default 1.5)")
    ph.add_argument("--no-cache",    action="store_true")

    # single subcommand
    sg = sub.add_parser("single", help="Single-session colour timelapse")
    sg.add_argument("directory", help="Source/output directory")
    sg.add_argument("--files-json", default=None)
    sg.add_argument("--size",            type=int,   default=1080)
    sg.add_argument("--sample-interval", type=float, default=1.0)
    sg.add_argument("--speedup",         type=float, default=1800.0)
    sg.add_argument("--gamma",           type=float, default=0.8)
    sg.add_argument("--sky-pct",         type=float, default=5.0)
    sg.add_argument("--high-pct",        type=float, default=99.5)
    sg.add_argument("--no-cache",        action="store_true")

    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.mode == "phase":
        sessions = json.loads(args.sessions_json)
        if not sessions:
            print("ERROR: no sessions provided.", flush=True)
            sys.exit(1)
        print(f"[Lunar Processor] phase mode — {len(sessions)} session(s) → {args.output_dir}",
              flush=True)
        run_phase(
            sessions    = sessions,
            out_dir     = args.output_dir,
            out_size    = args.size,
            gamma       = args.gamma,
            sky_pct     = args.sky_pct,
            high_pct    = args.high_pct,
            frame_hold  = args.frame_hold,
            no_cache    = args.no_cache,
        )

    else:  # single
        if args.files_json:
            video_files = json.loads(args.files_json)
        else:
            video_files = sorted(
                str(f) for f in Path(args.directory).iterdir()
                if f.suffix.lower() in VIDEO_EXT and not f.name.startswith("lunar_")
            )
        if not video_files:
            print("ERROR: no video files found.", flush=True)
            sys.exit(1)
        print(f"[Lunar Processor] single mode — {len(video_files)} video(s) → {args.directory}",
              flush=True)
        run_single(
            video_files     = video_files,
            out_dir         = args.directory,
            out_size        = args.size,
            sample_interval = args.sample_interval,
            speedup         = args.speedup,
            gamma           = args.gamma,
            sky_pct         = args.sky_pct,
            high_pct        = args.high_pct,
            no_cache        = args.no_cache,
        )
