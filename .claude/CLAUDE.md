# CLAUDE.md — Global Personal Configuration

> This file is the global behavioral context for Claude Code across all projects.
> It contains durable engineering policy, workflow conventions, and automation rules
> that apply regardless of which repository is active.
> Claude Code must read and apply every section before taking any action.
>
> **Project-specific overrides** — repository identity, architecture constraints,
> commands, and tooling — belong in the project's own `.claude/CLAUDE.md`.
> See the **Global vs Project Configuration** section below.

---

## Global vs Project Configuration

This file applies to all projects. Repository identity, architecture constraints,
commands, tooling and merge gates belong in each project's `.claude/CLAUDE.md`; the
full split and the project section table are in the
[project CLAUDE.md reference](skills/project-setup/references/project-claude-md.md).

### Layering rule

Claude Code reads this global file first, then the project's `.claude/CLAUDE.md`.
Project values take precedence when both define the same concept.
These global behavioral rules apply unless the project file explicitly overrides them.

---

## Safe Implementation Policy

Claude Code must follow these rules on every implementation task, without exception.

### Creation and source of truth

- Establish need, search before creating, and reference before repeating.
  Make only changes needed for the authorized outcome.
- Use canonical `requirement-zero:requirement-zero` for NEW unvalidated scope
  and `requirement-zero:codebase-zero` for EXISTING artifact audits. Approved
  work is settled; these skills do not reopen its requirement decision.
- Code is primary truth. Prefer naming, types, structure and API boundaries.
  Comments/docstrings explain non-obvious WHY, domain constraints, safety or
  compatibility invariants, external limits, workarounds, API documentation
  contracts or justified suppressions; omit obvious mechanics.
- Docs preserve durable knowledge code cannot express well. Tickets track
  useful outcomes. Neither duplicates code, another ticket, a PR or a canonical doc.
- Reuse only when semantic responsibility matches; avoid speculative abstraction
  and mechanical DRY. Small duplication can be safer than false generality.
- Deletion/consolidation is a valid scoped improvement. Preserve protective
  controls and unknown state; canonical subtraction skills own detailed procedures.
- Persistent/shared output leads with semantic outcome and supporting evidence.
  State material inference and unknowns honestly; reference canonical detail.
  Use `evidence-first-briefing` before persistent/shared engineering output.

### Validation sequence

12. After each logical unit of change, run impacted tests first.
13. Before declaring work complete, run the full validation suite.
14. Do not mark a task done while any test or lint check is red.
15. If a check cannot be run locally, say so explicitly before committing.

### Safety

16. Never overwrite uncommitted changes without explicit user confirmation.
17. Delete only within authorized scope after checking dependents and risk.
    Destructive operations outside scope require explicit confirmation.
18. Never force-push without explicit user confirmation.
19. Never skip pre-commit hooks (`--no-verify`) without explicit user confirmation.
20. If you discover unexpected repository state (unfamiliar files, branches, config),
    investigate before acting. Do not delete or overwrite unknown state.

---

## Testing Expectations

New features need tests and bug fixes need a regression test. Tests are
deterministic, isolated, fast, and assert behavior rather than private internals.
Do not disable failing tests; fix them or escalate. Run impacted tests while
iterating and the full suite before committing. Details are in
[quality-policies](skills/feature-implementation/references/quality-policies.md).

---

## Type Checking Policy

Annotate all public APIs, signatures and class attributes; justify any "any" escape
hatch and use only scoped type suppressions. Run the configured type checker before
every commit. Details are in
[quality-policies](skills/feature-implementation/references/quality-policies.md).

---

## Linting and Formatting Policy

Fix all lint errors before committing and explain any suppression with a comment;
pre-commit hooks enforce lint and format. Details are in
[quality-policies](skills/feature-implementation/references/quality-policies.md).

---

## CI/CD Triage Expectations

On CI failure: identify the failure type, reproduce locally before proposing a fix
(Reproduce locally; say so if it cannot be reproduced), fix the root cause, verify,
then push a focused commit.

- Do not merge on red CI.
- Do not add suppression annotations to silence failures.
- Do not delete failing tests.
- Do not bypass pre-commit with `--no-verify`.
- Flaky tests indicate real problems. Do not mark them as skip. Investigate the cause.

The full sequence is in
[claude-ci-triage](skills/ci-failure-triage/references/claude-ci-triage.md).

---

## MCP-Backed Systems

Check `.mcp.json` for configured MCP tools. When an MCP-backed capability is
available, prefer it over manual approximation; otherwise proceed and note the gap.
`fetch` is active by default; other servers are opt-in per project.
GitHub operations use `gh`; the `github` MCP is retired (`engineering-workflow`, GitHub tool selection).
The capability table is in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Time-Layer Design — Skill-First Polling and Scheduling

Prefer waking narrow skills (`pr-health-check`, `bot-pr-maintainer`, `release-watch`),
not full agents, for recurring polling. A skill woken by a loop must not trigger
another loop or enter a self-repair cycle; it reports failure and exits. Targets,
default intervals and scheduling are in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Skill Invocation Guide

