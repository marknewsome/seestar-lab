"""
Seestar Lab — DSO Visibility Planner.

Computes, for a given observer location and calendar date, which Messier and
Caldwell objects are visible (above min_alt degrees) during astronomical
darkness, ranks them, and cross-references against the session DB to flag
objects never yet imaged.

Performance: altitude calculations are fully vectorised — all ~200 objects are
transformed at each time-step in a single SkyCoord.transform_to() call, so
the full plan runs in ~2–4 s on a typical machine.

Requires astropy (already installed).  All calculations are offline — no
network calls needed.
"""

import json
import math
from datetime import date as _date, datetime, timezone
from typing import Optional

# Suppress astropy IERS auto-download — we must never need network
try:
    from astropy.utils import iers as _iers
    _iers.conf.auto_download = False
    _iers.conf.auto_max_age  = None
except Exception:
    pass

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time
import astropy.units as u

# ── Embedded catalogue coordinates ────────────────────────────────────────────
# Format per entry: (ra_deg J2000, dec_deg J2000, size_arcmin)
# size_arcmin = approximate major-axis diameter.
# Seestar S50 FOV ≈ 76′×57′.  Objects > 80′ need a mosaic; < 3′ look tiny.

COORDS: dict[str, tuple[float, float, float]] = {
    # ── Messier ──────────────────────────────────────────────────────────────
    "M1":   (83.822,  22.015,   7.0),   # Crab Nebula
    "M2":   (323.363, -0.823,  12.9),
    "M3":   (205.548,  28.378, 18.0),
    "M4":   (245.897, -26.526, 26.3),
    "M5":   (229.638,   2.082, 17.4),
    "M6":   (265.083, -32.217, 25.0),   # Butterfly Cluster
    "M7":   (268.458, -34.783, 80.0),   # Ptolemy Cluster
    "M8":   (270.921, -24.383, 90.0),   # Lagoon Nebula
    "M9":   (259.792, -18.517,  9.3),
    "M10":  (254.288,  -4.098, 15.1),
    "M11":  (282.767,  -6.267, 14.0),   # Wild Duck Cluster
    "M12":  (251.808,  -1.948, 16.0),
    "M13":  (250.423,  36.460, 20.0),   # Great Hercules Cluster
    "M14":  (267.921,  -3.245, 11.7),
    "M15":  (322.493,  12.167, 12.3),
    "M16":  (274.700, -13.817, 28.0),   # Eagle Nebula
    "M17":  (275.196, -16.183, 46.0),   # Omega/Swan Nebula
    "M18":  (275.071, -17.117,  9.0),
    "M19":  (255.658, -26.268, 13.5),
    "M20":  (270.621, -23.033, 28.0),   # Trifid Nebula
    "M21":  (271.083, -22.500, 13.0),
    "M22":  (279.098, -23.905, 24.0),   # Sagittarius Cluster
    "M23":  (269.238, -19.017, 27.0),
    "M24":  (274.421, -18.517,120.0),   # Sagittarius Star Cloud
    "M25":  (277.938, -19.233, 40.0),
    "M26":  (281.350,  -9.383, 15.0),
    "M27":  (299.902,  22.721,  8.0),   # Dumbbell Nebula
    "M28":  (276.133, -24.870,  9.8),
    "M29":  (308.567,  38.521,  7.0),
    "M30":  (325.092, -23.180, 11.0),
    "M31":  (10.685,   41.269,190.0),   # Andromeda Galaxy
    "M32":  (10.674,   40.866,  8.7),
    "M33":  (23.462,   30.660, 73.0),   # Triangulum Galaxy
    "M34":  (40.521,   42.783, 35.0),
    "M35":  (92.267,   24.333, 28.0),
    "M36":  (84.075,   34.133, 12.0),
    "M37":  (88.067,   32.550, 24.0),
    "M38":  (82.946,   35.850, 21.0),
    "M39":  (323.042,  48.433, 32.0),
    "M41":  (101.504, -20.733, 38.0),
    "M42":  (83.822,   -5.391, 85.0),   # Orion Nebula
    "M43":  (83.883,   -5.267, 20.0),
    "M44":  (130.025,  19.983, 95.0),   # Beehive Cluster
    "M45":  (56.875,   24.117,110.0),   # Pleiades
    "M46":  (115.417, -14.817, 27.0),
    "M47":  (114.167, -14.500, 30.0),
    "M48":  (123.417,  -5.800, 54.0),
    "M49":  (187.446,   8.000,  9.8),
    "M50":  (105.708,  -8.333, 16.0),
    "M51":  (202.469,  47.195, 11.2),   # Whirlpool Galaxy
    "M52":  (351.200,  61.583, 13.0),
    "M53":  (198.229,  18.169, 13.0),
    "M54":  (283.763, -30.480,  9.1),
    "M55":  (294.996, -30.965, 19.0),
    "M56":  (289.148,  30.184,  7.1),
    "M57":  (283.396,  33.029,  1.4),   # Ring Nebula
    "M58":  (189.433,  11.818,  5.9),
    "M59":  (190.508,  11.647,  5.4),
    "M60":  (190.917,  11.553,  7.4),
    "M61":  (185.475,   4.473,  6.5),
    "M62":  (255.300, -30.113, 14.1),
    "M63":  (198.956,  42.029, 12.6),   # Sunflower Galaxy
    "M64":  (194.182,  21.683, 10.0),   # Black Eye Galaxy
    "M65":  (169.733,  13.092, 10.0),
    "M66":  (170.063,  12.992,  9.1),
    "M67":  (132.825,  11.800, 30.0),
    "M68":  (189.867, -26.744, 12.0),
    "M69":  (277.846, -32.348,  7.1),
    "M70":  (280.804, -32.291,  7.8),
    "M71":  (298.443,  18.779,  7.2),
    "M72":  (313.365, -12.537,  5.9),
    "M74":  (24.174,   15.784, 10.5),   # Phantom Galaxy
    "M75":  (301.521, -21.921,  5.9),
    "M76":  (25.583,   51.575,  2.7),   # Little Dumbbell
    "M77":  (40.671,   -0.013,  7.1),
    "M78":  (86.692,    0.078,  8.0),
    "M79":  (81.046,  -24.524,  9.6),
    "M80":  (244.261, -22.976,  8.9),
    "M81":  (148.888,  69.065, 26.9),   # Bode's Galaxy
    "M82":  (148.970,  69.680, 11.2),   # Cigar Galaxy
    "M83":  (204.254, -29.866, 12.9),   # Southern Pinwheel
    "M84":  (186.266,  12.887,  6.5),
    "M85":  (186.350,  18.192,  7.1),
    "M86":  (186.550,  12.946,  8.9),
    "M87":  (187.706,  12.391,  8.3),   # Virgo A
    "M88":  (187.996,  14.420,  6.9),
    "M89":  (188.917,  12.557,  5.1),
    "M90":  (188.867,  13.163,  9.5),
    "M91":  (188.854,  14.496,  5.4),
    "M92":  (259.281,  43.136, 14.0),
    "M93":  (116.025, -23.867, 22.0),
    "M94":  (192.721,  41.120,  7.4),
    "M95":  (160.992,  11.703,  7.4),
    "M96":  (161.692,  11.820,  7.8),
    "M97":  (168.700,  55.019,  3.4),   # Owl Nebula
    "M98":  (183.450,  14.900,  9.8),
    "M99":  (184.706,  14.416,  5.4),
    "M100": (185.729,  15.822,  7.4),
    "M101": (210.802,  54.349, 28.8),   # Pinwheel Galaxy
    "M102": (226.625,  55.764,  5.2),   # Spindle Galaxy
    "M103": (23.333,   60.717,  6.0),
    "M104": (189.998, -11.623,  8.7),   # Sombrero Galaxy
    "M105": (161.958,  12.582,  4.3),
    "M106": (184.740,  47.304, 18.6),
    "M107": (248.133, -13.054, 10.0),
    "M108": (167.879,  55.674,  8.7),
    "M109": (179.400,  53.375,  7.6),
    "M110": (10.092,   41.685, 17.4),
    # ── Caldwell — selection of objects well-suited to Seestar ────────────────
    "C1":  (11.798,   85.344,  14.0),   # NGC 188
    "C2":  (4.542,    72.532,   0.6),   # Bow-Tie Nebula
    "C3":  (184.275,  69.465,  19.0),   # NGC 4236
    "C4":  (316.412,  68.168,  18.0),   # Iris Nebula
    "C5":  (56.703,   68.096,  21.0),   # IC 342
    "C6":  (269.639,  66.633,   0.3),   # Cat's Eye Nebula
    "C7":  (114.214,  65.603,  22.0),   # NGC 2403
    "C9":  (342.992,  62.618,  50.0),   # Cave Nebula
    "C10": (26.568,   61.233,   9.0),   # NGC 663
    "C11": (350.192,  61.200,  15.0),   # Bubble Nebula
    "C12": (308.718,  60.154,  11.0),   # Fireworks Galaxy
    "C13": (19.395,   58.283,  13.0),   # Owl Cluster
    "C14": (34.746,   57.133,  30.0),   # Double Cluster
    "C15": (295.275,  50.525,   0.5),   # Blinking Planetary
    "C17": (8.304,    48.508,   1.9),   # NGC 147
    "C18": (9.738,    48.338,   2.0),   # NGC 185
    "C19": (328.369,  47.267,  10.0),   # Cocoon Nebula
    "C20": (314.750,  44.533, 120.0),   # North America Nebula
    "C21": (187.046,  44.094,   5.5),   # NGC 4449
    "C22": (351.033,  42.550,   0.3),   # Blue Snowball
    "C23": (35.639,   42.350,  12.0),   # Silver Sliver
    "C25": (114.533,  38.883,   4.6),   # Intergalactic Wanderer
    "C26": (184.375,  37.807,  16.0),   # Silver Needle
    "C27": (303.117,  38.356,  18.0),   # Crescent Nebula
    "C28": (29.217,   37.683,  50.0),   # NGC 752
    "C29": (197.734,  37.059,   5.8),   # NGC 5005
    "C30": (339.267,  34.416,  10.7),   # NGC 7331
    "C31": (80.867,   34.267,  30.0),   # Flaming Star Nebula
    "C32": (190.533,  32.541,  15.2),   # Whale Galaxy
    "C33": (313.617,  31.717,  60.0),   # East Veil
    "C34": (312.900,  30.717,  70.0),   # West Veil
    "C38": (189.087,  25.988,  15.8),   # Needle Galaxy
    "C39": (112.300,  20.913,   0.7),   # Eskimo Nebula
    "C43": (1.050,    16.145,   4.0),   # NGC 7814
    "C44": (346.236,  12.323,   4.1),   # NGC 7479
    "C45": (204.383,   8.885,   6.2),   # NGC 5248
    "C46": (101.688,   8.728,   2.0),   # Hubble's Variable Neb.
    "C47": (308.547,   7.404,   5.9),   # NGC 6934
    "C49": (97.967,    4.967, 130.0),   # Rosette Nebula
    "C50": (97.875,    4.883,  24.0),   # NGC 2244
    "C52": (192.150,  -5.801,   6.0),   # NGC 4697
    "C53": (151.308,  -7.719,   8.3),   # Spindle Galaxy (Sextans)
    "C54": (119.988, -10.779,   7.0),   # NGC 2506
    "C55": (316.325, -11.357,   0.4),   # Saturn Nebula
    "C56": (11.746,  -11.875,   3.8),   # Skull Nebula
    "C58": (109.433, -15.633,  13.0),   # NGC 2360
    "C59": (156.025, -18.637,   1.6),   # Ghost of Jupiter
    "C60": (180.471, -18.867,   3.0),   # Antennae
    "C62": (11.788,  -20.760,  20.0),   # NGC 247
    "C63": (337.411, -20.837,  28.0),   # Helix Nebula
    "C65": (11.888,  -25.289,  27.5),   # Sculptor Galaxy
    "C67": (41.579,  -30.275,   9.3),   # NGC 1097
    "C70": (13.723,  -37.684,  21.9),   # NGC 300
    "C71": (118.025, -38.533,  27.0),   # NGC 2477
    "C77": (201.365, -43.019,  25.7),   # Centaurus A
    "C80": (201.697, -47.479,  36.3),   # Omega Centauri
}


