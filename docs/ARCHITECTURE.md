# Architecture

How this repository's pieces fit together, and why. Deeper detail on any one
area lives in its own doc (linked below) — this page is the map, not the
territory.

## Design goals

- **Autonomous-first, not bypass-security.** The main agent (Sonnet) acts
  directly on routine work without stopping for approval at every step, but
  never at the cost of managed/policy settings, secret handling, or
  destructive-action confirmation.
- **Token/context efficiency.** Global instructions stay concise; workflow
  detail lives in skills, rationale lives in docs/ADRs (this file's whole
  reason for existing).
- **High-signal observability.** Status line surfaces model/reasoning,
  context pressure, quota, Git state, and — where a profile is active —
  which one, without heavy polling.
- **Portable desired state, not tree snapshots.** Version only what's
  intentionally reproducible; never a whole `~/.claude`/`~/.codex` copy.
- **Secret/runtime-state exclusion by construction.** `${ENV_VAR}`
  placeholders in tracked config, layered scanning, one-time history audit.
- **Deterministic profile inheritance.** A small resolver walks filesystem
  ancestry; the model never infers which environment it's in.
- **Native-tool-first.** Reach for a heavier abstraction only when the
  native mechanism (settings precedence, `--settings`, ancestor `CLAUDE.md`)
  genuinely can't do the job.
- **Capability/reasoning-based delegation**, not simulated organizational
  roles — see [ADR-0002](adr/0002-capability-based-delegation.md).

## Non-goals

- Not a general dotfiles manager — scope is coding-agent configuration only.
- Not a product with its own release cadence; splits out only if that
  changes (see SPE-69's own planning note).
- Not Windows-first (see `docs/PROFILES.md`'s explicit out-of-scope list).
- Not a secrets vault — delegates entirely to `direnv`/environment.

## Layering

```
                    ┌─────────────────────────────┐
                    │   Managed / corporate policy  │  highest precedence,
                    │   (policySettings, managed-   │  never bypassed by
                    │   config.toml)                │  anything below
                    └───────────────┬───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │  Directory-scoped profile      │  ca-claude --settings /
                    │  overlay (Tier B)               │  --mcp-config; codex
                    │  docs/PROFILES.md               │  --profile
                    └───────────────┬───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │  Project (<repo>/.claude/,      │
                    │  <repo>/.codex/config.toml)     │
                    └───────────────┬───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │  Local/private overrides        │
                    │  (settings.local.json, .envrc)  │
                    └───────────────┬───────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │  User/global (~/.claude/,       │  this repo's
                    │  ~/.codex/) — THIS REPO's       │  desired-state
                    │  desired state applies here      │  baseline
                    └─────────────────────────────────┘
```

Validated precedence strings for both tools, and the Tier A (ancestor
instructions + `direnv`, any invocation) vs Tier B (`ca-claude`/`ca-codex`,
full overlay) split, are in `docs/PROFILES.md`.

## Model routing

Sonnet main (medium effort, Auto permission mode) handles routine work
directly; `opus-architect` (Opus, high effort) is invoked only for
non-trivial architecture/design. Full policy in `.claude/CLAUDE.md`'s
**Model Routing** section. Why the old role-simulating agent roster
(`dev-agent`/`qa-agent`/`dev-lead-agent`) was retired: [ADR-0002](adr/0002-capability-based-delegation.md).

## Permission governance

`.claude/settings.json#permissions` — Auto Mode `allow`/`ask`/`deny` rules,
the mandatory-safeguard list, and the known RTK self-decision defect
(SPE-83) this repo structurally works around: `docs/SECURITY.md`'s
Permission governance section. Why Auto Mode stays the default over
`bypassPermissions`, and why the `ask` list shrank from 60 to 47 rather
than growing indefinitely: [ADR-0012](adr/0012-autonomous-permission-governance.md).

## Observability

Status-line design, dependency closure, and performance constraints:
`docs/ALLOWLIST.md`'s Status-line dependency closure section. Why status
line counts as first-class reproducible config, not cosmetic: [ADR-0004](adr/0004-status-line-first-class.md).

## MCP / plugin / skill / agent desired state

- **`.mcp.json`** (repo root) — global MCP template, tiered
  `always-active`/`project-opt-in`, secrets by `${ENV_VAR}` reference.
- **`.claude/mcp-servers.runtime.json`** — project-runtime MCP servers,
  intentionally a second file (global template vs project runtime), not
  accidental duplication — see [ADR-0005](adr/0005-mcp-source-of-truth.md).
- **`.claude/skills/`** — capability-scoped workflow skills, retired of
  role-agent framing per [ADR-0002](adr/0002-capability-based-delegation.md).
- **`.claude/agents/opus-architect.md`** — the one capability-based agent
  currently tracked; spawned for architecture work, not a fixed role.
- Dead/unavailable integrations are removed from active desired state
  (SPE-70's audit), not kept as reminders.

## Toolchain

`mise.toml` + `Brewfile` for CLI dependencies with a reliable declarative
backend; `rtk`/`codegraph`/`codebase-memory-mcp` explicitly excluded with
reasons in `docs/TOOLCHAIN.md`. Decision rationale: [ADR-0006](adr/0006-toolchain-management.md).

## Directory-scoped profiles

Full design in `docs/PROFILES.md`. Decision rationale: [ADR-0007](adr/0007-directory-scoped-profiles.md).

## Bootstrap / sync / validation

`scripts/install.sh` (prerequisites), `scripts/check.sh` (diagnostics),
`scripts/sync-check.sh` (report-first live↔repo drift, never auto-copies),
layered secret scanning (`docs/SECURITY.md`).

## Repository identity

Renamed `claude-code-config` → `coding-agent-environment` to match final
scope (Claude Code + Codex + toolchain + profiles, not Claude-only).
Rationale: [ADR-0008](adr/0008-repository-rename.md).

## ADR index

| ADR | Decision |
|---|---|
| [0001](adr/0001-version-desired-state-not-full-trees.md) | Version desired state, not entire `~/.claude`/`~/.codex` trees |
| [0002](adr/0002-capability-based-delegation.md) | Capability-based delegation, not organizational-role agents |
| [0003](adr/0003-model-routing-strategy.md) | Sonnet main + selective Opus advisor/architect, not `opusplan` as default |
| [0004](adr/0004-status-line-first-class.md) | Status line is first-class reproducible operational config |
| [0005](adr/0005-mcp-source-of-truth.md) | MCP/plugin/config source-of-truth boundaries |
| [0006](adr/0006-toolchain-management.md) | mise + Brewfile toolchain management, explicit exceptions |
| [0007](adr/0007-directory-scoped-profiles.md) | Deterministic directory-scoped profiles + private corporate overlays |
| [0008](adr/0008-repository-rename.md) | Repository rename to `coding-agent-environment` |
| [0009](adr/0009-bootstrap-mechanism.md) | Bootstrap/apply mechanism: plain scripts, not a config-management tool |
| [0012](adr/0012-autonomous-permission-governance.md) | Autonomous-first permission governance (Auto Mode `allow`/`ask`/`deny`) |
