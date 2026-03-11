"""
Object type detection and short descriptions for common Seestar S50 targets.

Type hierarchy returned by detect_type():
  solar | lunar | planet | comet | messier | caldwell | dso | unknown

Messier and Caldwell are sub-categories of DSO, kept distinct so the
catalog pages can track capture progress separately.
"""

import re
from typing import Optional

from catalogs import CALDWELL, MESSIER

# ── Classification sets ───────────────────────────────────────────────────────

PLANETS = {"mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"}
SOLAR_KEYWORDS = {"sun", "solar"}
LUNAR_KEYWORDS = {"moon", "lunar"}

# Comets: C/YYYY or C-YYYY or CYYYY (4-digit year distinguishes from Caldwell C1–C109)
# Also matches periodic comets: 12P, 24P, 45D, etc.
_COMET_RE = re.compile(
    r"\bC[-/]?\s*\d{4}\b"     # C/2024, C-2024, C2024
    r"|\bP/\s*\d{4}\b"        # P/2024 style
    r"|\b\d+[PD]\b"           # 24P, 12P, 45D (periodic / defunct)
    r"|\bcomet\b",
    re.IGNORECASE,
)

# Messier: M + 1–3 digit number (1–110)
_MESSIER_RE = re.compile(r"(?<![A-Za-z])M\s*(\d{1,3})(?!\d)", re.IGNORECASE)

# Caldwell: C + 1–3 digit number (1–109); won't match 4-digit years due to \b
# Must be checked AFTER _COMET_RE to avoid matching C followed by year prefix.
_CALDWELL_RE = re.compile(r"(?<![A-Za-z/\-])C\s*(\d{1,3})(?!\d)", re.IGNORECASE)

# General DSO identifiers
_DSO_RE = re.compile(
    r"\b(NGC|IC|Sh2|vdB|Ced|LDN|LBN|Cr|Mel|Stock|Tr|OCl|GCl)\s*\d+\b"
    r"|\bnebula\b|\bgalaxy\b|\bcluster\b|\bsupernova\b|\bremnant\b",
    re.IGNORECASE,
)

# ── Short descriptions ────────────────────────────────────────────────────────

_DESC: dict[str, str] = {
    # Messier
    "M1":   "Crab Nebula — supernova remnant in Taurus",
    "M8":   "Lagoon Nebula — emission nebula in Sagittarius",
    "M13":  "Hercules Cluster — globular cluster",
    "M16":  "Eagle Nebula — star-forming region in Serpens",
    "M17":  "Omega/Swan Nebula — emission nebula in Sagittarius",
    "M20":  "Trifid Nebula — emission/reflection nebula in Sagittarius",
    "M27":  "Dumbbell Nebula — planetary nebula in Vulpecula",
    "M31":  "Andromeda Galaxy — nearest major spiral galaxy",
    "M33":  "Triangulum Galaxy — Local Group spiral",
    "M42":  "Orion Nebula — brightest stellar nursery in the sky",
    "M43":  "De Mairan's Nebula — part of the Orion complex",
    "M45":  "Pleiades — nearby open star cluster in Taurus",
    "M51":  "Whirlpool Galaxy — interacting spirals in Canes Venatici",
    "M57":  "Ring Nebula — planetary nebula in Lyra",
    "M63":  "Sunflower Galaxy — spiral in Canes Venatici",
    "M64":  "Black Eye Galaxy — spiral in Coma Berenices",
    "M74":  "Phantom Galaxy — face-on spiral in Pisces",
    "M78":  "Reflection nebula in Orion",
    "M81":  "Bode's Galaxy — bright spiral in Ursa Major",
    "M82":  "Cigar Galaxy — starburst galaxy in Ursa Major",
    "M97":  "Owl Nebula — planetary nebula in Ursa Major",
    "M101": "Pinwheel Galaxy — face-on spiral in Ursa Major",
    "M104": "Sombrero Galaxy — edge-on spiral in Virgo",
    "M106": "Spiral galaxy in Canes Venatici",
    # NGC
    "NGC 869":  "h Persei — young open cluster, Double Cluster",
    "NGC 884":  "χ Persei — young open cluster, Double Cluster",
    "NGC 1499": "California Nebula — emission nebula in Perseus",
    "NGC 2237": "Rosette Nebula — emission nebula in Monoceros",
    "NGC 3372": "Eta Carinae Nebula — massive star-forming region",
    "NGC 6992": "Eastern Veil Nebula — supernova remnant in Cygnus",
    "NGC 7000": "North America Nebula — emission nebula in Cygnus",
    "NGC 7293": "Helix Nebula — nearest planetary nebula to Earth",
    "NGC 7662": "Blue Snowball — planetary nebula in Andromeda",
    # IC
    "IC 405":  "Flaming Star Nebula — emission/reflection in Auriga",
    "IC 1805": "Heart Nebula — emission nebula in Cassiopeia",
    "IC 1848": "Soul Nebula — emission nebula in Cassiopeia",
    "IC 1318": "Butterfly Nebula — emission nebula around γ Cygni",
    "IC 5070": "Pelican Nebula — emission nebula in Cygnus",
    # Solar system
    "Sun":     "Our star — always use a solar filter",
    "Moon":    "Earth's natural satellite",
    "Mercury": "Innermost planet — best seen at greatest elongation",
    "Venus":   "Brightest planet — shows phases like the Moon",
    "Mars":    "The Red Planet — surface features visible near opposition",
    "Jupiter": "Largest planet — cloud bands and Galilean moons",
    "Saturn":  "Ringed planet — rings visible at low magnification",
    "Uranus":  "Ice giant — pale blue-green disk",
    "Neptune": "Ice giant — tiny blue disk",
}


