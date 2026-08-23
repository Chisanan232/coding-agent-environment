# SKILL.md — ticket-pickup-check

## Purpose
Before any implementation work begins, verify the target ticket is in an
acceptable state: correct workflow state ("Accepted"), no unresolved
blockers, no assignee conflict. Self-assign the ticket if all checks pass.

## Type
Auto-used. Run as the first action before any implementation task. Must
pass before `dev-impl-loop` begins.

## Do Not Assume
- Do not assume a ticket is ready just because it was handed to you.
- Do not assume no one else is working on it — always check the assignee field.
- Do not assume dependencies are resolved — check linked blocker tickets explicitly.
- Do not assume the ticket state is current — fetch fresh state from the tracker.

## Steps

### Check 1 — Ticket workflow state
1. Fetch the ticket's current state from the `CLAUDE_ISSUE_TRACKER`-routed MCP
   (`github`, `clickup`, or `jira`). Only query one provider.
2. Acceptable states: "Accepted", "Ready for Dev", "In Sprint".
3. Unacceptable states: "New", "Open", "Backlog", "Blocked", "In Review", "Done", "Closed".
4. If the state is not acceptable: **stop immediately**.
   - Report to the engineer with the current state.
   - Do not begin implementation.

### Check 2 — Blocking dependencies
5. Fetch all "blocks" / "depends on" relationships from the ticket.
6. For each linked blocker ticket, check its current state.
7. If any blocker is not "Done" or "Closed": **stop immediately**.
   - List the specific blocking tickets and their states.
   - Report to the engineer to resolve the dependency.

### Check 3 — Assignee conflict
8. Read the ticket's current assignee field.
9. If the ticket is assigned to a named developer or another session:
   - **Stop immediately.** Do not pick up a ticket already owned by someone else.
   - Report the conflict to the engineer.
10. If the ticket is unassigned: proceed to Check 4.

### Check 4 — Branch, worktree, self-assign, and state transition
11. Derive the branch name using the four-part format:
    ```
    <release-or-phase>/<ticket-number>/<type>/<short-summary>
    ```
    - `<release-or-phase>`: resolve in order — `$CLAUDE_CURRENT_RELEASE` env var,
      `.claude/.current-release` file, or the ticket's milestone/sprint field
      from the issue tracker. Examples: `v0.1.0`, `phase1`, `sprint3`.
    - `<ticket-number>`: exact ticket reference (e.g., `TEST-1`, `PROJ-123`, `42`).
    - `<type>`: GitEmoji category slug for the primary change type —
      `feat`, `fix`, `refactor`, `test`, `docs`, `config`, `deps`, `remove`, `lint`.
    - `<short-summary>`: 2–4 words from the ticket title in `snake_case`, max 30 characters.
    - Examples: `v0.1.0/TEST-1/feat/add_new_endpoint`, `phase1/PROJ-123/fix/auth_token_refresh`

12. Create a git worktree and branch for this ticket:
    ```bash
    REPO_ROOT=$(git rev-parse --show-toplevel)
    REPO_NAME=$(basename "$REPO_ROOT")
    BRANCH_NAME="[release-or-phase]/[ticket-number]/[type]/[short-summary]"
    # e.g. v0.1.0/TEST-1/feat/add_new_endpoint

    # Worktree path: replace '/' with '-' to avoid creating nested directories
    WORKTREE_SUFFIX=$(echo "$BRANCH_NAME" | tr '/' '-')
    WORKTREE_PATH="${REPO_ROOT}/../${REPO_NAME}-${WORKTREE_SUFFIX}"

    # Create the worktree and branch (-b creates a new branch):
    git worktree add "$WORKTREE_PATH" -b "$BRANCH_NAME"
    # If the branch already exists (resumed session), omit -b:
    # git worktree add "$WORKTREE_PATH" "$BRANCH_NAME"
    ```

13. Assign the ticket to the current session / developer identity.
14. Transition the ticket state to "In Progress".
15. Post a brief start comment on the ticket:
    ```
    Starting implementation — session [timestamp].
    Branch: [branch-name]
    Worktree: [worktree-path]
    ```