Invoke skills by their actual triggers; the skill-to-trigger table and the
language-specific repair skill guidance are in [skills/README.md](skills/README.md).
Load a language repair skill only when its tool fails.
Frontend work needs the [acceptance-validation](skills/acceptance-validation/SKILL.md)
frontend QA evidence, and [post-merge-close](skills/post-merge-close/SKILL.md) owns
closeout, the disk-free check and cleanup status. [decision-delegation](skills/decision-delegation/SKILL.md)
owns the `READY_FOR_RELEASE` completion token and decision levels.

---

<!-- coding-agent-environment:engineering-workflow:start -->
## Workflow Path

Every repository code change, however small, goes through a pull request that
follows [engineering-workflow](skills/engineering-workflow/SKILL.md), which owns
the branch, commit and pull-request rules. Never push directly to the default or
protected branch.

The ticket-driven workflow (`ticket-intake → dev-impl-loop → QA handoff → PR`) is
the contract for substantive work: new features, significant refactors, bug fixes
with regression risk, changes touching multiple files or modules, and work that
requires QA sign-off. Follow `dev-impl-loop` (5 phases): implement → relative
tests → full suite → pre-commit → QA handoff → PR. See the Skill Invocation Guide.

For a small change, keep the same PR and commit rules and scale only the ceremony:
no QA handoff, circuit breaker, or workflow state file is needed unless the change
carries regression risk.

---

## Engineering Workflow Contract

Use [engineering-workflow](skills/engineering-workflow/SKILL.md) for the shared
branch/worktree, commit, validation, review, pull-request, merge, ticket
reconciliation, and cleanup lifecycle. `code-review-prep` and `pr-readiness`
adapt that contract to Claude Code; they do not redefine it.

Project-level `CLAUDE.md` files provide repository commands and product
constraints. A later explicit user instruction can authorize a different choice
for that work.

---

## Push Gate and Worktree Adapter

Apply [engineering-workflow](skills/engineering-workflow/SKILL.md) before pushing.
Run the repository's configured full relevant checks and the Claude-specific
`full-test-gate.sh` and `precommit-gate.sh` when present. Fix failures; do not use
`--no-verify` or bypass a required check.

Claude workflow skills resolve the active worktree from
`$CLAUDE_CURRENT_WORKTREE`, then `.claude/.current-worktree`. They resolve the
release or phase from `$CLAUDE_CURRENT_RELEASE`, then
`.claude/.current-release`, then the authoritative ticket milestone or sprint.
The shared skill owns the branch format and cleanup boundary.

---

## Delegation Model

Delegation is capability-based, not role-based; the main agent does routine work
directly. Spawn a subagent only for stronger reasoning, isolated context, independent
verification or genuine parallelism (conditions in
[model-routing-and-delegation](skills/architecture-design/references/model-routing-and-delegation.md)).
Subagent concurrency limits and scope ownership are owned by the `engineering-workflow`
Delegation section.

---

## Merge Policy

Apply the required checks, CODEOWNERS-required approvals, conflict,
branch-currency, and review-thread gates from
[engineering-workflow](skills/engineering-workflow/SKILL.md), which owns the PR
classes: owner-authored PRs need no formal GitHub approval; non-owner and bot PRs
need a genuine owner `APPROVED` review on the current head. Use **Create a
merge commit**; do not squash or rebase-merge unless a later explicit user
instruction authorizes that strategy.

The shared contract is the sole canonical authority for the two narrow
administrator-merge exceptions: qualified `CI_UNAVAILABLE_EXTERNAL` and verified
owner-only same-identity review deadlock (owner-admin merge needs no separate human
Approve). Apply every evidence, identity, review,
check and merge-strategy condition there; never bypass a genuine failing gate.


This managed routing is authoritative for the shared engineering lifecycle;
read existing merge-specific wording through the canonical contract above.

## Resource Admission and GitHub Operations

Before heavy work run `scripts/memory-admission.py check --weight heavy` from the coding-agent-environment checkout (it also asks the
SPE-109 disk gate; it is not installed into `~/.claude`); one heavy
lane at a time, never kill or delete another session's work, and treat a swap-used number as history, not current
exhaustion. After a reboot treat earlier processes, locks, reservations and jobs as stale and reconcile before resuming.
Use `gh` for GitHub; it never bypasses host authorization, PR classes, CI, reviews or the merge gate. Merge an eligible PR only with
the `merge_command` that `scripts/owner-merge-gate.py check --settings` prints (the validating `owner-merge-runner` when offered, else the
pinned `gh pr merge`, which asks); never choose another merge spelling or interface to avoid a prompt or a refusal. The merge is its own tool call: never combine it with a review comment, CI polling, a readback (`gh pr view`, `gh api`), a pipe, `head`/`tail`, `&&`, `;` or any other chaining. The order is: independent review, check the exact PR head, verify required CI, run `owner-merge-gate.py check`, run the printed `merge_command` alone, then verify the merged state in a separate call. A compound shell call matches no merge rule, so the host asks about the whole thing; that is a workflow error, not a runner failure. Details:
[engineering-workflow](skills/engineering-workflow/SKILL.md).

