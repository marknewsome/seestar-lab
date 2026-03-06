"""
Seestar Lab — SQLite persistence layer.

Three tables:
  sessions      — one row per unique object name (merged across all nights)
  scanned_dirs  — one row per directory; mtime drives differential re-scan
  meta          — key/value store (last_scan timestamp, data_dir, schema_ver)
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

DB_PATH = Path(__file__).parent / "seestar-lab.db"

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    object_name           TEXT PRIMARY KEY,
    object_type           TEXT NOT NULL DEFAULT 'unknown',
    type_label            TEXT DEFAULT '',
    description           TEXT DEFAULT '',
    dates                 TEXT DEFAULT '[]',   -- JSON array of YYYY-MM-DD strings
    num_subs              INTEGER DEFAULT 0,
    num_videos            INTEGER DEFAULT 0,
    total_size            INTEGER DEFAULT 0,
    total_size_human      TEXT DEFAULT '',
    total_video_duration  INTEGER DEFAULT 0,   -- seconds of video (sum of all clips)
    paths                 TEXT DEFAULT '[]',   -- JSON array of directory paths
    thumbnail             TEXT DEFAULT NULL,   -- absolute path to best preview image
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS scanned_dirs (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    file_count INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Transit detection: one row per source video
CREATE TABLE IF NOT EXISTS video_jobs (
    video_path    TEXT PRIMARY KEY,
    session_name  TEXT NOT NULL,
    video_type    TEXT NOT NULL,    -- 'solar' | 'lunar'
    output_dir    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|cancelled
    error_msg     TEXT,
    pct           INTEGER DEFAULT 0,
    message       TEXT DEFAULT '',
    queued_at     TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

-- Sub-frame stacking: one row per session ever stacked
CREATE TABLE IF NOT EXISTS stack_jobs (
    session_name    TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error
    pct             INTEGER DEFAULT 0,
    stage           TEXT DEFAULT '',
    frames_total    INTEGER DEFAULT 0,
    frames_accepted INTEGER DEFAULT 0,
    output_path     TEXT,
    error_msg       TEXT,
    queued_at       TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

-- Transit detection: one row per detected event
CREATE TABLE IF NOT EXISTS transit_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path           TEXT NOT NULL,
    session_name         TEXT NOT NULL,
    video_type           TEXT NOT NULL,
    label                TEXT NOT NULL,   -- 'plane' | 'bird' | 'iss' | 'unknown'
    confidence           REAL NOT NULL,
    frame_start          INTEGER,
    frame_end            INTEGER,
    duration_s           REAL,
    velocity_pct_per_sec REAL,
    linearity            REAL,
    clip_path            TEXT,
    meta_path            TEXT,
    aircraft_candidates  TEXT,            -- JSON array of candidate dicts (may be NULL)
    thumb_path           TEXT,            -- absolute path to hero-frame JPEG
    yolo_label           TEXT,            -- YOLO-validated class ('airplane'/'bird') or NULL
    yolo_confidence      REAL,            -- YOLO detection confidence, or NULL
    detected_at          TEXT
);
"""

# Migration: add thumbnail column to existing databases that predate it
_MIGRATE = """
ALTER TABLE sessions ADD COLUMN thumbnail TEXT DEFAULT NULL;
"""


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Initialisation ────────────────────────────────────────────────────────────

def init_db() -> None:
    with _db() as conn:
        conn.executescript(_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "thumbnail" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN thumbnail TEXT DEFAULT NULL")
        if "total_video_duration" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN total_video_duration INTEGER DEFAULT 0")
        te_cols = {row[1] for row in conn.execute("PRAGMA table_info(transit_events)").fetchall()}
        if "aircraft_candidates" not in te_cols:
            conn.execute("ALTER TABLE transit_events ADD COLUMN aircraft_candidates TEXT")
        if "thumb_path" not in te_cols:
            conn.execute("ALTER TABLE transit_events ADD COLUMN thumb_path TEXT")
        if "yolo_label" not in te_cols:
            conn.execute("ALTER TABLE transit_events ADD COLUMN yolo_label TEXT")
        if "yolo_confidence" not in te_cols:
            conn.execute("ALTER TABLE transit_events ADD COLUMN yolo_confidence REAL")
        # stack_jobs table (added in later schema revision)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS stack_jobs ("
            "  session_name    TEXT PRIMARY KEY,"
            "  status          TEXT NOT NULL DEFAULT 'pending',"
            "  pct             INTEGER DEFAULT 0,"
            "  stage           TEXT DEFAULT '',"
            "  frames_total    INTEGER DEFAULT 0,"
            "  frames_accepted INTEGER DEFAULT 0,"
            "  output_path     TEXT,"
            "  error_msg       TEXT,"
            "  queued_at       TEXT NOT NULL,"
            "  started_at      TEXT,"
            "  finished_at     TEXT"
            ");"
        )


