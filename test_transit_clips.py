#!/usr/bin/env python3
"""
Quick test: run TransitDetector on the known-good Solar YOLO clips.
Usage:  python test_transit_clips.py
"""

import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))

from transit_detector import TransitDetector

CLIPS = [
    "/mnt/d/astro/transits/videos/2025-06-15-160043-Solar_airplane_event_001_1x.mp4",
    "/mnt/d/astro/transits/videos/2025-06-29-124512-Solar_airplane_event_001_1x.mp4",
    "/mnt/d/astro/transits/videos/event_001_1x.mp4",
]

OUT_DIR = "/tmp/transit_test_out"
os.makedirs(OUT_DIR, exist_ok=True)

for clip in CLIPS:
    if not os.path.exists(clip):
        print(f"  SKIP (not found): {clip}")
        continue

    name = os.path.basename(clip)
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    det = TransitDetector(clip, video_type="solar")
    print(f"  {det.total_frames} frames  {det.fps:.1f} fps  {det.width}x{det.height}")

    t0 = time.time()

    def progress(pct, total, msg):
        print(f"  [{pct:3d}%] {msg}", end="\r", flush=True)

    out_sub = os.path.join(OUT_DIR, name.replace(".mp4", ""))
    os.makedirs(out_sub, exist_ok=True)

    events = det.detect(out_sub, pad_secs=1.0, progress_cb=progress)
    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.1f}s — {len(events)} event(s) found")

    for i, ev in enumerate(events, 1):
        print(f"  Event {i}: label={ev.label}  conf={ev.confidence:.2f}  "
              f"frames={ev.frame_start}-{ev.frame_end}  "
              f"dur={ev.duration_s:.2f}s  "
              f"vel={ev.velocity_pct_per_sec:.1f}%Ø/s  "
              f"R²={ev.linearity:.3f}")

    if not events:
        print("  *** NO EVENTS DETECTED — possible false negative ***")

print("\nAll done. Output clips (if any) at:", OUT_DIR)
