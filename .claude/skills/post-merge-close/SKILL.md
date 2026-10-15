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

At the start of closeout, before any lifecycle action, retain the context status,
generation, scope ID and CAS token exported by bootstrap, together with the exact
ticket/worktree tuple. After verifying this exact task owns native ticket state
and has released its resources, observe notes and workflow state before acting.
Capture their revision tokens through `session-memory.sh snapshot "$TICKET"` and
`workflow-state.sh snapshot "$TICKET"`. Keep these original tokens with this
closeout's observations; they contain identity/digests, not note contents. A
token is not task-release authority.
Never replace a stale token with a newly observed token merely to make cleanup
succeed: a changed generation may belong to resumed or concurrent work.

Capture these values once before acting so a later bootstrap cannot replace the
CAS snapshot:
```bash
CONTEXT_STATUS="$ENGINEERING_CONTEXT_STATUS"
CONTEXT_GENERATION="$ENGINEERING_CONTEXT_GENERATION"
CONTEXT_SCOPE_ID="$ENGINEERING_CONTEXT_SCOPE_ID"
CONTEXT_CAS_TOKEN="$ENGINEERING_CONTEXT_CAS_TOKEN"
CONTEXT_TICKET="$ENGINEERING_CURRENT_TICKET"
CONTEXT_WORKTREE="$ENGINEERING_CURRENT_WORKTREE"
```

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
5. **Context (`context_cleared`).** For `managed` active state, verify the
   original early ticket and worktree match this closeout. Clear only with that
   original generation and token; never replace a stale snapshot:
   ```bash
   CONTEXT_CLEAR_RESULT=$(python3 "${ENGINEERING_RUNTIME}/ticket-state.py" context \
     "$ENGINEERING_CONTEXT_DIR" clear \
     --expected-generation "$CONTEXT_GENERATION" \
     --expected-token "$CONTEXT_CAS_TOKEN") || exit 1
   CONTEXT_AFTER=$(python3 "${ENGINEERING_RUNTIME}/ticket-state.py" context \
     "$ENGINEERING_CONTEXT_DIR" inspect) || exit 1
   ```
   Verify both captured responses and the original early tuple. The subsequent
   inspect must show a managed `cleared`
   tombstone with an empty tuple. This retains the context object and every
   legacy file. A stale token, changed tuple, malformed object or unavailable
   helper leaves the context untouched and this outcome pending. If removing
   the worktree also removed its managed context root, absence alone cannot
   satisfy this outcome: removal-time evidence must bind that exact context
   generation and fingerprint. Until that integration exists, keep the outcome
   pending and `effective_complete` false. Never clear context before a failed
   worktree removal merely to avoid this gap. For
   `legacy_uncoordinated`, preserve existing markers and report
   `context_changed_or_uncoordinated`; if all three markers were already absent,
   verify absence and record the outcome. `unavailable` never falls back to
   markers and stays pending. Never modify another host's context.
6. **Reporter (`reporter_handled`).** Identify the actual reporter. Only authorized
   notifications may be sent; tag in the existing completion comment when needed.
   Connector availability grants no notification authority. Record an authorized
   verified notification or explicit not-required decision; don't claim a message
   was sent when it wasn't. Notification failure is reported without repeating
   the completion comment.

Only after all outcomes are verified, archive the previously observed native
workflow state using its original token. Do not overwrite the current state with
an unconditional "complete" write: a resumed same-ticket task may own it now.
The retained shared receipt records verified completion separately.

```bash
bash "$ENGINEERING_RUNTIME/workflow-state.sh" archive "$TICKET" "$STATE_TOKEN"
```

After verifying the archive exists in the bound native ticket scope, record
`{"op":"mark_finalization","step":"workflow_archived","evidence":"Exact archive path and verified state"}`.
Record the verified outcome and merge SHA through `decision-log.sh`, then clear
only this completed ticket's session notes via
`session-memory.sh clear "$TICKET" "$NOTES_TOKEN"`. The helper durably archives
notes before clearing their current path; it never destroys session history.
All managed append/write/clear/archive operations use stable per-ticket writer
locks. Missing/stale tokens and legacy unknown writer revisions preserve data.
If a newer writer intervened, retain pending finalization for ownership resolution.
Re-observe
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