def _normalise(name: str) -> str:
    """Strip extra whitespace for dict lookups."""
    return re.sub(r"\s+", " ", name).strip()


class ObjectCatalog:
    def detect_type(self, name: str) -> str:
        """Return one of: solar | lunar | planet | comet | messier | caldwell | dso | unknown."""
        lower = name.lower().strip()

        if any(k in lower for k in SOLAR_KEYWORDS):
            return "solar"
        if any(k in lower for k in LUNAR_KEYWORDS):
            return "lunar"
        if lower in PLANETS:
            return "planet"

        # Comets before Caldwell: C/YYYY or C-YYYY patterns include a 4-digit year
        if _COMET_RE.search(name):
            return "comet"

        # Messier: M1–M110
        m = _MESSIER_RE.search(name)
        if m and 1 <= int(m.group(1)) <= 110:
            return "messier"

        # Caldwell: C1–C109 (1-3 digit number, won't fire on 4-digit years)
        m = _CALDWELL_RE.search(name)
        if m and 1 <= int(m.group(1)) <= 109:
            return "caldwell"

        if _DSO_RE.search(name):
            return "dso"

        return "unknown"

    def get_description(self, name: str) -> str:
        norm = _normalise(name)
        if norm in _DESC:
            return _DESC[norm]
        # Match ignoring internal spaces (e.g. "NGC7000" vs "NGC 7000")
        squished = re.sub(r"\s+", "", norm).upper()
        for key, val in _DESC.items():
            if re.sub(r"\s+", "", key).upper() == squished:
                return val
        return ""

    def type_label(self, object_type: str) -> str:
        return {
            "dso":      "DSO",
            "messier":  "Messier",
            "caldwell": "Caldwell",
            "planet":   "Planet",
            "solar":    "Solar",
            "lunar":    "Lunar",
            "comet":    "Comet",
            "unknown":  "Unknown",
        }.get(object_type, object_type.title())

    def messier_number(self, name: str) -> Optional[int]:
        """Return the Messier number if name matches a Messier object, else None."""
        m = _MESSIER_RE.search(name)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 110:
                return n
        return None

    def caldwell_number(self, name: str) -> Optional[int]:
        """Return the Caldwell number if name matches a Caldwell object, else None."""
        if _COMET_RE.search(name):
            return None  # comet wins
        m = _CALDWELL_RE.search(name)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 109:
                return n
        return None


def build_catalog_response(catalog: dict, catalog_type: str, captured_sessions: dict) -> list[dict]:
    """
    Build the full catalog list merging static catalog data with captured session data.

    captured_sessions: {object_name_lower: session_dict}
    Returns a list of dicts ready to JSON-serialise.
    """
    from catalogs import DSO_TYPE_GROUP, DSO_TYPE_LABELS

    prefix = "M" if catalog_type == "messier" else "C"
    result = []
    for number, (popular_name, dso_type, constellation, ngc_ref) in catalog.items():
        label = f"{prefix}{number}"
        # Try to match captured session — look for "M42", "M 42" variants
        session = _find_session(label, popular_name, ngc_ref, captured_sessions)
        result.append({
            "number":       number,
            "label":        label,
            "popular_name": popular_name,
            "dso_type":     dso_type,
            "dso_type_label": DSO_TYPE_LABELS.get(dso_type, dso_type),
            "dso_group":    DSO_TYPE_GROUP.get(dso_type, "other"),
            "constellation": constellation,
            "ngc_ref":      ngc_ref,
            "captured":     session is not None,
            "session":      session,
        })
    return result


def _find_session(label: str, popular_name: str, ngc_ref: str, sessions: dict) -> Optional[dict]:
    """
    Try several name variants to locate a matching captured session.
    sessions keys are object_name.lower() with spaces removed for fast lookup.
    """
    candidates = [label]  # "M42", "C63"
    # with space: "M 42", "C 63"
    if len(label) > 1:
        candidates.append(label[0] + " " + label[1:])
    if popular_name:
        candidates.append(popular_name)
    if ngc_ref:
        candidates.append(ngc_ref)
        # without space: "NGC7000"
        candidates.append(re.sub(r"\s+", "", ngc_ref))

    for cand in candidates:
        key = re.sub(r"\s+", "", cand).lower()
        if key in sessions:
            return sessions[key]
    return None
