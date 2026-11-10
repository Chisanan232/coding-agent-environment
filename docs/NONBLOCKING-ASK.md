# Non-blocking ASK handling (SPE-170)

A host ASK on an exact action (typically the `merge_command` that `owner-merge-gate.py check` prints) must not stall independent work, and must never be
treated as approved because time passed. Implementation: `scripts/ask-pending.py` (safety layer); tests: `tests/test-ask-pending.py`.

## Acceptance criteria (corrected 2026-11-10)

A text notification, a pending-list entry or a request to reply in chat does **not** satisfy this feature. The feature is complete only when every
criterion below is verified on the real host path (Claude Code Remote Control on the owner's iPhone).

| # | Criterion | Status |
|---|---|---|
| A1 | A real tool permission request stays a **live, actionable native Approve/Deny** on the iPhone (Remote Control) | NOT_RUN |
| A2 | A product decision is an **interactive Select** control where the host supports it | NOT_RUN |
| A3 | The waiting operation is assigned to **one background subagent** where the host supports it | NOT_RUN |
| A4 | The root agent (or another subagent) continues **independent authorized work** within existing concurrency limits while the prompt is open | NOT_RUN |
| A5 | The 15-minute rule changes **scheduling only**: the original prompt is not cancelled, dismissed or converted to a text reply when the timer fires | NOT_RUN |
| A6 | If the platform itself expires a prompt, that is **recorded explicitly** and a supported reissue path is determined | NOT_RUN |
| A7 | A watcher may observe the decision and resume work; it **cannot** grant permission or bypass the host gate | Design met (ledger has no approve path); live part NOT_RUN |
| A8 | Approve on the **original** prompt resumes **only** the intended exact-head action; Deny and Select are tested separately | NOT_RUN |
| A9 | An expired prompt, a changed head or a duplicate event **cannot execute the wrong action** | Ledger part tested in a disposable dir; live part NOT_RUN |
| A10 | A host mode that cannot support a criterion is reported by criterion; a notification-only flow is never marked complete | Applies to this page: **the feature is NOT complete** |

What exists today (PR #217) is only the **safety layer** behind A7 and A9: a ledger that records the exact action, target, head, dependencies, host request id and
resume trigger, revalidates the exact head and the owner-merge gate before a resume, hands the action back once, and handles denial, expiry, changed head,
duplicate and restart. It is **not** an approval channel, and a pending-list entry is not an approval prompt. The live prompt itself is the host's.

## Intended flow on the supported path

1. The root agent issues the gate's exact `merge_command` from **one background subagent lane**. The host raises its native prompt (shown in the session and,
   under Remote Control, on the phone); that prompt is the only approval request and it is **never replaced by text**.
2. The root records a ledger item (`prompt: live`, host request id if any) and **keeps scheduling other authorized work**. After 15 minutes it only moves on
   to the next runnable task; it does not cancel, dismiss, re-ask or reword the prompt, and does not turn it into a chat question.
3. When the owner taps Approve or Deny on the original prompt, the lane's tool call proceeds or is refused. The root observes the lane's result; a watcher may do
   the same, and neither can grant anything. The command is pinned to the exact head (`--match-head-commit`), so a stale approval after the head moved fails
   at GitHub instead of merging something else. The result is verified with `owner-merge-gate.py verify`, then `finish`.
4. If the platform expires or loses the prompt, record it (`finish --outcome expired`) and reissue only by a supported path: re-run the gate and issue the exact
   command again from a lane, after `ask-pending.py resume` revalidated head, review, checks and gate.

## What the host does (Claude Code 2.1.306; official docs read 2026-11-10; "not documented" means exactly that)

| Question | Answer | Source |
|---|---|---|
| Are permission prompts shown on a Remote Control device? | By implication yes; the docs do not describe native Approve/Deny buttons or whether the phone answer resolves the same request | remote-control, changelog 2.1.317 |
| Prompts and `AskUserQuestion` forwarded to Remote Control: do they expire? | They stay open until answered. Other forwarded dialogs expire after 5 min by default (`dialogExpiry`) | remote-control "Forwarded dialogs expire" |
| Platform timeout, idle or app-backgrounding behavior of a pending permission prompt; survival across disconnect/resume | Not documented | none |
| Does `AskUserQuestion` render a Select control on mobile? | Forwarded; the control type is not documented. **Not available to subagents**, so a Select is always a root-agent call | remote-control, sub-agents |
| Background subagent prompts | Surface in the main session and name the subagent; approve lets that subagent continue, Esc denies one call. Whether the root keeps running meanwhile is not stated; whether the phone can answer it is inferred, not documented | sub-agents |
| Supported reissue after an expired or denied prompt | Not documented (`/permissions` "Recently blocked" is not documented either). `PermissionDenied` `retry` exists only in auto mode. The only supported path is to re-run the exact tool call | hooks, permissions |
| Hooks that observe the outcome | `PermissionRequest` fires before the prompt (no `tool_use_id`); `PostToolUse` / `PostToolUseFailure` after the call; `Notification` `permission_prompt` after about 6 s (no outcome). **No documented event reports approve vs deny vs expire**; `PermissionDenied` does not fire for a manual denial | hooks |
| Push notification Approve/Deny buttons | Not documented | remote-control, mobile |
| `defer`, `permissions.denyInsteadOfPrompting`, `--permission-prompts none` | Print-mode or denying; **not** a live prompt and not used | hooks, permissions |

`PermissionRequest` input has no `tool_use_id`, so the ledger records a request id only when the host supplies one and otherwise `null`. Host policy is not changed:
no settings edit, no new allow, and no hook that answers a prompt.

## Observation instrument (owner installs; never decides)

`scripts/ask-observe.py` is a non-deciding hook (example: `docs/examples/ask-observe-hooks.example.json`) for `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`
and `Notification permission_prompt`. It prints nothing and exits 0, so the host prompt is untouched, and it records a sanitized line per event.
`ask-observe.py report` shows each prompt as `PROMPTED_NO_RESULT` (pending, denied or expired: the documented events do not say which), `EXECUTED_AFTER_PROMPT`,
`FAILED_AFTER_PROMPT` or `RAN_WITHOUT_PROMPT`. Without it nothing in this repository can tell whether a prompt was raised at all. Editing `~/.claude/settings.json`
is an owner action; this branch does not do it.

## Live results so far (2026-11-10, this Mac, session in `auto` mode)

| Probe | Result |
|---|---|
| Background lane ran `rtk proxy echo` (matches the `Bash(rtk proxy *)` ask) | Returned at once; no prompt was visible to the lane |
| Background lane ran `git -C <scratch repo> worktree prune --dry-run ...` (matches the `Bash(git * worktree prune*)` ask) | Returned at once (`ok`); no prompt or wait visible to the lane |

A lane cannot see host prompts and no observer hook is installed, so these probes **cannot distinguish "no prompt was raised" from "a prompt was answered within a
minute"**. Neither left a prompt open for 15 minutes. Criteria A1, A3, A5, A6 and A8 are therefore **not verified**.

## Proof status

| Claim | Status |
|---|---|
| A background subagent's prompt waits, actionable on the phone, while the root continues | **NOT verified** (see Live results) |
| The ledger keeps an unanswered ASK past 15 minutes, holds several, resumes only the intended one, and handles changed head, denial, duplicate and restart | Tested in a disposable directory with a stand-in gate (`tests/test-ask-pending.py`); no live prompt was involved |
| The normal merge gate is still the authority | Tested against a stand-in gate plus a contract test of the real gate's printed JSON; the gate's own evaluation is covered by `tests/test-owner-merge-gate.py`. No live merge was performed |
| The observer cannot decide | Tested (`tests/test-ask-observe.py`): no output, exit 0, no decision fields in the source |

## Verification protocol (needs the owner's phone)

1. The owner merges `docs/examples/ask-observe-hooks.example.json` into the settings and starts or reconnects Remote Control (`/remote-control`) on the session that will
   run the test. A session in `default` permission mode raises prompts more reliably than `auto`; if `auto` does not raise one for an ask-ruled command, that is itself
   recorded as the failed criterion.
2. The root launches one background lane per scenario (A: approve, B: deny, C: a second request for a different pinned head) with a harmless ask-ruled command whose
   text carries a distinct marker, records each in the ledger with `--prompt live`, and keeps doing independent authorized work, logging timestamps.
3. Past 15 minutes nothing is done to the prompts. The owner taps Approve on A's original prompt and Deny on B's. `ask-observe.py report` must show A
   `EXECUTED_AFTER_PROMPT` and B without an execution, and C must not have run.
4. Select is a root-only `AskUserQuestion`; background lanes keep working while the root waits on it.
5. Expiry, a changed head and a duplicate event are covered by `tests/test-ask-pending.py`; live, the pinned `--match-head-commit` makes a stale approval fail at GitHub.

Until step 3 passes, the feature is not complete.

## The ledger

State: one JSON file per item under `$ASK_PENDING_DIR` (default `~/.cache/coding-agent-environment/asks`, mode 0700/0600) and `receipts.jsonl`. Item key is a hash of
action, target and head, so a repeated request is the same item.

| Field | Meaning |
|---|---|
| `action`, `target`, `head` | The exact command, `owner/repo#N`, and the exact head SHA it is pinned to |
| `dependencies`, `resume_trigger` | Items or events it waits on; one of `owner_approval`, `owner_present`, `dependency_merged`, `ci_green`, `review_complete`, `manual_restart` |
| `request_id` | Host-supplied identifier or `null` |
| `prompt` | `live` (a host prompt is open now), `none`, or `unknown` (after a restart or a crashed resume). A pending item is a record; a live prompt is a different fact |
| `state` | `PENDING`, `RESUMING`, then `CONSUMED`, `DENIED`, `EXPIRED`, `STALE_HEAD`, `CANCELLED` |

Commands: `record`, `list [--json]`, `restart --session-id`, `resume --id --current-head`, `finish --id --outcome executed|denied|expired|failed [--ref]`, `cancel`.
There is deliberately no `approve` command: the ledger cannot authorize anything.

## Scope and limits

- Resume revalidation is the owner-merge gate, so the ledger covers **merge ASKs** (the direct `gh pr merge` or the runner command the gate prints). Another kind of ASK can be recorded as a note but has no revalidation and is out of scope here.
- `dependencies` and `resume_trigger` are recorded for the main agent's task graph; the ledger does not evaluate them. Resume is always an explicit call.
- If the gate's preferred executor changes (for example the runner is installed after a direct item was recorded), the old item is refused as "differs from the recorded action"; cancel it and record the new action.
- At most 20 items can be open; the 21st `record` is refused with an explicit error. `list` is read-only (it writes neither items nor receipts); only `resume` recovers a lapsed lease.
- `prompt` is `none` while an item is `RESUMING`; the host prompt for the handed-back command is a separate, immediate event.

- **Trust boundary.** The ledger is not tamper-evident: an agent with a shell can edit the JSON under the state directory, and `finish --outcome denied|expired|failed` and `cancel` take the caller's word. That can at worst keep an ASK from being resumed (a closed item, a reopened-after-stale item, or a re-recorded one); it can never approve or execute anything, because resume still needs the real gate PASS and the host's own answer. `executed` is the only outcome that needs outside proof (gate `verify`).
- A `RESUMING` lease that lapses (30 minutes) returns to `PENDING`. The next resume first runs `verify`; if the merge is already VERIFIED the item is closed as `CONSUMED` instead of handing the action back again.
- The gate runs while the ledger lock is held (up to 120 s), so other ledger writes wait during a resume; a recorded `STALE_HEAD` item is reopened by recording the same pinned action again.

## Flow

1. **Record.** When issuing an action the host will ask about, `record` it (exact action, target, head, dependencies, request id if any, trigger, `--prompt live`).
   The action itself is issued from a background lane so the host's native prompt is the approval request, and the root **continues the next runnable task immediately**.
2. **Grace.** `list` shows `move_on` after 15 minutes (`grace_s` is capped at 900). That only means "schedule other work"; it never changes state, approves, expires, cancels or rewords the live prompt, and never converts it to a chat question.
   Several items can be pending; none blocks another.
3. **Resume at a safe checkpoint** (between tasks, never mid-edit). `resume --id <id> --current-head <head just read from GitHub>`:
   - moves `PENDING` to `RESUMING` once; a second resume, a closed item or a lost race is refused;
   - a different head closes the item as `STALE_HEAD` (a new head needs a new independent review and a new record); recording a new head for the same target also supersedes the old item;
   - runs `owner-merge-gate.py check` with the settings layers recorded with the item (user, plus project and local in the recording directory, when they exist; managed settings are not included; a different `--settings` at resume is refused) and the gate options recorded with the item (`--gate-ci-unavailable`, `--gate-ci-exception`, `--gate-version-policy`, `--gate-local-first-config`), and requires PASS with the same head and the same printed `merge_command` (the gate covers exact head, independent review evidence, required checks, owner class and host permission). A gate that cannot run is a refusal;
   - prints `run_exactly` and nothing else. The agent runs that command alone, as the gate requires; **the host asks again and its answer is the only approval.**
4. **Finish.** `finish --outcome executed --ref <proof>` (for a merge, the `owner-merge-gate.py verify` result, run separately), `denied`, `expired`, or `failed`
   (back to `PENDING` with `prompt: unknown`). `executed` is accepted only when `owner-merge-gate.py verify` reports VERIFIED for the recorded repo, PR and head; `--ref` is just a pointer to the evidence. A duplicate finish is refused and receipted. A `RESUMING` item with no outcome for 30 minutes returns to `PENDING`
   (never to done).
5. **Restart.** State is on disk. `restart --session-id <new>` marks every live prompt `unknown`; nothing is assumed approved and every item re-validates on resume.

## What never counts as approval

Elapsed time, a handoff, a watcher, a subagent report, a notification, or the ledger itself. A subagent that reports "approved" is not evidence; only the host's own
answer to the exact command is, and the merged state must then be verified.

## Separate rules

- The 12-hour heavy-admission escalation rule (`docs/RESOURCE-ADMISSION.md`) concerns queue admission and is unrelated to the 15-minute ASK grace.
- Merge execution, evidence and verification rules in `OWNER-MERGE-WORKFLOW.md` are unchanged.
