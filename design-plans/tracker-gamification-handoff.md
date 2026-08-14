# Tracker gamification coding-agent handoff

Written against: `f22d8408417e4d8ff8f61a218d59b44afe8422c6`

## Outcome

Turn Tracker into a restrained game loop built from truthful Ableton activity:

`production session -> visible progress -> checkpoint -> personal record -> return to Ableton`

The implementation is split into three product plans and one final integration pass. Each product agent owns one root concept; do not scatter individual symptoms across agents.

## Execution order

1. **Agent 1 — Weekly Quest**
   - Execute `design-plans/tracker-weekly-quest.md` first.
   - This establishes the primary dashboard hierarchy and removes the largest duplicated region.
2. **Agent 2 — Production Run**
   - Execute `design-plans/tracker-production-run.md` after Agent 1 is merged.
   - Rebase before editing because both plans touch the top of `render(data)` in `templates/dashboard.html`.
3. **Agent 3 — Monthly Campaign**
   - Execute `design-plans/tracker-monthly-campaign.md` after Agents 1 and 2 are merged.
   - Preserve the Weekly Quest, Production Run, and Recent Entries markup exactly.
4. **Agent 4 — Integration verifier**
   - Make no feature additions. Run the full dashboard validation and inspect the complete Tracker surface at `1280x860`, `900x650`, and a narrow browser viewport.
   - Fix only merge regressions, duplicated CSS, broken selectors, stale detail bindings, overflow, or missing reduced-motion states.

## Shared product rules

- Reward recorded production time, never app opens, dashboard clicks, or cosmetic actions.
- Use real time, goals, ranks, checkpoints, and personal records; do not introduce fictional XP, currency, loot, badges, or random rewards.
- Never punish a missed day with red failure styling, lost-progress animation, shame copy, or notifications from this surface.
- While Ableton is actively recording, keep the dashboard visually quiet. Achievement feedback occurs when Tracker is deliberately viewed or a completed session refreshes the page.
- Preserve `~/.ableton_tracker/sessions.db`, current API fields, row-level session correction, notes, deletion, month history, week history, themes, and reduced-motion behavior.

## Merge-risk map

| Area | Primary owner | Other agents must preserve |
| --- | --- | --- |
| Weekly goal rendering and activity | Agent 1 | Agents 2 and 3 |
| Streak computation and top-left summary | Agent 2 | Agent 3 |
| Month rollups and project/category region | Agent 3 | Integration agent |
| Recent Entries and destructive data actions | Existing product | All agents |

## Final acceptance checklist

1. The first viewport answers three questions without scrolling: **Is tracking active? What is my current run? How far is the weekly quest?**
2. No weekly fact is rendered in more than one primary panel, and no project ranking mixes all-time totals with the selected month.
3. Polling or reloading does not replay milestone, rank, or run-completion animations.
4. All reward states have persistent non-motion cues and remain understandable with `prefers-reduced-motion: reduce`.
5. `python3 -m unittest -v test_dashboard.py` passes, the browser console is clean, and `git diff --check` reports no errors.

## Stop conditions

- Stop and re-route these plans if the implementation branch already contains the standalone `Tracker.app` / `Planner.app` split described outside this checkout. Reconfirm the active inline Tracker template before applying selectors or markup changes.
- Stop if a proposed change requires destructive migration of `sessions`, changes recorded durations, or reinterprets existing session rows.
- Stop if concurrent agents are editing `templates/dashboard.html`; sequence or rebase the work instead of resolving a shared-worktree conflict blindly.

