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

This file lives at `~/.claude/CLAUDE.md` and applies to **all projects** you work
on with Claude Code. It contains durable behavioral rules, workflow conventions,
and agent coordination policies that do not change from project to project.

### What belongs in this global file

- Safe implementation policy
- Commit and PR conventions
- CI triage, push gate, and auto-merge rules
- Workflow state, circuit breaker, and agent delegation model
- MCP capability routing
- Skill invocation conventions and agent coordination

### What belongs in each project's `.claude/CLAUDE.md`

Create a `.claude/CLAUDE.md` at the repository root and fill in these sections:

| Section | Content |
|---|---|
| Repository Identity | Repo name, owner, primary language, runtime, dependency policy |
| Architecture Constraints | Directory conventions, layer boundaries, design rules for this repo |
| Package and Build Commands | Exact install, test, lint, build commands |
| Testing Tooling | Test runner, coverage tool, fixture strategy, mocking library |
| Type Checker | Which type checker, config file, project-specific suppression rules |
| Linting Tooling | Linter name, formatter, pre-commit config location |
| Source-of-Truth Systems | Issue tracker URL, wiki/docs space, Slack channel |
| Merge Gates | Additional repository-specific approvals or protected-branch checks |
| Polling Intervals | PR health check and release watch cadence for this repo |
| Language-Specific Repair Skills | Which `<language>-*` repair skills apply (e.g., `python-ruff-fixing`) |

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

## Testing Expectations

New features need tests and bug fixes need a regression test. Tests are
deterministic, isolated, fast, and assert behavior rather than private internals.
Do not disable failing tests; fix them or escalate. Run impacted tests while
iterating and the full suite before committing. Details are in
[quality-policies](skills/feature-implementation/references/quality-policies.md).

---

## Type Checking Policy

- All public APIs must have complete type annotations.
- All function signatures must be annotated (parameters + return type).
- All class attributes must be annotated at the class level.
- Do not use the language's "any" escape hatch without a code comment explaining why.
- Suppress type errors only with the language-specific scoped suppression mechanism,
  never suppress an entire file or block without a documented reason.
- Type annotations must never change runtime behavior.
- Run the configured type checker before every commit.

> **Project-specific**: which type checker (mypy, pyright, tsc, etc.), config file
> location, and language-specific annotation syntax belong in the project's `.claude/CLAUDE.md`.

---

## Linting and Formatting Policy

- All lint errors must be fixed before committing.
- Do not suppress lint warnings without a comment explaining the exception.
- Pre-commit hooks enforce both linting and formatting.
- Config lives in the project's standard config file (see project-level CLAUDE.md).

> **Project-specific**: linter name, formatter, and config file location belong in the
> project's `.claude/CLAUDE.md`.

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

## CI/CD Triage Expectations

When CI fails, Claude Code must follow this sequence:

1. **Identify** the failure type: test failure, lint error, type error, build error,
   coverage drop, security alert, infrastructure issue.
2. **Reproduce locally** before proposing a fix. Do not guess from CI logs alone.
3. **Analyze** the root cause. Do not apply surface-level fixes.
4. **Fix** the underlying problem. Do not bypass or suppress CI checks.
5. **Verify** the fix resolves the failure locally.
6. **Commit** the fix as a focused, isolated commit.
7. **Push** and confirm CI passes.

### What not to do

- Do not merge on red CI.
- Do not add suppression annotations to silence failures.
- Do not delete failing tests.
- Do not bypass pre-commit with `--no-verify`.
- If a failure cannot be reproduced locally, say so before proposing a fix.

### Flaky tests

Flaky tests indicate real problems. Do not mark them as skip. Investigate
the root cause: race conditions, shared state, external dependencies, timing assumptions.

---

## MCP-Backed Systems

Check `.mcp.json` for configured MCP tools. When an MCP-backed capability is
available, prefer it over manual approximation; otherwise proceed and note the gap.
`fetch` and `github` are active by default; other servers are opt-in per project.
The capability table is in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Time-Layer Design — Skill-First Polling and Scheduling

Prefer waking narrow skills (`pr-health-check`, `bot-pr-maintainer`, `release-watch`),
not full agents, for recurring polling. A skill woken by a loop must not trigger
another loop or enter a self-repair cycle; it reports failure and exits. Targets,
default intervals and scheduling are in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Skill Invocation Guide

The following skills are available. Invoke them by their slash command or by
asking Claude Code to run the named procedure.

| Skill | Type | When to use |
|---|---|---|
| `ticket-intake` | Auto | When a new ticket arrives — before decomposition |
| `task-decomposition` | Auto | After ticket-intake marks a ticket Accepted |
| `ticket-pickup-check` | Auto | Before starting any implementation task |
| `dev-impl-loop` | Auto | Drives the full implement→test→QA→PR cycle |
| `feature-implementation` | Auto | Within dev-impl-loop: when implementing a feature |
| `test-design` | Auto | When designing tests for new or changed code |
| `code-review-prep` | Auto | Before opening a PR |
| `ci-failure-triage` | Auto | When CI is red |
| `acceptance-validation` | Auto | Before declaring implementation complete |
| `bot-pr-maintainer` | Auto | When a bot PR is classified as clean or conflicted |
| `pr-feedback-response` | Auto | When a PR has new review comments or Request Changes |
| `post-merge-close` | Auto | After a PR is merged — close ticket, delete branch |
| `cross-repo-coordinator` | Auto | Coordinate multi-repo ticket: sub-tickets, PR monitoring, integration gate, coordinated merge |
| `/project-setup` | Command | Onboard a new repo — scaffold `.claude/CLAUDE.md`, gitignore, verify hooks |
| `/workflow-resume` | Command | Resume an interrupted agent session for a ticket |
| `/pr-readiness` | Command | Before opening a PR (full checklist run) |
| `/pr-health-check` | Command | At each polling interval to assess all open PRs |
| `/release-readiness` | Command | Before tagging a release |
| `/release-preparation` | Command | When a release window opens |
| `/dependency-upgrade-review` | Command | Before merging a dependency bump PR |

