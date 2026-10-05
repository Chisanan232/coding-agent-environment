# Portable Codex routing

This repository provides three named Codex agent roles with portable
instructions and routing policy. The roles shape task selection and reasoning
effort; they do not grant tools, permissions, sandbox exceptions, or approval
authority. The root Codex model and default delegate remain owned by the
workstation and are intentionally not versioned here.

| Role | Intended use | Baseline model | Reasoning |
|---|---|---|---|
| `architect` | Unresolved architecture, contracts, trust/concurrency/persistence decisions, difficult root causes | `gpt-6-astra` | `high` |
| `implementer` | Bounded implementation after the contract and file ownership are stable | `gpt-6-luna` | `medium` |
| `reviewer` | Evidence-backed semantic and acceptance review | `gpt-6-astra` | `high` |

The semantic cost tiers are strongest/economical/strongest. The role TOML files
in `codex/agents/` are canonical. Codex's schema-validated configuration
contains only `agents.enabled`, the concurrency ceiling, and the three named
role descriptions/config-file references. Other Codex settings remain under
the workstation owner's control.

## Install and verify

The focused reconciler is available through the existing profile installer:

```bash
scripts/profile-install.sh --global --dry-run  # shared instruction policy
scripts/profile-install.sh --global
scripts/profile-install.sh --routing --dry-run
scripts/profile-install.sh --routing             # apply is the default
scripts/profile-install.sh --routing --check
scripts/sync-check.sh --routing
scripts/profile-install.sh --routing --remove
```

`--adopt-existing` can be added to report/apply when the current owned fields
and role files exactly match the canonical desired state. It does not adopt
differing existing state. Absent owned entries may be installed. The shared
instruction installer retires only the exact recognized legacy three-subagent
policy block; unknown legacy policy is an ownership conflict. The reconciler preserves all other config bytes,
including provider/auth settings, plugins, MCP, trust, Claude configuration,
and workstation root/default-delegate choices. It refuses symlinked state,
ambiguous dotted/inline TOML layouts, unowned differences, or unsupported
concurrent edits. Its byte reread detects changes before individual atomic
replacements; it is not an absolute compare-and-swap against writers that do
not cooperate with its lock. A multi-file failure leaves a prepared journal
for inspection and reports incomplete state; it never restores a stale full
config snapshot. Resolve the journal by inspecting the receipt and affected
files, then rerun check/apply/remove as appropriate.

Official baseline model validation applies only to the standard OpenAI
provider configuration. A custom provider needs a machine-local resolution
file passed as `--resolution FILE`. It must bind the exact provider fingerprint
to verified catalog model IDs for all three roles and include
`verification_evidence`; it is not committed. Provider/profile changes
invalidate that binding. The reconciler preserves model-role policy while
refusing check or launch when the provider binding no longer matches. It does
not guess model IDs for unknown providers.

`bin/ca-codex` is the guarded launcher when routing has an installation
receipt: it checks the effective provider/profile binding before launching.
Calling `codex` directly bypasses this preflight, so run
`scripts/sync-check.sh --routing` explicitly after changing provider, profile,
or routing configuration. Profile overlays and project routing overrides
that the checker cannot resolve fail closed pending separate verification.

The concurrency value `8` is a ceiling, not a target. Codex may run fewer
threads based on context, task scope, and available resources. Delegate only
independent work with a concrete time or quality benefit; avoid overlapping
ownership and never treat role selection or concurrency as authority to
weaken approval or sandbox rules.

## Versioned and live evidence

The role policy is versioned, while behavior depends on the installed Codex
binary and its actual model catalog. The implementation checks the installed
catalog's model/reasoning support and asks that binary to parse named roles
and concurrency keys using strict configuration before changing live state.
The version audit used [Codex 0.160's schema source at the tagged commit
79b1b666f2e8551f8abbbca34957227f67f3f553](https://github.com/openai/codex/tree/79b1b666f2e8551f8abbbca34957227f67f3f553).
CCSwitch host ownership was checked against [verified main commit
a33c156e5f6b2b0b0d9b0b9c574cd544237f354e: live/floor.rs](https://github.com/farion1231/cc-switch/blob/a33c156e5f6b2b0b0d9b0b9c574cd544237f354e/src-tauri/src/live/floor.rs)
and [live/project/codex.rs](https://github.com/farion1231/cc-switch/blob/a33c156e5f6b2b0b0d9b0b9c574cd544237f354e/src-tauri/src/live/project/codex.rs).
Those files identify the finite top-level model/provider/reasoning fields and
`agents.default_subagent_model`/`agents.default_subagent_reasoning_effort` as
CCSwitch-owned, while preserving named agent tables and concurrency.

The earlier repository audit covered only root `gpt-5.6-luna` at medium effort
and Claude's architect routing. That does not establish Codex named-role,
concurrency, provider-binding, or current root-model behavior. Current
versioned role and installer evidence is recorded above and in the role files;
the installed-machine acceptance record passed on 2026-10-05 using Codex 0.160.0: refreshed native catalog,
strict schema parsing with malformed-role warning rejection, scoped adoption,
apply/check and guarded launch; recorded fresh parent and three child
turn-context values matched the intended mapping. The personal root was
`gpt-6.1-sol / medium`; that remains a workstation choice. Native concurrency
configuration accepted eight spawned-agent threads. Twenty synthetic routing
tests cover removal, partial recovery, provider switching, omitted provider
defaults, ownership conflicts, exact types and preservation. Existing profile
and full-parity regressions passed. Claude/auth hashes and unrelated config
and instruction state stayed unchanged. Actual CC-Switch UI execution on the
other workstation remains unverified; coexistence evidence is pinned upstream
code and synthetic config transitions.

Reproduce native proof with a new private directory outside the repository:

```bash
python3 scripts/verify-codex-routing.py --output-dir /tmp/codex-routing-proof-new
```

This starts a fresh native parent, uses named roles without model/effort
flags, and compares actual child `turn_context` records. Transcripts, thread
IDs, provider fingerprints, resolved models and receipts stay machine-local.
The script refuses an existing output directory. Native parser success alone
is insufficient: malformed roles can produce warnings and exit zero, which
installation rejects. CI uses disposable fake-client fixtures; it does not
spend model quota or pretend those fixtures prove live execution.

A custom resolution has this shape (obtain its fingerprint from the report;
use provider-specific models already verified through that provider):

```json
{
  "provider_fingerprint": "<local-provider-context-sha256>",
  "verification_evidence": "<actual provider/model verification reference>",
  "models": {"architect": "<strongest>", "implementer": "<economical>", "reviewer": "<strong>"}
}
```

Guarded launches reject unresolved routing/backend/project/profile overrides,
including OSS/remote backends and alternate working directories. Native
managed-policy layers and provider changes after preflight cannot be enforced
by this scoped installer; direct CLI/desktop launches bypass the wrapper.
Removal restores only receipt-owned routing fields/files from their recorded
predecessors against fresh reads; it leaves the separately managed shared
engineering instruction policy installed. Never restore a full config backup
for routing rollback. Run the fresh verifier again after host/provider changes.
