# SKILL.md — dev-impl-loop

## Purpose
Drive a single ticket through the full implementation cycle:
implement → run relative tests → iterate until green → full test suite →
pre-commit → explicit QA handoff. Provides a defined entry point, exit
condition, and circuit breaker threshold for every iteration phase.

## Type
Auto-used. Run immediately after `ticket-pickup-check` passes.

## Do Not Assume
- Do not assume the branch is current — pull before writing any code.
- Do not assume relative tests passing means the full suite passes.
- Do not assume a passing full suite means pre-commit will pass.
- Do not assume implementation is done until QA verdict is "ready".

## Ticket context
Resolve the active ticket reference at the start of every phase:
```bash
TICKET="${CLAUDE_CURRENT_TICKET:-$(cat .claude/.current-ticket 2>/dev/null || echo '')}"
```
If empty, stop and ask the engineer to run `ticket-pickup-check` first.

## Steps

### Phase 0 — Environment verification (before the loop starts)
1. Resolve the active worktree and change into it:
   ```bash
   WORKTREE="${CLAUDE_CURRENT_WORKTREE:-$(cat .claude/.current-worktree 2>/dev/null || echo '')}"
   if [ -n "$WORKTREE" ] && [ -d "$WORKTREE" ]; then
     cd "$WORKTREE"
   elif [ -n "$WORKTREE" ]; then
     echo "Worktree path '$WORKTREE' does not exist — run ticket-pickup-check first" >&2
     exit 1
   fi
   # For new branches created by ticket-pickup-check, no upstream exists yet.
   # Pull only when an upstream tracking branch is configured:
   UPSTREAM_REMOTE=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null | cut -d'/' -f1 || echo "")
   ```
2. Load and surface prior session notes:
   ```bash
   bash ~/.claude/hooks/session-memory.sh read "$TICKET"
   ```
   Review any recorded decisions, partial work, or blockers before proceeding.
   Do not repeat steps already logged as complete in session notes.
3. Confirm the circuit breaker for this ticket is in "closed" state:
   ```bash
   bash ~/.claude/hooks/circuit-breaker-gate.sh check "$TICKET"
   ```
4. Pull from the branch's configured upstream when one exists:
   ```bash
   if [ -n "$UPSTREAM_REMOTE" ]; then
     git fetch "$UPSTREAM_REMOTE" --quiet
     git pull --rebase
   fi
   ```
5. Confirm working directory is clean (no stale changes from a previous session).
6. Update workflow state: step 1 of 5.
   ```bash
   bash ~/.claude/hooks/workflow-state.sh write \
     "$TICKET" "dev-impl-loop" "1" "5" "in_progress"
   ```
   Record the decision:
   ```bash
   bash ~/.claude/hooks/decision-log.sh record \
     --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
     --phase "0" --decision "proceed" \
     --reason "Circuit closed, branch current, working tree clean"
   ```

### Phase 1 — Implementation loop (relative tests)
6. Begin iterative implementation: write code, run relative tests, fix failures,
   repeat until all acceptance criteria are covered and relative tests are green.
7. Within each loop iteration:
   a. Implement **one unit of work** — the smallest independently meaningful
      piece: a new data model, a new function, a bug fix, a single refactoring
      step, or a requirement adjustment. Do not bundle multiple units into one
      iteration. Follow all conventions in CLAUDE.md (naming, structure, type hints).
   b. Run **relative tests only** — tests in the affected module or package.
      Do not run the full suite here (too slow for iteration).
   c. If relative tests pass → commit the change with a GitEmoji message.
      One unit of work = one commit. Examples:
      - `✨ model(order): Add OrderStatus enum with PENDING, CONFIRMED, CANCELLED`
      - `✨ repo(order): Add OrderRepository.find_by_user_id()`
      - `🐛 service(payment): Fix double-charge on retry by checking idempotency key`
      - `♻️ api(user): Extract _build_response() to remove duplication`
      - `✅ test(order): Add unit tests for OrderRepository.find_by_user_id`
   d. If relative tests fail:
      - Analyze the root cause (do not guess — read the failure output).
      - Apply the minimal fix.
      - Re-run relative tests. Repeat from (b).
      - Record the failure and check the circuit breaker:
        ```bash
        bash ~/.claude/hooks/circuit-breaker-gate.sh record-failure "$TICKET" 5
        ```
        If the circuit opens, stop and escalate to `dev-lead-agent`.
   e. If relative tests pass after a fix:
      ```bash
      bash ~/.claude/hooks/circuit-breaker-gate.sh record-success "$TICKET"
      ```
