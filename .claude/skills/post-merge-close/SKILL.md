---
name: post-merge-close
description: "Verify merged PR acceptance, close the linked ticket, and resume checkpointed cleanup safely."
---

# Post-merge closeout

Read `engineering-runtime` and initialize its host-aware environment first.
Use one canonical workflow on both hosts. This skill is agent-invoked after
confirmed merge; it is not an installed native merge/session-end hook.

## Confirm and bind

Fetch the current PR through the connected repository provider. Require merged
status, repository identity, PR number, exact merged head, merge SHA, merged-at
and base/head branches. A closed unmerged PR stops here. A reverted merge or
new unique topic commits prevents automatic closure/removal.

Resolve the linked ticket from description/title and verify actual acceptance,
required checks and review gates before transition. No ticket reference means
closeout stays pending. Use `ENGINEERING_ISSUE_TRACKER`; do not infer completion
from merge alone. Resolve the real tracking remote and Git common directory;
never fall back to the first remote or a guessed origin.

Create a private JSON binding file outside Git, with these explicit fields:
`repository` (credential-free HTTPS remote identity), `pr` (integer), `head_sha`,
`merge_sha`, `ticket`, `git_common_dir`, `worktree`, `branch`, `base_branch`,
`remote`. Paths must be absolute. Resolve all values from authoritative evidence,
not a stale `.current-worktree` marker alone. Keep merged-at and acceptance
references in verification evidence. Never include credentials or session logs.

Start the shared receipt holder using a native background/process handle:

```bash
python3 "$ENGINEERING_RUNTIME/closeout.py" hold --binding "$BINDING_FILE"
```

Keep its stdin open. Its first JSON reply must be `status: locked`; `busy`
(exit 75) means another host owns this closeout. Do not act while busy. Keep
the same live handle through observe → act → verify → checkpoint. Send JSON
requests through that handle; do not invoke a second helper for each update:

```json
{"op":"status"}
{"op":"mark","step":"ticket_closed","evidence":"Verified tracker state and existing acceptance comment"}
{"op":"release"}
```

Both hosts use `~/.local/state/coding-agent-environment/closeout`. The helper
namespaces by repository+PR, binds immutable merge/workflow identity, holds a
kernel lock, writes atomically and retains terminal receipts. It only records
outcomes; it neither authorizes deletion nor executes Git/tracker commands.
If the holder ends or its authoritative handle disappears, stop mutations,
reacquire and re-observe. Never infer success from a started action. Receipt
corruption or changed binding refuses; preserve it for diagnosis. Old host-private
PR-number-only checkpoints remain untouched and require explicit verified
repository/merge reconciliation; never auto-import them.

## Reconcile each outcome

Before every action, inspect the holder's verified steps and current external
state. Use `effective_complete`, never the historical `record.complete` flag.
Schema-1 receipts remain readable but report `finalization_unverified`; do not
repeat their already verified cleanup merely to upgrade the receipt. A crash can occur after an action succeeds but before its receipt write;
re-observation prevents duplicate comments/deletions. Mark distinct outcomes
only after successful verification. A failed operation remains pending.

1. **Ticket closure (`ticket_closed`).** Confirm acceptance and merge gates,
   transition through the tracker's actual supported states, and use
   `evidence-first-briefing` for one completion comment. Reuse the existing
   comment on retry. If any required criterion remains unverified, keep the
   ticket open with its precise limitation; do not record closure.
2. **Owned worktree (`worktree_removed`).** Retire only this workflow's explicitly
   owned worktree after owned jobs have settled and no conflicting session is
   known. Confirm canonical repository/common-directory, exact branch/head,
   merge ancestry, clean tracked/untracked/ignored state and absence of unique
   artifacts. Inspect fresh file/process use with bounded complete probes.
   Unknown ownership, unavailable probes, dirty/unmerged/unique data, locked
   worktrees or active Claude/Codex/subagent/build use refuses. An empty lsof,
   timestamp, expired status tracker, old PID, or Done ticket alone is insufficient.
   Run ordinary `git worktree remove "$WORKTREE_PATH"` from a retained checkout,
   after leaving the owned worktree. Never use `--force`, broad prune or git clean.
   Verify its exact registration is absent. Already absent counts only after
   bound identity/registration is checked. Preserve context on any failure.
