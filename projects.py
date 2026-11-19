"""Read-only Ableton set discovery and project launch helpers (no GUI imports)."""
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata

PINNED_FOLDERS_KEY = "pinned_project_folders"
TEMPLATES_KEY = "project_templates"
TEMPLATES_DIR = Path.home() / "Music" / "Ableton" / "User Library" / "Templates"
VOLUMES_ROOT = Path("/Volumes")
RECENT_LIMIT = 10
FOLDER_LIMIT = 25
SCAN_ENTRY_LIMIT = 20000
SCAN_SECONDS = 5

_WHITESPACE_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_ALS_SUFFIX_RE = re.compile(r"\.als\s*$", re.IGNORECASE)
_SEARCH_TOKEN_RE = re.compile(r"[^\W_]+")
_STAGE_FALLBACK_ORDER = {"mastering": 2, "mixing": 1, None: 0}


def _exact_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name or "").casefold()


def _collapsed_name_key(name: str) -> str:
    text = unicodedata.normalize("NFC", name or "").strip()
    text = _ALS_SUFFIX_RE.sub("", text).strip()
    return _WHITESPACE_RE.sub(" ", text).strip(" -–—").casefold()


def _normalized_name_key(name: str) -> str:
    """Match tracker.parse_project_title normalization so DB names match disk filenames."""
    text = unicodedata.normalize("NFC", name or "").strip()
    text = _BRACKET_RE.sub("", text).strip()
    text = _PAREN_RE.sub("", text).strip()
    text = _ALS_SUFFIX_RE.sub("", text).strip()
    return _WHITESPACE_RE.sub(" ", text).strip(" -–—").casefold()


def _match_tier(target_name: str, candidate_name: str):
    """Return 0 (exact NFC), 1 (collapsed whitespace), 2 (paren/bracket-stripped), or None."""
    exact_target = _exact_name_key(target_name)
    if exact_target and _exact_name_key(candidate_name) == exact_target:
        return 0
    collapsed_target = _collapsed_name_key(target_name)
    if collapsed_target and _collapsed_name_key(candidate_name) == collapsed_target:
        return 1
    norm_target = _normalized_name_key(target_name)
    if norm_target and _normalized_name_key(candidate_name) == norm_target:
        return 2
    return None


def _best_candidate(target_name: str, candidates: list[dict]):
    best = None
    best_rank = None
    for candidate in candidates:
        tier = _match_tier(target_name, candidate["name"])
        if tier is None:
            continue
        rank = (tier, -float(candidate.get("modified") or 0), candidate.get("path") or "")
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = candidate
    return best


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


def list_templates():
    """Top-level .als files in Live's Templates folder; subfolders are not templates."""
    try:
        entries = list(TEMPLATES_DIR.iterdir())
    except OSError:
        return []
    return sorted((p.name for p in entries if p.suffix.lower() == '.als' and not p.name.startswith('.') and p.is_file()), key=str.casefold)


def normalize_templates(value):
    """Selected template filenames, in menu order; missing files stay valid selections."""
    try:
        names = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError) as exc:
        raise ValueError("Templates must be a JSON list") from exc
    if not isinstance(names, list) or len(names) > 50:
        raise ValueError("Choose up to 50 templates")
    result = []
    for name in names:
        if (not isinstance(name, str) or name != name.strip() or not name.lower().endswith('.als')
                or name.startswith('.') or '/' in name or any(ord(c) < 32 for c in name)):
            raise ValueError("Each template must be a Live set file name")
        if name in result:
            raise ValueError("That template is already selected")
        result.append(name)
    return result


def load_templates(db_path):
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (TEMPLATES_KEY,)).fetchone()
    selected = normalize_templates(row[0]) if row else []
    return [{"name": Path(n).stem, "path": str(TEMPLATES_DIR / n)} for n in selected if (TEMPLATES_DIR / n).is_file()]


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


