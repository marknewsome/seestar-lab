"""
Seestar Lab — differential filesystem scanner.

scan_differential()  — only re-processes directories whose mtime changed.
scan_full()          — forces a re-read of every directory.

Both are generators that yield event dicts consumed by app.py and broadcast
over SSE to the browser:

    {'type': 'progress', 'message': str}
    {'type': 'session',  'data': dict}
    {'type': 'session_removed', 'object_name': str}
    {'type': 'complete', 'changed': int, 'total': int}
    {'type': 'error',    'message': str}
"""

import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

import db
from object_catalog import ObjectCatalog

try:
    import cv2 as _cv2
    _HAVE_CV2 = True
except ImportError:
    _HAVE_CV2 = False

# ── Constants ─────────────────────────────────────────────────────────────────

FITS_EXT   = {".fit", ".fits", ".fts"}
VIDEO_EXT  = {".mp4", ".avi", ".mov", ".mkv"}
IMAGE_EXT  = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
TARGET_EXT = FITS_EXT | VIDEO_EXT

_SKIP_DIRS = {"venv", ".git", "__pycache__", "node_modules", "output"}
_GENERIC   = {
    "stars", "photos", "videos", "subs", "xfer", "output",
    "autorun", "temp", "tmp", "preview", "thumbnails",
}
_DATE_RE = re.compile(r"(\d{4})[_\-](\d{2})[_\-](\d{2})")

# Keywords that identify a "best" preview image (checked against filename stem,
# case-insensitive, in priority order).
_THUMB_KEYWORDS = ("enhanced", "stacked", "stack", "mosaic", "preview", "final", "processed")

Event = dict  # type alias for readability


def backfill_video_durations_iter() -> Generator[dict, None, None]:
    """
    Generator: for each session that has videos but a zero total_video_duration
    (e.g. scanned before this feature was added), compute and persist the
    duration then yield the updated session dict so the caller can broadcast it.

    Requires cv2; silently returns nothing if it is unavailable.
    """
    if not _HAVE_CV2:
        return

    for s in db.get_all_sessions():
        if s.get("num_videos", 0) == 0 or s.get("total_video_duration", 0) > 0:
            continue

        duration = 0
        for dir_path in s.get("paths", []):
            try:
                for fname in os.listdir(dir_path):
                    if Path(fname).suffix.lower() in VIDEO_EXT:
                        vf = os.path.join(dir_path, fname)
                        cap = _cv2.VideoCapture(vf)
                        fc  = cap.get(_cv2.CAP_PROP_FRAME_COUNT)
                        fps = cap.get(_cv2.CAP_PROP_FPS)
                        cap.release()
                        if fps > 0 and fc > 0:
                            duration += int(fc / fps)
            except Exception:
                pass

        if duration > 0:
            s["total_video_duration"] = duration
            db.upsert_session(s)
            yield s


