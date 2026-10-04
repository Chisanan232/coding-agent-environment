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
| Merge Strategy | squash merge, rebase merge, or merge commit |
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

## When to Use the Full Workflow vs. the Quick-Fix Path

The ticket-driven workflow (`ticket-intake → dev-impl-loop → QA handoff → PR`) is
the right contract for substantive feature work. It is overkill for small changes.
Use judgment to choose the right path.

### Quick-fix path (no ticket required)

Use for: single-file bugfixes, documentation updates, configuration tweaks,
personal/exploratory projects with no issue tracker, data science notebooks.

1. Read the relevant code.
2. Make the minimal change.
3. Run impacted tests only — no full suite required for trivial changes.
4. Commit with a clear message.
5. Push directly if all checks pass.

No ticket, no QA handoff, no circuit breaker, no workflow state file needed.
Claude Code acts directly per Rule 5 of the Agent Delegation Model.

### Full workflow path (ticket required)

Use for: new features, significant refactors, bug fixes with regression risk,
any change that touches multiple files or modules, work that requires QA sign-off.

Follow `dev-impl-loop` (5 phases): implement → relative tests → full suite →
pre-commit → QA handoff → PR. See the Skill Invocation Guide.

### Decision rule

> If you can describe the complete change in one sentence and it touches fewer
> than three files, use the quick-fix path.
> Otherwise, use the full workflow.

---

## Testing Expectations

- All new features require tests. All bug fixes require a regression test.
- Tests must be deterministic, isolated, and fast.
- Test behavior, not implementation. Tests must not assert on private internals.
- Do not disable failing tests. Fix them or escalate.
- Coverage is a metric, not the goal. Meaningful tests matter more than coverage %.
- Run impacted tests during iteration. Run the full suite before committing.

> **Project-specific**: test runner, coverage tool, fixture strategy, mocking library,
> test directory layout, coverage threshold, and database test strategy belong in the
> project's `.claude/CLAUDE.md`.

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

## Commit Policy

Every commit must be:

- **Atomic**: one logical concern per commit. If you need two sentences to describe it, split it.
- **Small**: prefer many small commits over one large commit.
- **Bisectable**: the repository must be in a working state after every commit.
- **Descriptive**: subject line under 72 characters in imperative mood.

### Commit message format

```
<emoji> <scope>: <imperative summary under 72 chars>

[Optional body: what changed and why. Not how.]

[Optional footer: closes #123, refs #456]
```

### GitEmoji conventions

| Emoji | Scope |
|---|---|
| `✨` | New feature |
| `🐛` | Bug fix |
| `♻️` | Refactor |
| `✅` | Tests |
| `📝` | Documentation |
| `🔧` | Configuration |
| `🔌` | MCP / integrations |
| `🪝` | Hooks |
| `👨‍💻` | Skills |
| `🧭` | Workflow skills |
| `⬆️` | Dependency upgrade |
| `🗑️` | Delete / remove |
| `🚨` | Fix linting / type errors |

### Commit granularity during implementation

Keep each independently reviewable concern and its necessary tests together.
Choose boundaries by behavior and risk, rather than one commit per function.

### What not to commit

- `.env` files or secrets of any kind
- Build artifacts, compiled outputs
- IDE-specific files not in `.gitignore`
- Large binary files
- Commented-out dead code

---

## Pull Request Policy

### Before opening a PR

- All tests pass locally.
- All lint and type checks pass locally.
- Pre-commit hooks pass.
- CI is not blocked by an unrelated red branch.
- You have reviewed your own diff before requesting review.

### PR size and scope

- Keep PRs under 500 lines when possible.
- One concern per PR. Do not bundle unrelated changes.
- If a change is large, break it into a sequence of stacked PRs.

### PR title format

```
[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>
```

- `[<ticket-number>]` — issue/ticket reference in brackets (e.g., `[PROJ-123]`, `[42]`)
- `<emoji>` — GitEmoji from the Commit Policy table
- `<scope>` — affected module, package, or area
- `<imperative summary>` — what changed, imperative mood

Example: `[PROJ-123] ✨ restapi: Add new user authentication endpoint`

### PR description must include

1. What changed (one paragraph)
2. Why it changed (motivation, context, issue reference)
3. How to verify (manual steps or automated test reference)
4. Related issues / tickets: `Closes #<issue-reference>`

### Review process

