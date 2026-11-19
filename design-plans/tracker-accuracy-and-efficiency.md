# Tracker accuracy, strict inactivity, and efficiency plan

Status: implemented and installed on 2026-10-27. Automated, HTTP, migration, and packaged runtime checks passed. See `tracker-accuracy-verification.md` for measured results and the remaining controlled audio/hardware qualification.
Audited 2026-10-27 at commit `8ef59f6`, macOS 15.8.1, Python 3.13.
Objective: count evidenced Ableton work, stop unattended counting predictably, explain every pause, and minimize background work.

## Findings verified against current code

1. **Inactivity is permissive by design.** `tracker.py:1689` checks global HID idle, without checking which application received input. Activity in a browser can keep an open Live project counting. `is_audio_active()` falls back to a display-wide ScreenCaptureKit filter (`tracker.py:1027`), so unrelated audio can count. Unknown audio prevents idle pause indefinitely; failed HID reads also keep ticking. Several existing tests explicitly require these behaviors. This explains plausible overcounting, but does not establish when a regression occurred.
2. **Playback has no attendance limit.** OSC playing or audible output can keep a session alive indefinitely. A running loop is not evidence of an attentive listener. Resume from idle starts the remembered project before resolving the current title, potentially crediting the wrong project for a cycle. Initial title resolution chooses the first plausible title even when multiple candidates exist (`get_project_name`).
3. **Accounting has failure-path errors.** `_tick()` advances its time watermark before its transaction succeeds: a failed write loses elapsed time on retry. `_close()` adds an uncapped interval since the last tick, unlike `_tick()`'s 60-second cap. A delayed close can credit an unobserved gap. The wake grace explicitly suppresses an idle pause after a long gap. On this Mac Python's monotonic clock uses `mach_absolute_time()`; do not claim every sleep interval is currently counted. Explicit sleep handling is still needed to prevent stale evidence and wall-clock discontinuities.
4. **The actual loop runs every 5 seconds plus probe duration.** `menubar.py:41,161` overrides the nominal 30-second tracker interval. It repeatedly spawns `pgrep`, `osascript`, and, when needed, an audio helper; OSC can add a 0.5-second timeout, and capture can take up to 5 seconds. These operations hold the same lock used by menu status reads. Menu refresh also recomputes daily/weekly/history totals every 5 seconds. Every tracker construction checks/possibly compiles Swift; repeated compilation failures have no cooldown. A standalone tracker does not share the menu app's instance lock.
5. **Recorded totals lack exact temporal evidence.** `_start()` reopens old rows across gaps, while `allocate_session_activity()` spreads active seconds uniformly across their full span. Totals can remain correct while hour/day attribution is wrong. `dashboard.py` also physically merges sessions; new evidence records must survive that path. Existing cleanup intentionally deletes closed Untitled/short sessions: preserve that contract in this project rather than silently changing it.

### Audit verification already completed

All 61 existing `test_tracker.py` tests passed in a temporary database, with `_ensure_audio_probe_binary` stubbed to avoid compiling into the real user directory. No live capture was started. Isolated controlled-clock probes produced:

```text
HID idle=3600s, audio=None, open known project -> state=tracking
_close after 300s without a tick -> adds 300 active seconds
initial titles=['Plugin Editor', 'Real Song'] -> chooses 'Plugin Editor'
failed _tick at t=1405 -> watermark advances to 1405
successful retry at t=1410 -> credits 5 seconds instead of 10
```

Source and installed `tracker.py` hashes matched. An installed menu process and dashboard were running; no paused marker existed. Recent tracker logs showed roughly five-second idle checks. A point-in-time process CPU reading was 0.0%; this is **not** a capture-overhead benchmark or proof about Live audio glitches. Live behavior, capture permissions, MIDI activity, and actual OSC replies remain unverified.

## Required counting policy

These are recommended product defaults, not facts that can be inferred from macOS. The user's request for stricter accountability motivates the finite limits. Software cannot prove attention; use observable evidence and disclose limits.

