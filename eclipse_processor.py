#!/usr/bin/env python3
"""
Seestar Lab — Lunar eclipse processor.

Three passes over OBS recordings of a total lunar eclipse:

  Pass 1 — Quality scan  (cache-able)
      Seeks to one frame every SCAN_INTERVAL_S seconds across all files.
      Per frame: detect the lunar disk, measure edge sharpness (cloud proxy)
      and disk brightness (eclipse-depth proxy).  Writes a JSON manifest so
      later passes can be re-run without repeating the scan.

  Pass 2 — Timelapse
      For each TIMELAPSE_INTERVAL_S window, picks the highest-quality frame
      from the manifest, re-extracts it from the source video, crops/normalises
      to a fixed square, and writes a timelapse frame.  Cloud gaps become a
      labelled black frame so the video is honest about conditions.

  Pass 3 — Panel
      Selects PANEL_COLS × PANEL_ROWS representative frames spaced across
      the eclipse arc, crops each to the lunar disk, labels phase and time,
      and composes a high-resolution grid montage.

Usage:
    python -u eclipse_processor.py

    Add --scan-only to stop after Pass 1 and inspect the JSON before
    committing to the 20-30 minute extraction passes.

Outputs (all written to ECLIPSE_DIR):
    eclipse_quality_scan.json   — per-sample manifest (cached)
    eclipse_timelapse.mp4       — ~30 s eclipse timelapse
    eclipse_panel.jpg           — 4×3 phase grid
    eclipse_keyframes/          — individual panel tile JPEGs
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ── Output paths ───────────────────────────────────────────────────────────────

ECLIPSE_DIR   = "/mnt/d/xfer/Lunar_eclipse_2026-03-03"
OUT_DIR       = ECLIPSE_DIR
SCAN_JSON     = os.path.join(OUT_DIR, "eclipse_quality_scan.json")
TIMELAPSE_OUT = os.path.join(OUT_DIR, "eclipse_timelapse.mp4")
PANEL_OUT     = os.path.join(OUT_DIR, "eclipse_panel.jpg")
KEYFRAMES_DIR = os.path.join(OUT_DIR, "eclipse_keyframes")

# ── Tunable parameters ─────────────────────────────────────────────────────────

SCAN_INTERVAL_S       = 15     # seek to 1 frame per N seconds for quality scan
TIMELAPSE_INTERVAL_S  = 30     # 1 output frame per N real seconds
TIMELAPSE_FPS         = 24     # output timelapse frame rate
TIMELAPSE_SIZE        = 520    # output frame square size (pixels)
PANEL_COLS            = 4      # panel grid columns
PANEL_ROWS            = 3      # panel grid rows  → 12 tiles
PANEL_TILE_PX         = 320    # each tile size (pixels, square)
PANEL_LABEL_H         = 44     # height of label strip below each panel tile
MIN_QUALITY_TIMELAPSE = 0.10   # below this → cloud-gap placeholder frame
MIN_QUALITY_PANEL     = 0.10   # below this → skip tile (show cloud-gap)

# Eclipse-phase brightness thresholds
# (mean DN of detected disk after OBS auto-gain, 0–255).
# Calibrated from actual data: Seestar RTSP auto-exposure holds the full moon
# at ~75-95 DN; deep-partial/totality = 5-55 DN; clouds = 0-1 DN.
BRIGHTNESS_PARTIAL    =  68    # below → umbral shadow clearly visible
BRIGHTNESS_TOTALITY   =  15    # below → likely in or near totality

# Blood-moon colour boost for totality frames.
# Seestar auto-white-balance tends to neutralise the reddish tint; this
# partially restores it.  Set both to 1.0 to disable.
TOTALITY_RED_SCALE    = 1.35
TOTALITY_BLUE_SCALE   = 0.78

# Normalisation stretch cap: larger values make dark (totality) frames
# brighter at the cost of amplifying noise.  Calibrated for this dataset.
NORM_MAX_FACTOR       = 8.0    # was 3.8; boost for very dark totality/partial frames
NORM_TARGET           = 175    # target mean disk brightness after stretch

# Extra sky margin around the moon radius for crops
MOON_PAD              = 0.20

# ── Timezone (OBS filenames use local time) ────────────────────────────────────

try:
    from zoneinfo import ZoneInfo as _ZI
    _TZ = _ZI("America/Los_Angeles")
except ImportError:
    _TZ = None

_OBS_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2})-(\d{2})-(\d{2})')


def _parse_file_start(path: str) -> Optional[datetime]:
    m = _OBS_RE.search(Path(path).stem)
    if not m:
        return None
    naive = datetime(int(m[1]), int(m[2]), int(m[3]),
                     int(m[4]), int(m[5]), int(m[6]))
    return naive.replace(tzinfo=_TZ) if _TZ else naive


def _fmt_time(dt: Optional[datetime]) -> str:
    return dt.strftime("%-I:%M:%S %p") if dt else "?"


def _fmt_time_long(dt: Optional[datetime]) -> str:
    return dt.strftime("%b %-d  %-I:%M %p") if dt else "?"


# ── Moon detection ─────────────────────────────────────────────────────────────

def _detect_moon(gray: np.ndarray) -> Optional[tuple[int, int, int]]:
    """
    Return (cx, cy, radius) or None.
    Tries HoughCircles at three accumulator thresholds (progressively more
    lenient) to handle both the bright full moon and faint totality moon.
    Falls back to the largest bright contour.
    """
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    dim = min(gray.shape[:2])

    for p2 in (45, 28, 16):
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.5,
            minDist=dim // 2,
            param1=50, param2=p2,
            minRadius=dim // 5,
            maxRadius=dim // 2 + 20,
        )
        if circles is not None:
            c = np.round(circles[0][0]).astype(int)
            return int(c[0]), int(c[1]), int(c[2])

    # Fallback: largest bright contour (works for crescent / totality)
    _, th = cv2.threshold(blurred, 0, 255, cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        lg = max(cnts, key=cv2.contourArea)
        (x, y), r = cv2.minEnclosingCircle(lg)
        if r > dim // 8:
            return int(x), int(y), max(1, int(r))
    return None


def _edge_sharpness(gray: np.ndarray, cx: int, cy: int, r: int) -> float:
    """
    0–1 sharpness of the disk edge (Sobel gradient in the rim annulus).
    High = clear, sharp limb.  Low = clouds blurring the edge.
    """
    h, w = gray.shape
    outer = np.zeros((h, w), dtype=np.uint8)
    inner = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(outer, (cx, cy), min(r + 14, dim - 1 if (dim := min(h, w) // 2) else 1), 255, -1)
    cv2.circle(inner, (cx, cy), max(r - 14, 1), 255, -1)
    ring = outer & ~inner
    if not ring.any():
        return 0.0
    sx = cv2.Sobel(gray.astype(np.float32), cv2.CV_32F, 1, 0)
    sy = cv2.Sobel(gray.astype(np.float32), cv2.CV_32F, 0, 1)
    grad = np.sqrt(sx ** 2 + sy ** 2)
    return float(min(1.0, grad[ring > 0].mean() / 80.0))


def _assess(gray: np.ndarray,
            fb_cx: int, fb_cy: int, fb_r: int) -> dict:
    """Assess a frame: detect moon, compute quality metrics."""
    det = _detect_moon(gray)
    cx, cy, r = det if det else (fb_cx, fb_cy, fb_r)

    mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.circle(mask, (cx, cy), max(r - 5, 1), 255, -1)
    dpix = gray[mask > 0]

    brightness = float(dpix.mean()) if len(dpix) else 0.0
    fill       = float((dpix > dpix.max() * 0.28).sum() / max(len(dpix), 1)) if len(dpix) else 0.0
    sharpness  = _edge_sharpness(gray, cx, cy, r) if det else 0.0
    quality    = sharpness * (0.55 + 0.45 * fill) if det else 0.0

    return {
        "detected":   bool(det),
        "cx": cx, "cy": cy, "radius": r,
        "sharpness":  round(sharpness,  3),
        "brightness": round(brightness, 1),
        "fill":       round(fill,       3),
        "quality":    round(quality,    3),
    }


# ── Frame utilities ────────────────────────────────────────────────────────────

def _crop_moon(bgr: np.ndarray, cx: int, cy: int, r: int,
               out_size: int) -> np.ndarray:
    """Crop a padded square around the moon, resize to out_size."""
    half = int(r * (1.0 + MOON_PAD))
    h, w = bgr.shape[:2]
    x1, y1 = max(0, cx - half), max(0, cy - half)
    x2, y2 = min(w, cx + half), min(h, cy + half)
    crop = bgr[y1:y2, x1:x2]
    ch, cw = crop.shape[:2]
    side = max(ch, cw, 1)
    canvas = np.zeros((side, side, 3), dtype=np.uint8)
    oy, ox = (side - ch) // 2, (side - cw) // 2
    canvas[oy:oy + ch, ox:ox + cw] = crop
    return cv2.resize(canvas, (out_size, out_size), interpolation=cv2.INTER_AREA)


def _refine_center(bgr: np.ndarray, cx: int, cy: int, r: int) -> tuple[float, float]:
    """
    Refine an approximate lunar disk centre using the centroid of disk pixels.

    Converts to grayscale, masks to the circle ROI, then finds the centroid of
    the brightest pixels (top 40% within the disk, clamped to ≥ background+6 DN).
    Falls back to the original cx/cy if too few pixels are found.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    # ROI bounding box
    x1, y1 = max(0, cx - r), max(0, cy - r)
    x2, y2 = min(w, cx + r), min(h, cy + r)
    roi = gray[y1:y2, x1:x2].astype(np.float32)
    # Circular mask within ROI
    ry, rx = np.ogrid[:roi.shape[0], :roi.shape[1]]
    lcx, lcy = cx - x1, cy - y1
    disk_mask = ((rx - lcx) ** 2 + (ry - lcy) ** 2) <= r * r
    pixels = roi[disk_mask]
    if pixels.size == 0:
        return float(cx), float(cy)
    # Threshold at 60th percentile — keeps the brightest part of the disk
    thresh = max(float(np.percentile(pixels, 60)), np.mean(pixels[pixels > 0]) * 0.6
                 if np.any(pixels > 0) else 1.0)
    bright_mask = disk_mask & (roi >= thresh)
    if np.count_nonzero(bright_mask) < 5:
        return float(cx), float(cy)
    m = cv2.moments(bright_mask.astype(np.uint8))
    if m["m00"] == 0:
        return float(cx), float(cy)
    return x1 + m["m10"] / m["m00"], y1 + m["m01"] / m["m00"]


