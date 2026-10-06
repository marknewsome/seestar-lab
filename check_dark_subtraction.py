#!/usr/bin/env python3
"""
Did the Seestar's dark-frame subtraction actually do anything to these subs?

ZWO added a user-facing dark-frame option (suggested above ~25C). No FITS
header records whether it was on — `BIAS` is just the pedestal value (467 on
fw 9.31) and is present either way — so the only way to tell is from the pixels.

What dark subtraction removes is *fixed-pattern* signal: the same hot pixels in
the same places every frame, scaling with exposure and temperature. That is
detectable without owning any dark frames, because across a dithering /
field-rotating session the sky and stars move between frames while the sensor
pattern does not. So the per-pixel median across many frames isolates whatever
is fixed.

Reports, per session:
  hot px        pixels far above the median level in the stacked median
  fixed/noise   spatial structure of that median vs frame-to-frame noise
  half-corr     correlation of the hot-pixel pattern between two disjoint
                halves of the sample — near 1.0 means a real fixed pattern,
                near 0 means it is just noise

Interpretation:
  many hot px + half-corr near 1.0   -> fixed pattern PRESENT (darks not applied,
                                        or not effective) — worth subtracting
  ~0 hot px + low fixed/noise        -> already clean (darks applied on-device,
                                        or the sensor is clean at this temp)

Usage:
    python3 check_dark_subtraction.py "/mnt/mac_share/xfer/<target>_sub" [...]

Compare a warm darks-on session against a warm darks-off one — temperature
matters more than almost anything else here, so match it when you can.
"""
import glob
import os
import sys
import numpy as np
from astropy.io import fits

N_FRAMES = 60


def analyse(target, since=None):
    files = sorted(glob.glob(os.path.join(target, "*.fit")))
    name = os.path.basename(target.rstrip("/")).replace("_sub", "")
    if since:
        # Target folders accumulate subs across many nights and firmware eras,
        # so an unfiltered sample silently mixes them — which makes a
        # darks-on/darks-off comparison meaningless. Filter by the capture date
        # in the filename.
        import re
        keep = []
        for f in files:
            m = re.search(r"(20\d{6})-", os.path.basename(f))
            if m and m.group(1) >= since:
                keep.append(f)
        files = keep
        name = f"{name} [>={since}]"
    if len(files) < 10:
        print(f"{name:<30} (only {len(files)} subs — need >= 10)")
        return

    idx = np.linspace(0, len(files) - 1, min(N_FRAMES, len(files))).astype(int)
    frames, temps, fw, bias = [], [], None, None
    for f in (files[i] for i in idx):
        try:
            with fits.open(f, memmap=False) as h:
                hdr, d = h[0].header, h[0].data
        except Exception:
            continue
        if d is None:
            continue
        frames.append(d.astype(np.float32))
        temps.append(float(hdr.get("CCD-TEMP", np.nan)))
        fw = fw or str(hdr.get("PROGRAM", "?"))
        bias = hdr.get("BIAS", "-") if bias is None else bias

    if len(frames) < 10:
        print(f"{name:<30} (could not read enough frames)")
        return

    arr = np.stack(frames)
    med = np.median(arr, axis=0)
    noise = float(np.median(np.std(arr, axis=0)))

    base = float(np.median(med))
    mad = float(np.median(np.abs(med - base))) * 1.4826
    # Count only ISOLATED bright pixels. A plain threshold counts nebulosity
    # too — on NGC 281 it reported 687,869 "hot pixels", which is the nebula.
    # These are RAW BAYER frames, so a plain 3x3 median compares pixels of
    # different colours and the colour mosaic itself reads as isolated
    # structure; compare each pixel against its own-colour neighbours instead,
    # by de-interleaving the CFA into its four sub-planes first.
    isolated = np.zeros_like(med, dtype=np.float32)
    for dy in (0, 1):
        for dx in (0, 1):
            plane = med[dy::2, dx::2].astype(np.float32)
            import cv2 as _cv2
            isolated[dy::2, dx::2] = plane - _cv2.medianBlur(plane, 3)
    hot = int((isolated > 10 * mad).sum()) if mad > 0 else 0

    half = arr.shape[0] // 2
    m1 = np.median(arr[:half], axis=0)
    m2 = np.median(arr[half:], axis=0)
    hi = isolated > 5 * mad if mad > 0 else np.zeros_like(med, bool)
    corr = (float(np.corrcoef(m1[hi].ravel(), m2[hi].ravel())[0, 1])
            if hi.sum() > 100 else float("nan"))

    fixed_ratio = med.std() / max(noise, 1e-9)
    # half-corr is the load-bearing number, not the hot count. On a bright
    # extended target the "hot" count tracks nebulosity no matter how the
    # isolation filter is tuned (NGC 281 scores six figures either way), but a
    # genuine sensor pattern is the SAME pixels in both halves of the sample,
    # so it correlates near 1.0 while sky structure does not. Threshold set
    # high deliberately: measured across this archive, darks-on and darks-off
    # sessions alike top out around 0.5-0.76, so anything below ~0.9 is not a
    # fixed pattern.
    verdict = ("FIXED PATTERN PRESENT" if (corr == corr and corr > 0.9)
               else "clean (no fixed pattern to subtract)")

    print(f"{name:<30} fw={fw:<5} BIAS={str(bias):<5} T={np.nanmean(temps):5.1f}C  "
          f"n={arr.shape[0]:<3} min={arr.min():5.0f}  "
          f"hot={hot:>7}  fixed/noise={fixed_ratio:5.2f}  "
          f"half-corr={corr:6.3f}  -> {verdict}")


if __name__ == "__main__":
    args = sys.argv[1:]
    since = None
    if args and args[0].startswith("--since="):
        since = args.pop(0).split("=", 1)[1]
    if not args:
        print(__doc__)
        sys.exit(1)
    print(f"{'session':<30} {'headers':<30} {'measurements'}")
    for t in args:
        analyse(t, since=since)
