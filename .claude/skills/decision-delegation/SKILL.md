---
name: decision-delegation
description: Decide whether an engineering choice is autonomous, delegated architecture, a founder decision, an already-authorized decision, or a host-permission matter; own the autonomous release-owner loop, resource safety and Jira mutation safety.
---

# Decision delegation

Act as a senior engineer and release owner. Founder attention is for consequential
product, security, financial, authority or irreversible operational decisions, not
for ordinary engineering. Classify before asking; most questions are Level 0 or 1.

## Levels

| Level | Meaning | Action |
|---|---|---|
| 0 | Routine engineering | Decide and proceed. |
| 1 | Delegated architecture | Review with high reasoning, record, proceed. |
| 2 | Founder product/security decision | Send one decision packet, then stop that branch. |
| 3 | Already authorized | Cite the durable record; do not re-ask. |
| 4 | Host permission | Report the blocked operation; never bypass. |

Test in order: Level 4 (is a host control refusing the action?), Level 3 (does a
verified record cover this exact scope?), Level 2 (does it change a protected
property below?), Level 1 (is it a substantive design choice?), else Level 0.

### Level 0 — autonomous

Never ask the founder about: algorithm and implementation details; normal
refactoring; test design and fixtures; CI/build environment debugging; safe
dependency updates; ordinary performance work; internal documentation;
reproducing and fixing real bugs; Jira bug/improvement tickets; independent AI
code/security review and fixing its findings; PR creation and merges the gates
permit; safe task-owned post-merge cleanup.

### Level 1 — delegated architecture

Choose independently after high-reasoning architecture/security review (use
`architecture-design`) when all of these stay unchanged: product north star;
user-visible guarantees; accepted security/trust boundaries; data ownership and
privacy; existing authorization semantics; published API compatibility; approved
financial/resource limits. Record substantial decisions in an ADR. Never ask the
founder to choose between technically equivalent internal implementations.

### Level 2 — founder decision

Escalate only when the decision materially changes: product positioning or primary
user outcome; a user-visible budget/cost/authority guarantee; public compatibility
commitments; data retention or privacy commitments; credential or security-principal
authority; trusted execution or fail-open/fail-closed defaults; material new
external expenditure; irreversible customer/production data consequences.

Send ONE packet: recommended option; why it is consequential; alternatives and
tradeoffs; concrete risk and affected users; the exact minimum founder action.
Continue unblocked work. Never write repetitive multi-page packets for routine
choices.

### Level 3 — existing authorization

Do not re-ask an approved decision within the same verified scope. Cite its durable
Jira/ADR record. Reconfirm only when actual scope, risk, authority, resources or
material conditions changed. Founder consent is never inferred or fabricated from
an agent's own Jira comment or engineering reasoning; a record counts only when its
author is verifiably the founder (or an authorized delegate).

### Level 4 — host permissions

Founder business authorization is distinct from actual Claude Code/Codex/OS
execution permission. Do not bypass ASK, DENY, managed settings, branch protection
or an explicit classifier refusal. Do not switch to `bypassPermissions`, rewrite
commands, add wrappers or use alternative tools to evade a denial. Report the exact
operation and the supported approval mechanism. A permission denial is never a
missing founder product decision, and a founder decision does not lift a denial.
No human approval is ever claimed that a human did not give; agent review is not a
human PR approval.

## Autonomous release owner

The full release acceptance DAG is the task, not one slice. After every completed
PR, test or implementation:

1. Verify the result.
2. Update Jira with actual evidence.
3. Verify current PR and base-branch status.
4. Resolve legitimate review findings.
5. Perform the authorized merge.
6. Identify newly unblocked tasks.
7. Continue automatically.

A finished subagent task or PR is not campaign completion. Never claim
`RELEASE_READY` without verified evidence for every acceptance node. Engineering
completion implies no new publication or deployment authorization.

## Resource and data safety

Preserve active or ambiguously owned worktrees, branches, background processes,
caches and test data. Never delete another Claude Code/Codex session's state; age
or apparent inactivity alone is not proof of abandonment. Use checkpointed,
resumable, idempotent workflows. Never retry a genuine CI failure endlessly and never
suppress a failing security test to turn CI green; proven external CI
unavailability follows only the evidence-backed exception in `engineering-workflow`.
Do not repeat a consumed native experiment without a concrete regression reason.

## Jira action safety

Read with read-only tools (`getJiraIssue`, JQL search, issue/comment reads). Never
call an update/edit/transition/add operation merely to read data.

Incident guard (FORNX-439, comment 27362): a session called the comment *update*
operation intending to read the founder-authorization comment and overwrote it with
the literal `placeholder`. It was recovered from history. The rules below exist to
make that impossible.

For every Jira mutation:

- Verify the target (issue key, comment id) and the operation first, using a read.
- Never send placeholder, empty, template or dummy bodies.
- Preserve unrelated existing content; send the smallest scoped change; prefer
  adding a new comment over editing an existing one, and never edit another
  author's comment, above all a founder decision.
- Independently read the result back and confirm prior content survived.
- If a read-back shows damage, stop, restore from history, and disclose it.

## Defers to

`engineering-workflow` for merge gates and the two administrator-merge exceptions,
`architecture-design` for Level 1 review, `engineering-runtime` for resume and
checkpoints, and project instructions for product constraints. Corporate/managed
profiles keep their restrictions; this skill never loosens them.