1. **Active work:** count only a reliably identified Live project with recent qualifying input delivered while Live owns focus. Qualifying input is a key press, mouse button, drag with a button held, or scroll; bare pointer motion and merely bringing Live forward do not reset attendance. Input to Live-owned plugin windows counts after ownership is verified. Keep the existing 30-second inactivity allowance. The effective cutoff is `last_qualifying_input + 30s`, not the time a slow poll finishes. This allowance is counted and must be described plainly.
2. **Listening or performing:** fresh Live transport-playing or Live-only output can extend counting, while Live remains foreground, until **120 seconds after the last qualifying Live input**. Label this time “Listening” rather than “Working.” Repeated audio samples, looping, a permanently armed track, and a playing transport do not reset that deadline. A stopped transport must not suppress Live-only audio evidence from live instruments. Do not add a MIDI hook merely to ship this phase; long hands-off playing/recording beyond two minutes will pause and this limitation must be visible. Future verified MIDI input can reset attendance, but requires separate validation.
3. **Away or paused:** stop at the focus-loss event when another app becomes foreground; never borrow that app's HID activity or audio. Sleep, user-session deactivation/lock, Live exit, and manual pause stop immediately at the observed event boundary. No wake grace and no automatic resume from audio alone. Resume only after fresh qualifying Live input and current project resolution. A transition back to Live can arm detection but cannot itself credit time.
4. **Missing evidence:** unknown is neither silence nor activity. Fresh qualifying input may still support the normal 30-second allowance when audio is unavailable. Audio failure cannot extend it. Cached project identity gets at most 10 seconds of grace during a transient lookup failure for the same Live PID and no switch evidence; repeated failures do not renew grace. With no reliable project at startup, count nothing. Loss of input/focus observability stops at the earlier of the existing evidence deadline or 10 seconds after detection loss. Surface “Paused — cannot verify activity.” Never backfill the unknown interval on recovery.
5. **Explain the decision:** preserve existing state strings and API fields; add optional reason, evidence source, observation time, cutoff, and policy version. Show “Working,” “Listening — pauses in …,” “Paused — no Live input for 30s,” “Paused — listening limit reached,” “Paused — another app is active,” or “Paused — cannot verify activity.” Use existing menu/dashboard status surfaces. Goals use committed seconds; do not animate beyond a verified cutoff. No guilt messages or new reminder prompts.

Evidence expiration: playback/audio observations are valid for at most 10 seconds without successful refresh. Silence ends a listening extension when observed; never infer continuous silence between sparse samples. For a 1-second audio sample, publish its actual start/end and completion timestamps. Reject late responses from a previous PID/project/generation. If a first negative observation cannot establish the exact stop time, stop at observation time, within the maximum five-second fallback detection latency; disclose this bound.

## Execution order and completion gates

### 1. Freeze the contract and build the replay harness first

Estimate: 2–3 hours. Files: `test_tracker.py`, a new `scripts/verify_tracker_policy.py`, fixture files under `tests/fixtures/tracker/`.

Write failure cases before implementation: false activity from other apps, pointer jitter, unattended loops, unknown/stale signals, permission loss, wrong project on resume, delayed/out-of-order callbacks, sleep/wake, wall-clock jumps, failed/ambiguous commits, duplicate writers, missing helper, and shutdown during capture. Include long MIDI performance as a documented unsupported-observation case, not an invented success.

Build a deterministic replay driver that feeds observations and explicit wall/monotonic times through the real tracker, persistence, and summary paths using a temporary DB. Use simulated adapters only at the macOS boundary; do not call this live macOS E2E. Inject DB and helper paths, compile stub, clocks, and observations before constructing Tracker. Restore patched module globals after each test. Never access the user's real DB from the harness.

Each run writes `manifest.json` (revision, policy, environment), `observations.jsonl`, `decisions.jsonl`, `expected.json`, `actual.json`, and `sessions.sqlite` under a caller-selected temporary output directory. Add invariant checks: no overlap/double credit, no negative duration, no credit after cutoff, and session total equals credited intervals. Record expected baseline failures before changing behavior. Replace the tests that deliberately demand indefinite fail-open behavior with the specified finite policy; retain unrelated coverage.

Gate: baseline failures reproduce the verified bugs and policy gaps; existing unrelated tests remain green. The verification script exits nonzero on any mismatch and prints its artifact directory.

