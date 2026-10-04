# SKILL.md — cross-repo-coordinator

## Purpose
Coordinate work that spans multiple repositories under a single parent ticket.
Tracks per-repo sub-tickets, monitors PRs across repos, and verifies integration
once all per-repo work is merged. Prevents partial cross-repo merges.

## Type
Auto-used. Run when a parent ticket is decomposed into work items that span
more than one repository.

## Do Not Assume
- Do not assume all repos are owned by the same GitHub org — verify remotes.
- Do not assume per-repo work is independent — check integration dependencies.
- Do not assume a PR merged in one repo means the feature is complete.
- Do not close the parent ticket until all per-repo sub-tickets are closed.

## Concepts

### Ticket hierarchy
```
[Parent ticket]  ← the feature or bug that requires cross-repo work
  ├── [Repo A sub-ticket]  ← work in repository A
  ├── [Repo B sub-ticket]  ← work in repository B
  └── [Repo C sub-ticket]  ← work in repository C (if applicable)
```

Parent ticket: holds the integration acceptance criteria.
Sub-ticket: holds an independently useful outcome and its acceptance criteria.
Where no child is warranted, the parent owns repo-specific steps and readiness;
apply the same integration/merge gates to those steps.

### Session notes scope
Cross-repo state is stored in session notes under the **parent ticket** ref:
```bash
bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
  "Cross-repo state" \
  "Repo A: [status] | Repo B: [status] | Integration: [status]"
```

Use the parent ticket ref as the coordination anchor across sessions.

---

## Steps

### Phase 1 — Decompose into per-repo sub-tickets
1. Read the parent ticket's acceptance criteria via the issue tracker MCP.
2. Identify the list of repositories affected. Confirm with the engineer:
   ```
   Repos identified: [repo-a], [repo-b], ...
   Confirm this list before creating sub-tickets.
   ```
3. Apply the separate tracking gate in `task-decomposition`; reuse existing
   issues and keep steps in the parent when no independent tracking value exists.
   For each repository that needs an independently tracked outcome:
   a. Create a sub-ticket linked to the parent (use supported tracker relations).
   b. Title: `[Parent title] — [Repo name]`
   c. Body: copy relevant per-repo acceptance criteria from the parent.
   d. Link: add a reference back to the parent ticket.
4. Record the sub-ticket map in session notes:
   ```bash
   bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
     "Sub-ticket map" \
     "Repo A → [sub-ticket-A] | Repo B → [sub-ticket-B]"
   ```
5. Record decision:
   ```bash
   bash ~/.claude/hooks/decision-log.sh record \
     --ticket "[parent-ticket]" --agent "main-agent" --skill "cross-repo-coordinator" \
     --phase "1" --decision "decomposed" \
     --reason "Parent ticket requires work in [N] repos" \
     --context "[sub-ticket list]"
   ```

### Phase 2 — Assign and track per-repo work
6. Assign each sub-ticket to a per-repo working session (own session, or a
   developer, as appropriate).
7. Each repo's work follows the standard `ticket-pickup-check → dev-impl-loop` flow.
   The sub-ticket is the active ticket for that repo session.
8. At each polling interval (or on request), check per-repo progress:
   ```bash
   # For each sub-ticket, check state via issue tracker MCP
   # Expected states: In Progress → Ready for QA → Done
   ```
9. Surface a consolidated status table:
   ```
   | Repository | Sub-ticket | State | PR |
   |---|---|---|---|
   | [repo-a] | [sub-A] | In Progress | — |
   | [repo-b] | [sub-B] | Ready for QA | #42 |
   ```
10. Update session notes with current state:
    ```bash
    bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
      "Progress snapshot ([timestamp])" \
      "Repo A: In Progress | Repo B: PR #42 open"
    ```

### Phase 3 — PR monitoring across repos
11. For each repo with an open PR, verify at each polling cycle:
    - CI is green on the PR branch.
    - No unresolved review comments.
    - Branch is not behind base.
12. Do not merge any per-repo PR until all repos have passed QA:
    ```
    Merge gate: hold all per-repo PRs until every sub-ticket is "Ready for QA".
    ```
    This prevents partial integration (e.g., API change merged in Repo A but
    consumer not yet updated in Repo B).
