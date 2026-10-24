"""Read-only Ableton set discovery and project launch helpers (no GUI imports)."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time

PINNED_FOLDERS_KEY = "pinned_project_folders"
RECENT_LIMIT = 10
FOLDER_LIMIT = 25
SCAN_ENTRY_LIMIT = 20000
SCAN_SECONDS = 5


def normalize_pinned_folders(value):
    """Validate saved configuration; offline/removable folders remain valid pins."""
    try:
        folders = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError) as exc:
        raise ValueError("Folders must be a JSON list") from exc
    if not isinstance(folders, list) or len(folders) > 20:
        raise ValueError("Choose up to 20 folders")
    result, seen = [], set()
    for folder in folders:
        if not isinstance(folder, dict) or not isinstance(folder.get("path"), str):
            raise ValueError("Each folder needs a path")
        raw = folder["path"].strip()
        if not raw or any(ord(c) < 32 for c in raw):
            raise ValueError("Folder paths cannot be blank or contain control characters")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("Use an absolute folder path or a path starting with ~/")
        path = path.resolve()
        if path.exists() and not path.is_dir():
            raise ValueError("Choose a folder, rather than a file")
        label = folder.get("label", "")
        if not isinstance(label, str) or any(ord(c) < 32 for c in label):
            raise ValueError("Folder labels cannot contain control characters")
        label = label.strip() or path.name or str(path)
        if len(label) > 80:
            raise ValueError("Folder labels can have up to 80 characters")
        if str(path) in seen:
            raise ValueError("That folder is already pinned")
        seen.add(str(path))
        result.append({"path": str(path), "label": label})
    return result


def load_pinned_folders(db_path):
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (PINNED_FOLDERS_KEY,)).fetchone()
    return normalize_pinned_folders(row[0]) if row else []


def recent_projects(db_path, limit=RECENT_LIMIT):
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        rows = conn.execute("""
            SELECT project_name, MAX(COALESCE(last_seen_time, start_time)) AS latest
            FROM sessions
            WHERE trim(project_name) != ''
              AND lower(trim(project_name)) NOT IN ('untitled', 'untitled project')
            GROUP BY project_name ORDER BY latest DESC, project_name LIMIT ?
        """, (min(max(int(limit), 0), RECENT_LIMIT),)).fetchall()
    return [row[0] for row in rows]


def _scan_folder(folder, deadline):
    root = Path(folder)
    found, errors = [], []
    visited = 0
    for base, dirs, files in os.walk(root, onerror=errors.append):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.casefold() != 'backup')
        for filename in files:
            visited += 1
            if visited > SCAN_ENTRY_LIMIT or time.monotonic() >= deadline:
                return sorted(found, key=lambda p: (-p["modified"], p["path"])), "Scan limit reached; showing sets found so far"
            if not filename.lower().endswith('.als') or filename.startswith('.'):
                continue
            path = Path(base) / filename
            try:
                if path.is_file():
                    found.append({"name": path.stem, "path": str(path), "modified": path.stat().st_mtime})
            except OSError:
                continue
        if time.monotonic() >= deadline:
            return sorted(found, key=lambda p: (-p["modified"], p["path"])), "Scan limit reached; showing sets found so far"
    return sorted(found, key=lambda p: (-p["modified"], p["path"])), ("Some folders could not be read" if errors else None)


def _spotlight_matches(names):
    if not names:
        return []
    def quoted(name):
        return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'
    query = " || ".join(f"kMDItemFSName == {quoted(name + '.als')}c" for name in names)
    try:
        result = subprocess.run(["/usr/bin/mdfind", "-0", query], capture_output=True, timeout=3)
        if result.returncode:
            return []
        paths = result.stdout.decode('utf-8', errors='replace').split('\0')
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for raw in paths[:SCAN_ENTRY_LIMIT]:
        if not raw:
            continue
        path = Path(raw)
        if any(p.casefold() == 'backup' for p in path.parts):
            continue
        try:
            if path.suffix.lower() == '.als' and path.is_file():
                found.append({"name": path.stem, "path": str(path), "modified": path.stat().st_mtime})
        except OSError:
            continue
    return sorted(found, key=lambda p: (-p["modified"], p["path"]))


def build_catalog(db_path):
    names = recent_projects(db_path)
    warning = None
    try:
        pins = load_pinned_folders(db_path)
    except ValueError:
        pins = []
        warning = "Folder settings could not be read. Open Customize… to save them again."
    folders, candidates = [], []
    for pin in pins:
        path = Path(pin["path"])
        missing = not path.is_dir()
        found, problem = ([], None) if missing else _scan_folder(path, time.monotonic() + SCAN_SECONDS)
        candidates.extend(found)
        folders.append({**pin, "missing": missing, "projects": found[:FOLDER_LIMIT], "more": len(found) > FOLDER_LIMIT, "warning": problem})
    matches = {}
    for candidate in sorted(candidates, key=lambda p: (-p["modified"], p["path"])):
        matches.setdefault(candidate["name"].casefold(), candidate["path"])
    unresolved = [name for name in names if name.casefold() not in matches]
    for candidate in _spotlight_matches(unresolved):
        matches.setdefault(candidate["name"].casefold(), candidate["path"])
    return {"recent": [{"name": name, "path": matches.get(name.casefold())} for name in names], "folders": folders, "warning": warning}


def open_project(path, app_path):
    path = Path(path)
    if path.suffix.lower() != '.als' or not path.is_file():
        raise ValueError("This Live set is no longer available. Check its folder or reconnect the drive.")
    args = ["/usr/bin/open"]
    if app_path:
        args += ["-a", app_path]
    result = subprocess.run(args + [str(path.resolve())], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "macOS could not open this Live set")


class ProjectCatalog:
    """Only publish complete snapshots; rumps stays on the main thread."""
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._busy = False
        self._pending = None

    def refresh_async(self):
        with self._lock:
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        try:
            snapshot = build_catalog(self.db_path)
        except Exception as exc:
            snapshot = {"recent": [], "folders": [], "warning": f"Projects unavailable: {exc}"}
        with self._lock:
            self._pending = snapshot
            self._busy = False

    def take_snapshot(self):
        with self._lock:
            snapshot, self._pending = self._pending, None
            return snapshot
