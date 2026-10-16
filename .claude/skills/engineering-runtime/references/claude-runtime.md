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

