# ADR-0007: Deterministic directory-scoped profiles + private corporate overlays

## Status
Accepted (SPE-74)

## Context
A corporate machine may need different MCPs, permissions, environment
variables, and instructions depending on which client/company subtree a
session runs under — independent of Git repository boundaries. The model
must never be asked to infer which environment it's in from context clues;
identity must come from a deterministic, auditable mechanism.

## Decision
A `.coding-agent-profile` marker file (single-line name, nothing else —
no endpoints/secrets/instructions in the marker itself) resolved by
walking from the physical cwd upward, nearest-marker-wins. Real overlays
(`~/.coding-agent-profiles/<name>/`) are never committed to this public
repo — only `profiles/example-profile/`'s generic template ships. Applied
via each tool's **native** overlay mechanism (`--settings`/`--mcp-config`
for Claude, `--profile` for Codex) through purpose-built launchers
(`ca-claude`/`ca-codex`) that never shadow the real binary names and emit
only from a closed, fixed flag set — no profile-controlled data ever
becomes an arbitrary CLI argument.

A concrete trade-off was resolved during design: Claude Code exposes no
environment-variable equivalent of `--settings`, so a **plain** `claude`/
`codex` invocation cannot get full settings/MCP/status-line inheritance
without either an unavailable env var or a PATH shim named `claude`/
`codex` — explicitly disallowed as a shadowing/recursion/opaque-upgrade
risk. Resolved as a two-tier model: Tier A (any invocation, including
plain `claude`/`codex`) gets ancestor-instruction + `direnv` inheritance;
Tier B (`ca-claude`/`ca-codex` only) gets the full overlay.

## Consequences
- Full-fidelity profile inheritance requires using `ca-claude`/`ca-codex`
  instead of the bare commands — a real (documented, not hidden) UX cost
  in exchange for never shadowing the real tools.
- Managed/corporate policy settings remain structurally unbypassable —
  `--settings`/`--profile` sit below the managed-config layer in both
  tools' validated precedence, verified against the installed binaries,
  not assumed. `scripts/check.sh`'s `check_settings_precedence` re-verifies
  this holds on every diagnostic run, since it's observed behavior rather
  than a published contract.
- `coding-agent-profile explain` exists specifically so "why did this
  session behave this way" is always answerable without guessing.
