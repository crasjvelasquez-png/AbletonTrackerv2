# Compact, scannable Planner Finder cards

Written against: `05de2f867f90292bd6073305ec027ffaf219be30`

## Evidence chain

- Surface: Planner → Folders → Cards view, rendered by `renderPlanner()` in `templates/dashboard.html`.
- Problem: supplied rendered evidence shows sparse folder and project cards roughly 340px tall, with status, counts/duration, and the action trigger treated as separate full-width rows. The action trigger reads as ordinary body copy.
- Design evidence: card mode currently inherits the Finder table-row grid and only relocates its children (`templates/dashboard.html:3596-3601`). The same Planner’s maintained board-card treatment establishes compact 11×12px padding, 8px gaps, muted metadata chips, restrained depth, and an intentionally distinct folder surface (`templates/dashboard.html:3445-3475`).
- Owner: `templates/dashboard.html`: `renderPlanner()` owns the Finder card markup and the `.finder-*` rules own its layout and styling.
- Scope and affected surfaces: Finder Cards view for folders and projects in the inline Dashboard template; list view, Planner board, folder/project data, menus, drag-and-drop, and API payloads remain consumers to preserve.
- Uncertainty: validate the card at its minimum 230px grid width with a multi-line project title and with missing status, duration, dates, and folder counts. No product-data change is required.

## Design decision

Give Finder Cards their own three-zone composition instead of adapting the table row: a compact header (selection, type icon, title, overflow menu), a quiet status line, and a bottom metadata line. Replace the text `Actions` disclosure label with a familiar three-dot overflow affordance while preserving the existing `<details>` menu and its commands. Reuse the Planner board’s folder-only dashed, accent-tinted treatment so folders are distinguishable before reading their count.

## Reuse

- Existing tokens: `--surface`, `--surface-2`, `--surface-hover`, `--accent-soft`, `--accent`, `--border`, `--border-strong`, `--ink`, `--ink-2`, `--ink-3`, `--ink-4`.
- Existing Finder ownership: `.finder-items.is-cards`, `.finder-item`, `.finder-item-name`, `.finder-item-menu`, `plannerFolderIcon`, and `plannerProjectIcon`.
- Exemplar: compact Planner board cards at `templates/dashboard.html:3445-3475`, especially `.planner-board-project.is-folder`.
- No shared primitive is needed: this presentation is specific to the Finder’s card variant, while its list and board presentations intentionally have different structure.

## Changes

1. `templates/dashboard.html` — give the Finder Cards variant a compact, semantic card layout.
   - Change: adjust the card branch emitted by `renderPlanner()` to group each item’s title/action header, status, and metadata into dedicated Finder-card elements. In CSS, make `.is-cards .finder-item` a compact, content-height layout with 10–12px padding and 8px internal gaps; do not retain the list row’s per-field grid placement. Keep the checkbox, title button, all existing status/time/date values, and drag target behavior.
   - Preserve: list-view grid and styling, card selection (`.is-selected`), drag states, title opening behavior, search-path subtitle, menu placement above adjacent cards, 230px responsive grid minimum, and the existing reduced-motion rule.
   - Verify: the screenshot’s empty folder and finished project become content-height cards with title, status, and metadata visually grouped; a long title wraps without covering controls; empty data uses the existing copy rather than creating blank spacer rows.

2. `templates/dashboard.html` — move item actions into the header as an overflow control.
   - Change: retain `.finder-item-menu` as the existing `<details>` owner and retain its menu buttons/data attributes, but replace the visible `Actions` text in its `<summary>` with an inline three-dot icon. Keep the current accessible name, `Actions for <item name>`, on the summary. Position it in the header’s trailing edge; style its compact hover, open, and focus-visible states with the existing Finder tokens.
   - Preserve: the current command set and command wiring (`Move To…`, edit/new/delete folder, merge/manage projects), click-away behavior native to `<details>`, keyboard activation, menu z-index, and destructive-action wording.
   - Verify: the icon is visible and focusable in light/dark themes; opening it exposes exactly the current context menu; the title neither overlaps nor becomes less clickable; keyboard focus retains an obvious outline.

3. `templates/dashboard.html` — visually differentiate folders only in Finder Cards.
   - Change: add a folder-specific class when `item.folder` is rendered, then scope a Finder Cards modifier to that class. Reuse the board exemplar’s dashed border and subtle `color-mix` accent surface; retain the existing folder SVG rather than introducing a new icon or color system. Project cards stay neutral.
   - Preserve: folder selected, dragged, and drop-target states; nested-folder navigation; project card styling; all status/time/count content; high-contrast and reduced-transparency behavior.
   - Verify: a folder remains recognizably distinct in neutral, selected, hovered, and drop-target states without competing with the selected-state accent; projects do not gain a dashed border.

4. `test_dashboard.py` and manual Dashboard verification — protect the rendered contract.
   - Change: add focused template contract assertions for the Finder card grouping hooks, folder-only class, overflow summary accessible label/icon hook, and the existing menu command data attributes. Do not add browser automation or alter backend tests.
   - Preserve: the complete current dashboard test suite and all planner API contracts.
   - Verify: use representative data covering an empty folder, a finished project, long project/folder names, a project with date/status/duration, selected card, and a nested folder; inspect Cards at desktop, approximately 650px, and below 650px in light/dark plus `prefers-reduced-motion`.

## Scope

- Inherit: every Finder folder/project shown when the user selects `Cards` in the Planner.
- Verify: Finder list view remains row-oriented; Planner board keeps its current implementation; context-menu actions, drag/drop, selection, and status/date values remain unchanged.
- Exclude: new status definitions, data/schema/API changes, new action commands, new icons/assets, dashboard-wide card redesign, board redesign, and animation beyond existing hover behavior.

## Validation

- Product: open Planner → Folders → Cards and confirm folders navigate, projects open their owner, selection works, drag/drop works, and all menu commands still invoke their current flows.
- Interface: compare a zero-content folder and a finished 13-minute project against the supplied screenshot; confirm the new layout reads header → status → metadata and has no large blank interior. Check long names, missing values, selected/drop-target cards, light/dark, narrow layout, reduced motion, increased contrast, and reduced transparency.
- System: verify all new styling is scoped to `.finder-items.is-cards`; the list view and `.planner-board-project` must not receive Finder-card rules.
- Repository: `python3 -m unittest -v test_dashboard.py && git diff --check` → dashboard tests pass and no whitespace errors.

## Stop conditions

- Stop if the active Dashboard no longer serves `templates/dashboard.html`, if a menu behavior depends on text content rather than the existing accessible label/data attributes, or if the compact layout causes a card control to overlap at the established 230px minimum width. Resolve the proven owner/layout constraint before widening scope.

## Design documentation

- After acceptance and validation: none. This reuses established Planner card styling rather than introducing a new product-wide design rule.
