# Richer native menu-bar panel

Written against: 83629485ee18a55cb1ef02a374d6b19b0b87a3ce
Status: implemented, built, signed and restarted. Latest accepted refinement: flatter macOS system surfaces, compact menu rows and Streak wording in the panel and fallback menu.

## Evidence chain

- Surface: AbletonTrackerApp in menubar.py:257; initialization builds a rumps menu, _refresh supplies values, callbacks own actions.
- Requested improvement: adopt the attached reference's rounded container, layered charcoal surfaces, stronger numeric hierarchy, quiet outlines and generous spacing while preserving functionality.
- Design evidence: user-provided Screenshot 2026-10-27 at 8.52.22 PM.png. This is inspiration, not a screenshot of the current app. Current appearance was not inspected live.
- Owner: menubar.py. Dashboard CSS does not style this native menu.
- Existing compact title: _build_title shows the state badge and today's time, without a goal or streak. Preserve it.
- Existing design-plans/menubar-mockups/prompts.txt contains older speculative concepts with Pause, Yesterday, extra timers and expanded status titles. Those are not the current behavior and must not be implemented from those mockups.
- Explicit exceptions: None documented.
- Validation scope: native anchoring, focus, keyboard shortcuts, original project-menu display and installed dashboard launch verified. Pinned-folder callbacks retain their existing native tree; actual nested-folder selection and real Ableton launching were not exercised in the installed app.

## Design decision

Replace the opened text menu's presentation with a compact AppKit NSPopover using existing Python/PyObjC integration. Keep rumps lifecycle and all existing data/action owners. Use opaque tonal surfaces, not wallpaper-dependent blur. This is a native presentation change, not a CSS-only restyle. Do not add SwiftUI, a frontend framework or a second server.

Implemented visual specification: 320 pt wide with 16 pt content padding. Use a single opaque native window background, semantic macOS label/secondary-label/separator colors and the system accent for active status. Remove nested metric cards and solid action-button fills. Use fine dividers, 32 pt action rows and six-point hover rounding. System font: 12 pt captions, 13 pt status/actions, 14 pt semibold app title and up to 22 pt tabular totals. Open Project, conditional Open Ableton and Open Tracker use full-width rows; Quit sits below a separator. Keyboard focus appears after keyboard navigation, with native focus color. Light/dark appearance follows macOS. The compact menu-bar title and its badge remain intact.

Panel composition, top to bottom:
1. Small Ableton Tracker heading and status indicator; exact existing status message underneath, with wrapping for long project/error text.
2. One activity card: Today and This week in equal columns, with existing fmt_goal_time outputs intact. A Streak row below uses _update_streak's current wording. Preserve goal fractions; do not invent a new timer, chart or statistic.
3. Full-width Open Project control with chevron, opening the existing native project menu. Preserve Recent, Pinned Folders, nested folders, missing/empty/loading/warning states and Customize….
4. Open Ableton appears only under the current _update_open_ableton condition. Keep hidden in error state, matching current behavior.
5. Action rows: Open Tracker and separated Quit, with current command-D / command-Q behavior. Do not add Settings, Pause, Yesterday, tabs or the reference's unrelated system metrics.

## Reuse

Reuse _refresh's current values and five-second cadence, _build_title, _set_badge, fmt_goal_time, _update_streak, ProjectCatalog snapshots, open_ableton, open_dashboard, customize_projects, _open_project and quit_app. Keep error handling and project launching intact.

A new menubar_panel.py presentation module is justified because standard rumps text MenuItems cannot express the reference's card layout and independently styled totals. It should own only AppKit views, layout, presentation state and callbacks. It must not read SQLite, poll Ableton or calculate totals. Existing NSMenu project entries remain the sole project action tree.

## Changes

1. Prove the native shell before restyling.
   - Inspect installed rumps/AppKit bindings and build_app.py's resource packaging. Prototype status-button anchoring, transient dismissal and opening the existing project submenu from the panel.
   - Click toggles open/closed; Escape and outside click dismiss; a project submenu must not close its parent prematurely. Reopening cannot create a duplicate tracker or dashboard.
   - Keep the existing menu as a fallback if popover initialization fails. Do not silently accept broken native behavior.
2. Implement presentation in menubar_panel.py.
   - Build the specified hierarchy using native controls and system typography; support light/dark appearance, constrained screen height and long text without clipping actions.
   - Provide quiet hover/pressed/focus states. Any custom transition is 100–180 ms and honors reduced motion. No animated totals or pulsing indicators.
