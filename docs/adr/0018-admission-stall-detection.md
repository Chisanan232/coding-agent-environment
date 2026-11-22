# ADR 0018: Admission stall detection and bounded recovery (SPE-176)

Status: accepted for the repository for the monitor (observe-first). **The memory-policy correction in "Separate decision" is a proposal
that needs its own review and an owner decision; nothing here changes a threshold or the latch.** Installing the persistent trigger is an
owner-run step.

**Update (SPE-176 wiring): the guarded recovery of ADR 0019 sections 3, 5 and 6 is wired, enabled only under `tick --act`.** `--observe-only` and a plain
`tick` still never act. With `--act` the monitor may also (1) cancel a conclusively abandoned, unstarted GRANTED entry through the scheduler's own
`queue cancel` and (2) send one SIGTERM, no escalation, to a conclusively obsolete heavy-run / la-cargo wrapper whose entry is terminal. Both go through
`scripts/admission-recovery.py`, are limited to one action per tick and three per rolling 30 minutes (recorded before acting), are verified on a later tick,
and are switched off by the file `recovery.disabled` in the monitor directory. Every sentence below that says the monitor "never signals" or never cancels
is superseded by exactly those two actions; a healthy workload, an arbitrary process and an unidentified process are still never touched.

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
- **Backfilled grants (ADR 0017 revision 9).** With bounded backfill on, a GRANTED or CLAIMED entry can have a higher seq than the QUEUED
  head it overtook. That is not a stall or an ordering fault: the monitor still takes the oldest waiting entry as the head, a backfilled
  grant carries the normal runner stamp (no `outdated-caller`), its bytes are within the floor the scheduler checked for the head (no
  false `failed-continuation`), and one left unclaimed falls under the same per-grant window as any other grant.
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
- **Queued-wait and execution incidents (ADR 0019 clocks 1 and 3, SPE-176).** Two more incident kinds share the hand-off incident record, dedup,
  owner-notice states (`handed_off_at`, `delivered_at`, `acknowledged_at`, `ack_deadline`), retention, strict delivery and the `owner_notice_undelivered` list.
  Neither can make the queue "stalled", start an advance, cancel, release or signal anything; both are keyed per entry and skipped on an observation-gap tick.

  | Cause (`CAUSE_TEXT`) | Clock | Threshold | Notices | Key | Owner action |
  |---|---|---|---|---|---|
  | `queue-wait-excessive` | age since `created`, entry still QUEUED/ELIGIBLE inside its window | status notice at 4 h (no page); incident at 12.5 h; second phase `wait-expiry-near` at 20 h of the 24 h window | at most two per incident | entry id + `wait` | `check-queued-wait` |
  | `workload-over-deadline` | CLAIMED, holder alive, elapsed since `claimed_at` | 2x the entry's declared `deadline_s` (none is written today), else 2 x 6 h | one | entry id + `run` | `check-over-deadline-workload` |

  **Abandoned claim (clock `claim`, cause `stale-claim`, SPE-176).** A CLAIMED entry whose supervising wrapper is gone (pid absent, a zombie, or reused by another process, compared by
  start token) and was never inspected running (`holder_status` reason `workload-not-witnessed`: the scheduler keeps the lane on purpose) used to read as `unresolved`, which `classify` neither
  flagged nor excluded from the `disk-floor` sum, so the queue behind it showed a false `disk-floor` indefinitely. The monitor now counts observations of the same holder identity
  (state `dead_claims`): **5 observations at least 30 s apart (about 5 min) on a claim at least 15 min old** confirm it. The count survives an observation gap and restarts if the
  holder is alive, uninspectable, still has a recorded workload process running (`workload-outlives-wrapper`), or is a different process. Confirmed, the verdict is `stale-claim`
  (ahead of `disk-floor`, which no longer counts the dead claim as running work), `status.json` lists `abandoned_claims` (id, label, repo, claim age, bytes held, owner kind and
  reachability, the entries blocked behind it), and one `stale-claim` incident (key entry id + `claim`, same dedup, strict delivery, retention and outbox line with evidence
  `holder-gone`, `claim-unwitnessed`) is raised on any tick, gap or not. It is never released, cancelled, advanced or signalled (decision B): the owner confirms the claim ended and
  releases it explicitly. A claim whose exit the scheduler can prove (`exited`) keeps the verdict-level alert and advance path. **Limit:** `stale-claim` is not a ZeroClaw spool kind,
  so it reaches the supervisor only after a spool kind and schema revision are added; it reaches every backend that accepts the `session` kind.
  **Clock.** Gap detection compares wall time with `host_uptime()` (`CLOCK_UPTIME_RAW`, else `CLOCK_MONOTONIC`). `time.monotonic()` under Apple's `/usr/bin/python3` 3.9.6 restarts near 0 in each process, so with a
  new launchd process per tick every tick over 60 s apart was an observation gap (all 2171 recorded ticks), which reset blocker confirmations and the progress clock and skipped wait incidents and escalated
  hand-offs. `status.json` carries `observation_gap_streak`; a streak that never returns to 0 means a broken clock, not a sleeping host.
  A global cause already holding the queue (`disk-floor`, `memory-pressure`, `memory-hysteresis*`) is attributed in the incident's `blocker` field (also
  `higher-entry-running` or `none`); the entry still gets its one incident past 12.5 h. At most 12 wait and 6 run incidents are open at once, oldest wait first; the
  rest are counted in `wait_overflow` so 200 old entries never page 200 times or grow the state past its cap. An incident is resolved (`resolved_at`, owner-notice state `resolved`) when the
  wait or the claim ends. That is never an acknowledgement: `acknowledged_at` stays empty. Resolution stops retries, but a notice that was tried and never arrived
  stays `undelivered` and listed in `owner_notice_undelivered` (and is kept until the usual 7 h); only a delivered, acknowledged or never-attempted one is dropped after an hour. Low CPU or a quiet log is never an input to either clock; `stalled-running` keeps its own rule. The run incident
  records the holder pid, its start token as stored, the elapsed time and the deadline source (`declared` or `default`). **Known gap:** no producer writes `deadline_s` into a queue entry or claim, so
  today every run clock uses the 6 h default (escalation at 12 h); a job that legitimately runs longer must have its owner raise that through a future declared field.
  **Sleep and clock steps are discounted.** Ages are wall age minus the observed gaps since the entry was created or claimed. A tick that sees a gap (sleep, a forward clock
  step, a reboot, or a monitor that did not run for over three intervals) records `[last tick, now, gap seconds]` (32 kept) and every later tick subtracts it, so a job claimed
  5 h before an 8 h sleep does not page at 12 wall hours, while real uptime still counts. The cost, stated plainly: an entry that legitimately waits on a sleeping host is not
  "excessive", and an interval in which the monitor itself was not running is not counted either. `discounted_s` is recorded in the incident. The ledger keeps the newest 32 gaps (older ones stop discounting, which only pages sooner) and a gap is capped at 48 h; a stored gap that is not a finite number in
  0..48 h is dropped on load, as is any non-finite number in any other numeric state field (NaN and Infinity are valid JSON to Python and would otherwise silence the clocks).
  **Suppression:** a wait past 12.5 h whose only blocker is running work that is healthy (every CLAIMED entry has a live holder, CPU changed within 15 minutes, claim time readable, inside its own deadline) is a status note
  plus an incident in state `suppressed-higher-entry`: no page, no outbox line. It pages (same incident, same two-notice cap) as soon as a global resource cause holds
  the queue, a running entry passes its deadline, the running work ends with the entry still queued, or at 20 h (`wait-expiry-near` always pages). A granted but
  unclaimed higher entry, a dead, stale-claim, unresolved or CPU-stalled holder never suppresses.
  Bounds: the state file is read through a 64 KiB cap (a larger file restarts observation), so open incidents are capped (12 wait, 6 run) and past 64 incidents only closed ones are evicted (oldest first); an open incident is never dropped. An outbox line is skipped when its key and phase are already in
  the outbox or its backup, so a crash between the append and the state write cannot duplicate it.
  `resolve_owner(entry, sessions, process, launcher)` is a pure function returning `{kind, session_id, reachable, evidence}` (`kind`: claude, codex,
  heavy-run, la-cargo, unknown); the inspectors are injected and an uninspectable owner is `reachable: null`, never gone. Nothing calls a model or messages a peer:
  each incident phase appends one closed-schema line to `owner-outbox.jsonl` in the monitor directory (job id, incident key, observed blocker, evidence enums,
  requested check, owner kind and reachability, no session id; rotated to one backup at 1 MiB) for the coordinating session or a future adapter to forward. Hand-off (unclaimed grant) incidents write the same line at escalate and at lapse (cause `unclaimed-handoff`, clock `grant`, evidence `grant-unclaimed-escalate` or
  `grant-lapsed-unclaimed` plus `no-waiter`), once per incident key and phase.
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