# ── Sessions ──────────────────────────────────────────────────────────────────

def upsert_session(session: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (object_name, object_type, type_label, description,
                 dates, num_subs, num_videos, total_size, total_size_human,
                 total_video_duration, paths, thumbnail, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(object_name) DO UPDATE SET
                object_type           = excluded.object_type,
                type_label            = excluded.type_label,
                description           = excluded.description,
                dates                 = excluded.dates,
                num_subs              = excluded.num_subs,
                num_videos            = excluded.num_videos,
                total_size            = excluded.total_size,
                total_size_human      = excluded.total_size_human,
                total_video_duration  = excluded.total_video_duration,
                paths                 = excluded.paths,
                thumbnail             = excluded.thumbnail,
                updated_at            = excluded.updated_at
            """,
            (
                session["object_name"],
                session["object_type"],
                session["type_label"],
                session.get("description", ""),
                json.dumps(session.get("dates", [])),
                session.get("num_subs", 0),
                session.get("num_videos", 0),
                session.get("total_size", 0),
                session.get("total_size_human", ""),
                session.get("total_video_duration", 0),
                json.dumps(session.get("paths", [])),
                session.get("thumbnail"),
            ),
        )


def remove_session(object_name: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE object_name = ?", (object_name,))


def get_all_sessions() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [_session_row(r) for r in rows]


def _session_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["dates"] = json.loads(d.get("dates") or "[]")
    d["paths"] = json.loads(d.get("paths") or "[]")
    return d


# ── Scanned directories ───────────────────────────────────────────────────────

def get_all_scanned_dirs() -> dict[str, float]:
    """Return {path: mtime} for every directory we have on record."""
    with _db() as conn:
        rows = conn.execute("SELECT path, mtime FROM scanned_dirs").fetchall()
    return {r["path"]: r["mtime"] for r in rows}


def upsert_scanned_dir(path: str, mtime: float, file_count: int) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO scanned_dirs (path, mtime, file_count, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                mtime      = excluded.mtime,
                file_count = excluded.file_count,
                updated_at = excluded.updated_at
            """,
            (path, mtime, file_count),
        )


def remove_missing_dirs(current_paths: set[str]) -> list[str]:
    """
    Delete scanned_dirs rows for paths no longer on disk.
    Returns the list of removed paths.
    """
    with _db() as conn:
        stored = {
            r["path"]
            for r in conn.execute("SELECT path FROM scanned_dirs").fetchall()
        }
        removed = list(stored - current_paths)
        for p in removed:
            conn.execute("DELETE FROM scanned_dirs WHERE path = ?", (p,))
    return removed


# ── Reclassification ──────────────────────────────────────────────────────────

def reclassify_sessions(catalog) -> int:
    """
    Re-run detect_type() on every session's object_name and update the DB row
    if the classification or label has changed.  Returns the number of rows updated.
    No filesystem access — fast even for large libraries.
    """
    updated = 0
    with _db() as conn:
        rows = conn.execute(
            "SELECT object_name, object_type, type_label FROM sessions"
        ).fetchall()
        for row in rows:
            new_type  = catalog.detect_type(row["object_name"])
            new_label = catalog.type_label(new_type)
            if new_type != row["object_type"] or new_label != row["type_label"]:
                conn.execute(
                    "UPDATE sessions SET object_type=?, type_label=?, updated_at=datetime('now') "
                    "WHERE object_name=?",
                    (new_type, new_label, row["object_name"]),
                )
                updated += 1
    return updated


# ── Meta ──────────────────────────────────────────────────────────────────────

def set_meta(key: str, value: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with _db() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


# ── Video jobs ─────────────────────────────────────────────────────────────────

def queue_video_job(
    video_path: str,
    session_name: str,
    video_type: str,
    output_dir: str,
    force: bool = False,
) -> bool:
    """
    Insert a pending video_job row.  Returns True if inserted, False if the job
    already exists with status 'pending', 'running', or 'done' (and force=False).
    """
    with _db() as conn:
        existing = conn.execute(
            "SELECT status FROM video_jobs WHERE video_path = ?", (video_path,)
        ).fetchone()
        if existing and not force:
            if existing["status"] in ("pending", "running", "done"):
                return False
        conn.execute(
            """
            INSERT INTO video_jobs
                (video_path, session_name, video_type, output_dir, status, queued_at)
            VALUES (?, ?, ?, ?, 'pending', datetime('now'))
            ON CONFLICT(video_path) DO UPDATE SET
                status     = 'pending',
                error_msg  = NULL,
                pct        = 0,
                message    = '',
                queued_at  = datetime('now'),
                started_at = NULL,
                finished_at= NULL
            """,
            (video_path, session_name, video_type, output_dir),
        )
    return True


def start_video_job(video_path: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE video_jobs SET status='running', started_at=datetime('now'), pct=0 "
            "WHERE video_path=?",
            (video_path,),
        )


def update_video_job_progress(video_path: str, pct: int, message: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE video_jobs SET pct=?, message=? WHERE video_path=?",
            (pct, message, video_path),
        )


def finish_video_job(video_path: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE video_jobs SET status='done', pct=100, finished_at=datetime('now') "
            "WHERE video_path=?",
            (video_path,),
        )


def fail_video_job(video_path: str, error_msg: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE video_jobs SET status='error', error_msg=?, finished_at=datetime('now') "
            "WHERE video_path=?",
            (error_msg, video_path),
        )


def get_pending_jobs() -> list[dict]:
    """Return all jobs that were left 'running' (from a crash) plus 'pending'."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM video_jobs WHERE status IN ('pending','running') "
            "ORDER BY queued_at"
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_video_jobs(session_name: Optional[str] = None) -> int:
    """
    Mark pending/running jobs as 'cancelled'.
    If session_name is given, only that session's jobs are cancelled.
    Returns the number of rows updated.
    """
    with _db() as conn:
        if session_name:
            cur = conn.execute(
                "UPDATE video_jobs SET status='cancelled', finished_at=datetime('now') "
                "WHERE session_name=? AND status IN ('pending','running')",
                (session_name,),
            )
        else:
            cur = conn.execute(
                "UPDATE video_jobs SET status='cancelled', finished_at=datetime('now') "
                "WHERE status IN ('pending','running')"
            )
    return cur.rowcount


# ── Transit events ─────────────────────────────────────────────────────────────

def delete_transit_events_for_video(video_path: str) -> int:
    """Delete all transit_events rows for *video_path*. Returns deleted count."""
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM transit_events WHERE video_path=?", (video_path,)
        )
        return cur.rowcount


def purge_resource_fork_jobs() -> int:
    """
    Remove macOS resource-fork pseudo-files (basename starting with '._')
    from video_jobs and transit_events.  These are created when Mac users copy
    files to non-HFS volumes and are not real video files.
    Returns the number of video_job rows deleted.
    """
    with _db() as conn:
        # SQLite's substr+instr can't easily match on basename, but we can use
        # LIKE on the full path since the separator is always '/'.
        cur = conn.execute(
            "DELETE FROM video_jobs WHERE video_path LIKE '%/._%%'"
        )
        conn.execute(
            "DELETE FROM transit_events WHERE video_path LIKE '%/._%%'"
        )
        return cur.rowcount


def insert_transit_event(ev: dict) -> int:
    """Insert one event row and return its new id."""
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO transit_events
                (video_path, session_name, video_type, label, confidence,
                 frame_start, frame_end, duration_s, velocity_pct_per_sec,
                 linearity, clip_path, meta_path, aircraft_candidates, thumb_path,
                 yolo_label, yolo_confidence,
                 detected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            (
                ev["video_path"], ev["session_name"], ev["video_type"],
                ev["label"], ev["confidence"],
                ev.get("frame_start"), ev.get("frame_end"),
                ev.get("duration_s"), ev.get("velocity_pct_per_sec"),
                ev.get("linearity"), ev.get("clip_path"), ev.get("meta_path"),
                ev.get("aircraft_candidates"), ev.get("thumb_path"),
                ev.get("yolo_label"), ev.get("yolo_confidence"),
            ),
        )
    return cur.lastrowid


def get_transit_event(event_id: int) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM transit_events WHERE id=?", (event_id,)
        ).fetchone()
    return dict(row) if row else None


def get_transit_gallery() -> list[dict]:
    """
    Return all transit events as a flat list ordered by detected_at descending.
    Includes video_type and detected_at, which get_transit_summary omits.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM transit_events ORDER BY detected_at DESC"
        ).fetchall()
    result = []
    for ev in rows:
        ac: list = []
        try:
            if ev["aircraft_candidates"]:
                ac = json.loads(ev["aircraft_candidates"])
        except (json.JSONDecodeError, TypeError):
            pass
        result.append({
            "id":                   ev["id"],
            "video_path":           ev["video_path"],
            "session_name":         ev["session_name"],
            "video_type":           ev["video_type"],
            "label":                ev["label"],
            "confidence":           ev["confidence"],
            "duration_s":           ev["duration_s"],
            "velocity_pct_per_sec": ev["velocity_pct_per_sec"],
            "linearity":            ev["linearity"],
            "clip_path":            ev["clip_path"],
            "thumb_path":           ev["thumb_path"],
            "aircraft_candidates":  ac,
            "yolo_label":           ev["yolo_label"],
            "yolo_confidence":      ev["yolo_confidence"],
            "detected_at":          ev["detected_at"],
        })
    return result


def get_transit_summary() -> dict:
    """
    Return {session_name: {video_jobs: [...], events: [...]}} for all sessions
    that have ever had a job queued.  Used by /api/transit/all.
    """
    with _db() as conn:
        jobs   = conn.execute("SELECT * FROM video_jobs ORDER BY queued_at").fetchall()
        events = conn.execute(
            "SELECT * FROM transit_events ORDER BY detected_at"
        ).fetchall()

    result: dict[str, dict] = {}
    for j in jobs:
        sn = j["session_name"]
        result.setdefault(sn, {"video_jobs": [], "events": []})
        result[sn]["video_jobs"].append({
            "video_path": j["video_path"],
            "basename":   j["video_path"].rsplit("/", 1)[-1],
            "status":     j["status"],
            "pct":        j["pct"],
            "message":    j["message"] or "",
            "error_msg":  j["error_msg"],
        })

    for ev in events:
        sn = ev["session_name"]
        result.setdefault(sn, {"video_jobs": [], "events": []})
        ac: list = []
        try:
            if ev["aircraft_candidates"]:
                ac = json.loads(ev["aircraft_candidates"])
        except (json.JSONDecodeError, TypeError):
            pass
        result[sn]["events"].append({
            "id":                   ev["id"],
            "video_path":           ev["video_path"],
            "label":                ev["label"],
            "confidence":           ev["confidence"],
            "duration_s":           ev["duration_s"],
            "velocity_pct_per_sec": ev["velocity_pct_per_sec"],
            "clip_path":            ev["clip_path"],
            "thumb_path":           ev["thumb_path"],
            "aircraft_candidates":  ac,
            "yolo_label":           ev["yolo_label"],
            "yolo_confidence":      ev["yolo_confidence"],
        })

    return result


# ── Stack jobs ─────────────────────────────────────────────────────────────────

def queue_stack_job(session_name: str, force: bool = False) -> bool:
    """
    Insert a pending stack_job row.
    Returns False (without inserting) if one already exists in a non-error state
    and force=False.
    """
    with _db() as conn:
        existing = conn.execute(
            "SELECT status FROM stack_jobs WHERE session_name = ?", (session_name,)
        ).fetchone()
        if existing and not force:
            if existing["status"] in ("pending", "running", "done"):
                return False
        conn.execute(
            """
            INSERT INTO stack_jobs
                (session_name, status, pct, stage, frames_total, frames_accepted,
                 output_path, error_msg, queued_at)
            VALUES (?, 'pending', 0, '', 0, 0, NULL, NULL, datetime('now'))
            ON CONFLICT(session_name) DO UPDATE SET
                status          = 'pending',
                pct             = 0,
                stage           = '',
                frames_total    = 0,
                frames_accepted = 0,
                output_path     = NULL,
                error_msg       = NULL,
                queued_at       = datetime('now'),
                started_at      = NULL,
                finished_at     = NULL
            """,
            (session_name,),
        )
    return True


def start_stack_job(session_name: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE stack_jobs SET status='running', started_at=datetime('now'), pct=0 "
            "WHERE session_name=?", (session_name,)
        )


def update_stack_job_progress(
    session_name: str, pct: int, stage: str,
    frames_accepted: int, frames_total: int,
) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE stack_jobs SET pct=?, stage=?, frames_accepted=?, frames_total=? "
            "WHERE session_name=?",
            (pct, stage, frames_accepted, frames_total, session_name),
        )


def finish_stack_job(
    session_name: str, output_path: str,
    frames_accepted: int, frames_total: int,
) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE stack_jobs SET status='done', pct=100, "
            "output_path=?, frames_accepted=?, frames_total=?, "
            "finished_at=datetime('now') WHERE session_name=?",
            (output_path, frames_accepted, frames_total, session_name),
        )


def fail_stack_job(session_name: str, error_msg: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE stack_jobs SET status='error', error_msg=?, "
            "finished_at=datetime('now') WHERE session_name=?",
            (error_msg, session_name),
        )


def get_stack_job(session_name: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM stack_jobs WHERE session_name=?", (session_name,)
        ).fetchone()
    return dict(row) if row else None


def get_all_stack_jobs() -> dict:
    """Return {session_name: job_dict} for every stack job."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM stack_jobs").fetchall()
    return {r["session_name"]: dict(r) for r in rows}


def get_pending_stack_jobs() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM stack_jobs WHERE status IN ('pending','running') "
            "ORDER BY queued_at"
        ).fetchall()
    return [dict(r) for r in rows]
