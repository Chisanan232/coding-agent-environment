# ADR 0019: Queue wait escalation, bounded hand-off grants and owner messaging (SPE-176, extends ADR 0018)

Status: proposed for the repository; implemented in slices (see "Delivery"). Extends the stall monitor of ADR 0018; there is one monitor, not two.
Automatic actions are limited to what the owner authorized on 2026-11-19: notify through the approved destinations, message the identified
owner session, cancel or release **conclusively abandoned, unstarted** entries through supported scheduler mechanisms, and gracefully
terminate **conclusively obsolete job wrappers or waiters**. Healthy workloads, arbitrary applications, system services and unidentified
processes are never touched. Any broader termination policy is prepared here and shipped disabled, for approval.

## Context (2026-11-19, q000811)

The HORO-2139 hand-off entry was granted at 10:53:44Z. Its owner's live waiter (a 6 h python executor with its own readiness and pin checks)
exited at 10:54:08Z with `q627 executor refused: grant record malformed`: it validates the grant as exactly `{at, expires}` and the
runner stamp added inside the grant (ADR 0018, first version) broke it. The Codex session stayed idle, nothing claimed the grant, and one
hand-off grant held the only usable lane for the rest of its hour. A grant with no live claimant had no clock and no owner alert.

## Decision

### 1. Three clocks, thresholds from observed behaviour

Observed on 815 entries (2026-10-30 to 2026-11-19): enqueue to grant p50 12 min, p90 4.0 h, p99 12.5 h; grant to claim p50 25 s, p90 139 s,
p99 29 min; claim to release p50 3 min, p90 59 min, p99 5.4 h.

| Clock | Starts | Notice (owner message, local notice) | Escalate (owner message + Telegram) | Automatic action |
|---|---|---|---|---|
| QUEUED wait | `created` | 4 h (about p90) | 12.5 h (about p99), or earlier if the queue is stalled | none: the 24 h `wait_expires` already bounds it |
| GRANTED, unclaimed | `grant.at` | 5 min | 20 min (the hand-off claim window) | see 3 |
| Execution | claim time | job-declared deadline, else 6 h (about p99) | 2x the deadline | none by default; see 4 |

An alert is per incident (cause plus entry plus clock), deduplicated, and re-sent only on a state change: escalation, automatic action,
failure to recover, verified recovery.

### 2. Owner first; delivery, acknowledgement and deadline are separate facts

Every incident has an owner record resolved from the entry (`session_id`, `harness`, `owner.ticket`, worktree) and local session records
(Claude project files, `~/.codex` thread and rollout metadata, the process that holds the session open). The monitor writes an owner message
to a persisted outbox: job id, observed blocker, evidence (state, clock values, waiter and process facts), requested check, deadline.
Per message the state keeps `delivered_at` (a channel accepted it), `acknowledged_at` (the owner answered or acted: a claim, a release, a
cancel by the owner, or an explicit ack record) and `ack_deadline`. **A missing acknowledgement is never, alone, evidence of abandonment**; it
only moves an incident to the next escalation step. Delivery channels, in order: a peer-session adapter (the coordinating Claude session
forwards outbox messages to named peers; a deterministic monitor cannot call a model), Telegram, and a local fallback (macOS banner through
session-notify). A channel that fails is retried with backoff and shown in `status`.

### 3. Unclaimed grants

An unclaimed grant is **conclusively abandoned** only when all hold: the entry is GRANTED with no holder, release or workload; no process holds
a claim or a live waiter for it (the entry has no `waiter`, or its waiter is provably gone by pid and UTC start token); the owner's known
execution mechanism is gone or has refused (for q000811: the executor process exited and its last record is a refusal); the grant is older
than the notice threshold; and a re-read just before acting still shows the same facts. Then the monitor cancels the entry through the
supported `queue cancel` (no session id, no state file edit), receipts it, messages the owner, and later verifies that the lane freed and
the next eligible head started. Anything short of that (an owner that may still be about to claim) only escalates.

**Bounded hand-off grants.** An entry without a declared waiter is a manual hand-off. It keeps strict FIFO and its place, but its grant
window is the claim window (20 min, about p99 of observed grant to claim) instead of 60 min, so an absent owner can occupy the lane at most 20
minutes per grant; when it lapses the entry expires through the existing path and the owner re-enqueues (it joins the end of the line).
Entries with a live declared waiter keep the 60 min window. No entry is reordered or cancelled by this rule.

### 4. Running workloads

Low CPU or a quiet log is not a hang. The monitor reports (never kills) from: the job's declared deadline, process state, tree CPU change, output
freshness where the job declares a log, and the owner's reply. The default is notify only. A broader automatic termination policy is a file the
owner must create (a closed-schema allowlist, shipped absent): job label or class, a deadline, the progress evidence required, and the
signal. It is not enabled by this ADR.

### 5. Signalling rules (obsolete wrappers and waiters only)

A wrapper or waiter is **conclusively obsolete** when its queue entry is terminal (expired, cancelled, released) or was cancelled by the
monitor, it holds no claim, and it has no child workload. Before any signal the monitor verifies, immediately before sending: the pid exists
and its start time equals the recorded token; its process group and session match what the wrapper recorded; the uid is ours; the
process is the registered wrapper for that entry (declared waiter identity), not merely a name match. It sends SIGTERM only and waits a
bounded time. **There is no automatic escalation to SIGKILL**; a survivor becomes an alert with the exact pid and a request for the owner.
Detached children are discovered and reported, never signalled.

### 6. After any recovery

Confirm the wrapper and its descendants ended, confirm the scheduler released the lane (reservation files, grant marker, no CLAIMED
entry), confirm no detached workload remains, then observe that an existing eligible entry actually starts (a claim with a live holder pid).
Scheduler state files are never deleted or edited by the monitor.

### 7. Notifications

Telegram (Bot API over HTTPS, credentials as environment-variable names or 0600 files, never values, approved destination
`api.telegram.org`) and a local macOS banner as fallback, both through session-notify's existing validator, retry and dedup. `status.json`
alone is not delivery. Messages carry only fixed phrases and the entry id, clock and cause enums.

### 8. Acceptance tests

Owner messaging and missing owner; manual hand-off versus abandoned entry; delayed acknowledgement; PID reuse; concurrent recovery (two monitors,
a recovery and a claim racing); detached children; legitimate long-running jobs (quiet logs, low CPU, within the deadline); the q000811 replay
(executor refused, grant lapsed unclaimed); the bounded hand-off window; cancel never touches a CLAIMED entry; no SIGKILL; no state-file edit.

## Delivery

1. Grant schema fix (closed key set restored; stamp in the receipt only). 2. Bounded hand-off grant window in `disk-admission.py`.
3. session-notify Telegram backend. 4. Monitor escalation engine: clocks, incidents, outbox, acknowledgement, abandoned-unstarted cancel,
obsolete-wrapper graceful termination (guards above), verification, notifier wiring. 5. Owner-resolution helpers (Claude and Codex records).
Termination of anything beyond obsolete wrappers, and any SIGKILL rule, are separate decisions.