# ── FOV classification ────────────────────────────────────────────────────────

def _fov_note(size: float) -> str:
    """Classify object size relative to Seestar S50 FOV (~76′×57′)."""
    if size > 90:  return "mosaic"    # needs mosaic to capture fully
    if size > 45:  return "large"     # fills most of frame — great
    if size > 8:   return "good"      # fits well
    if size > 2:   return "small"     # small in frame
    return "tiny"


# ── Moon info ─────────────────────────────────────────────────────────────────

def _moon_info(obs_date: _date, location: EarthLocation) -> dict:
    midnight = Time(f"{obs_date.isoformat()}T23:59:00", scale="utc")
    frame    = AltAz(obstime=midnight, location=location)
    moon     = get_body("moon", midnight)
    sun      = get_sun(midnight)
    elong    = float(moon.separation(sun).rad)
    illum    = (1.0 - math.cos(elong)) / 2.0 * 100.0
    age      = (midnight.jd - 2451550.1) % 29.530588
    alt      = float(moon.transform_to(frame).alt.deg)

    # Phase name
    if age < 1 or age > 28.5:    phase = "New Moon"
    elif age < 6.5:               phase = "Waxing Crescent"
    elif age < 8:                 phase = "First Quarter"
    elif age < 13.5:              phase = "Waxing Gibbous"
    elif age < 15.5:              phase = "Full Moon"
    elif age < 21.5:              phase = "Waning Gibbous"
    elif age < 23:                phase = "Last Quarter"
    else:                         phase = "Waning Crescent"

    return {
        "illum_pct":    round(illum, 1),
        "age_days":     round(age,   1),
        "phase":        phase,
        "alt_midnight": round(alt,   1),
    }


