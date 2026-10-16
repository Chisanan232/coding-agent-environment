# Claude runtime reference

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

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