- Address all reviewer comments before merging.
- Do not force-push during active review.
- CI must be green before merging.
- Merge strategy: use the strategy configured for this repository (see project-level CLAUDE.md).

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

Claude Code can use MCP-connected tools when available. Check `.mcp.json` for
what is configured. The following capability categories may be available:

| Capability | What it provides | When to use |
|---|---|---|
| `code_repository` | Git operations, PR creation, branch management | Branching, PRs, diffs |
| `issue_tracking` | Read/write issues and tickets | Linking commits to issues |
| `communication` | Post to Slack, notify teams | Status updates on deploys |
| `static_analysis` | SonarQube quality gates, code smells | Pre-PR quality checks |
| `coverage_reporting` | Codecov coverage trends | Coverage regression detection |
| `observability` | Datadog, Sentry alerts, logs | Incident triage |
| `knowledge_search` | Confluence, Notion, internal docs | Architecture lookups |
| `browser_automation` | Playwright UI interaction | Web UI acceptance testing |

When an MCP-backed capability is available for a task, prefer it over manual
approximation. When it is not available, proceed without it and note the gap.

**Active vs opt-in servers:** `fetch` and `github` are active by default.
All other servers (`sonarqube`, `playwright`, `slack`, `clickup`, `jira`, `codecov`, `datadog`)
are disabled by default and must be enabled per-project in the project's `.mcp.json`
or by setting their required credentials in the environment.

---

## Time-Layer Design — Skill-First Polling and Scheduling

Claude Code may run recurring tasks using the `CronCreate` tool or manual periodic invocation. The rule is:
**prefer waking narrow skills, not full agents, for polling and status checks.**

### Default rule

- Wake a **skill** for narrow, scoped checks (PR health, bot PR maintenance, pipeline state).
- Handle strategic re-planning or multi-step coordination directly in the main
  agent only when the polling result actually needs it — delegate to
  `opus-architect` only if that re-planning is itself non-trivial.

### Primary recurring automation targets

| Target | Skill to wake | Wake agent if |
|---|---|---|
| PR health checks | `pr-health-check` | A PR requires merge decision or escalation |
| Bot PR maintenance | `bot-pr-maintainer` (via `pr-health-check`) | CI failure caused by the update itself |
| Release pipeline observation | `release-watch` | Pipeline fails and engineer action is needed |

### Default polling intervals

- PR health check: every 30 minutes during working hours
  (override per project in `.claude/CLAUDE.md`)
- Release watch: every 5 minutes during an open release window
  (override per project in `.claude/CLAUDE.md`)

### How to configure

Use the `CronCreate` tool to schedule recurring skill invocations:
- PR health check: schedule `/pr-health-check` at the 30-minute interval.
- Release window: schedule `/release-preparation` then `/release-watch` at the 5-minute interval.

Alternatively, invoke skills manually at the polling intervals listed above.

Do not schedule a full sub-agent for routine polling. Sub-agents are
stateful and expensive to wake repeatedly — use one only when the skill's
output indicates a decision or coordination genuinely needs isolated
context or stronger reasoning (see Delegation Model above).

### Loop safety rules

- A skill woken by a loop must not trigger another loop.
- A skill must not enter a self-repair cycle — it must report failure and exit.
- If a repair action is needed, the skill reports it; the engineer or agent decides.

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

## Auto-Merge Policy

A pull request may be merged automatically only when **all** of the following conditions are met:

1. **Code owner approval is present** — at least one required reviewer has approved.
2. **All required CI checks pass** — no red status checks on the PR.
3. **No merge conflicts** — the branch merges cleanly into the base.
4. **No unresolved blocking comments** — all `Request Changes` reviews are resolved or dismissed.
5. **Branch is up to date** — the PR branch includes the latest commits from the base branch.

If any condition is not met, do not merge. Wait, fix, or escalate.

### Who may trigger auto-merge

- The main agent may approve merge decisions once all conditions above are met.
- A sub-agent spawned for isolated review/verification must not independently
  trigger a merge — it reports findings back to the main agent.
- Engineer may override and merge manually at any time.

### Merge strategy

Use the merge strategy configured for this repository (defined in the project-level
`.claude/CLAUDE.md` — e.g., squash merge for feature PRs, rebase merge for dependency bumps).

---

## Bot PR Policy

Dependency bots (Dependabot, Renovate) and pre-commit maintenance bots produce
automated PRs that follow a distinct handling path.

