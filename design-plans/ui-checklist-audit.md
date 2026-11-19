# Tracker UI checklist audit

Reviewed October 25, 2026 against the 20 patterns in the supplied screenshot.
Scope: dashboard, all four Settings panes, project/date/color dialogs, menu-bar labels, and notification copy. Existing gray/blue palette and full-date Year labels were preserved.

| # | Pattern | Finding and result |
| --- | --- | --- |
| 1 | Purple-to-blue gradient | Absent from the rendered app. Blue is a solid action/data accent. Category colors remain user data. |
| 2 | Gradient hero text | Absent. Headings use solid text. |
| 3 | Emojis in headings | Removed the disc emoji from the native run menu item and menu-bar title. Kept the run count as `Run 7d` / `Run: 7 days`. State symbols still convey tracking/error status. |
| 4 | Inter everywhere | Absent. System font stack is active. The legacy `Inter` preference value already maps to the system stack for compatibility. |
| 5 | Colored border cards | Content cards have neutral borders. Accent borders are limited to selected controls, focus, and semantic state. |
| 6 | Glassmorphism cards | Absent in rendered surfaces. The shared stylesheet overrides older inline blur rules with solid panels. |
| 7 | Low-contrast dark mode | No automated text-contrast violations in the dashboard or any Settings pane in either theme at 390, 900, or 1280px. This does not certify arbitrary user-picked colors. |
| 8 | Three icon boxes in a row | Replaced the three large theme preview tiles with compact labeled choices. Also compacted the oversized weekday picker while retaining full names and selected checkmarks. |
| 9 | Badge above the headline | Absent. Tracking status conveys current app state. |
| 10 | Lucide icons everywhere | Absent. No icon-library dependency; small SVGs identify actual controls. |
| 11 | Untouched shadcn UI | Absent. Python plus vanilla HTML/CSS/JS, with no shadcn dependency. |
| 12 | Fade-in on scroll | Absent. No scroll-reveal observer or content entrance sequence. Short dialog/state transitions remain and respect reduced motion. |
| 13 | Cursor-following beam | Absent. Pointer movement is used by the functional color picker. |
| 14 | Buttons fade on hover | Absent. Hover strengthens background/border feedback without reducing opacity. Disabled controls remain visibly disabled. |
| 15 | Inconsistent spacing | Fixed mismatched scale initialization/runtime values. Card padding and section spacing now share a 24px base scaled by the saved zoom. Restored the category form's grid, field spacing, help-text spacing, and action spacing. Prevented its input stretching to the color control's height. Removed remaining overview card shadows. |
| 16 | Em dashes everywhere | Replaced the category empty-state sentence's em dash with a period. Remaining dashes are missing-data placeholders, comments, or an isolated native status separator. |
| 17 | Generic buzzword copy | Replaced “vibe-check visits” with “brief sessions.” Preserved the exact session-consolidation thresholds and guarantees. No marketing claims added. |
| 18 | Serif italic accents | Absent. Headings use the system sans stack with upright emphasis. |
| 19 | Space Grotesk + Instrument Serif | Absent. Neither font is loaded. |
| 20 | Grain over a gradient | Absent. Page background is solid. Theme swatches, chart geometry, and color-picker gradients encode actual choices/data. |

## Validation

Browser validation used a SQLite backup under `/tmp/tracker-ui-audit`, not destructive actions on the live database. Playwright checked dashboard + General + Appearance + Projects + Advanced at 1280, 900, and 390px in light/dark themes. Checks cover text contrast, horizontal overflow, solid card surfaces, theme-control dimensions, category-input height, Year keyboard navigation, command search/Escape, invalid goal feedback, keyboard theme selection, stable button hover opacity, zoom at 85/100/120%, reduced motion, and color-picker Escape.

Repeatable session evidence: `/tmp/tracker-ui-audit/verify.cjs`, `verification.json`, and `after-*.png`. Run `node /tmp/tracker-ui-audit/verify.cjs` while the isolated source server is running on port 7422. Its server setup is saved in `/tmp/tracker-ui-audit/serve.py` and uses SQLite's backup API.

Dashboard: 56 existing tests. Native menu: existing refresh-label contract updated before implementation, then all 46 menu tests passed. After restart, macOS Accessibility inspection read the live title as `○ 0m Run 3d`; LaunchAgent remained loaded. JavaScript syntax and whitespace checks run. The packaged app was rebuilt from source, its signature/plist checked, and served assets compared with source. The running packaged dashboard also passed compact-control, input-size, weekly/project-dialog, and JavaScript-console checks. The live tracker was restarted successfully. Diagnostics: 10 passed, zero failures; warnings were Ableton not running and the already occupied dashboard port before restart.

The Impeccable mechanical scanner ran in degraded regex mode because its optional parser modules are unavailable. Its remaining reports concern older overridden styles, an `--ease-spring` variable whose effective value is `ease-out`, and existing data/progress transitions. Rendered browser checks supply the contrast/layout evidence; a regex scan is not treated as runtime proof.

Live audio/hardware tracking and destructive data-management actions are outside this visual audit.
