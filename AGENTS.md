# AGENTS.md

Start here. This is the repository-specific source of truth for coding agents. Use `CLAUDE.md` only as historical context when this file does not answer a question, and verify historical guidance against the current code before relying on it.

## Priorities

- Preserve tracking correctness and existing user data over cleanup or refactoring.
- Diagnose runtime problems before changing code. The installed LaunchAgent, a stale dashboard process, permissions, or a paused marker can make correct source code appear broken.
- Prefer focused changes that preserve existing workflows, API payloads, labels, and visual patterns unless the user explicitly asks for a redesign or contract change.
- Read enough surrounding code and tests to understand the behavior. Use targeted searches for large files, but expand the context or read a complete file when that materially improves correctness.
- Keep changes dependency-light and consistent with the current Python, SQLite, and vanilla HTML/CSS/JS architecture.

## Efficient Use Of Context And Tools

- Save tokens when the answer is clear: search for exact symbols or strings first, read only the relevant windows, and summarize repeated patterns instead of reopening identical context.
- Prefer `rg`/`rg --files` over slower broad scans, and batch independent reads, searches, and checks when possible.
- Avoid redundant existence checks after a successful edit; inspect the diff or run the relevant validation instead.
- Do not impose arbitrary read-size limits or skip necessary context. If behavior spans files, callbacks, migrations, or tests, follow the complete path needed to make a correct change.
- Keep progress updates and final responses concise, but include the evidence, validation, and caveats needed for a reliable handoff.

## What This Is

- A macOS Ableton Live activity tracker. `menubar.py` runs the rumps menu bar app, `tracker.py` records sessions, and `dashboard.py` serves the local dashboard on `127.0.0.1:7421`.
- User data lives in `~/.ableton_tracker/sessions.db`.
- The project has no package manager, frontend framework, bundler, or pytest setup. Tests use `unittest`.
- `dashboard.py` and `templates/dashboard.html` are large monolithic files. The dashboard template currently contains the active dashboard CSS and most dashboard JavaScript inline. `templates/settings.html` and `static/js/settings.js` own the settings fragment and behavior.
- `static/js/dashboard.js`, `static/js/projects.js`, and `static/css/dashboard.css` are not currently loaded by `templates/dashboard.html`. Treat them as extracted or reference copies unless current source inspection proves otherwise; do not patch them instead of the inline runtime. Mirror a change only when there is a deliberate maintenance reason.

## Commands

- Full development app: `python3 menubar.py`
- Dashboard only: `python3 dashboard.py` or `./open_dashboard.command`
- Install LaunchAgent: `./install.command`
- Restart installed runtime: `./restart.command`
- Diagnostics: `python3 debug.py` or `./debug.command`
- Package app: `python3 build_app.py` or `./build_app.command`
- Tracker tests: `python3 -m unittest -v test_tracker.py`
- Dashboard tests: `python3 -m unittest -v test_dashboard.py`
- Menu bar tests: `python3 -m unittest -v test_menubar.py`
- All tests: `python3 -m unittest -v test_tracker.py test_dashboard.py test_menubar.py`
- Syntax check changed Python: `python3 -m py_compile <changed_files>`

## Dependencies

- Runtime dependencies are installed ad hoc: `python3 -m pip install --user rumps pywebview`. The tracker also imports macOS frameworks such as `AVFoundation`, `CoreMedia`, and `ScreenCaptureKit` for audio probing.
- Pillow is needed only for icon generation in `build_app.py`: `python3 -m pip install --user Pillow`.
- Unit tests mock GUI and macOS modules and use temporary SQLite databases. Keep platform seams injectable so tests remain runnable from a non-GUI shell.
- Do not introduce a package manager, build system, frontend framework, telemetry, authentication service, or remote dependency unless the user explicitly requests it and the tradeoff is justified.

## Navigation And Runtime Ownership

- Use `rg -n` to locate symbols, routes, DOM IDs, classes, and tests before opening a narrow region of a large file. Increase the read window when control flow or shared state crosses the initial region; correctness matters more than an arbitrary line limit.
- In `dashboard.py`, useful anchors include `class Handler`, `do_GET`, `do_POST`, the exact `/api/...` route, schema helpers, and the domain helper called by the route.
- For dashboard UI, locate the visible DOM ID/class and its render/event functions in `templates/dashboard.html`. Verify whether the code is inline or loaded before editing an extracted static copy.
- For settings UI, begin with `templates/settings.html` and `static/js/settings.js`, then inspect the dashboard shell where globals or navigation are involved.
- For tracking behavior, begin with `Tracker.poll_once`, `_start`, `_tick`, `_close`, state constants, and detector helpers in `tracker.py`.
- For menu bar or app lifecycle behavior, begin with `menubar.py`; it owns pause/resume, status formatting, and dashboard process launch.
- Inspect the relevant tests before changing a contract. It is fine to read a complete test file or implementation file when the task spans broad behavior.
- Batch independent searches or reads when convenient, but do not optimize tool calls at the expense of understanding the code.

## Architecture And Data Safety

