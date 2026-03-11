"""
Seestar Lab — Flask web application backend.

════════════════════════════════════════════════════════════════════════════════
ARCHITECTURE OVERVIEW
════════════════════════════════════════════════════════════════════════════════

  Single-process Flask app (threaded=True) running on localhost:5000.
  All long-running work is delegated to daemon background threads so the
  HTTP server always stays responsive.

  Environment variables (from .env or shell):
    SEESTAR_DATA_DIR    Root of the raw data tree (default /mnt/d/xfer).
    SEESTAR_OUTPUT_DIR  Where rendered outputs are written (default /mnt/d/seestar-lab).

════════════════════════════════════════════════════════════════════════════════
SESSION SCANNER
════════════════════════════════════════════════════════════════════════════════

  On startup and on demand, scanner.Scanner walks SEESTAR_DATA_DIR looking
  for Seestar observation sessions.  A session is a directory (or group of
  files) identified by object name and observation date.

  Two scan modes:
    Full scan     — re-walks the entire tree; used on first run or when forced.
    Differential  — stat-based comparison against the DB; skips unchanged
                    directories.  Typically completes in < 1 s on subsequent runs.

  Each discovered or updated session fires an SSE event that all connected
  browser tabs receive in real time.

════════════════════════════════════════════════════════════════════════════════
SSE PUB/SUB  (/api/events)
════════════════════════════════════════════════════════════════════════════════

  The main data channel is a Server-Sent Events stream.  Each browser tab
  subscribes by opening GET /api/events, which:
    1. Creates a per-subscriber in-process queue (maxsize=500 events).
    2. Immediately replays all sessions from the SQLite DB so new tabs get
       the full current state without waiting for a rescan.
    3. Streams new events from the queue as they arrive (scan updates,
       transit progress/done, stack done, comet job progress).

  _broadcast() fans out a JSON payload to every subscriber queue.
  Slow or disconnected clients are detected by queue.Full and dropped.

════════════════════════════════════════════════════════════════════════════════
TRANSIT DETECTION  (transit_detector.py)
════════════════════════════════════════════════════════════════════════════════

  Detects objects transiting the solar or lunar disk in Seestar video files
  (MP4 / AVI).  Algorithm summary (see transit_detector.py for full detail):

  1. Temporal median background — samples N_BG_SAMPLES frames evenly and
     computes the pixel-wise median.  Sunspots and craters are stationary so
     they cancel out; a transiting satellite/ISS/plane shows up cleanly in
     the per-frame difference.

  2. Disk detection — Hough circle transform on the blurred background frame.
     Falls back to contour fitting for crescent moons where Hough struggles.

  3. Blob tracking — each frame is differenced from the background,
     thresholded, morphologically opened, and masked to the disk interior.
     Connected components are matched to active tracks by nearest-centroid
     assignment with linear-extrapolation prediction.  Camera-shake frames
     (where more than SHAKE_HOT_FRAC of the disk is lit up) are skipped
     entirely to avoid mass false positives from re-pointing events.

  4. Track scoring — tracks are scored on linearity (R²), velocity (% of disk
     diameter per second), and duration.  Tracks slower than MIN_VEL_PCT %/s
     are rejected (sunspot residuals move ~0.04 Ø/day — far below 3 %/s).

  5. YOLO validation — detected clips are optionally run through a YOLOv8
     model (yolo_validator.py) that classifies the object type (plane,
     satellite, ISS, meteor, etc.) and provides a secondary confidence score.

  6. Clip extraction — PAD_SECS of context is prepended/appended; UTC time
     (parsed from the filename) is burned onto each frame; a JSON sidecar
     with full track metadata is written alongside each MP4 clip.

  Job lifecycle:
    POST /api/transit/detect  →  enqueues jobs in _transit_queue
    _transit_worker_loop()    →  pulls jobs, dispatches to a thread pool
                                  (_TRANSIT_CONCURRENCY workers in parallel;
                                  OpenCV and NumPy release the GIL so real
                                  parallelism is achieved without multiprocessing)
    _run_transit_job()        →  optionally copies video to local temp dir for
                                  faster random-seek access, then runs the detector
    _broadcast(transit_done)  →  notifies all SSE subscribers of results

  Videos on a different filesystem device than /tmp are copied to a local
  temp file before processing; random seeks on a network or external drive
  are orders of magnitude slower than on the system SSD.

  Pause / cancel:
    POST /api/transit/pause   — sets _transit_paused; workers block at the gate.
    POST /api/transit/resume  — clears the gate; workers continue.
    POST /api/transit/cancel  — adds session_name to _cancel_sessions; the
                                 worker skips or stops that session's jobs.

════════════════════════════════════════════════════════════════════════════════
STACKING WORKER
════════════════════════════════════════════════════════════════════════════════

  A single-threaded daemon runs _stack_worker_loop(), pulling jobs from
  _stack_queue.  Each job mean-stacks a list of FITS files and writes a
  JPEG output.  Stacking is CPU-heavy and sequential to avoid memory pressure.
  Jobs are enqueued on startup to recover any interrupted sessions, and on
  demand via POST /api/stack/start.

════════════════════════════════════════════════════════════════════════════════
COMET WIZARD  (comet_processor.py)
════════════════════════════════════════════════════════════════════════════════

  The Comet Wizard is a 3-step UI (select subs → parameters → render) that
  drives comet_processor.py as a subprocess.

  POST /api/comet/render    — launches the processor subprocess and returns a
                               job_id.  Accepts:
                                 files          — list of .fit paths
                                 fps, gamma, crop, sky_pct, high_pct, noise,
                                 width          — processing parameters
                                 force_realign  — pass --no-cache to processor
                                 redetect_nucleus — pass --redetect-nucleus
                                 nucleus_hint_x/y — fractional nucleus hint

  GET  /api/comet/status/<id>  — returns {status, log, outputs} for the job.
                                  outputs includes URLs for both animations,
                                  both stack images, the track image, and
                                  individual annotated frame JPEGs.

  GET  /api/comet/preview-frame — renders a single reference frame with the
                                   current stretch parameters for the live
                                   preview pane in Step 2.

  GET  /api/comet/frame/<job_id>/<idx> — serve one annotated _frames/frame_NNNN.jpg.

  GET  /api/comet/output/<job_id>/<filename> — serve any processor output file.

  Comet jobs are tracked in the SQLite DB (comet_jobs table) so the browser
  can reconnect to an in-progress render after a page refresh.

════════════════════════════════════════════════════════════════════════════════
DATABASE  (db.py / SQLite)
════════════════════════════════════════════════════════════════════════════════

  A single SQLite file stores:
    sessions           — discovered Seestar observation sessions
    transit_jobs       — one row per video file queued for transit detection
    transit_events     — detected transiting-object events with clip paths
    impact_events      — detected lunar impact flash events
    stack_jobs         — stacking job queue and results
    comet_jobs         — comet processor job queue and results

  The DB is the source of truth for the SSE replay on new connections.

════════════════════════════════════════════════════════════════════════════════
API ROUTE SUMMARY
════════════════════════════════════════════════════════════════════════════════

  Pages
    GET  /                         Main sessions dashboard
    GET  /catalog/messier          Messier bingo-card catalog page
    GET  /catalog/caldwell         Caldwell bingo-card catalog page
    GET  /transits                 Transit detection page
    GET  /activity                 Background-activity log page
    GET  /comet                    Comet Wizard page

  Sessions / data
    GET  /api/events               SSE stream (sessions + live updates)
    GET  /api/scan                 Trigger a full rescan
    GET  /api/thumbnail/<name>     Serve a resized session preview JPEG
    GET  /api/fits/<path>          Serve a FITS file for download

  Catalog
    GET  /api/catalog/messier      Messier catalog JSON
    GET  /api/catalog/caldwell     Caldwell catalog JSON

  Transit detection
    POST /api/transit/detect       Queue transit jobs for a session
    GET  /api/transit/all          All jobs + events JSON
    GET  /api/transit/clip/<id>    Serve a transit clip MP4
    GET  /api/transit/thumb/<id>   Serve a transit event thumbnail
    POST /api/transit/pause        Pause the transit worker
    POST /api/transit/resume       Resume the transit worker
    POST /api/transit/cancel       Cancel a session's transit jobs
    POST /api/transit/confirm/<id> Mark an event as confirmed / rejected

  Stacking
    POST /api/stack/start          Queue a stacking job
    GET  /api/stack/status/<name>  Get stack job status
    GET  /api/stack/result/<name>  Serve the stacked JPEG

  Comet Wizard
    POST /api/comet/render         Launch comet processor subprocess
    GET  /api/comet/status/<id>    Poll job status + outputs
    GET  /api/comet/preview-frame  Single-frame stretch preview
    GET  /api/comet/frame/<id>/<n> Serve an annotated frame JPEG
    GET  /api/comet/output/<id>/<f> Serve any processor output file

  Utility
    POST /api/shutdown             Gracefully stop the server (os._exit after 0.3 s)
"""

