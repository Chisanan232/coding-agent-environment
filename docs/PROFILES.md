# Directory-scoped coding-agent profiles

Every Claude Code / Codex session launched anywhere under a configured
filesystem subtree inherits that subtree's profile — independent of Git
repository boundaries, and working from arbitrarily deeply nested paths.
Example: `/work/company-a/**/*` and `/work/company-b/**/*` resolve to
different deterministic profiles.

## Marker grammar

`.coding-agent-profile` at the root of the subtree — a **pointer, nothing
else**. No endpoints, no secrets, no instructions in the marker itself:

```
# comments and blank lines are ignored
company-a
```

After stripping comment/blank lines, exactly one line must remain, matching
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. Anything else is **malformed** — see
Fallback behavior below.

## Resolution algorithm

`bin/coding-agent-profile resolve` walks from the **physical** cwd
(symlinks followed — matters for `/tmp` → `/private/tmp` and
temp-registered worktrees) upward toward `/`, looking for the marker.
**Nearest marker wins** — a marker closer to cwd shadows any ancestor
marker. Markers at `$HOME` or `/` are ignored by default (set
`CODING_AGENT_ALLOW_HOME_PROFILE=1` to change this). Depth-capped at 64
ancestors.

| Status | Meaning | Exit code |
|---|---|---|
| `ok` | Marker found, names a profile whose overlay directory exists | 0 |
| `none` | No marker found anywhere up to `/` | 0 |
| `malformed` | Marker exists but fails the grammar above | 3 |
| `unresolved` | Marker names a profile with no matching overlay directory | 4 |

`malformed`/`unresolved` never silently fall back to a grandparent marker or
apply a different profile — no profile is applied, and the condition is
loudly reported.

## Overlay layout

`$CODING_AGENT_PROFILE_DIR` (default `~/.coding-agent-profiles`), **never
committed to this public repo** — see `profiles/example-profile/` for the
generic template and `profiles/example-profile/README.md` for setup steps:

```
~/.coding-agent-profiles/<name>/
├── profile.env            # non-secret vars + a `required:` list of secret var NAMES
├── claude/
│   ├── settings.json      # -> ca-claude --settings
│   ├── mcp.json           # -> ca-claude --mcp-config
│   ├── strict-mcp         # presence (empty file) -> --strict-mcp-config
│   └── CLAUDE.md           # fallback only, see below
└── codex/
    ├── config.toml         # -> symlinked to ~/.codex/<name>.config.toml, used via `codex --profile <name>`
    └── AGENTS.md            # fallback only, see below
```

### Instruction channel is single-valued

For each tool, exactly one instruction source is active — never both:

1. **Preferred**: `<profile-root>/CLAUDE.md` (Claude) / `<profile-root>/AGENTS.md`
   (Codex) — loads natively via each tool's own ancestor walk. No flag needed.
2. **Fallback**: `<overlay>/claude/CLAUDE.md` / `<overlay>/codex/AGENTS.md` —
   for corporate trees you can't write into. Injected via
   `--append-system-prompt-file` (Claude).

If both exist, (1) wins and (2) is **not** injected — `ca-claude
--explain`/`coding-agent-profile explain` reports the suppression explicitly
so it's never a silent surprise.

## Inheritance tiers

The requirement ("every session inherits") and the constraint that this
system must never shadow the real `claude`/`codex` binaries turn out to be
in tension — there's no way to give a **plain** `claude`/`codex` invocation
full settings/MCP/status-line overlay without either an env var neither tool
exposes, or a PATH shim named `claude`/`codex` (which risks recursion and
opaque upgrades — explicitly disallowed). The resolved trade-off:

| Tier | Applies to | Carries |
|---|---|---|
| **A** | Any invocation, including plain `claude`/`codex` | Ancestor `CLAUDE.md`/`AGENTS.md` instructions, `direnv` env |
| **B** | `ca-claude` / `ca-codex` only | Settings, MCP config, strict-mcp, status-line (via settings), Codex profile toml |

Use `ca-claude`/`ca-codex` (from `scripts/profile-install.sh`'s
`~/.local/bin` symlinks) for full-fidelity profile inheritance.

## Claude Code integration

Validated against Claude Code 2.1.238. Settings cascade, low → high:

```
userSettings < projectSettings < localSettings < flagSettings (--settings) < policySettings (managed)
```

`ca-claude` passes exactly this closed flag set, drawn only from the
resolved profile — **never** arbitrary profile-controlled data:

- `--settings <overlay>/claude/settings.json` — refines the cascade, still
  beaten by any managed policy. This is *why* `--settings` and not
  `CLAUDE_CONFIG_DIR` or `--setting-sources` (the latter *removes* layers
  rather than refining them, and neither is used here).
- `--mcp-config <overlay>/claude/mcp.json`
- `--strict-mcp-config` — only when `<overlay>/claude/strict-mcp` exists
  (opt-in per profile; strict mode also drops the repo's own `.mcp.json`,
  so `--explain` states which mode is active)
- `--append-system-prompt-file <overlay>/claude/CLAUDE.md` — only as the
  single-valued instruction-channel fallback described above

There is no environment-variable equivalent of `--settings` — verified
negative against the installed binary's flag/env-var strings.

