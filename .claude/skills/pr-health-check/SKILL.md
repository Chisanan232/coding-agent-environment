---
name: pr-health-check
description: "Inspect all open PRs in the repository and produce a health report: which PRs are ready to merge, which are blocked, which are stale, and which are bot PRs requiring automated maintenance."
---

# SKILL.md — pr-health-check  [COMMAND-LIKE SKILL]

Read `engineering-runtime` first and initialize its host-aware environment before
using the shell examples below. Resolve installed helper paths from that skill;
never borrow another host’s private config, credentials or mutable state.

## Purpose
Inspect all open PRs in the repository and produce a health report: which PRs
are ready to merge, which are blocked, which are stale, and which are bot PRs
requiring automated maintenance.

## Type
Command-like. Run at each polling interval, or explicitly by naming `pr-health-check`.

## When to use
- At each scheduled polling interval (see time-layer design in the active repository instructions).
- When resuming to reassess repository state.
- Before beginning a new task (to catch PRs that need unblocking first).

## When not to use
Do not run this mid-implementation on a focused implementation task — it is a
coordination-level check, not a developer progress check.

## Steps

### 1. List open PRs
- Use GitHub MCP to list all open PRs on the repository.
- For each PR, collect: title, author, CI status, review status, merge conflict state,
  last activity timestamp, and labels.

### 2. Classify each PR
Classify each PR into one of:

| Class | Condition |
|---|---|
| `ready-to-merge` | All `engineering-workflow` merge gates met |
| `blocked-ci` | CI is red |
| `blocked-review` | Missing required approvals or unresolved review requests |
| `blocked-conflict` | Merge conflict present |
| `blocked-comments` | Unresolved blocking review comments |
| `bot-pr-clean` | Bot author, CI green, no conflict |
| `bot-pr-conflict` | Bot author, has lock-file conflict |
| `stale` | No activity for `$ENGINEERING_STALE_PR_DAYS` days after last review comment (default: 14, set in the active host runtime configuration) |
| `in-progress` | Active, not yet ready for review |

### 3. Act on each class

| Class | Action |
|---|---|
| `ready-to-merge` | Approve and merge when the shared workflow gates are met |
| `blocked-ci` | Note the failure — invoke `ci-failure-triage` if repair is in scope |
| `blocked-review` | Note awaiting reviewer — no action unless stale |
| `blocked-conflict` | Note conflict — flag to engineer |
| `blocked-comments` | Note unresolved comments — flag to engineer |
| `bot-pr-clean` | Invoke `bot-pr-maintainer` skill |
| `bot-pr-conflict` | Invoke `bot-pr-maintainer` skill (rebase path) |
| `stale` | Comment on PR noting staleness; close if beyond hard timeout |
| `in-progress` | No action |

### 4. Produce health report
Output the health report (see Output format).

## Output format

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
Lead with merge readiness or the decision needed. Include relevant PR/run
references, blocking check/review/conflict and required owner action. Aggregate
routine healthy PRs; use a comparison table only when it materially helps a
multi-PR decision. Omit empty classes and repeated unchanged polling updates.

## Safe-Fix Guidance
- Do not merge a PR that does not meet all `engineering-workflow` conditions, even if it
  looks ready at a glance.
- Do not close a stale PR without commenting first to give the author a chance to respond.
- Do not repair CI directly from this skill — delegate to `ci-failure-triage`.
