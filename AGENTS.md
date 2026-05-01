# AGENTS.md

## Purpose
- Maintain a macOS Ableton activity tracker: a menu bar daemon (`menubar.py` + `tracker.py`), local dashboard server (`dashboard.py`), and SQLite state in `~/.ableton_tracker/sessions.db`.
- Optimize for runtime correctness over theoretical cleanup: preserve tracking behavior, session integrity, and dashboard/API parity.
- Make focused edits with minimal traversal; this repo is Python-first with a large single-template frontend file.
- Verify with the smallest relevant tests first, then broader checks only when needed.

## Commands
- install: `./install.command`
- dev: `python3 menubar.py`
- dev (dashboard only): `./open_dashboard.command` or `python3 dashboard.py`
- test (tracker): `python3 -m unittest -v test_tracker.py`
- test (dashboard): `python3 -m unittest -v test_dashboard.py`
- test (all): `python3 -m unittest -v test_tracker.py test_dashboard.py`
- lint/syntax check (changed files): `python3 -m py_compile tracker.py dashboard.py menubar.py`
- typecheck: `N/A (no static type checker configured)`
- build app bundle: `python3 build_app.py`
- restart runtime after code edits: `./restart.command`
- diagnostics/preflight: `python3 debug.py`

## Project map
- `tracker.py`: Core tracker daemon, Ableton detection, idle/audio logic, session writes, rollups.
- `menubar.py`: Menu bar app entrypoint, tracker thread lifecycle, pause/resume UX, dashboard launcher hook.
- `dashboard.py`: HTTP server (`:7421`), API routes, DB migrations, stats shaping, category/target endpoints.
- `templates/dashboard.html`: Main dashboard UI (CSS + HTML + JS in one file); includes Dashboard + Settings views.
- `templates/settings.html`: Settings UI fragments/styles tied to dashboard settings behavior.
- `static/js/settings.js`: Settings-side client logic utilities.
- `test_tracker.py`: Unit tests for tracker state transitions, idle/audio behavior, project switching.
- `test_dashboard.py`: Unit tests for category APIs, migrations, targets, dashboard-side data behavior.
- `debug.py`: Environment and LaunchAgent readiness checks.
- `install.command`: LaunchAgent install/bootstrap.
- `restart.command`: Safe restart path for tracker/dashboard processes.
- `open_dashboard.command`: Local dashboard runner for manual UI checks.
- `build_app.py` + `build_app.command`: macOS app bundle packaging.

## Feature entry points
- For tracker state bugs, check `tracker.py` first (`Tracker.poll_once`, state constants, session open/close paths).
- For menu bar status/pause issues, check `menubar.py` before touching tracker internals.
- For dashboard API changes, inspect `dashboard.py` handlers and payload shape before editing UI JS.
- For dashboard visuals or interactions, check `templates/dashboard.html` first; keep API contracts unchanged unless required.
- For category management, check `dashboard.py` (`ensure_category_definitions_table`, `create_category`, `update_category`, `delete_category`) and matching UI code in `templates/dashboard.html`.
- For streak or rollup regressions, check `tracker.py` (`allocate_session_activity`, `build_activity_rollups`, condensation helpers) before patching dashboard rendering.
- For install/startup problems, check `debug.py`, then `install.command`, then `restart.command`.

## Conventions
- Use `python3` for all commands and scripts.
- Keep DB contract stable: `sessions` is the source of truth; category tables are `project_categories` and `category_definitions`.
- Reuse existing endpoint patterns in `dashboard.py` (`/api/...` JSON responses, existing helper functions).
- Keep frontend logic inside existing dashboard template patterns; avoid introducing new framework/tooling layers.
- Preserve existing naming and normalization flows (for example category key normalization and uppercase hex color normalization).
- Place/extend tests in `test_tracker.py` or `test_dashboard.py` matching the edited behavior.
- Avoid full-file reads on large files; search first, then read targeted ranges.

## Token efficiency
- Search before reading: use `rg -n "pattern" <mapped files>` and open only relevant ranges.
- Never read whole large files (`dashboard.py`, `templates/dashboard.html`); read focused slices (about 50-200 lines).
- Reuse context from this file and recent command output; do not rescan the repo for known paths.
- Prefer narrow validation (`py_compile` + one relevant test file) before full test runs.
- Avoid repeating unchanged file dumps in outputs; summarize unless exact lines are required.
- Do not run duplicate discovery commands (`ls`, `rg --files`) multiple times per task unless scope changes.

## Validation
- Run minimal syntax gate on touched Python files:
  - `python3 -m py_compile <changed_python_files>`
- Run feature-specific tests next:
  - Tracker logic edits: `python3 -m unittest -v test_tracker.py`
  - Dashboard/API/category/targets edits: `python3 -m unittest -v test_dashboard.py`
- Run both suites when changing shared logic or contracts:
  - `python3 -m unittest -v test_tracker.py test_dashboard.py`
- Run runtime/preflight checks when behavior depends on local environment:
  - `python3 debug.py`
- Use restart path before manual verification:
  - `./restart.command`
- Run build only for packaging/distribution changes:
  - `python3 build_app.py`

## Change strategy
- Prefer small diffs.
- Reuse existing helpers/utilities before adding new modules.
- Do not rename or move files unless required.
- Preserve API payload shapes consumed by `templates/dashboard.html` unless the change explicitly includes both backend and frontend updates.
- Inspect shared rollup/session/category helpers before changing feature-specific logic.
- Search locally in mapped files first; avoid broad repo-wide wandering.
- Avoid editing generated/build artifacts and binary app assets unless the task is packaging or icon/build work.

## Notes for nested agents
- Treat this as the root operating guide; add nested `AGENTS.md` later for `templates/`, `static/`, or packaging-specific subflows if complexity grows.
- Keep nested guides scoped to local commands/contracts; do not duplicate root-level rules.
- In nested guides, document only delta behavior (extra validations, local conventions, file ownership boundaries).
- When nested rules conflict, prefer the most local `AGENTS.md` for that subtree.
