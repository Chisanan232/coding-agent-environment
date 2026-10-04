---
name: acceptance-validation
description: "Validate that a delivered implementation meets its acceptance criteria from an external tester perspective, covering happy paths, edge cases, and regressions."
---

# SKILL.md — acceptance-validation

Read `engineering-runtime` first and initialize its host-aware environment before
using the shell examples below. Resolve installed helper paths from that skill;
never borrow another host’s private config, credentials or mutable state.

## Purpose
Validate that a delivered implementation meets its acceptance criteria from an
external tester perspective, covering happy paths, edge cases, and regressions.

## Type
Auto-used. Run this skill before declaring implementation complete, to produce
a pre-merge validation report from an external tester perspective.

## Do Not Assume
- Do not assume the stated acceptance criteria are complete — look for implicit requirements.
- Do not assume the implementation is correct just because tests pass.
- Do not assume happy-path coverage means edge cases are handled.
- Do not assume regressions are impossible — check what could have been affected.

## Steps

### Phase 1 — Criteria extraction
1. Read the ticket or PR description to identify acceptance criteria.
2. If acceptance criteria are missing, derive them from the stated purpose and scope.
3. If criteria cannot be determined, stop and confirm with the engineer before proceeding.

### Phase 2 — Happy path validation
4. For each acceptance criterion, identify the primary scenario that should satisfy it.
5. Verify that scenario works as expected using the available implementation.
6. Record: criterion → scenario → result (pass / fail / not verifiable).

### Phase 3 — Adversarial and boundary validation
7. For each changed behavior, identify:
   - Boundary values (min, max, empty, zero, null, overflow)
   - Invalid inputs (wrong types, missing required fields, malformed data)
   - Error paths (what happens when dependencies fail)
   - Concurrent or race condition scenarios (if applicable)
8. Verify each adversarial scenario produces the correct outcome.

### Phase 3b — UI and E2E validation (when applicable)
9. If the ticket involves UI changes or end-to-end user flows, run browser validation:
   1. If `ENGINEERING_E2E_COMMAND` is set: `${ENGINEERING_E2E_COMMAND}`
   2. If Playwright MCP is enabled: navigate key user journeys via accessibility tree,
      capture page snapshots for pass evidence and screenshots for failures.
   3. If neither is configured: note the gap in the report; describe manual steps taken.
   Record result in the validation report.

### Phase 4 — Regression check
10. Identify the existing behaviors that could be affected by the change.
11. Run the existing test suite and check for failures.
12. If failures are found, report them — do not fix them directly.
13. Identify behavioral regressions that the test suite does not catch.

### Phase 5 — Validation report
14. Produce the summary-first validation report (see Output format).
15. If any blocking items are found, report them to the engineer with detail.
16. Do not declare the work ready if any criterion fails or any blocking regression exists.

## Output format

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Lead with the acceptance verdict and aggregate required assertions passed/failed,
with the run/test/PR reference. Keep the criterion-to-scenario record as supporting
evidence. Expand failures, material not-verified cases, regressions and evidence
needed for review; omit routine passing-case tables and inapplicable sections.
Describe the observed coverage boundary; do not infer absence of all regressions
from passing tests. Unverified blocking criteria prevent a ready verdict.

## Safe-Fix Guidance
- If a criterion fails, report it. Do not modify tests or code to make it pass.
- If the test suite is red, stop and escalate to the engineer — do not proceed
  with validation on a broken baseline.
