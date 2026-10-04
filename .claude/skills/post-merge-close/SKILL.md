---
name: post-merge-close
description: "Verify merged PR acceptance, close the linked ticket, and resume checkpointed cleanup safely."
---

# SKILL.md — post-merge-close

Read `engineering-runtime` first and initialize its host-aware environment before
using the shell examples below. Resolve installed helper paths from that skill;
never borrow another host’s private config, credentials or mutable state.

## Purpose
After a PR is merged, perform all required close-out actions: transition the
ticket to Done, delete the feature branch, post a completion comment, and
notify the reporter. All steps are idempotent and checkpointed — if the skill
is interrupted and re-run, completed steps are skipped safely.

## Type
Auto-used. Run immediately after a PR merge is confirmed.

## Do Not Assume
- Do not assume the PR was actually merged — verify the merge status before acting.
- Do not assume the ticket reference is in the PR title — check the description too.
- Do not delete the branch before confirming the ticket is closed in the tracker.
- Do not assume the reporter and the assignee are the same person.

## Checkpoint pattern

All steps write their completion to a per-PR checkpoint file, making every
operation safe to re-run after a partial failure.

**Important**: `PR_NUMBER` is extracted in Phase 1 step 3. Initialise these
variables and define the helper functions AFTER step 3, once `PR_NUMBER` is
known:

```bash
TICKET="${ENGINEERING_CURRENT_TICKET:-$(cat "${ENGINEERING_CONTEXT_DIR}/.current-ticket" 2>/dev/null || echo '')}"
CHECKPOINT_DIR="${ENGINEERING_STATE_DIR}/merge-closeout"
mkdir -p "$CHECKPOINT_DIR"
CHECKPOINT="${CHECKPOINT_DIR}/${PR_NUMBER}.json"   # set AFTER PR_NUMBER is known
```

Define checkpoint helpers (shell-to-Python boundary: pass file path and all
values via environment variables — never interpolate them into Python source
strings, as a file path or value containing `'` would break Python syntax):

```bash
# Returns field value or empty string if checkpoint does not exist or field unset.
_checkpoint_get() {
  local field="$1"
  _CP_FILE="$CHECKPOINT" _CP_FIELD="$field" python3 - <<'PYEOF' 2>/dev/null || echo ""
import json, os
try:
    print(json.load(open(os.environ["_CP_FILE"])).get(os.environ["_CP_FIELD"], ""))
except Exception:
    print("")
PYEOF
}

# Writes key=value into the checkpoint JSON atomically.
_checkpoint_set() {
  local key="$1" value="$2"
  _CP_FILE="$CHECKPOINT" _CP_KEY="$key" _CP_VALUE="$value" python3 - <<'PYEOF' 2>/dev/null
import json, os
p   = os.environ["_CP_FILE"]
key = os.environ["_CP_KEY"]
val = os.environ["_CP_VALUE"]
d = {}
try:
    with open(p) as fh:
        d = json.load(fh)
except Exception:
    pass
d[key] = val
tmp = p + ".tmp"
with open(tmp, "w") as fh:
    json.dump(d, fh)
os.replace(tmp, p)
PYEOF
}
```

Before each step, check if it was already completed:
```bash
[[ "$(_checkpoint_get ticket_closed)" == "true" ]] && echo "skip: ticket already closed"
```

## Steps

### Phase 1 — Confirm merge
1. Fetch the PR's current state using `code_repository` MCP.
2. Confirm the PR status is "merged" (not just "closed").
3. Record: PR number, merge commit SHA, merged-at timestamp, base branch.
   Set `PR_NUMBER`, `MERGE_SHA`, `MERGED_AT` from the MCP response.
   **Then** initialise the checkpoint path and helper functions (defined above).
4. If the PR was closed without merging: stop. Do not transition the ticket or
   delete the branch. Report the closure reason to the engineer.
5. Initialise checkpoint:
   ```bash
   _checkpoint_set pr_number "$PR_NUMBER"
   _checkpoint_set merge_sha "$MERGE_SHA"
   _checkpoint_set merged_at "$MERGED_AT"
   ```

### Phase 2 — Close the ticket
6. Skip if `_checkpoint_get ticket_closed` == "true".
7. Fetch the linked ticket reference from the PR description
   (look for `Closes #`, `Fixes #`, `Refs #` patterns, or a ClickUp/JIRA URL).
