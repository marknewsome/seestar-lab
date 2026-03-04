"""Seestar Lab — aircraft lookup via OpenSky Network REST API.

Queries the bounding box around the observer at a given UTC time and returns
the closest airborne aircraft.  Uses only the Python standard library (urllib).

Environment variables
---------------------
OBSERVER_LAT      Observer latitude  (default 44.5646 = Corvallis, OR)
OBSERVER_LON      Observer longitude (default -123.2620)
OPENSKY_BBOX_DEG  Half-width of bounding box in degrees (default 1.5)
OPENSKY_USERNAME  OpenSky account username  (required for historical data)
OPENSKY_PASSWORD  OpenSky account password

Notes
-----
* Free OpenSky accounts can access historical state vectors up to 30 days old.
* Each state-vector query costs 1 API credit; free accounts get 400/day.
* Requests older than 30 days return 403 — the function returns [] gracefully.
"""

import base64
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

OBSERVER_LAT = float(os.environ.get("OBSERVER_LAT",  "44.5646"))
OBSERVER_LON = float(os.environ.get("OBSERVER_LON", "-123.2620"))
BBOX_DEG     = float(os.environ.get("OPENSKY_BBOX_DEG", "1.5"))

MAX_ALT_M    = 15_000   # ignore objects above 15 km (satellites / ISS)
_OPENSKY_URL = "https://opensky-network.org/api/states/all"
_TIMEOUT     = 10       # seconds per request


def lookup_aircraft(
    utc_dt: datetime,
    username: Optional[str] = None,
    password: Optional[str] = None,
    max_results: int = 5,
) -> list[dict]:
    """
    Return up to *max_results* aircraft (closest to the observer first) that
    were airborne in the bounding box around (OBSERVER_LAT, OBSERVER_LON) at
    *utc_dt*.

    Returns an empty list on any network or parse error — callers should treat
    a missing result as "data unavailable", not a hard error.
    """
    username = username or os.environ.get("OPENSKY_USERNAME", "")
    password = password or os.environ.get("OPENSKY_PASSWORD", "")

    params = {
        "time":  int(utc_dt.timestamp()),
        "lamin": OBSERVER_LAT - BBOX_DEG,
        "lamax": OBSERVER_LAT + BBOX_DEG,
        "lomin": OBSERVER_LON - BBOX_DEG,
        "lomax": OBSERVER_LON + BBOX_DEG,
    }
    url = _OPENSKY_URL + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={"User-Agent": "seestar-lab/1.0"})
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []

    states = data.get("states") or []
    candidates: list[dict] = []
    for s in states:
        # OpenSky state vector field indices:
        # 0  icao24        1  callsign       2  origin_country
        # 3  time_position 4  last_contact   5  longitude    6  latitude
        # 7  baro_altitude 8  on_ground      9  velocity     10 true_track
        # 11 vertical_rate 12 sensors        13 geo_altitude
        try:
            if s[8]:          # on_ground
                continue
            alt_m = s[7]
            if alt_m is None or alt_m > MAX_ALT_M:
                continue
            lat = s[6]
            lon = s[5]
            if lat is None or lon is None:
                continue
            icao24   = (s[0] or "").strip()
            callsign = (s[1] or "").strip() or icao24
            vel_ms   = s[9]
            heading  = s[10]
            candidates.append({
                "icao24":   icao24,
                "callsign": callsign,
                "lat":      round(lat, 4),
                "lon":      round(lon, 4),
                "alt_m":    int(alt_m),
                "alt_ft":   int(alt_m * 3.28084),
                "vel_ms":   round(vel_ms, 1) if vel_ms is not None else None,
                "heading":  int(heading)      if heading is not None else None,
            })
        except (IndexError, TypeError):
            continue

    # Sort by angular distance from observer (closest first)
    candidates.sort(key=lambda c: math.hypot(
        c["lat"] - OBSERVER_LAT,
        c["lon"] - OBSERVER_LON,
    ))
    return candidates[:max_results]
