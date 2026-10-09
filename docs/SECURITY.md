# Security model

## Secrets boundary

Tracked config uses `${ENV_VAR}` placeholders only — never literal
credential values. Real secrets are supplied at runtime via environment
variables, loaded by `direnv` from an untracked `.envrc`/`secrets.env` (see
README's Secrets Management section) or a directory-scoped profile's
`profile.env` (see `docs/PROFILES.md` — non-secret values only; secret
readiness is reported by variable *name*, never value).

Never tracked, by policy and `.gitignore`: `.claude.json`, `auth.json`,
OAuth credentials (Keychain), session/history/cache state,
`~/.codex/config.toml`'s `[projects.*]` trust entries, real directory-scoped
profile overlays (`.coding-agent-profiles/`). Full list in
[docs/ALLOWLIST.md](ALLOWLIST.md).

## Layered secret scanning

1. **Local pre-commit** (`.pre-commit-config.yaml`, gitleaks) — scans staged
   changes before every commit. Install: `pre-commit install`.
2. **CI** (`.github/workflows/secret-scan.yml`) — gitleaks-action on every
   push to `main` and every PR, full history fetch (`fetch-depth: 0`).
3. **One-time full-history scan** — the repository was public before this
   explicit secrets-boundary model existed, so `.gitignore` only protects
   future commits, not history already pushed. Ran once during SPE-75:

   ```
   $ gitleaks detect --source . --log-opts="--all"
   38 commits scanned, ~485.56 KB
   no leaks found
   ```

   **Result: clean.** No rotation/redaction action was needed. If a future
   scan (local, CI, or another manual run) ever finds a real historical
   credential, the response is: **stop**, report the secret *type* and
   whether it appears live — never the value itself — and propose a
   rotation/redaction plan. Do not automatically rewrite public Git history.

4. **Allowlist** (`.gitleaks.toml`) — only intentional placeholder patterns
   (`${ENV_VAR}`, README's illustrative `sk-ant-...`/`ghp_...` examples,
   test fixtures under `tests/fixtures/`) are excluded from scanning. No
   real-looking secret is ever allowlisted.

## Public / private boundary

This repository is public and generic. It contains no company-internal
metadata, credentials, private endpoints, or confidential instructions.
Directory-scoped profile overlays (`docs/PROFILES.md`) — where real
organization names, MCP servers, and instructions would live — are
explicitly never committed here; only `profiles/example-profile/`'s generic
template ships.

## Managed/corporate policy

Directory-scoped profiles never bypass Claude Code's `policySettings`
(managed-settings.json) or Codex's equivalent managed-config layer — both
sit above any profile overlay in the validated precedence (see
`docs/PROFILES.md`). `scripts/check.sh`'s `check_settings_precedence`
audits that this remains true against the currently installed `claude`
binary, since it's observed behavior rather than a published contract.

## Permission governance (Auto Mode `allow`/`ask`/`deny`)

`.claude/settings.json#permissions` is this repo's own attack-surface
boundary, not just a convenience list. Full rationale, root-cause evidence,
and the 60→47 `ask`-rule history: [ADR-0012](adr/0012-autonomous-permission-governance.md).

**Baseline**: `defaultMode: "auto"` — Auto Mode's classifier, not
`bypassPermissions`. `ask` rules force a prompt even in Auto Mode; they
would be silently ineffective under `bypassPermissions` (`allow` rules lose
effect there too), so the two settings are not interchangeable — never
switch this repo's default to `bypassPermissions` to "fix" prompting.

**Mandatory `ask`-gated operations** (never move to `allow` without a
fresh, documented decision — not merely to reduce prompts):

| Category | Rules |
|---|---|
| Force-push, any branch | `git push --force*` / `-f*`, bare and RTK-rewritten forms |
| Destructive local history | `git reset --hard`, `git clean -f*` |
| Unattributable process kill | `kill -9`, `pkill`, `killall` |
| Repo/resource deletion via API | `gh repo delete`, `gh api … -X DELETE` / `--method DELETE` (both flag orderings) |
| Infrastructure mutation | `terraform apply` (can mutate/replace production without `destroy`) |
| Arbitrary cloud execution | `mcp__gcloud__run_gcloud_command` |
| Production monetization | `mcp__polar-production__execute_tool` |
| Managed-DB/storage deletion | every `mcp__neon__*delete*`/`disable_auth`/`reset_from_parent`/`*_credential`, every `mcp__cloudflare-bindings__*_delete` |
| Trust-boundary widening | `mcp__github__delete_file`, `create_repository`, `fork_repository`, `mcp__claude_ai_Google_Drive__share_file` |

**`deny` (no legitimate interactive use in this workflow)**: `terraform
destroy` (and `apply -destroy` variants), `rtk proxy` (documented
debug-only raw-execution bypass — see
[SPE-83](https://lightning-dust-mite.atlassian.net/browse/SPE-83)),
`gcloud secrets versions access`, `mcp__neon__get_connection_string` (its
return value *is* the secret — a settings-file rule cannot scope by
parameter, so the whole tool is denied).

**Known unresolved defect**: [SPE-83](https://lightning-dust-mite.atlassian.net/browse/SPE-83)
— RTK's own PreToolUse self-decision can match an `allow` rule at
`git <subcommand>` granularity (ignoring flags) and bypass a configured
`ask` rule for the same subcommand. This repo's mitigation is structural:
**no `allow` rule in `.claude/settings.json` matches `git push` in any
form.** Do not reintroduce one — here or in a private profile overlay —
without first re-verifying SPE-83's reproduction steps against the
installed RTK version.

**What a text-based permission rule cannot do** (classifier/`autoMode.environment`
territory instead, by design — not a gap to "fix" with more rules): judge
whether a `gh api` POST/PATCH/PUT targets content vs. settings; judge
whether a deletion is inside or outside the agent's authorized scope;
enforce a specific merge strategy (flags cannot be reliably
pattern-matched, since `*` spans arbitrary trailing text); distinguish a
transient classifier failure (fail-closed `429`/`automode-unavailable`,
expected and correct) from an actual policy decision.

## Repository visibility

This repo is PUBLIC (verified via `gh repo view`, unchanged across the
SPE-71 rename). Visibility changes are a consequential decision and are
never made without the repository owner's explicit request.