import functools
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path as _Path
from typing import Generator

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from dotenv import load_dotenv

load_dotenv()

import db
from catalogs import CALDWELL, MESSIER
from object_catalog import ObjectCatalog, build_catalog_response
from scanner import Scanner

app = Flask(__name__)
DATA_DIR   = os.environ.get("SEESTAR_DATA_DIR",   "/mnt/d/xfer")
OUTPUT_DIR = os.environ.get("SEESTAR_OUTPUT_DIR", "/mnt/d/seestar-lab")

_catalog = ObjectCatalog()

# ── Pub / sub ─────────────────────────────────────────────────────────────────

_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()

def _subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=500)
    with _sub_lock:
        _subscribers.append(q)
    return q


def _unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _broadcast(event: dict) -> None:
    payload = json.dumps(event)
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


# ── Background scan ───────────────────────────────────────────────────────────

_scan_running = False
_scan_lock = threading.Lock()


def _run_scan(force: bool = False) -> None:
    global _scan_running
    scanner = Scanner(DATA_DIR)
    gen = scanner.scan_full() if force else scanner.scan_differential()
    try:
        for event in gen:
            _broadcast(event)
    except Exception as exc:
        _broadcast({"type": "error", "message": str(exc)})
    finally:
        with _scan_lock:
            _scan_running = False


def start_scan(force: bool = False) -> bool:
    """Spawn a background scan thread. Returns False if one is already running."""
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return False
        _scan_running = True
    t = threading.Thread(target=_run_scan, kwargs={"force": force}, daemon=True)
    t.start()
    return True


# ── Transit job queue ─────────────────────────────────────────────────────────

_transit_queue:       queue.Queue      = queue.Queue()
_transit_paused:      threading.Event  = threading.Event()
_transit_paused.set()   # set = running; clear = paused
_cancel_sessions:     set              = set()
_cancel_lock:         threading.Lock   = threading.Lock()
_TRANSIT_CONCURRENCY: int             = 3   # video files processed concurrently


def _is_cancelled(session_name: str) -> bool:
    with _cancel_lock:
        return session_name in _cancel_sessions


