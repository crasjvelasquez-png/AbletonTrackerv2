# AGENTS.md

Start here. This is the repo-specific, compact source of truth for future coding agents; use `CLAUDE.md` only as historical backup if this file is missing context.

## ⚠️ CRITICAL: TOKEN SAVING RULES
- **Large File Handling:** `dashboard.py` and `dashboard.html` are >4,000 lines. **NEVER** `read_file` these in their entirety. 
- **Targeted Reading:** Use `grep -n` to find line numbers first, then use `sed` or targeted tool calls to read only the necessary blocks (max 200 lines at a time).
- **No Redundant Verification:** Do not run `ls` or `cat` to verify a file exists after you have just edited it. Trust your internal state.
- **Concise Responses:** Skip preambles and "I have updated the file" summaries. Only provide the code or the confirmation.
- **Commit-Ready Handoff:** At the end of every implementation-plan execution, include a concise `Summary title` and `Description` that the user can paste into GitHub when committing. The title should describe the completed change in one line; the description should briefly list the implementation and validation performed.
- **Testing:** Only run `build_app.py` or specific tracking tests when logic changes. Do not run for CSS/UI-only tweaks.

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
- Runtime dependency is ad hoc: `python3 -m pip install --user rumps pywebview`. `rumps` brings PyObjC; `pywebview` powers the embedded dashboard window; tracker also imports macOS frameworks such as `AVFoundation`, `CoreMedia`, and `ScreenCaptureKit` for audio probing.
- Build-only dependency: `python3 -m pip install --user Pillow` for `build_app.py` icon generation.
- Unit tests mock the GUI/macOS modules and use tempfile SQLite DBs, so they should run from a non-GUI shell with only stdlib plus importable project code.

## Token-Saving Navigation
- Do not read all of `dashboard.py` or `templates/dashboard.html`; each is about 5k lines. Grep first, then read 50-200 line windows around matches.
- Best starting greps: `class Handler`, `def do_GET`, `def do_POST`, endpoint strings like `/api/data`, DOM IDs/classes, or JS function names.
- For settings UI, inspect `templates/settings.html` and `static/js/settings.js` before reading the dashboard monolith.
- For tracking bugs, start at `Tracker.poll_once`, state constants, `_start`, `_tick`, `_close`, and the detector helpers in `tracker.py`.
- For menu bar bugs, start in `menubar.py`; it wraps tracker state and lazily starts `dashboard.py` as a subprocess.
- Do not verify existence by rereading files you just edited; trust tool results and run the targeted syntax/test command instead.
- **Prefer `grep` over `read` for code lookup** — locate functions/classes by pattern search rather than reading files incrementally.
- **Batch parallel tool calls** — send independent reads, greps, and glob searches in a single message instead of sequentially.
- **Delegate multi-file exploration** — use the `task` tool with `subagent_type="explore"` for broad codebase research rather than chaining individual grep/read calls.
- **Test file discipline** — grep test files for the specific test name/class first, then read only the relevant 30-60 line window. Never read entire test files.

## Live Tracking Triage
- If the user says tracking is not logging or the menu bar shows no minutes, verify the installed runtime before editing code. Check `launchctl list | rg -i "ableton|tracker|c4milo"`, `ps aux | rg -i "menubar.py|tracker.py|dashboard.py" | rg -v rg`, and whether `~/.ableton_tracker/paused` exists.
- Confirm real DB writes with `sqlite3 ~/.ableton_tracker/sessions.db "PRAGMA busy_timeout=3000; SELECT id, project_name, datetime(start_time,'unixepoch','localtime'), datetime(last_seen_time,'unixepoch','localtime'), end_time IS NULL, round(active_seconds/60,2) FROM sessions ORDER BY id DESC LIMIT 5;"`.
- Check today's total separately: `sqlite3 ~/.ableton_tracker/sessions.db "PRAGMA busy_timeout=3000; SELECT round(COALESCE(SUM(active_seconds),0)/60,2) FROM sessions WHERE start_time >= strftime('%s','now','localtime','start of day','utc');"`.
- Inspect recent runtime logs with `tail -n 120 ~/.ableton_tracker/menubar.log` and `tail -n 120 ~/.ableton_tracker/tracker.log`. `audio_active=unknown` does not by itself mean tracking is stopped; current behavior continues tracking when the audio probe is unavailable.
- Confirm Ableton title visibility with `osascript -e 'tell application "System Events" to tell process "Live" to get name of every window'`. If this fails, run `python3 debug.py` to check Accessibility, Ableton detection, title parsing, DB access, and LaunchAgent state.
- Interpret the menu title carefully: `menubar.py` renders the title with `fmt_goal_time()`/`fmt_quarter()`, so early time may show `0m` or quarter-hour glyphs rather than exact minutes. Use SQLite as the source of truth before concluding minutes are not logging.
- Compare the current time, process start time, session `start_time`, and `last_seen_time`. If the menu-bar app launched after the user's work began, earlier minutes cannot be recovered from runtime evidence and should be reported as a startup/runtime gap, not a write failure.

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
