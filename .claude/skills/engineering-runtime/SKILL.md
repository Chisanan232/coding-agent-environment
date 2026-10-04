---
name: engineering-runtime
description: "Resolve host-native ticket/worktree context and invoke shared workflow state, notes, circuit, decision and callable quality gates. Use when a workflow needs persisted engineering state."
---

# Engineering runtime

This skill supplies helpers, not automatically registered hooks. Resolve this
installed skill directory and initialize each shell that runs a workflow:

```bash
ENGINEERING_BOOTSTRAP='<installed engineering-runtime>/scripts/bootstrap.py'
eval "$(python3 "$ENGINEERING_BOOTSTRAP")"
# Only the host's own user-managed shell configuration may customize defaults.
if [[ -f "$ENGINEERING_CONFIG_ENV" ]]; then source "$ENGINEERING_CONFIG_ENV"; fi
eval "$(python3 "$ENGINEERING_BOOTSTRAP")"
```

The bootstrap returns quoted exports for helper paths, native `.claude`/`.codex`
context, host-private state/config and explicit ticket/worktree/release values.
Codex never sources Claude configuration or borrows Claude mutable state. To
handoff from another host, read that host's relevant notes/checkpoints explicitly
and reconcile them against current Git, PR and tracker evidence. Do not copy
history, OAuth, secrets or an entire state directory.

Invoke the canonical helpers through `$ENGINEERING_RUNTIME`: `workflow-state.sh`,
`session-memory.sh`, `circuit-breaker-gate.sh`, `decision-log.sh`, `audit_log.sh`,
`full-test-gate.sh`, `precommit-gate.sh`, `quality_gate.sh`. State/notes/decisions
are phase evidence, not proof of acceptance. Circuit reset requires the
engineer's resolution; do not clear an open breaker to keep trying.

The callable push/audit/quality helpers accept a normalized JSON stdin payload:
`tool_input.command`, `tool_input.file_path`, `tool_response.exitCode` and
`hook_event_name`. Translate native `cmd` to `command` only at this boundary;
do not claim hook execution merely because these files exist. The existing
Claude defaults and warning/strict semantics are retained. Run required checks
even when a convenience gate warns that a tool is unavailable. Gate opt-outs
do not authorize bypassing `engineering-workflow`.

Use native process/task handles for background work; poll the same known live
handle with bounded backoff. A healthy finite operation is a verified wait,
not a blocker. Never restart it after a mere observation timeout. Reuse the
minimum necessary simulator/emulator and preserve runtimes owned by active jobs.

The [RTK reference](references/RTK.md) describes optional direct CLI compression.
Claude's command-rewrite hook is host-specific; no shell interception is added.

Host-private configuration is optional: Claude retains `~/.claude/config.env`;
Codex uses `~/.codex/engineering.env`. Neither is installed or overwritten.
Use `ENGINEERING_ISSUE_TRACKER` (`github`, `clickup`, `jira`),
`ENGINEERING_E2E_COMMAND`, `ENGINEERING_INTEGRATION_TEST_COMMAND`,
`ENGINEERING_STALE_PR_DAYS` and `ENGINEERING_CURRENT_{TICKET,WORKTREE,RELEASE}`
for overrides. State-directory, strict/skip and threshold overrides use the
`ENGINEERING_` names exported by the bootstrap. Claude legacy `CLAUDE_` names
remain supported in Claude only. Never store credentials in these files; use
native supported authentication. Opt-outs never replace required validation.
