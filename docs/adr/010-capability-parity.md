# ADR-0010: Capability parity is a contract, not identical hooks

## Status

Accepted (SPE-34)

## Context

Claude Code and Codex expose overlapping capabilities through different
native mechanisms. Treating one client's settings, plugin, MCP, profile, or
hook syntax as portable would create false parity claims and encourage
unbounded configuration copying. The public repository also cannot contain
organization, account, machine, or credential metadata.

This decision extends the global shared-skill and native directory-profile
work recorded across SPE-84 through SPE-89.

## Decision

Use [CAPABILITIES.md](../CAPABILITIES.md) as the canonical public contract.
The private capability inventory records actual live state; the repository
records generic desired state and adapter rules. Claude and Codex each get a
thin adapter that translates only supported concepts. Directory-scoped
profiles select the applicable overlay, while managed policy remains above
the profile and cannot be bypassed.

The contract has exactly six classifications: MUST_HAVE_PARITY, SHOULD_HAVE_PARITY,
HOST_SPECIFIC_NO_PARITY, SECURITY_RESTRICTED, CURRENTLY_BLOCKED, and ALREADY_EQUIVALENT. Every entry records the ten matrix fields
defined by the contract, including ownership, drift, lifecycle, scope, OAuth
lifecycle, read/write boundary, secret handling, verification, and host
exceptions.

Native plugins and client configuration are preferred. The installed Neon
plugin is observed as a broad capability with no fixed read-only research
grant. A research task may use an official read-only MCP or a
profile-disabled broad app connection; duplicate active connections are not a
parity strategy. Codex's baseline is CLI 0.160 with native plugins and
`config.toml`; an external `name.config.toml` is profile materialization, and
the old `mcp.json` path is not Codex source of truth.

Mutation adapters are gated per operation and target. This decision grants no
global authorization to write production data. Secrets remain runtime-only,
and fresh bounded verification is required after lifecycle changes.

## Consequences

- Parity claims can be reviewed against one provider-neutral inventory without
  pretending that native hooks are interchangeable.
- Live/repository drift is visible and actionable instead of being hidden by
  copied configuration.
- Shared semantic skills such as Requirement Zero, Codebase Zero, and
  evidence-first briefing remain reusable while provider instructions stay in
  their own adapters.
- Some capabilities will report an explicit blocked or restricted classification
  until a supported client mechanism, credential lifecycle, or explicit gate
  exists.
- Private account, company, machine, and case metadata remains outside the
  public repository.

## Rejected alternatives

- Copying Claude configuration into Codex, or vice versa, would imply native
  compatibility that has not been established.
- Treating presence or health as authorization would overstate safety.
- Adding broad duplicate MCP/app connections would increase ambiguity and
  secret exposure without improving capability coverage.
