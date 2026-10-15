---
name: ticket-pickup-check
description: "Verify ticket readiness, blockers and assignment before binding branch/worktree context."
---

# SKILL.md — ticket-pickup-check

Read `engineering-runtime` first and initialize its host-aware environment before
using the shell examples below. Resolve installed helper paths from that skill;
never borrow another host’s private config, credentials or mutable state.

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
1. Fetch the ticket's current state from the `ENGINEERING_ISSUE_TRACKER`-routed MCP
   (`github`, `clickup`, or `jira`). Only query one provider.
2. Use the tracker’s real workflow states. "Accepted", "Ready for Dev" and "In Sprint"
   indicate ready work; "In Progress" may be a valid owned resume. Explicit user
   authorization to execute approved scope also establishes readiness.
3. "New", "Open" or "Backlog" without scope approval need intake. Blocked work
   requires resolving its actual dependency; "Done" or "Closed" must not be redone.
   "In Review" means continue the existing review lifecycle, not fresh implementation.
4. If readiness is unsupported or conflicts with current evidence: **stop immediately**.
   - Report to the engineer with the current state.
   - Do not begin implementation.

### Check 2 — Blocking dependencies
5. Fetch all "blocks" / "depends on" relationships from the ticket.
6. For each linked blocker ticket, check its current state.
7. If any blocker is not "Done" or "Closed": **stop immediately**.
   - List the specific blocking tickets and their states.
   - Report to the engineer to resolve the dependency.

### Check 3 — Assignee conflict
8. Read the ticket's current assignee field and reconcile the current authorized identity.
9. If the ticket is owned by a different developer or active conflicting session:
   - **Stop immediately.** Do not pick up a ticket already owned by someone else.
   - Report the conflict to the engineer.
10. If unassigned or owned by the current authorized identity: proceed to Check 4.

### Check 4 — Branch, worktree, self-assign, and state transition
11. Derive the branch name using the four-part format:
    ```
    <release-or-phase>/<ticket-number>/<type>/<short-summary>
    ```
    - `<release-or-phase>`: for `legacy_uncoordinated`, resolve the existing
      `$ENGINEERING_CURRENT_RELEASE` env/marker precedence, then the ticket's
      milestone/sprint field. For managed state, use the bootstrap-resolved
      release if present; otherwise use the ticket's milestone/sprint field.
      Never consult legacy markers when status is `managed` or `unavailable`.
      Store the selected prefix in `RELEASE`. Examples: `v0.1.0`, `phase1`, `sprint3`.
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
15. The In Progress transition records pickup. Post a comment only if a new
    scope/dependency decision or blocker needs attention; keep branch/worktree
    location in local context and the PR, rather than duplicating it in Jira.

16. **Bind the ticket reference, release prefix, and worktree path** without
    adopting or overwriting legacy state. Inspect the target host root before
    changing context:
    ```bash
    HOST=$(basename "$ENGINEERING_STATE_DIR")
    CONTEXT_ROOT="${WORKTREE_PATH}/${HOST}"
    CONTEXT_HELPER="${ENGINEERING_RUNTIME}/ticket-state.py"
    CONTEXT_RELEASE_ARGS=()
    if [[ -n ${RELEASE:-} ]]; then
      CONTEXT_RELEASE_ARGS=(--release "$RELEASE")
    fi
    CONTEXT_JSON=$(python3 "$CONTEXT_HELPER" context "$CONTEXT_ROOT" inspect) || exit 1
    ```
    Only when `[[ ! -e "$CONTEXT_ROOT" && ! -L "$CONTEXT_ROOT" ]]`, create a
    managed context with the explicit
    fresh-scope operation:
    ```bash
    CONTEXT_RESULT=$(python3 "$CONTEXT_HELPER" context "$CONTEXT_ROOT" initialize \
      --host "${HOST#.}" --worktree "$WORKTREE_PATH" --ticket "$TICKET" \
      --fresh-owned-scope "${CONTEXT_RELEASE_ARGS[@]}") || exit 1
    ```
    The operation exclusively creates the canonical worktree host directory.
    If it already exists, never initialize over it. For an existing managed
    active context already bound to this same accepted ticket and worktree,
    capture its generation and CAS token from one `inspect`
    result and publish the accepted tuple with those exact values:
    ```bash
    CONTEXT_JSON=$(python3 "$CONTEXT_HELPER" context "$CONTEXT_ROOT" inspect) || exit 1
    CONTEXT_GENERATION=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["generation"])' <<<"$CONTEXT_JSON")
    CONTEXT_TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$CONTEXT_JSON")
    CONTEXT_RESULT=$(python3 "$CONTEXT_HELPER" context "$CONTEXT_ROOT" publish \
      --expected-generation "$CONTEXT_GENERATION" --expected-token "$CONTEXT_TOKEN" \
      --ticket "$TICKET" --worktree "$WORKTREE_PATH" "${CONTEXT_RELEASE_ARGS[@]}") || exit 1
    ```
    A CAS snapshot does not authorize taking over another ticket's context.
    If the existing ticket differs, preserve it and use a fresh owned worktree
    unless an authoritative handover has independently been verified.
    A stale token or generation refuses the update. An existing tombstone or
    unavailable context cannot be reused; preserve it and use a new owned
    worktree. Unexpected legacy markers also block publish; preserve the active
    context and markers and use a fresh worktree. An existing root without a managed object is
    `legacy_uncoordinated`: do not import it, create a managed object inside it,
    or write/truncate `.current-*` markers. Keep legacy contexts unchanged and
    bind this session with environment variables only. Once in the selected
    worktree, run its bootstrap and verify `ENGINEERING_CONTEXT_STATUS` and the
    resolved tuple before continuing. `managed` is authoritative; `unavailable`
    suppresses all fallback; only `legacy_uncoordinated` uses the old env/marker
    precedence.

    ```bash
    cd "$WORKTREE_PATH"
    eval "$(python3 "${ENGINEERING_RUNTIME}/bootstrap.py")"
    ```
    For an uncoordinated legacy root, export the accepted ticket/worktree/release
    for this shell after bootstrap:
    ```bash
    if [[ $ENGINEERING_CONTEXT_STATUS == legacy_uncoordinated ]]; then
      export ENGINEERING_CURRENT_TICKET="$TICKET"
      export ENGINEERING_CURRENT_WORKTREE="$WORKTREE_PATH"
      export ENGINEERING_CURRENT_RELEASE="$RELEASE"
    fi
    ```
    Do not persist them in marker files.
    All subsequent development work happens inside `$WORKTREE_PATH`.