def _smooth_trajectory(
    xs: list[float], ys: list[float], rs: list[float], window: int = 7
) -> tuple[list[float], list[float], list[float]]:
    """
    Apply a sliding median filter to a sequence of (x, y, r) positions.
    Gaps (NaN) are preserved; only non-NaN neighbours contribute to the median.
    """
    n = len(xs)
    half = window // 2
    sx, sy, sr = [], [], []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        nx = [v for v in xs[lo:hi] if not np.isnan(v)]
        ny = [v for v in ys[lo:hi] if not np.isnan(v)]
        nr = [v for v in rs[lo:hi] if not np.isnan(v)]
        sx.append(float(np.median(nx)) if nx else xs[i])
        sy.append(float(np.median(ny)) if ny else ys[i])
        sr.append(float(np.median(nr)) if nr else rs[i])
    return sx, sy, sr


def _normalize(bgr: np.ndarray, brightness: float) -> np.ndarray:
    """
    Normalise frame brightness so every phase looks well-exposed in the
    timelapse/panel.  Applies a gentle blood-moon tint for totality frames.
    """
    out = bgr.astype(np.float32)
    if brightness > 2:
        factor = float(np.clip(NORM_TARGET / brightness, 0.4, NORM_MAX_FACTOR))
        out = np.clip(out * factor, 0, 255)
    if brightness < BRIGHTNESS_TOTALITY:
        b, g, rc = cv2.split(out)
        rc = np.clip(rc * TOTALITY_RED_SCALE,  0, 255)
        b  = np.clip(b  * TOTALITY_BLUE_SCALE, 0, 255)
        out = cv2.merge([b, g, rc])
    return out.astype(np.uint8)


