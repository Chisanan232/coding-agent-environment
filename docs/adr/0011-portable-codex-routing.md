# ADR-0011: Portable named Codex roles with workstation-owned defaults

## Status

Accepted; implemented and native-verified on Codex 0.160.0 (2026-10-05).

## Context

The prior repository audit established only a root `gpt-5.6-luna` medium
default and Claude architect routing. It did not prove Codex named-agent
support, concurrency configuration, or current workstation model defaults.
Codex and host applications can also have independent ownership of overlapping
settings. Replacing a complete live config would risk overwriting provider,
authentication, plugin, MCP, trust, or other workstation state.

## Decision

- Version the `architect`, `implementer`, and `reviewer` role files with
  baseline models `gpt-6-astra`, `gpt-6-luna`, and `gpt-6-astra`, at high,
  medium, and high reasoning effort respectively.
- Keep the workstation root model and default delegate live-only. Role
  selection carries no additional tool, approval, or sandbox authority.
- Reconcile only `agents.enabled`,
  `agents.max_concurrent_threads_per_session = 8`, and the named role
  description/config-file references. Preserve all unowned configuration.
- Validate against the installed Codex catalog and strict native config
  parser before mutation. Nonstandard providers require a machine-local,
  provider-fingerprint-bound resolution with verification evidence.
- Use `ca-codex` to check an installed receipt's effective provider/profile
  binding before launch. Plain `codex` bypasses the wrapper and requires an
  explicit routing check after relevant configuration changes.
- Treat eight as a concurrency ceiling. Delegate only independent work with a
  concrete benefit, keep ownership disjoint, and never use routing to upgrade
  authority.
- Record partial multi-file failures as incomplete through a journal; do not
  restore stale whole-file snapshots. Individual atomic replacements plus
  optimistic hash rereads detect many concurrent changes but do not provide
  absolute compare-and-swap against noncooperating writers.

## Evidence and limits

The schema audit is pinned to [Codex 0.160 source commit
79b1b666f2e8551f8abbbca34957227f67f3f553](https://github.com/openai/codex/tree/79b1b666f2e8551f8abbbca34957227f67f3f553).
CCSwitch ownership was inspected at [commit
a33c156e5f6b2b0b0d9b0b9c574cd544237f354e, `floor.rs`](https://github.com/farion1231/cc-switch/blob/a33c156e5f6b2b0b0d9b0b9c574cd544237f354e/src-tauri/src/live/floor.rs)
and [`codex.rs`](https://github.com/farion1231/cc-switch/blob/a33c156e5f6b2b0b0d9b0b9c574cd544237f354e/src-tauri/src/live/project/codex.rs).
They document finite top-level model/provider/reasoning ownership and two
default-subagent fields while named role tables and concurrency remain
preserved. The reconciler's runtime checks are version-sensitive. Installed-machine acceptance passed using a fresh parent and the three
named children: actual recorded models and reasoning matched the baseline.
Twenty routing cases and existing parity/profile regressions passed. See
[the verification procedure](../CODEX-ROUTING.md) for scope and limitations.

## Consequences

This keeps portable delegation policy reviewable while leaving workstation
defaults, provider choices, and unrelated live state under their existing
owners. Unknown providers require explicit local evidence. Native launches
outside `ca-codex` do not receive wrapper preflight, and must be checked
explicitly. Partial application can require operator inspection of the
prepared receipt and affected files.
