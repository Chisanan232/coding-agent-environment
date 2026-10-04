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

1. **MUST_HAVE_PARITY** — a required semantic capability needs equivalent verified host access.
2. **SHOULD_HAVE_PARITY** — useful current capability whose absence does not block the current work.
3. **HOST_SPECIFIC_NO_PARITY** — native host behavior intentionally has no identical implementation.
4. **SECURITY_RESTRICTED** — broader access is intentionally gated, even if another host has it configured.
5. **CURRENTLY_BLOCKED** — a verified missing credential, permission or runtime prevents the capability.
6. **ALREADY_EQUIVALENT** — verified semantic capability already exists through either native tools or shared CLI/API.

Classification does not grant permission. Record installed, configured, healthy and action-verified observations separately. Mutation and production operations retain explicit gates.

## Capability matrix

Each capability row records all ten dimensions, with unknowns explicit:

| Dimension | Required record |
|---|---|
| 1 | Claude-side implementation |
| 2 | Codex-side current implementation |
| 3 | Whether semantic capability is equivalent, with evidence |
| 4 | Configuration and authentication differences |
| 5 | Read, write and destructive scope |
| 6 | Recommended Codex-native solution: official plugin, native MCP, shared skill, native hook, CLI/API or intentionally no equivalent |
| 7 | Security implications |
| 8 | Migration/apply strategy |
| 9 | Fresh-session validation method |
| 10 | Drift-detection ownership |

Canonical shared skill bodies are reused; host adapters contain only installation/configuration differences. The private inventory is the actual capability-level matrix, not one row per config file.

## Current parity boundary

The official Neon plugin is an installed capability, but its broad database
surface is not a fixed read-only research grant. Treat it as a provider
capability requiring explicit scope and verification for each use. When a
specific read-only operation is unavailable, the research fallback is
an official read-only MCP or a profile-disabled broad app connection. Do not
create duplicate active connections to compensate for a missing narrow
capability.

Claude uses its native plugin/skill and settings mechanisms. Codex uses its
native plugin/profile mechanisms and `config.toml`; an external
`name.config.toml` may be materialized for a profile. The old `mcp.json`
configuration path is not a source of truth for Codex. The CLI baseline is
0.160, and native plugins remain the preferred integration boundary.

Shared skills are reused by semantic responsibility, including
Requirement Zero, Codebase Zero, evidence-first briefing, and the engineering
workflow. The workflow has one host-neutral source: Claude uses thin native
adapters, while the managed Codex installer materializes the exact same skill
and adds a concise trigger to its owned instruction block. Reuse does not imply
identical invocation syntax or native hook compatibility.

## Verification contract

An adapter must report the observed source, scope, capability classification,
credential readiness by name only, and the exact bounded check performed. A
successful install or a healthy process proves only that observation; it does
not prove authorization, production safety, semantic parity, or complete
coverage. Fresh verification is required after applying, updating, removing,
or changing a profile. Host-specific exceptions belong in the private
inventory and must not be copied into this public repository.

## Desired state and lifecycle

The capability maintainer owns the public adapter and desired-state profile; account/project owners own the private inventory and scope approvals. The existing profile installer owns only explicitly named artifacts. Apply and update preserve unrelated user config, generated state and OAuth credentials. Dry-run reports planned changes; check reports drift without applying; removal refuses drift or unknown ownership. Backups and private runtime inventories stay outside Git. User-global plugins supply reusable skills; opt-in directory/native profiles narrow active tool access. Repository configuration cannot weaken managed host policy.

OAuth belongs to the supported client credential store, never a repository file or copied token. Install/configure precedes login; expiration or a scope change requires refresh or supported logout/re-authorization, followed by a fresh verification. Removing a profile does not silently revoke shared account access; deliberate credential revocation is a separate operation.

Read-only research, authorized mutation and destructive/production operations are distinct profiles or gates. No broad mutation profile is installed merely for symmetry. Fresh verification must show tool discovery, identity, one bounded actual read, and absence/refusal of excluded write/secret tools. Unavailable and host-specific capabilities remain explicit inventory rows; they do not count as successful parity.
