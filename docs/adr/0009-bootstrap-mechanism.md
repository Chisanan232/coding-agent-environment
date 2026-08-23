# ADR-0009: Bootstrap/apply mechanism — plain scripts, not a config-management tool

## Status
Accepted (SPE-71/SPE-75)

## Context
SPE-71 asked to evaluate whether a tool like chezmoi would materially
improve portability over simple repository-managed scripts.

## Decision
Kept plain scripts (`scripts/install.sh`, `scripts/check.sh`,
`scripts/profile-install.sh`, `scripts/sync-check.sh`) rather than
adopting a dedicated dotfiles/config-management tool. The repo's install
step is a handful of `cp` commands plus a symlink-based profile installer
— genuinely simple enough that a templating/application layer would add
indirection without solving a real problem this repo has. `mise`/`brew
bundle` were adopted specifically for CLI *version* management
(ADR-0006), which they're built for; nothing here needed chezmoi's
templating or encrypted-secret features.

## Consequences
- Bootstrap is transparent — reading `scripts/install.sh` tells you
  exactly what happens, no templating language to learn.
- If a real cross-machine templating need emerges later (e.g. many
  machine-specific variants of the same file), this decision should be
  revisited rather than silently worked around with more `cp` commands.
- Directory-scoped profiles (ADR-0007) reuse this same plain-script
  philosophy — `scripts/profile-install.sh` symlinks and reports, it
  doesn't templating-engine anything.
