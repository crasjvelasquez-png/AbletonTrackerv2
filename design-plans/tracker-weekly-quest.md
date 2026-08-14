# Consolidate weekly progress into one Weekly Quest

Written against: `f22d8408417e4d8ff8f61a218d59b44afe8422c6`

## Evidence chain

- Surface: Tracker Dashboard, `templates/dashboard.html`, current `#dashboard` view.
- Problem: `Weekly Pace`, `Today's Required`, and `Activity` render overlapping interpretations of the same week, splitting the primary game loop across three cards.
- Design evidence: rendered Tracker Dashboard; `render(data)` at `templates/dashboard.html:9032`; weekly target render/load path at `templates/dashboard.html:9753` and `templates/dashboard.html:9891`; activity model at `templates/dashboard.html:5574`.
- Owner: `templates/dashboard.html` consumes `/api/weekly-target`; `dashboard.py:get_weekly_target()` owns the target contract.
- Scope and affected surfaces: Dashboard weekly cards, week navigation, weekly-goal editing, activity visualization, weekly-target detail modal, related inline CSS and JavaScript.
- Uncertainty: none. The existing weekly API and `year_daily` data are sufficient; no schema or API expansion is required.

## Design decision

Replace the three weekly surfaces with one `Weekly Quest` that presents the weekly goal, logged progress, next checkpoint, remaining time, and seven daily activity nodes. Preserve historical week navigation and goal editing, but reveal editing only on deliberate request. This creates one legible engagement loop and removes guilt-oriented duplicate copy.

## Reuse

- `weeklyTargetState`, `get_weekly_target()`, `/api/weekly-target`, `renderWeeklyTargetCard()`, `loadWeeklyTarget()`, `fmt.dur()`, `targetTone()`, `weekStartDayName()`, `weekEndDayName()`.
- Existing CSS variables: `--accent`, `--live`, `--ink`, `--ink-3`, `--border`, `--surface`, `--control-bg`.
- Exemplar: existing current-week navigation and seven-day data mapping inside `renderWeeklyTargetCard()`.

No new shared primitive or dependency is required. Keep the implementation in the active inline template.

## Changes

1. `templates/dashboard.html` — replace the weekly markup in `render(data)`
   - Change: remove the separate `Weekly Pace`, `Today's Required`, and `Activity` containers and mount one `Weekly Quest` container with week navigation, one progress track, checkpoint row, daily-node row, and collapsed edit region.
   - Preserve: previous/next/current week behavior, historical targets, error/empty states, selected week label, and the rest of Dashboard order.
   - Verify: only one primary weekly panel is visible and navigating weeks refreshes every value inside that panel together.
2. `templates/dashboard.html` — build a deterministic quest presentation model
   - Change: derive checkpoints at 25%, 50%, 75%, and 100% of `goal_hours`; expose the next unmet checkpoint, exact time to it, overall remaining time, completion state, days remaining, and seven daily totals from the existing payload and `weeklyTargetState.dailyRows`.
   - Preserve: exact stored goal value and recorded seconds. Do not award points or alter time totals.
   - Verify: zero goal, zero activity, partial progress, exactly-on-checkpoint, over-goal, historical week, and current week states produce stable copy without negative durations.
3. `templates/dashboard.html` — replace obligation copy and streamline goal editing
   - Change: use `Weekly Quest`, `Next checkpoint`, `Quest complete`, `Logged`, and `Remaining`. Remove `Today's Required`, `Behind by`, and `needed per remaining day`. Hide the number field and preset chips until the existing goal value/edit affordance is activated; save through the existing endpoint.
   - Preserve: presets `10h`, `20h`, `30h`, `40h`, input validation, loading state, and saved-value confirmation.
   - Verify: view mode exposes one clear objective; edit mode is reversible, keyboard operable, and does not navigate or open the detail modal accidentally.
4. `templates/dashboard.html` — add causal, restrained progress feedback
   - Change: remember the last rendered week/progress in page memory; animate only a newly crossed checkpoint after initial render. Use an interruptible, critically damped visual settle approximated with existing CSS transitions (`transform` and `opacity`, 150–350 ms), plus a persistent filled checkpoint and label.
   - Preserve: instant data correctness, active polling, reduced-motion support, and quiet rendering while values are unchanged.
   - Verify: initial load, polling with identical data, week navigation, and page refresh do not replay completion; increased progress crossing one or several checkpoints produces one bounded response.
5. `test_dashboard.py` and live Dashboard
   - Change: extend source-contract tests for one Weekly Quest mount, removed duplicate labels, retained weekly endpoint/navigation hooks, and reduced-motion rules. Add pure JavaScript model coverage only if the repository already has a supported JS-test seam; otherwise keep calculation small and validate through rendered fixtures/manual states.
   - Preserve: `DashboardWeeklyTargetTests`, daily-target independence, Friday-to-Thursday/custom week starts, and all API response fields.
   - Verify: run `python3 -m unittest -v test_dashboard.py`; serve Dashboard; inspect current, previous, empty, complete, and over-goal weeks; confirm a clean console.

## Scope

- Inherit: Tracker Dashboard weekly progress and weekly-target detail presentation.
- Verify: card spacing at `1280x860`, minimum app size `900x650`, narrow viewport, light/dark themes, increased UI scale, reduced motion.
- Exclude: daily-goal API behavior, Planner goals, notifications, database migrations, project rankings, session correction, and production-run rules.

## Validation

- Product: start or simulate an Ableton session and confirm recorded time advances the quest without double-counting or requiring a manual action.
- Interface: test no goal, no activity, partial, checkpoint, complete, over-complete, current week, historical week, load error, and save error.
- System: confirm the implementation reuses `/api/weekly-target` and does not introduce a competing weekly-progress store.
- Repository: `python3 -m unittest -v test_dashboard.py && git diff --check` -> all tests pass and no whitespace errors.

## Stop conditions

- Stop if the active Tracker build no longer loads `templates/dashboard.html`, if weekly progress is owned by a newer surface, or if consolidation would remove historical-week access.

## Design documentation

- After acceptance and validation: record that Tracker represents weekly engagement through one truthful `Weekly Quest` using real recorded time and fixed percentage checkpoints. Destination: future Tracker surface brief or `DESIGN.md` when one exists.