def _run_transit_job(job: dict) -> None:
    from datetime import datetime as _datetime
    from pathlib import Path as _Path
    from transit_detector import TransitDetector

    video_path   = job["video_path"]
    session_name = job["session_name"]
    basename     = _Path(video_path).name

    db.start_video_job(video_path)
    # Clear any stale events so re-detection starts with a clean slate.
    db.delete_transit_events_for_video(video_path)
    db.delete_impact_events_for_video(video_path)

    def progress_cb(pct: int, _total: int, message: str) -> None:
        db.update_video_job_progress(video_path, pct, message)
        _broadcast({
            "type":           "transit_progress",
            "session_name":   session_name,
            "video_path":     video_path,
            "video_basename": basename,
            "video_type":     job["video_type"],
            "status":         "running",
            "pct":            pct,
            "message":        message,
        })

    def cancel_cb() -> bool:
        return _is_cancelled(session_name)

    # ── Optional local-cache copy ─────────────────────────────────────────────
    # If the source video lives on a different (slower) device than the system
    # temp dir — e.g. a spinning external drive vs. the WSL2 virtual disk —
    # copy it to a local temp file first.  The detector reads the video many
    # times with random seeks; local SSD access is dramatically faster.
    # Clips are still written to output_dir on the original drive as usual.
    _tmp_video = None
    work_path  = video_path
    try:
        if os.stat(video_path).st_dev != os.stat(tempfile.gettempdir()).st_dev:
            suffix = _Path(video_path).suffix or ".mp4"
            fd, _tmp_video = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            progress_cb(1, 100, f"Caching {basename} to local disk…")
            shutil.copy2(video_path, _tmp_video)
            work_path = _tmp_video
    except OSError:
        pass  # stat or copy failed — fall back to reading from original path

    try:
        det    = TransitDetector(work_path, job["video_type"], source_path=video_path)
        events = det.detect(
            output_dir=job["output_dir"],
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
    finally:
        if _tmp_video:
            try:
                os.unlink(_tmp_video)
            except OSError:
                pass

    event_rows = []
    for ev in events:
        eid = db.insert_transit_event({
            "video_path":           video_path,
            "session_name":         session_name,
            "video_type":           job["video_type"],
            "label":                ev.label,
            "confidence":           ev.confidence,
            "frame_start":          ev.frame_start,
            "frame_end":            ev.frame_end,
            "duration_s":           ev.duration_s,
            "velocity_pct_per_sec": ev.velocity_pct_per_sec,
            "linearity":            ev.linearity,
            "clip_path":            ev.clip_path,
            "meta_path":            ev.meta_path,
            "aircraft_candidates":  None,
            "thumb_path":           ev.thumb_path,
            "yolo_label":           ev.yolo_label,
            "yolo_confidence":      ev.yolo_confidence,
        })
        event_rows.append({
            "id":                   eid,
            "video_path":           video_path,
            "label":                ev.label,
            "confidence":           ev.confidence,
            "duration_s":           ev.duration_s,
            "velocity_pct_per_sec": ev.velocity_pct_per_sec,
            "clip_path":            ev.clip_path,
            "thumb_path":           ev.thumb_path,
            "yolo_label":           ev.yolo_label,
            "yolo_confidence":      ev.yolo_confidence,
        })

    db.finish_video_job(video_path)
    _broadcast({
        "type":           "transit_done",
        "session_name":   session_name,
        "video_path":     video_path,
        "video_basename": basename,
        "video_type":     job["video_type"],
        "events":         event_rows,
        "total_events":   len(event_rows),
    })


def _transit_worker_loop() -> None:
    """
    Pull video jobs from the queue and dispatch them to a small thread pool so
    _TRANSIT_CONCURRENCY files are processed concurrently.

    OpenCV decode and NumPy both release the GIL, so threading achieves real
    parallelism without multiprocessing overhead.
    """
    sema = threading.Semaphore(_TRANSIT_CONCURRENCY)

    def _broadcast_status(job: dict, status: str, message: str) -> None:
        _broadcast({
            "type":           "transit_progress",
            "session_name":   job["session_name"],
            "video_path":     job["video_path"],
            "video_basename": job["video_path"].rsplit("/", 1)[-1],
            "video_type":     job.get("video_type"),
            "status":         status,
            "message":        message,
            "pct":            0,
        })

    def _run_one(job: dict) -> None:
        try:
            _run_transit_job(job)
        except RuntimeError as exc:
            if "cancelled" in str(exc).lower():
                db.cancel_video_jobs(job["session_name"])
                _broadcast_status(job, "cancelled", "Cancelled")
            else:
                db.fail_video_job(job["video_path"], str(exc))
                _broadcast_status(job, "error", str(exc))
        except Exception as exc:
            db.fail_video_job(job["video_path"], str(exc))
            _broadcast_status(job, "error", str(exc))
        finally:
            _transit_queue.task_done()
            sema.release()

    while True:
        job = _transit_queue.get()

        # ── Pause gate ────────────────────────────────────────────────────────
        _transit_paused.wait()

        # ── Cancellation check ────────────────────────────────────────────────
        if _is_cancelled(job["session_name"]):
            db.cancel_video_jobs(job["session_name"])
            _broadcast_status(job, "cancelled", "Cancelled")
            _transit_queue.task_done()
            continue

        # Acquire a worker slot (blocks if _TRANSIT_CONCURRENCY jobs are
        # already running), then hand the job to a background thread so the
        # main loop can immediately dequeue the next job.
        sema.acquire()
        threading.Thread(target=_run_one, args=(job,), daemon=True).start()


# ── Stack job queue ───────────────────────────────────────────────────────────

_stack_queue: queue.Queue = queue.Queue()


def _run_stack_job(job: dict) -> None:
    from stack_processor import StackProcessor, StackCancelled

    session_name = job["session_name"]
    fits_files   = job["fits_files"]
    output_path  = job["output_path"]

    db.start_stack_job(session_name)

    def progress_cb(pct: int, stage: str, frames_accepted: int, frames_total: int) -> None:
        db.update_stack_job_progress(session_name, pct, stage, frames_accepted, frames_total)
        _broadcast({
            "type":            "stack_progress",
            "session_name":    session_name,
            "pct":             pct,
            "stage":           stage,
            "frames_accepted": frames_accepted,
            "frames_total":    frames_total,
            "status":          "running",
        })

    try:
        result = StackProcessor().run(fits_files, output_path, progress_cb)
        db.finish_stack_job(
            session_name, output_path,
            result["frames_accepted"], result["frames_total"],
        )
        # Update the session thumbnail directly so the card refreshes without a rescan
        sessions_list = db.get_all_sessions()
        session = next((s for s in sessions_list if s["object_name"] == session_name), None)
        if session:
            session["thumbnail"] = output_path
            db.upsert_session(session)
            _broadcast({"type": "session", "data": session})
        _broadcast({
            "type":            "stack_done",
            "session_name":    session_name,
            "output_path":     output_path,
            "frames_accepted": result["frames_accepted"],
            "frames_total":    result["frames_total"],
        })
    except StackCancelled:
        db.fail_stack_job(session_name, "Cancelled")
        _broadcast({
            "type":         "stack_progress",
            "session_name": session_name,
            "pct":          0,
            "stage":        "Cancelled",
            "status":       "error",
            "frames_accepted": 0,
            "frames_total":    len(fits_files),
        })
    except Exception as exc:
        db.fail_stack_job(session_name, str(exc))
        _broadcast({
            "type":         "stack_progress",
            "session_name": session_name,
            "pct":          0,
            "stage":        str(exc),
            "status":       "error",
            "frames_accepted": 0,
            "frames_total":    len(fits_files),
        })


def _stack_worker_loop() -> None:
    """Single background thread: process stack jobs one at a time."""
    while True:
        job = _stack_queue.get()
        try:
            _run_stack_job(job)
        finally:
            _stack_queue.task_done()


# ── SSE endpoint ──────────────────────────────────────────────────────────────

@app.route("/api/events")
def api_events() -> Response:
    q = _subscribe()

    def stream() -> Generator[str, None, None]:
        # 1. Immediately replay every session already in the DB —
        #    the browser gets a full view before the scan even starts.
        sessions = db.get_all_sessions()
        for s in sessions:
            if s.get("object_type") == "comet":
                _enrich_comet_session(s)
            yield _sse({"type": "session", "data": s})
        yield _sse({"type": "db_loaded", "count": len(sessions)})

        # 2. Relay live broadcast events indefinitely.
        #    Heartbeat comments keep the connection alive through proxies.
        while True:
            try:
                payload = q.get(timeout=20)
                yield f"data: {payload}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"

    def on_close() -> None:
        _unsubscribe(q)

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.call_on_close(on_close)
    return resp


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    last_scan = db.get_meta("last_scan")
    return render_template("index.html", data_dir=DATA_DIR, last_scan=last_scan)


@app.route("/catalog/messier")
def catalog_messier() -> str:
    return render_template(
        "catalog.html",
        catalog_type="messier",
        catalog_title="Messier Catalog",
        catalog_total=len(MESSIER),
        data_dir=DATA_DIR,
    )


@app.route("/catalog/caldwell")
def catalog_caldwell() -> str:
    return render_template(
        "catalog.html",
        catalog_type="caldwell",
        catalog_title="Caldwell Catalog",
        catalog_total=len(CALDWELL),
        data_dir=DATA_DIR,
    )


def _enrich_comet_session(s: dict) -> None:
    """Attach animation paths and image files to a comet session dict in-place."""
    animations: dict = {}
    # Check each path, its parent, and DATA_DIR/object_name.
    # The wizard writes outputs into the directory the user selected, which may
    # be the parent of the leaf FITS paths stored by the scanner.
    dirs_to_check: list[str] = []
    seen_dirs: set[str] = set()

    def _add(d: str) -> None:
        d = d.rstrip("/\\")
        if d and d not in seen_dirs:
            seen_dirs.add(d)
            dirs_to_check.append(d)

    for p in (s.get("paths") or []):
        _add(p)
        _add(os.path.dirname(p.rstrip("/\\")))
    _add(os.path.join(str(DATA_DIR), s["object_name"]))

    anim_dir: str = ""
    for p in dirs_to_check:
        for fname, key in [
            ("comet_stars_fixed.mp4",   "stars_mp4"),
            ("comet_nucleus_fixed.mp4", "nucleus_mp4"),
            ("comet_track.jpg",         "track_jpg"),
        ]:
            if key not in animations and os.path.isfile(os.path.join(p, fname)):
                animations[key] = os.path.join(p, fname)
                if not anim_dir:
                    anim_dir = p
    animations["anim_dir"] = anim_dir
    s["animations"] = animations

    # Backfill thumbnail for _sub sessions processed after the initial scan.
    if s["object_name"].endswith("_sub") and not s.get("thumbnail"):
        for jpg_key in ("stack_jpg", "track_jpg"):
            if animations.get(jpg_key):
                s["image_files"] = [animations[jpg_key]]
                break

    # Gallery images for stacked (non-_sub) comet sessions.
    if not s["object_name"].endswith("_sub"):
        _WIZARD_OUTPUTS = {
            "comet_stars_fixed.mp4", "comet_nucleus_fixed.mp4",
            "comet_track.jpg", "comet_stack.jpg",
        }
        img_exts  = {".jpg", ".jpeg", ".png"}
        seen_imgs: set[str] = set()
        imgs: list[str] = []
        for p in dirs_to_check:
            try:
                for fname in sorted(os.listdir(p)):
                    if fname in _WIZARD_OUTPUTS:
                        continue
                    if _Path(fname).suffix.lower() not in img_exts:
                        continue
                    full = os.path.join(p, fname)
                    if full not in seen_imgs and os.path.isfile(full):
                        seen_imgs.add(full)
                        imgs.append(full)
            except OSError:
                pass
        s["image_files"] = imgs


@app.route("/api/sessions")
def api_sessions():
    sessions = db.get_all_sessions()
    for s in sessions:
        if s.get("object_type") == "comet":
            _enrich_comet_session(s)
    return jsonify(sessions)


@app.route("/api/catalog/<catalog_type>")
def api_catalog(catalog_type: str):
    if catalog_type not in ("messier", "caldwell"):
        abort(404)
    catalog = MESSIER if catalog_type == "messier" else CALDWELL
    sessions = db.get_all_sessions()
    # Build lookup keyed by squished-lower object_name for fast matching
    session_map = {
        re.sub(r"\s+", "", s["object_name"]).lower(): s
        for s in sessions
    }
    result = build_catalog_response(catalog, catalog_type, session_map)
    return jsonify(result)


@app.route("/transits")
def transits() -> str:
    return render_template("transits.html", data_dir=DATA_DIR)


@app.route("/api/transit/gallery")
def api_transit_gallery():
    return jsonify(db.get_transit_gallery())


@app.route("/api/thumbnail/<path:object_name>")
def api_thumbnail(object_name: str):
    """
    Serve a resized JPEG thumbnail for the given object.
    Falls back to a 404 if no thumbnail exists or Pillow is unavailable.
    """
    sessions = db.get_all_sessions()
    session = next(
        (s for s in sessions if s["object_name"] == object_name), None
    )
    if not session or not session.get("thumbnail"):
        abort(404)

    thumb_path = session["thumbnail"]
    if not os.path.isfile(thumb_path):
        abort(404)

    try:
        from PIL import Image
        img = Image.open(thumb_path)
        img.thumbnail((600, 600))
        # Convert to RGB if needed (TIFF/RGBA can't be JPEG)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception:
        # Serve the file directly if Pillow fails
        return send_file(thumb_path)


@app.route("/api/image")
def api_image():
    """Serve an arbitrary image file by absolute path (local tool only)."""
    path = request.args.get("path", "").strip()
    if not path or not os.path.isfile(path):
        abort(404)
    ext = Path(path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
        abort(400)
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((1400, 1400))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")
    except Exception:
        return send_file(path)


@app.route("/api/transit/detect", methods=["POST"])
def api_transit_detect():
    """
    Queue transit detection for every solar/lunar video in a session.
    Body: {"session_name": str, "force": bool}
    """
    from pathlib import Path as _Path
    body         = request.get_json(silent=True) or {}
    session_name = body.get("session_name", "").strip()
    force        = bool(body.get("force", False))

    if not session_name:
        return jsonify({"error": "session_name required"}), 400

    sessions = db.get_all_sessions()
    session  = next((s for s in sessions if s["object_name"] == session_name), None)
    if not session:
        return jsonify({"error": "session not found"}), 404

    video_type = session.get("object_type")  # 'solar' | 'lunar'
    if video_type not in ("solar", "lunar"):
        return jsonify({"error": "session is not solar or lunar"}), 400

    VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv"}
    queued = []
    for dir_path in session.get("paths", []):
        try:
            entries = os.listdir(dir_path)
        except OSError:
            continue
        for fname in sorted(entries):
            if fname.startswith('.'):   # skip macOS ._* resource forks
                continue
            if _Path(fname).suffix.lower() in VIDEO_EXT:
                vpath = os.path.join(dir_path, fname)
                if db.queue_video_job(vpath, session_name, video_type, OUTPUT_DIR, force=force):
                    if force:
                        # Delete events immediately so a page refresh shows a
                        # clean slate while the job is still pending in the queue.
                        db.delete_transit_events_for_video(vpath)
                    queued.append(vpath)
                    _transit_queue.put({
                        "video_path":   vpath,
                        "session_name": session_name,
                        "video_type":   video_type,
                        "output_dir":   OUTPUT_DIR,
                    })

    return jsonify({"queued": len(queued), "videos": queued})


@app.route("/api/transit/all")
def api_transit_all():
    summary = db.get_transit_summary()
    # Null out clip/thumb paths for files that have been deleted from disk
    # so the UI doesn't show dead links.
    for sn_data in summary.values():
        for ev in sn_data.get("events", []):
            if ev.get("clip_path") and not os.path.exists(ev["clip_path"]):
                ev["clip_path"] = None
            if ev.get("thumb_path") and not os.path.exists(ev["thumb_path"]):
                ev["thumb_path"] = None
    return jsonify(summary)


@app.route("/api/transit/running")
def api_transit_running():
    """Return the set of video_types (solar/lunar) with pending or running jobs."""
    jobs = db.get_pending_jobs()
    types = list({j["video_type"] for j in jobs if j.get("video_type")})
    return jsonify({"types": types})


@app.route("/api/transit/clip/<int:event_id>")
def api_transit_clip(event_id: int):
    ev = db.get_transit_event(event_id)
    if not ev or not ev.get("clip_path"):
        abort(404)
    clip = ev["clip_path"]
    if not os.path.isfile(clip):
        abort(404)
    return send_file(clip, mimetype="video/mp4")


@app.route("/api/transit/thumb/<int:event_id>")
def api_transit_thumb(event_id: int):
    ev = db.get_transit_event(event_id)
    if not ev or not ev.get("thumb_path"):
        abort(404)
    thumb = ev["thumb_path"]
    if not os.path.isfile(thumb):
        abort(404)
    return send_file(thumb, mimetype="image/jpeg")


@app.route("/api/transit/pause", methods=["POST"])
def api_transit_pause():
    _transit_paused.clear()   # clear = paused
    _broadcast({"type": "transit_queue_state", "paused": True})
    return jsonify({"status": "paused"})


@app.route("/api/transit/resume", methods=["POST"])
def api_transit_resume():
    _transit_paused.set()     # set = running
    _broadcast({"type": "transit_queue_state", "paused": False})
    return jsonify({"status": "running"})


@app.route("/api/transit/cancel", methods=["POST"])
def api_transit_cancel():
    body         = request.get_json(silent=True) or {}
    session_name = body.get("session_name")
    cancel_all   = bool(body.get("all", False))

    with _cancel_lock:
        if cancel_all:
            # Mark every active session in the queue as cancelled
            pending = db.get_pending_jobs()
            for j in pending:
                _cancel_sessions.add(j["session_name"])
        elif session_name:
            _cancel_sessions.add(session_name)
        else:
            return jsonify({"error": "session_name or all=true required"}), 400

    # Mark DB rows immediately so the UI updates even before the worker loops
    cancelled = db.cancel_video_jobs(session_name if not cancel_all else None)

    # Wake the worker if paused so it can drain the cancelled jobs
    _transit_paused.set()

    _broadcast({"type": "transit_queue_state", "paused": False,
                "cancelled_session": session_name, "cancel_all": cancel_all})
    return jsonify({"cancelled": cancelled})


@app.route("/api/stack/start", methods=["POST"])
def api_stack_start():
    """
    Queue a stacking job for a _sub session.
    Body: {"session_name": str, "force": bool}
    """
    from pathlib import Path as _Path
    body         = request.get_json(silent=True) or {}
    session_name = body.get("session_name", "").strip()
    force        = bool(body.get("force", False))

    if not session_name:
        return jsonify({"error": "session_name required"}), 400

    sessions_list = db.get_all_sessions()
    session = next((s for s in sessions_list if s["object_name"] == session_name), None)
    if not session:
        return jsonify({"error": "session not found"}), 404

    # Collect FITS files from every directory belonging to this session
    FITS_EXT = {".fit", ".fits", ".fts"}
    fits_files: list[str] = []
    for dir_path in session.get("paths", []):
        try:
            for fname in sorted(os.listdir(dir_path)):
                if fname.startswith("."):
                    continue
                if _Path(fname).suffix.lower() in FITS_EXT:
                    fits_files.append(os.path.join(dir_path, fname))
        except OSError:
            pass

    if not fits_files:
        return jsonify({"error": "no FITS files found in session"}), 400

    # Output path: write into the sub directory so scanner picks it up as thumbnail
    output_dir  = session["paths"][0] if session.get("paths") else OUTPUT_DIR
    output_path = os.path.join(output_dir, "seestar_stacked.jpg")

    if not db.queue_stack_job(session_name, force=force):
        return jsonify({"error": "job already queued or running", "status": "already_queued"}), 409

    _stack_queue.put({
        "session_name": session_name,
        "fits_files":   fits_files,
        "output_path":  output_path,
    })
    return jsonify({"status": "queued", "fits_count": len(fits_files), "output": output_path})


@app.route("/api/stack/status")
def api_stack_status():
    """Return all stack job statuses keyed by session_name."""
    return jsonify(db.get_all_stack_jobs())


@app.route("/api/stack/image/<path:session_name>")
def api_stack_image(session_name: str):
    """Serve the full-size stacked JPEG for a session."""
    job = db.get_stack_job(session_name)
    if not job or not job.get("output_path"):
        abort(404)
    path = job["output_path"]
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/impacts")
def impacts() -> str:
    return render_template("impacts.html", data_dir=DATA_DIR)


@app.route("/api/impacts")
def api_impacts():
    gallery = db.get_impact_gallery()
    # Null out paths for files that have been deleted from disk
    for ev in gallery:
        if ev.get("clip_path") and not os.path.exists(ev["clip_path"]):
            ev["clip_path"] = None
        if ev.get("thumb_path") and not os.path.exists(ev["thumb_path"]):
            ev["thumb_path"] = None
    return jsonify(gallery)


@app.route("/api/impact/clip/<int:event_id>")
def api_impact_clip(event_id: int):
    ev = db.get_impact_event(event_id)
    if not ev or not ev.get("clip_path"):
        abort(404)
    clip = ev["clip_path"]
    if not os.path.isfile(clip):
        abort(404)
    return send_file(clip, mimetype="video/mp4")


@app.route("/api/impact/thumb/<int:event_id>")
def api_impact_thumb(event_id: int):
    ev = db.get_impact_event(event_id)
    if not ev or not ev.get("thumb_path"):
        abort(404)
    thumb = ev["thumb_path"]
    if not os.path.isfile(thumb):
        abort(404)
    return send_file(thumb, mimetype="image/jpeg")


@app.route("/activity")
def activity() -> str:
    return render_template("activity.html", data_dir=DATA_DIR)


@app.route("/api/activity")
def api_activity():
    import calendar
    from datetime import date as _date

    sessions  = db.get_all_sessions()
    days: dict = {}

    for s in sessions:
        dates    = s.get("dates", [])
        if not dates:
            continue
        n        = max(len(dates), 1)
        subs_d   = s.get("num_subs", 0) / n
        video_d  = s.get("total_video_duration", 0) / n
        otype    = s.get("object_type") or "unknown"
        for d in dates:
            if d not in days:
                days[d] = {"sessions": 0, "subs": 0.0, "video_s": 0.0, "types": set()}
            days[d]["sessions"] += 1
            days[d]["subs"]     += subs_d
            days[d]["video_s"]  += video_d
            days[d]["types"].add(otype)

    days_json = {
        date: {
            "sessions": v["sessions"],
            "subs":     int(round(v["subs"])),
            "video_s":  int(round(v["video_s"])),
            "types":    sorted(v["types"]),
        }
        for date, v in days.items()
    }

    sorted_dates = sorted(days_json)
    total_days   = len(sorted_dates)
    total_subs   = sum(v["subs"]    for v in days_json.values())
    total_video_s = sum(v["video_s"] for v in days_json.values())

    # Longest streak of consecutive observation nights
    max_streak = streak = 0
    prev = None
    for d in sorted_dates:
        cur = _date.fromisoformat(d)
        streak = (streak + 1) if (prev and (cur - prev).days == 1) else 1
        max_streak = max(max_streak, streak)
        prev = cur

    # Busiest month by sub count
    month_subs: dict = {}
    for d, v in days_json.items():
        month_subs[d[:7]] = month_subs.get(d[:7], 0) + v["subs"]
    busiest = max(month_subs, key=month_subs.get) if month_subs else None
    if busiest:
        y, m       = busiest.split("-")
        busiest_lbl = f"{calendar.month_abbr[int(m)]} {y}"
    else:
        busiest_lbl = "—"

    return jsonify({
        "days": days_json,
        "stats": {
            "total_days":     total_days,
            "total_subs":     total_subs,
            "total_video_s":  total_video_s,
            "longest_streak": max_streak,
            "busiest_month":  busiest_lbl,
        },
    })


# ── Comet wizard ──────────────────────────────────────────────────────────────

_COMET_THUMB_DIR = os.path.join(tempfile.gettempdir(), "seestar_comet_thumbs")
os.makedirs(_COMET_THUMB_DIR, exist_ok=True)

_comet_jobs: dict = {}
_comet_jobs_lock  = threading.Lock()


def _comet_thumb_bytes(fits_path: str, width: int = 240) -> bytes:
    """Render a FITS sub to a small JPEG; returns raw bytes."""
    import cv2
    import numpy as np
    from astropy.io import fits as _fits

    _BAYER = {
        "BGGR": cv2.COLOR_BayerBG2RGB, "GBRG": cv2.COLOR_BayerGB2RGB,
        "GRBG": cv2.COLOR_BayerGR2RGB, "RGGB": cv2.COLOR_BayerRG2RGB,
    }
    with _fits.open(fits_path, memmap=False) as hdul:
        hdr = hdul[0].header
        raw = hdul[0].data
    bscale = float(hdr.get("BSCALE", 1))
    bzero  = float(hdr.get("BZERO",  0))
    bayer  = hdr.get("BAYERPAT", "")

    if raw.ndim == 2 and bayer:
        code = _BAYER.get(bayer.upper(), cv2.COLOR_BayerGR2RGB)
        rgb  = cv2.cvtColor(raw.astype(np.uint16), code).astype(np.float32) * bscale + bzero
    elif raw.ndim == 3 and raw.shape[0] == 3:
        rgb = np.transpose(raw, (1, 2, 0)).astype(np.float32) * bscale + bzero
    else:
        mono = raw.astype(np.float32) * bscale + bzero
        rgb  = np.stack([mono, mono, mono], axis=2)

    lum  = 0.299*rgb[...,0] + 0.587*rgb[...,1] + 0.114*rgb[...,2]
    sky  = np.percentile(lum, 25)
    rgb  = rgb - sky
    hi   = np.percentile(lum[lum > sky] - sky, 99.5) if np.any(lum > sky) else 1.0
    rgb  = np.power(np.clip(rgb / max(hi, 1e-9), 0, 1), 0.5)

    h_px, w_px = rgb.shape[:2]
    th = int(h_px * width / w_px)
    bgr = cv2.cvtColor((rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    bgr = cv2.resize(bgr, (width, th), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return bytes(buf)


def _comet_output_exists(directory: str, name: str):
    p = os.path.join(directory, name)
    return p if os.path.isfile(p) else None


def _run_comet_job(job_id: str, cmd: list, out_dir: str) -> None:
    _PASS_RE = re.compile(r"\[Pass\s+(\d+)\]")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        with _comet_jobs_lock:
            job = _comet_jobs[job_id]
            job["log"].append(line)
            if len(job["log"]) > 500:
                job["log"].pop(0)
            job["message"] = line
            m = _PASS_RE.search(line)
            if m:
                job["pct"] = min(int((int(m.group(1)) - 1) / 5 * 100), 95)
    proc.wait()
    with _comet_jobs_lock:
        job = _comet_jobs[job_id]
        if proc.returncode == 0:
            frames_dir  = os.path.join(out_dir, "_frames")
            frame_count = 0
            if os.path.isdir(frames_dir):
                frame_count = sum(1 for f in os.listdir(frames_dir)
                                  if f.lower().endswith(".jpg"))
            job["status"]  = "done"
            job["pct"]     = 100
            job["outputs"] = {
                "stars_mp4":         _comet_output_exists(out_dir, "comet_stars_fixed.mp4"),
                "nucleus_mp4":       _comet_output_exists(out_dir, "comet_nucleus_fixed.mp4"),
                "track_jpg":         _comet_output_exists(out_dir, "comet_track.jpg"),
                "stack_jpg":         _comet_output_exists(out_dir, "comet_stack.jpg"),
                "nucleus_stack_jpg": _comet_output_exists(out_dir, "comet_nucleus_stack.jpg"),
                "ls_jpg":            _comet_output_exists(out_dir, "comet_ls.jpg"),
                "portrait_jpg":      _comet_output_exists(out_dir, "comet_portrait.jpg"),
                "frame_count":       frame_count,
                "frame_dir":         out_dir if frame_count > 0 else None,
            }
        else:
            job["status"] = "error"
            job["error"]  = f"Process exited {proc.returncode}"


_comet_info_cache: dict = {}


@app.route("/api/comet/info")
def api_comet_info():
    """Fetch official designation + orbit class from JPL SBDB for a comet directory name."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({}), 400
    if name in _comet_info_cache:
        return jsonify(_comet_info_cache[name])

    # Strip common directory suffixes to get a bare designation
    clean = re.sub(
        r'[\s_]+(sub|processed|raw|stack|stacked|final|test|backup|archive)\b.*$',
        '', name, flags=re.IGNORECASE,
    ).strip()
    # Normalise C-YYYY → C/YYYY for JPL query
    designation = re.sub(r'^C-(\d{4})', r'C/\1', clean)

    result = {"fullname": None, "orbit_class": None, "designation": designation}
    try:
        import urllib.request as _urlreq
        import json as _json
        url = ("https://ssd-api.jpl.nasa.gov/sbdb.api"
               f"?sstr={urllib.parse.quote(designation)}&full-prec=false")
        with _urlreq.urlopen(url, timeout=6) as r:
            data = _json.loads(r.read())
        obj = data.get("object", {})
        result["fullname"]    = obj.get("fullname")
        result["orbit_class"] = obj.get("orbit_class", {}).get("name")
    except Exception:
        pass  # Offline or unknown comet — just return empty fields

    _comet_info_cache[name] = result
    return jsonify(result)


@app.route("/api/comet/discover")
def api_comet_discover():
    """Scan one directory level for comet-named subdirectories."""
    root = request.args.get("root", DATA_DIR).strip()
    if not os.path.isdir(root):
        return jsonify({"error": f"Directory not found: {root}"}), 400

    import re
    # Match: C-YYYY…  (non-periodic)  or  NNP / NND… (periodic/defunct)
    _COMET_RE = re.compile(r'C-\d{4}|\b\d+[PD]\b', re.IGNORECASE)

    results = []
    try:
        for name in sorted(os.listdir(root)):
            if not _COMET_RE.search(name):
                continue
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            try:
                fits_count = sum(
                    1 for f in os.listdir(full)
                    if f.lower().endswith((".fit", ".fits"))
                )
            except OSError:
                fits_count = 0
            results.append({"name": name, "path": full, "fits_count": fits_count})
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"root": root, "comets": results})


@app.route("/api/comet/preview-frame", methods=["POST"])
def api_comet_preview_frame():
    """Render a FITS frame with custom stretch/noise params — returns JPEG for live preview."""
    import cv2 as _cv2
    import numpy as _np

    body     = request.get_json(silent=True) or {}
    path     = body.get("path", "")
    if not path or not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    sky_pct  = max(1.0,  min(50.0,  float(body.get("sky_pct",  25.0))))
    high_pct = max(90.0, min(100.0, float(body.get("high_pct", 99.8))))
    gamma    = max(0.1,  min(3.0,   float(body.get("gamma",    0.5))))
    noise    = max(0,    min(5,     int(body.get("noise",  0))))
    width    = max(200,  min(900,   int(body.get("width",  640))))

    try:
        from astropy.io import fits as _fits
        _BAYER = {
            "BGGR": _cv2.COLOR_BayerBG2RGB, "GBRG": _cv2.COLOR_BayerGB2RGB,
            "GRBG": _cv2.COLOR_BayerGR2RGB, "RGGB": _cv2.COLOR_BayerRG2RGB,
        }
        with _fits.open(path, memmap=False) as hdul:
            hdr = hdul[0].header
            raw = hdul[0].data
        bscale = float(hdr.get("BSCALE", 1))
        bzero  = float(hdr.get("BZERO",  0))
        bayer  = hdr.get("BAYERPAT", "")

        if raw.ndim == 2 and bayer:
            code = _BAYER.get(bayer.upper(), _cv2.COLOR_BayerGR2RGB)
            rgb  = _cv2.cvtColor(raw.astype(_np.uint16), code).astype(_np.float32) * bscale + bzero
        elif raw.ndim == 3 and raw.shape[0] == 3:
            rgb = _np.transpose(raw, (1, 2, 0)).astype(_np.float32) * bscale + bzero
        else:
            mono = raw.astype(_np.float32) * bscale + bzero
            rgb  = _np.stack([mono, mono, mono], axis=2)

        # Stretch
        out = rgb.copy()
        for c in range(3):
            sky = _np.percentile(out[..., c], sky_pct)
            out[..., c] = out[..., c] - sky
        lum      = 0.299*out[..., 0] + 0.587*out[..., 1] + 0.114*out[..., 2]
        high_val = _np.percentile(lum[lum > 0], high_pct) if _np.any(lum > 0) else 1.0
        if high_val > 0:
            out = out / high_val
        out = _np.power(_np.clip(out, 0, 1), gamma)
        out = _np.clip(out, 0, 1)

        # Resize
        h_px, w_px = out.shape[:2]
        th  = int(h_px * width / w_px)
        bgr = _cv2.cvtColor((out * 255).astype(_np.uint8), _cv2.COLOR_RGB2BGR)
        bgr = _cv2.resize(bgr, (width, th), interpolation=_cv2.INTER_LANCZOS4)

        # Noise reduction
        if noise > 0:
            sigma = float(10 + noise * 10)
            bgr = _cv2.bilateralFilter(bgr, 9, sigmaColor=sigma, sigmaSpace=sigma)

        ok, buf = _cv2.imencode(".jpg", bgr, [_cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return Response(bytes(buf), mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/comet")
def comet_wizard() -> str:
    return render_template("comet.html", data_dir=DATA_DIR)


@app.route("/comet/results")
def comet_results() -> str:
    return render_template("comet_results.html")


@app.route("/api/comet/scan", methods=["POST"])
def api_comet_scan():
    body      = request.get_json(silent=True) or {}
    directory = body.get("directory", "").strip()
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": f"Directory not found: {directory}"}), 400

    from astropy.io import fits as _fits

    files = []
    for f in sorted(_Path(directory).glob("*.fit")):
        try:
            with _fits.open(str(f), memmap=False) as hdul:
                hdr = hdul[0].header
            stem  = f.stem
            parts = stem.split("_")
            nsubs = 1
            if parts[0].lower() == "stacked":
                try:
                    nsubs = int(parts[1])
                except (IndexError, ValueError):
                    pass
            files.append({
                "path":      str(f),
                "filename":  f.name,
                "date_obs":  hdr.get("DATE-OBS", ""),
                "exptime":   float(hdr.get("EXPTIME", 0)),
                "nsubs":     nsubs,
                "thumb_url": f"/api/comet/thumb?path={urllib.parse.quote(str(f))}",
            })
        except Exception:
            continue

    files.sort(key=lambda x: x["date_obs"])
    return jsonify({"directory": directory, "count": len(files), "files": files})


@app.route("/api/comet/thumb")
def api_comet_thumb():
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path):
        abort(404)
    width = min(int(request.args.get("width", 240)), 800)
    key   = hashlib.md5(f"{path}:{width}".encode()).hexdigest()
    cache = os.path.join(_COMET_THUMB_DIR, key + ".jpg")
    if not os.path.isfile(cache):
        try:
            data = _comet_thumb_bytes(path, width=width)
            with open(cache, "wb") as fh:
                fh.write(data)
        except Exception as e:
            return str(e), 500
    return send_file(cache, mimetype="image/jpeg")


@app.route("/api/comet/render", methods=["POST"])
def api_comet_render():
    body      = request.get_json(silent=True) or {}
    directory = body.get("directory", "").strip()
    sel_files = body.get("files", [])
    if not directory or not sel_files:
        return jsonify({"error": "directory and files required"}), 400

    job_id = hashlib.md5(f"{directory}:{datetime.utcnow().isoformat()}".encode()).hexdigest()[:12]
    with _comet_jobs_lock:
        _comet_jobs[job_id] = {
            "id": job_id, "status": "running", "pct": 0,
            "message": "Starting…", "log": [], "error": None, "outputs": {},
        }

    cmd = [
        sys.executable, "-u",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "comet_processor.py"),
        directory,
        "--files-json", json.dumps(sel_files),
        "--fps",        str(body.get("fps",        10)),
        "--gamma",      str(body.get("gamma",       0.5)),
        "--crop",       str(body.get("crop",        700)),
        "--max-frames", str(body.get("max_frames",  300)),
        "--sky-pct",    str(body.get("sky_pct",     25.0)),
        "--high-pct",   str(body.get("high_pct",    99.8)),
        "--noise",        str(body.get("noise",         0)),
        "--width",        str(body.get("width",         1080)),
        "--max-gap-mult", str(body.get("max_gap_mult",  4.0)),
    ]
    if body.get("no_vfr"):
        cmd.append("--no-vfr")
    if body.get("no_cache"):
        cmd.append("--no-cache")
    if body.get("redetect_nucleus"):
        cmd.append("--redetect-nucleus")
    hx = body.get("nucleus_hint_x")
    hy = body.get("nucleus_hint_y")
    if hx is not None and hy is not None:
        try:
            hx_f, hy_f = float(hx), float(hy)
            if 0.0 <= hx_f <= 1.0 and 0.0 <= hy_f <= 1.0:
                cmd += ["--nucleus-hint-x", str(hx_f),
                        "--nucleus-hint-y", str(hy_f)]
        except (TypeError, ValueError):
            pass

    threading.Thread(target=_run_comet_job, args=(job_id, cmd, directory),
                     daemon=True, name=f"comet-{job_id}").start()
    return jsonify({"job_id": job_id})


@app.route("/api/comet/status")
def api_comet_status():
    job_id = request.args.get("job_id", "")
    with _comet_jobs_lock:
        job = _comet_jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(job))


@app.route("/api/comet/output")
def api_comet_output():
    """Serve a comet output file (mp4 / jpg) by absolute path."""
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path):
        abort(404)
    ext  = os.path.splitext(path)[1].lower()
    mime = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext)
    if not mime:
        abort(400)
    return send_file(path, mimetype=mime, conditional=True)


@app.route("/api/comet/check")
def api_comet_check():
    """Check whether comet outputs already exist for a given directory."""
    directory = request.args.get("dir", "").strip()
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "Directory not found"}), 400
    frames_dir  = os.path.join(directory, "_frames")
    frame_count = 0
    if os.path.isdir(frames_dir):
        frame_count = sum(1 for f in os.listdir(frames_dir)
                          if f.lower().endswith(".jpg"))
    outputs = {
        "stars_mp4":         _comet_output_exists(directory, "comet_stars_fixed.mp4"),
        "nucleus_mp4":       _comet_output_exists(directory, "comet_nucleus_fixed.mp4"),
        "track_jpg":         _comet_output_exists(directory, "comet_track.jpg"),
        "stack_jpg":         _comet_output_exists(directory, "comet_stack.jpg"),
        "nucleus_stack_jpg": _comet_output_exists(directory, "comet_nucleus_stack.jpg"),
        "ls_jpg":            _comet_output_exists(directory, "comet_ls.jpg"),
        "portrait_jpg":      _comet_output_exists(directory, "comet_portrait.jpg"),
        "frame_count":       frame_count,
        "frame_dir":         directory if frame_count > 0 else None,
    }
    return jsonify({"directory": directory, "outputs": outputs})


@app.route("/api/comet/frames")
def api_comet_frames():
    """List annotated frame JPEGs saved in {dir}/_frames/ with basic metadata."""
    directory = request.args.get("dir", "").strip()
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "Directory not found"}), 400
    frames_dir = os.path.join(directory, "_frames")
    if not os.path.isdir(frames_dir):
        return jsonify({"frames": []}), 200
    frames = []
    for fname in sorted(os.listdir(frames_dir)):
        if fname.lower().endswith(".jpg"):
            frames.append({
                "path": os.path.join(frames_dir, fname),
                "name": fname,
            })
    return jsonify({"directory": directory, "frames": frames})


@app.route("/api/comet/rejections")
def api_comet_rejections():
    """Return the rejected_indices list stored in comet_alignment.json."""
    directory = request.args.get("dir", "").strip()
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "Directory not found"}), 400
    cache_json = os.path.join(directory, "comet_alignment.json")
    rejected_indices = []
    if os.path.isfile(cache_json):
        try:
            with open(cache_json) as fh:
                cache = json.load(fh)
            rejected_indices = cache.get("rejected_indices", [])
        except Exception:
            pass
    return jsonify({"rejected_indices": rejected_indices})


@app.route("/api/comet/set_rejections", methods=["POST"])
def api_comet_set_rejections():
    """Persist a frame rejection list into comet_alignment.json."""
    data      = request.get_json(force=True) or {}
    directory = data.get("dir", "").strip()
    indices   = [int(x) for x in data.get("rejected_indices", [])]
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "Directory not found"}), 400
    cache_json = os.path.join(directory, "comet_alignment.json")
    cache = {}
    if os.path.isfile(cache_json):
        try:
            with open(cache_json) as fh:
                cache = json.load(fh)
        except Exception:
            pass
    cache["rejected_indices"] = sorted(indices)
    with open(cache_json, "w") as fh:
        json.dump(cache, fh, indent=2)
    return jsonify({"ok": True, "count": len(indices)})


@app.route("/api/comet/cancel", methods=["POST"])
def api_comet_cancel():
    job_id = (request.get_json(silent=True) or {}).get("job_id", "")
    with _comet_jobs_lock:
        job = _comet_jobs.get(job_id)
        if job and job["status"] == "running":
            job["status"] = "cancelled"
    return jsonify({"ok": True})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    body  = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))
    if not start_scan(force=force):
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started", "force": force})


@app.route("/api/status")
def api_status():
    with _scan_lock:
        running = _scan_running
    return jsonify({
        "running":   running,
        "last_scan": db.get_meta("last_scan"),
        "data_dir":  DATA_DIR,
        "sessions":  len(db.get_all_sessions()),
    })


# ── Startup ───────────────────────────────────────────────────────────────────

def _reclassify_and_broadcast() -> None:
    """Reclassify stale sessions in the DB and broadcast any changes over SSE."""
    n = db.reclassify_sessions(_catalog)
    if n:
        print(f"[startup] Reclassified {n} session(s) with updated type detection.")
        for session in db.get_all_sessions():
            _broadcast({"type": "session", "data": session})


def _backfill_video_durations() -> None:
    """
    Background thread: compute and broadcast video durations for any session
    that has num_videos > 0 but total_video_duration == 0 (sessions scanned
    before the duration feature was added).  Updates arrive progressively.
    """
    from scanner import backfill_video_durations_iter
    try:
        count = 0
        for session in backfill_video_durations_iter():
            _broadcast({"type": "session", "data": session})
            count += 1
        if count:
            print(f"[startup] Backfilled video durations for {count} session(s).")
    except Exception as exc:
        print(f"[startup] Duration backfill error: {exc}")


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Gracefully stop the server without killing the shell."""
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"status": "shutting down"})


if __name__ == "__main__":
    db.init_db()
    n_purged = db.purge_resource_fork_jobs()
    if n_purged:
        print(f"[startup] Purged {n_purged} macOS resource-fork pseudo-file job(s) from DB.")
    _reclassify_and_broadcast()

    # Start the single transit-detection worker thread.
    _worker = threading.Thread(target=_transit_worker_loop, daemon=True, name="transit-worker")
    _worker.start()

    # Start the single stacking worker thread.
    _stack_worker = threading.Thread(target=_stack_worker_loop, daemon=True, name="stack-worker")
    _stack_worker.start()
    pending_stack = db.get_pending_stack_jobs()
    for sj in pending_stack:
        # Re-queue interrupted stack jobs — we need the fits_files list, so
        # rebuild it from the session's paths rather than storing it in the DB.
        sessions_list = db.get_all_sessions()
        session = next((s for s in sessions_list if s["object_name"] == sj["session_name"]), None)
        if not session:
            db.fail_stack_job(sj["session_name"], "Session no longer found")
            continue
        from pathlib import Path as _Path
        FITS_EXT = {".fit", ".fits", ".fts"}
        fits_files = []
        for dir_path in session.get("paths", []):
            try:
                for fname in sorted(os.listdir(dir_path)):
                    if not fname.startswith(".") and _Path(fname).suffix.lower() in FITS_EXT:
                        fits_files.append(os.path.join(dir_path, fname))
            except OSError:
                pass
        if fits_files:
            output_dir  = session["paths"][0] if session.get("paths") else OUTPUT_DIR
            _stack_queue.put({
                "session_name": sj["session_name"],
                "fits_files":   fits_files,
                "output_path":  os.path.join(output_dir, "seestar_stacked.jpg"),
            })
    if pending_stack:
        print(f"[startup] Re-queued {len(pending_stack)} interrupted stack job(s).")

    # Backfill missing video durations (one-time, silently skips when all done).
    _bf = threading.Thread(target=_backfill_video_durations, daemon=True, name="duration-backfill")
    _bf.start()

    # Re-queue any jobs that were left 'running' or 'pending' from a previous crash.
    pending_jobs = db.get_pending_jobs()
    for job in pending_jobs:
        _transit_queue.put(job)
    if pending_jobs:
        print(f"[startup] Re-queued {len(pending_jobs)} interrupted transit job(s).")

    # Keyboard listener: type 'x' + Enter at the terminal to exit cleanly.
    def _kbd_listener():
        import sys
        try:
            while True:
                line = sys.stdin.readline()
                if not line or line.strip().lower() in ("x", "q", "exit", "quit"):
                    print("\n[shutdown] Keyboard exit — stopping server.")
                    os._exit(0)
        except Exception:
            pass
    _kbd = threading.Thread(target=_kbd_listener, daemon=True, name="kbd-listener")
    _kbd.start()

    # Kick off a differential scan right away; it will be fast if nothing changed.
    start_scan(force=False)
    print("Type 'x' + Enter to stop the server.")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
