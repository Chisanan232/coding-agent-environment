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
| `scripts/check.sh`, `scripts/install.sh` | Bootstrap/diagnosis, prerequisite install |
| `README.md`, `docs/` | Install/onboarding/architecture docs |

## Never tracked (runtime/private/generated)

- `.claude.json`, `auth.json`, OAuth credentials (Keychain), session/history state
- `~/.codex/*.sqlite*`, `logs_*`, `history.jsonl`, `session_index.jsonl`
- `~/.codex/config.toml`'s `[projects."<path>"]` trust entries — machine-specific
- `~/.codex/config.toml`'s `notify` field — points at a local `.app` bundle path
- Plugin caches (`**/plugins/`)
- Real credentials of any kind — tracked config uses `${ENV_VAR}` placeholders only
- `.claude/settings.local.json` — session-scoped local permission grants

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

## Precedence (informal — see SPE-74 for the formal directory-profile model)

1. Global (`~/.claude/`, `~/.codex/`)
2. Directory-scoped profile overlay (SPE-74, not yet implemented)
3. Project (`<repo>/.claude/`)
4. Local/private overrides (untracked)
