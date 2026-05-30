# AGENTS.md

Start here. This is the repo-specific, compact source of truth for future coding agents; use `CLAUDE.md` only as historical backup if this file is missing context.

## What This Is
- macOS Ableton Live activity tracker: `menubar.py` runs the rumps menu bar app, `tracker.py` records sessions, `dashboard.py` serves a local dashboard on `127.0.0.1:7421`, data lives in `~/.ableton_tracker/sessions.db`.
- Python + SQLite + vanilla HTML/CSS/JS only. There is no README, manifest, lockfile, CI config, package manager, frontend framework, bundler, or pytest setup.
- Preserve runtime tracking correctness and existing user data over cleanup/refactors.

## Commands
- Full dev app: `python3 menubar.py`
- Dashboard only: `python3 dashboard.py` or `./open_dashboard.command`
- Install LaunchAgent: `./install.command`
- Restart after runtime changes: `./restart.command`
- Diagnostics: `python3 debug.py` or `./debug.command`
- Package app: `python3 build_app.py` or `./build_app.command`
- Tracker tests: `python3 -m unittest -v test_tracker.py`
- Dashboard tests: `python3 -m unittest -v test_dashboard.py`
- Menu bar tests: `python3 -m unittest -v test_menubar.py`
- All tests: `python3 -m unittest -v test_tracker.py test_dashboard.py test_menubar.py`
- Syntax check changed Python: `python3 -m py_compile <changed_files>`

## Dependencies
- Runtime dependency is ad hoc: `python3 -m pip install --user rumps`. It brings PyObjC; tracker also imports macOS frameworks such as `AVFoundation`, `CoreMedia`, and `ScreenCaptureKit` for audio probing.
- Build-only dependency: `python3 -m pip install --user Pillow` for `build_app.py` icon generation.
- Unit tests mock the GUI/macOS modules and use tempfile SQLite DBs, so they should run from a non-GUI shell with only stdlib plus importable project code.

## Token-Saving Navigation
- Do not read all of `dashboard.py` or `templates/dashboard.html`; each is about 5k lines. Grep first, then read 50-200 line windows around matches.
- Best starting greps: `class Handler`, `def do_GET`, `def do_POST`, endpoint strings like `/api/data`, DOM IDs/classes, or JS function names.
- For settings UI, inspect `templates/settings.html` and `static/js/settings.js` before reading the dashboard monolith.
- For tracking bugs, start at `Tracker.poll_once`, state constants, `_start`, `_tick`, `_close`, and the detector helpers in `tracker.py`.
- For menu bar bugs, start in `menubar.py`; it wraps tracker state and lazily starts `dashboard.py` as a subprocess.
- Do not verify existence by rereading files you just edited; trust tool results and run the targeted syntax/test command instead.

## Architecture And Data
- `tracker.py` owns session lifecycle and writes the `sessions` source-of-truth rows.
- `menubar.py` owns user-facing app lifecycle, pause/resume, dashboard launch, and menu status formatting.
- `dashboard.py` owns schema migrations, stats, local HTTP API routes, static/partial serving, and dashboard mutations.
- Frontend is hash-based SPA navigation in `templates/dashboard.html` with views for dashboard/projects/settings; settings fragments are in `templates/settings.html`, and settings behavior is in `static/js/settings.js`.
- Database path is `~/.ableton_tracker/sessions.db`; tests reassign `tracker.DB_PATH` and `dashboard.DB_PATH`, so do not cache DB connections or resolved paths globally.
- Important tables: `sessions`, `project_categories`, `category_definitions`, `daily_metrics`, `app_settings`.
- Schema setup exists in both `tracker.setup_db()` and `dashboard.run_schema_migrations()`; keep expectations in sync when adding shared columns.
- Migrations must be idempotent and preserve user data. Never delete or rewrite the real DB in diagnostics/tests/scripts; copy it first for destructive investigation.

## API Entry Points
- Main route class: `dashboard.Handler`; extend the existing `do_GET`/`do_POST` branching style and `_json` response conventions.
- Core GET routes: `/api/data`, `/api/daily-target`, `/api/weekly-target`, `/api/app-settings`, `/api/project-list`, `/api/project-report`, `/api/last-session-todos`, `/api/project-report/download`.
- Core POST routes: `/api/session-notes`, `/api/project-category`, `/api/category-options`, `/api/category-options/update`, `/api/category-options/delete`, `/api/daily-target`, `/api/weekly-target`, `/api/app-settings`, cleanup/delete routes.
- Keep API payload field names stable; `templates/dashboard.html` and `static/js/settings.js` may both consume them.

## Feature Entry Points
- Categories: `dashboard.py` functions `normalize_category_key`, `normalize_hex_color`, `create_category`, `update_category`, `delete_category`, plus category UI in `templates/dashboard.html` and `static/js/settings.js`.
- Goals/settings: `ensure_daily_metrics_table`, `ensure_app_settings_table`, daily/weekly target helpers in `dashboard.py`, plus settings fragment/script.
- Session notes/todos/reports: `dashboard.py` note/report helpers and routes; settings report UI lives in `templates/settings.html` + `static/js/settings.js`.
- Streaks/rollups: `tracker.allocate_session_activity`, `tracker.build_activity_rollups`, `day_seconds`, `week_seconds`, `compute_streak_days`.
- Audio/idle detection: `tracker.py` detector helpers and macOS framework seams; tests mock these paths, so preserve injectability.
- Install/startup behavior: `install.command`, `restart.command`, `uninstall.command`, `debug.py`; consider LaunchAgent behavior separately from user DB data.
- Packaging/icon: `build_app.py`, `icon_source.png`, generated app/icon artifacts. Do not hand-edit `.app` bundles.

## Conventions That Matter
- Use `python3` and `unittest`; there is no pytest config.
- Keep shell wrapper output readable because users double-click `*.command` files in Terminal; preserve executable bits.
- Do not add package managers, build systems, telemetry, auth services, remote dependencies, or frontend frameworks unless explicitly requested.
- Reuse existing CSS variables/card/button/settings shell patterns. No generated bundles and no npm.
- Avoid cosmetic renames of states, API keys, labels, colors, and time-allocation behavior; tests assert many exact values.
- Time/session logic should take explicit times or use existing helpers where possible; avoid new wall-clock calls in deterministic logic.
- Generated/local artifacts to leave out of commits: `__pycache__/`, `.DS_Store`, `.history/`, `.app` bundles, local DBs, temporary debug output.

## Validation Shortcuts
- Tracker/session change: `python3 -m py_compile tracker.py && python3 -m unittest -v test_tracker.py`
- Dashboard/API change: `python3 -m py_compile dashboard.py && python3 -m unittest -v test_dashboard.py`
- Menu bar change: `python3 -m py_compile menubar.py && python3 -m unittest -v test_menubar.py`
- Frontend-only change: no build required; run `python3 dashboard.py` for serve sanity if needed.
- Runtime/startup/package change: run the relevant py_compile/tests, then `python3 debug.py`; use `./restart.command` before manual runtime verification.
