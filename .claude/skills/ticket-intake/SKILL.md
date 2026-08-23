# SKILL.md — ticket-intake

## Purpose
Scan the issue tracker for new requirement tickets, facilitate requirement
discussion, document conclusions and reference links back into the ticket,
and prepare the ticket for decomposition.

## Type
Command-like. Run at the start of a sprint or planning cycle, when notified
of a new ticket, or explicitly via `/ticket-intake`.

## When to use
- At the start of every sprint or planning session.
- When notified of a newly created ticket.
- Before running `task-decomposition` on any ticket.

## When not to use
Do not run this on tickets already in "Accepted" or later states — they have
already passed through intake.

## Steps

### Phase 1 — Scan for new tickets
1. Use the `CLAUDE_ISSUE_TRACKER`-routed MCP (`github`, `clickup`, or `jira`) to list
   all tickets in "New", "Open", or "Backlog" state with no current assignee.
2. Filter to tickets in the current sprint or milestone (if one is active).
3. Sort by: blocker urgency first, then milestone deadline, then creation date.
4. Select the highest-priority ticket to process. Process one at a time.

### Phase 2 — Requirement review
5. Read the full ticket description, attachments, and any existing comments.
6. Identify three things:
   - **Desired behavior**: what the system should do after this ticket is done.
   - **Current behavior**: what it does today (or "new feature" if additive).
   - **Definition of done**: what observable state proves the work is complete.
7. List all ambiguities, missing context, and unstated assumptions explicitly.
8. If ambiguities exist:
   - Post clarifying questions as a numbered comment on the ticket.
   - Tag the ticket with a "needs-clarification" label if available.
   - Do not proceed to Phase 3 until responses arrive.

### Phase 3 — Discussion and conclusion
9. When responses are received (or if the ticket is self-contained):
   a. Summarize the agreed requirements in plain language.
   b. Draft explicit acceptance criteria (observable, testable, unambiguous).
   c. Note any out-of-scope items explicitly — scope creep starts here.
   d. Note any risk areas, performance constraints, or security considerations.
10. Post the conclusions as a structured comment on the ticket using the
    output format below.
11. If the discussion happened in an external channel (Slack, document, meeting):
    include a reference link to that discussion in the conclusion comment.
12. Update the ticket description: append a `## Refined Requirements` section
    with the finalized acceptance criteria. Do not delete the original description.

### Phase 3b — Cross-repo scope detection
13. Before confirming readiness, check whether the ticket requires changes across
    more than one repository. Look for these signals:
    - The description mentions two or more repository names or service names.
    - Acceptance criteria span a backend and a frontend, or an API and a consumer.
    - Linked issues or dependencies are in different repositories.
    - Words like "shared library update", "API contract change", "monorepo boundary",
      or "simultaneous release" appear.
14. If any signal is present, add a `## Cross-repo scope` section to the conclusion
    comment:
    ```
    ## Cross-repo scope detected
    Repositories affected: [repo-a], [repo-b]
    Coordination required: yes — will use cross-repo-coordinator
    ```
    Tag the ticket with a `cross-repo` label if the tracker supports it.
    This routes the ticket to `cross-repo-coordinator` in the next phase.
15. If no cross-repo signal is found, note: "Single-repo scope confirmed."

### Phase 4 — Readiness confirmation
16. Confirm all of the following before marking the ticket accepted:
    - Acceptance criteria are explicit and testable.
    - Dependencies on other tickets are identified and their states checked.
    - No open blocking questions remain unanswered.
    - Scope is agreed and documented.
    - Cross-repo scope: detected and labelled, or confirmed single-repo.
17. Transition the ticket state to "Accepted".
18. Route based on the scope determined in Phase 3b:
    - Single-repo: run `task-decomposition`.
    - Cross-repo: run `cross-repo-coordinator`.

## Output format

```
## Ticket intake — [ticket reference] [title]

### Requirement summary
**Desired behavior:** [one paragraph]
**Current behavior:** [one paragraph or "new feature"]
**Definition of done:** [one or two sentences]

### Acceptance criteria
- [ ] [criterion 1 — observable, testable]
- [ ] [criterion 2]

### Out of scope
- [item explicitly excluded]

### Risk areas
- [performance / security / compatibility note]

### Dependencies
- Blocked by: [ticket ref] / none
- Blocks: [ticket ref] / none

### Discussion references
- [link to Slack thread / document / meeting notes]

### Cross-repo scope
- Detected: yes → repos: [repo-a], [repo-b] — routing to cross-repo-coordinator
- Detected: no → single-repo confirmed — routing to task-decomposition

### Status
- Questions posted: yes / no
- Conclusions posted: yes
- Ticket state: Accepted / Blocked (awaiting clarification on: [question #])
```

## Safe-Fix Guidance
- Do not proceed to `task-decomposition` while any blocking question is open.
- Do not infer acceptance criteria from the ticket title alone.
- If requirements are contradictory, stop and escalate to the engineer.
- If the ticket is a duplicate, close it and reference the original.
- Do not assume single-repo scope — always run the cross-repo detection check.
