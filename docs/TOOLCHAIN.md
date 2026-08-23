# Toolchain management

Coding-agent CLI dependencies, split by declarative manager. See
`scripts/check.sh` for the drift audit (missing/untracked/hardcoded-path
report) and `scripts/install.sh` for interactive/selective install.

## mise-managed (`mise.toml`)

| Tool | Backend | Used by |
|---|---|---|
| `uv` (provides `uvx`) | `aqua:astral-sh/uv` | Run Python tools without global installs |
| `direnv` | `aqua:direnv/direnv` | Secrets loading (`.envrc`) |
| `gh` | `aqua:cli/cli` | GitHub CLI — PR/release skills |

Install: `mise install` (from repo root).

## Homebrew-managed (`Brewfile`)

| Package | Used by |
|---|---|
| `jq` | `scripts/check.sh`, various skills |

Install: `brew bundle`.

## Intentionally NOT mise/Brewfile-managed

| Tool | Why | Install path |
|---|---|---|
| `rtk` | Not in the mise core/aqua registry; niche cargo crate | `cargo install rtk-token-killer` (see README Prerequisites) |
| `codegraph` | Custom tool, installed via `git clone` + `cargo build --release`, not a published package | See README's CodeGraph section |
| `codebase-memory-mcp` | Installed on-demand via `npx` from MCP config itself — no separate global install step needed | `.mcp.json`'s `codebase-memory-mcp` entry runs `npx -y codebase-memory-mcp` |

Forcing these into mise/Brewfile would add indirection without improving
reproducibility — `scripts/install.sh` retains bespoke install logic for
them, per SPE-73's guardrail against forcing every dependency into one
manager. `install_direnv()` in `scripts/install.sh` now prefers `mise use -g`
when `mise` is present, falling back to its existing OS-specific logic
otherwise.

## Required vs optional

All three tables above are **required** for the full experience described in
this repo's `settings.json`/hooks/skills. None are optional/experimental.

## Drift auditing

`scripts/check.sh` already reports missing/present for `rtk`, `codegraph`,
`uvx`, `codebase-memory-mcp`, `direnv` (see its `check_*` functions). It does
not yet cross-reference `mise.toml`/`Brewfile` state or flag
machine-specific hardcoded paths outside of what SPE-70's audit already
found and fixed (`.mcp.json`, `mcp-servers.runtime.json`,
`cbm-code-discovery-gate`) — extending `check.sh` to do a full
mise/Brewfile-state diff is left as a natural follow-up, not required by
SPE-73's acceptance criteria as written (declarative manifests + audit of
what's already checked + no duplicate ownership).