17. Write the initial workflow state file:
    ```bash
    bash "${ENGINEERING_RUNTIME}/workflow-state.sh" write \
      "[ticket-ref]" "dev-impl-loop" "0" "5" "in_progress"
    ```
18. Load any prior session notes for this ticket and surface them:
    ```bash
    bash "${ENGINEERING_RUNTIME}/session-memory.sh" read "[ticket-ref]"
    ```
    If notes exist, display them to the engineer before proceeding so that
    prior context (decisions made, blockers hit, partial work logged) is
    visible at session start. Do not re-execute steps already recorded as done.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report whether work can proceed and any material blocker/owner action. Reference
the ticket. Routine successful state/assignee/worktree checks stay internal.

## Ticket context resolution (for all skills)

All skills that need the ticket reference resolve it in this order:
For `managed` and `unavailable`, use only the bootstrap's
`ENGINEERING_CURRENT_TICKET`; an empty value means no usable context. For
`legacy_uncoordinated`, retain the existing environment-then-marker precedence.
Prompt the engineer if neither source yields a ticket.

Skills should never hardcode a ticket ref or worktree path. Use this pattern:
```bash
if [[ ${ENGINEERING_CONTEXT_STATUS:-legacy_uncoordinated} == legacy_uncoordinated ]]; then
  TICKET="${ENGINEERING_CURRENT_TICKET:-$(cat "${ENGINEERING_CONTEXT_DIR}/.current-ticket" 2>/dev/null || echo '')}"
else
  TICKET="${ENGINEERING_CURRENT_TICKET-}"
fi
if [[ -z "$TICKET" ]]; then
  echo "No active ticket context. Run ticket-pickup-check first." >&2
  exit 1
fi

if [[ ${ENGINEERING_CONTEXT_STATUS:-legacy_uncoordinated} == legacy_uncoordinated ]]; then
  WORKTREE="${ENGINEERING_CURRENT_WORKTREE:-$(cat "${ENGINEERING_CONTEXT_DIR}/.current-worktree" 2>/dev/null || echo '')}"
else
  WORKTREE="${ENGINEERING_CURRENT_WORKTREE-}"
fi
```

Managed state exposes its status, generation, scope ID and CAS token through
the bootstrap environment. The token is an early-observation CAS snapshot, not
proof that a task is released. Legacy marker files remain readable only while
the context object is absent; never create or overwrite them during pickup.

## Safe-Fix Guidance
- Never bypass the state check — implementing a "New" or "Backlog" ticket
  skips intake and decomposition, producing unreviewed work.
- If the assignee field shows a stale assignment (inactive user, old session),
  escalate to the engineer to resolve before self-assigning.
- If two parallel sessions attempt the same ticket simultaneously, the one
  that loses the assignee race must stop and report the conflict.
- If the worktree path already exists (resumed session), use
  `git worktree add "$WORKTREE_PATH" "$BRANCH_NAME"` (without `-b`) to
  attach only if the path is absent. Reuse an already registered healthy path;
  `git worktree add` cannot reattach over an existing directory.
- If worktree creation fails, inspect `git worktree list` and the actual paths.
  Reuse a healthy existing owned worktree. Prune only verified stale records;
  do not delete unknown or dirty worktrees to retry.
