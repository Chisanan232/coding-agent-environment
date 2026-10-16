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