8. Continue iterations until all ticket acceptance criteria are implemented
   and relative tests are green.
9. Exit the implementation loop. Do not proceed to Phase 2 until all relative
   tests are green and all acceptance criteria are covered.

### Phase 2 — Full test suite
10. Run the complete test suite (all modules, not just relative).
    Update workflow state: step 2 of 5.
    ```bash
    bash ~/.claude/hooks/workflow-state.sh write \
      "$TICKET" "dev-impl-loop" "2" "5" "in_progress"
    ```
11. If any test fails:
    a. Determine: is the failure in code I changed, or pre-existing?
    b. Pre-existing failure → document it, report to `dev-lead-agent`, do not fix.
       ```bash
       bash ~/.claude/hooks/decision-log.sh record \
         --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
         --phase "2" --decision "escalate" \
         --reason "Pre-existing test failure — not caused by this change" \
         --context "[test name and failure output]"
       ```
    c. Failure in changed code → one fix iteration to resolve.
       Record failure and check circuit breaker:
       ```bash
       bash ~/.claude/hooks/circuit-breaker-gate.sh record-failure "$TICKET" 3
       ```
       On success: `bash ~/.claude/hooks/circuit-breaker-gate.sh record-success "$TICKET"`
12. All tests must pass before proceeding to Phase 3.
    Record decision:
    ```bash
    bash ~/.claude/hooks/decision-log.sh record \
      --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
      --phase "2" --decision "proceed" \
      --reason "Full test suite green" --context "[N passed, 0 failed]"
    ```

### Phase 3 — Pre-commit checks
13. Run `pre-commit run --all-files`.
    Update workflow state: step 3 of 5.
    ```bash
    bash ~/.claude/hooks/workflow-state.sh write \
      "$TICKET" "dev-impl-loop" "3" "5" "in_progress"
    ```
14. If any check fails: use the language-appropriate pre-commit repair skill
    (e.g., `python-precommit-repair` for Python) or linter-fixing skill
    (e.g., `python-ruff-fixing` for Python/ruff). Re-run after repair.
    Do not use `--no-verify`.
15. When all checks pass: write the test sentinel (scoped to this repo+branch).
    ```bash
    SENTINEL_BASE="${CLAUDE_SENTINEL_DIR:-${HOME}/.claude/sentinels}"
    # Portable SHA-256: shasum (macOS/BSD) with fallback to sha256sum (Linux/GNU)
    _sha256() { shasum -a 256 2>/dev/null || sha256sum; }
    # Resolve repo URL from whatever remote is configured — do not hardcode 'origin'.
    # Hardcoding produces a shared 'unknown' key when the remote has a non-standard name,
    # causing sentinel collisions between repos. Must match full-test-gate.sh exactly.
    _FIRST_REMOTE=$(git remote 2>/dev/null | head -1 || echo "")
    REPO_REMOTE_URL=$([ -n "$_FIRST_REMOTE" ] && git remote get-url "$_FIRST_REMOTE" 2>/dev/null \
        || echo "unknown")
    REPO_KEY=$(echo "$REPO_REMOTE_URL" | _sha256 | cut -c1-12)
    BRANCH=$(git branch --show-current | tr '/' '_')
    mkdir -p "${SENTINEL_BASE}/${REPO_KEY}/${BRANCH}"
    touch "${SENTINEL_BASE}/${REPO_KEY}/${BRANCH}/.last-test-pass"
    ```
    Record decision:
    ```bash
    bash ~/.claude/hooks/decision-log.sh record \
      --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
      --phase "3" --decision "proceed" \
      --reason "Pre-commit clean; sentinel updated"
    ```

