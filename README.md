# coding-agent-environment

Versioned, portable configuration for [Claude Code](https://claude.com/claude-code)
and [Codex](https://openai.com/codex/) — global behavioral policy, hooks, skills,
MCP server config, and status-line/toolchain/profile support — with all secrets
redacted to `${ENV_VAR}` placeholders. See [docs/ALLOWLIST.md](docs/ALLOWLIST.md)
for exactly what is and isn't tracked.

## Prerequisites

This configuration depends on several CLI tools. `uv`/`uvx`, `direnv`, and
`gh` are declaratively managed — run `mise install` (see `mise.toml`) and
`brew bundle` (see `Brewfile`). `rtk`, `codegraph`, and `codebase-memory-mcp`
are not mise/Brewfile-manageable — see [docs/TOOLCHAIN.md](docs/TOOLCHAIN.md)
for why and their install paths:

| Tool | Install | Used by |
|---|---|---|
| `rtk` | `cargo install rtk-token-killer` | Token-optimized CLI proxy for dev operations |
| `codegraph` | See [CodeGraph](#codegraph) section | Codebase knowledge graph for symbol/call-path queries |
| `codebase-memory-mcp` | `npm install -g codebase-memory-mcp` (installed via `npx` in MCP config, no separate global install required) | MCP server for structural code queries |
| `uvx` | `mise install` (provides `uv`/`uvx`) | Run Python tools without global installs |
| `direnv` | `mise install` | Auto-load secrets from `.envrc` |
| `gh` | `mise install` | GitHub CLI — used by PR/release skills |

**Graceful degradation**: Most features work without every tool installed.
Missing tools trigger warnings in hooks but do not block execution. Install
tools incrementally as you adopt each capability.

## Layout

```
.mcp.json                      # MCP server templates (env placeholders) - must be at project root
mise.toml                      # Declarative dev-CLI versions (uv, direnv, gh)
Brewfile                       # macOS system packages (jq)
.claude/
├── CLAUDE.md                  # Global behavioral policy (all projects)
├── RTK.md                     # RTK (token-killer proxy) command reference
├── settings.json              # Permissions, model, hook wiring (global + project scope)
├── settings.global-only.json  # Settings that only take effect in ~/.claude/ (plugins, theme, TUI)
├── config.env                 # Hook env overrides (gate toggles)
├── mcp-servers.runtime.json   # Runtime MCP servers, secrets redacted
├── statusline.py              # Status line (2 rows + optional background-task row)
├── subagent-statusline.py     # Custom per-subagent status line rows
├── hooks/                     # Workflow / gate shell hooks (includes bg-track.py, statusline's data source)
└── skills/                    # Custom skills (SKILL.md each)
codex/
├── config.toml                # Portable Codex desired state (see file header for exclusions)
└── AGENTS.md                  # Global Codex instructions
bin/
├── coding-agent-profile        # Directory-scoped profile resolver + --explain
├── coding-agent-profile-explain
├── ca-claude                   # Profile-aware `claude` launcher
└── ca-codex                    # Profile-aware `codex` launcher
profiles/example-profile/       # Generic profile overlay template (copy, don't commit a real one)
scripts/
└── profile-install.sh          # Symlinks bin/ + materializes Codex profile configs
tests/
└── test-profile-resolver.sh    # 13-case / 31-assertion offline test suite
docs/
├── ALLOWLIST.md                # Tracked-file allowlist and runtime/private/generated boundary
├── TOOLCHAIN.md                # mise/Brewfile split, what's intentionally excluded and why
└── PROFILES.md                 # Directory-scoped profile design, precedence, setup
```

## Install

Copy into your home config (review first):

```bash
cp .mcp.json ~/.claude/.mcp.json
cp -R .claude/CLAUDE.md .claude/RTK.md .claude/settings.json \
      .claude/settings.global-only.json .claude/config.env \
      .claude/mcp-servers.runtime.json .claude/statusline.py \
      .claude/subagent-statusline.py .claude/hooks .claude/skills ~/.claude/
```

### Codex

`codex/config.toml` is a curated subset, not a drop-in replacement — merge it
by hand if you already have machine-specific `[projects.*]`/`[mcp_servers.*]`
entries in `~/.codex/config.toml`. `codex/AGENTS.md` can be copied directly:

```bash
cp codex/AGENTS.md ~/.codex/AGENTS.md
```

## MCP servers

`mcp-servers.runtime.json` lists the configured servers. Secrets are redacted —
supply real values via environment before use:

| Server | Auth | How |
|---|---|---|
| cloudflare-* (docs, bindings, observability, browser) | OAuth | `/mcp` → Authenticate |
| neon | OAuth | `/mcp` → Authenticate |
| circleci-mcp-server | `CIRCLECI_TOKEN` | Personal API token |
| gcloud | gcloud CLI creds | `gcloud auth login` |
| codegraph, codebase-memory-mcp | none | local index |
| github | local | localhost bridge |

Register a server:

```bash
claude mcp add --scope user --transport http neon https://mcp.neon.tech/mcp
```

## CodeGraph

[CodeGraph](https://github.com/colbymchenry/codegraph) builds a SQLite knowledge
graph of your codebase's symbols, edges, and files. One `codegraph_explore` call
returns verbatim, line-numbered source of relevant symbols plus call paths between
them — replacing a grep + Read loop with a single round-trip.

### Installation

```bash
# Clone and build
git clone https://github.com/colbymchenry/codegraph.git
cd codegraph
cargo build --release

# Add to PATH (add to ~/.zshrc for persistence)
export PATH="$PATH:$(pwd)/target/release"
```

### Usage

```bash
# Initialize index in a project (creates .codegraph/)
cd /path/to/your/project
codegraph init

# Query symbols and call paths
codegraph explore "function_name"
```

> **Warning**: Do NOT run `codegraph install` — it modifies your global Claude Code
> config in ways that may conflict with this configuration. Only use `codegraph init`
> to create per-project indexes.

### MCP integration

When CodeGraph is installed and a project has a `.codegraph/` directory,
Claude Code can use the `codegraph_explore` MCP tool for structural code queries.
The MCP server is configured in `mcp-servers.runtime.json`.

## Configuration Scope Hierarchy

Claude Code reads configuration from two locations with a defined precedence:

| Scope | Location | Applies to |
|---|---|---|
| **Global** | `~/.claude/` | All projects on this machine |
| **Project** | `<repo>/.claude/` | Single repository only |

### Layering behavior

1. Claude Code loads global config (`~/.claude/`) first
2. Then loads project config (`<repo>/.claude/`) if present
3. Project values **override** global values for the same key

### What belongs where

| File | Global scope | Project scope |
|---|---|---|
| `CLAUDE.md` | Durable behavioral policy, workflow conventions | Repo-specific commands, architecture constraints |
| `settings.json` | Default permissions, model preference | Project-specific permissions, hook overrides |
| `hooks/` | Shared workflow hooks | Project-specific automation |
| `skills/` | General-purpose skills | Domain-specific skills |

### Example: permission layering

```jsonc
// ~/.claude/settings.json (global)
{ "permissions": { "allow": ["Bash(git *)"] } }

// <repo>/.claude/settings.json (project)
{ "permissions": { "allow": ["Bash(npm *)"] } }

// Result: both "git *" and "npm *" are allowed in this repo
```

**Tip**: Keep global config minimal and stable. Use project config for
repo-specific overrides and experimental settings.

## Directory-Scoped Profiles

Every session launched anywhere under a configured filesystem subtree can
inherit that subtree's profile — independent of Git repo boundaries, e.g.
`/work/company-a/**/*` vs `/work/company-b/**/*`. See
[docs/PROFILES.md](docs/PROFILES.md) for the full design (marker grammar,
precedence, Claude/Codex integration, `--explain` diagnostics). Quick start:

```bash
cp -R profiles/example-profile ~/.coding-agent-profiles/<name>
# edit the copy, then:
./scripts/profile-install.sh
cd /path/to/subtree && echo "<name>" > .coding-agent-profile
coding-agent-profile explain
```

## Secrets Management

Use [direnv](https://direnv.net/) to auto-load secrets when entering a project directory.

### Setup

1. Install direnv (managed by `mise.toml`):
   ```bash
   mise install
   ```

2. Add to your shell (e.g., `~/.zshrc`):
   ```bash
   eval "$(direnv hook zsh)"
   ```

3. Create `~/.claude/secrets.env` (never committed):
   ```bash
   # MCP server tokens
   export CIRCLECI_TOKEN="your-token-here"
   export ANTHROPIC_API_KEY="sk-ant-..."

   # Other service credentials
   export GITHUB_TOKEN="ghp_..."
   ```

4. Create `~/.claude/.envrc`:
   ```bash
   source_env secrets.env
   ```

5. Allow direnv:
   ```bash
   cd ~/.claude && direnv allow
   ```

### Security best practices

- **Never commit** `secrets.env` or any file containing credentials
- Add `secrets.env` and `.envrc` to `.gitignore`
- Use `${ENV_VAR}` placeholders in committed config files
- Rotate tokens immediately if accidentally committed
- Use short-lived tokens where possible (OAuth preferred over API keys)

### Per-project secrets

For project-specific secrets, create `<repo>/.envrc`:

```bash
source_up  # Inherit from parent .envrc
export PROJECT_SPECIFIC_KEY="..."
```

## AI-Assisted Onboarding

Copy and paste this prompt into Claude Code to automate setup:

```text
I want to set up coding-agent-environment. Please:

1. Clone the repo if not present:
   git clone https://github.com/Chisanan232/coding-agent-environment.git ~/coding-agent-environment

2. Run the setup check script:
   bash ~/coding-agent-environment/scripts/check.sh

3. Review the output and help me:
   - Install any missing prerequisites
   - Copy config files to ~/.claude/
   - Set up direnv for secrets management
   - Initialize CodeGraph if I want it

4. Verify the setup is complete by checking:
   - ~/.claude/CLAUDE.md exists
   - ~/.claude/settings.json exists
   - Required CLI tools are in PATH

My preferences:
- Scope: [global / project at <path>]
- MCP servers I need: [github, jira, fetch, playwright, codegraph, etc.]

Guide me through each step, explaining what each config file does.
```

### What the onboarding does

- Checks for required CLI tools (rtk, codegraph, uvx, direnv, gh — the last three via `mise install`)
- Validates existing `~/.claude/` configuration
- Guides you through copying config files
- Sets up secrets management with direnv
- Optionally initializes CodeGraph for your projects

## Security

No real credentials are committed. `.claude.json` (holds live tokens / project
history), OAuth credentials (macOS Keychain), and all session/runtime state are
excluded via `.gitignore`. Rotate any token that was ever committed by accident.