3. Connect menubar.py with minimal changes.
   - Feed the panel the same display strings/state used by existing menu items, including the early error-return path. Refresh open panels on the existing cadence and initialize on opening.
   - Route every control to its existing callback. Preserve project-menu refresh at its existing cadence and stale/missing-path alerts. Keep command shortcuts operational while the panel is active without introducing global hotkeys.
   - Keep pause behavior hidden, startup pause-marker clearing, notifications, single-instance locking, process ownership and quit cleanup unchanged.
4. Verify and package.
   - Add any necessary automated scenarios before implementation, not afterward. Prefer a repeatable isolated native E2E harness with synthetic display data; never mutate the real sessions database.
   - Include the new module through build_app.py if its file list requires it. Build from source; never edit the generated bundle.

## Scope

- Inherit: opened menu-bar panel only.
- Verify: status button, native project menus, dashboard launch, Ableton launch, quit and packaging.
- Exclude: tracker.py, schema, data migrations, totals/rounding logic, dashboard appearance, new features and dependency changes.

## Validation

Product: cover tracking, Ableton open without a project, Ableton closed, idle-paused, paused and error; zero/nonzero Run; goals absent/present/exceeded; long names; all existing project-menu states. Confirm exact current strings, values, visibility and actions. Dismiss before launching another app/window as appropriate without dropping the action.

Interface: inspect actual native output in light/dark mode and Retina scaling; repeated open/close, Escape, outside click, keyboard traversal, command-D/Q, nested submenu navigation and a screen with limited available height. Preserve readable focus and labels. A browser mockup is a design aid, not native runtime proof.

Repository: run python3 -m py_compile menubar.py menubar_panel.py and python3 -m unittest -v test_menubar.py; run project-catalog tests discovered in the checkout for menu integration. Run git diff --check. If packaging changes, run python3 build_app.py. Run python3 debug.py and ./restart.command, then verify the installed runtime and absence of duplicate processes.

Repeatable evidence: save native screenshots for tracking, closed and error states in both appearances, plus a short action/dismissal recording and a checklist with commands/results under a dated verification artifact directory. Record source commit, app path and macOS version. Separate synthetic-state evidence from real runtime proof.

## Stop conditions

If the installed rumps bridge cannot support stable anchoring/submenus, resolve a minimal AppKit status-item adapter before proceeding. Do not widen into rewriting lifecycle, tracking or dashboard. If dependency or deployment-target changes are required, document the concrete constraint before changing the architecture.

## Design documentation

After implementation acceptance, record final measured dimensions, palette and native interaction decisions in this plan. No other design documentation changes required.


## Implementation evidence

- Files: menubar_panel.py, menubar.py, build_app.py; presentation consumes existing strings and callbacks, with the original native menu retained as a failure fallback.
- Python compilation and git diff --check passed. All 50 menu-bar unit tests passed. verify_projects.py passed five integration checks against a temporary database.
- scripts/verify_menubar_panel.py passed seven native integration groups and saved six dark/light screenshots, covering status action toggling, long error wrapping, goal totals, conditional Open Ableton, button dispatch, command-D/Q, Escape, Tab/shift-Tab, native callback identities and fallback restoration. Display fixtures and recording callbacks do not launch Ableton or touch the user's database.
- Built and signed dist/Tracker.app using python3 build_app.py; ./restart.command completed. SHA-256 hashes of menubar.py and menubar_panel.py match the installed bundle. One tracker process remains running.
- Installed native panel visually inspected. Existing project menu and missing-file messages displayed, Tab moved focus correctly, and Open Tracker dismissed the panel and opened the actual Tracker window.
- Artifacts: /tmp/ableton-panel-verification/results.json, projects.json, installed.json, installed-panel.png and six synthetic-state screenshots.
- Repeat fixture verification: python3 scripts/verify_menubar_panel.py --output /tmp/ableton-panel-verification. Real installed screenshots and action checks require a desktop session.


## Flat native refinement

- User requested a flatter, more integrated macOS appearance and renamed Run to Streak.
- Removed custom palette, card fills and filled buttons in favor of semantic AppKit colors, separators and compact menu rows.
- Updated the existing streak-label expectation before implementation; all 50 menu-bar tests pass.
- Native fixture verification passed all seven groups with six dark/light screenshots under /tmp/ableton-panel-flat-verification.
- Repeat: python3 scripts/verify_menubar_panel.py --output /tmp/ableton-panel-flat-verification.

- Rebuilt, signed and restarted the installed bundle after this refinement. Verified matching installed source bytes and inspected the actual native panel showing Streak; one tracker process is running. Installed capture: /tmp/ableton-panel-flat-verification/installed-panel.png.
