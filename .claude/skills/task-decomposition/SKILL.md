# SKILL.md — task-decomposition

## Purpose
Translate a high-level ticket or requirement into a concrete, ordered list of
implementation steps with clear dependency relationships, executed directly
unless a step genuinely needs an isolated sub-agent.

## Type
Auto-used. Run this skill when a new ticket or task arrives.

## Do Not Assume
- Do not assume the requirement is self-contained — check for cross-cutting concerns.
- Do not assume the current implementation is correct — read the relevant code first.
- Do not assume dependencies between tasks are obvious — make them explicit.
- Do not assume every step needs a sub-agent — most are executed directly;
  reserve sub-agents for the cases in the Delegation Model (CLAUDE.md).

## Steps

### Phase 1 — Requirement intake
1. Read the ticket description in full.
2. Identify the acceptance criteria. If none are stated, draft them and confirm
   with the engineer before continuing.
3. Identify the affected system areas (services, packages, files, APIs).
4. Identify external dependencies: APIs, data stores, infrastructure, third-party services.

### Phase 2 — Constraint mapping
5. Check the CLAUDE.md Architecture Constraints section for any rules that apply.
6. Identify any performance, security, or compatibility constraints not stated in the ticket.
7. Identify any configuration or environment changes required (new env vars, flags, etc.).
8. Note any migrations, schema changes, or breaking API changes.

### Phase 3 — Task breakdown
9. Break the work into the smallest logical steps that can each be implemented and
   tested independently.
10. Order the steps by dependency: no step should depend on a later step.
11. For each step, state:
    - What changes
    - What tests will be added
    - Whether it requires an isolated sub-agent (yes/no) and, if yes, why —
      stronger reasoning, isolated context, independent verification, or
      genuine parallelism (see Delegation Model in CLAUDE.md); otherwise it
      is executed directly
    - Whether it can be parallelized with any other step

### Phase 4 — Create sub-tickets and output
12. Produce the decomposition as a numbered task list.
13. Mark each task with its sub-agent requirement (yes/no + reason, or "direct").
14. Identify the critical path (the minimum sequence required to reach a shippable state).
15. **Create discrete child/sub-tickets in the issue tracker** for each parallelizable
    task unit (not just a comment — actual trackable tickets):
    a. For each task in the breakdown, create a child ticket:
       - Title: `[parent-ref] [short task description]`
       - Description: the full task detail from step 11.
       - Acceptance criteria: the subtask-specific criteria.
       - State: "Accepted" (ready for pickup immediately).
       - Link to parent ticket.
       - Label/tag: sub-agent requirement (e.g., "direct", "sub-agent: isolated-review").
    b. For tasks that can run in parallel, create all of them at once so
       multiple parallel sessions can pick them up independently.
    c. For tasks that have dependencies, set the "blocked by" field to the
       parent sub-ticket they depend on.
16. Post a summary comment on the parent ticket:
    - List all created sub-tickets with their references and states.
    - Show the dependency chain and which tasks can start immediately.
17. Transition the parent ticket state to "In Decomposition" or "In Progress".

## Output format

```
## Task decomposition — [ticket reference]

### Acceptance criteria
- [ ] [criterion 1]
- [ ] [criterion 2]

### Sub-tickets created
1. [sub-ticket ref] [direct] [task description] — depends on: none — state: Accepted
2. [sub-ticket ref] [direct] [task description] — depends on: 1 — state: Blocked
3. [sub-ticket ref] [sub-agent: acceptance-validation] [validate behavior X] — depends on: 2 — state: Blocked
4. [sub-ticket ref] [direct] [PR review and merge] — depends on: 3 — state: Blocked

### Critical path
[sub-1] → [sub-2] → [sub-3] → [sub-4]

### Can start immediately (no blockers)
- [sub-ticket ref]: [task description] — execute directly
- [sub-ticket ref]: [task description] — execute directly (parallel)
```

## Safe-Fix Guidance
- If the requirement is ambiguous, stop and ask. Do not decompose an ambiguous ticket.
- If the decomposition reveals a hidden dependency on an incomplete feature, surface it
  to the engineer before starting implementation.
