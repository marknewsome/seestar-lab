#!/usr/bin/env python3
"""
Seestar Lab — Solar timelapse processor (Phase 1).

════════════════════════════════════════════════════════════════════════════════
OUTPUTS  (written to the output directory — defaults to first video's parent)
════════════════════════════════════════════════════════════════════════════════

  solar_fulldisk.mp4    VFR timelapse of disk-normalised frames.  Frame pacing
                        is proportional to real time gaps so intervals within a
                        continuous session appear smooth; gaps between separate
                        sessions compress proportionally.

  solar_portrait.jpg    Sharpest single normalised frame overlaid with the
                        session date range and sub-count.

  solar_alignment.json  Per-frame disk detection cache (cx, cy, r, quality,
                        timestamp_utc).  Avoids re-running HoughCircles on a
                        re-render with different stretch or speed settings.

════════════════════════════════════════════════════════════════════════════════
PIPELINE  (3 passes)
════════════════════════════════════════════════════════════════════════════════

Pass 1 — Frame extraction & disk detection
  Each source video is sampled at ~1 frame / sample_interval seconds.  A UTC
  timestamp is assigned to each sample using the recording-start time embedded
  in the Seestar filename (YYYY-MM-DD-HHMMSS) plus the in-video offset.  The
  frame is converted to grayscale and the solar disk is located via
  HoughCircles with a broad radius search (dim//5 … dim//2+20).  A Laplacian
  variance score estimates limb sharpness / focus quality.  Frames whose
  detected radius deviates more than 15 % from the per-video median radius are
  rejected (cloud cover / severe defocus).

Pass 2 — Normalisation
  Each accepted frame is mapped to a fixed out_size × out_size canvas:
    scale   = target_r / detected_r
    tx, ty  = out_size/2 − scale·cx,  out_size/2 − scale·cy
  A 2×3 affine matrix is applied with cv2.warpAffine.  The result is then
  background-subtracted (sky percentile of the disk annulus) and gamma-curved.
  Each normalised frame is written immediately to a temporary JPEG on disk
  rather than accumulated in memory — peak RAM stays at O(1) regardless of
  session length.  (A long solar session can produce thousands of frames;
  holding them all as NumPy arrays caused OOM kills at ~21 GB RSS.)

Pass 3 — VFR timelapse assembly
  Frames are sorted by UTC timestamp.  The display duration for each frame is
  real_gap_to_next_frame / speedup_factor, clamped to [1/60, 5] seconds.
  The ffconcat manifest references the already-written Pass 2 JPEGs directly,
  so there is no second full-frame copy in RAM.  Falls back to fixed-fps
  VideoWriter when ffmpeg is unavailable.  The temporary frame directory is
  deleted in a finally block after the MP4 is written.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False

# ── Tunable constants ─────────────────────────────────────────────────────────

VIDEO_TIMEZONE    = "America/Los_Angeles"
TARGET_R_FRAC     = 0.43        # disk radius / output_size  →  ~86 % fill
MAX_RADIUS_DRIFT  = 0.15        # reject frame if |r/r_median − 1| > this
DISK_BLUR_K       = 9           # GaussianBlur kernel for HoughCircles
MIN_QUALITY       = 5.0         # Laplacian variance threshold; below = reject
TITLE_SECS        = 3.0         # duration of the title card at the start of the timelapse

# Filename date pattern: YYYY-MM-DD-HHMMSS
_FNAME_DT_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})')

# ── Timestamp parser ──────────────────────────────────────────────────────────

def _parse_video_start_utc(video_path: str) -> Optional[datetime]:
    """Parse recording-start UTC from a Seestar filename like 2025-08-05-085206-Solar.mp4."""
    m = _FNAME_DT_RE.search(Path(video_path).stem)
    if not m:
        return None
    if not _HAVE_ZONEINFO:
        # Fall back to treating as UTC
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


# ── Disk detection ────────────────────────────────────────────────────────────

def _find_disk(gray: np.ndarray) -> Optional[tuple[int, int, int]]:
    """
    Locate the solar disk in a grayscale frame.  Returns (cx, cy, r) or None.
    """
    h, w = gray.shape[:2]
    dim  = min(w, h)
    blurred = cv2.GaussianBlur(gray, (DISK_BLUR_K, DISK_BLUR_K), 2)

    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.5,
        minDist=dim // 2,
        param1=50, param2=30,
        minRadius=dim // 5,
        maxRadius=dim // 2 + 20,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    # Fallback: fit circle to the largest bright contour
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_OTSU)
    cnts, _   = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        largest   = max(cnts, key=cv2.contourArea)
        (x, y), r = cv2.minEnclosingCircle(largest)
        if r > dim // 5:
            return int(x), int(y), max(1, int(r))

    return None


def _disk_sharpness(gray: np.ndarray, cx: int, cy: int, r: int) -> float:
    """
    Laplacian-variance sharpness score inside the disk.  Higher = sharper /
    better focused.
    """
    mask  = np.zeros(gray.shape, dtype=np.uint8)
    # Sample a slightly inset annulus to avoid limb-darkening edge effects
    inner = max(1, int(r * 0.3))
    outer = max(inner + 1, int(r * 0.95))
    cv2.circle(mask, (cx, cy), outer, 255, -1)
    cv2.circle(mask, (cx, cy), inner,   0, -1)
    roi = cv2.Laplacian(gray, cv2.CV_64F)
    vals = roi[mask == 255]
    return float(np.var(vals)) if vals.size > 0 else 0.0


# ── Frame normalisation ───────────────────────────────────────────────────────

def _normalise_frame(
    frame_bgr: np.ndarray,
    cx: int, cy: int, r: int,
    out_size: int,
    target_r: int,
) -> np.ndarray:
    """
    Apply affine warp to centre the disk and scale its radius to target_r.
    Returns a (out_size × out_size) BGR image.
    """
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
    Histogram stretch: subtract sky percentile (sampled from the black corners
    outside the disk), scale by the high percentile, apply gamma.
    """
    f32 = frame_bgr.astype(np.float32)

    # Build a corner mask (outside the disk) to sample sky/background
    corner_mask = np.ones((out_size, out_size), dtype=np.uint8)
    cx = cy = out_size // 2
    cv2.circle(corner_mask, (cx, cy), int(target_r * 0.97), 0, -1)

    for c in range(3):
        ch = f32[..., c]
        bg_vals = ch[corner_mask == 1]
        sky = float(np.percentile(bg_vals, sky_pct)) if bg_vals.size > 0 else 0.0
        ch -= sky

    lum = 0.299 * f32[..., 2] + 0.587 * f32[..., 1] + 0.114 * f32[..., 0]
    disk_vals = lum[corner_mask == 0]
    hi_val = float(np.percentile(disk_vals, high_pct)) if disk_vals.size > 0 else 1.0
    if hi_val > 0:
        f32 /= hi_val

    f32  = np.power(np.clip(f32, 0.0, 1.0), gamma)
    return (np.clip(f32, 0.0, 1.0) * 255).astype(np.uint8)