### Standard bot PR handling

If a bot PR meets all of the following:
- CI is green
- No merge conflicts
- No scope expansion beyond the automated update

Then: **approve and merge automatically**.

### Bot PR with lock-file conflicts

If a bot PR has lock-file conflicts (e.g., `poetry.lock`, `uv.lock`, `package-lock.json`):

1. Request a rebase using the bot's supported rebase mechanism
   (e.g., comment `@dependabot rebase` or `@renovatebot rebase`).
2. Wait for the bot to rebase and CI to rerun.
3. Re-evaluate the PR against the standard bot PR handling criteria.
4. Approve and merge only when it is clean and CI is green.

Do not manually resolve lock-file conflicts in bot PRs — let the bot handle it.

### Bot PR with CI failure

If a bot PR has CI failure after rebase:
- Investigate the failure root cause.
- If the failure is unrelated to the update, note it and proceed.
- If the failure is caused by the update itself, escalate to the engineer — do not merge.

### Bot PR oversight

The `bot-pr-maintainer` skill and `pr-health-check` skill manage this loop;
the main agent coordinates bot PR state at each polling interval.

---

## Push Gate Policy

Claude Code must not push to any remote branch unless all of the following are true:

1. **Full test suite passes** — run the complete test suite locally, not just impacted tests.
2. **Pre-commit hooks pass** — run `pre-commit run --all-files`. Zero failures.
3. **Linter is clean** — zero violations.
4. **Type checker is clean** — zero errors.
5. **No uncommitted changes remain** — working tree is clean before pushing.
6. **Branch is not behind remote** — pull or rebase before pushing to avoid clobbering.

### Force-push rules

- Force-push is **forbidden** on `main` / `master` / release branches under any circumstance.
- Force-push on feature branches requires **explicit engineer confirmation** and is
  only permitted when rebasing on the base branch (not to rewrite merged history).
- Never force-push during an active code review.

### What gates the push

`full-test-gate.sh` and `precommit-gate.sh` implement these checks. They are
not currently wired as `settings.json` hooks in this repository — run them
manually before pushing (`bash .claude/hooks/full-test-gate.sh`,
`bash .claude/hooks/precommit-gate.sh`) until/unless a project wires them as
a `PreToolUse`/`Stop` hook. Fix the failure — do not use `--no-verify`.

---

## Development Preconditions

Before beginning any implementation task, Claude Code must verify:

1. **Branch is current** — local branch is up to date with the expected remote base.
   Resolve the tracking remote (`git rev-parse --abbrev-ref --symbolic-full-name @{u} | cut -d'/' -f1`)
   and run `git fetch <remote>`. Do not hardcode `origin` — the remote may be named differently.
2. **CI is not red on the base branch** — do not start work on top of a broken base.
   Check the most recent CI run on `main` (or the target branch) before branching.
3. **No uncommitted state** — working tree must be clean before switching branches
   or beginning a new task.
4. **Dependencies are installed** — run the install command if the lock file
   has changed since the last install.
5. **Pre-commit hooks are active** — confirm `.git/hooks/pre-commit` is installed.
   Run `pre-commit install` if missing.

If any precondition fails, stop and resolve it before writing any code.
Do not proceed on a stale or broken foundation.

---

## Git Worktree Workflow

Each ticket is developed in an isolated git worktree so the main working tree
stays clean and multiple tickets can be worked on concurrently without branch-switching.

### Branch naming convention

All feature and fix branches must follow this four-part format:

```
<release-or-phase>/<ticket-number>/<type>/<short-summary>
```

- `<release-or-phase>`: milestone or sprint identifier — resolve from the ticket's
  milestone/sprint field, `$CLAUDE_CURRENT_RELEASE` env var, or
  `.claude/.current-release` file. Examples: `v0.1.0`, `phase1`, `sprint3`.
- `<ticket-number>`: exact ticket reference (e.g., `TEST-1`, `PROJ-123`, `42`)
- `<type>`: GitEmoji category slug matching the primary change type:

  | Type | GitEmoji | When to use |
  |---|---|---|
  | `feat` | ✨ | New feature or capability |
  | `fix` | 🐛 | Bug fix |
  | `refactor` | ♻️ | Refactor with no behavior change |
  | `test` | ✅ | Test-only change |
  | `docs` | 📝 | Documentation change |
  | `config` | 🔧 | Configuration change |
  | `deps` | ⬆️ | Dependency upgrade |
  | `remove` | 🗑️ | Deletion or removal |
  | `lint` | 🚨 | Lint or type error fix |

