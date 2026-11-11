# Non-blocking ASK handling (SPE-170)

A host ASK on an exact action (typically the `merge_command` that `owner-merge-gate.py check` prints) must not stall independent work, and must never be
treated as approved because time passed. Implementation: `scripts/ask-pending.py` (safety layer); tests: `tests/test-ask-pending.py`.

## Acceptance criteria (corrected 2026-11-10)

A text notification, a pending-list entry or a request to reply in chat does **not** satisfy this feature. The feature is complete only when every
criterion below is verified on the real host path (Claude Code Remote Control on the owner's iPhone).

| # | Criterion | Status |
|---|---|---|
| A1 | A real tool permission request stays a **live, actionable native Approve/Deny** on the iPhone (Remote Control) | PASS for Allow once and Deny on a harmless ask-ruled command (2026-11-11 run 3, deny lane); owner attests both taps were on the iPhone, telemetry records the decision source only |
| A2 | A product decision is an **interactive Select** control where the host supports it | Select value verified (chosen "Blue" returned as Blue); the device for this one was not separately confirmed, and the observer cannot corroborate it (an `AskUserQuestion` stays `PROMPTED_NO_RESULT`) |
| A3 | The waiting operation is assigned to **one background subagent** where the host supports it | PASS: one background subagent held the prompt (lane A3) |
| A4 | The root agent (or another subagent) continues **independent authorized work** within existing concurrency limits while the prompt is open | PASS: a new subagent launched about 40 s after the prompt and finished independent read-only work while it was still pending |
| A5 | The 15-minute rule changes **scheduling only**: the original prompt is not cancelled, dismissed or converted to a text reply when the timer fires | PASS for one run, for what was observed: the original prompt survived past T0+15m untouched and was accepted after 24m28s; nothing cancelled, reworded or converted it. No scheduling timer was involved, only elapsed time |
| A6 | If the platform itself expires a prompt, that is **recorded explicitly** and a supported reissue path is determined | Not expired within 7h21m on an unanswered prompt (ended by the owner's manual denial), and a second prompt survived 5h23m before it was answered; no expiry event observed, so expiry and a reissue path stay unverified |
| A7 | A watcher may observe the decision and resume work; it **cannot** grant permission or bypass the host gate | Design met; live part observed: the observer printed no decision and the accept recorded `user_temporary`, not the observer or a watcher |
| A8 | Approve on the **original** prompt resumes **only** the intended exact-head action; Deny and Select are tested separately | Allow once resumed only the intended command and Deny did not run its command (separate lanes); the exact-head merge was approved with Allow once on the original prompt after 5h23m and merged only the pinned PR head (merge commit verified) |
| A9 | An expired prompt, a changed head or a duplicate event **cannot execute the wrong action** | Ledger part tested in a disposable dir. Live: no prompt expired in 7h21m; a stale approval (pinned head moved after the card was raised) was refused by GitHub (`Head branch was modified`) and the PR stayed unmerged; two identical pinned approvals produced exactly one merge, the second was refused (`Merge already in progress`). Expiry itself was never observed, so the expired-prompt case stays unverified |
| A10 | A host mode that cannot support a criterion is reported by criterion; a notification-only flow is never marked complete | iPhone Allow once, Deny and Select verified (see Live results); the feature is **not yet complete** against A6 (no expiry or reissue observed in 7h21m) and the expired-prompt case of A9; changed-head and duplicate-event are verified live |

What exists today (PR #217) is only the **safety layer** behind A7 and A9: a ledger that records the exact action, target, head, dependencies, host request id and
resume trigger, revalidates the exact head and the owner-merge gate before a resume, hands the action back at most once per lease (a lapsed lease after a crash can produce a second hand-back, which is flagged `possible_duplicate_handback`), and handles denial, expiry, changed head,
duplicate and restart. It is **not** an approval channel, and a pending-list entry is not an approval prompt. The live prompt itself is the host's.

## Before opening an ASK (triage)

Most blockers are not ASKs. Open one only when the next action genuinely requires **owner authority** (a host ASK for a gated operation, a credential, an admin or
sudo action, release authorization) or a **material product, security, cost or release decision**. Otherwise decide it yourself, per `decision-delegation` Levels 0-1.

1. **Resolve routine and reversible choices yourself** (implementation, ordering, retries, safe cleanup of what this task owns).
2. **Preserve what you do not own.** Worktrees, branches, processes and queue entries of unknown ownership stay untouched; a session's own work is its own (ownership
   needs a checkpoint record, not a name match). Never delete or overwrite to "unblock".
3. **Investigate and prepare first.** Diagnose, reproduce and prepare the fix on a separate branch (its own worktree) and gather the evidence, so the owner only decides.
4. **Ask only for the decision that remains**, with the evidence and the impact of each option (what happens on Approve, on Deny, on each Select choice), as one
   request. Never bundle several questions or ask what the code or an existing ADR already settles.
5. **Deduplicate across sessions.** Before asking, `ask-pending.py list`; a blocker already recorded (same merge action, or same decision subject and target) belongs to its
   **coordinator** session. Do not open a second request (this is a convention the ledger makes visible, not a lock: nothing technical stops another session from raising its own host prompt, and the ledger cannot see one); record your dependency and keep working. Use `record-decision` for non-command blockers: the first session to
   record it coordinates the one owner action, a later session gets the same item (`created: false`). `takeover` moves coordination only explicitly and is receipted.

For a genuine owner decision the original clickable Approve, Deny or Select request stays available (never withdrawn, reworded or converted to a text question), and
independent authorized work continues immediately in the root or a separate subagent. The 15-minute threshold is for switching focus only: it never expires a
request or grants permission. The 12-hour heavy-queue escalation rule is separate and unchanged: a queue wait escalates only after 12 continuous hours, with queue id,
elapsed wait, concrete blocker and exact action needed.

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
and `Notification permission_prompt`. It prints nothing and exits 0, so the host prompt is untouched, and it records a line per event holding only the tool name, the program name for a command and a hash of the input (never arguments, paths or URLs; the program name is stored only if it is on a short allowlist, otherwise `other`; the hash is an HMAC under a per-install key; the log rotates at 1 MB). What is tested: secret-shaped arguments, paths and URLs do not appear in the file. Not claimed: that nothing sensitive can ever be inferred from timing and tool names. Scope the `PostToolUse` matcher to the tools you test with if you do not want every Bash call logged.
`ask-observe.py report` shows each prompt as `PROMPTED_NO_RESULT` (pending, denied or expired: the documented events do not say which), `RAN_AFTER_OPEN_PROMPT`,
`FAILED_AFTER_OPEN_PROMPT` or `RAN_WITHOUT_PROMPT` (the same call ran while or after a prompt was open: consistent with an approval, but a denied prompt followed by the same call under an allow rule looks identical, so it is never reported as consent). Without it nothing in this repository can tell whether a prompt was raised at all. Editing `~/.claude/settings.json`
is an owner action; this branch does not do it.

## Live results (2026-11-10 to 2026-11-11, this Mac, Claude Code 2.1.306, session in `auto` mode, observer installed)

Earlier probes (2026-11-10, before the observer was installed) returned at once and could not distinguish "no prompt raised" from "answered within a minute".
With the observer installed, the same `Bash(rtk proxy *)` ask rule raised a real prompt in `auto` mode: an explicit ask rule is not auto-resolved.

| Run | Prompt raised (UTC) | Resolved (UTC) | Decision | Counted |
|---|---|---|---|---|
| 1 (2026-11-10) | 23:40:43 | 23:41:28 | accept, `user_temporary` | No: the owner did not know it was a test |
| 2 (2026-11-10) | 23:47:52 | 23:51:40 | accept, `user_temporary` | No: same |
| 3 hold (2026-11-11) | 00:04:02 | 00:28:30 | accept, `user_temporary` | Yes: pending 24m28s, owner told the hold-until time in advance |
| Deny lane (2026-11-11) | 00:29:07 | 00:30:33 | reject, `user_reject` | Yes: "Denied by user", command did not run |
| A6 hold (2026-11-11) | 03:17:24 | denied by the owner at about 10:39 (7h21m) as manual cleanup | reject, "Denied by user" | Yes: owner told to leave it unanswered; no expiry event, command did not run |
| A8 exact-head merge (2026-11-11) | 04:39:19 | 10:02:37 | accept, `user_temporary` | Yes: pinned `gh --repo ... pr merge 220 --merge --match-head-commit 65c2477`, pending 5h23m, merged as `1d7f81b` and verified |
| A9 stale head (2026-11-11) | 10:41:21 | about 10:52 | accept, `user_temporary` | Yes: pinned to head `a54f6c8` on a disposable PR into a throwaway base branch, head moved to `6c61992` before the owner answered; GitHub refused, PR stayed open |
| A9 duplicate (2026-11-11) | 10:52:43 and 10:52:44 | about 10:52-10:57 | accept, `user_temporary` (both) | Yes: two identical pinned merges of the same PR head; one merged (`d1d4a08`), the other was refused, one merge commit on the base |

Run 3: command `rtk proxy echo spe170-allow3-hold-0825TPE`. A second subagent launched about 40 s after the prompt and finished a read-only ticket verification by 00:06:26, while the prompt was still pending. The prompt survived T0+15m (00:19:02) and the announced hold-until (08:25 Asia/Taipei = 00:25:00Z). After the owner's Allow once the observer showed `RAN_AFTER_OPEN_PROMPT` and the lane returned the echo output.

A6 hold: command `rtk proxy echo spe170-a6-1794367034`, one lane, never answered. Polled every 10-15 minutes against a 60-minute window (to 04:17Z). The observer row stayed `PROMPTED_NO_RESULT` and the lane transcript never gained a `permissionDecision` or a result for the marker, so the platform did not expire the prompt and the command did not run within the window. The prompt was still open at 10:03:00Z (6h45m, observer `waiting=24349s`) with no decision in the transcript, and was then denied by the owner at about 10:39Z (7h21m) as manual cleanup after the window; the lane reported "Denied by user" and the command did not run. This does not show the platform never expires a prompt; it bounds expiry above 7h21m for this host mode.

A8 merge: PR #220 (docs only) at head `65c2477`, review and CI green, gate ALLOW. The runner is host-allowed and raises no prompt, so the test used the ask-ruled `gh --repo ... pr merge` spelling in a lane. The owner answered Allow once on the original card 5h23m after it was raised; the lane transcript shows `user_temporary`, the observer shows `RAN_AFTER_OPEN_PROMPT`, `owner-merge-gate.py verify` returned VERIFIED (head is the merge's parent, two parents). The head did not change while the prompt was open, so the changed-head and duplicate cases of A9 were exercised separately on a disposable PR (see the A9 rows).

Each transcript also shows a second `accept` 4-5 s after the owner's decision with source `config` and reason `classifier`; it belongs to the lane's final report call and did not change the owner's decision.

Limits: the decision source (`user_temporary`, `user_reject`) is read from the subagent transcript's `permissionDecision`; no telemetry records the device, so the iPhone claim rests on the owner's confirmation. The observer reports a denied prompt as `PROMPTED_NO_RESULT` (not `FAILED_AFTER_OPEN_PROMPT`), so by itself it cannot tell denied from open. The test must tell the owner it is a test, name the exact command and give the hold-until time, otherwise the owner may answer early (runs 1 and 2).

## Expiry and reissue: what the docs say (2026-11-11, read-only research)

Documented: Remote Control "keeps permission prompts and AskUserQuestion questions open until you answer them" (code.claude.com/docs/en/remote-control, "Forwarded dialogs expire"); other forwarded dialogs get a 5-minute default, controlled by `dialogExpiry` (2.1.224+, default and values not read). `permissions.denyInsteadOfPrompting` (2.1.319+, user settings or `--settings` only) is an opt-in auto-deny and is not enabled here.
Not documented: a timeout for a background-subagent prompt, a way to re-surface or reissue a missed prompt (only `/remote-control` after a connection failure), or a maximum pending time.
This agrees with the 7h21m observation but proves nothing about expiry, so A6 and the expired-prompt case of A9 stay unverified. The changelog for 2.1.306 itself could not be confirmed (the fetched page began at 2.1.311).

## Proof status

| Claim | Status |
|---|---|
| A background subagent's prompt waits, actionable on the phone, while the root continues | Verified once on a harmless command (Allow once after 24m28s, Deny separately); not verified for an exact-head merge or for expiry beyond 24m28s |
| The ledger keeps an unanswered ASK past 15 minutes, holds several, resumes only the intended one, and handles changed head, denial, duplicate and restart | Tested in a disposable directory with a stand-in gate (`tests/test-ask-pending.py`); no live prompt was involved |
| The normal merge gate is still the authority | Tested against a stand-in gate plus a contract test of the real gate's printed JSON; the gate's own evaluation is covered by `tests/test-owner-merge-gate.py`. No live merge was performed |
| The observer cannot decide | Tested (`tests/test-ask-observe.py`): no output, exit 0, no decision fields in the source |

## Verification protocol (needs the owner's phone)

1. The owner installs the observer with `scripts/ask-observe-install.py` (`plan` prints the exact settings diff and the two pins; `apply` needs both pins and adds four hook groups only; `rollback --receipt` removes exactly those four and keeps later edits; run `apply` and `rollback` from a checkout merged to origin/main). The example JSON is only a reference for what it adds. Then start or reconnect Remote Control (`/remote-control`) on the session that will
   run the test. An explicit ask rule raised a prompt in `auto` mode in the 2026-11-11 runs; if a mode does not raise one for an ask-ruled command, that is itself
   recorded as the failed criterion. Tell the owner beforehand that it is a test, the exact command and the hold-until time.
2. The root launches one background lane per scenario (A: approve, B: deny, C: a second request for a different pinned head) with a harmless ask-ruled command whose
   text carries a distinct marker, records each in the ledger with `--prompt live`, and keeps doing independent authorized work, logging timestamps.
3. Past 15 minutes nothing is done to the prompts. The owner taps Approve on A's original prompt and Deny on B's. `ask-observe.py report` must show A
   `RAN_AFTER_OPEN_PROMPT` and B without an execution, and C must not have run.
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
| `kind`, `coordinator` | `merge` (an exact command, revalidated by the gate) or `decision` (a shared owner-decision blocker, answered on the original request). The coordinating session is the one expected to close it (the check compares a caller-supplied `--session-id` with a value readable from the ledger, so it is a convention and a visible receipt, not enforcement). `finish --outcome answered --ref gh-comment:<owner/repo>:<id>` is **verified** by reading that GitHub comment back (in the item's own repo and on its own issue/PR, naming the item id, by a recorded owner, newer than the request, and by an identity other than the one `gh` is authenticated as here; on this machine the agent posts as the owner's own login, so such a comment is normally *not* independent proof and the closure stays unverified); any other closure (denied, expired, an unverifiable ref) is the coordinator's own word, closes only that session's wait, and a later `record-decision` for the subject reopens the request (generation +1) instead of suppressing it. This includes an owner's explicit denial recorded here: decisions are re-asked rather than durably denied, so record a denial in the ticket or an ADR. Free-form notes are stored only as a hash; the subject and any `takeover`/`cancel` reason are stored as given and are not scrubbed, so never put a secret in them. Who may verify an answer is never taken from the recording or closing session or from any agent-writable file: it is only the target repo's owner login. For an org-owned target the owner login is the org, which never authors a comment, so an answer there stays unverified and the request reopens (record the answer in the ticket or an ADR instead). Merge items still pass the gate the `--owners` given at record time; the gate re-derives owner class itself and the host still asks. `cancel` is coordinator-only as well; `takeover` is the explicit, receipted way another session coordinates; the first recording session coordinates and a duplicate record from another session returns the same item |

Commands: `record`, `record-decision`, `takeover`, `list [--json]`, `restart --session-id`, `resume --id --current-head`, `finish --id --outcome executed|denied|expired|failed [--ref]`, `cancel`.
There is deliberately no `approve` command: the ledger cannot authorize anything.

## Scope and limits

- Resume revalidation is the owner-merge gate, so the ledger covers **merge ASKs** (the direct `gh pr merge` or the runner command the gate prints). Another kind of ASK can be recorded as a note but has no revalidation and is out of scope here.
- `dependencies` and `resume_trigger` are recorded for the main agent's task graph; the ledger does not evaluate them. Resume is always an explicit call.
- If the gate's preferred executor changes (for example the runner is installed after a direct item was recorded), the old item is refused as "differs from the recorded action"; cancel it and record the new action.
- At most 20 items can be open per coordinating session (200 in the ledger); the next `record` is refused with an explicit error, and one session cannot fill the ledger for the others. `list` is read-only (it writes neither items nor receipts); only `resume` recovers a lapsed lease.
- `prompt` is `unknown` while an item is `RESUMING`: the host prompt for the handed-back command opens next and its state is not observable here.

- **Trust boundary.** The ledger is not tamper-evident: an agent with a shell can edit the JSON under the state directory, and `finish --outcome denied|expired|failed` and `cancel` take the caller's word. That can at worst keep an ASK from being resumed (a closed item, a reopened-after-stale item, or a re-recorded one); it can never approve or execute anything, because resume still needs the real gate PASS and the host's own answer. `executed` is the only outcome that needs outside proof (gate `verify`).
- `resume` and a merge item's `finish` carry no coordinator check (any session can resume or finish any merge item), and `takeover` needs no liveness proof of the old coordinator; both are receipted conventions, and a denied item is closed until a new request is made.
- `resume` refuses when the settings layers present now differ from those recorded (resume from the recording directory); managed settings are never included.
- A `RESUMING` item cannot be cancelled (its host prompt may still be open): finish it as executed, denied or expired. A `RESUMING` lease that lapses (30 minutes) returns to `PENDING`. The next resume first runs `verify`; if the merge is already VERIFIED the item is closed as `CONSUMED` instead of handing the action back again.
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
   - prints `run_exactly` and the `gate_options_used` (a recorded CI exception, version policy or local-first config), which the request to the owner must state because the host prompt shows only the command. The agent runs that command alone, as the gate requires; **the host asks again and its answer is the only approval** for the settings layers the host actually enforces: the gate checks the user, project and local layers, so a managed allow rule it cannot see could let the command run without an ASK.
4. **Finish.** `finish --outcome executed --ref <proof>` (for a merge, the `owner-merge-gate.py verify` result, run separately), `denied`, `expired`, or `failed`
   (back to `PENDING` with `prompt: unknown`). `executed` is accepted only when `owner-merge-gate.py verify` reports VERIFIED for the recorded repo, PR and head; `--ref` is just a pointer to the evidence. A duplicate finish is refused and receipted. A `RESUMING` item with no outcome for 30 minutes returns to `PENDING`
   (never to done).
5. **Restart.** State is on disk. `restart --session-id <new>` marks every other session's live prompt `unknown`; nothing is assumed approved and every item re-validates on resume.

## What never counts as approval

Elapsed time, a handoff, a watcher, a subagent report, a notification, or the ledger itself. A subagent that reports "approved" is not evidence; only the host's own
answer to the exact command is, and the merged state must then be verified.

## Separate rules

- The 12-hour heavy-admission escalation rule (`docs/RESOURCE-ADMISSION.md`) concerns queue admission and is unrelated to the 15-minute ASK grace.
- Merge execution, evidence and verification rules in `OWNER-MERGE-WORKFLOW.md` are unchanged.

## Known limits (reviewers' open Medium items, accepted)

- A `RESUMING` item cannot be cancelled (its host prompt may still be open): finish it as executed, denied or expired first.
- A verified `answered` closure proves who wrote a comment naming the item id, on which issue, and when; the answer text is **not parsed**. Treat the cited comment as the record.
- `action`, `subject`, `request_id` and the `takeover`/`cancel` reasons are stored as given (length-capped: action 500, reasons 200, request id 80, session id 80, owners 200) in mode-0600 files; do not put secrets in them.
- Gate options (`--gate-ci-exception` and friends) are chosen by the recording session, as they are for a direct `owner-merge-gate.py check`; they are shown at resume time and must be stated in the owner request.
- The `.gitleaksignore` entry names one historic commit holding a synthetic AWS-key-shaped literal (sequential digits, written by the test author, never issued by any provider).
