# Full behavioral parity audit (SPE-90)

This audit extends SPE-34 rather than changing its six external-capability
classes. The exhaustive behavioral inventory uses the seven SPE-90 dispositions
in [full-parity-inventory.json](full-parity-inventory.json). Each row owns one
identified surface and records source, semantic behavior, current/desired Codex,
security, verification, drift owner and migration. Container rows require their
field/section rows; a settings file is not a blanket authorization.

## Observation and proof boundary

Baseline `aac9976`: Claude Code 2.1.274, Codex CLI 0.160.0. Both binaries'
version/help were executed. Codex reports hooks, plugins, apps, multi_agent,
worktrees and goals stable. [Native hooks](https://learn.chatgpt.com/docs/hooks)
require exact-definition trust; supported command events include SessionStart,
Pre/PostToolUse, SubagentStart/Stop and Stop. Prompt/agent hook handlers are
skipped. [Claude plugin conversion](https://developers.openai.com/plugins/guides/submit-claude-plugin)
requires native adapters for host mechanics. No unknown hook is trusted here.

Tracked versus actual live state is separately observed in a private hash-only
snapshot. Auth and private account/project metadata are never committed.
Live Claude is stale versus SPE-33: engineering-workflow is missing, and old
PR/bot/project merge wording remains. Live-only opaque-secret and environment
policy must survive reconciliation; no full CLAUDE/settings replacement.
Live Codex has only two authored engineering skills, canonical subtraction,
native CBM session reminder, Libra hooks and native UI fields.

Fresh tools-disabled nonpersistent Claude execution exits 1 with account weekly
limit, reset October 8 at 02:00 Asia/Taipei. SPE-36 remains READY FOR QA; SPE-92
cannot attest two-host behavior until a successful fresh Claude matrix exists.
No quota purchase or subscription change. Presence/drift checks are not behavior
proof. The detailed gate must record PASS/FAIL/NOT_VERIFIED per required scenario.

## Skills, individually

| Claude skill | Disposition | Codex migration |
|---|---|---|
| acceptance-validation | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill acceptance-validation; engineering-runtime adapter for paths/state/config |
| bot-pr-maintainer | PORTABLE_SHARED_SOURCE | Native Agent Skill bot-pr-maintainer |
| ci-failure-triage | PORTABLE_SHARED_SOURCE | Native Agent Skill ci-failure-triage |
| code-review-prep | PORTABLE_SHARED_SOURCE | Native Agent Skill code-review-prep |
| codebase-memory | PORTABLE_SHARED_SOURCE | Native Agent Skill codebase-memory |
| cross-repo-coordinator | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill cross-repo-coordinator; engineering-runtime adapter for paths/state/config |
| dependency-upgrade-review | PORTABLE_SHARED_SOURCE | Native Agent Skill dependency-upgrade-review |
| dev-impl-loop | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill dev-impl-loop; engineering-runtime adapter for paths/state/config |
| engineering-workflow | ALREADY_EQUIVALENT | Native Agent Skill engineering-workflow |
| evidence-first-briefing | ALREADY_EQUIVALENT | Native Agent Skill evidence-first-briefing |
| feature-implementation | PORTABLE_SHARED_SOURCE | Native Agent Skill feature-implementation |
| go-golangci-fixing | PORTABLE_SHARED_SOURCE | Native Agent Skill go-golangci-fixing |
| go-vet-debugging | PORTABLE_SHARED_SOURCE | Native Agent Skill go-vet-debugging |
| node-precommit-repair | PORTABLE_SHARED_SOURCE | Native Agent Skill node-precommit-repair |
| post-merge-close | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill post-merge-close; engineering-runtime adapter for paths/state/config |
| pr-feedback-response | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill pr-feedback-response; engineering-runtime adapter for paths/state/config |
| pr-health-check | PORTABLE_SHARED_SOURCE | Native Agent Skill pr-health-check |
| pr-readiness | PORTABLE_SHARED_SOURCE | Native Agent Skill pr-readiness |
| project-setup | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill project-setup; engineering-runtime adapter for paths/state/config |
| python-mypy-debugging | PORTABLE_SHARED_SOURCE | Native Agent Skill python-mypy-debugging |
| python-precommit-repair | PORTABLE_SHARED_SOURCE | Native Agent Skill python-precommit-repair |
| python-pytest-failure-debugging | PORTABLE_SHARED_SOURCE | Native Agent Skill python-pytest-failure-debugging |
| python-ruff-fixing | PORTABLE_SHARED_SOURCE | Native Agent Skill python-ruff-fixing |
| release-preparation | PORTABLE_SHARED_SOURCE | Native Agent Skill release-preparation |
| release-readiness | PORTABLE_SHARED_SOURCE | Native Agent Skill release-readiness |
| release-watch | PORTABLE_SHARED_SOURCE | Native Agent Skill release-watch |
| task-decomposition | PORTABLE_SHARED_SOURCE | Native Agent Skill task-decomposition |
| test-design | PORTABLE_SHARED_SOURCE | Native Agent Skill test-design |
| ticket-intake | PORTABLE_SHARED_SOURCE | Native Agent Skill ticket-intake |
| ticket-pickup-check | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill ticket-pickup-check; engineering-runtime adapter for paths/state/config |
| typescript-eslint-fixing | PORTABLE_SHARED_SOURCE | Native Agent Skill typescript-eslint-fixing |
| typescript-tsc-debugging | PORTABLE_SHARED_SOURCE | Native Agent Skill typescript-tsc-debugging |
| workflow-resume | CODEX_NATIVE_ADAPTER_NEEDED | Native Agent Skill workflow-resume; engineering-runtime adapter for paths/state/config |

## Hooks and utility enforcement

The full-test, precommit, quality, audit, state, notes, decision and circuit scripts
are callable utilities, **not registered automatic hooks** in source/live settings.
SPE-91 supplies native skill helpers; it must not claim newly enforced timing.
Background/subagent observability uses native Codex handles and task progress;
Claude's custom renderer, disk/memory widgets and RTK command-rewrite invocation
remain intentional host mechanics. Graph-first reminders already exist.

## Implementation frontier

SPE-91 must install every portable skill with valid Agent Skills metadata, preserve
canonical bodies, expose safe state/notes/gates through a small host adapter,
make architecture/testing/release/safety routing discoverable, and add the
missing safe Context7 documentation capability. Managed drift must include every
required shared skill/resource, instruction block and owned capability profile.
No auth copying, permission widening, host-hook trust mutation or wholesale config
replacement. Live unknown state is preserved.

The live administrator-merge exceptions conflict with SPE-33's no-bypass contract.
This is a founder authority decision, explicitly CURRENTLY_BLOCKED, rather than
a silently dropped behavior. Independent work proceeds with the existing safer
contract; no administrator merge is attempted.

## Actual live-only surface

Nine synced skills are individually disposed in the inventory. Native documents,
spreadsheets, presentations, Pages, skill creation and Google connector workflows
serve the established format/safety invariants. PDF operations, daily brief and
memory handoff require shared procedures; the absent calendar connector is a
separate explicit CURRENTLY_BLOCKED input. Native scheduling is request-only.
Fornax and Libra already own versioned Codex adapters; existing notification
transport and trusted hooks stay preserved. Dodo payment mutation and private
local permission grants remain SECURITY_RESTRICTED.

## Current native registrations (not a behavior PASS)

| Event | Claude live | Codex live |
|---|---|---|
| SessionStart | CBM + Fornax | CBM in config.toml |
| PreToolUse | RTK + CBM + Fornax | No custom hook; graph policy/native tools |
| PostToolUse | Background + Fornax + Libra | Libra in hooks.json |
| SubagentStart/Stop | Background tracker | Native process state; no custom hook |
| UserPromptSubmit | CodeGraph + Fornax + Libra | Libra in hooks.json |
| Stop | Fornax + Libra | Libra in hooks.json |

Hook support does not imply registration or execution. Callable utility gates
stay unwired on both hosts. Native task observability supplies the portable
observation semantics; exact custom UI remains host-specific. Trust hashes stay
preserved. Fornax owns distinct rollout/notify transport; do not overwrite an
existing notify entry.

Canonical subtraction content lives in `~/.agents/skills/requirement-zero`
and `~/.agents/skills/codebase-zero`, not `.codex/skills`. Both match every
pinned digest in `codex/subtraction-skills.json`.
[SPE-89](https://lightning-dust-mite.atlassian.net/browse/SPE-89) and
[SPE-33](https://lightning-dust-mite.atlassian.net/browse/SPE-33) own existing
native discovery/behavior proof; the full matrix is still NOT_VERIFIED.
Synced-source hashes record provenance without private account/bucket paths.
Tests enforce the authored census; manual live inspection owns observations.
Neither pretends to be the two-host behavior gate.

## Portable implementation (SPE-91)

The manifest covers 38 individually routed authored skills, including native
architecture review and the live-only PDF/morning/memory procedures. Canonical
engineering-workflow/evidence-first bodies and external subtraction provenance
are reused unchanged. All shared copies are byte-checked; supporting scripts and
RTK reference are materialized from their existing sources. Runtime bootstrap
selects native context/config/state and prevents inherited Claude state or skip
flags from altering Codex gates. Helpers remain callable rather than claiming
new automatic hook registration. Host-private configuration remains user-owned.

The global apply/check covers every manifest body/resource and both native
profiles. Tests inject missing skill/source, unsafe resource symlink, unrelated
private state, native state overlap, open circuit, missing acceptance sentinel
and warning/strict outcomes. These prove apply and helper contracts, not model
behavior. Existing SPE-33 and SPE-84–89 regressions remain required.

The live administrator-merge exception conflicts with the completed SPE-33
contract and awaits a founder decision. Installation preserves that live policy;
no administrator merge is authorized by this migration. Fresh Claude quota and
missing calendar source remain explicit external blockers. SPE-92 requires
independent fresh two-host scenario evidence before a full-parity verdict.
