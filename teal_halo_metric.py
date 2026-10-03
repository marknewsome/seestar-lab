#!/usr/bin/env python3
"""
Measure the green/teal star-halo defect in a rendered stack JPEG.

Why this exists: the defect is plainly visible but several intuitive metrics do
NOT track it. Halo-annulus mean G-R reads *negative* on IC 434 (green below red)
even though the halos are obviously teal, and median hue over the brightest
stars is equally uninformative (median saturation ~14/255, hues scattered across
the whole circle). Averaging fails because the teal is a coherent coloured
region around a few of the brightest stars, mixed in the average with neutral
sky and the star's own white core.

So instead of averaging, count: how much of the area immediately around bright
stars is rendered as actually-teal pixels (green-cyan hue, meaningfully
saturated, bright enough to see).

Validated against visual inspection:

    M 43      16.18%   obvious teal/green contamination throughout
    IC 434     5.67%   clear teal halos on bright stars
    M 27       0.00%   visually neutral
    M 81       0.00%   clean white stars
    M 13 / M 33 / SH2-142 / SH2-158 / IC 5146 / C 34   <= 0.05%

That is a 100x+ gap between affected and clean targets, not a gradient, so a
simple threshold (say >1%) separates them reliably.

Usage:
    python3 teal_halo_metric.py <rendered.jpg> [more.jpg ...]
    python3 teal_halo_metric.py --mask <rendered.jpg>   # write a check image

Note the star finder uses an absolute luminance percentile, not SEP's
sigma-relative threshold: renders differ enough in noise that a fixed sigma
cut finds zero stars on some of them (M 13 and SH2-158 have globalrms ~31, so
an 8-sigma cut demands 254 of 255).
"""
import sys
import cv2
import numpy as np

HUE_LO, HUE_HI = 140, 200   # green-cyan band, degrees
S_MIN, V_MIN = 30, 100      # must be saturated and bright enough to be visible
RADIUS = 25                 # halo zone around each star, px
N_STARS = 60


def find_bright_stars(im, n=N_STARS):
    f = im.astype(np.float32)
    lum = 0.299 * f[:, :, 2] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 0]
    thr = np.percentile(lum, 99.8)
    nlab, lab, stats, cent = cv2.connectedComponentsWithStats(
        (lum >= thr).astype(np.uint8), 8)
    cands = []
    for i in range(1, nlab):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < 4 or area > 5000:   # noise specks / large nebula regions
            continue
        cands.append((lum[lab == i].max(), cent[i][0], cent[i][1]))
    cands.sort(key=lambda t: -t[0])
    return [(c[1], c[2]) for c in cands[:n]]


def teal_masks(im):
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    H = hsv[:, :, 0].astype(np.int32) * 2
    S = hsv[:, :, 1].astype(np.int32)
    V = hsv[:, :, 2].astype(np.int32)
    base = (S >= S_MIN) & (V >= V_MIN)
    return base & (H >= HUE_LO) & (H <= HUE_HI), S


def star_zone(im, stars):
    zone = np.zeros(im.shape[:2], bool)
    yy, xx = np.mgrid[0:im.shape[0], 0:im.shape[1]]
    for x, y in stars:
        x, y = int(round(x)), int(round(y))
        y0, y1 = max(0, y - RADIUS), min(im.shape[0], y + RADIUS + 1)
        x0, x1 = max(0, x - RADIUS), min(im.shape[1], x + RADIUS + 1)
        zone[y0:y1, x0:x1] |= ((yy[y0:y1, x0:x1] - y) ** 2
                               + (xx[y0:y1, x0:x1] - x) ** 2) <= RADIUS ** 2
    return zone


def score(path, write_mask=False):
    im = cv2.imread(path)
    if im is None:
        print(f"{path}: cannot read")
        return None
    stars = find_bright_stars(im)
    if not stars:
        print(f"{path}: no stars found")
        return None
    teal, S = teal_masks(im)
    zone = star_zone(im, stars)
    hit = teal & zone
    pct = 100.0 * hit.sum() / max(zone.sum(), 1)
    sat = float(S[hit].mean()) if hit.any() else 0.0
    verdict = "TEAL DEFECT" if pct > 1.0 else "clean"
    print(f"{path}\n    stars={len(stars)}  teal={pct:.2f}%  mean_sat={sat:.1f}  -> {verdict}")

    if write_mask:
        vis = im.copy()
        vis[hit] = (0, 0, 255)
        out = path.rsplit('.', 1)[0] + "_tealmask.jpg"
        h, w = vis.shape[:2]
        cv2.imwrite(out, cv2.resize(vis, (w // 2, h // 2)))
        print(f"    wrote {out}")
    return pct


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--mask"]
    want_mask = "--mask" in sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    for p in args:
        score(p, write_mask=want_mask)