### Language-specific repair skills

Load the matching skill only when its tool fails. The installed inventory and
extension guidance live in [skills/README.md](skills/README.md).

---

<!-- coding-agent-environment:engineering-workflow:start -->
## Merge Policy

Apply the required checks, code-owner approvals, conflict, branch-currency, and
review-thread gates from
[engineering-workflow](skills/engineering-workflow/SKILL.md). Use **Create a
merge commit**; do not squash or rebase-merge unless a later explicit user
instruction authorizes that strategy.

The shared contract is the sole canonical authority for the two narrow
administrator-merge exceptions: qualified `CI_UNAVAILABLE_EXTERNAL` and verified
owner-only same-identity review deadlock. Apply every evidence, identity, review,
check and merge-strategy condition there; never bypass a genuine failing gate.


This managed routing is authoritative for the shared engineering lifecycle;
read existing merge-specific wording through the canonical contract above.

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
approved and merged. Lock-file conflicts are resolved by asking the bot to rebase,
never by hand. If the update itself causes a CI failure, escalate and do not merge.
`bot-pr-maintainer` and `pr-health-check` run this loop; the full policy is in
[bot-pr-policy](skills/bot-pr-maintainer/references/bot-pr-policy.md).

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

## Release Operations Policy

The external release workflow (automated tag creation, version bumping, changelog
generation) handles the mechanics of releasing. Claude Code's role is **observational
and preparatory**, not operational.

### What Claude Code does during a release window

1. **Identify changes** — inspect the commits in the release window (since the last tag).
2. **Prepare release notes** — draft human-readable changelog content.
3. **Update release intent config** — update version references or release config files
   if the project uses them (e.g., `pyproject.toml`, `package.json`, `CHANGELOG.md`).
4. **Observe release workflow state** — monitor the automated release CI pipeline.
5. **Summarize outcome** — report success or failure with links to the release artifact.

### What Claude Code must not do during release

- Do not manually create git tags unless the automated workflow has definitively failed
  and the engineer has explicitly requested manual intervention.
- Do not push directly to `main` / `master` during a release window.
- Do not modify CI/CD pipeline definitions during active release.
- Do not publish packages to registries — this is the automated workflow's job.

### Release coordination

Release observation is handled directly by the main agent via the
`release-watch`/`release-preparation` skills — thin by design, monitoring
and summarizing the automated workflow rather than replacing it.

---

## Environment Variable Reference

All hooks and utility scripts source `~/.claude/config.env` at startup (copy
`coding-agent-environment/.claude/hooks/config.env` there). The full variable
table lives in the [claude-runtime reference](skills/engineering-runtime/references/claude-runtime.md).

---

## Model Routing

- Main model: Sonnet, effort medium, permission mode Auto. This is the default
  for routine implementation, testing, fixing, tool usage, CI work, and
  orchestration — do not switch off it for ordinary work.
- Non-trivial architecture/design/planning (meaningful architecture, significant
  multi-module changes, migrations, security-sensitive design, important
  trade-offs, Jira/task decomposition, materially ambiguous requirements):
  delegate first to the `opus-architect` subagent (`.claude/agents/opus-architect.md`,
  Opus, high effort). It inspects, designs, and returns a distilled plan.
  Sonnet then continues execution automatically — do not stop for approval
  merely because planning finished.
- Skip `opus-architect` for trivial work: typos, renames, formatting, obvious
  small fixes, simple tests, dependency bumps, isolated straightforward edits.
- The `advisorModel` (Opus) is available for important decisions, repeated
  failures, security/architecture uncertainty, and final review of
  sufficiently complex work. Use it selectively, not ritualistically on every
  task.
- Do not enter interactive Plan Mode solely to obtain Opus planning — that's
  what `opus-architect` is for. Only stop and ask the user when there is a
  genuinely necessary human decision.

---

## Delegation Model

Delegation is capability/reasoning-based, not role-based. The main agent
(Sonnet, medium effort, Auto) executes routine work directly — planning,
implementation, testing, CI repair, merge decisions, release observation.
There is no fixed organizational-role roster (no `dev-agent`/`qa-agent`/
`dev-lead-agent`/`release-agent` split) and no required hand-off between
agent "roles" to move a piece of work forward.

Spawn an isolated sub-agent only when one of these is genuinely true:

1. **Stronger reasoning helps.** Non-trivial architecture/design/planning —
   delegate to `opus-architect` (Opus, high effort). See Model Routing above.
2. **Isolated context reduces noise.** A broad read-only search/audit that
   would otherwise bloat the main context — use a general-purpose or
   `Explore`-type agent, tools restricted to what the task needs.
3. **Independent verification benefits from a fresh perspective.** A
   separate read-only review/QA pass while the main lane keeps working.
4. **Genuine parallelism** — two provably independent pieces of work that
   don't touch overlapping files/state.

Subagent concurrency limits and scope ownership are owned by the `engineering-workflow`
Delegation section.

Do not invent new fixed role names as a substitute for the old roster.
When none of the above applies, do the work directly.

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