## Decision Delegation

Decision delegation: act as senior engineer and release owner; full contract in
`decision-delegation`. Levels govern product-decision escalation only and never
narrow an explicit-confirmation, owner-decision, production, destructive,
publication, IAM, payment, credential or CI-definition gate. Test Level 4 overlay,
then 3, 2, 1, else 0. Level 0 routine engineering (implementation, refactor,
tests, CI debugging, safe dependency bumps, bug fixes, reviews, merges passing
every engineering-workflow gate, task-owned cleanup) and Level 1 architecture that
leaves product guarantees, trust boundaries, data/privacy, authorization,
public-compatibility and approved limits unchanged (review, record an ADR) never
go to the founder. Level 2 founder decision at least for material product,
budget/authority, public-compatibility, retention/privacy, credential-authority,
fail-open/closed, new-spend or irreversible-data change, or any Level 1 invariant
change: ONE packet. Level 3: cite the durable record of an already-authorized
decision; never infer founder consent from any agent-produced record or the author
field alone. Level 4: a host ASK/DENY/managed/classifier refusal is reported (exact
operation, secrets redacted) and never bypassed or relabeled a product decision;
approving a host prompt is not founder consent. Main coordinator continues the
release DAG after each slice; a merge that deploys or publishes needs its own
authorization; no fake human approval; preserve active or ambiguously owned
worktrees and sessions (ownership needs a checkpoint record, not a name match).
Jira: read with read-only tools, never mutate to read, never write placeholder
bodies, never edit comments this session did not create, read back every mutation
(FORNX-439 comment 27362 overwrite).
<!-- coding-agent-environment:engineering-workflow:end -->

---

## Bot PR Policy

Bot PRs that are CI-green, conflict-free and limited to the automated update are
merged once the owner has genuinely approved the current head (the agent never submits
that approval). Lock-file conflicts are resolved by asking the bot to rebase,
never by hand. If the update itself causes a CI failure, escalate and do not merge.
`bot-pr-maintainer` and `pr-health-check` run this loop; the full policy is in
[bot-pr-policy](skills/bot-pr-maintainer/references/bot-pr-policy.md).

---

## Release Operations Policy

The external release workflow (automated tag creation, version bumping, changelog
generation) handles the mechanics of releasing. Claude Code's role is **observational
and preparatory**, not operational.

During a release window Claude Code observes and prepares (identify changes, draft
notes, update release intent config, watch the pipeline, summarize) via the
`release-watch` and `release-preparation` skills; see
[release-operations](skills/release-watch/references/release-operations.md).

### What Claude Code must not do during release

- Do not manually create git tags unless the automated workflow has definitively failed
  and the engineer has explicitly requested manual intervention.
- Do not push directly to `main` / `master` during a release window.
- Do not modify CI/CD pipeline definitions during active release.
- Do not publish packages to registries — this is the automated workflow's job.

---

## Environment Variable Reference

All hooks and utility scripts source `~/.claude/config.env` at startup (copy
`coding-agent-environment/.claude/hooks/config.env` there). The full variable
table lives in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Model Routing

Main model: Sonnet, effort medium, permission mode Auto. Use it for routine work. For
non-trivial architecture/design/planning, delegate first to the `opus-architect`
subagent, then continue execution without waiting for approval; skip it for trivial
work and do not enter Plan Mode just to get Opus planning. Use `advisorModel`
selectively. Details are in
[model-routing-and-delegation](skills/architecture-design/references/model-routing-and-delegation.md).

---

## Workflow State Management

Skills persist per-ticket progress with `workflow-state.sh` so `/workflow-resume <ticket>`
can continue an interrupted session; git history stays the record of what changed.
Active-ticket resolution, state fields, observability layers and the per-skill
tracking table are in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Circuit Breaker Policy

A circuit breaker stops runaway repair loops: when it opens, stop repairing, write
state `circuit_open`, report to the engineer, and do not retry until they reset it.
Thresholds and commands are in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Session Memory

`session-memory.sh` persists per-ticket notes across interrupted sessions; it does not
replace workflow state, the decision log or git history. Usage and storage are in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Cross-Repo Work

Use the `cross-repo-coordinator` skill, not single-repo `task-decomposition`, when a
change spans repositories. Merges are gated on per-repo QA and integration tests.
Triggers, the gate and the parent-ticket note convention are in
[cross-repo-work](skills/cross-repo-coordinator/references/cross-repo-work.md).

---

## What Claude Code Must Never Do Without Explicit Confirmation

- Delete outside the authorized scope or remove protective controls without owner approval
- Force-push to any branch
- Run `git reset --hard`
- Run `git clean -fd`
- Drop or truncate database tables
- Pipe remote content directly to a shell (`curl | bash`)
- Publish packages to registries
- Modify CI/CD pipeline definitions without review
- Commit `.env` or any file containing credentials

@RTK.md

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
