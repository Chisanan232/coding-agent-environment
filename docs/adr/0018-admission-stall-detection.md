# ADR 0018: Admission stall detection and bounded recovery (SPE-176)

Status: accepted for the repository for the monitor (observe-first). **The memory-policy correction in "Separate decision" is a proposal
that needs its own review and an owner decision; nothing here changes a threshold or the latch.** Installing the persistent trigger is an
owner-run step.

## Context

On 2026-11-19 queued heavy work stalled for about 9.5 hours. Measured causes, in the order they were found (SPE-175, SPE-176):

1. The memory latch stayed on. The only failing exit predicate was the static compressor (26.2% of RAM against exit 25%, enter 35%) with
   kernel pressure 1, swap I/O 0 and load 0.4 per core. After one idle rust-analyzer was stopped the compressor fell 2.7 GB and then moved by only
   about 0.04 GB in the next 15 minutes (still 26.1%, an observation, not proof it never decays). Only `memory-admission.py check --weight heavy` writes the recovery streak, and it runs from `queue
   advance` (only when the head already fits), `queue claim` retries and direct agent calls, so the streak starves when nobody polls.
2. `queue advance` granted without the memory gate (fixed in #263/#264); a caller pinned to an old checkout still did, burning one
   hour of grant per entry.
3. A GRANTED entry reserves a lane and its bytes while nothing runs. q000810 held 32 GiB and one lane for an hour, which alone made the
   next head fail the 100 GiB floor.
4. GRANTED was read as running.

A design review reproduced the latch with the pure `decide()` and hand-built inputs (no live state): lane reasons (heavy lanes full, a
concurrent non-idle heavy tree, including unmanaged cargo such as rust-analyzer flycheck) latch the gate, after which lane 1 is judged by
the stricter EXIT thresholds. A fresh gate with the same metrics admits. That contradicts ADR 0017 ("lane 1 keeps today's ENTER
thresholds") and is a defect in the policy, not a gap the monitor can close.

## Decision

Add `scripts/admission-monitor.py`: a stdlib-only, deterministic, single-shot `tick` with no conversation and no model call.

- **Progress.** Per entry: waiting, granted-not-started, claimed, started (claimed, alive, and the workload tree's CPU time grew), completed.
  **GRANTED never counts as execution.** A global stall is waiting work with no claim, release or CPU growth for 10 minutes.
- **Causes**, first match is primary: `state-unreadable`, `disk-floor` (running work holds the space), `memory-pressure` (an ENTER predicate),
  `memory-hysteresis-static-compressor` (latched, ENTER passes, the compressor alone fails EXIT and was flat for 10 minutes),
  `memory-hysteresis`, `stale-claim` (holder exited, claim unreleased), `frozen-queue` (waiting work, nothing blocks it, nothing is being granted),
  `unclaimed-handoff` (a grant with no `waiter` unclaimed for 15 minutes, 75% of its 20 minute window; see below),
  `failed-continuation` (a grant with a waiter unclaimed for 45 minutes, 75% of its 60 minute window, or unclaimed grants whose reservation breaks the floor), `outdated-caller`
  (a grant without the runner stamp of the current protocol), `stalled-running` (a claimed workload whose CPU time has not changed for
  15 minutes: reported, never touched) and `unobservable` (memory metrics unreadable). A grant that lapsed unclaimed is a note in the
  status, not a trigger. Only `stale-claim`, `frozen-queue`, `failed-continuation` and the two hysteresis causes can start an advance.
- **Unclaimed hand-off incidents (ADR 0019 monitor slice, SPE-176).** A hand-off grant (no `waiter`) lapses to EXPIRED after 20 minutes, which
  made the 45 minute rule unreachable for it. Each grant's own window now sets the clock: a status notice at 5 minutes (no page), an incident at 75%
  of the window (15 minutes for a hand-off), and, if the monitor first sees the grant after it lapsed (EXPIRED, or GRANTED past its window, with no claim,
  holder or claim/release receipt, within 6 hours), one incident on lapse with the recovery phrase `grant lapsed`. An incident is keyed by entry id plus
  `grant.at`, so ticks, restarts and a second monitor are one incident. **At most two notices per incident: escalate and lapse.** A lapse after a raise
  upgrades the incident with one more notice whose text says `grant lapsed`. A lapsed waiter entry raises nothing here. On a tick after an observation gap
  (sleep, reboot, clock step) wall-clock age is not trusted: only an EXPIRED state the scheduler itself recorded can raise.
  The state records, per incident, `handed_off_at` (session-notify answered `queued`), `delivered_at` (set only when session-notify reports a backend
  accepted that notice; `queued` alone, or `deduped` for a notice not already handed off by this record, is never delivery), `delivery`
  (`sent`, `pending` while polled, `dropped`, `unknown` when an earlier call finished it, `unconfirmed` after 20 polls), `acknowledged_at` (set only when the
  entry is later CLAIMED or RELEASED; silence is never read as abandonment), `claim_seen`, `ack_deadline` (the escalate time), that the entry had no
  waiter, and `action: none`: the monitor does not cancel, because the scheduler's own expiry frees the lane. The request uses `emit-resource --strict`, so a
  no-backend outcome leaves no dedup record and a retry after a backend is enabled still notifies. The monitor sends no `--clear` while any incident's notice is unsettled (a clear bumps session-notify's cycle), and session-notify never queues a second entry for a key that is still pending. A `deduped` answer for a notice this record never handed off (a crash before the state was saved) is asked for at most 5 times, then recorded as `handed_off_unverified`. **`status.json` is not delivery.** Each incident carries `owner_notice_state` (`pending`, `delivered`, `acknowledged`, `unknown`, `undelivered`); an incident
  whose notice ended undelivered (dropped, unconfirmed after 20 polls, unverified, or the hand-off keeps failing) is listed in `owner_notice_undelivered`
  (incident key, entry id, phase, time raised) in `status.json`, and `status` prints its count. That list does not hold back `--clear`. With no backend enabled
  the only path to the owner is the coordinating Claude session reading that list and forwarding it. After a lapse the monitor records whether the lane is in
  use and whether the next waiting entry started. Incidents are dropped 7 hours after the grant (1 hour after an acknowledgement). Beyond 24 kept, acknowledged
  ones go first, then ones past the 6 hour lapse window; one still inside the window is never evicted (hard bound 64), so the state file stays far below its
  64 KiB read cap.
- **Runner stamp.** The granted receipt (never the grant object) records `runner: {protocol, checkout}` and `memory_gate`; the monitor reads
  it from the receipt that matches the grant time. The grant object stays exactly `{at, expires}`: a hand-off consumer validates it as a
  closed key set, and an earlier version of this change that put the stamp inside the grant made the HORO-2139 executor refuse the grant
  and exit (2026-11-19). A test pins the key set. `RUNNER_PROTOCOL` is bumped when a runner gains behaviour another runner's grants
  cannot be trusted without.
- **Recovery** has one verb: `disk-admission.py queue advance` from this checkout, only with `--act`, only after the same cause held for two
  ticks, never on `disk-floor`, `memory-pressure` or `state-unreadable`, at most one per 120 s and five per rolling 30 minutes, then
  `recovery-exhausted`. Advance already releases claims whose holder provably exited, expires lapsed grants and probes the memory gate, so
  the monitor also becomes a legitimate sampler of the recovery streak through the gate's own path.
- **Never**: signal or kill a process (a slow advance is left to finish), run `launchctl`, reset the latch, change a threshold, write disk or
  memory admission state from the monitor process itself (the advance it starts writes queue and disk state, and counts the memory
  streak through the gate, under their own locks), delete
  anything, pass `--confirm-ended`, or present an entry's session id (an ownership attestation). A test enforces that shape. Redirected Cargo
  targets stay ask-only; on `disk-floor` the monitor reports and does not delete.
- **Observation gaps.** It persists wall clock, monotonic clock and boot time. A reboot, a backward clock step, a gap over three intervals or
  wall minus monotonic over 60 s (sleep) marks a gap: confirmations reset and nothing is done until two later ticks agree. Corrupt state
  restarts observation. A non-blocking `flock` makes a second monitor exit. The queue files are the truth; the monitor's state is a cache.
- **Escalation.** One alert per cause plus blocker signature, again only on change or after 6 hours, through an injected notifier (the
  `session-notify` wiring is the next slice). An alert counts as sent only when the notifier reports delivery; an undelivered one is
  retried with exponential backoff and shows in `status.json` as `last_alert`. The cause, the attempted recovery and one owner-action name are in the alert; ids and numbers
  stay in `status.json`.
- **Caller inventory.** `callers` reads each checkout's `disk-admission.py` as text (protocol, stable lane cap, whether advance is memory
  gated) and never executes it or reads a process environment. Canonical path: `coding-agent-environment-main-view`, fast-forward only.
- **Trigger.** The primary trigger is a user LaunchAgent printed by `plist` (StartInterval 60, no KeepAlive). Writing it and running
  `launchctl bootstrap gui/$(id -u) ...` is an owner-run step: a LaunchAgent created from an agent session was classifier-denied before
  ("Unauthorized Persistence", 2026-11-08) and was not worked around.

### Targets (from 815 queue entries, 2026-10-30 to 2026-11-19)

| Measure | p50 | p90 | p99 |
|---|---|---|---|
| enqueue to grant | 12 min | 4.0 h | 12.5 h |
| grant to claim | 25 s | 139 s | 29 min |
| claim to release | 3 min | 59 min | 5.4 h |

- Detection: a global stall or refusal-keyed cause is classified within 12 minutes of onset; a GRANTED age alone alerts at 45 minutes.
- Recovery: for frozen-queue and exited-claim causes, a grant or release within two ticks of confirmation.
- Notification: within 25 minutes of onset for causes the monitor cannot recover (the incident took about 9.5 hours), at most one alert per
  signature per 6 hours, none on a replay of busy windows with progress.

## Separate decision (not part of the monitor)

The exit criterion can refuse forever after the work that raised the compressor has ended, because compressed pages are a stock owned by
long-lived processes and nothing at pressure level 1 evicts them. Options, each a separate PR with shadow measurement first:

- **P1 (recommended).** Only memory reasons latch (pressure, swap I/O, compressor at ENTER, load, unreadable). Lane-full and
  concurrent-lane reasons still refuse every check but do not latch. Thresholds unchanged; lane 1 returns to the ADR 0017 ENTER rule.
- **P2.** While latched with the compressor as the only EXIT blocker and below ENTER, release after ten good checks 60 s apart with
  pressure 1 every time, swap I/O at most 1 MiB/s, the compressor not rising and a low compression rate. Lane 1 only.
- **P3.** A rate-based exit everywhere (deferred).

Safety property to test: P1 and P2 admit only states a fresh, never-latched gate admits (P2 requires more evidence), and any ENTER
violation re-latches at once. Revert trigger in shadow or canary: pressure 2 or higher, or swap I/O above 16 MiB/s within 30 minutes of a
shadow release.

## Consequences

- Needs no new privilege for the code. The persistent trigger and any notification backend need owner action (see RESOURCE-ADMISSION).
- Direct `run` (reserve) still does not consult the memory gate (RESOURCE-ADMISSION open decision); the monitor does not change that.
- Old checkouts keep working but their grants are reported as `outdated-caller` until the owner restarts the waiting session on the
  maintained checkout.
