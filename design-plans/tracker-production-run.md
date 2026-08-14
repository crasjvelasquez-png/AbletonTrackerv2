# Replace the passive streak with a qualifying Production Run

Written against: `f22d8408417e4d8ff8f61a218d59b44afe8422c6`

## Evidence chain

- Surface: Tracker Dashboard streak summary and streak detail.
- Problem: the current streak increments for any nonzero daily activity and displays only a count, personal best, and binary seven-day bars; it does not show what qualifies today or make advancement causal.
- Design evidence: streak computation in `dashboard.py:get_stats()` around the `active_days` loop; rendered card at `templates/dashboard.html:9138`; detail path `renderStreakDetail()` and `data-detail="streak"`; rollover tests in `test_dashboard.py:1610`.
- Owner: `dashboard.py:get_stats()` owns daily rollups and current streak; `templates/dashboard.html` owns the rendered run.
- Scope and affected surfaces: `/api/data` summary extension, Production Run card/detail, run animation, rollover tests.
- Uncertainty: the proposed qualifying threshold is a product choice. Use a constant of 15 recorded minutes per local calendar day for this implementation; do not expose another setting in this pass.

## Design decision

Preserve legacy `summary.streak_days` for compatibility and add a separate `summary.production_run` contract. A day qualifies after 15 minutes of allocated recorded activity. The UI explains today's progress, preserves the midnight grace rule, shows the current run and personal record, and rewards threshold crossing once without punishing missed days.

## Reuse

- `build_activity_rollups()`, `allocate_session_activity()`, `daily_totals`, current local-date handling, `year_daily`, `fmt.dur()`, existing streak detail modal and seven-day visual footprint.
- Existing tokens: `--accent`, `--live`, `--ink`, `--ink-3`, `--border`, `--surface`.
- Exemplar: the existing `streakTrackDays` construction and `DashboardRolloverTests` frozen-date pattern.

Add a backend helper because the existing zero-threshold streak contract cannot express qualifying progress, best qualifying run, or today's threshold state without duplicating logic between API and frontend.

## Changes

1. `dashboard.py` — introduce one pure Production Run calculator
   - Change: add `PRODUCTION_RUN_QUALIFYING_SECONDS = 900` and a helper that accepts daily totals plus an explicit `today`; return `current_days`, `best_days`, `qualifying_seconds`, `today_seconds`, `today_qualifies`, `today_progress_ratio`, `remaining_seconds`, and the seven most recent day states.
   - Preserve: allocation by local day, cross-midnight correctness, the existing after-midnight grace where yesterday's run remains current until today qualifies, and legacy `streak_days` semantics/field.
   - Verify: no activity, partial today, exact threshold, above threshold, consecutive qualifying days, broken run, best run in history, and post-midnight grace are deterministic.
2. `dashboard.py:get_stats()` and `test_dashboard.py`
   - Change: expose the helper result as `summary.production_run`; add focused unit tests using temporary databases and frozen dates, including a cross-midnight session whose allocated fragments independently qualify or do not qualify.
   - Preserve: existing `summary.streak_days` tests, API keys, database schema, ETag behavior, and session rows.
   - Verify: Production Run totals derive only from allocated activity and never mutate persisted data.
3. `templates/dashboard.html` — replace the Current Streak card with Production Run
   - Change: render `Production Run`, `Day N`, today's `X / 15m`, `N days to your record` or `New record`, and seven connected day nodes with completed, partial-today, missed, and future states. Keep the top-left footprint established by the current card.
   - Preserve: keyboard/detail activation, personal-best context, current theme, and responsive card layout.
   - Verify: the card explains exactly how today's run advances without opening the detail modal; singular/plural and zero states read naturally.
4. `templates/dashboard.html` — update detail and feedback behavior
   - Change: rewrite the existing streak detail as Production Run history using the same contract. On a live transition from below 15 minutes to qualified, fill today's node and briefly settle the run value; store previous threshold state in page memory so polling does not replay it.
   - Preserve: `prefers-reduced-motion`, modal close/focus behavior, visible static completion state, and no sound.
   - Verify: initial load and reload are static; threshold crossing responds once; missing a day uses neutral styling and never shakes, drains, or flashes red.
5. `test_dashboard.py` and live Dashboard
   - Change: add template contract assertions for Production Run labels/hooks, seven-node states, removed Current Streak copy, and the reduced-motion fallback.
   - Preserve: all existing dashboard tests and legacy API consumers.
   - Verify: `python3 -m unittest -v test_dashboard.py`; manually inspect 0-day, partial-today, qualified-today, active record, broken run, light/dark, and reduced-motion states.

## Scope

- Inherit: Tracker Production Run card and detail only.
- Verify: menu-bar or notification consumers of `summary.streak_days` remain unchanged.
- Exclude: notifications, user-configurable thresholds, freezes/streak protection, XP, badges, social comparison, Weekly Quest calculations, and Planner goals.

## Validation

- Product: add sessions below, at, and above 15 minutes on controlled dates and confirm the run advances only on qualifying local days.
- Interface: verify partial progress is legible, record distance never becomes negative, and the run is understandable without animation.
- System: confirm one backend helper owns qualification and the frontend does not recalculate history from raw rows.
- Repository: `python3 -m py_compile dashboard.py && python3 -m unittest -v test_dashboard.py && git diff --check` -> syntax and tests pass with no whitespace errors.

## Stop conditions

- Stop if any current consumer relies on redefining `summary.streak_days`; add the new object without changing that field. Stop if the active Tracker surface is no longer the inline template.

## Design documentation

- After acceptance and validation: record the 15-minute local-day qualification rule, midnight grace, neutral failure treatment, and compatibility boundary for `streak_days`. Destination: future Tracker surface brief or `DESIGN.md` when one exists.