### 2. Implement one evidence-driven accounting path

Estimate: 4–6 hours. Primary files: `tracker.py`, then `menubar.py` lifecycle integration.

Introduce an immutable observation snapshot and a small decision function with explicit timestamps, Live PID, project confidence, focus/input freshness, transport and audio tri-states, and source errors. Pass a single snapshot through each decision. Separate effective event time from acquisition completion time. Keep existing public state strings, adding explanatory fields instead of renaming payloads.

Use one interval-credit operation for tick, cutoff, pause, switch, and close. Credit only the intersection of elapsed awake time and valid policy evidence. Settle at the actual deadline even when delivered late. Do not replace the uncapped close with an arbitrary 60-second cap: split at evidence expiry and lifecycle boundaries. Keep monotonic duration accounting separate from wall timestamps; on clock jumps split the wall mapping without adding elapsed wall time. Do not infer continuous work across a missed observation window.

Advance persisted watermarks only after commit. Give each credited interval a stable identity so retry after an ambiguous commit is idempotent. Reconcile externally closed/deleted sessions without recreating them. Re-resolve project on resume; ambiguous startup titles must not select the first window. For resumed rows require nonnegative gaps within the configured limit. Preserve Unsaved-to-saved rename and existing cleanup semantics.

Move writer ownership into a common lock held before schema setup/stale-session cleanup, used by standalone and menu runtimes. A second writer must exit without changing any rows. Do not reuse the same non-reentrant file lock twice. Stop/join cleanly without holding status locks across worker shutdown; rename the thread's `_stop` attribute so it does not shadow `threading.Thread._stop()` when joining.

Gate: replay passes exact timing/accounting cases, injected DB failures lose or duplicate zero eligible seconds, and a second process cannot close the first process's session.

### 3. Replace expensive polling with bounded native observations

Estimate: 4–8 hours plus hardware checks. Files: `tracker.py`, `menubar.py`, narrowly scoped new detector module if useful, and `build_app.py` only for the helper.

Use `NSWorkspace` notifications for process launch/termination, activation, sleep/wake and session activation changes on the correct notification center. Verify lock detection separately: display sleep is not equivalent to a locked session. Cache the current Live PID. Try AX notifications for title/focused-window changes; record unsupported registrations and retain bounded direct AX refresh. If AX cannot work reliably, use the existing AppleScript adapter with timeout and error backoff. Do not install a high-frequency global event tap or collect key contents.

For qualifying input, prototype a native event monitor that records only event category, timestamp and current application ownership. Check permissions on this Mac before choosing it. If unsupported, use per-event-type CoreGraphics counter deltas at five-second cadence, accepting input only when Live held focus throughout the entire sampling interval. Reject ambiguous focus-crossing intervals; disclose reduced precision. Bare all-event HID idle cannot implement the stricter contract alone. Coalesce input callbacks to one pending update; do no SQLite work in callbacks.

Keep OSC optional. Validate padded address/type tags/payload rather than searching for `,T`/`,F`; support documented boolean and 0/1 integer encodings, reject malformed/unrelated packets, and validate sender. One owner holds the response socket; do not steal port 11001 from another app. Verify installed AbletonOSC version and routing first: upstream sends to a fixed response port, so choosing an ephemeral local port alone will not fix conflicts. Prefer state-change subscriptions with a five-second health/query refresh only while needed, and invalidate after ten seconds without a successful reply. Unavailable OSC backs off at 5, 15, 30, then 60 seconds, resetting on Live relaunch. Never change transport or project state to detect it.

If transport is false/unknown and passive evidence is needed, sample only the current Live application with ScreenCaptureKit. Never fall back to all-system audio. Stop probing when Live is absent, backgrounded, paused, or the 120-second attendance allowance expires. Treat missing displays, zero samples, format errors, and permission denial as unavailable. Fix the Swift buffer handling to allocate the required AudioBufferList size, check OSStatus/sample format, and synchronize callback aggregation before reading results. Keep the existing threshold initially; do not present -70 dBFS as a universally reliable noise floor.

