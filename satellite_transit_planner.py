"""
Satellite Solar/Lunar Transit Planner for Seestar Lab.

Predicts when tracked satellites will transit the solar or lunar disk as seen
from the observer's location.  Transit corridors are typically 2–10 km wide on
the ground; durations are 0.5–2 s for ISS and somewhat longer for
higher-altitude objects.

Algorithm:
  1. Fetch TLEs from Celestrak (stations + visual groups), cache to JSON.
  2. Pre-compute Sun/Moon positions on a 1-minute grid for the full window.
  3. Per satellite: compute alt/az on the same grid; find pass windows.
  4. Candidate pass filter: satellite track comes within 5° of body.
  5. Medium scan at 1-second steps inside candidate windows.
  6. Ultra-fine scan at 0.05-second steps to locate precise contact times.

Requires skyfield (pip install skyfield).  de421.bsp (~17 MB) is downloaded
automatically on first use and cached in the project directory.
"""

import json
import logging
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE           = Path(__file__).parent
_TLE_CACHE_PATH = _HERE / "tle_cache.json"
_TLE_MAX_AGE_S  = 12 * 3600  # refresh after 12 h

# ── Celestrak ─────────────────────────────────────────────────────────────────
_CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
_TLE_GROUPS    = ["stations", "visual"]

# ── Notable satellite registry (NORAD ID → display name) ──────────────────────
NOTABLE: dict[int, str] = {
    25544: "ISS",
    20580: "Hubble Space Telescope",
    48274: "Tiangong",
}

# ── Disk radii (arcminutes) — slightly generous to catch grazes ───────────────
_SOLAR_RADIUS_AM   = 16.4
_LUNAR_RADIUS_AM   = 16.0
_CANDIDATE_SEP_DEG = 5.0   # coarse-grid proximity filter margin


# ── TLE cache ─────────────────────────────────────────────────────────────────

def _fetch_tles(force: bool = False) -> dict[int, tuple[str, str, str]]:
    """Return {norad: (name, line1, line2)}.  Reads cache if fresh."""
    if not force and _TLE_CACHE_PATH.exists():
        try:
            data = json.loads(_TLE_CACHE_PATH.read_text())
            if time.time() - data.get("fetched_at", 0) < _TLE_MAX_AGE_S:
                return {int(k): tuple(v) for k, v in data["tles"].items()}
        except Exception:
            pass

    tles: dict[int, tuple[str, str, str]] = {}
    for group in _TLE_GROUPS:
        url = _CELESTRAK_URL.format(group=group)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                text = r.read().decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for i in range(0, len(lines) - 2, 3):
                name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
                if l1.startswith("1 ") and l2.startswith("2 "):
                    norad = int(l1[2:7])
                    tles[norad] = (name.strip(), l1, l2)
        except Exception as exc:
            log.warning("TLE fetch failed for group %s: %s", group, exc)

    if tles:
        try:
            _TLE_CACHE_PATH.write_text(json.dumps({
                "fetched_at": time.time(),
                "tles": {str(k): list(v) for k, v in tles.items()},
            }, indent=2))
        except Exception as exc:
            log.warning("TLE cache write failed: %s", exc)
    elif _TLE_CACHE_PATH.exists():
        # Network unavailable — use stale cache rather than failing
        try:
            data = json.loads(_TLE_CACHE_PATH.read_text())
            tles = {int(k): tuple(v) for k, v in data["tles"].items()}
            log.warning("Using stale TLE cache (network unavailable)")
        except Exception:
            pass

    return tles


def tle_cache_info() -> dict:
    """Return metadata about the current TLE cache (for UI display)."""
    if not _TLE_CACHE_PATH.exists():
        return {"exists": False}
    try:
        data = json.loads(_TLE_CACHE_PATH.read_text())
        age_s = time.time() - data.get("fetched_at", 0)
        return {
            "exists":    True,
            "age_h":     round(age_s / 3600, 1),
            "stale":     age_s > _TLE_MAX_AGE_S,
            "n_sats":    len(data.get("tles", {})),
            "fetched_at": datetime.fromtimestamp(
                data["fetched_at"], tz=timezone.utc
            ).isoformat() if "fetched_at" in data else None,
        }
    except Exception:
        return {"exists": True, "corrupt": True}


# ── Geometry ──────────────────────────────────────────────────────────────────

def _sep_deg(
    alt1: np.ndarray, az1: np.ndarray,
    alt2: np.ndarray, az2: np.ndarray,
) -> np.ndarray:
    """Angular separation (degrees) between two alt/az directions (degrees)."""
    a1 = np.radians(alt1); a2 = np.radians(alt2)
    da = np.radians(az1 - az2)
    return np.degrees(np.arccos(np.clip(
        np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(da),
        -1.0, 1.0,
    )))