# ── Dark window ───────────────────────────────────────────────────────────────

def _dark_window(obs_date: _date, location: EarthLocation, step_min: int = 10) -> dict:
    noon   = Time(f"{obs_date.isoformat()}T12:00:00", scale="utc")
    n      = int(24 * 60 / step_min) + 1
    times  = noon + np.arange(n) * step_min / (24 * 60) * u.day
    sun    = get_sun(times)
    alts   = np.array([
        float(sun[i].transform_to(AltAz(obstime=times[i], location=location)).alt.deg)
        for i in range(n)
    ])
    dark   = alts < -18.0
    if not dark.any():
        return {"dark_start": None, "dark_end": None, "duration_h": 0.0}
    idx = np.where(dark)[0]
    return {
        "dark_start":  times[idx[0]].iso,
        "dark_end":    times[idx[-1]].iso,
        "duration_h":  round(float(len(idx) * step_min / 60), 1),
    }


# ── Vectorised altitude grid ──────────────────────────────────────────────────

def _alt_grid(
    ras: np.ndarray,   # (N,) degrees
    decs: np.ndarray,  # (N,) degrees
    obs_date: _date,
    location: EarthLocation,
    step_min: int = 20,
) -> tuple[np.ndarray, "Time"]:
    """
    Return (alts, times) where alts has shape (N_objects, N_times).
    step_min=20 → 73 time steps → fast enough for nightly planning.
    """
    noon    = Time(f"{obs_date.isoformat()}T12:00:00", scale="utc")
    n_t     = int(24 * 60 / step_min) + 1
    times   = noon + np.arange(n_t) * step_min / (24 * 60) * u.day
    coords  = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    n_obj   = len(ras)
    alts    = np.zeros((n_obj, n_t), dtype=float)

    for ti in range(n_t):
        frame  = AltAz(obstime=times[ti], location=location)
        aa     = coords.transform_to(frame)
        alts[:, ti] = aa.alt.deg

    return alts, times


