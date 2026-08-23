# example-profile

Generic template for a directory-scoped coding-agent profile overlay. Copy
this whole directory to `$CODING_AGENT_PROFILE_DIR/<your-profile-name>/`
(default `~/.coding-agent-profiles/<name>/`) — **do not** commit a real
overlay to this public repo; keep it in a private location. See
[docs/PROFILES.md](../../docs/PROFILES.md) for the full design.

## Layout

```
<name>/
├── profile.env       # optional: non-secret KEY=VALUE lines, plus a
│                      # `required:` section listing secret var NAMES
│                      # (never values) that --explain checks readiness for
├── claude/
│   ├── settings.json  # -> ca-claude --settings
│   ├── mcp.json       # -> ca-claude --mcp-config
│   ├── strict-mcp     # optional, empty file — presence enables --strict-mcp-config
│   └── CLAUDE.md       # fallback only — used via --append-system-prompt-file
│                        # ONLY if the profile root itself has no CLAUDE.md
│                        # (prefer a real CLAUDE.md at the profile root instead;
│                        # it loads natively via Claude's ancestor walk)
└── codex/
    ├── config.toml     # -> symlinked to ~/.codex/<name>.config.toml by
    │                    # scripts/profile-install.sh, applied via
    │                    # `codex --profile <name>`
    └── AGENTS.md        # same fallback caveat as claude/CLAUDE.md
```

## Setup

1. `cp -R profiles/example-profile ~/.coding-agent-profiles/company-a`
2. Edit the copied `claude/settings.json`, `claude/mcp.json`, `codex/config.toml`
   for your organization — secrets by `${ENV_VAR}` reference only, never
   literal values (same rule as this repo's own `.mcp.json`).
3. Prefer a real `CLAUDE.md`/`AGENTS.md` at the profile root
   (`/work/company-a/CLAUDE.md`) over the overlay fallback files — it's the
   native mechanism and doesn't need `ca-claude`/`ca-codex` to work.
4. Place `.coding-agent-profile` (single line: `company-a`) at
   `/work/company-a/.coding-agent-profile`.
5. Run `scripts/profile-install.sh` to link the launchers and materialize
   the Codex symlink.
6. Verify: `cd /work/company-a && coding-agent-profile --explain`.
