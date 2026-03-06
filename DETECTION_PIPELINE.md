# Seestar Lab — Transit Detection Pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│                          INPUT VIDEO                                 │
│               solar .mp4  ·  lunar .mp4  (any resolution)           │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  1. BACKGROUND MODEL                                                 │
│                                                                      │
│  Single forward pass through the video.                              │
│  cap.grab() advances without decoding; cap.read() on every Nth frame │
│  (N = total_frames ÷ 30) fully decodes the 30 keeper frames.        │
│                                                                      │
│  pixel-wise median of 30 frames  →  static background image         │
│  (sunspots, craters, and fixed noise are baked in and subtract away) │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  2. DISK DETECTION                                                   │
│                                                                      │
│  Hough circle transform on blurred background                        │
│     → falls back to enclosing circle of largest bright contour       │
│        (handles crescent moon, partial disk, heavy cloud)            │
│     → last resort: image centre + half-frame radius                  │
│                                                                      │
│  result: (cx, cy, radius)  +  filled-circle mask                    │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
              ┌───────────────▼───────────────┐
              │    FOR EACH FRAME (all N)     │◄─────────────────────┐
              └───────────────┬───────────────┘                      │
                              │                                       │
                              ▼                                       │
              ┌───────────────────────────────┐                      │
              │  drift compensation            │                      │
              │                                │                      │
              │  phase-correlate 256×256 disk  │                      │
              │  crop (frame vs background)    │                      │
              │  → (dx, dy) sub-pixel shift    │                      │
              │  texture gate: skip if crop    │                      │
              │  std < 4 (featureless disk)    │                      │
              │  clamp |shift| ≤ 8 px          │                      │
              │  if |dx|≥0.3 or |dy|≥0.3:     │                      │
              │    warpAffine background       │                      │
              └───────────────┬───────────────┘                      │
                              │                                       │
              ┌───────────────▼───────────────┐                      │
              │  per-frame differencing        │                      │
              │                                │                      │
              │  |frame − aligned_bg| → diff  │                      │
              │  threshold (8 lunar / 12 solar)│                      │
              │  morphological open (3×3)      │                      │
              │  AND with disk mask            │                      │
              └───────────────┬───────────────┘                      │
                              │                                       │
                              ▼                                       │
              ┌───────────────────────────────┐  shake / noise       │
              │  camera-shake rejection        ├──────────────────►   │
              │                                │  age active tracks   │
              │  hot_frac > 1.5 % of disk      │  skip frame          │
              │  OR  blob count > limit        │                      │
              │      with no dominant blob     │                      │
              └───────────────┬───────────────┘                      │
                              │ ok                                    │
                              ▼                                       │
              ┌───────────────────────────────┐                      │
              │  blob-to-track matching        │                      │
              │                                │                      │
              │  filter: area ≥ 8 px           │                      │
              │  oversized-dominant bypass:    │                      │
              │    single blob > size cap AND  │                      │
              │    ≥ 20× larger than next      │                      │
              │    → allow (large aircraft)    │                      │
              │                                │                      │
              │  match each blob to nearest    │                      │
              │  active track by predicted     │                      │
              │  position (linear extrap.)     │                      │
              │  within 12 % of disk radius    │                      │
              │                                │                      │
              │  unmatched blobs → new tracks  │                      │
              │  gap > 4 frames → close track  │                      │
              └───────────────┬───────────────┘                      │
                              │                                       │
                              └──────────────────────────────────────┘

                       (all frames scanned)
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  3. TRACK SCORING                       (per completed track)        │
│                                                                      │
│  ① len(pts) < min_track_frames?                                      │
│       lunar 7 / solar 8                              → REJECT        │
│                                                                      │
│  ② net displacement < 5 % of disk radius?            → REJECT        │
│                                                                      │
│  ③ velocity < min_vel_pct?                                           │
│       lunar 2.0 %Ø/s / solar 3.0 %Ø/s               → REJECT        │
│                                                                      │
│  ④ fill fraction < 0.50?   (lunar only)                              │
│       fill = n_points ÷ (frame_end − frame_start + 1)               │
│       shimmer accumulates points through erratic gaps → REJECT       │
│                                                                      │
│  ⑤ R² < 0.60?   (lunar only)                                         │
│       unless ISS candidate: velocity > 40 %Ø/s AND R² > 0.95        │
│       seeing shimmer clusters below 0.60             → REJECT        │
│                                                                      │
│  ⑥ perimeter check?   (lunar only)                                   │
│       max(radial_start, radial_end) ÷ disk_radius < 0.60            │
│       blob materialises mid-disk, not at edge        → REJECT        │
│                                                                      │
│  ⑦ weighted confidence score < threshold?                            │
│       0.50 × R²  +  0.30 × vel_score  +  0.20 × dur_score          │
│       lunar 0.60 / solar 0.70                        → REJECT        │
│                                                                      │
│                          ▼  PASS                                     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  4. CLASSIFICATION                                                   │
│                                                                      │
│  velocity > 40 %Ø/s  AND  R² > 0.97   →  iss                        │
│  R² ≥ 0.90            AND  vel ≥ 3 %/s →  plane                     │
│  velocity < 8 %Ø/s                     →  bird                      │
│  otherwise                             →  unknown                   │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  5. CLIP + THUMBNAIL WRITING                                         │
│                                                                      │
│  For each event, one VideoCapture pass:                              │
│  • 5 s padding before first detection frame                          │
│  • UTC timestamp burned into every frame (top-left)                 │
│  • hero frame (track point closest to disk centre) saved in-pass    │
│  • 5 s padding after last detection frame                            │
│                                                                      │
│  ffmpeg post-process:                                                │
│  • transcode mp4v → H.264 (browser-compatible)                      │
│  • embed hero JPEG as cover-art attached picture                     │
│                                                                      │
│  JSON sidecar written alongside clip:                                │
│  label · confidence · R² · velocity · duration · track coords       │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  6. YOLO SECOND-STAGE VALIDATION   (optional — needs ultralytics)   │
│                                                                      │
│  YOLOv8n inference on the full hero-frame JPEG                       │
│  COCO classes checked:  airplane (4)  ·  bird (14)                  │
│  confidence threshold: 0.25                                          │
│                                                                      │
│  result → yolo_label / yolo_confidence stored on event              │
│  UI shows  ✓ airplane  or  ✓ bird  badge on confirmed pills         │
│  "confirmed only" toggle hides all unconfirmed events               │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  OUTPUT   list[TransitEvent]                                         │
│                                                                      │
│  per event:  label · confidence · duration_s · velocity_%Ø/s · R²   │
│              clip .mp4  ·  sidecar .json  ·  thumbnail .jpg         │
│              yolo_label · yolo_confidence  (None if unvalidated)     │
└──────────────────────────────────────────────────────────────────────┘
```

## Lunar vs solar parameter differences

| Parameter            | Solar        | Lunar        | Rationale                                      |
|----------------------|-------------|--------------|------------------------------------------------|
| `diff_thresh`        | 12           | 8            | Moon is dimmer → lower silhouette contrast     |
| `min_track_frames`   | 8            | 7            | Slightly shorter tracks tolerated              |
| `min_vel_pct`        | 3.0 %Ø/s     | 2.0 %Ø/s     | Slower objects still detectable                |
| `min_confidence`     | 0.70         | 0.60         | Relaxed to catch faint crossings               |
| `min_linearity`      | —            | 0.60         | Hard R² floor; seeing shimmer clusters < 0.60 |
| `min_fill_frac`      | —            | 0.50         | Rejects erratic gap-heavy shimmer tracks       |
| `min_perimeter_frac` | —            | 0.60         | Blob must reach 60 % of disk radius at one end |
| `shake_hot_frac`     | 1.5 %        | 4.0 %        | No sunspots → less strict on illuminated area  |
| `max_blobs_per_frame`| 5            | 10           | More lenient for lunar surface texture         |
| `max_blob_frac`      | 2 % disk     | 95 % disk    | Allows large nearby aircraft (e.g. Cessna)     |

**Drift-compensation constants** (same for solar and lunar):

| Constant      | Value   | Purpose                                                    |
|---------------|---------|------------------------------------------------------------|
| `DRIFT_CROP`  | 256 px  | Side of square disk crop used for phase correlation        |
| `DRIFT_MAX`   | 8.0 px  | Shift clamp — larger values treated as re-points, ignored  |
| `DRIFT_MIN`   | 0.3 px  | Sub-pixel noise floor — shifts below this are ignored      |
