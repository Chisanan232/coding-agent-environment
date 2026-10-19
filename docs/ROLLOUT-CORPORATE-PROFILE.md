# Corporate workstation profile (separate packet, SPE-116)

Public template, placeholders only. Nothing here is deployed to, or transmitted from, any corporate machine. Never put
corporate hostnames, controls, documents or logs in this repository.

## Principle

On a verified corporate machine, work data leaves only to destinations inside the formally approved corporate trust
boundary (internal git, package registries, the company LLM proxy). Everything else is a hard deny, not an ask. The
approved internal destinations must stay usable. Do not infer a machine's trust from a username; an unverified or
mismatched profile fails closed.

## What this repository provides

- `docs/examples/workstation-profile-corporate.example.json`: template with `<placeholders>` for the fingerprint and approved hosts.
- `scripts/workstation-policy.py evaluate`: the decision oracle (test and design aid).
- `scripts/workstation-policy-hook.py`: a best-effort PreToolUse adapter for upload commands and security-surface edits.
- `docs/examples/candidate-settings-corporate.example.json`: the `autoMode` and ask candidate (same asks as personal plus a corporate environment line).

## What it does NOT enforce

The hook sees curl, wget, scp, rsync, sftp and nc uploads (including inside `bash -c`, `$()` and common wrappers). It does
not see git or gh traffic, package managers, interpreter-made requests (`python -c`, `node -e`), MCP tools, WebFetch,
redirects or file contents, and shell parsing is best-effort. A crashed or timed-out hook fails open unless the entry sets
`onFailure: "block"`. It is a guard rail, not a boundary.

## What real enforcement needs (organization-owned, not this repository)

Per the Claude Code documentation, deterministic controls live in managed settings and the sandbox:

- Managed settings (`managed-settings.json`, MDM or the console) outrank user and project settings. Relevant keys:
  `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, `disableBypassPermissionsMode`.
- Sandbox network limits for shell commands: `sandbox.network.allowedDomains` / `deniedDomains`, locked by
  `allowManagedDomainsOnly`; `sandbox.failIfUnavailable` makes a missing sandbox fatal. The sandbox covers Bash,
  PowerShell and Monitor only. Read, Edit, Write, WebFetch, MCP servers and hooks run outside it, so a managed
  `permissions` and MCP allowlist must cover them.

The corporate IT or security owner decides and deploys these through the corporate channel. Mapping this policy onto them is
an owner task: the approved-destination list becomes `allowedDomains`, and the policy hook becomes a managed hook.

## Rollout order (owner and corporate IT)

1. Corporate owner reviews the template and supplies the approved destination list privately.
2. On the corporate machine, outside this repository, create the private profile and pin it with `workstation-policy.py fingerprint`.
3. Test the candidate on a scratch settings copy with the matrix in `docs/ROLLOUT-PERSONAL-MAC.md` using the corporate
   profile (uploads to unapproved hosts must be denied; approved internal hosts, downloads and `ls` must have no opinion).
4. Deploy the settings and hook only through the organization's managed-settings channel, never by this repository.
5. Rollback: remove the managed entries through the same channel and confirm the effective settings with `claude doctor`.

Open owner decisions: whether raw-credential outbound inside the approved boundary stays denied (the implementation denies it
on the corporate profile), and whether the hook is installed as a managed hook with `onFailure: "block"`.