13. When all sub-tickets are "Ready for QA", signal integration verification (Phase 4).
14. Record decision:
    ```bash
    bash ~/.claude/hooks/decision-log.sh record \
      --ticket "[parent-ticket]" --agent "main-agent" --skill "cross-repo-coordinator" \
      --phase "3" --decision "all-ready" \
      --reason "All per-repo sub-tickets passed QA — proceeding to integration check" \
      --context "[sub-ticket list and PR numbers]"
    ```

### Phase 4 — Integration verification
15. Run the integration verification command (if configured):
    ```bash
    ${CLAUDE_INTEGRATION_TEST_COMMAND:-echo "No integration test command configured. Set CLAUDE_INTEGRATION_TEST_COMMAND in ~/.claude/config.env"}
    ```
    If not configured: ask the engineer to specify the integration check before proceeding.
16. If integration tests pass: proceed to Phase 5.
17. If integration tests fail:
    a. Identify which repo's change causes the failure.
    b. Route the fix to the appropriate sub-ticket and repo session.
    c. Record failure and update session notes:
       ```bash
       bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
         "Integration failure ([timestamp])" \
         "[Failure summary — which repo, what failed]"
       ```
    d. Re-run integration check after fix. Do not merge until clean.

### Phase 5 — Coordinated merge
18. Merge per-repo PRs in dependency order (consumer repos last):
    - If no dependency order exists, merge simultaneously.
    - Confirm each merge before proceeding to the next.
    - Do not force-merge — each PR must satisfy the auto-merge policy independently.
19. After all PRs are merged, invoke `post-merge-close` for each sub-ticket.
20. Transition the parent ticket to "Done" / "Closed".
21. Post a completion summary on the parent ticket:
    ```
    ## Cross-repo work complete

    | Repository | PR | Merged |
    |---|---|---|
    | [repo-a] | [PR URL] | ✅ |
    | [repo-b] | [PR URL] | ✅ |

    Integration tests: ✅ passed
    All sub-tickets closed.
    ```
22. Clear session notes for the parent ticket:
    ```bash
    bash ~/.claude/hooks/session-memory.sh clear "[parent-ticket]"
    ```
23. Record final decision:
    ```bash
    bash ~/.claude/hooks/decision-log.sh record \
      --ticket "[parent-ticket]" --agent "main-agent" --skill "cross-repo-coordinator" \
      --phase "5" --decision "complete" \
      --reason "All per-repo PRs merged; integration tests passed; parent ticket closed"
    ```

## Output

```
## Cross-repo coordination — [parent-ticket]

| Repository | Sub-ticket | PR | State |
|---|---|---|---|
| [repo-a] | [sub-A] | #[N] | ✅ merged |
| [repo-b] | [sub-B] | #[N] | ✅ merged |

Integration check: ✅ passed
Parent ticket: ✅ closed
```

## Resuming an interrupted cross-repo-coordinator session

If `cross-repo-coordinator` is interrupted (context limit, crash, manual stop),
resume using session notes — they are the coordinator-level state store:

```bash
bash ~/.claude/hooks/session-memory.sh read "[parent-ticket]"
```

From the notes, determine which phase was active:

| Last note section | Resume action |
|---|---|
| "Sub-ticket map" only | Re-enter Phase 2 — re-read sub-ticket states |
| "Progress snapshot" | Re-enter Phase 2 or 3 — check current PR states vs snapshot |
| "Integration failure" | Re-enter Phase 4 — re-run integration check after fix |
| No notes / empty | Re-enter Phase 1 — check if sub-tickets already exist before recreating |

**Do not recreate sub-tickets** if the session notes show a sub-ticket map already
recorded. Look up the existing sub-tickets by their recorded refs instead.

After determining the resume point, write a new snapshot note before proceeding:
```bash
bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
  "Session resumed ([timestamp])" \
  "Resuming at Phase [N]. Prior state: [summary from last note]."
```

## Safe-Fix Guidance
- Never merge a per-repo PR before all sub-tickets have cleared QA.
  Partial cross-repo merges create integration debt that is hard to reverse.
- If one repo's sub-ticket is blocked, hold all other merges until resolved.
  Merging one side of an API change while the other is blocked produces broken state.
- If integration tests cannot be run (no `CLAUDE_INTEGRATION_TEST_COMMAND`),
  ask the engineer to confirm integration manually before merging.
- Do not close the parent ticket while any sub-ticket remains open.
- Do not recreate sub-tickets on resume — check session notes for existing refs first.