- `<short-summary>`: 2–4 words from the ticket title in `snake_case`, max 30 characters

Examples:
- `v0.1.0/TEST-1/feat/add_new_endpoint`
- `phase1/PROJ-123/fix/auth_token_refresh`
- `sprint3/42/refactor/extract_payment_service`

### Worktree path convention

Worktrees are created as sibling directories of the main repository.
Because the branch name contains `/` separators, replace each `/` with `-`
when forming the directory name:

```
<repo-parent-dir>/<repo-name>-<release-or-phase>-<ticket-number>-<type>-<short-summary>/
```

Example: main repo at `~/code/my-app`, branch `v0.1.0/TEST-1/feat/add_endpoint`
→ worktree at `~/code/my-app-v0.1.0-TEST-1-feat-add_endpoint/`.

### Lifecycle

| Phase | Command |
|---|---|
| `ticket-pickup-check` — create | `git worktree add <path> -b <branch-name>` |
| Development | All implementation work happens inside `<path>` |
| `workflow-resume` — verify | Check `git worktree list` matches `.claude/.current-worktree` |
| `post-merge-close` — clean up | `git worktree remove <path>` + `git worktree prune` |

### Worktree context resolution

Skills resolve the active worktree path in this order:
1. `$CLAUDE_CURRENT_WORKTREE` environment variable
2. `.claude/.current-worktree` file in the main repo root (written by `ticket-pickup-check`)

Skills resolve the release or phase prefix in this order:
1. `$CLAUDE_CURRENT_RELEASE` environment variable
2. `.claude/.current-release` file in the main repo root (written by `ticket-pickup-check`)
3. The ticket's milestone or sprint field via the issue tracker MCP

Add `.claude/.current-worktree` and `.claude/.current-release` to `.gitignore`.

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