First benchmark corrected on-demand sampling at most once per five seconds against a single reusable helper/capture stream scoped to the listening window. Ship the cheaper reliable option based on aggregate CPU/wakeups including capture services, not just Python. Make capture cancellable and asynchronous; status reads must not wait behind a 0.5–5-second probe. No overlapping probes. Package/precompile the helper, or use a source-hash cache with compilation outside startup and exponential failure cooldown. Preserve developer fallback without requiring compiler availability for basic tracking.

Use a five-second lightweight fallback check while Live is foreground, 30–60-second reconciliation while closed/backgrounded with native notifications active, and single-shot deadline timers. Without working native lifecycle notifications, retain five-second lightweight lifecycle detection to meet latency bounds. Cache historical rollups/settings until committed-data revision, external DB change, setting change, local midnight, or timezone change. Refresh visible status from immutable snapshots. Flush eligible credit at most every five seconds and immediately at a boundary; no unchanged-state writes, recurring cleanup scans, or per-poll log spam when paused.

Gate: no routine `pgrep`/`osascript` on the healthy native path, no capture while ineligible, bounded retries, no main-thread stalls, and the identical replay suite still passes.

### 4. Persist explainable intervals and connect existing summaries

Estimate: 4–6 hours. Files: both schema owners, tracker rollups, dashboard merge/delete/export paths, existing menu/dashboard status rendering.

Add a versioned interval table with stable ID, session ID, start/end wall timestamps, credited duration, reason, evidence source and policy version. Store spans at transitions and update their committed endpoint on the five-second flush; do not store audio or individual key events. Create session aggregates and interval updates in the same transaction. Index session/time lookup. Include short pauses as evidence even though existing focus-break reports suppress pauses under 60 seconds.

For new covered sessions, derive day/hour allocations from actual credited intervals rather than uniformly spreading across idle gaps. Preserve `sessions.active_seconds` as the compatible aggregate. For pre-upgrade rows, retain existing totals/estimated allocation and label historical precision; do not fabricate evidence. Start a new covered row after upgrade instead of reopening a legacy row, allowing presentation-level condensation to preserve grouping. If this changes row IDs exposed by the API, test notes/todos/session selection explicitly.

Enumerate all session mutations before migrating: dashboard physical merge must reparent interval rows transactionally; delete/cleanup must remove associated evidence; rename must preserve it. Keep external-close behavior. Migration runs twice without data changes; backup and rollback are tested on a copied database. Preserve old rows, notes, todos, totals, and state/API fields. Shared schema setup must not mutate on normal reads.

Show the policy and current reason in existing status/detail areas with one short explanation: “Counts Live interaction plus up to 30s of inactivity; Live playback extends this to 2m after your last interaction. Switching apps pauses.” State that long hands-off performances need renewed interaction under this default. Expose missing permission/source information as a recoverable status. Do not add modes or settings until the fixed policy is verified.

Gate: midnight/gaps/merge/delete fixtures reconcile exactly; menu and dashboard agree with committed SQLite seconds; render and exercise changed status states at small and normal window sizes without console errors.

### 5. Qualify end to end and package only after the gates pass

Estimate: 2–3 hours automated/integration work, plus at least 60 minutes of live A/B measurement.

Run syntax checks and each existing unittest suite in a separate process against temporary databases, then the replay artifact command. Run `git diff --check`. Do not claim simulated observations validate permissions or actual Live behavior. For installed validation, build with `python3 build_app.py`, run `python3 debug.py` after inspecting its side effects, and restart with `./restart.command`; compare source/bundle hashes and verify only one writer. Back up the live SQLite DB with SQLite's backup API before deploying schema changes. Do not restore an old backup over new work during rollback: stop the writer, preserve the current DB, and use the tested compatible rollback path.

Live scenario checklist (record timestamps, expected cutoff, recorded delta, visible status, and source health):