8. If a ticket reference is found:
   a. Confirm actual acceptance/validation evidence and required merge gates,
      then transition the ticket state to "Done" / "Closed" via
      `ENGINEERING_ISSUE_TRACKER`-routed MCP.
   b. Apply `evidence-first-briefing` to a close comment: verified semantic outcome,
      merged PR and acceptance evidence, plus any remaining owner action. Check
      actual acceptance evidence before transitioning; never assert all criteria
      were verified merely because the PR merged. Reuse an existing completion
      comment rather than posting a duplicate after interruption.
   c. Mark checkpoint: `_checkpoint_set ticket_closed true`
9. If no ticket reference is found: log the gap to the decision log and notify
   the engineer. Do not proceed to branch deletion until resolved.
   ```bash
   bash "${ENGINEERING_RUNTIME}/decision-log.sh" record \
     --ticket "$TICKET" --agent "main-agent" --skill "post-merge-close" \
     --phase "2" --decision "escalate" \
     --reason "No ticket reference found in PR description — cannot auto-close"
   ```

### Phase 3 — Branch cleanup
10. Skip if `_checkpoint_get branch_deleted` == "true".
11. Remove the git worktree for this ticket (must happen before branch deletion):
    ```bash
    WORKTREE_PATH=$(cat "${ENGINEERING_CONTEXT_DIR}/.current-worktree" 2>/dev/null || echo "")
    if [ -n "$WORKTREE_PATH" ] && git worktree list | grep -qF "$WORKTREE_PATH"; then
        git worktree remove "$WORKTREE_PATH"
    fi
    git worktree prune
    rm -f "${ENGINEERING_CONTEXT_DIR}/.current-worktree"
    ```
    If `git worktree remove` fails (uncommitted changes remain), do not use
    `--force`. Report to the engineer — all work must be committed before
    the worktree is removed.
12. Delete the remote feature branch (detect the remote name — do not assume `origin`):
    ```bash
    REMOTE=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null \
        | cut -d'/' -f1 || git remote | head -1 || echo "origin")
    git push "$REMOTE" --delete [feature-branch-name]
    ```
    Do not delete protected branches (`main`, `master`, `release/*`).
14. Delete the local tracking branch (safe delete only):
    ```bash
    git branch -d [feature-branch-name]
    ```
    If `-d` fails (branch not fully merged in local index), log and skip —
    do not use `-D`. Report to the engineer.
15. Mark checkpoint: `_checkpoint_set branch_deleted true`

### Phase 4 — Notify reporter
14. Skip if `_checkpoint_get reporter_notified` == "true".
15. Identify the ticket reporter (original filer, not the implementer).
16. If authorized notification is needed, tag the reporter in the existing
    completion comment. Avoid a second comment that repeats the PR outcome.
17. If an authorized project channel notification is needed, apply
    `evidence-first-briefing`: changed capability/behavior and PR reference, with
    material limitation or owner action. Tool availability alone is not permission.
18. Mark checkpoint: `_checkpoint_set reporter_notified true`

### Phase 5 — Finalise
19. Write final workflow state:
    ```bash
    bash "${ENGINEERING_RUNTIME}/workflow-state.sh" write \
      "$TICKET" "post-merge-close" "done" "done" "complete"
    ```
20. Archive the workflow state for this ticket:
    ```bash
    bash "${ENGINEERING_RUNTIME}/workflow-state.sh" archive "$TICKET"
    ```
21. Record decision:
    ```bash
    bash "${ENGINEERING_RUNTIME}/decision-log.sh" record \
      --ticket "$TICKET" --agent "main-agent" --skill "post-merge-close" \
      --phase "5" --decision "complete" \
      --reason "Ticket closed, branch deleted, reporter notified" \
      --context "merge SHA: [sha]"
    ```
22. Clear session notes for this ticket — the work is done:
    ```bash
    bash "${ENGINEERING_RUNTIME}/session-memory.sh" clear "$TICKET"
    ```
23. Clean up the checkpoint file:
    ```bash
    rm -f "$CHECKPOINT"
    ```

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report the verified semantic outcome with merge/acceptance references. Surface
failed cleanup, missing evidence or required owner action; keep successful
checkpoint mechanics internal.

## Safe-Fix Guidance
- If the skill fails mid-way, re-run it — completed steps are checkpointed and skipped.
- Never use `git branch -D` (force delete) — if `-d` fails, report to the engineer.
- Do not close a ticket as Done if the PR was reverted — escalate instead.
- If Slack notification fails, the ticket comment is sufficient — do not block on it.
- Protected branch delete attempts exit non-zero — treat as a bug, report immediately.