# ── Overlay ───────────────────────────────────────────────────────────────────

def _make_title_frame(w: int, h: int,
                      date_label: str,
                      frame_count: int,
                      session_dur: str) -> np.ndarray:
    """Render a title-card BGR frame for the start of the solar timelapse."""
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

    # Title — warm golden / amber accent
    _put("Solar Timelapse", int(h * 0.33), 1.2, (50, 185, 255), 2)

    # Thin divider
    lw = int(w * 0.36)
    ly = int(h * 0.45)
    cv2.line(frame, (cx - lw // 2, ly), (cx + lw // 2, ly),
             (20, 80, 90), max(1, round(scale)))

    # Date label
    _put(date_label, int(h * 0.57), 0.95, (210, 210, 210), 1)

    # Frame count + session duration
    detail = f"{frame_count} frames"
    if session_dur:
        detail += f"  |  {session_dur} session"
    _put(detail, int(h * 0.70), 0.65, (115, 115, 115), 1)

    # Bottom-right watermark
    wm    = "Seestar Lab"
    wm_fs = 0.42 * scale
    (ww, _), _ = cv2.getTextSize(wm, font, wm_fs, 1)
    cv2.putText(frame, wm,
                (w - ww - int(18 * scale), h - int(18 * scale)),
                font, wm_fs, (55, 55, 55), 1, cv2.LINE_AA)

    return frame


def _prepend_title(frames: list,
                   durations: Optional[list[float]],
                   date_label: str,
                   frame_count: int,
                   session_dur: str) -> tuple:
    """Prepend a title card to the timelapse frames (and matching duration).

    ``frames`` may be a list of numpy arrays **or** a list of JPEG file paths
    (str).  When paths are supplied the title card is also written to a JPEG
    file and its path is prepended.
    """
    if not frames:
        return frames, durations

    if isinstance(frames[0], str):
        first = cv2.imread(frames[0])
        h, w  = first.shape[:2]
    else:
        h, w = frames[0].shape[:2]

    title = _make_title_frame(w, h, date_label, frame_count, session_dur)

    if isinstance(frames[0], str):
        title_path = os.path.join(os.path.dirname(frames[0]), "title.jpg")
        cv2.imwrite(title_path, title, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if durations is not None:
            return [title_path] + list(frames), [TITLE_SECS] + list(durations)
        n_title = max(1, round(TITLE_SECS * 30))
        return [title_path] * n_title + list(frames), None

    if durations is not None:
        return [title] + frames, [TITLE_SECS] + durations
    n_title = max(1, round(TITLE_SECS * 30))
    return [title] * n_title + frames, None


def _overlay_label(img: np.ndarray, text: str, pos: str = "bottom") -> np.ndarray:
    """Overlay a white label with a dark drop shadow."""
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


# ── Video writer ──────────────────────────────────────────────────────────────

def _write_mp4(
    path:       str,
    frames_bgr: list,
    durations:  Optional[list],
    fps_cfr:    int = 30,
) -> None:
    """
    Write frames to an MP4.  When durations is provided, uses the ffconcat
    demuxer for a VFR output.  Falls back to fixed-fps VideoWriter.

    ``frames_bgr`` may be a list of numpy arrays **or** a list of JPEG file
    paths (str).  When paths are supplied they are referenced directly in the
    ffconcat manifest without re-encoding to avoid a second full copy in RAM.
    """
    if not frames_bgr:
        return

    ffbin = "/usr/bin/ffmpeg" if os.path.isfile("/usr/bin/ffmpeg") else shutil.which("ffmpeg")

    if durations is not None and ffbin:
        paths_on_disk = isinstance(frames_bgr[0], str)
        # Only need a temp dir for the concat manifest (and any numpy frames).
        tmp_dir = tempfile.mkdtemp(prefix="solar_vfr_")
        try:
            concat_path = os.path.join(tmp_dir, "frames.txt")
            with open(concat_path, "w") as fh:
                fh.write("ffconcat version 1.0\n")
                for j, (frame, dur) in enumerate(zip(frames_bgr, durations)):
                    if paths_on_disk:
                        jpg = frame          # already written to disk
                    else:
                        jpg = os.path.join(tmp_dir, f"f{j:05d}.jpg")
                        cv2.imwrite(jpg, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    fh.write(f"file '{jpg}'\nduration {dur:.6f}\n")
                last_frame = frames_bgr[-1]
                last = last_frame if paths_on_disk else os.path.join(tmp_dir, f"f{len(frames_bgr)-1:05d}.jpg")
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
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # CFR fallback
    if not frames_bgr:
        return
    first = cv2.imread(frames_bgr[0]) if isinstance(frames_bgr[0], str) else frames_bgr[0]
    h, w   = first.shape[:2]
    raw    = path + ".raw.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(raw, fourcc, fps_cfr, (w, h))
    for f in frames_bgr:
        writer.write(cv2.imread(f) if isinstance(f, str) else f)
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


# ── Disk-position smoother ────────────────────────────────────────────────────

def _smooth_positions(frames: list[dict], window: int) -> None:
    """
    Gaussian-weighted rolling smooth of the detected (cx, cy, r) across frames.
    Reduces jitter caused by wind shake without blurring slow intentional drift.
    `window` is the full kernel width in frames (should be odd; e.g. 7 = ±3 frames).
    Edges are handled by padding with the nearest detected value so border frames
    are not pulled toward zero.
    """
    if window < 3 or len(frames) < 2:
        return
    half  = window // 2
    sigma = window / 4.0
    k     = np.arange(-half, half + 1, dtype=np.float64)
    kern  = np.exp(-k ** 2 / (2.0 * sigma ** 2))
    kern /= kern.sum()

    for key in ("cx", "cy", "r"):
        arr    = np.array([f[key] for f in frames], dtype=np.float64)
        padded = np.pad(arr, half, mode="edge")
        smoothed = np.convolve(padded, kern, mode="valid")[: len(frames)]
        for i, f in enumerate(frames):
            f[key] = float(smoothed[i])


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    video_files:     list[str],
    out_dir:         str,
    out_size:        int   = 1080,
    sample_interval: float = 1.0,
    speedup:         float = 1800.0,
    gamma:           float = 0.7,
    sky_pct:         float = 5.0,
    high_pct:        float = 99.5,
    no_cache:        bool  = False,
    min_quality:     float = None,
    stab_window:     int   = 0,
    progress_cb             = None,
) -> dict:
    """
    Full three-pass pipeline.  Returns dict of output paths.
    progress_cb(pct, message) — optional callable.
    """

    def _progress(pct: int, msg: str) -> None:
        print(f"[Pass {1 if pct < 40 else (2 if pct < 75 else 3)}] {msg}", flush=True)
        if progress_cb:
            progress_cb(pct, msg)

    os.makedirs(out_dir, exist_ok=True)
    target_r     = int(out_size * TARGET_R_FRAC)
    cache_path   = os.path.join(out_dir, "solar_alignment.json")
    _min_quality = min_quality if min_quality is not None else MIN_QUALITY

    # ─────────────────────────────────────────────────────────────────────────
    # Pass 1 — frame extraction & disk detection
    # ─────────────────────────────────────────────────────────────────────────
    _progress(0, "Pass 1 — Detecting solar disk in each frame…")

    # Load cache if available
    cache: dict = {}
    if not no_cache and os.path.isfile(cache_path):
        try:
            with open(cache_path) as fh:
                cache = json.load(fh)
            print(f"  Loaded alignment cache ({len(cache.get('frames', []))} entries)", flush=True)
        except Exception:
            cache = {}

    # Collect raw frame entries: {video, frame_idx, ts_utc, cx, cy, r, quality}
    raw_frames: list[dict] = []
    video_files = sorted(video_files)  # chronological by filename

    for vi, vpath in enumerate(video_files):
        pct_base = int(vi / len(video_files) * 38)
        _progress(pct_base, f"  [{vi+1}/{len(video_files)}] {Path(vpath).name}")

        cache_key  = vpath
        start_utc  = _parse_video_start_utc(vpath)

        # Try to use cached detections for this video
        cached_entries = None
        if not no_cache and cache_key in (cache.get("video_cache") or {}):
            cached_entries = cache["video_cache"][cache_key]

        if cached_entries is not None:
            raw_frames.extend(cached_entries)
            print(f"    (cached: {len(cached_entries)} frames)", flush=True)
            continue

        # Open video
        cap = cv2.VideoCapture(vpath)
        if not cap.isOpened():
            print(f"  Warning: cannot open {vpath}", flush=True)
            continue

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        stride = max(1, int(fps * sample_interval))
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_entries: list[dict] = []

        frame_idx  = 0
        _tick_time = time.time()
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            result = _find_disk(gray)
            if result is not None:
                cx, cy, r = result
                quality = _disk_sharpness(gray, cx, cy, r)
                offset_s = frame_idx / fps
                ts_utc   = (
                    (start_utc + timedelta(seconds=offset_s)).isoformat()
                    if start_utc else None
                )
                video_entries.append({
                    "video":     vpath,
                    "frame_idx": frame_idx,
                    "ts_utc":    ts_utc,
                    "cx": cx, "cy": cy, "r": r,
                    "quality":   quality,
                })

            frame_idx += stride

            # Print a heartbeat every 30 s so the UI shows activity
            now = time.time()
            if now - _tick_time >= 30:
                _tick_time = now
                if n_frames > 0:
                    print(f"    ...{frame_idx}/{n_frames} frames ({frame_idx*100//n_frames}%)"
                          f"  detected={len(video_entries)}", flush=True)
                else:
                    print(f"    ...{frame_idx} frames  detected={len(video_entries)}", flush=True)

            if n_frames > 0 and frame_idx >= n_frames:
                break

        cap.release()
        print(f"    detected disk in {len(video_entries)} samples", flush=True)

        # Radius outlier rejection (per-video)
        if video_entries:
            radii    = [e["r"] for e in video_entries]
            quals    = [e["quality"] for e in video_entries]
            r_med    = float(np.median(radii))
            q_median = float(np.median(quals))
            q_min    = float(np.min(quals))
            q_max    = float(np.max(quals))
            print(f"    quality  min={q_min:.1f}  median={q_median:.1f}  max={q_max:.1f}  (threshold={_min_quality})", flush=True)
            print(f"    radius   median={r_med:.0f}px  (drift limit ±{MAX_RADIUS_DRIFT*100:.0f}%)", flush=True)

            before = len(video_entries)
            passed_r = [
                e for e in video_entries
                if abs(e["r"] / r_med - 1.0) <= MAX_RADIUS_DRIFT
            ]
            passed_q = [e for e in passed_r if e["quality"] >= _min_quality]

            if passed_q:
                video_entries = passed_q
            elif passed_r:
                # Quality threshold rejects everything — use top-half by quality
                # (happens on hazy/compressed video where all scores are low)
                q_cutoff = float(np.percentile([e["quality"] for e in passed_r], 50))
                video_entries = [e for e in passed_r if e["quality"] >= q_cutoff]
                print(f"    quality threshold too strict — keeping top 50% (≥{q_cutoff:.1f})", flush=True)
            else:
                # Radius filter rejects everything — keep by quality only
                q_cutoff = float(np.percentile(quals, 50))
                video_entries = [e for e in video_entries if e["quality"] >= q_cutoff]
                print(f"    radius filter too strict — keeping top 50% by quality", flush=True)

            rejected = before - len(video_entries)
            if rejected:
                print(f"    rejected {rejected} frames  kept {len(video_entries)}", flush=True)

        raw_frames.extend(video_entries)

        # Store in cache
        if "video_cache" not in cache:
            cache["video_cache"] = {}
        cache["video_cache"][cache_key] = video_entries

    # Save cache
    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh, indent=2)
    except Exception as exc:
        print(f"  Warning: could not write cache: {exc}", flush=True)

    if not raw_frames:
        print("ERROR: no frames with detected disk.", flush=True)
        sys.exit(1)

    # Sort by timestamp (fall back to video order for entries without ts)
    def _sort_key(e):
        return (e["ts_utc"] or "0000", e["video"], e["frame_idx"])

    raw_frames.sort(key=_sort_key)
    if stab_window >= 3:
        _smooth_positions(raw_frames, stab_window)
        print(f"  Stabilised disk positions (window={stab_window} frames)", flush=True)
    print(f"  Total accepted frames: {len(raw_frames)}", flush=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Pass 2 — Normalisation & stretch
    # ─────────────────────────────────────────────────────────────────────────
    _progress(40, "Pass 2 — Normalising and stretching frames…")

    frame_ts_utc:   list[Optional[str]] = []
    frame_quality:  list[float] = []

    # Write each normalised frame to disk immediately so we never hold the
    # full frame set in RAM.  At 1080×1080 BGR each frame is ~3.5 MB; for a
    # long solar session (thousands of frames) keeping them all in memory
    # caused OOM kills.
    frame_tmp_dir = tempfile.mkdtemp(prefix="solar_frames_")
    normed_frame_paths: list[str] = []

    # Open videos in order; cache last used cap to avoid repeated re-opens
    open_caps: dict[str, cv2.VideoCapture] = {}

    def _get_cap(vpath: str) -> cv2.VideoCapture:
        if vpath not in open_caps:
            open_caps[vpath] = cv2.VideoCapture(vpath)
        return open_caps[vpath]

    try:
      for i, entry in enumerate(raw_frames):
        if i % 50 == 0:
            pct = 40 + int(i / len(raw_frames) * 33)
            _progress(pct, f"  Normalising frame {i+1}/{len(raw_frames)}…")

        cap     = _get_cap(entry["video"])
        cap.set(cv2.CAP_PROP_POS_FRAMES, entry["frame_idx"])
        ok, frame = cap.read()
        if not ok:
            continue

        normed    = _normalise_frame(frame, entry["cx"], entry["cy"], entry["r"],
                                     out_size, target_r)
        stretched = _stretch_frame(normed, sky_pct, high_pct, gamma, out_size, target_r)

        if entry.get("ts_utc"):
            # Format as "YYYY-MM-DD  HH:MM:SS UTC" — replace the T separator with two spaces
            ts_label = entry["ts_utc"].replace("T", "  ").split(".")[0] + " UTC"
            stretched = _overlay_label(stretched, ts_label, pos="top")

        jpg_path = os.path.join(frame_tmp_dir, f"f{i:05d}.jpg")
        cv2.imwrite(jpg_path, stretched, [cv2.IMWRITE_JPEG_QUALITY, 92])
        normed_frame_paths.append(jpg_path)
        frame_ts_utc.append(entry["ts_utc"])
        frame_quality.append(entry["quality"])

      for cap in open_caps.values():
          cap.release()

      if not normed_frame_paths:
          print("ERROR: no normalised frames produced.", flush=True)
          sys.exit(1)

      _progress(73, f"  Normalised {len(normed_frame_paths)} frames")

      # ─────────────────────────────────────────────────────────────────────────
      # Pass 3 — Portrait + timelapse assembly
      # ─────────────────────────────────────────────────────────────────────────
      _progress(74, "Pass 3 — Assembling timelapse…")

      # Portrait: sharpest frame — read back from disk (single frame, tiny RAM)
      best_idx = int(np.argmax(frame_quality))
      portrait = cv2.imread(normed_frame_paths[best_idx])

      # Build date-range label
      ts_valid = [t for t in frame_ts_utc if t]
      if ts_valid:
          t0 = ts_valid[0][:10]
          t1 = ts_valid[-1][:10]
          date_label = t0 if t0 == t1 else f"{t0} – {t1}"
      else:
          date_label = "Solar"
      n_frames = len(normed_frame_paths)
      n_frames_str = f"Solar  |  {n_frames} frames  |  {date_label}"

      portrait = _overlay_label(portrait, n_frames_str, pos="bottom")
      portrait_path = os.path.join(out_dir, "solar_portrait.jpg")
      cv2.imwrite(portrait_path, portrait, [cv2.IMWRITE_JPEG_QUALITY, 95])
      print(f"  Wrote portrait → {portrait_path}", flush=True)

      # Compute VFR durations
      durations: list[float] = []
      min_dur  = 1.0 / 60.0
      max_dur  = 5.0

      for i in range(n_frames - 1):
          ts_curr = frame_ts_utc[i]
          ts_next = frame_ts_utc[i + 1]
          if ts_curr and ts_next:
              try:
                  dt = (datetime.fromisoformat(ts_next) -
                        datetime.fromisoformat(ts_curr)).total_seconds()
              except ValueError:
                  dt = sample_interval
          else:
              dt = sample_interval
          dur = max(min_dur, min(max_dur, dt / speedup))
          durations.append(dur)
      durations.append(durations[-1] if durations else 1.0 / 30.0)

      # Compute session duration for title card
      session_dur = ""
      if len(ts_valid) >= 2:
          try:
              elapsed = (datetime.fromisoformat(ts_valid[-1]) -
                         datetime.fromisoformat(ts_valid[0])).total_seconds()
              h_s, rem = divmod(int(elapsed), 3600)
              m_s = rem // 60
              session_dur = f"{h_s}h {m_s:02d}m" if h_s else f"{m_s}m"
          except Exception:
              pass

      # Prepend title card
      title_frames, title_durations = _prepend_title(
          normed_frame_paths, durations, date_label, n_frames, session_dur)

      # Write timelapse
      timelapse_path = os.path.join(out_dir, "solar_fulldisk.mp4")
      _progress(80, f"  Encoding {n_frames} frames → {Path(timelapse_path).name}")
      _write_mp4(timelapse_path, title_frames, title_durations)
      print(f"  Wrote timelapse → {timelapse_path}", flush=True)

      _progress(100, "Done.")
      return {
          "timelapse": timelapse_path if os.path.isfile(timelapse_path) else None,
          "portrait":  portrait_path  if os.path.isfile(portrait_path)  else None,
          "frame_count": n_frames,
          "date_label":  date_label,
      }

    finally:
        shutil.rmtree(frame_tmp_dir, ignore_errors=True)


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seestar solar timelapse processor")
    p.add_argument("directory", help="Output directory (also default search path)")
    p.add_argument("--files-json", default=None,
                   help="JSON array of video paths to process")
    p.add_argument("--size",            type=int,   default=1080)
    p.add_argument("--sample-interval", type=float, default=1.0,
                   help="Seconds between sampled frames per video (default 1.0)")
    p.add_argument("--speedup",         type=float, default=1800.0,
                   help="Real-time speedup factor for VFR pacing (default 1800 = 30min→1sec)")
    p.add_argument("--gamma",           type=float, default=0.7)
    p.add_argument("--sky-pct",         type=float, default=5.0)
    p.add_argument("--high-pct",        type=float, default=99.5)
    p.add_argument("--no-cache",        action="store_true")
    p.add_argument("--min-quality",     type=float, default=None,
                   help="Override Laplacian variance quality threshold (default 5.0)")
    p.add_argument("--stab-window",     type=int,   default=0,
                   help="Stabilisation smoothing window in frames (0=off, e.g. 7)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Collect video files
    if args.files_json:
        video_files = json.loads(args.files_json)
    else:
        VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv"}
        d = Path(args.directory)
        video_files = sorted(
            str(f) for f in d.iterdir()
            if f.suffix.lower() in VIDEO_EXT and not f.name.startswith("solar_")
        )

    if not video_files:
        print("ERROR: no video files found.", flush=True)
        sys.exit(1)

    print(f"[Solar Processor] {len(video_files)} video(s) → {args.directory}", flush=True)

    run(
        video_files     = video_files,
        out_dir         = args.directory,
        out_size        = args.size,
        sample_interval = args.sample_interval,
        speedup         = args.speedup,
        gamma           = args.gamma,
        sky_pct         = args.sky_pct,
        high_pct        = args.high_pct,
        no_cache        = args.no_cache,
        min_quality     = args.min_quality,
        stab_window     = args.stab_window,
    )