All hooks and utility scripts source `~/.claude/config.env` at startup.
Copy `coding-agent-environment/.claude/hooks/config.env` to `~/.claude/config.env`
and uncomment the variables you want to override.

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_WORKFLOW_STATE_DIR` | `~/.claude/workflow-state` | Per-ticket workflow state JSON files |
| `CLAUDE_CIRCUIT_BREAKER_DIR` | `~/.claude/circuit-breaker` | Circuit breaker state files |
| `CLAUDE_CIRCUIT_BREAKER_THRESHOLD` | `5` | Default failure count before circuit opens |
| `CLAUDE_SENTINEL_DIR` | `~/.claude/sentinels` | Per-repo/branch test pass sentinels |
| `CLAUDE_AUDIT_LOG_DIR` | `~/.claude/audit` | Append-only JSONL command audit log |
| `CLAUDE_DECISION_LOG_DIR` | `~/.claude/decisions` | Structured decision log (daily JSONL) |
| `CLAUDE_DECISION_LOG_ENABLED` | `1` | Set to `0` to disable the decision log |
| `CLAUDE_DECISION_LOG_MAX_CONTEXT` | `500` | Max chars of context captured per entry |
| `CLAUDE_ISSUE_TRACKER` | `github` | Which MCP to use for tickets: `github`, `clickup`, or `jira` |
| `CLAUDE_CURRENT_TICKET` | _(from `.claude/.current-ticket`)_ | Active ticket ref; overrides file lookup |
| `CLAUDE_CURRENT_WORKTREE` | _(from `.claude/.current-worktree`)_ | Active worktree path for the current ticket |
| `CLAUDE_CURRENT_RELEASE` | _(from `.claude/.current-release`)_ | Release or phase prefix for branch naming (e.g., `v0.1.0`, `phase1`); overrides file lookup |
| `CLAUDE_E2E_COMMAND` | _(unset)_ | Command to run E2E tests (e.g., `npx playwright test`) |
| `CLAUDE_SKIP_AUDIT` | `0` | Set to `1` to disable command audit logging |
| `CLAUDE_STALE_PR_DAYS` | `14` | Days of inactivity before a PR is considered stale |
| `CLAUDE_SKIP_TEST_GATE` | `0` | Set to `1` to disable the full-test-gate push gate globally |
| `CLAUDE_SKIP_PRECOMMIT_GATE` | `0` | Set to `1` to disable the precommit-gate push gate globally (useful when pre-commit is run manually during dev-impl-loop) |
| `CLAUDE_STRICT` | `0` | Set to `1` to treat quality_gate warnings (debug statements, unlinked TODOs) as errors that block the next action |
| `CLAUDE_SESSION_NOTES_DIR` | `~/.claude/session-notes` | Directory for per-ticket Markdown session notes |
| `CLAUDE_INTEGRATION_TEST_COMMAND` | _(unset)_ | Command to run cross-repo integration tests (cross-repo-coordinator Phase 4) |
| `CODING_AGENT_PROFILE_DIR` | `~/.coding-agent-profiles` | Directory-scoped profile overlays root — see `docs/PROFILES.md` |
| `CODING_AGENT_ALLOW_HOME_PROFILE` | `0` | Set to `1` to allow a `.coding-agent-profile` marker at `$HOME` (ignored by default) |
| `CODING_AGENT_MANAGED_SETTINGS_PATH` | OS default | Override for testing/diagnostics — Claude managed-settings.json path |
| `CODING_AGENT_CODEX_MANAGED_CONFIG_PATH` | OS default | Override for testing/diagnostics — Codex managed-config path |
| `CA_DRY_RUN` | `0` | Set to `1` on `ca-claude`/`ca-codex` to print resolved argv instead of exec'ing |

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

Do not invent new fixed role names as a substitute for the old roster.
When none of the above applies, do the work directly.

---

## Workflow State Management

Claude Code persists workflow progress for each ticket so interrupted sessions
can be resumed without restarting from zero.

### How it works

- Skills write state at each phase transition using the `workflow-state.sh` utility:
  ```bash
  bash ~/.claude/hooks/workflow-state.sh write <ticket> <workflow> <step> <total> <status>
  ```
- State is stored as JSON in `~/.claude/workflow-state/<ticket>.json`.
- When a session is interrupted, run `/workflow-resume <ticket>` to continue
  from the last recorded phase without re-running completed work.
- After a ticket is fully complete, state is archived:
  ```bash
  bash ~/.claude/hooks/workflow-state.sh archive <ticket>
  ```

### State file fields

| Field | Purpose |
|---|---|
| `ticket` | Ticket reference (e.g., `PROJ-123`) |
| `workflow` | Skill name currently executing (e.g., `dev-impl-loop`) |
| `step` | Current step number within the skill |
| `total_steps` | Total steps in the skill |
| `status` | `in_progress`, `awaiting_review`, `complete`, `escalated`, `circuit_open` |
| `timestamp` | ISO 8601 UTC time of last write |

### Ticket context — how skills resolve the active ticket reference

1. `$CLAUDE_CURRENT_TICKET` environment variable (set by CI or the engineer)
2. `.claude/.current-ticket` file in the repo root (written by `ticket-pickup-check`)
3. Prompt the engineer if neither is set

`ticket-pickup-check` writes the ticket ref to both on self-assign. Skills
must never hardcode a ticket ref — always use the resolution order above.
Add `.claude/.current-ticket`, `.claude/.current-worktree`, and `.claude/.current-release` to `.gitignore`.

### Three-layer observability

| Layer | Tool | Records |
|---|---|---|
| Command audit | `audit_log.sh` | Every Bash command run, exit code |
| Decision log | `decision-log.sh` | Why each phase decision was made |
| Workflow state | `workflow-state.sh` | Which phase the skill was in |

Use `decision-log.sh tail` or `decision-log.sh query --ticket T` to trace
why the system acted as it did without reconstructing from raw command logs.

### Which skills use workflow-state.sh

Not all skills write to `workflow-state.sh`. The table below shows the tracking
mechanism for each multi-phase skill:

| Skill | State tracking | Resume via |
|---|---|---|
| `dev-impl-loop` | `workflow-state.sh` | `/workflow-resume <ticket>` |
| `ticket-pickup-check` | `workflow-state.sh` (initial write only) | `/workflow-resume <ticket>` |
| `post-merge-close` | `workflow-state.sh` (final write only) | Re-run skill (checkpoint file handles idempotency) |
| `cross-repo-coordinator` | `session-memory.sh` | Follow the "Resuming an interrupted session" section in the skill's own SKILL.md — `/workflow-resume` will find no state file |

### What workflow state does not replace

- State tracks **which phase** was reached, not the content of changes made.
- Git history is the authoritative record of what code was committed.
- The audit log records what commands ran; the decision log records why.
- Use `git log`, audit log, and decision log to understand *what* and *why*;
  use the state file only to determine *where to resume*.

---

## Circuit Breaker Policy

Claude Code uses a circuit breaker to prevent runaway repair loops. When the
same ticket accumulates too many consecutive failures, the circuit opens and
all further attempts are blocked until an engineer intervenes.

### States

| State | Meaning |
|---|---|
| **Closed** | Normal operation — failures recorded but below threshold |
| **Open** | Threshold exceeded — further attempts blocked |
| **Reset** | Manually cleared by engineer — returns to Closed |

### Thresholds (per skill)

| Skill / Phase | Threshold |
|---|---|
| `dev-impl-loop` Phase 1 (relative tests) | 5 consecutive failures |
| `dev-impl-loop` Phase 2 (full suite repair) | 3 consecutive failures |
| `dev-impl-loop` Phase 5 (post-QA repair cycles) | 3 QA rejection cycles |

### How to use

Skills call the utility directly:
```bash
# Check before entering a loop
bash ~/.claude/hooks/circuit-breaker-gate.sh check <ticket> [threshold]

