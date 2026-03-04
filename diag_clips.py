#!/usr/bin/env python3
"""
Diagnose why transit detector misses events in short YOLO clips.
"""

import os, sys
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from transit_detector import TransitDetector, N_BG_SAMPLES

CLIPS = [
    "/mnt/d/astro/transits/videos/2025-06-15-160043-Solar_airplane_event_001_1x.mp4",
    "/mnt/d/astro/transits/videos/2025-06-29-124512-Solar_airplane_event_001_1x.mp4",
    "/mnt/d/astro/transits/videos/event_001_1x.mp4",
]

DIFF_THRESH = 12

for clip_path in CLIPS:
    if not os.path.exists(clip_path):
        print(f"SKIP: {clip_path}"); continue

    name = os.path.basename(clip_path)
    print(f"\n{'='*60}\n  {name}\n{'='*60}")

    cap = cv2.VideoCapture(clip_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"  {total} frames  {fps:.1f}fps  {w}x{h}")

    # Collect background sample frames (same logic as detector)
    n_bg = min(N_BG_SAMPLES, total)
    sample_idxs = [int(i * total / n_bg) for i in range(n_bg)]
    print(f"  BG samples: {n_bg} frames at indices {sample_idxs}")

    frames = []
    for idx in sample_idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, f = cap.read()
        if ok:
            frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))

    bg = np.median(np.array(frames), axis=0).astype(np.uint8)
    bg_blur = cv2.GaussianBlur(bg, (5, 5), 0)

    # Check disk detection
    det = TransitDetector(clip_path, video_type="solar")
    print(f"  Disk: center={det._disk_center} r={det._disk_radius}")
    cx, cy = det._disk_center
    r = det._disk_radius
    disk_area = np.pi * r * r
    print(f"  Disk area: {disk_area:.0f} px²")

    # Scan every frame — measure diff vs background
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    max_hot_frac = 0.0
    bright_frames = []
    for fi in range(total):
        ok, frame = cap.read()
        if not ok: break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray, bg_blur)
        _, mask = cv2.threshold(diff, DIFF_THRESH, 255, cv2.THRESH_BINARY)

        # Mask to disk
        dm = np.zeros_like(mask)
        cv2.circle(dm, (cx, cy), r, 255, -1)
        masked = cv2.bitwise_and(mask, dm)

        hot_px = np.count_nonzero(masked)
        hot_frac = hot_px / disk_area
        if hot_frac > max_hot_frac:
            max_hot_frac = hot_frac

        if hot_frac > 0.001:
            # Count connected components
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(masked, connectivity=8)
            blobs = []
            for lbl in range(1, num_labels):
                area = stats[lbl, cv2.CC_STAT_AREA]
                if area >= 8:
                    bx = stats[lbl, cv2.CC_STAT_LEFT] + stats[lbl, cv2.CC_STAT_WIDTH] / 2
                    by = stats[lbl, cv2.CC_STAT_TOP] + stats[lbl, cv2.CC_STAT_HEIGHT] / 2
                    blobs.append((area, bx, by))
            if blobs:
                bright_frames.append((fi, hot_frac, hot_px, blobs))

    cap.release()

    print(f"  Max hot_frac across all frames: {max_hot_frac:.4f}  (shake threshold: {det._shake_hot_frac})")
    print(f"  Frames with blobs (hot_frac>0.1%): {len(bright_frames)}")
    if bright_frames:
        print(f"  {'Frame':>6}  {'hot_frac':>9}  {'hot_px':>7}  #blobs  largest_blob_px")
        for fi, hf, hpx, blobs in bright_frames:
            blobs_sorted = sorted(blobs, reverse=True)
            largest = blobs_sorted[0][0] if blobs_sorted else 0
            # flag shake
            shake = "SHAKE" if hf > det._shake_hot_frac else ""
            blob_cap = "BLOB_CAP" if len(blobs) > det._max_blobs_per_frame else ""
            size_cap = "SIZE_CAP" if largest > disk_area * det._max_blob_frac else ""
            flags = " ".join(f for f in [shake, blob_cap, size_cap] if f)
            print(f"  {fi:6d}  {hf:9.4f}  {hpx:7d}  {len(blobs):>6}  {largest:>14.0f}  {flags}")
