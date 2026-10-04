---
name: pr-feedback-response
description: "Resolve substantive review concerns with evidence and request review while retaining unresolved threads."
---

# SKILL.md — pr-feedback-response

Read `engineering-runtime` first and initialize its host-aware environment before
using the shell examples below. Resolve installed helper paths from that skill;
never borrow another host’s private config, credentials or mutable state.

## Purpose
Process reviewer comments on an open PR, address each one with targeted code
changes, and re-request review when all feedback is resolved. Ensures no
reviewer comment is skipped or silently dropped.

## Type
Auto-used. Run when a PR has new review comments or a "Request Changes" verdict.

## Do Not Assume
- Do not assume all comments require code changes — some require a reply only.
- Do not assume a comment is resolved just because you addressed the intent;
  verify the reviewer's exact ask was met.
- Do not assume one round of feedback is the last — the reviewer may comment again.
- Do not force-push during active review.

## Steps

### Phase 1 — Triage feedback
1. Fetch the PR's current review state using `code_repository` MCP
   (GitHub MCP `pull_request_read`).
2. Collect all unresolved review comments and Request Changes verdicts.
3. Categorize each comment:
   - **Must-fix**: blocking issue (correctness, security, policy violation).
   - **Should-fix**: strong preference, non-blocking but important.
   - **Discuss**: disagreement requiring dialogue before action.
   - **Acknowledge**: informational, no code change needed.
4. Sort: Must-fix first, then Should-fix, then Discuss, then Acknowledge.
5. Keep triage internally; surface blocking/disputed concerns that affect a decision.

### Phase 2 — Address each comment
6. For each Must-fix and Should-fix comment:
   a. Read the relevant code in context (do not patch blindly).
   b. Apply the minimal change that satisfies the comment.
   c. Run impacted tests after the change.
   d. Commit the fix as a standalone atomic commit referencing the reviewer:
      Example: `🔧 fix(module): Address review feedback — [short description]`
7. For each Discuss comment:
   a. Post a reply on the PR explaining the trade-off or asking a clarifying
      question. Do not make the change until consensus is reached.
8. For informational comments, reply only if acknowledgment is requested or
   adds a decision/action; omit receipt-only noise.

### Phase 3 — Validation after all fixes
9. Run the full test suite after all Must-fix and Should-fix changes are applied.
10. Run `pre-commit run --all-files`.
11. Both must pass before requesting re-review.

### Phase 4 — Re-request review
12. Push the branch (all gate hooks must pass).
13. Apply `evidence-first-briefing` to any substantive reply or re-review request.
    State the concern resolved, changed behavior and relevant test/commit/thread
    reference. Distinguish an implemented fix from reviewer acceptance; surface
    remaining disputed/unverified items. Do not repeat resolved thread details.
14. Re-request review from all reviewers who had active "Request Changes" verdicts.
15. Update workflow state:
    ```bash
    bash "${ENGINEERING_RUNTIME}/workflow-state.sh" write \
      "[ticket-ref]" "pr-feedback-response" "done" "done" "awaiting_review"
    ```

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Report the review decision/action, resolution evidence and remaining owner
action. Reference threads for detail; do not publish an action table or claim
reviewer acceptance from code changes alone.

## Safe-Fix Guidance
- Do not mark a comment resolved until the reviewer accepts it.
- If a Must-fix requires a design change beyond the current PR scope,
  escalate to the engineer — do not scope-creep silently.
- If feedback contradicts a previous reviewer's approval, flag the conflict
  and do not resolve it unilaterally.