# Record a failure after each failed iteration
bash ~/.claude/hooks/circuit-breaker-gate.sh record-failure <ticket> [threshold]

# Record a success (resets consecutive failure count)
bash ~/.claude/hooks/circuit-breaker-gate.sh record-success <ticket>

# Manual reset after engineer review
bash ~/.claude/hooks/circuit-breaker-gate.sh reset <ticket>
```

### When the circuit opens

1. Stop the repair loop immediately.
2. Write workflow state as `circuit_open`.
3. Report the failure summary and ticket reference back to the engineer.
4. Do not attempt further repairs until the engineer reviews the situation
   and resets the breaker.

---

## Session Memory

Claude Code can persist notes for a ticket across interrupted sessions using the
`session-memory.sh` utility. Notes survive context resets and process restarts.

### When to use

- When beginning work on a ticket that was previously interrupted.
- When the circuit breaker trips — record why before stopping.
- When a meaningful decision is made mid-session that future sessions should know.

### How to use

```bash
# Surface prior notes at session start
bash ~/.claude/hooks/session-memory.sh read "$TICKET"

# Append a decision or blocker note
bash ~/.claude/hooks/session-memory.sh append "$TICKET" "Section title" "Body text"

# Clear notes after the ticket is fully closed
bash ~/.claude/hooks/session-memory.sh clear "$TICKET"

# List all tickets with active session notes
bash ~/.claude/hooks/session-memory.sh list
```

### Notes storage

Notes are stored as Markdown in `${CLAUDE_SESSION_NOTES_DIR:-~/.claude/session-notes}/<ticket>.md`.
Each note has a frontmatter header and timestamped sections.

### What session notes do not replace

- **Workflow state** — which phase the skill is in. Use `workflow-state.sh`.
- **Decision log** — why each phase decision was made. Use `decision-log.sh`.
- **Git history** — what code was committed. Use `git log`.
- Session notes capture **conversational context** (decisions, blockers, partial
  work completed) that is not represented in any of the above.

---

## Cross-Repo Work

When a feature or bugfix requires changes across multiple repositories,
use `cross-repo-coordinator` skill instead of single-repo `task-decomposition`.

### When to use

A ticket requires cross-repo coordination when:
- An API changes in one repo and a consumer must be updated in another.
- A shared library is updated and dependent services need simultaneous bumps.
- A new feature spans a backend repo and a frontend repo.

### How it works

`cross-repo-coordinator` uses per-repo child tickets only when separate tracking
has concrete value (see `task-decomposition`); otherwise the parent owns the plan. It
tracks PR status across all repos, and gates all merges until:
1. Every per-repo sub-ticket has passed QA.
2. Integration tests pass (`CLAUDE_INTEGRATION_TEST_COMMAND`).

Only then does it coordinate the merge in dependency order and close the parent.

### Parent ticket as coordination anchor

Use the parent ticket ref for all cross-repo session notes:
```bash
bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
  "Cross-repo state" "Repo A: done | Repo B: in progress"
```

This allows any agent, in any repo session, to see the full cross-repo picture.

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
