# Capability contract

This document defines the public, provider-neutral contract for capability
parity across Claude Code and Codex. It describes what the repository can
declare and verify; it does not promise that the two clients expose identical
native hooks.

It extends the global shared-skill and native directory-profile decisions
recorded across SPE-84 through SPE-89.

## Canonical inventory and ownership

The private capability inventory is the canonical record of what is actually
installed, enabled, authenticated, and usable on a machine. It is an audit
output, not a replacement for desired state. The repository owns the generic
desired-state contract and adapter semantics. Claude and Codex adapters own
client-specific translation. A directory-scoped profile owns the selection of
an overlay for a filesystem subtree; it does not own provider policy or
secrets.

The inventory must distinguish live state from repository state. A repository
entry can be desired without being installed; a live entry can exist without
being declared. Presence, configuration, and health are separate observations:
presence says that an object was found, configuration says that its declared
shape was read, and health says that a bounded check succeeded. None of these
alone proves that a capability is authorized or safe for production use.

## Six capability classifications

Every inventory entry has exactly one classification:

1. **native** — provided directly by the client and managed through its
   supported interface.
2. **adapter** — translated by a client-specific adapter into an equivalent
   repository capability.
3. **fallback** — an official read-only or reduced-scope substitute used when
   native support is absent.
4. **profile** — selected by a directory-scoped profile and applied through a
   client's supported profile/configuration mechanism.
5. **managed** — controlled by an enterprise or host policy layer above the
   repository and therefore only observed, never overridden here.
6. **unavailable** — discovered or requested but not applicable, installed,
   authorized, or supported in the current environment.

These classifications describe capability provenance, not quality or
permission. Mutation adapters remain explicitly gated; no classification
globally authorizes writes to production systems.

## Capability matrix

The inventory records these ten fields for each capability. Empty values are
reported as unknown rather than inferred.

| Capability | Claude adapter | Codex adapter | Desired-state owner | Live vs repository drift | Install/apply/update/remove | Global vs repo scope | OAuth lifecycle | Read/write and production boundary | Secrets, verification, and host exceptions |
|---|---|---|---|---|---|---|---|---|---|
| Provider tool, skill, plugin, or profile | Native name or adapter mapping; no invented flags | Native name or adapter mapping; no invented flags | Repository contract, private inventory, or managed policy as applicable | Compare declared intent with observed live state; never call presence proof | Use the provider's supported lifecycle; removal requires an explicit target | Record whether global, directory-profile, or repository scoped | Record credential class, expiry/refresh owner, and revocation path; never store tokens | Default read-only; writes require an explicit adapter gate and a bounded target; production writes require separate authorization | `${ENV_VAR}` or platform secret store only; verify with a bounded health/readback check; record host exceptions without publishing account or machine data |

Provider-specific instructions stay in provider documentation and adapters.
This matrix is the shared contract and must not duplicate those instructions.

## Current parity boundary

The official Neon plugin is an installed capability, but its broad database
surface is not a fixed read-only production contract. Treat it as a provider
capability requiring explicit scope and verification for each use. When a
specific read-only operation is unavailable, the generic document fallback is
an official read-only MCP or a profile-disabled broad app connection. Do not
create duplicate active connections to compensate for a missing narrow
capability.

Claude uses its native plugin/skill and settings mechanisms. Codex uses its
native plugin/profile mechanisms and `config.toml`; an external
`name.config.toml` may be materialized for a profile. The old `mcp.json`
configuration path is not a source of truth for Codex. The CLI baseline is
0.160, and native plugins remain the preferred integration boundary.

Shared skills are reused by semantic responsibility, including
Requirement Zero, Codebase Zero, and evidence-first briefing. Reuse does not
imply identical invocation syntax or native hook compatibility.

## Verification contract

An adapter must report the observed source, scope, capability classification,
credential readiness by name only, and the exact bounded check performed. A
successful install or a healthy process proves only that observation; it does
not prove authorization, production safety, semantic parity, or complete
coverage. Fresh verification is required after applying, updating, removing,
or changing a profile. Host-specific exceptions belong in the private
inventory and must not be copied into this public repository.
