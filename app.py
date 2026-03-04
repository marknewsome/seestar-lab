"""
Seestar Lab — Flask backend.

Key design points:
  • On startup: init DB, kick off a differential scan in a background thread.
  • GET /api/events  — SSE stream; immediately replays all DB sessions, then
                       relays live broadcast events as the scan runs.
  • Pub/sub via per-subscriber queues so multiple browser tabs all receive
    the same events without blocking each other.
  • GET /catalog/messier, /catalog/caldwell — catalog bingo-card pages.
  • GET /api/catalog/messier, /api/catalog/caldwell — JSON catalog data.
  • GET /api/thumbnail/<object_name> — serve resized preview image.
  • POST /api/transit/detect — queue transit detection jobs for a session.
  • GET  /api/transit/all   — return all job + event data.
  • GET  /api/transit/clip/<id> — serve a detected-event clip MP4.
"""

import io
import json
import os
import queue
import re
import shutil
import tempfile
import threading
from datetime import datetime
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

_transit_queue:    queue.Queue    = queue.Queue()
_transit_paused:   threading.Event = threading.Event()
_transit_paused.set()   # set = running; clear = paused
_cancel_sessions:  set  = set()
_cancel_lock:      threading.Lock = threading.Lock()


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

    # Aircraft lookup is optional — gracefully absent if module not installed
    try:
        from aircraft_lookup import lookup_aircraft as _lookup_fn
    except ImportError:
        _lookup_fn = None

    db.start_video_job(video_path)
    # Clear any stale events so re-detection starts with a clean slate.
    db.delete_transit_events_for_video(video_path)

    def progress_cb(pct: int, _total: int, message: str) -> None:
        db.update_video_job_progress(video_path, pct, message)
        _broadcast({
            "type":           "transit_progress",
            "session_name":   session_name,
            "video_path":     video_path,
            "video_basename": basename,
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

    # ── Aircraft lookup ───────────────────────────────────────────────────────
    # One OpenSky query per qualifying event (plane / iss / unknown with a UTC
    # timestamp).  Birds are skipped — they don't appear in ADS-B data.
    # Any failure returns [] so the job always completes successfully.
    if _lookup_fn and any(
        ev.frame_utc_start and ev.label != "bird" for ev in events
    ):
        progress_cb(98, 100, "Looking up aircraft…")

    event_rows = []
    for ev in events:
        aircraft_candidates: list = []
        if _lookup_fn and ev.frame_utc_start and ev.label != "bird":
            try:
                utc_dt = _datetime.fromisoformat(
                    ev.frame_utc_start.replace("Z", "+00:00")
                )
                aircraft_candidates = _lookup_fn(utc_dt)
            except Exception:
                pass

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
            "aircraft_candidates":  json.dumps(aircraft_candidates) if aircraft_candidates else None,
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
            "aircraft_candidates":  aircraft_candidates,
            "yolo_label":           ev.yolo_label,
            "yolo_confidence":      ev.yolo_confidence,
        })

    db.finish_video_job(video_path)
    _broadcast({
        "type":           "transit_done",
        "session_name":   session_name,
        "video_path":     video_path,
        "video_basename": basename,
        "events":         event_rows,
        "total_events":   len(event_rows),
    })


def _transit_worker_loop() -> None:
    while True:
        job = _transit_queue.get()
        try:
            # ── Pause gate ────────────────────────────────────────────────────
            _transit_paused.wait()   # blocks here while paused

            # ── Cancellation check ────────────────────────────────────────────
            if _is_cancelled(job["session_name"]):
                db.cancel_video_jobs(job["session_name"])
                _broadcast({
                    "type":           "transit_progress",
                    "session_name":   job["session_name"],
                    "video_path":     job["video_path"],
                    "video_basename": job["video_path"].rsplit("/", 1)[-1],
                    "status":         "cancelled",
                    "message":        "Cancelled",
                    "pct":            0,
                })
                continue

            _run_transit_job(job)

        except RuntimeError as exc:
            if "cancelled" in str(exc).lower():
                db.cancel_video_jobs(job["session_name"])
                _broadcast({
                    "type":           "transit_progress",
                    "session_name":   job["session_name"],
                    "video_path":     job["video_path"],
                    "video_basename": job["video_path"].rsplit("/", 1)[-1],
                    "status":         "cancelled",
                    "message":        "Cancelled",
                    "pct":            0,
                })
            else:
                db.fail_video_job(job["video_path"], str(exc))
                _broadcast({
                    "type":           "transit_progress",
                    "session_name":   job["session_name"],
                    "video_path":     job["video_path"],
                    "video_basename": job["video_path"].rsplit("/", 1)[-1],
                    "status":         "error",
                    "message":        str(exc),
                    "pct":            0,
                })
        except Exception as exc:
            db.fail_video_job(job["video_path"], str(exc))
            _broadcast({
                "type":           "transit_progress",
                "session_name":   job["session_name"],
                "video_path":     job["video_path"],
                "video_basename": job["video_path"].rsplit("/", 1)[-1],
                "status":         "error",
                "message":        str(exc),
                "pct":            0,
            })
        finally:
            _transit_queue.task_done()


# ── SSE endpoint ──────────────────────────────────────────────────────────────

@app.route("/api/events")
def api_events() -> Response:
    q = _subscribe()

    def stream() -> Generator[str, None, None]:
        # 1. Immediately replay every session already in the DB —
        #    the browser gets a full view before the scan even starts.
        sessions = db.get_all_sessions()
        for s in sessions:
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


@app.route("/api/sessions")
def api_sessions():
    return jsonify(db.get_all_sessions())


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


if __name__ == "__main__":
    db.init_db()
    n_purged = db.purge_resource_fork_jobs()
    if n_purged:
        print(f"[startup] Purged {n_purged} macOS resource-fork pseudo-file job(s) from DB.")
    _reclassify_and_broadcast()

    # Start the single transit-detection worker thread.
    _worker = threading.Thread(target=_transit_worker_loop, daemon=True, name="transit-worker")
    _worker.start()

    # Backfill missing video durations (one-time, silently skips when all done).
    _bf = threading.Thread(target=_backfill_video_durations, daemon=True, name="duration-backfill")
    _bf.start()

    # Re-queue any jobs that were left 'running' or 'pending' from a previous crash.
    pending_jobs = db.get_pending_jobs()
    for job in pending_jobs:
        _transit_queue.put(job)
    if pending_jobs:
        print(f"[startup] Re-queued {len(pending_jobs)} interrupted transit job(s).")

    # Kick off a differential scan right away; it will be fast if nothing changed.
    start_scan(force=False)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