## Codex integration

Validated against Codex 0.147.0.

- `codex --profile <name>` reads `$CODEX_HOME/<name>.config.toml` —
  `--profile` cannot take an arbitrary path, so `scripts/profile-install.sh`
  materializes a symlink from the overlay into `$CODEX_HOME`.
- Precedence (validated via `codex debug prompt-input`, no model call):
  `-c` session flags > `--profile` toml > project `.codex/config.toml` >
  … > any managed/enterprise config.
- Codex's `AGENTS.md` ancestor walk is **git-root-bounded by default**.
  `ca-codex` passes `-c 'project_root_markers=[".coding-agent-profile"]'`
  to extend it to the profile root — **but only when a profile actually
  resolved**. Setting this globally with no marker present anywhere causes
  Codex to load **zero** project docs at all (verified) — this is why it's
  conditional per-invocation, never static `~/.codex/config.toml`.

### Where Codex can't match Claude

| Claude capability | Codex equivalent | Gap / fallback |
|---|---|---|
| `--settings <path>` — any path, per-invocation | none | Overlay must be symlinked into `$CODEX_HOME` at install time |
| `--mcp-config <file>` | none | `[mcp_servers.*]` inside the profile toml itself |
| Ancestor instruction walk out of the box | git-bounded by default | `-c project_root_markers=…`, conditional |
| Overlay is a plain file reference | Overlay is machine state (symlink) | Drift risk — `scripts/check.sh` checks symlink health |

## Corporate / private overlay boundary

Generic resolver machinery (`bin/`, `profiles/example-profile/`,
`scripts/profile-install.sh`) ships in this public repo. Real overlays —
company names, internal endpoints, actual MCP servers, actual instructions
— live only under `$CODING_AGENT_PROFILE_DIR`, which is gitignored and
never committed here. The public repo never depends on a private overlay
being present; with none, every resolution is simply `status=none`.

## Managed/corporate policy safety

Managed settings (`/Library/Application Support/ClaudeCode/managed-settings.json`
for Claude, an equivalent Codex managed-config path) sit **above**
`--settings`/`--profile` in both tools' validated precedence. This system
never attempts to read, modify, or bypass them — `--explain` reports their
presence purely informationally. See test case 8 in
`tests/test-profile-resolver.sh` for the mechanical assertion that emitted
launcher argv never contains a layer-removing or policy-bypassing flag.

## `--explain` diagnostic

```
coding-agent-profile explain [--json] [--cwd PATH]
```

Reports: logical + physical cwd, profile status/name/marker/root/overlay,
ancestor scan depth, shadowed/nested markers, which Claude config layer is
active (with suppression noted per the single-valued-channel rule), Codex
symlink health, managed-settings presence (informational), direnv/env
variable **readiness by name only** — never values — and the final
fallback behavior in plain language. Exit codes match the resolve table
above (0/0/3/4).

## Setup

**Deterministic path** (required):

```bash
cp -R profiles/example-profile ~/.coding-agent-profiles/<your-profile-name>
# edit the copy for your organization
./scripts/profile-install.sh
```

**Optional convenience prompt** — paste into Claude Code on a machine
that already has this repo cloned, as an aid, never the only path:

```text
I want to set up a directory-scoped coding-agent profile for
[organization/subtree name] rooted at [absolute path].

1. Copy profiles/example-profile to
   ~/.coding-agent-profiles/<profile-name> and help me fill in
   claude/settings.json, claude/mcp.json, and codex/config.toml for my
   organization — secrets as ${ENV_VAR} references only, never literal
   values.
2. Place a .coding-agent-profile marker (single line: the profile name)
   at the subtree root.
3. Run scripts/profile-install.sh.
4. Verify with `coding-agent-profile explain` from inside the subtree.

Do not print or ask me to paste any real credential values.
```

## Test matrix

`tests/test-profile-resolver.sh` — 13 cases, 31 assertions, pure bash +
python3, fully offline (no CLI/API calls): profile root, nested repo inside
a profile root, deeply nested child, no-marker fallback, nested/conflicting
markers (nearest wins), malformed (empty / multi-line / invalid-name
including a path-traversal attempt), unresolved (unknown profile name),
symlinked cwd, `$HOME`-marker exclusion, `ca-claude`/`ca-codex` argv with
and without a resolved profile, and the single-valued instruction channel.

## Explicitly out of scope

- **Windows.** This repo is macOS-first (`Brewfile`, `statusline.py` reads
  `/System/Volumes/Data`, managed-settings path assumed). Linux's managed
  path is reported by `--explain` where free, but untested/unclaimed.
- **Profile inheritance/composition** beyond nearest-marker-wins.
- **Secret storage or injection** — delegated entirely to `direnv` (already
  a declared dependency, see `docs/TOOLCHAIN.md`). This system reports
  readiness by variable name only.
- **Inferring organization identity** from git remotes, hostnames, or model
  reasoning — the marker is the only source of truth, by design.
- **Any PATH shim literally named `claude`/`codex`.**
- **`CLAUDE_CONFIG_DIR`-based isolation** — fragments credentials/session
  history across profiles, which is worse, not better, for this repo's
  stated secrets boundary.