class Scanner:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.catalog  = ObjectCatalog()

    # ── Public API ────────────────────────────────────────────────────────────

    def scan_full(self) -> Generator[Event, None, None]:
        yield from self._scan(force=True)

    def scan_differential(self) -> Generator[Event, None, None]:
        yield from self._scan(force=False)

    # ── Core scan ─────────────────────────────────────────────────────────────

    def _scan(self, force: bool) -> Generator[Event, None, None]:
        if not self.data_dir.exists():
            yield {"type": "error", "message": f"Directory not found: {self.data_dir}"}
            return

        # ── Phase 1: walk tree, collect mtimes and file lists (fast) ──────────
        yield {"type": "progress", "message": "Walking directory tree…"}

        stored_mtimes = db.get_all_scanned_dirs()   # {path: mtime}
        current_mtimes: dict[str, float] = {}        # {path: mtime}  — every dir
        leaf_files: dict[str, list[str]] = {}        # {path: [filenames]}  — dirs with target files

        dir_count = 0
        for root, dirs, files in os.walk(str(self.data_dir)):
            dirs[:] = sorted(
                d for d in dirs
                if d not in _SKIP_DIRS
                and not d.startswith(".")
                and not _is_copy_dir(d)          # skip "Foo - Copy" directories
            )
            current_mtimes[root] = os.path.getmtime(root)
            dir_count += 1

            relevant = [f for f in files if Path(f).suffix.lower() in TARGET_EXT]
            if relevant:
                leaf_files[root] = files   # keep all files for size accounting

            if dir_count % 2000 == 0:
                yield {"type": "progress", "message": f"Walked {dir_count:,} directories…"}

        yield {
            "type": "progress",
            "message": (
                f"Found {len(leaf_files):,} data directories in "
                f"{dir_count:,} total — checking for changes…"
            ),
        }

        # ── Phase 2: remove stale directories from DB ─────────────────────────
        removed_paths = db.remove_missing_dirs(set(current_mtimes.keys()))
        if removed_paths:
            yield {
                "type": "progress",
                "message": f"Removed {len(removed_paths)} disappeared director(ies) from index",
            }
            # If an entire object lost all its directories, remove its session
            for path in removed_paths:
                obj = self._object_name(Path(path))
                remaining = [p for p in leaf_files if self._object_name(Path(p)) == obj]
                if not remaining:
                    db.remove_session(obj)
                    yield {"type": "session_removed", "object_name": obj}

        # ── Phase 3: identify changed leaf directories ─────────────────────────
        changed_dirs: set[str] = set()
        for path in leaf_files:
            if force or path not in stored_mtimes or stored_mtimes[path] != current_mtimes[path]:
                changed_dirs.add(path)

        if not changed_dirs:
            yield {"type": "progress", "message": "No changes detected — index is current"}
            yield {"type": "complete", "changed": 0, "total": len(db.get_all_sessions())}
            return

        n = len(changed_dirs)
        yield {
            "type": "progress",
            "message": f"{n:,} director{'y' if n == 1 else 'ies'} changed — rebuilding sessions…",
        }

        # ── Phase 4: find all affected object names ────────────────────────────
        # A changed directory may belong to an object that spans other,
        # unchanged directories (e.g. M42 over 3 nights, only night 2 changed).
        # We must rebuild the full merged session for any affected object.
        affected_objects: set[str] = {self._object_name(Path(p)) for p in changed_dirs}

        obj_dirs: dict[str, list[str]] = defaultdict(list)
        for path in leaf_files:
            obj = self._object_name(Path(path))
            if obj in affected_objects:
                obj_dirs[obj].append(path)

        # ── Phase 5: rebuild one session per affected object ───────────────────
        total_objs = len(obj_dirs)
        sessions_changed = 0
        for i, (obj_name, paths) in enumerate(obj_dirs.items()):
            session = self._build_session(obj_name, paths, leaf_files, current_mtimes)
            if session:
                db.upsert_session(session)
                for p in paths:
                    db.upsert_scanned_dir(p, current_mtimes[p], len(leaf_files.get(p, [])))
                sessions_changed += 1
                yield {"type": "session", "data": session}
            else:
                db.remove_session(obj_name)
                yield {"type": "session_removed", "object_name": obj_name}

            pct = round((i + 1) / total_objs * 100) if total_objs else 100
            yield {
                "type":    "progress",
                "message": f"Rebuilding sessions\u2026 {i + 1:,} / {total_objs:,}",
                "pct":     pct,
            }

        db.set_meta("last_scan", datetime.now().isoformat(timespec="seconds"))
        db.set_meta("data_dir", str(self.data_dir))

        yield {
            "type": "complete",
            "changed": sessions_changed,
            "total": len(db.get_all_sessions()),
        }

    # ── Session builder ───────────────────────────────────────────────────────

    def _build_session(
        self,
        obj_name: str,
        paths: list[str],
        leaf_files: dict[str, list[str]],
        dir_mtimes: dict[str, float],
    ) -> Optional[dict]:
        fits_files:  list[str] = []
        video_files: list[str] = []
        image_files: list[str] = []
        dates: set[str] = set()

        for path in paths:
            all_files = leaf_files.get(path, [])
            for f in all_files:
                if f.startswith('.'):    # skip macOS ._* resource forks and hidden files
                    continue
                ext = Path(f).suffix.lower()
                full = os.path.join(path, f)
                if ext in FITS_EXT:
                    fits_files.append(full)
                elif ext in VIDEO_EXT:
                    video_files.append(full)
                elif ext in IMAGE_EXT:
                    image_files.append(full)

            path_dates = _dates_from_path(Path(path))
            if path_dates:
                dates.update(path_dates)
            else:
                dates.update(_dates_from_mtimes(path, all_files[:20]))

        if not fits_files and not video_files:
            return None

        total_size = sum(
            os.path.getsize(f)
            for f in fits_files + video_files + image_files
            if os.path.isfile(f)
        )

        # Sum video durations by reading container metadata (no frame decoding)
        total_video_duration = 0
        if _HAVE_CV2:
            for vf in video_files:
                try:
                    cap = _cv2.VideoCapture(vf)
                    fc  = cap.get(_cv2.CAP_PROP_FRAME_COUNT)
                    fps = cap.get(_cv2.CAP_PROP_FPS)
                    cap.release()
                    if fps > 0 and fc > 0:
                        total_video_duration += int(fc / fps)
                except Exception:
                    pass

        obj_type = self.catalog.detect_type(obj_name)
        thumbnail = _find_thumbnail(image_files)

        return {
            "object_name":          obj_name,
            "object_type":          obj_type,
            "type_label":           self.catalog.type_label(obj_type),
            "description":          self.catalog.get_description(obj_name),
            "dates":                sorted(dates),
            "num_subs":             len(fits_files),
            "num_videos":           len(video_files),
            "total_size":           total_size,
            "total_size_human":     _human_size(total_size),
            "total_video_duration": total_video_duration,
            "paths":                paths,
            "thumbnail":            thumbnail,
        }

    # ── Object-name extraction ────────────────────────────────────────────────

    def _object_name(self, path: Path) -> str:
        try:
            rel_parts = path.relative_to(self.data_dir).parts
        except ValueError:
            rel_parts = path.parts
        for part in reversed(rel_parts):
            lower = part.lower()
            if lower in _GENERIC:
                continue
            if _DATE_RE.match(part):
                continue
            if re.match(r"^\d{6,}$", part):
                continue
            return part
        return path.name


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_copy_dir(name: str) -> bool:
    """Return True if the directory name ends with '- Copy' (case-insensitive)."""
    return bool(re.search(r"\s*-\s*copy\s*$", name, re.IGNORECASE))