def _escape_spotlight_wildcards(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def _spotlight_quote(value: str, escape_wildcards: bool = False) -> str:
    if escape_wildcards:
        value = _escape_spotlight_wildcards(value)
    else:
        value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    return f'"{value}"'


def _spotlight_tokens(name: str) -> list[str]:
    stripped = _PAREN_RE.sub("", _BRACKET_RE.sub("", unicodedata.normalize("NFC", name or ""))).strip()
    tokens = [t for t in _SEARCH_TOKEN_RE.findall(stripped) if len(t) >= 2]
    if not tokens:
        tokens = _SEARCH_TOKEN_RE.findall(stripped)
    if not tokens:
        tokens = [t for t in stripped.split() if t]
    deduped = list(dict.fromkeys(tokens))
    if len(deduped) > 4:
        top = set(sorted(deduped, key=len, reverse=True)[:4])
        deduped = [t for t in deduped if t in top]
    return deduped


def _run_mdfind(
    query: str,
    target_names: list[str],
    seen_paths: set[str],
    timeout: float = SCAN_SECONDS,
    only_in: str | None = None,
) -> list[dict]:
    cmd = ["/usr/bin/mdfind"]
    if only_in:
        cmd += ["-onlyin", str(only_in)]
    cmd += ["-0", query]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode:
            return []
        paths = result.stdout.decode("utf-8", errors="replace").split("\0")
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for raw in paths[:SCAN_ENTRY_LIMIT]:
        if not raw or raw in seen_paths:
            continue
        path = Path(raw)
        if path.name.startswith(".") or any(p.casefold() == "backup" for p in path.parts):
            continue
        try:
            if path.suffix.lower() != ".als" or not path.is_file():
                continue
            stem = path.stem
            if not any(_match_tier(target, stem) is not None for target in target_names):
                continue
            seen_paths.add(raw)
            found.append({"name": stem, "path": str(path), "modified": path.stat().st_mtime})
        except OSError:
            continue
    return found


def _spotlight_matches(names, only_in: str | None = None):
    clean_names = list(dict.fromkeys(n for n in (names or ()) if n and n.strip()))
    if not clean_names:
        return []
    seen_paths: set[str] = set()
    exact_query = " || ".join(
        f"kMDItemFSName == {_spotlight_quote(name + '.als', escape_wildcards=True)}c"
        for name in clean_names
    )
    found = _run_mdfind(exact_query, clean_names, seen_paths, timeout=SCAN_SECONDS, only_in=only_in)
    missing = [name for name in clean_names if _best_candidate(name, found) is None]
    for start in range(0, len(missing), 15):
        batch = missing[start:start + 15]
        clauses = []
        for name in batch:
            tokens = _spotlight_tokens(name)
            if tokens:
                clauses.append(
                    "(" + " && ".join(
                        f'kMDItemFSName == {_spotlight_quote("*" + _escape_spotlight_wildcards(tok) + "*")}cd'
                        for tok in tokens
                    ) + ")"
                )
        if clauses:
            wildcard_query = 'kMDItemFSName == "*.als"c && (' + " || ".join(clauses) + ")"
            found.extend(_run_mdfind(wildcard_query, batch, seen_paths, timeout=SCAN_SECONDS, only_in=only_in))
    return sorted(found, key=lambda p: (-p["modified"], p["path"]))


def _external_volume_roots() -> tuple[list[Path], bool]:
    """Return accessible mounted external volumes and whether TCC denied any volume."""
    volumes: list[Path] = []
    permission_denied = False
    try:
        entries = list(os.scandir(VOLUMES_ROOT))
    except PermissionError:
        return [], True
    except OSError:
        return [], False
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if path.resolve() == Path("/"):
                continue
            with os.scandir(path):
                pass
        except PermissionError:
            permission_denied = True
        except OSError:
            continue
        else:
            volumes.append(path)
    return volumes, permission_denied


def build_catalog(db_path):
    names = recent_projects(db_path)
    warning = None
    try:
        pins = load_pinned_folders(db_path)
    except ValueError:
        pins = []
        warning = "Folder settings could not be read. Open Customize\u2026 to save them again."
    try:
        templates = load_templates(db_path)
    except ValueError:
        templates = []
        warning = "Template settings could not be read. Open Customize\u2026 to save them again."
    folders, candidates = [], []
    for pin in pins:
        path = Path(pin["path"])
        missing = not path.is_dir()
        found, problem = ([], None) if missing else _scan_folder(path, time.monotonic() + SCAN_SECONDS)
        candidates.extend(found)
        folders.append({**pin, "missing": missing, "projects": found[:FOLDER_LIMIT], "more": len(found) > FOLDER_LIMIT, "warning": problem})
    resolved = {}
    unresolved = []
    for name in names:
        best = _best_candidate(name, candidates)
        if best is not None:
            resolved[name] = best["path"]
        else:
            unresolved.append(name)
    if unresolved:
        spotlight_candidates = _spotlight_matches(unresolved)
        for name in unresolved:
            best = _best_candidate(name, spotlight_candidates)
            if best is not None:
                resolved[name] = best["path"]
    return {"recent": [{"name": name, "path": resolved.get(name)} for name in names], "templates": templates, "folders": folders, "warning": warning}


def resolve_project_path(db_path, name, fallback_names=None):
    """Newest saved set matching this name (or fallbacks); explains an unplugged drive instead of guessing."""
    candidates = []
    seen = set()
    for candidate_name in [name, *(fallback_names or ())]:
        if candidate_name and candidate_name not in seen:
            seen.add(candidate_name)
            candidates.append(candidate_name)
    if not candidates:
        raise ValueError("Could not find this Live set. If it lives on an external drive, reconnect the drive and try again.")
    primary = candidates[0]
    fallbacks = candidates[1:]
    spotlight = _spotlight_matches([primary])
    best = _best_candidate(primary, spotlight)
    if best is not None:
        return best["path"]
    try:
        pins = load_pinned_folders(db_path)
    except ValueError:
        pins = []
    folder_found = []
    permission_denied = False
    for pin in pins:
        pin_path = Path(pin["path"])
        try:
            with os.scandir(pin_path):
                pass
        except PermissionError:
            permission_denied = True
            continue
        except OSError:
            continue
        found, _ = _scan_folder(pin_path, time.monotonic() + SCAN_SECONDS)
        folder_found.extend(found)
    best = _best_candidate(primary, folder_found)
    if best is not None:
        return best["path"]
    volumes, vol_denied = _external_volume_roots()
    permission_denied = permission_denied or vol_denied
    for volume in volumes:
        vol_spotlight = _spotlight_matches([primary], only_in=str(volume))
        best = _best_candidate(primary, vol_spotlight)
        if best is not None:
            return best["path"]
    if fallbacks:
        fallback_spotlight = _spotlight_matches(fallbacks)
        for fallback_name in fallbacks:
            best = _best_candidate(fallback_name, fallback_spotlight)
            if best is not None:
                return best["path"]
            best = _best_candidate(fallback_name, folder_found)
            if best is not None:
                return best["path"]
        for volume in volumes:
            vol_fallback_spotlight = _spotlight_matches(fallbacks, only_in=str(volume))
            for fallback_name in fallbacks:
                best = _best_candidate(fallback_name, vol_fallback_spotlight)
                if best is not None:
                    return best["path"]
    if permission_denied:
        raise PermissionError(
            "Tracker needs permission to access external drives. Allow Tracker in System Settings > Privacy & Security > Files & Folders, then try again."
        )
    raise ValueError(f"Could not find \u201c{primary}.als\u201d. If it lives on an external drive, reconnect the drive and try again.")


def _song_candidate_names(song: dict) -> list[str]:
    stage = song.get("stage")
    stage_type = stage if stage in ("mixing", "mastering") else None
    files = list(song.get("files") or [])
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value):
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)

    add(song.get("open_name"))
    same_stage = sorted(
        (f for f in files if f.get("stage") == stage_type),
        key=lambda f: (-float(f.get("last_seen") or 0), f.get("name") or ""),
    )
    for item in same_stage:
        add(item.get("name"))
    others = sorted(
        (f for f in files if f.get("stage") != stage_type),
        key=lambda f: (-_STAGE_FALLBACK_ORDER.get(f.get("stage"), 0), -float(f.get("last_seen") or 0), f.get("name") or ""),
    )
    for item in others:
        add(item.get("name"))
    return ordered