1. Work in Live, stop touching controls, and leave transport stopped: cutoff at +30s; pointer-only motion does not reset it. Type/scroll in another app with unrelated music playing: zero additional counted time after focus loss. Return to Live without input: stay paused; then make a qualifying interaction: resume within five seconds.
2. Listen to Live playback without input for three minutes: display Listening and stop at +120s. An indefinite loop cannot resume it. Repeat with transport stopped and a monitored instrument. Document long MIDI takes as limited by the current observation policy; do not accidentally label them as fully supported.
3. Deny/fail audio, title and input detection independently; disconnect OSC, occupy its port, kill the helper, and recover. No indefinite counting, retry storm, unrelated-audio fallback, stale project restart, or frozen menu. Include plugin windows and a real project switch during pause.
4. Sleep/lock/wake, quit/relaunch, terminate the tracker abruptly, and attempt a second writer. Verify no sleeping/locked interval is credited, no wake-only resume, no double writer, and crash loss is at most the five-second flush window. Hardware tests use a dedicated test set; never manipulate the user's open set without coordination.
5. Run matching ten-minute baseline/candidate traces for Live closed, foreground editing, and idle playback (60 minutes total), with the same set/device/buffer settings. Capture process CPU seconds, child helper CPU, available capture-service CPU/wakeups, process launches, probe latency, write counts, log growth, memory, and Live overload/dropout observations. Store raw measurements and a calculated report with the revision and environment. Do not infer causation from one audio glitch.

Proposed performance acceptance targets, to be measured rather than claimed now: no capture when ineligible; zero steady-state shell subprocesses on the native path; less than 0.5% of one core average for tracker plus its helpers in closed/editing states; no CPU/wakeup regression in listening; at least 50% fewer subprocess launches than baseline; no added repeatable Live dropouts. Baseline near zero cannot support a percentage-reduction claim—report absolute CPU seconds. Track capture-service cost separately when system-wide attribution is ambiguous. If a target fails, profile the cause and report it; do not loosen accuracy to pass.

Timing acceptance: exact simulated cutoffs to 1ms; native lifecycle transitions visible within 1s, fallback transitions within 5s; persisted totals exclude time beyond the effective cutoff even if the UI observes it later. For fallback-only focus detection, stop conservatively at the last known foreground observation and disclose up to five seconds of undercount. Catch-up after missed heartbeats may not invent evidence. Closed/paused totals remain flat.

## Research and implementation constraints

Apple recommends event notifications and minimizing recurring timers for energy efficiency: [Energy Efficiency Guide: Minimize Timer Usage](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/Timers.html). This supports the event-first design; it does not prove its savings on this machine.

Workspace supplies lifecycle notifications; wake observers must use its own notification center: [NSWorkspace](https://developer.apple.com/documentation/appkit/nsworkspace), [didWakeNotification](https://developer.apple.com/documentation/appkit/nsworkspace/didwakenotification). Validate individual AX/input notifications and permission behavior on installed Live before relying on them.

ScreenCaptureKit supports application-level audio filtering: [Apple WWDC22: Take ScreenCaptureKit to the next level](https://developer.apple.com/videos/play/wwdc2022/10155/). App filtering removes unrelated application audio; it cannot distinguish attended listening from a loop or guarantee capture of every external-device routing configuration.

AbletonOSC exposes transport queries/listeners: [upstream song handler](https://raw.githubusercontent.com/ideoforms/AbletonOSC/master/abletonosc/song.py). Its [handler](https://raw.githubusercontent.com/ideoforms/AbletonOSC/master/abletonosc/handler.py) sends initial/listener values; its [server](https://raw.githubusercontent.com/ideoforms/AbletonOSC/master/abletonosc/osc_server.py) uses fixed response-port routing. Inspect the installed version; do not assume multiple clients can own that port or that transport proves audible output.

Python documents monotonic clock behavior and platform implementations: [time documentation](https://docs.python.org/3/library/time.html). Use the verified local clock implementation and explicit lifecycle events; keep wall time out of elapsed-duration arithmetic.

## Execution handoff

Execute phases 1–5 in order; stop a phase at a failed gate and fix it before broadening work. Preserve unrelated changes and real user data. No commit/push is implied. Expected engineering effort: approximately 16–26 hours plus live validation, with native input/capture integration the largest uncertainty.

On completion report measured results and remaining hardware limitations, link the replay and performance artifacts, and include a GitHub summary title/description. Do not describe this audit as an implemented fix.