16. **Bind the ticket reference, release prefix, and worktree path** to the current session:
    ```bash
    # In the MAIN repo — write context files for cross-session reference
    mkdir -p .claude
    echo "[ticket-ref]"        > .claude/.current-ticket
    echo "$WORKTREE_PATH"      > .claude/.current-worktree
    echo "[release-or-phase]"  > .claude/.current-release

    # In the WORKTREE — write the ticket context so skills work from inside it
    mkdir -p "${WORKTREE_PATH}/.claude"
    echo "[ticket-ref]"       > "${WORKTREE_PATH}/.claude/.current-ticket"
    echo "[release-or-phase]" > "${WORKTREE_PATH}/.claude/.current-release"

    # Export for the current shell session
    export CLAUDE_CURRENT_TICKET="[ticket-ref]"
    export CLAUDE_CURRENT_WORKTREE="$WORKTREE_PATH"
    export CLAUDE_CURRENT_RELEASE="[release-or-phase]"
    ```
    All subsequent development work happens inside `$WORKTREE_PATH`.

17. Write the initial workflow state file:
    ```bash
    bash ~/.claude/hooks/workflow-state.sh write \
      "[ticket-ref]" "dev-impl-loop" "0" "5" "in_progress"
    ```
18. Load any prior session notes for this ticket and surface them:
    ```bash
    bash ~/.claude/hooks/session-memory.sh read "[ticket-ref]"
    ```
    If notes exist, display them to the engineer before proceeding so that
    prior context (decisions made, blockers hit, partial work logged) is
    visible at session start. Do not re-execute steps already recorded as done.

## Output

```
## Ticket pickup check — [ticket reference]

| Check | Result | Detail |
|---|---|---|
| Workflow state | ✅ Accepted / ❌ [state] | [acceptable / reason blocked] |
| Blocker check | ✅ No blockers / ❌ Blocked | [blocker ticket refs if any] |
| Assignee check | ✅ Unassigned → self-assigned / ❌ Assigned to [name] | |
| Branch / worktree | ✅ [branch-name] at [worktree-path] / ❌ creation failed | |

### Decision
- Proceed with implementation: yes / no
- Reason (if no): [reason]
- Next action (if no): escalate to the engineer
```

## Ticket context resolution (for all skills)

All skills that need the ticket reference resolve it in this order:
1. `$CLAUDE_CURRENT_TICKET` environment variable (set by CI or the engineer)
2. `.claude/.current-ticket` file in the repository root (written by this skill)
3. Prompt the engineer if neither is set

Skills should never hardcode a ticket ref or worktree path. Use this pattern:
```bash
TICKET="${CLAUDE_CURRENT_TICKET:-$(cat .claude/.current-ticket 2>/dev/null || echo '')}"
if [[ -z "$TICKET" ]]; then
  echo "No active ticket context. Run ticket-pickup-check first." >&2
  exit 1
fi

WORKTREE="${CLAUDE_CURRENT_WORKTREE:-$(cat .claude/.current-worktree 2>/dev/null || echo '')}"
```

Ensure `.claude/.current-ticket`, `.claude/.current-worktree`, and
`.claude/.current-release` are listed in `.gitignore` — they are session
state, not source code.

## Safe-Fix Guidance
- Never bypass the state check — implementing a "New" or "Backlog" ticket
  skips intake and decomposition, producing unreviewed work.
- If the assignee field shows a stale assignment (inactive user, old session),
  escalate to the engineer to resolve before self-assigning.
- If two parallel sessions attempt the same ticket simultaneously, the one
  that loses the assignee race must stop and report the conflict.
- If the worktree path already exists (resumed session), use
  `git worktree add "$WORKTREE_PATH" "$BRANCH_NAME"` (without `-b`) to
  reattach to the existing branch without recreating it.
- If worktree creation fails, run `git worktree list` to inspect active
  worktrees and `git worktree prune` to remove stale entries, then retry.