def resolve_song_path(db_path, song: dict, resolver=None):
    """Resolve an On Deck song, preferring open_name and falling back to other files in the song."""
    candidates = _song_candidate_names(song)
    if not candidates:
        raise ValueError("Could not find this Live set. If it lives on an external drive, reconnect the drive and try again.")
    if resolver is None or resolver is resolve_project_path:
        return resolve_project_path(db_path, candidates[0], fallback_names=candidates[1:])
    first_error = None
    for candidate_name in candidates:
        try:
            return resolver(db_path, candidate_name)
        except ValueError as exc:
            if first_error is None:
                first_error = exc
    raise first_error


def find_ableton_app():
    """Newest installed Live; None lets macOS pick the .als handler."""
    candidates = sorted(
        glob.glob("/Applications/Ableton Live *.app")
        + glob.glob("/Applications/*/Ableton Live *.app")
    )
    if candidates:
        return candidates[-1]
    if Path("/Applications/Ableton Live.app").exists():
        return "/Applications/Ableton Live.app"
    return None


def open_project(path, app_path):
    path = Path(path)
    if path.suffix.lower() != '.als':
        raise ValueError("This Live set is no longer available. Check its folder or reconnect the drive.")
    try:
        with os.scandir(path.parent):
            pass
        is_file = path.is_file()
    except PermissionError as exc:
        raise PermissionError(
            "Tracker needs permission to access external drives. Allow Tracker in System Settings > Privacy & Security > Files & Folders, then try again."
        ) from exc
    except OSError as exc:
        raise ValueError("This Live set is no longer available. Check its folder or reconnect the drive.") from exc
    if not is_file:
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
            snapshot = {"recent": [], "templates": [], "folders": [], "warning": f"Projects unavailable: {exc}"}
        with self._lock:
            self._pending = snapshot
            self._busy = False

    def take_snapshot(self):
        with self._lock:
            snapshot, self._pending = self._pending, None
            return snapshot
