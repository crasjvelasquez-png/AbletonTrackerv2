# AGENTS.md

## Purpose
- macOS Ableton activity tracker: menu bar daemon (`menubar.py` + `tracker.py`), local dashboard server (`dashboard.py`), SQLite state in `~/.ableton_tracker/sessions.db`.
- Python-first repo with a single-file frontend template (`templates/dashboard.html`). No framework, no bundler.
- Optimize for runtime correctness over theoretical cleanup: preserve tracking behavior, session integrity, and dashboard/API parity.

## Commands
- Install: `./install.command` (creates LaunchAgent)
- Dev (full): `python3 menubar.py`
- Dev (dashboard only): `./open_dashboard.command` or `python3 dashboard.py` (port 7421)
- Test (tracker): `python3 -m unittest -v test_tracker.py`
- Test (dashboard): `python3 -m unittest -v test_dashboard.py`
- Test (menubar): `python3 -m unittest -v test_menubar.py`
- Test (all): `python3 -m unittest -v test_tracker.py test_dashboard.py test_menubar.py`
- Syntax check: `python3 -m py_compile <changed_files>`
- Build: `python3 build_app.py` or `./build_app.command`
- Restart runtime: `./restart.command`
- Diagnostics: `python3 debug.py` or `./debug.command`
- Uninstall: `./uninstall.command`

## Dependencies
- **Runtime**: `rumps` (menu bar framework). Install: `python3 -m pip install --user rumps`. Bundles `PyObjC`; also imports `AVFoundation`, `CoreMedia`, `ScreenCaptureKit` (macOS-only audio probe).
- **Build only**: `Pillow`. Install: `python3 -m pip install --user Pillow`. Used by `build_app.py` for icon generation.
- No `requirements.txt`, `setup.py`, or `pyproject.toml`. Dependencies are ad-hoc pip installs.

## Project map
| File | Lines | Role |
|------|-------|------|
| `tracker.py` | ~1100 | Core daemon: Ableton detection, idle/audio logic, session writes, rollups, DB setup |
| `menubar.py` | ~340 | Menu bar entrypoint: rumps app, tracker thread, pause/resume, dashboard launcher |
| `dashboard.py` | ~4500 | HTTP server (:7421): API routes, DB migrations, stats, category/target endpoints |
| `templates/dashboard.html` | ~4700 | Single-file frontend: CSS + HTML + Vanilla JS (Dashboard + Settings views) |
| `templates/settings.html` | ~215 | Settings UI fragments served as partials |
| `static/js/settings.js` | ~650 | Settings-side client logic |
| `test_tracker.py` | ~565 | Tracker state transitions, idle/audio, project switching, session condense |
| `test_dashboard.py` | ~565 | Category APIs, migrations, targets, rollovers |
| `test_menubar.py` | ~580 | Menu bar formatting, tracker thread, pause/resume/quit, dashboard process |
| `debug.py` | — | Pre-flight diagnostic (AppleScript, rumps, PyObjC, LaunchAgent checks) |
| `*.command` | — | Shell wrappers (install, restart, debug, build, uninstall, open_dashboard) |
| `build_app.py` | — | App bundle packaging with rounded-corner icon |
| `engineering_handoff.md` | — | Design rationale and historical context |

## Database
- Path: `~/.ableton_tracker/sessions.db`
- Tables: `sessions` (source of truth), `project_categories`, `category_definitions`, `daily_metrics`, `app_settings`
- Schema migrations: `dashboard.run_schema_migrations()` — run on dashboard startup and after any schema change
- Category key normalization: `normalize_category_key()` lowercases, strips non-alphanumeric, collapses whitespace to hyphens
- Color normalization: uppercase hex

## Feature entry points
- **Tracker state bugs**: `tracker.py` → `Tracker.poll_once`, state constants (`STATE_*`), session open/close paths
- **Menu bar status/pause**: `menubar.py` before touching tracker internals
- **Dashboard API**: `dashboard.py` handlers → check payload shape → then edit `templates/dashboard.html` JS
- **Category management**: `dashboard.py` (`ensure_category_definitions_table`, `create_category`, `update_category`, `delete_category`) + `templates/dashboard.html` UI
- **Streak/rollup**: `tracker.py` (`allocate_session_activity`, `build_activity_rollups`, condensation helpers)
- **Install/startup**: `debug.py` → `install.command` → `restart.command`

## Testing quirks
- All tests use tempfile SQLite DBs; no real DB needed
- `test_menubar.py` mocks the entire `rumps` module via `sys.modules['rumps']` — don't expect real GUI imports
- `test_tracker.py` mocks `AppKit`, `AVFoundation`, `CoreMedia`, `ScreenCaptureKit` — tests run without macOS GUI
- Test discovery is via `unittest`; no pytest configuration present

## Conventions
- Use `python3` for all commands and scripts
- `templates/dashboard.html` is a monolith — never read it whole; search first, read 50-200 line slices
- `dashboard.py` is also large — same rule
- Reuse existing `/api/...` endpoint patterns; keep JSON payload shapes stable
- Preserve category key and color normalization logic
- Place/extend tests in the matching test file (`test_tracker.py`, `test_dashboard.py`, `test_menubar.py`)
- Never rename or move files
- Never edit `.app` bundles or build artifacts unless packaging

## Validation flow
1. `python3 -m py_compile <changed_files>`
2. Run the relevant test suite(s)
3. If behavior touches runtime environment: `python3 debug.py`
4. Before manual verification: `./restart.command`

## Additional references
- `CLAUDE.md` — complementary notes with line-count context and API/data reference summaries
- `.claude/settings.local.json` — Claude-specific permissions (ignore for OpenCode)
