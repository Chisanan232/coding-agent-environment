# coding-agent-environment

Versioned, portable configuration for [Claude Code](https://claude.com/claude-code)
and [Codex](https://openai.com/codex/) — global behavioral policy, hooks, skills,
MCP server config, and status-line/toolchain/profile support — with all secrets
redacted to `${ENV_VAR}` placeholders. See [docs/ALLOWLIST.md](docs/ALLOWLIST.md)
for exactly what is and isn't tracked, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for design goals, layering, and links to every ADR behind a durable decision.

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
.gitleaks.toml                 # Secret-scan config (allowlist for intentional placeholders)
.pre-commit-config.yaml        # Local gitleaks pre-commit hook
.github/workflows/secret-scan.yml  # CI gitleaks scan on push/PR
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
├── config.toml                # Portable subset; never globally replaced
├── capabilities-readonly.config.toml # Opt-in read-only external profile
├── AGENTS.md                  # Global instructions + managed signal-first block
└── subtraction-skills.json    # Canonical source revision/digests, no skill bodies
bin/
├── coding-agent-profile        # Directory-scoped profile resolver + --explain
├── coding-agent-profile-explain
├── ca-claude                   # Profile-aware `claude` launcher
└── ca-codex                    # Profile-aware `codex` launcher
profiles/example-profile/       # Generic profile overlay template (copy, don't commit a real one)
scripts/
├── check.sh                    # Diagnostics (prerequisites, config, profiles, precedence)
├── install.sh                  # Prerequisite CLI installer
├── profile-install.sh          # Profile links + managed Codex global report/apply/check
└── sync-check.sh               # Report-first live<->repo drift check
tests/
├── test-profile-resolver.sh    # 13-case / 31-assertion offline test suite
└── test-capability-profile.sh  # External-profile ownership/lifecycle checks
docs/
├── ARCHITECTURE.md             # Design goals, layering, ADR index
├── ALLOWLIST.md                # Tracked-file allowlist and runtime/private/generated boundary
├── TOOLCHAIN.md                # mise/Brewfile split, what's intentionally excluded and why
├── PROFILES.md                 # Directory-scoped profile design, precedence, setup
├── SECURITY.md                 # Secrets boundary, scanning layers, history-scan result
└── adr/                        # 9 ADRs for durable migration/model/tooling/profile/rename decisions
```

## Install

Copy into your home config (review first):

```bash
cp .mcp.json ~/.claude/.mcp.json
cp -R .claude/CLAUDE.md .claude/RTK.md .claude/settings.json \
      .claude/config.env \
      .claude/mcp-servers.runtime.json .claude/statusline.py \
      .claude/subagent-statusline.py .claude/hooks .claude/skills ~/.claude/
```

### Canonical subtraction skills

Merge the `enabledPlugins` and `extraKnownMarketplaces` entries from
`.claude/settings.global-only.json` into `~/.claude/settings.json`; the separate
file is a desired-state reference, not a settings file Claude loads automatically.
Preserve existing permissions, hooks and machine-specific settings.

Install from the canonical repository (verified with Claude Code 2.1.274):

```bash
claude plugin marketplace add Chisanan232/requirement-zero --scope user
claude plugin install requirement-zero@requirement-zero --scope user
claude plugin details requirement-zero@requirement-zero
```

The plugin exposes `requirement-zero:requirement-zero` and
`requirement-zero:codebase-zero`. Restart Claude Code to load newly installed
skills. Update through `claude plugin update requirement-zero@requirement-zero`;
never copy its skill bodies here. Approved work is not re-opened by Requirement Zero.

### Codex

`codex/config.toml` is a curated subset, not a drop-in replacement — merge it
by hand if you already have machine-specific `[projects.*]`/`[mcp_servers.*]`
entries in `~/.codex/config.toml`. Preserve unrelated global instructions when
merging `codex/AGENTS.md`.

For an opt-in read-only capability session, install the native external profile
and launch Codex with it:

```bash
scripts/profile-install.sh --capabilities --dry-run
scripts/profile-install.sh --capabilities
scripts/profile-install.sh --capabilities --check
codex --profile capabilities-readonly
```

The profile disables the broad Neon app for that session and enables the
official Neon read-only MCP endpoint with a fixed ten-tool allowlist. Only
`run_sql` has Codex's native per-tool `approve` override, because the fixed URL
enforces read-only SQL and this profile explicitly authorizes bounded reads.
OAuth requests only the `read` scope; the profile does not grant write or
secret-management tools. Installation owns only
`~/.codex/capabilities-readonly.config.toml`, keeps it mode `0600`, backs up an
owned prior version before update/removal, and refuses unknown or symlinked
targets. It never rewrites `~/.codex/config.toml` or OAuth/auth state. Remove it
with `scripts/profile-install.sh --capabilities --remove`; inspect drift with
`scripts/sync-check.sh --capabilities`. `CODING_AGENT_SYNC_HOME` selects a
disposable home for offline lifecycle validation.

The shared authored skills are
[evidence-first-briefing](.claude/skills/evidence-first-briefing/SKILL.md) and
[engineering-workflow](.claude/skills/engineering-workflow/SKILL.md). Claude
consumes them directly; Codex receives exact materialized user-scoped copies.
Edit only these sources, then report, apply and check:

```bash
scripts/profile-install.sh --global --dry-run
scripts/profile-install.sh --global
scripts/profile-install.sh --global --check
```

This mode owns the marked signal-first AGENTS block, the materialized shared skills,
and canonical subtraction installations. Apply preflights the complete plan,
backs up changed owned paths under `~/.codex/backups/coding-agent-environment/`,
and atomically replaces the managed AGENTS/shared-skill files. It preserves
unrelated instructions and Codex config/plugins/trust. External skill provenance and file digests live
in `codex/subtraction-skills.json`; bodies remain owned by
[Chisanan232/requirement-zero](https://github.com/Chisanan232/requirement-zero).
The pinned skills CLI installs the selected revision into universal
`~/.agents/skills`, which native Codex discovers. Approved work is never reopened.
It uses `CODING_AGENT_SYNC_HOME` for disposable validation homes; native profile
installation without `--global` retains its existing behavior.

Successful installation is not behavior proof: start a fresh Codex process after
applying. `scripts/sync-check.sh --codex` checks only the managed signal-first
block, shared-skill bytes and canonical subtraction body/reference digests; it
ignores unrelated global instructions and private/generated config. The default
sync check also covers the existing Claude and launcher surfaces. Restore the
corresponding saved paths from the reported backup if recovery is needed.
Global mode supports the standard user home; a custom `CODEX_HOME` is refused
rather than silently applying to a different session scope. Report/check are
offline and never run the external installer. Apply needs `npx`/network only
when canonical subtraction content is missing or differs.

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

## Bootstrap, Sync, and Validation

- `scripts/install.sh` — installs prerequisite CLI tools (interactive,
  `--missing`, or `--all`).
- `scripts/check.sh` — diagnostics: prerequisites, config file validity,
  env vars, directory-profile resolver health, settings-precedence
  confirmation. `--json` for machine-readable output. Non-zero exit on any
  issue found.
- `scripts/sync-check.sh` — **report-first** bidirectional drift check
  between this repo's tracked files and the live machine (`~/.claude`,
  `~/.codex`, `~/.local/bin`). Reports `DIFFERS`/`MISSING-LIVE` per file;
  never copies anything automatically in either direction — you decide
  which side is correct per file, then `cp` explicitly.

Run the relevant offline suites before completion:

```bash
bash tests/test-profile-resolver.sh
bash tests/test-capability-profile.sh
python3 tests/test-signal-density.py
pre-commit run --all-files
```

The signal suite consumes synthetic preservation/negative-control fixtures and
checks report-only bootstrap/drift behavior. Its anchors do not grade arbitrary
prose or establish measured reading time; review meaning against the source.
The enforcement decision and verification limits are owned by
[SPE-82](https://lightning-dust-mite.atlassian.net/browse/SPE-82).

For an isolated desired-state installation, set `CODING_AGENT_SYNC_HOME` when
running `scripts/sync-check.sh`. It selects the inspected config root without
changing the process home or copying files. Live settings compare owned fields
and allow native plugin root entries; canonical plugin state is checked separately.

See [docs/SECURITY.md](docs/SECURITY.md) for the secret-scanning layers
(pre-commit, CI, one-time full-history scan) and rotation policy.

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
