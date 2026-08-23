# Tracked-file allowlist

What this repo versions, and the boundary against runtime/private/generated
state. A file is tracked because it is intentionally portable desired
state — not because a whole `~/.claude`/`~/.codex` tree was copied.

## Tracked (source of truth)

| Path | What |
|---|---|
| `.mcp.json` | Global MCP server templates, secrets as `${ENV_VAR}` |
| `.claude/CLAUDE.md` | Global behavioral policy |
| `.claude/RTK.md` | `rtk` command reference |
| `.claude/settings.json` | Permissions/model/hooks — global + project scope |
| `.claude/settings.global-only.json` | Settings that only take effect in `~/.claude/` |
| `.claude/config.env` | Push-gate env overrides |
| `.claude/mcp-servers.runtime.json` | Project-runtime MCP servers, secrets redacted |
| `.claude/hooks/` | Workflow/gate shell hooks |
| `.claude/skills/` | Custom skills (`SKILL.md` each) |
| `codex/config.toml` | Curated portable subset of `~/.codex/config.toml` (see file header for exclusions) |
| `codex/AGENTS.md` | Global Codex instructions (generic, no machine-specific content) |
| `scripts/check.sh`, `scripts/install.sh`, `scripts/profile-install.sh`, `scripts/sync-check.sh` | Bootstrap/diagnosis, prerequisite install, profile setup, live/repo drift report |
| `.gitleaks.toml`, `.pre-commit-config.yaml`, `.github/workflows/secret-scan.yml` | Layered secret-scanning config (see `docs/SECURITY.md`) |
| `mise.toml`, `Brewfile` | Declarative CLI toolchain (see `docs/TOOLCHAIN.md`) |
| `bin/coding-agent-profile`, `bin/coding-agent-profile-explain`, `bin/ca-claude`, `bin/ca-codex` | Directory-scoped profile resolver + launchers (see `docs/PROFILES.md`) |
| `profiles/example-profile/` | Generic profile overlay template — never a real overlay |
| `tests/` | Test suite (profile resolver) |
| `README.md`, `docs/` | Install/onboarding/architecture docs |

## Never tracked (runtime/private/generated)

- `.claude.json`, `auth.json`, OAuth credentials (Keychain), session/history state
- `~/.codex/*.sqlite*`, `logs_*`, `history.jsonl`, `session_index.jsonl`
- `~/.codex/config.toml`'s `[projects."<path>"]` trust entries — machine-specific
- `~/.codex/config.toml`'s `notify` field — points at a local `.app` bundle path
- Plugin caches (`**/plugins/`)
- Real credentials of any kind — tracked config uses `${ENV_VAR}` placeholders only
- `.claude/settings.local.json` — session-scoped local permission grants
- `~/.coding-agent-profiles/` — real profile overlays (company names, endpoints, actual MCP servers/instructions); only `profiles/example-profile/`'s generic template is tracked
- `~/.codex/<profile-name>.config.toml` — generated symlink, materialized by `scripts/profile-install.sh`

## Status-line dependency closure

`.claude/statusline.py`, `.claude/subagent-statusline.py`, and
`.claude/hooks/bg-track.py` depend only on: `python3` (stdlib only, no pip
packages), and via `subprocess` — `git`, `df`, `sysctl`, `vm_stat` — all
base macOS/POSIX tools, not separately declared anywhere. Every subprocess
call has a sub-second timeout; failures are swallowed and the segment is
omitted, never blocking the prompt. `statusline.py`'s optional caveman-mode
badge reads a path under `~/.claude/plugins/cache/...` (plugin cache,
intentionally untracked, machine/install-specific) — gated by `os.access`,
degrades to omitted if absent, on any machine without that plugin.

## Precedence

Validated Claude Code settings cascade, low → high (see `docs/PROFILES.md`
for how this was confirmed against the installed binary):

```
userSettings < projectSettings < localSettings < flagSettings (--settings, via ca-claude) < policySettings (managed)
```

Codex precedence (validated via `codex debug prompt-input`):

```
project .codex/config.toml < --profile toml (via ca-codex) < -c session flags < managed config
```

Directory-scoped profiles overlay via `--settings`/`--mcp-config` (Claude)
and `--profile` (Codex) — see `docs/PROFILES.md` for the full design,
including the Tier A (plain invocation: ancestor instructions + direnv
only) vs Tier B (`ca-claude`/`ca-codex`: full overlay) split, and why
managed/policy settings can never be bypassed by a profile.