# ── Main entry point ──────────────────────────────────────────────────────────

def find_transits(
    lat:          float,
    lon:          float,
    elevation:    float = 50.0,
    days_ahead:   int   = 7,
    target:       str   = "both",       # "sun" | "moon" | "both"
    notable_only: bool  = False,
    progress_cb:  Optional[Callable[[str, int], None]] = None,
) -> dict:
    """
    Predict solar/lunar satellite transits for the next `days_ahead` days.

    Returns a result dict with a sorted ``events`` list.  Each event dict
    contains timing, satellite identity, transit geometry, and a
    ``solar_filter`` flag when the target is the Sun.
    """
    from skyfield.api import Loader, wgs84, EarthSatellite  # type: ignore

    def _prog(msg: str, pct: int) -> None:
        if progress_cb:
            try:
                progress_cb(msg, pct)
            except Exception:
                pass

    # ── Skyfield setup ────────────────────────────────────────────────────────
    _prog("Loading ephemeris…", 2)
    loader = Loader(str(_HERE))     # de421.bsp stored alongside app
    ts     = loader.timescale()
    eph    = loader("de421.bsp")   # auto-downloads ~17 MB on first run
    earth  = eph["earth"]
    sun    = eph["sun"]
    moon   = eph["moon"]

    observer = wgs84.latlon(lat, lon, elevation_m=elevation)

    now = datetime.now(timezone.utc)
    t0  = ts.from_datetime(now)
    t1  = ts.from_datetime(now + timedelta(days=days_ahead))

    # ── TLEs ─────────────────────────────────────────────────────────────────
    _prog("Fetching TLE catalogue…", 5)
    all_tles = _fetch_tles()

    satellites: dict[int, tuple[str, "EarthSatellite"]] = {}
    for norad, (raw_name, l1, l2) in all_tles.items():
        if notable_only and norad not in NOTABLE:
            continue
        try:
            satellites[norad] = (raw_name, EarthSatellite(l1, l2, raw_name, ts))
        except Exception:
            pass

    n_sats = len(satellites)
    _prog(f"Scanning {n_sats} satellites over {days_ahead} days…", 10)

    # ── Coarse body grid (1-minute steps) ─────────────────────────────────────
    GRID_MIN = 1
    n_grid   = int(days_ahead * 24 * 60 / GRID_MIN) + 1
    t_grid   = ts.tt_jd(np.linspace(t0.tt, t1.tt, n_grid))

    bodies_to_check: list[tuple] = []
    if target in ("sun", "both"):
        sun_app = (earth + observer).at(t_grid).observe(sun).apparent()
        s_alt_g, s_az_g, _ = sun_app.altaz()
        bodies_to_check.append(("sun",  s_alt_g.degrees, s_az_g.degrees, _SOLAR_RADIUS_AM))
    if target in ("moon", "both"):
        moon_app = (earth + observer).at(t_grid).observe(moon).apparent()
        m_alt_g, m_az_g, _ = moon_app.altaz()
        bodies_to_check.append(("moon", m_alt_g.degrees, m_az_g.degrees, _LUNAR_RADIUS_AM))

    events: list[dict] = []

    # ── Per-satellite scan ────────────────────────────────────────────────────
    for sat_i, (norad, (raw_name, sat)) in enumerate(satellites.items()):
        if sat_i % 10 == 0:
            label = NOTABLE.get(norad, raw_name[:24])
            _prog(f"Scanning {label}…", 10 + int(85 * sat_i / n_sats))

        # Satellite alt/az on coarse grid
        try:
            sat_topo_g          = (sat - observer).at(t_grid)
            sat_alt_g, sat_az_g, _ = sat_topo_g.altaz()
            sat_alt_arr         = sat_alt_g.degrees   # (n_grid,)
            sat_az_arr          = sat_az_g.degrees
        except Exception:
            continue

        above_hor = sat_alt_arr > 0.0

        for body_name, body_alt_arr, body_az_arr, radius_am in bodies_to_check:
            body_vis = body_alt_arr > 0.0
            combined = above_hor & body_vis
            if not combined.any():
                continue

            # Find contiguous windows where both satellite and body are visible
            padded  = np.concatenate(([0], combined.astype(np.int8), [0]))
            changes = np.diff(padded)
            starts  = np.where(changes ==  1)[0]
            ends    = np.where(changes == -1)[0]

            for si, ei in zip(starts, ends):
                # Coarse proximity filter — skip windows where track > 5° from body
                rough_sep = _sep_deg(
                    sat_alt_arr[si:ei], sat_az_arr[si:ei],
                    body_alt_arr[si:ei], body_az_arr[si:ei],
                )
                if rough_sep.min() > _CANDIDATE_SEP_DEG:
                    continue

                # ── Medium scan: 1-second steps across the candidate window ──
                t_s = t_grid[max(0, si - 1)].tt
                t_e = t_grid[min(n_grid - 1, ei)].tt
                n_m = max(2, int((t_e - t_s) * 86400) + 2)
                t_m = ts.tt_jd(np.linspace(t_s, t_e, n_m))

                try:
                    sat_m = (sat - observer).at(t_m)
                    sa_m, saz_m, _ = sat_m.altaz()

                    if body_name == "sun":
                        b_app_m = (earth + observer).at(t_m).observe(sun).apparent()
                    else:
                        b_app_m = (earth + observer).at(t_m).observe(moon).apparent()
                    ba_m, baz_m, _ = b_app_m.altaz()
                except Exception:
                    continue

                sep_m = _sep_deg(sa_m.degrees, saz_m.degrees, ba_m.degrees, baz_m.degrees)
                threshold = radius_am / 60.0
                inside_m  = (sep_m < threshold) & (ba_m.degrees > 0)
                if not inside_m.any():
                    continue

                # ── Ultra-fine: 0.05 s steps around confirmed transit ─────
                idx_in = np.where(inside_m)[0]
                uf_t0  = t_m[max(0, idx_in[0] - 3)].tt
                uf_t1  = t_m[min(n_m - 1, idx_in[-1] + 3)].tt
                n_uf   = max(10, int((uf_t1 - uf_t0) * 86400 / 0.05))
                t_uf   = ts.tt_jd(np.linspace(uf_t0, uf_t1, n_uf))

                try:
                    sat_uf = (sat - observer).at(t_uf)
                    sa_uf, saz_uf, sdist_uf = sat_uf.altaz()

                    if body_name == "sun":
                        b_app_uf = (earth + observer).at(t_uf).observe(sun).apparent()
                    else:
                        b_app_uf = (earth + observer).at(t_uf).observe(moon).apparent()
                    ba_uf, baz_uf, _ = b_app_uf.altaz()
                except Exception:
                    continue

                sep_uf   = _sep_deg(sa_uf.degrees, saz_uf.degrees, ba_uf.degrees, baz_uf.degrees)
                inside_uf = (sep_uf < threshold) & (ba_uf.degrees > 0)
                if not inside_uf.any():
                    continue

                uf_idx    = np.where(inside_uf)[0]
                min_i     = int(np.argmin(sep_uf))
                mid_i     = uf_idx[len(uf_idx) // 2]

                c1_dt  = t_uf[uf_idx[0]].utc_datetime()
                c2_dt  = t_uf[uf_idx[-1]].utc_datetime()
                mid_dt = t_uf[mid_i].utc_datetime()

                duration_s   = (t_uf[uf_idx[-1]].tt - t_uf[uf_idx[0]].tt) * 86400.0
                min_sep_am   = float(sep_uf[min_i]) * 60.0
                r, d         = radius_am, min_sep_am
                chord_pct    = int(100 * 2 * math.sqrt(max(0.0, r**2 - d**2)) / (2 * r)) if d < r else 0
                slant_km     = float(sdist_uf.km[mid_i])
                center_km    = round(min_sep_am / 60.0 * math.pi / 180.0 * slant_km, 2)

                events.append({
                    "norad":          norad,
                    "satellite":      NOTABLE.get(norad, raw_name.strip()),
                    "is_notable":     norad in NOTABLE,
                    "body":           body_name,
                    "c1_utc":         c1_dt.isoformat(),
                    "mid_utc":        mid_dt.isoformat(),
                    "c2_utc":         c2_dt.isoformat(),
                    "duration_s":     round(duration_s, 2),
                    "min_sep_arcmin": round(min_sep_am, 2),
                    "chord_pct":      chord_pct,
                    "centerline_km":  center_km,
                    "body_alt_deg":   round(float(ba_uf.degrees[mid_i]),  1),
                    "body_az_deg":    round(float(baz_uf.degrees[mid_i]), 1),
                    "sat_alt_deg":    round(float(sa_uf.degrees[mid_i]),  1),
                    "sat_az_deg":     round(float(saz_uf.degrees[mid_i]), 1),
                    "solar_filter":   body_name == "sun",
                })

    events.sort(key=lambda e: e["mid_utc"])
    _prog("Done", 100)

    return {
        "lat":                lat,
        "lon":                lon,
        "elevation":          elevation,
        "days_ahead":         days_ahead,
        "target":             target,
        "notable_only":       notable_only,
        "satellites_scanned": n_sats,
        "events":             events,
        "generated_at":       datetime.now(timezone.utc).isoformat(),
    }
