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

## Repository visibility

This repo is PUBLIC (verified via `gh repo view`, unchanged across the
SPE-71 rename). Visibility changes are a consequential decision and are
never made without the repository owner's explicit request.
