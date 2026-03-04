"""
Seestar Lab — transit detection for solar and lunar videos.

Algorithm
---------
1. Temporal median background  — samples N frames evenly across the video and
   computes the pixel-wise median.  Sunspots and lunar craters are stationary
   and are baked into the background; they subtract away cleanly.

2. Disk detection  — Hough circle transform on the blurred background frame.
   Falls back to fitting a circle to the largest bright contour, which handles
   crescent moons where HoughCircles struggles.

3. Blob tracking  — each frame is differenced against the background, thresholded,
   morphologically opened, and masked to the disk interior.  Connected components
   are matched to active tracks by nearest-centroid with linear extrapolation.

   Camera-shake rejection: if more than SHAKE_HOT_FRAC of the disk area is lit
   up in a single diff frame, the whole frame is skipped (wind / re-pointing
   moves every sunspot edge simultaneously, creating many false blobs).  A
   per-frame blob-count cap provides a second line of defence.

4. Track scoring  — each track is evaluated on:
     • linearity (R²)       — planes/ISS travel in straight lines
     • velocity             — % of disk diameter per second
     • duration             — must be physically plausible
   Tracks moving < MIN_VEL_PCT of disk Ø per second are rejected; this is the
   primary sunspot-residual filter (sunspots move ~0.04 Ø/day, far below 3%/s).

5. Clip extraction  — pad_secs of context is prepended and appended; UTC time
   (parsed from the filename) is burned onto each frame; a JSON sidecar with
   the full track metadata is written alongside each clip.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

try:
    from scipy.stats import linregress as _scipy_linreg
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False

import yolo_validator

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False

# ── Tunable constants ─────────────────────────────────────────────────────────

N_BG_SAMPLES      = 30     # frames sampled for median background
DIFF_THRESH       = 12     # abs pixel difference to flag as foreground
MIN_BLOB_PX       = 8      # minimum foreground blob (pixels)
MAX_BLOB_FRAC     = 0.02   # max blob area as fraction of disk area
MIN_TRACK_FRAMES  = 8      # shortest valid track (raised from 4)
MAX_GAP_FRAMES    = 4      # frames a track may miss before it is ended
MIN_DISP_FRAC     = 0.05   # min displacement relative to disk radius
MIN_VEL_PCT       = 3.0    # min velocity (% of disk Ø/s); raised from 1.0
MIN_CONFIDENCE    = 0.70   # overall score threshold; raised from 0.65
PAD_SECS          = 5.0    # seconds of context added to each clip

# False-positive / camera-shake guards
SHAKE_HOT_FRAC    = 0.015  # >1.5 % of disk pixels lit up → shake / noise frame
MAX_BLOBS_PER_FRAME = 5    # more blobs than this in one frame → treat as noise
MAX_EVENTS_PER_VIDEO = 30  # safety cap: more than this almost certainly means FPs

# Telescope tracking-drift / wobble compensation
# A slow correction move shifts crater edges against the static background,
# creating linear blob tracks that look like transits.  Per-frame phase
# correlation detects the drift and realigns the background before differencing.
DRIFT_CROP = 256   # px: side of the square crop used for phase correlation
DRIFT_MAX  = 8.0   # px: clamp on the correction (larger = re-pointing or error)
DRIFT_MIN  = 0.3   # px: ignore sub-pixel noise below this threshold

# Timezone used when parsing timestamps from filenames
VIDEO_TIMEZONE    = "America/Los_Angeles"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class TransitEvent:
    label:                str            # 'plane' | 'bird' | 'iss' | 'unknown'
    confidence:           float          # 0–1
    frame_start:          int
    frame_end:            int
    duration_s:           float
    velocity_pct_per_sec: float          # % of disk diameter per second
    linearity:            float          # R² of linear fit through track
    fps:                  float
    width:                int
    height:               int
    disk_center:          list           # [cx, cy]
    disk_radius:          int
    frame_utc_start:      Optional[str] = None   # ISO-8601 UTC of frame_start
    clip_path:            Optional[str] = None
    meta_path:            Optional[str] = None
    thumb_path:           Optional[str] = None   # JPEG of hero frame
    yolo_label:           Optional[str]   = None # YOLO-validated class ('airplane'/'bird') or None
    yolo_confidence:      Optional[float] = None # YOLO detection confidence, or None
    track_xs:             list = field(default_factory=list)
    track_ys:             list = field(default_factory=list)
    track_frames:         list = field(default_factory=list)


# ── Detector ──────────────────────────────────────────────────────────────────

class TransitDetector:
    """
    Parameters
    ----------
    video_path : str
        Path to the source video (.mp4 / .avi / .mov / .mkv).
    video_type : str
        'solar' or 'lunar'.  The core algorithm is the same; lunar uses
        lower thresholds because the moon is dimmer than the sun, creating
        less contrast against transiting objects.
    """

    def __init__(
        self,
        video_path:  str,
        video_type:  str = "solar",
        source_path: Optional[str] = None,
    ) -> None:
        self.video_path   = video_path                  # file to read (may be a local cache copy)
        self._source_path = source_path or video_path   # original path — used for naming & metadata
        self.video_type   = video_type

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {video_path}")

        self.fps          = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()

        # Will be set during detect()
        self._disk_center: tuple[int, int] = (self.width // 2, self.height // 2)
        self._disk_radius: int             = min(self.width, self.height) // 2

        # Parsed recording start (UTC) — parse from original filename, not temp copy
        self._video_start_utc: Optional[datetime] = _parse_video_start_utc(self._source_path)

        # Per-type detection parameters.
        # Lunar: the moon is dimmer than the sun, producing lower-contrast
        # silhouettes.  Relax thresholds so faint blobs aren't discarded.
        # The sunspot false-positive guards are loosened too — the moon has no
        # sunspots and its surface features cause less blob noise.
        if video_type == "lunar":
            self._diff_thresh         = 8     # was 12 — lower contrast vs solar
            self._min_track_frames    = 7     # raised from 5: ≥7 pts for reliable R²
            self._min_vel_pct         = 2.0   # was 3.0
            self._min_confidence      = 0.60  # was 0.70
            self._min_linearity       = 0.60  # hard R² floor — seeing shimmer clusters < 0.60
            self._min_fill_frac       = 0.50  # blob must appear in ≥50 % of spanned frames
            self._min_perimeter_frac  = 0.60  # one track end must reach 60 % of disk radius
            self._shake_hot_frac      = 0.04  # was 0.015 — more lenient
            self._max_blobs_per_frame = 10    # was 5    — more lenient
            self._max_blob_frac       = 0.95  # allow large objects (e.g. nearby aircraft)
        else:  # solar — original calibrated values
            self._diff_thresh         = DIFF_THRESH
            self._min_track_frames    = MIN_TRACK_FRAMES
            self._min_vel_pct         = MIN_VEL_PCT
            self._min_confidence      = MIN_CONFIDENCE
            self._min_linearity       = 0.0   # no hard floor for solar
            self._min_fill_frac       = 0.0   # disabled for solar
            self._min_perimeter_frac  = 0.0   # disabled for solar
            self._shake_hot_frac      = SHAKE_HOT_FRAC
            self._max_blobs_per_frame = MAX_BLOBS_PER_FRAME
            self._max_blob_frac       = MAX_BLOB_FRAC

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(
        self,
        output_dir: str,
        pad_secs: float = PAD_SECS,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        cancel_cb:   Optional[Callable[[], bool]]              = None,
    ) -> list[TransitEvent]:
        """
        Run detection, write clips + JSON sidecars, return all events found.

        progress_cb(pct_0_100, total_100, message) is called periodically.
        cancel_cb() returns True if the job should be aborted; checked every
        30 frames during blob tracking.
        """
        def _cb(pct: int, msg: str = "") -> None:
            if progress_cb:
                progress_cb(pct, 100, msg)

        _cb(0,  "Computing background frame…")
        background = self._compute_background()

        _cb(8,  "Locating disk…")
        self._disk_center, self._disk_radius = self._find_disk(background)
        mask = self._make_mask()

        _cb(12, "Scanning frames…")
        tracks = self._track_blobs(background, mask, progress_cb, cancel_cb)

        _cb(92, "Scoring tracks…")
        events = self._score_tracks(tracks)

        # Safety cap: if we got an absurd number of events the shake filter
        # was not enough — return nothing rather than write hundreds of clips.
        if len(events) > MAX_EVENTS_PER_VIDEO:
            _cb(100, f"⚠ {len(events)} events (exceeds cap of {MAX_EVENTS_PER_VIDEO})"
                     " — likely false positives; no clips written")
            return []

        # Attach UTC timestamps
        if self._video_start_utc is not None:
            for ev in events:
                ev_utc = self._video_start_utc + timedelta(seconds=ev.frame_start / self.fps)
                ev.frame_utc_start = ev_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        if events:
            _cb(95, f"Writing {len(events)} clip(s)…")
            self._write_clips(events, output_dir, pad_secs)

            if yolo_validator.is_available():
                _cb(98, "Running YOLO validation…")
                for ev in events:
                    if ev.thumb_path:
                        ev.yolo_label, ev.yolo_confidence = yolo_validator.validate(ev.thumb_path)

        _cb(100, f"Done — {len(events)} event(s) found")
        return events

    # ── Background model ──────────────────────────────────────────────────────

    def _compute_background(self) -> np.ndarray:
        """
        Sample N_BG_SAMPLES frames evenly across the video in a single
        forward pass.  cap.grab() advances the stream without a full pixel
        decode; only the N keeper frames pay the decompression cost, making
        this faster than N random cap.set() seeks regardless of storage type.
        """
        interval  = max(1, self.total_frames // N_BG_SAMPLES)
        cap       = cv2.VideoCapture(self.video_path)
        frames:   list[np.ndarray] = []
        frame_idx = 0
        next_keep = 0
        while len(frames) < N_BG_SAMPLES:
            if frame_idx == next_keep:
                ret, frame = cap.read()      # full decode — keeper frame
                if not ret:
                    break
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
                next_keep += interval
            else:
                if not cap.grab():           # advance stream, skip decode
                    break
            frame_idx += 1
        cap.release()
        if not frames:
            return np.zeros((self.height, self.width), dtype=np.uint8)
        return np.median(np.stack(frames, axis=0), axis=0).astype(np.uint8)

    # ── Disk detection ────────────────────────────────────────────────────────

    def _find_disk(self, bg: np.ndarray) -> tuple[tuple[int, int], int]:
        blurred = cv2.GaussianBlur(bg, (9, 9), 2)
        dim     = min(self.width, self.height)
        min_r   = dim // 5
        max_r   = dim // 2 + 20

        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.5,
            minDist=max_r, param1=50, param2=30,
            minRadius=min_r, maxRadius=max_r,
        )
        if circles is not None:
            c = np.round(circles[0][0]).astype(int)
            return (int(c[0]), int(c[1])), int(c[2])

        # Fallback: fit circle to the largest bright contour (crescent moon, etc.)
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_OTSU)
        cnts, _   = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            largest    = max(cnts, key=cv2.contourArea)
            (x, y), r  = cv2.minEnclosingCircle(largest)
            return (int(x), int(y)), max(1, int(r))

        # Last resort: image centre
        return (self.width // 2, self.height // 2), dim // 2

    def _make_mask(self) -> np.ndarray:
        mask = np.zeros((self.height, self.width), dtype=np.uint8)
        cv2.circle(mask, self._disk_center, self._disk_radius, 255, -1)
        return mask

    # ── Blob tracker ──────────────────────────────────────────────────────────

    # ── Drift compensation ────────────────────────────────────────────────────

    def _estimate_disk_shift(
        self,
        frame_gray: np.ndarray,
        bg_f32: np.ndarray,
    ) -> tuple[float, float]:
        """
        Estimate the translational drift of the disk since the background was
        computed, using phase correlation on a square crop centred on the disk.

        The Seestar's alt-az mount makes smooth servo corrections that shift
        crater edges against the static background, generating blobs that look
        like transiting objects.  Measuring the shift here lets _track_blobs
        realign the background before differencing so those artefacts subtract
        away cleanly.

        Returns (dx, dy) in pixels (sub-pixel accurate).
        """
        cx, cy = self._disk_center
        half   = DRIFT_CROP // 2
        x1, x2 = cx - half, cx + half
        y1, y2 = cy - half, cy + half
        # If the disk is too close to the frame edge for a full crop, skip.
        if x1 < 0 or y1 < 0 or x2 > self.width or y2 > self.height:
            return 0.0, 0.0
        bg_crop = bg_f32[y1:y2, x1:x2]
        # Texture gate: a featureless crop (dark side of a crescent moon, or a
        # very smooth solar disk with no sunspots) gives noisy phase-correlation
        # output and should not be used for drift correction.
        if float(bg_crop.std()) < 4.0:
            return 0.0, 0.0
        (dx, dy), _ = cv2.phaseCorrelate(
            bg_crop,
            frame_gray[y1:y2, x1:x2].astype(np.float32),
        )
        return float(dx), float(dy)

    def _track_blobs(
        self,
        background: np.ndarray,
        mask: np.ndarray,
        progress_cb,
        cancel_cb,
    ) -> list[dict]:
        disk_px    = float(cv2.countNonZero(mask))   # pixels inside disk
        disk_area  = max(1.0, np.pi * self._disk_radius ** 2)
        max_blob   = disk_area * self._max_blob_frac
        # Secondary dominance floor: a blob must exceed this many pixels to be
        # treated as a large aircraft (rules out random large seeing-noise blobs).
        # 0.3 % of the disk area ≈ a circle of radius ~0.055 × disk_radius.
        min_dominant_px = disk_area * 0.003
        match_dist = self._disk_radius * 0.12        # 12 % of disk radius
        kernel     = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        # Pre-convert background to float32 once for phase-correlation drift
        # estimation (phaseCorrelate requires float input).
        bg_f32 = background.astype(np.float32)

        active: dict[int, dict]  = {}
        completed: list[dict]    = []
        next_id = 0

        cap = cv2.VideoCapture(self.video_path)
        for frame_idx in range(self.total_frames):
            ret, frame = cap.read()
            if not ret:
                break

            # ── Cancellation check ────────────────────────────────────────────
            if cancel_cb and frame_idx % 30 == 0 and cancel_cb():
                cap.release()
                raise RuntimeError("cancelled")

            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Telescope drift compensation: estimate how far the disk has
            # shifted since the background was computed, then realign before
            # differencing.  This prevents servo corrections from producing
            # linear crater-edge blobs that mimic transiting objects.
            _dx, _dy = self._estimate_disk_shift(gray, bg_f32)
            _dx = max(-DRIFT_MAX, min(DRIFT_MAX, _dx))
            _dy = max(-DRIFT_MAX, min(DRIFT_MAX, _dy))
            if abs(_dx) >= DRIFT_MIN or abs(_dy) >= DRIFT_MIN:
                _M  = np.float32([[1, 0, _dx], [0, 1, _dy]])
                _bg = cv2.warpAffine(
                    background, _M, (self.width, self.height),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
                diff = cv2.absdiff(gray, _bg)
            else:
                diff = cv2.absdiff(gray, background)

            _, th = cv2.threshold(diff, self._diff_thresh, 255, cv2.THRESH_BINARY)
            th    = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
            th    = cv2.bitwise_and(th, mask)

            # ── Blob detection ────────────────────────────────────────────────
            hot_frac = cv2.countNonZero(th) / max(disk_px, 1.0)
            n, _labels, stats, centroids = cv2.connectedComponentsWithStats(th)
            # Collect ALL blobs ≥ MIN_BLOB_PX without an upper size cap first.
            # The size cap is applied after the dominant-blob check so that a
            # large aircraft isn't silently discarded before we can identify it.
            all_blobs: list[dict] = []
            for i in range(1, n):
                area = float(stats[i, cv2.CC_STAT_AREA])
                if area >= MIN_BLOB_PX:
                    w = stats[i, cv2.CC_STAT_WIDTH]
                    h = stats[i, cv2.CC_STAT_HEIGHT]
                    all_blobs.append({
                        "cx":     float(centroids[i][0]),
                        "cy":     float(centroids[i][1]),
                        "area":   area,
                        "aspect": max(w, h) / max(min(w, h), 1),
                        "frame":  frame_idx,
                    })

            # ── Camera-shake / noise rejection ────────────────────────────────
            # Apply the normal per-blob size cap for routine tracking.
            blobs: list[dict] = [b for b in all_blobs if b["area"] <= max_blob]

            # Oversized-dominant bypass: if the single largest blob exceeds the
            # size cap *and* clearly dominates everything else, it is almost
            # certainly a large aircraft (not noise).  Substitute it for the
            # size-capped blob list so it stays visible to the tracker even in
            # frames where the size-capped blob count is within normal limits
            # (and therefore the many-blob dominance check below never fires).
            if all_blobs:
                _top       = sorted(all_blobs, key=lambda b: -b["area"])
                _lg_area   = _top[0]["area"]
                _sec_area  = _top[1]["area"] if len(_top) > 1 else 0
                _ratio     = _lg_area / _sec_area if _sec_area > 0 else 0
                if _lg_area > max_blob:   # only for blobs exceeding the cap
                    _dom = _ratio >= 20
                    if not _dom and self.video_type == "solar":
                        _dom = _ratio >= 5 and _lg_area >= min_dominant_px
                    if _dom:
                        blobs = [_top[0]]

            if len(blobs) > self._max_blobs_per_frame:
                # Too many blobs after size-capping.  Before treating the frame
                # as camera shake, check whether one object vastly dominates —
                # the signature of a large aircraft rather than shake or seeing
                # noise.  Dominance is tested against the *unfiltered* all_blobs
                # list so an oversized aircraft (e.g. a nearby Cessna whose blob
                # exceeds MAX_BLOB_FRAC) isn't hidden by the size cap.
                is_dominant = False
                if all_blobs:
                    all_sorted   = sorted(all_blobs, key=lambda b: -b["area"])
                    largest_area = all_sorted[0]["area"]
                    second_area  = all_sorted[1]["area"] if len(all_sorted) > 1 else 0
                    ratio        = largest_area / second_area if second_area > 0 else 0
                    # Primary tier (both video types): ≥ 20× is unambiguous.
                    # Nearby large aircraft (Cessna-scale in lunar) easily clear this.
                    is_dominant = ratio >= 20
                    if not is_dominant and self.video_type == "solar":
                        # Secondary tier (solar only): smaller aircraft at altitude
                        # may only reach 5–19× but are still identifiable if the
                        # blob is large enough to rule out ordinary seeing noise.
                        # This tier is intentionally skipped for lunar — aircraft
                        # visible against the moon are close enough to clear 20×,
                        # and applying the looser threshold to lunar videos causes
                        # crater-shimmer blobs to generate false detections.
                        is_dominant = ratio >= 5 and largest_area >= min_dominant_px

                if is_dominant:
                    blobs = [all_sorted[0]]
                else:
                    # Genuine shake or seeing noise — age tracks and skip frame.
                    for tid in list(active):
                        active[tid]["gap"] = active[tid].get("gap", 0) + 1
                        if active[tid]["gap"] > MAX_GAP_FRAMES:
                            completed.append(active.pop(tid))
                    if progress_cb and frame_idx % 30 == 0:
                        pct = 12 + int(frame_idx / max(self.total_frames, 1) * 78)
                        progress_cb(pct, 100, f"Scanning frame {frame_idx:,} / {self.total_frames:,}…")
                    continue

            # Match blobs to active tracks using predicted position
            unmatched = list(blobs)
            for tid in list(active):
                track = active[tid]
                pts   = track["points"]
                last  = pts[-1]
                # Linear extrapolation from last two points
                if len(pts) >= 2:
                    dx = pts[-1]["cx"] - pts[-2]["cx"]
                    dy = pts[-1]["cy"] - pts[-2]["cy"]
                    pred_x = last["cx"] + dx
                    pred_y = last["cy"] + dy
                else:
                    pred_x, pred_y = last["cx"], last["cy"]

                best_d, best_i = float("inf"), -1
                for i, b in enumerate(unmatched):
                    d = ((b["cx"] - pred_x) ** 2 + (b["cy"] - pred_y) ** 2) ** 0.5
                    if d < best_d:
                        best_d, best_i = d, i

                if best_d <= match_dist and best_i >= 0:
                    track["points"].append(unmatched.pop(best_i))
                    track["gap"] = 0
                else:
                    track["gap"] = track.get("gap", 0) + 1
                    if track["gap"] > MAX_GAP_FRAMES:
                        completed.append(active.pop(tid))

            # Start new tracks for unmatched blobs
            for b in unmatched:
                active[next_id] = {"id": next_id, "points": [b], "gap": 0}
                next_id += 1

            # Progress: 12 % (disk found) → 90 % (scoring starts)
            if progress_cb and frame_idx % 30 == 0:
                pct = 12 + int(frame_idx / max(self.total_frames, 1) * 78)
                progress_cb(
                    pct, 100,
                    f"Scanning frame {frame_idx:,} / {self.total_frames:,}…",
                )

        cap.release()
        for t in active.values():
            completed.append(t)
        return completed

    # ── Scoring and classification ────────────────────────────────────────────

    def _score_tracks(self, tracks: list[dict]) -> list[TransitEvent]:
        events: list[TransitEvent] = []

        for track in tracks:
            pts = track["points"]
            if len(pts) < self._min_track_frames:
                continue

            xs     = np.array([p["cx"]    for p in pts], dtype=float)
            ys     = np.array([p["cy"]    for p in pts], dtype=float)
            frames = np.array([p["frame"] for p in pts], dtype=int)

            disp = float(np.hypot(xs[-1] - xs[0], ys[-1] - ys[0]))
            if disp < self._disk_radius * MIN_DISP_FRAC:
                continue

            duration_frames  = int(frames[-1]) - int(frames[0]) + 1
            duration_s       = duration_frames / self.fps
            velocity_px_s    = (disp / max(duration_frames, 1)) * self.fps
            velocity_pct     = velocity_px_s / (2.0 * self._disk_radius) * 100.0

            # Primary velocity filter (sunspot residuals / surface shimmer)
            if velocity_pct < self._min_vel_pct:
                continue

            # Track continuity: a real transit blob is visible in most frames
            # it spans.  Seeing shimmer materialises and vanishes erratically,
            # accumulating the minimum point count but with many gaps in between.
            fill_frac = len(pts) / max(duration_frames, 1)
            if fill_frac < self._min_fill_frac:
                continue

            linearity  = _linearity_r2(xs, ys)

            # Hard R² floor.  ISS candidates (fast + near-perfect linearity) are
            # exempt so a genuine satellite transit isn't silently discarded even
            # if the track is short.  For everything else, low-R² tracks are
            # almost always atmospheric seeing shimmer; reject them early.
            _iss_candidate = velocity_pct > 40 and linearity > 0.95
            if linearity < self._min_linearity and not _iss_candidate:
                continue

            # Entry/exit proximity: real transits cross the disk and therefore
            # begin or end near the disk perimeter.  Blobs that materialise
            # entirely inside the disk are almost certainly atmospheric artefacts.
            if self._min_perimeter_frac > 0:
                _cx, _cy = self._disk_center
                _r       = max(self._disk_radius, 1)
                start_r  = ((xs[0]  - _cx) ** 2 + (ys[0]  - _cy) ** 2) ** 0.5 / _r
                end_r    = ((xs[-1] - _cx) ** 2 + (ys[-1] - _cy) ** 2) ** 0.5 / _r
                if max(start_r, end_r) < self._min_perimeter_frac:
                    continue

            label      = _classify(velocity_pct, linearity, duration_s)
            confidence = _confidence(linearity, velocity_pct, duration_s)

            if confidence < self._min_confidence:
                continue

            events.append(TransitEvent(
                label=label,
                confidence=round(confidence, 3),
                frame_start=int(frames[0]),
                frame_end=int(frames[-1]),
                duration_s=round(duration_s, 2),
                velocity_pct_per_sec=round(velocity_pct, 1),
                linearity=round(linearity, 3),
                fps=self.fps,
                width=self.width,
                height=self.height,
                disk_center=list(self._disk_center),
                disk_radius=self._disk_radius,
                track_xs=xs.tolist(),
                track_ys=ys.tolist(),
                track_frames=frames.tolist(),
            ))

        events.sort(key=lambda e: e.frame_start)
        return events

    # ── Hero frame ────────────────────────────────────────────────────────────

    def _hero_frame_num(self, ev: "TransitEvent") -> int:
        """
        Return the frame index where the track is closest to the disk centre.
        That is the frame where the object is most 'central' in the disk —
        typically the most photogenic moment of the transit.
        Falls back to the track midpoint if track data is absent.
        """
        if not ev.track_frames:
            return (ev.frame_start + ev.frame_end) // 2
        cx, cy   = self._disk_center
        min_dist = float("inf")
        hero     = ev.track_frames[len(ev.track_frames) // 2]
        for x, y, f in zip(ev.track_xs, ev.track_ys, ev.track_frames):
            d = (x - cx) ** 2 + (y - cy) ** 2
            if d < min_dist:
                min_dist = d
                hero     = f
        return int(hero)

    # ── Clip extraction ───────────────────────────────────────────────────────

    def _write_clips(
        self,
        events: list[TransitEvent],
        output_dir: str,
        pad_secs: float,
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        stem   = Path(self._source_path).stem
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

        for i, ev in enumerate(events, start=1):
            pad     = int(pad_secs * self.fps)
            start_f = max(0, ev.frame_start - pad)
            end_f   = min(self.total_frames - 1, ev.frame_end + pad)
            hero_f  = self._hero_frame_num(ev)   # always within [start_f, end_f]

            slug      = f"{stem}_t{i:02d}_{ev.label}_{ev.confidence:.2f}"
            clip_path = os.path.join(output_dir, slug + ".mp4")
            meta_path = os.path.join(output_dir, slug + ".json")

            # Extract clip and capture the hero frame in the same pass —
            # no second VideoCapture / seek needed for the thumbnail.
            cap = cv2.VideoCapture(self.video_path)
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
            out = cv2.VideoWriter(
                clip_path, fourcc, self.fps, (self.width, self.height)
            )
            hero_frame = None
            for clip_frame_i in range(end_f - start_f + 1):
                ret, frame = cap.read()
                if not ret:
                    break
                abs_frame = start_f + clip_frame_i
                _burn_utc(frame, self._video_start_utc, abs_frame, self.fps)
                out.write(frame)
                if abs_frame == hero_f:
                    hero_frame = frame.copy()   # UTC already burned in
            out.release()
            cap.release()

            thumb_path = os.path.join(output_dir, slug + "_thumb.jpg")
            if hero_frame is not None:
                cv2.imwrite(thumb_path, hero_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                ev.thumb_path = thumb_path
                _embed_thumbnail(clip_path, thumb_path)

            ev.clip_path = clip_path
            ev.meta_path = meta_path

            # JSON sidecar
            sidecar = {
                "video_path":  self._source_path,
                "video_type":  self.video_type,
                "detected_at": datetime.now().isoformat(timespec="seconds"),
                **asdict(ev),
            }
            with open(meta_path, "w") as f:
                json.dump(sidecar, f, indent=2)


# ── Pure-function helpers ─────────────────────────────────────────────────────

def _embed_thumbnail(clip_path: str, thumb_path: str) -> None:
    """
    Transcode the OpenCV-written mp4v clip to H.264 (browser-compatible) and
    embed thumb_path as cover-art so file browsers show the hero frame.
    Requires ffmpeg on PATH; silently skips if unavailable or if it fails.
    """
    if not shutil.which("ffmpeg"):
        return
    tmp = clip_path + ".thumb_tmp.mp4"
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", clip_path,
                "-i", thumb_path,
                "-map", "0",
                "-map", "1",
                "-c:v:0", "libx264",   # transcode mp4v → H.264 for browser playback
                "-crf", "23",
                "-preset", "fast",
                "-c:v:1", "mjpeg",
                "-disposition:v:1", "attached_pic",
                tmp,
            ],
            capture_output=True,
        )
        if r.returncode == 0:
            os.replace(tmp, clip_path)
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_FNAME_DT_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})')


def _parse_video_start_utc(video_path: str) -> Optional[datetime]:
    """
    Parse the recording start time from a Seestar filename such as
    2025-08-05-085206-Solar.mp4 and return it as a UTC-aware datetime.
    Returns None if the filename doesn't match or zoneinfo is unavailable.
    """
    m = _FNAME_DT_RE.search(Path(video_path).stem)
    if not m:
        return None
    if not _HAVE_ZONEINFO:
        return None
    try:
        local_tz = _ZoneInfo(VIDEO_TIMEZONE)
        local_dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
            tzinfo=local_tz,
        )
        return local_dt.astimezone(timezone.utc)
    except Exception:
        return None


def _burn_utc(
    frame: np.ndarray,
    video_start_utc: Optional[datetime],
    frame_number: int,
    fps: float,
) -> None:
    """Burn a UTC timestamp into the top-left corner of a frame (in-place)."""
    if video_start_utc is None:
        return
    frame_utc = video_start_utc + timedelta(seconds=frame_number / fps)
    ts = frame_utc.strftime("UTC  %Y-%m-%d  %H:%M:%S")
    # Shadow pass (black outline) then white text for contrast on any background
    cv2.putText(frame, ts, (8, 30), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, ts, (8, 30), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (255, 255, 255), 1, cv2.LINE_AA)


def _linearity_r2(xs: np.ndarray, ys: np.ndarray) -> float:
    """R² of a linear regression, choosing the axis with more spread."""
    if len(xs) < 2:
        return 0.0
    # Use the axis with greater variance as independent variable
    x_var, y_var = (xs, ys) if np.std(xs) >= np.std(ys) else (ys, xs)
    if np.std(x_var) < 1e-9:   # degenerate: all points on a vertical line → R²=1
        return 1.0
    if _HAVE_SCIPY:
        _, _, r, _, _ = _scipy_linreg(x_var, y_var)
        return float(r ** 2)
    # NumPy fallback
    try:
        c      = np.polyfit(x_var, y_var, 1)
        resid  = y_var - np.polyval(c, x_var)
        ss_res = float(np.sum(resid ** 2))
        ss_tot = float(np.sum((y_var - y_var.mean()) ** 2))
        return max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 1.0
    except Exception:
        return 0.0


def _classify(velocity_pct: float, linearity: float, duration_s: float) -> str:
    if velocity_pct > 40 and linearity > 0.97:
        return "iss"
    if linearity >= 0.90 and velocity_pct >= 3:
        return "plane"
    if velocity_pct < 8:
        return "bird"
    return "unknown"


def _confidence(linearity: float, velocity_pct: float, duration_s: float) -> float:
    """
    Weighted confidence score.

    Weights:  linearity 50 %  |  velocity 30 %  |  duration 20 %

    Velocity score is 1.0 in the core plane/ISS band (3–50 % Ø/s), tapers for
    very slow (birds) or extremely fast (ISS at >50 %) objects.
    Duration score is 1.0 for physically plausible transits (0.1–30 s).
    """
    lin_score = linearity

    if 3 <= velocity_pct <= 50:
        vel_score = 1.0
    elif velocity_pct > 50:
        vel_score = min(1.0, 100.0 / velocity_pct)   # ISS ~125 %/s → 0.80
    else:
        vel_score = velocity_pct / 3.0                # slow ramp-in for birds

    dur_score = 1.0 if 0.1 <= duration_s <= 30 else 0.3

    return 0.50 * lin_score + 0.30 * vel_score + 0.20 * dur_score