def _phase_label(brightness: float, detected: bool) -> str:
    if not detected:
        return "Cloud cover"
    if brightness >= BRIGHTNESS_PARTIAL:
        return "Full Moon"
    if brightness >= BRIGHTNESS_TOTALITY:
        return "Partial eclipse"
    return "Totality"


# ── Text / overlay helpers ────────────────────────────────────────────────────

def _overlay_text(img: np.ndarray,
                  line1: str, line2: str = "",
                  fg: tuple = (220, 210, 160)) -> None:
    """Burn two-line label into the bottom of img (in-place)."""
    h, w = img.shape[:2]
    bar_h = 50
    roi = img[h - bar_h:h, 0:w]
    dark = np.zeros_like(roi)
    cv2.addWeighted(roi, 0.38, dark, 0.62, 0, roi)
    img[h - bar_h:h, 0:w] = roi
    cv2.putText(img, line1, (8, h - bar_h + 20),
                cv2.FONT_HERSHEY_DUPLEX, 0.56, fg, 1, cv2.LINE_AA)
    if line2:
        cv2.putText(img, line2, (8, h - bar_h + 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (145, 145, 145), 1, cv2.LINE_AA)


def _make_label_strip(w: int, h: int, line1: str, line2: str) -> np.ndarray:
    strip = np.full((h, w, 3), (14, 14, 14), dtype=np.uint8)
    cv2.putText(strip, line1, (6, 22),
                cv2.FONT_HERSHEY_DUPLEX, 0.52, (220, 195, 110), 1, cv2.LINE_AA)
    if line2:
        cv2.putText(strip, line2, (6, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (120, 120, 120), 1, cv2.LINE_AA)
    return strip


def _cloud_tile(size: int) -> np.ndarray:
    tile = np.zeros((size, size, 3), dtype=np.uint8)
    text = "Cloud cover"
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.60, 1)
    cv2.putText(tile, text, ((size - tw) // 2, (size + th) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.60, (60, 60, 60), 1, cv2.LINE_AA)
    return tile


# ── Pass 1: Quality scan ───────────────────────────────────────────────────────

def scan_quality(files: list[str]) -> list[dict]:
    """
    Seek to one frame per SCAN_INTERVAL_S across all files.
    Returns a list of per-sample dicts including quality metrics.
    """
    manifest: list[dict] = []

    for path in files:
        file_start = _parse_file_start(path)
        cap   = cv2.VideoCapture(path)
        fps   = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fw    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        fh    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        step  = max(1, int(fps * SCAN_INTERVAL_S))

        # Sensible fallback: Seestar keeps moon near centre
        fb_cx, fb_cy, fb_r = fw // 2, fh // 2, min(fw, fh) // 3
        n_samples = (total + step - 1) // step

        print(f"  {Path(path).name}  "
              f"({total:,} frames @ {fps:.0f} fps  →  {n_samples} samples)")

        for i, frame_no in enumerate(range(0, total, step)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = cap.read()
            if not ret:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            info = _assess(gray, fb_cx, fb_cy, fb_r)
            if info["detected"]:
                fb_cx, fb_cy, fb_r = info["cx"], info["cy"], info["radius"]

            dt  = file_start + timedelta(seconds=frame_no / fps) if file_start else None
            iso = dt.isoformat() if dt else None

            manifest.append({
                "file":      path,
                "frame_no":  frame_no,
                "timestamp": iso,
                **info,
            })

            # Print progress every 20 samples
            if (i + 1) % 20 == 0 or i == n_samples - 1:
                pct = (i + 1) / n_samples * 100
                ts  = _fmt_time(dt)
                q   = info["quality"]
                b   = info["brightness"]
                lbl = _phase_label(b, info["detected"])
                print(f"    {pct:5.1f}%  [{ts}]  "
                      f"q={q:.2f}  b={b:5.1f}  {lbl}")

        cap.release()
        file_samples = [s for s in manifest if s["file"] == path]
        n_det = sum(1 for s in file_samples if s["detected"])
        print(f"    → moon detected in {n_det}/{len(file_samples)} samples\n")

    return manifest


# ── Pass 2: Timelapse ──────────────────────────────────────────────────────────

def build_timelapse(manifest: list[dict]) -> None:
    """
    Select best frame per TIMELAPSE_INTERVAL_S window and compose timelapse.
    """
    # Global median moon position (stabilises the crop across all frames)
    det = [s for s in manifest if s["detected"]]
    gcx = int(np.median([s["cx"]     for s in det])) if det else 640
    gcy = int(np.median([s["cy"]     for s in det])) if det else 360
    gr  = int(np.median([s["radius"] for s in det])) if det else 250
    print(f"  Global moon: centre ({gcx}, {gcy})  radius {gr} px")

    # Build time-keyed windows
    first_ts: Optional[datetime] = None
    for s in manifest:
        if s.get("timestamp"):
            first_ts = datetime.fromisoformat(s["timestamp"])
            break

    windows: dict[int, list[dict]] = {}
    for s in manifest:
        if s.get("timestamp") and first_ts:
            key = int((datetime.fromisoformat(s["timestamp"]) - first_ts
                       ).total_seconds() // TIMELAPSE_INTERVAL_S)
        else:
            key = s.get("frame_no", 0) // max(1, int(30 * TIMELAPSE_INTERVAL_S))
        windows.setdefault(key, []).append(s)

    sorted_keys = sorted(windows)
    n_windows   = len(sorted_keys)
    duration_s  = n_windows / TIMELAPSE_FPS
    print(f"  {n_windows} windows → {duration_s:.1f}s timelapse at {TIMELAPSE_FPS} fps")

    fourcc   = cv2.VideoWriter_fourcc(*"mp4v")
    raw_path = TIMELAPSE_OUT + ".raw.mp4"
    writer   = cv2.VideoWriter(raw_path, fourcc, TIMELAPSE_FPS,
                               (TIMELAPSE_SIZE, TIMELAPSE_SIZE))

    open_caps: dict[str, cv2.VideoCapture] = {}
    cloud_frames = clear_frames = 0

    # ── Pre-pass: collect and refine per-frame centres, then smooth ────────────
    # For cloud-gap frames store NaN so the smoother can skip them.
    raw_xs: list[float] = []
    raw_ys: list[float] = []
    raw_rs: list[float] = []
    best_frames: list[dict] = []

    for key in sorted_keys:
        best = max(windows[key], key=lambda s: s.get("quality", 0.0))
        best_frames.append(best)
        q = best.get("quality", 0.0)
        if q < MIN_QUALITY_TIMELAPSE or not best.get("detected"):
            raw_xs.append(float("nan"))
            raw_ys.append(float("nan"))
            raw_rs.append(float("nan"))
        else:
            fpath    = best["file"]
            frame_no = best["frame_no"]
            if fpath not in open_caps:
                open_caps[fpath] = cv2.VideoCapture(fpath)
            cap = open_caps[fpath]
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = cap.read()
            if ret:
                rx, ry = _refine_center(frame, int(best["cx"]), int(best["cy"]),
                                        int(best["radius"]))
                raw_xs.append(rx)
                raw_ys.append(ry)
                raw_rs.append(float(best["radius"]))
            else:
                raw_xs.append(float("nan"))
                raw_ys.append(float("nan"))
                raw_rs.append(float("nan"))

    # Smooth the trajectory; fill any leading/trailing NaNs with global centre
    smx, smy, smr = _smooth_trajectory(raw_xs, raw_ys, raw_rs, window=9)
    for i in range(len(smx)):
        if np.isnan(smx[i]):
            smx[i], smy[i], smr[i] = float(gcx), float(gcy), float(gr)

    # ── Write pass ─────────────────────────────────────────────────────────────
    try:
        for wi, (key, best) in enumerate(zip(sorted_keys, best_frames)):
            q    = best.get("quality", 0.0)
            b    = best.get("brightness", 128.0)
            dt   = datetime.fromisoformat(best["timestamp"]) if best.get("timestamp") else None

            if q < MIN_QUALITY_TIMELAPSE:
                tile = np.zeros((TIMELAPSE_SIZE, TIMELAPSE_SIZE, 3), dtype=np.uint8)
                _overlay_text(tile, "Cloud cover", _fmt_time_long(dt), fg=(70, 70, 70))
                cloud_frames += 1
            else:
                fpath    = best["file"]
                frame_no = best["frame_no"]
                if fpath not in open_caps:
                    open_caps[fpath] = cv2.VideoCapture(fpath)
                cap = open_caps[fpath]
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ret, frame = cap.read()

                if not ret:
                    tile = np.zeros((TIMELAPSE_SIZE, TIMELAPSE_SIZE, 3), dtype=np.uint8)
                else:
                    fx = int(smx[wi])
                    fy = int(smy[wi])
                    fr = int(smr[wi])

                    norm = _normalize(frame, b)
                    tile = _crop_moon(norm, fx, fy, fr, TIMELAPSE_SIZE)
                    phase = _phase_label(b, best["detected"])
                    _overlay_text(tile, phase, _fmt_time_long(dt))
                    clear_frames += 1

            writer.write(tile)

            if (wi + 1) % 60 == 0:
                print(f"    {wi+1}/{n_windows} frames written …")

    finally:
        writer.release()
        for c in open_caps.values():
            c.release()

    print(f"  Clear: {clear_frames}  Cloud gaps: {cloud_frames}")
    _transcode(raw_path, TIMELAPSE_OUT)
    print(f"  Timelapse → {TIMELAPSE_OUT}")


# ── Pass 3: Panel ──────────────────────────────────────────────────────────────

def build_panel(manifest: list[dict]) -> None:
    """
    Divide manifest into PANEL_COLS×PANEL_ROWS segments, pick the best frame
    from each, and compose a labelled grid montage.
    """
    n_tiles = PANEL_COLS * PANEL_ROWS

    # Global median position
    det = [s for s in manifest if s["detected"]]
    gcx = int(np.median([s["cx"]     for s in det])) if det else 640
    gcy = int(np.median([s["cy"]     for s in det])) if det else 360
    gr  = int(np.median([s["radius"] for s in det])) if det else 250

    segment_size = max(1, len(manifest) // n_tiles)
    selected: list[Optional[dict]] = []
    for i in range(n_tiles):
        seg = manifest[i * segment_size: (i + 1) * segment_size]
        good = [s for s in seg if s.get("quality", 0) >= MIN_QUALITY_PANEL]
        selected.append(max(good, key=lambda s: s["quality"]) if good else None)

    # Canvas dimensions
    gap     = 10
    tile_h  = PANEL_TILE_PX + PANEL_LABEL_H
    pw      = PANEL_COLS * PANEL_TILE_PX + (PANEL_COLS + 1) * gap
    ph      = PANEL_ROWS * tile_h          + (PANEL_ROWS + 1) * gap
    panel   = np.full((ph, pw, 3), (16, 16, 16), dtype=np.uint8)

    os.makedirs(KEYFRAMES_DIR, exist_ok=True)
    open_caps: dict[str, cv2.VideoCapture] = {}

    try:
        for idx, s in enumerate(selected):
            row = idx // PANEL_COLS
            col = idx  % PANEL_COLS
            x0  = gap + col * (PANEL_TILE_PX + gap)
            y0  = gap + row * (tile_h + gap)

            if s is None:
                tile_img = _cloud_tile(PANEL_TILE_PX)
                lbl_img  = _make_label_strip(PANEL_TILE_PX, PANEL_LABEL_H,
                                              "Cloud cover", "")
            else:
                fpath    = s["file"]
                frame_no = s["frame_no"]
                if fpath not in open_caps:
                    open_caps[fpath] = cv2.VideoCapture(fpath)
                cap = open_caps[fpath]
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ret, frame = cap.read()

                dt     = datetime.fromisoformat(s["timestamp"]) if s.get("timestamp") else None
                b      = s.get("brightness", 128.0)
                phase  = _phase_label(b, s.get("detected", False))
                dt_str = _fmt_time_long(dt)

                if not ret:
                    tile_img = _cloud_tile(PANEL_TILE_PX)
                    lbl_img  = _make_label_strip(PANEL_TILE_PX, PANEL_LABEL_H,
                                                  "Read error", dt_str)
                else:
                    fx = int(0.30*gcx + 0.70*s["cx"])     if s["detected"] else gcx
                    fy = int(0.30*gcy + 0.70*s["cy"])     if s["detected"] else gcy
                    fr = int(0.30*gr  + 0.70*s["radius"]) if s["detected"] else gr

                    norm     = _normalize(frame, b)
                    tile_img = _crop_moon(norm, fx, fy, fr, PANEL_TILE_PX)
                    lbl_img  = _make_label_strip(PANEL_TILE_PX, PANEL_LABEL_H,
                                                  phase, dt_str)

                    kf_path = os.path.join(KEYFRAMES_DIR, f"keyframe_{idx+1:02d}.jpg")
                    cv2.imwrite(kf_path, tile_img, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    print(f"    Tile {idx+1:2d}: [{dt_str}]  {phase}"
                          f"  q={s['quality']:.2f}  b={b:.0f}")

            panel[y0:y0 + PANEL_TILE_PX,  x0:x0 + PANEL_TILE_PX] = tile_img
            panel[y0 + PANEL_TILE_PX:y0 + tile_h, x0:x0 + PANEL_TILE_PX] = lbl_img

    finally:
        for c in open_caps.values():
            c.release()

    cv2.imwrite(PANEL_OUT, panel, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"  Panel → {PANEL_OUT}")
    print(f"  Key frames → {KEYFRAMES_DIR}/")


# ── Transcode helper ──────────────────────────────────────────────────────────

def _transcode(raw_path: str, out_path: str) -> None:
    """Transcode OpenCV mp4v → H.264 (browser-compatible) via ffmpeg.

    Tries codecs in order: libx264 (best), libopenh264 (conda-friendly),
    libvpx-vp9 (fallback).  If all fail, keeps the raw mp4v file.
    Also searches /usr/bin for a system ffmpeg with libx264 if the PATH
    ffmpeg lacks it.
    """
    # Prefer a system ffmpeg that has libx264 if the in-PATH one doesn't
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.isfile(candidate):
                ffmpeg_bin = candidate
                break
    if not ffmpeg_bin:
        print("  (ffmpeg not found — keeping raw mp4v output)")
        os.rename(raw_path, out_path)
        return

    # If PATH ffmpeg is the conda one (no libx264), prefer system ffmpeg
    if ffmpeg_bin == shutil.which("ffmpeg"):
        probe = subprocess.run(
            [ffmpeg_bin, "-codecs"], capture_output=True, text=True)
        if "libx264" not in probe.stdout and os.path.isfile("/usr/bin/ffmpeg"):
            ffmpeg_bin = "/usr/bin/ffmpeg"

    tmp = out_path + ".tmp.mp4"
    codec_attempts = [
        ["-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p"],
        ["-c:v", "libopenh264", "-b:v", "2M"],
        ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"],
    ]
    for codec_args in codec_attempts:
        r = subprocess.run(
            [ffmpeg_bin, "-y", "-loglevel", "error", "-i", raw_path]
            + codec_args + [tmp],
            capture_output=True,
        )
        if r.returncode == 0:
            os.replace(tmp, out_path)
            try:
                os.remove(raw_path)
            except OSError:
                pass
            return
        try:
            os.remove(tmp)
        except OSError:
            pass

    print("  ffmpeg: all codec attempts failed — keeping raw mp4v")
    os.rename(raw_path, out_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Lunar eclipse processor")
    parser.add_argument("--scan-only", action="store_true",
                        help="Stop after Pass 1 (quality scan)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached scan JSON and re-scan")
    args = parser.parse_args()

    # Gather source MP4 files (exclude our own outputs)
    files = sorted(
        str(p) for p in Path(ECLIPSE_DIR).glob("*.mp4")
        if not p.name.startswith("eclipse_")
    )
    if not files:
        print(f"No MP4 files found in {ECLIPSE_DIR}")
        sys.exit(1)

    print(f"\n{'─'*60}")
    print(f"Lunar Eclipse Processor")
    print(f"Directory : {ECLIPSE_DIR}")
    print(f"Files     : {len(files)}")
    for f in files:
        fs = _parse_file_start(f)
        sz = os.path.getsize(f) / 1e9
        print(f"  {Path(f).name}  ({sz:.1f} GB  starts {_fmt_time_long(fs)})")
    print(f"{'─'*60}\n")

    # ── Pass 1: Quality scan ────────────────────────────────────────────────
    if not args.no_cache and os.path.exists(SCAN_JSON):
        print("[Pass 1] Loading cached quality scan…")
        with open(SCAN_JSON) as fp:
            manifest = json.load(fp)
        print(f"         {len(manifest)} samples loaded from {SCAN_JSON}")
    else:
        print(f"[Pass 1] Quality scan  ({SCAN_INTERVAL_S}s interval)…")
        t0 = datetime.now()
        manifest = scan_quality(files)
        with open(SCAN_JSON, "w") as fp:
            json.dump(manifest, fp, indent=2)
        elapsed = (datetime.now() - t0).total_seconds()
        print(f"  Scan complete in {elapsed:.0f}s  →  {SCAN_JSON}")

    # Summary stats
    n_det  = sum(1 for s in manifest if s["detected"])
    n_tot  = len(manifest)
    q_det  = [s["quality"]    for s in manifest if s["detected"]]
    b_det  = [s["brightness"] for s in manifest if s["detected"]]
    print(f"\n  Samples       : {n_tot}")
    print(f"  Moon detected : {n_det}  ({100*n_det/max(n_tot,1):.0f}%)")
    if q_det:
        print(f"  Quality       : mean {np.mean(q_det):.2f}  max {max(q_det):.2f}")
    if b_det:
        print(f"  Brightness    : min {min(b_det):.0f}  mean {np.mean(b_det):.0f}"
              f"  max {max(b_det):.0f}")
        n_totality = sum(1 for b in b_det if b < BRIGHTNESS_TOTALITY)
        n_partial  = sum(1 for b in b_det if BRIGHTNESS_TOTALITY <= b < BRIGHTNESS_PARTIAL)
        print(f"  Phase dist.   : {n_totality} totality  {n_partial} partial  "
              f"{n_det - n_totality - n_partial} full/penumbral")

    if args.scan_only:
        print("\nScan-only mode — stopping here.")
        print(f"Inspect {SCAN_JSON} and re-run without --scan-only to generate outputs.")
        return

    # ── Pass 2: Timelapse ────────────────────────────────────────────────────
    print(f"\n[Pass 2] Building timelapse  ({TIMELAPSE_INTERVAL_S}s/frame → "
          f"{TIMELAPSE_FPS}fps output)…")
    t0 = datetime.now()
    build_timelapse(manifest)
    print(f"  Done in {(datetime.now()-t0).total_seconds():.0f}s")

    # ── Pass 3: Panel ────────────────────────────────────────────────────────
    print(f"\n[Pass 3] Building {PANEL_COLS}×{PANEL_ROWS} phase panel…")
    t0 = datetime.now()
    build_panel(manifest)
    print(f"  Done in {(datetime.now()-t0).total_seconds():.0f}s")

    print(f"\n{'─'*60}")
    print("All done.")
    print(f"  Timelapse : {TIMELAPSE_OUT}")
    print(f"  Panel     : {PANEL_OUT}")
    print(f"  Key frames: {KEYFRAMES_DIR}/")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
