# Strict tracking implementation and verification

Implemented and installed on 2026-10-27. The LaunchAgent runs the rebuilt source bundle with a signed arm64 audio helper. No commit or push was made.

## Behavior delivered

1. Only qualifying input while Live has focus starts or resumes counting. Pointer motion, app activation alone, other applications, and audio alone cannot resume it. The ordinary inactivity allowance is 30 seconds.
2. Fresh Live transport or Live-only output may extend foreground listening to two minutes after the last qualifying input. Playback cannot renew attendance indefinitely. Switching apps, sleeping, locking, or pausing ends counting. Long hands-off playing and recording are subject to the same two-minute limit; MIDI attendance is not implemented.
3. Project, input, focus, and audio evidence expire. Unknown signals cannot count indefinitely. Effective accounting deadlines are independent of delayed polls. UI status explains why time is working, listening, paused, or unverifiable.
4. New activity has committed interval evidence. Retries are idempotent, elapsed time uses monotonic clocks, and exact intervals drive calendar allocations. Historical totals retain their original estimated allocation. Compatible covered sessions can resume without losing draft-to-saved work or short-pause evidence; legacy rows remain separate.
5. Native workspace/focus events and qualifying-event counters replace repeated process/title shell commands. Direct Accessibility reads obtain project titles. Audio probing is asynchronous, Live-specific, cancellable, backed off on failure, and disabled when ineligible. Cached rollups and quiet paused-state logging reduce background work.

## Verification completed

The evidence folder is `~/.ableton_tracker/verification/20261027-strict-tracking/`.

1. **176 tests pass:** 47 tracker regressions, 56 dashboard tests, 46 menu tests, 23 policy/accounting tests, and four detector-boundary tests. The old assertions requiring indefinite fail-open activity and audio-only resume were replaced by the strict replay scenarios written before implementation. Unrelated parser, notes, cleanup, pause-history, condensation, and menu coverage was retained.
2. **Repeatable real-persistence checks pass:** the replay contains source hashes, explicit observations/decisions, expected/actual totals, and its SQLite database. Two delayed-observation fixtures verify that project and audio freshness deadlines expire between polls. A separate process is rejected before it can initialize or alter an already-owned database.
3. **HTTP and stress checks pass:** the real HTTP server reports the recorded totals, keeps exact evidence through physical merging, and prevents merging historical estimates into covered rows. The seeded stress replay covers 10,000 simulated seconds, 2,000 observations, clock corrections, focus changes, and 34 injected write failures. Aggregate, interval, and calendar totals reconcile; SQLite integrity is `ok`.
4. **Data and rollback checks pass:** migration twice on a copy of the live DB preserved original session fields. All 622 pre-deployment session IDs, notes, and todos remain present afterward. The rollback utility was exercised on a separate database and preserved totals and notes. A SQLite backup and the previous bundle were retained before deployment.
5. **Native and rendered checks pass within their stated scope:** direct AX returned the current Live project; the app registered workspace and input monitoring; a Live-only capture returned quiet with valid audio samples. A synthetic OS scroll was detected using the counter fallback with the foreground identity and title supplied by a fixture, without constructing Tracker or opening its DB. Six status reasons rendered at 820px without overflow or fixture-page console errors; the normal 1280px view was also inspected. The installed page reports background pause and no active session. The rebuilt bundle verifies with codesign; tracking modules match source hashes. Concurrent menu edits were preserved; the final source/bundle comparison found only a whitespace difference in the menu file. The installed HTTP status is healthy.

The global monitor registering in a CLI probe did not by itself demonstrate callback delivery. Native counters therefore remain available even when registration succeeds. The successful OS-event check establishes the counter path; it does not claim human input or controller/MIDI qualification in Live.

An installed-page screenshot attempt timed out in the collaborative preview. The installed status was inspected directly in the rendered DOM and HTTP response; saved fixture screenshots provide the visual-state evidence. No alternate browser was required for the completed checks.

## Performance measurement

Ten-minute baseline: the previous menu process consumed **3.90 CPU seconds over 600.06 seconds**, or **0.650% of one core on average**. The final ten-minute candidate trace was stopped at the user's request to finish. No matching A/B reduction is claimed. Before the final restart, the candidate had used **2.12 CPU seconds over a process lifetime of 9m55s**, approximately **0.36% of one core**, including startup. This is a cumulative process snapshot under an uncontrolled workload, not a completed matched trace.

The baseline followed the existing user workload with Live in the background and other applications active. Live was closed during the final observation window; the candidate process lifetime was not continuously classified by workload. They are not controlled editing or playback trials. The profiler samples child processes and shared capture services, but can miss short-lived children and cannot assign all shared-service costs to Tracker. Use the absolute parent CPU seconds for the primary comparison; do not describe these results as a full-machine energy or audio-latency benchmark.

## Repeat the checks

Run from the repository root. Every persistence check uses a fresh isolated database and prints its artifact directory.

```bash
python3 scripts/verify_tracker_policy.py
python3 scripts/verify_tracker_http.py
python3 scripts/stress_tracker_policy.py
python3 -m unittest -v test_tracker.py test_dashboard.py test_menubar.py
```

`python3 scripts/verify_native_input.py` performs the separate OS-boundary check. It posts a one-pixel scroll to the current foreground app, records only event timestamps/categories, and never opens the user's sessions database.

For another background sample, obtain the menu process PID from `launchctl list`, then run:

```bash
python3 scripts/profile_tracker_runtime.py --pid PID --seconds 600 --output /tmp/tracker-runtime.json
```

## Remaining qualification

Controlled editing/playback A/B traces, real MIDI/control-surface performance, long monitored recordings, actual sleep/lock timing, and forced capture/Accessibility permission changes have not been exercised against the user's open Live set. The deterministic replays cover their policy decisions and error boundaries; they do not replace live equipment or permission checks. The current policy intentionally pauses hands-off performances after two minutes.

No historical missing work was reconstructed and no historical totals were reclassified as exact. Capture threshold tuning remains device/workflow dependent. A sustained Live playback benchmark is still required before claiming capture-path CPU/wakeup savings or freedom from audio dropouts.

## Rollback

Stop the LaunchAgent first. Preserve the current database and run the compatibility utility with explicit paths before using the old runtime:

```bash
python3 scripts/rollback_tracker_policy.py --db /absolute/path/to/sessions.db --backup /absolute/path/to/new-backup.sqlite
```

The utility takes the shared writer lock, makes the mandatory backup, closes open rows at their last recorded timestamp, and marks covered rows as historical estimates while preserving their totals, notes, and retained interval evidence. It does not overwrite newer work with an older backup. Restore the preserved old bundle only after stopping the current runtime. The retained bundle and backup locations are listed in `deployment.json` in the evidence folder.

## GitHub handoff

Summary title: Tighten inactivity tracking and reduce background processing

Description: Count foreground Live interaction with finite inactivity/listening allowances, explain pause reasons, and persist exact activity intervals without changing historical totals. Use native observations, bounded asynchronous Live-only audio probes, shared writer ownership, and cached rollups. Verified with 176 tests, replay/HTTP/stress artifacts, copied-database migration/rollback checks, rendered states, and a rebuilt signed runtime. Controlled Live playback and hardware qualification remain pending.
