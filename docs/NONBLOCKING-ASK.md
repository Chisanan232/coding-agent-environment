# Non-blocking ASK handling (SPE-170)

A host ASK on an exact action (typically the `merge_command` that `owner-merge-gate.py check` prints) must not stall independent work, and must never be
treated as approved because time passed. This page is the canonical contract. Implementation: `scripts/ask-pending.py`; tests: `tests/test-ask-pending.py`.

## What the host does (Claude Code 2.1.306, from the official docs, 2026-11-10)

| Mechanism | Blocks other work? | Late answer then resume? | Use here |
|---|---|---|---|
| Native prompt for a **foreground** call | Not documented; a foreground subagent blocks its caller | Interactive answer only | Avoid when other work is runnable |
| Native prompt raised by a **background subagent** | No: the subagent runs concurrently and its prompt is shown in the main session, naming the subagent (`sub-agents`) | Yes, answered in the main session | **Preferred live path**: the exact gate command is run by one background lane |
| Background Bash hitting an ASK | Not documented | Not documented | Do not rely on it |
| PreToolUse `defer` | Ends the turn (print mode only, ignored in interactive sessions) | `claude -p --resume <id>` re-runs the hook | Not used: it needs a headless host; this setup is interactive |
| `PermissionRequest` hook / `--permission-prompt-tool` / SDK `canUseTool` | Per call; the hook cannot return ask or defer | Callback only | Not used: would make a hook the approver |
| `permissions.denyInsteadOfPrompting`, `--permission-prompts none` | Deny the call, no wait | No | Not used |

`PermissionRequest` input has no `tool_use_id`; `PreToolUse` and `PermissionDenied` have one. There is no documented permission-request id, so the ledger
records a request id only when the host supplies one (for example a `tool_use_id` seen in a hook) and otherwise stores `null`. Nothing is invented.

Host policy is not changed: no settings edit, no new allow, no hook that answers a prompt.

## Proof status

| Claim | Status |
|---|---|
| A background subagent's prompt waits in the main session while the main agent continues | **Documented, not observed here.** A 2026-11-10 probe lane ran `rtk proxy echo` (a `permissions.ask` rule) and returned at once with no visible prompt, so it exercised no live prompt. Treat the live-prompt behavior as `HOST_PROMPT_BACKGROUND_UNVERIFIED` until an owner-observed run shows a pending prompt while the main agent keeps working |
| The ledger keeps an unanswered ASK pending past 15 minutes, holds several at once, resumes only the intended one, and handles changed head, denial, duplicate and restart | Tested (`tests/test-ask-pending.py`, class `Scenario` and `Resume`) in a disposable directory with a stand-in gate |
| The normal merge gate is still the authority | Tested: resume is refused unless `owner-merge-gate.py check` returns PASS for the same repo, PR and head and prints exactly the recorded action |

Because the live-prompt claim is unverified, the design does not depend on it: with no live prompt the item simply stays pending with `prompt: none`, other work
continues, and the action is issued when the owner is present.

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

## Flow

1. **Record.** Before issuing an action the host will ask about, `record` it (exact action, target, head, dependencies, request id if any, trigger). Issue it from a
   background lane, or leave it pending if the owner is away, and **continue the next runnable task immediately**.
2. **Grace.** `list` shows `move_on` after 15 minutes (`grace_s` is capped at 900). That only means "do other work"; it never changes state, approves or expires the item.
   Several items can be pending; none blocks another.
3. **Resume at a safe checkpoint** (between tasks, never mid-edit). `resume --id <id> --current-head <head just read from GitHub>`:
   - moves `PENDING` to `RESUMING` once; a second resume, a closed item or a lost race is refused;
   - a different head closes the item as `STALE_HEAD` (a new head needs a new independent review and a new record); recording a new head for the same target also supersedes the old item;
   - runs `owner-merge-gate.py check` and requires PASS for the same repo, PR and head and the same printed `merge_command` (the gate covers exact head, independent review evidence, required checks, owner class and host permission);
   - prints `run_exactly` and nothing else. The agent runs that command alone, as the gate requires; **the host asks again and its answer is the only approval.**
4. **Finish.** `finish --outcome executed --ref <proof>` (for a merge, the `owner-merge-gate.py verify` result, run separately), `denied`, `expired`, or `failed`
   (back to `PENDING` with `prompt: unknown`). A duplicate finish is refused and receipted. A `RESUMING` item with no outcome for 30 minutes returns to `PENDING`
   (never to done).
5. **Restart.** State is on disk. `restart --session-id <new>` marks every live prompt `unknown`; nothing is assumed approved and every item re-validates on resume.

## What never counts as approval

Elapsed time, a handoff, a watcher, a subagent report, a notification, or the ledger itself. A subagent that reports "approved" is not evidence; only the host's own
answer to the exact command is, and the merged state must then be verified.

## Separate rules

- The 12-hour heavy-admission escalation rule (`docs/RESOURCE-ADMISSION.md`) concerns queue admission and is unrelated to the 15-minute ASK grace.
- Merge execution, evidence and verification rules in `OWNER-MERGE-WORKFLOW.md` are unchanged.