### Phase 4 — QA handoff (explicit trigger)
16. Update ticket state to "Ready for QA" in the issue tracker.
    Update workflow state: step 4 of 5.
    ```bash
    bash ~/.claude/hooks/workflow-state.sh write \
      "$TICKET" "dev-impl-loop" "4" "5" "in_progress"
    ```
    Record decision:
    ```bash
    bash ~/.claude/hooks/decision-log.sh record \
      --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
      --phase "4" --decision "qa-handoff" \
      --reason "All phases green; signalling qa-agent for acceptance-validation"
    ```
17. Post a QA handoff comment on the ticket:
    ```
    ## Implementation complete — ready for QA

    ### What was implemented
    [one paragraph summary]

    ### Tests added/changed
    - [test file]: [what it covers]

    ### Known edge cases or concerns
    - [any area that needs extra attention during QA]

    Requesting qa-agent to begin acceptance-validation.
    ```
18. **Explicitly signal `qa-agent`** to begin `acceptance-validation`.
    Do not proceed until the qa-agent verdict arrives.

### Phase 5 — Post-QA resolution
19. If qa-agent verdict is "ready":
    a. Update workflow state: step 5 of 5, status "complete".
       ```bash
       bash ~/.claude/hooks/workflow-state.sh write \
         "$TICKET" "dev-impl-loop" "5" "5" "complete"
       bash ~/.claude/hooks/decision-log.sh record \
         --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
         --phase "5" --decision "open-pr" \
         --reason "QA verdict: ready" --context "[qa-agent verdict summary]"
       ```
    b. Open a PR using `code-review-prep` and `pr-readiness` skills.
    c. Link the PR to the ticket.
20. If qa-agent verdict is "blocked":
    a. Read the blocking items from the verdict output.
    b. Re-enter the Phase 1 implementation loop to address each item.
       Circuit breaker applies — max attempts before escalating.
    c. After fixes: re-run Phase 2 (full suite) and Phase 3 (pre-commit)
       before signaling QA again.

## Circuit breaker thresholds
- Phase 1 (relative tests): max **5 consecutive failures** or **60 min** elapsed.
- Phase 2 (full suite repair): max **3 consecutive failures** or **30 min** elapsed.
- Phase 5 (post-QA repair): max **3 QA rejection cycles** before escalating.

When the circuit breaker trips:
1. Stop the loop immediately.
2. Write state as "circuit_open" with an escalation reason:
   ```bash
   bash ~/.claude/hooks/workflow-state.sh write \
     "$TICKET" "dev-impl-loop" "[current-step]" "5" "escalated"
   bash ~/.claude/hooks/decision-log.sh record \
     --ticket "$TICKET" --agent "dev-agent" --skill "dev-impl-loop" \
     --phase "[current-phase]" --decision "escalate" \
     --reason "Circuit open after [N] consecutive failures" \
     --context "[last failure output summary]"
   ```
3. Write a session note so the next session can see why work was interrupted:
   ```bash
   bash ~/.claude/hooks/session-memory.sh append "$TICKET" \
     "Circuit breaker tripped" \
     "Phase [current-phase] hit [N] consecutive failures. Last error: [summary]. Awaiting engineer reset."
   ```
4. Report to `dev-lead-agent` with the failure summary and ticket reference.
5. Do not retry until the engineer resets the breaker:
   `bash ~/.claude/hooks/circuit-breaker-gate.sh reset $TICKET`

## Output
On successful completion: PR opened, linked to ticket, workflow state = complete.

## Safe-Fix Guidance
- Do not skip Phase 2 (full suite) even if Phase 1 relative tests are green.
- Do not open the PR before qa-agent produces a "ready" verdict.
- Do not mark work complete while the circuit breaker is open.
- If the implementation loop exits without all acceptance criteria met, that is a
  decomposition problem — escalate to `dev-lead-agent`.
