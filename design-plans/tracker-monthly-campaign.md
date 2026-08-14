# Replace fragmented project statistics with a Monthly Campaign

Written against: `f22d8408417e4d8ff8f61a218d59b44afe8422c6`

## Evidence chain

- Surface: Tracker Dashboard selected-month summaries, category chart, Projects table, and Recent Entries boundary.
- Problem: `Top Project` and `This Month` use selected-month data, while the numbered Projects table uses all-time totals; the category chart repeats the same monthly allocation in a separate visual. One screen therefore presents two incompatible ranking periods.
- Design evidence: month rollup in `dashboard.py:get_stats()` (`month_per_project` and `project["month_seconds"]`); top cards at `templates/dashboard.html:9152`; category panel at `templates/dashboard.html:9218`; Projects table at `templates/dashboard.html:9226`; selected-month allocation tests at `test_dashboard.py:1738`.
- Owner: `dashboard.py:get_stats()` owns project/month allocation; `templates/dashboard.html:render(data)` and `renderCategoryChart()` own presentation.
- Scope and affected surfaces: selected-month API data, top-project/month/category/projects region, month navigation response, project rank motion, Recent Entries boundary.
- Uncertainty: project aliases must be canonicalized consistently. Reuse the same canonical name mapping as the all-time project query; do not expose duplicate alias campaigns.

## Design decision

Replace the two month summary cards, category chart, and all-time Projects table with one `Monthly Campaign` ranked by recorded time in the selected month. Each row shows rank, movement from the previous calendar month, time, share, category, and live state. Keep Recent Entries as the factual correction ledger directly below it.

## Reuse

- `parse_month_key()`, `allocate_session_activity()`, `month_per_project`, project alias/category metadata, selected-month navigation, `projectColor()`, `projectBadge()`, `fmt.dur()`, `selectedMonthLabel()`.
- Existing tokens: `--accent`, category colors, `--live`, `--surface`, `--border`, `--ink`, `--ink-3`, `--control-bg`.
- Exemplar: selected-month allocation assertions in `DashboardRolloverTests.test_selected_month_uses_allocated_time_within_that_month`.

Extend the existing project row contract rather than introducing a new endpoint. The API already returns selected-month project values and is the shared refresh owner.

## Changes

1. `dashboard.py:get_stats()` — compute comparable campaign periods
   - Change: allocate canonicalized project activity for the selected month and its immediately preceding calendar month; sort positive selected-month projects deterministically by seconds descending then display name; add `month_rank`, `previous_month_rank`, `rank_delta`, `previous_month_seconds`, `month_share_percent`, and `is_live_project` to project rows.
   - Preserve: `month_seconds`, all-time `total_seconds`, aliases, category metadata, current/future month clamping, cross-midnight allocation, and zero-activity project rows for non-campaign consumers.
   - Verify: new, rising, falling, unchanged, tied, alias-merged, historical-month, and month-boundary projects return stable values. Use `None` for no previous rank rather than a fabricated movement value.
2. `test_dashboard.py` — lock the monthly campaign contract
   - Change: extend selected-month tests across two adjacent months; assert ranks, movement direction, share totaling approximately 100%, deterministic ties, canonical alias aggregation, live-project marking, and no movement for projects absent from the prior month.
   - Preserve: existing month totals and per-project allocated seconds.
   - Verify: test fixtures include a cross-month session and at least one project changing rank.
3. `templates/dashboard.html:render(data)` — render one Monthly Campaign
   - Change: remove the standalone `Top Project`, `This Month`, `By category`, and all-time `Projects` surfaces. Render a single campaign header with selected month total/project count, followed by ranked active-month rows containing non-zero-padded rank, project name, time, share, category marker, and `Up N`, `Down N`, `New`, or unchanged state.
   - Preserve: month navigation, full project names/tooltips, theme/category colors, empty state, and Recent Entries immediately after the campaign.
   - Verify: changing month reranks the entire campaign and never leaves stale top-project/category/all-time values on screen.
4. `templates/dashboard.html` — add restrained live/rank states and clean obsolete code
   - Change: mark the live Ableton project with a persistent filled active icon and accent edge. Animate rank changes only after deliberate month navigation, using transform/opacity transitions no longer than 250 ms; skip first-load and polling animations. Remove obsolete category-chart, top-project, rank-table, and duplicate detail bindings/CSS after proving no remaining consumer.
   - Preserve: reduced-motion fallback, keyboard readability, category color meaning, and stable row geometry for long names.
   - Verify: polling does not reorder-animate unchanged data; reduced motion switches immediately; no dead selectors or console errors remain.
5. `templates/dashboard.html` and live Dashboard — preserve the correction boundary
   - Change: ensure `Load older entries` belongs to Recent Entries rather than the removed Projects surface; keep notes, row deletion, live-session protection, and data cleanup controls outside the campaign game layer.
   - Preserve: Recent Entries payload, event bindings, pagination cursor, confirmation dialogs, and destructive-action behavior.
   - Verify: loading older entries changes only Recent Entries and never affects the campaign period/rank.

## Scope

- Inherit: Tracker selected-month project analytics.
- Verify: project aliases, categories, live project, month navigation, Recent Entries pagination, light/dark themes, long project names.
- Exclude: Planner project statuses/tasks, public leaderboards, cross-user comparison, XP, project renaming, category editing, and session mutation.

## Validation

- Product: compare API campaign totals with SQLite allocated session totals for current and historical months; confirm they reconcile exactly.
- Interface: inspect empty, one-project, many-project, new-rank, tie, live-project, long-name, uncategorized, historical-month, and narrow-width states.
- System: confirm `get_stats()` remains the single project/month owner and no parallel campaign endpoint or persistence is introduced.
- Repository: `python3 -m py_compile dashboard.py && python3 -m unittest -v test_dashboard.py && git diff --check` -> syntax and tests pass with no whitespace errors.

## Stop conditions

- Stop if alias canonicalization cannot be reconciled with existing totals without a wider data-correctness change. Stop before changing session rows, saved metadata, or API fields used by Planner.

## Design documentation

- After acceptance and validation: record that Tracker project ranking is always scoped to the selected month, compares against the immediately previous calendar month, and uses truthful recorded-time allocation. Destination: future Tracker surface brief or `DESIGN.md` when one exists.