def _find_thumbnail(image_files: list[str]) -> Optional[str]:
    """
    Pick the best preview image from a list of absolute image paths.
    Prefers files whose stem contains recognised keywords; falls back to
    the first available JPG/PNG, then any image.
    Returns the absolute path or None.
    """
    if not image_files:
        return None

    # Separate JPEG/PNG (smaller, web-friendly) from TIFF
    web_images = [f for f in image_files if Path(f).suffix.lower() in {".jpg", ".jpeg", ".png"}]
    candidates = web_images or image_files

    # Score by keyword presence in filename stem (lower is better)
    def _score(path: str) -> int:
        stem = Path(path).stem.lower()
        for i, kw in enumerate(_THUMB_KEYWORDS):
            if kw in stem:
                return i
        return len(_THUMB_KEYWORDS)

    return min(candidates, key=_score)


def _dates_from_path(path: Path) -> list[str]:
    seen: list[str] = []
    for part in path.parts:
        m = _DATE_RE.search(part)
        if m:
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if d not in seen:
                seen.append(d)
    return seen


def _dates_from_mtimes(directory: str, filenames: list[str]) -> list[str]:
    dates: set[str] = set()
    for f in filenames:
        try:
            mtime = os.path.getmtime(os.path.join(directory, f))
            dates.add(datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"))
        except OSError:
            pass
    return sorted(dates)


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes //= 1024
    return f"{size_bytes:.1f} PB"
