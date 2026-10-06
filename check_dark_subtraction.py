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


def analyse(target):
    files = sorted(glob.glob(os.path.join(target, "*.fit")))
    name = os.path.basename(target.rstrip("/")).replace("_sub", "")
    if len(files) < 10:
        print(f"{name:<22} (only {len(files)} subs — need >= 10)")
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
        print(f"{name:<22} (could not read enough frames)")
        return

    arr = np.stack(frames)
    med = np.median(arr, axis=0)
    noise = float(np.median(np.std(arr, axis=0)))

    base = float(np.median(med))
    mad = float(np.median(np.abs(med - base))) * 1.4826
    hot = int((med > base + 10 * mad).sum()) if mad > 0 else 0

    half = arr.shape[0] // 2
    m1 = np.median(arr[:half], axis=0)
    m2 = np.median(arr[half:], axis=0)
    hi = med > base + 5 * mad if mad > 0 else np.zeros_like(med, bool)
    corr = (float(np.corrcoef(m1[hi].ravel(), m2[hi].ravel())[0, 1])
            if hi.sum() > 100 else float("nan"))

    fixed_ratio = med.std() / max(noise, 1e-9)
    verdict = ("FIXED PATTERN PRESENT" if (hot > 500 and corr > 0.5)
               else "clean (no fixed pattern to subtract)")

    print(f"{name:<22} fw={fw:<5} BIAS={str(bias):<5} T={np.nanmean(temps):5.1f}C  "
          f"n={arr.shape[0]:<3} min={arr.min():5.0f}  "
          f"hot={hot:>7}  fixed/noise={fixed_ratio:5.2f}  "
          f"half-corr={corr:6.3f}  -> {verdict}")


if __name__ == "__main__":
    targets = sys.argv[1:]
    if not targets:
        print(__doc__)
        sys.exit(1)
    print(f"{'session':<22} {'headers':<30} {'measurements'}")
    for t in targets:
        analyse(t)