- `tracker.py` owns session lifecycle and writes the source-of-truth `sessions` rows.
- `menubar.py` owns user-facing app lifecycle, pause/resume, dashboard launch, and menu status formatting.
- `dashboard.py` owns schema migrations, rollups, local HTTP routes, static/partial serving, and dashboard mutations.
- Tests reassign `tracker.DB_PATH` and `dashboard.DB_PATH`; do not cache database connections or resolved paths globally.
- Schema setup exists in both `tracker.setup_db()` and `dashboard.run_schema_migrations()`. Keep shared table and column expectations compatible.
- Migrations must be idempotent and preserve data. Never run destructive diagnostics, tests, or migration experiments against the real database. Copy it first when destructive investigation is necessary.
- Keep API request and response fields stable unless the change explicitly includes a contract migration. Search all frontend and test consumers before changing a field.
- Time/session logic should accept explicit times or use existing helpers where possible. Avoid new wall-clock reads in deterministic logic.

## Change Guidelines

- Reuse existing CSS variables, cards, buttons, settings-shell patterns, and interaction conventions.
- Keep motion purposeful, quick, and interruptible—generally 100–180 ms for small state or spatial changes—and provide a `prefers-reduced-motion` fallback.
- Avoid cosmetic renames of states, API keys, labels, colors, or time-allocation behavior; tests and saved user data may depend on exact values.
- Keep `*.command` output understandable for users who launch scripts by double-clicking in Terminal, and preserve executable bits.
- Do not hand-edit generated `.app` bundles. Change source/build scripts and rebuild when packaging work is requested.
- Keep generated and local artifacts out of commits: `__pycache__/`, `.DS_Store`, `.history/`, `.app` bundles, local databases, and temporary diagnostic output.
- Preserve unrelated user changes in a dirty worktree. Check the diff before editing and again before handoff.

## Live Tracking Triage

If tracking is not logging or the menu bar appears stuck, establish whether the installed runtime is healthy before editing source:

- Check LaunchAgent/process state with `launchctl list | rg -i "ableton|tracker|c4milo"` and `ps aux | rg -i "menubar.py|tracker.py|dashboard.py" | rg -v rg`.
- Check whether `~/.ableton_tracker/paused` exists.
- Confirm recent database writes:
  `sqlite3 ~/.ableton_tracker/sessions.db "PRAGMA busy_timeout=3000; SELECT id, project_name, datetime(start_time,'unixepoch','localtime'), datetime(last_seen_time,'unixepoch','localtime'), end_time IS NULL, round(active_seconds/60,2) FROM sessions ORDER BY id DESC LIMIT 5;"`
- Check today's total separately:
  `sqlite3 ~/.ableton_tracker/sessions.db "PRAGMA busy_timeout=3000; SELECT round(COALESCE(SUM(active_seconds),0)/60,2) FROM sessions WHERE start_time >= strftime('%s','now','localtime','start of day','utc');"`
- Inspect `~/.ableton_tracker/menubar.log` and `~/.ableton_tracker/tracker.log`. `audio_active=unknown` alone does not mean tracking stopped; current behavior continues when the audio probe is unavailable.
- Confirm Ableton window-title visibility with `osascript -e 'tell application "System Events" to tell process "Live" to get name of every window'`. Run `python3 debug.py` when Accessibility, title parsing, database access, or LaunchAgent state is unclear.
- Treat SQLite as the source of truth for recorded minutes. The menu title uses `fmt_goal_time()`/`fmt_quarter()` and may display early activity as `0m` or a quarter-hour glyph.
- Compare current time, process start time, and the latest session's `start_time`/`last_seen_time`. Work performed before the app started cannot be reconstructed from runtime evidence; report that as a startup gap, not a database write failure.

## Validation

Validation should match the changed surface and risk. Run more than the minimum when a change crosses boundaries or affects user data.

- Tracker/session logic: `python3 -m py_compile tracker.py && python3 -m unittest -v test_tracker.py`
- Dashboard/API/schema logic: `python3 -m py_compile dashboard.py && python3 -m unittest -v test_dashboard.py`
- Menu bar logic: `python3 -m py_compile menubar.py && python3 -m unittest -v test_menubar.py`
- Dashboard markup or JavaScript behavior: run `test_dashboard.py`, serve the dashboard, and manually verify the affected interaction and browser console. Restart the installed runtime when stale processes could mask the result.
- CSS-only changes: no app build is required. Serve and inspect the affected view at relevant window sizes, including reduced-motion behavior when motion is involved.
- Runtime/startup changes: run relevant syntax/tests, then `python3 debug.py` and `./restart.command` before manual verification.
- Packaging changes: run the relevant tests and `python3 build_app.py`; inspect the resulting bundle only when packaging is in scope.
- Do not run broad builds merely as a ritual, but do not skip focused tests or live verification because a change looks small.

## Handoff

- Report what changed, what was validated, and any remaining uncertainty or manual step.
- After implementation work, include a concise `Summary title` and `Description` the user can paste into GitHub. The title should describe the completed change in one line; the description should list the implementation and validation performed.
