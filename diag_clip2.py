#!/usr/bin/env python3
"""Diagnose clip 2 specifically - show top-2 blob sizes per frame."""

import os, sys
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from transit_detector import TransitDetector, N_BG_SAMPLES

clip_path = "/mnt/d/astro/transits/videos/2025-06-29-124512-Solar_airplane_event_001_1x.mp4"
DIFF_THRESH = 12
MIN_BLOB_PX = 8

cap = cv2.VideoCapture(clip_path)
fps = cap.get(cv2.CAP_PROP_FPS)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
print(f"{total} frames  {fps:.1f}fps  {w}x{h}")

n_bg = min(N_BG_SAMPLES, total)
sample_idxs = [int(i * total / n_bg) for i in range(n_bg)]
print(f"BG samples: {sample_idxs}")

frames = []
for idx in sample_idxs:
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, f = cap.read()
    if ok:
        frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
bg = np.median(np.array(frames), axis=0).astype(np.uint8)
bg_blur = cv2.GaussianBlur(bg, (5, 5), 0)

det = TransitDetector(clip_path, video_type="solar")
cx, cy = det._disk_center
r = det._disk_radius
disk_area = np.pi * r * r
max_blob = disk_area * det._max_blob_frac
print(f"Disk center={cx,cy} r={r} area={disk_area:.0f}  max_blob={max_blob:.0f}")

cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
print(f"\n{'Frame':>6}  {'hot_frac':>9}  {'n_all':>6}  {'largest':>9}  {'2nd':>9}  {'ratio':>7}  {'action'}")
for fi in range(total):
    ok, frame = cap.read()
    if not ok: break
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(gray, bg_blur)
    _, th = cv2.threshold(diff, DIFF_THRESH, 255, cv2.THRESH_BINARY)
    import cv2 as _cv2
    kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (3, 3))
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
    dm = np.zeros_like(th)
    cv2.circle(dm, (cx, cy), r, 255, -1)
    th = cv2.bitwise_and(th, dm)

    hot_px = cv2.countNonZero(th)
    hot_frac = hot_px / disk_area

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(th, connectivity=8)
    all_blobs = sorted(
        [float(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= MIN_BLOB_PX],
        reverse=True
    )
    if not all_blobs:
        continue

    largest = all_blobs[0]
    second  = all_blobs[1] if len(all_blobs) > 1 else 0
    ratio   = largest / second if second > 0 else 0
    n_all   = len(all_blobs)
    n_sized = sum(1 for a in all_blobs if a <= max_blob)

    if ratio >= 20:
        action = "DOMINANT→keep"
    elif n_sized > det._max_blobs_per_frame:
        action = f"shake({n_sized}blobs)"
    else:
        action = "track"

    if hot_frac > 0.001 or action != "track":
        print(f"{fi:6d}  {hot_frac:9.4f}  {n_all:6d}  {largest:9.0f}  {second:9.0f}  {ratio:7.1f}  {action}")

cap.release()