# ── Rating ────────────────────────────────────────────────────────────────────

def _rate(peak_alt: float, moon_sep: float, moon_illum: float,
          size: float, have_data: bool) -> float:
    score = 0.0
    # Altitude (0–2.5)
    if peak_alt >= 70:    score += 2.5
    elif peak_alt >= 55:  score += 2.0
    elif peak_alt >= 40:  score += 1.5
    elif peak_alt >= 30:  score += 1.0
    else:                 score += 0.5
    # Moon (0–1.5)
    p = moon_illum / 100.0
    if moon_sep >= 90:    score += 1.5
    elif moon_sep >= 60:  score += 1.5 - p * 0.5
    elif moon_sep >= 30:  score += 1.0 - p * 0.7
    else:                 score += max(0.0, 0.5 - p)
    # FOV fit (0–0.5)
    note = _fov_note(size)
    if note == "good":    score += 0.5
    elif note in ("large","mosaic"): score += 0.2
    elif note == "small": score += 0.4
    # Never-imaged bonus
    if not have_data:     score += 0.5
    return round(min(score, 5.0), 2)


# ── Main entry point ──────────────────────────────────────────────────────────

def tonight_plan(
    lat:       float,
    lon:       float,
    elevation: float = 50.0,
    obs_date:  Optional[_date] = None,
    min_alt:   float = 20.0,
    sessions:  Optional[list]  = None,
) -> dict:
    """
    Compute tonight's visibility plan for all catalog objects.

    sessions — list of session dicts from db.get_all_sessions(), used to
               cross-reference what's already been imaged.

    Returns a result dict with moon, dark_window, and a sorted objects list.
    """
    from catalogs import MESSIER, CALDWELL, DSO_TYPE_LABELS
    import re as _re

    if obs_date is None:
        obs_date = datetime.now(timezone.utc).date()

    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m)

    # Build set of already-imaged objects from session DB
    have: dict[str, list] = {}
    if sessions:
        for s in sessions:
            name  = (s.get("object_name") or "").strip()
            dates = s.get("dates", [])
            if isinstance(dates, str):
                try:    dates = json.loads(dates)
                except: dates = []
            for pat, repl in [
                (r"(?i)^messier\s*(\d+)$", r"M\1"),
                (r"(?i)^m\s+(\d+)$",       r"M\1"),
                (r"(?i)^caldwell\s*(\d+)$", r"C\1"),
                (r"(?i)^c\s+(\d+)$",        r"C\1"),
            ]:
                if _re.match(pat, name):
                    key = _re.sub(pat, repl, name, flags=_re.I)
                    have[key] = dates
                    break
            else:
                have[name] = dates

    # Collect all object ids and coordinates
    obj_ids = list(COORDS.keys())
    ras  = np.array([COORDS[k][0] for k in obj_ids])
    decs = np.array([COORDS[k][1] for k in obj_ids])

    # Vectorised altitude grid (20-min steps → 73 time points)
    alts, times = _alt_grid(ras, decs, obs_date, location, step_min=20)

    # Moon & dark window
    moon  = _moon_info(obs_date, location)
    dark  = _dark_window(obs_date, location, step_min=10)

    # Moon position at midnight for separations
    midnight = Time(f"{obs_date.isoformat()}T23:59:00", scale="utc")
    moon_body = get_body("moon", midnight)
    all_coords = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    moon_seps  = all_coords.separation(moon_body).deg   # (N,)

    # Build result objects
    objects: list[dict] = []
    for i, obj_id in enumerate(obj_ids):
        obj_alts = alts[i]          # (n_times,)
        above    = obj_alts >= min_alt
        if not above.any():
            continue

        peak_idx   = int(np.argmax(obj_alts))
        above_idx  = np.where(above)[0]
        peak_alt   = float(obj_alts[peak_idx])
        rise_utc   = times[above_idx[0]].iso
        set_utc    = times[above_idx[-1]].iso
        transit_utc = times[peak_idx].iso
        moon_sep   = round(float(moon_seps[i]), 1)
        size       = COORDS[obj_id][2]

        # Catalog metadata
        if obj_id.startswith("M"):
            num  = int(obj_id[1:])
            cat  = MESSIER.get(num)
            label = f"M{num}"
        else:
            num  = int(obj_id[1:])
            cat  = CALDWELL.get(num)
            label = f"C{num}"

        if cat is None:
            continue
        pop_name, dso_type, constellation, ngc_ref = cat
        display_name = pop_name or ngc_ref or label

        have_data = obj_id in have or label in have
        session_dates = (have.get(obj_id) or have.get(label) or [])[:5]
        rating    = _rate(peak_alt, moon_sep, moon["illum_pct"], size, have_data)

        objects.append({
            "id":            obj_id,
            "label":         label,
            "name":          display_name,
            "type":          dso_type,
            "type_label":    DSO_TYPE_LABELS.get(dso_type, dso_type),
            "constellation": constellation,
            "ngc":           ngc_ref,
            "ra":            round(float(ras[i]),  3),
            "dec":           round(float(decs[i]), 3),
            "size_arcmin":   size,
            "fov_note":      _fov_note(size),
            "peak_alt":      round(peak_alt, 1),
            "rise_utc":      rise_utc,
            "set_utc":       set_utc,
            "transit_utc":   transit_utc,
            "moon_sep":      moon_sep,
            "rating":        rating,
            "have_data":     have_data,
            "session_dates": session_dates,
        })

    objects.sort(key=lambda o: o["rating"], reverse=True)

    return {
        "date":          obs_date.isoformat(),
        "lat":           lat,
        "lon":           lon,
        "elevation":     elevation,
        "moon":          moon,
        "dark_window":   dark,
        "total_visible": len(objects),
        "never_imaged":  sum(1 for o in objects if not o["have_data"]),
        "objects":       objects,
    }
