# ADR-0006: mise + Brewfile toolchain management, explicit exceptions

## Status
Accepted (SPE-73)

## Context
Coding-agent CLI dependencies (`rtk`, `codegraph`, `uv`/`uvx`, `direnv`,
`gh`, `codebase-memory-mcp`) were installed via ad-hoc bash functions in
`scripts/install.sh`, with no single declarative source of truth and at
least one hardcoded install path baked into a live hook script.

## Decision
Split by what each tool's install path actually supports: `mise.toml` for
tools with a reliable declarative backend (`uv`, `direnv`, `gh` — all
verified present in mise's `aqua` registry before being added), `Brewfile`
for system packages (`jq`). `rtk`, `codegraph`, and `codebase-memory-mcp`
are explicitly **not** forced into either — `rtk` is a niche cargo crate,
`codegraph` is a custom `git clone` + `cargo build` tool, `codebase-memory-mcp`
installs on-demand via `npx` from the MCP config itself. Forcing these into
mise/Brewfile would add indirection without improving reproducibility.
`scripts/install.sh`'s `install_direnv()` now tries `mise use -g` first,
falling back to its original per-OS logic unchanged.

## Consequences
- Two declarative manifests to keep in sync with actual usage, audited by
  `scripts/check.sh`'s prerequisite checks (not yet a full manifest-state
  diff — noted as a reasonable follow-up, not required by this decision).
- The three excluded tools keep their bespoke install logic in
  `scripts/install.sh` rather than being shoehorned into a manager that
  doesn't fit them well — documented with rationale in
  `docs/TOOLCHAIN.md` so a future maintainer doesn't "fix" this by
  force-fitting them in.