3. **Remote branch (`remote_branch_removed`).** Re-fetch exact PR/head and remote
   ref. Confirm it has no newer commits and is the bound topic branch. Refuse
   main/master, the base branch, protected refs, release branches or uncertain
   governance. Require server-side expected-head comparison during deletion,
   using the verified exact SHA (Git's `--force-with-lease=refs/heads/BRANCH:SHA`
   together with an explicit delete refspec provides a conditional deletion,
   not a history rewrite). Never omit the expected SHA or use blind `--force`.
   A concurrent remote update rejects deletion; preserve and report it. Verify
   exact-ref absence after success.
   Do not delete branches before acceptance/ticket closure.
4. **Local branch (`local_branch_removed`).** Verify the bound topic branch has no
   new commits or other linked worktrees. Use `git branch -d "$FEATURE_BRANCH"`
   only and verify absence. Failure stays pending; never use `-D`.
5. **Context (`context_cleared`).** Current context writers are not all coordinated
   by a shared lock. Preserve existing `.current-ticket`, `.current-worktree`
   and `.current-release` markers and report `context_changed_or_uncoordinated`;
   compare-then-unlink could erase a concurrent session's assignment. If all
   three markers were already absent, verify absence and record this outcome.
   Never modify another host's private markers. Use explicit environment context
   for the next task until a coordinated writer contract is available.
6. **Reporter (`reporter_handled`).** Identify the actual reporter. Only authorized
   notifications may be sent; tag in the existing completion comment when needed.
   Connector availability grants no notification authority. Record an authorized
   verified notification or explicit not-required decision; don't claim a message
   was sent when it wasn't. Notification failure is reported without repeating
   the completion comment.

Only after all outcomes are verified, finalize native workflow state:

```bash
bash "$ENGINEERING_RUNTIME/workflow-state.sh" write \
  "$TICKET" post-merge-close done done complete
bash "$ENGINEERING_RUNTIME/workflow-state.sh" archive "$TICKET"
```

After verifying the archive exists in the bound native ticket scope, record
`{"op":"mark_finalization","step":"workflow_archived","evidence":"Exact archive path and verified state"}`.
Record the verified outcome and merge SHA through `decision-log.sh`, then clear
only this completed ticket's session notes via `session-memory.sh`. Re-observe
and reference an existing matching durable decision on retry; do not append a
duplicate simply because its checkpoint is missing. Record `decision_recorded`
with that durable reference and `notes_cleared` only after verifying absence in
the exact host/ticket scope, using `mark_finalization` requests. These three
finalization outcomes are required alongside the six lifecycle steps. If archive,
log or notes handling fails, keep the holder open or release with pending
finalization; re-observe these operations on retry. Send `{"op":"finish"}`
after all nine outcomes are verified and `pending` is empty. Require its reply
to report `effective_complete: true`, then send `{"op":"release"}`. Keep the terminal receipt;
removing it would lose idempotency evidence. Release on an unresolved refusal
and retain the partial record rather than marking the workflow complete.

Storage candidates go through the established Glomeris CLI/policy/executor,
never raw recursive deletion. Post-merge success does not grant cache authority.
See `engineering-runtime` for shared storage integration and diagnostics.

## Report

Use `evidence-first-briefing`: verified acceptance/merge references, actual cleanup
outcomes, and precise pending ownership/configuration/notification/finalization
work. Do not declare success merely because a PR merged or a receipt exists.

Receipt schema 2 requires the corrected helper in both hosts. Old helpers refuse
new receipts safely; install both host copies through the existing scoped
installer before reconciliation. Status/hold preserve schema-1 bytes. The first
valid `mark_finalization` under the held lock upgrades the binding and existing
evidence atomically without inferring historical finalization. Unknown ownership
or active same-ticket work still prevents state/notes mutation.
