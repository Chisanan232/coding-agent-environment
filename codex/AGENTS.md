
<!-- codebase-memory-mcp:start -->
# Codebase Knowledge Graph (codebase-memory-mcp)

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase.
ALWAYS prefer MCP graph tools over grep/glob/file-search for code discovery.

## Priority Order
1. `search_graph` — find functions, classes, routes, variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

## When to fall back to grep/glob
- Searching for string literals, error messages, config values
- Searching non-code files (Dockerfiles, shell scripts, configs)
- When MCP tools return insufficient results

## Examples
- Find a handler: `search_graph(name_pattern=".*OrderHandler.*")`
- Who calls it: `trace_path(function_name="OrderHandler", direction="inbound")`
- Read source: `get_code_snippet(qualified_name="pkg/orders.OrderHandler")`
<!-- codebase-memory-mcp:end -->

<!-- coding-agent-environment:signal-first:start -->
# Signal-first engineering

Create only what has a current need. Search before creating; reuse or extend
only matching semantic responsibilities, and reference canonical truth before
repeating it. Avoid speculative abstractions; deletion/consolidation can improve
an existing system.

Code is primary implementation truth. Prefer clear names, types and boundaries.
Comments/docstrings explain non-obvious why, constraints, invariants, safety,
compatibility or external limitations—not obvious mechanics. Docs and tickets
store durable decisions/outcomes without duplicating code or another source.

Communicate semantic outcomes, not file/command chronology. Support material
claims with evidence; keep inference and unknown/unverified state explicit.
Use $evidence-first-briefing for persistent/shared engineering records, preserving
decision-critical evidence, risks and action in roughly 10–30 seconds of reading.

Use canonical $requirement-zero for NEW unvalidated scope and $codebase-zero for
EXISTING artifact audits. Never reopen already-approved work through these skills.

Use $engineering-workflow for the engineering lifecycle: branch and worktree,
commits, validation, review, pull request, merge, Jira reconciliation, and cleanup.
Pull-request titles use
`[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>`.

The shared engineering-workflow contract alone owns the two narrowly authorized
administrator-merge exceptions: qualified `CI_UNAVAILABLE_EXTERNAL` and verified
owner-only same-identity review deadlock. Apply every evidence, identity, review,
check and merge-strategy condition there; never bypass a genuine failing gate.

Apply the installed engineering skills by their actual triggers: `ticket-intake`
for new unaccepted work, `ticket-pickup-check` before ticket implementation,
`task-decomposition` for approved outcome/dependency planning, and
`cross-repo-coordinator` for coordinated integration. Use the tracker's actual
supported states; an explicit instruction to execute accepted work is pickup
authorization unless a real owner/blocker conflict exists. Never redo completed
work or create a child issue for each routine step.

Use `architecture-design` for significant design decisions; routine work stays
on the main agent under cost-aware routing. Use `feature-implementation`,
`dev-impl-loop`, `test-design` and `acceptance-validation` for implementation and
observable acceptance. Resolve `engineering-runtime` before persisted state,
circuit/notes/gates or resume work; initialize its environment in each shell.
Use `workflow-resume` for interrupted work and `project-setup` for onboarding.

Choose the installed Python, TypeScript/Node or Go repair skill only when its
real tool fails. Read project config, reproduce the error, repair the cause,
review auto-fix diffs, run targeted then full relevant checks. Preserve public
types and runtime behavior; scoped suppressions need a documented real reason.
Tests cover public behavior, meaningful boundaries and regressions, not private
implementation. Never skip/delete a failing test to make a check green.

Use `code-review-prep`, `pr-readiness`, `pr-health-check`, `ci-failure-triage`,
`pr-feedback-response` and `post-merge-close` at their lifecycle boundaries.
Use `dependency-upgrade-review`/`bot-pr-maintainer` for bot updates: inspect scope,
compatibility/security and exact-head checks; bot rebase owns lockfile conflicts.
A genuine unrelated required-check failure still blocks merge.

Use `release-preparation`, `release-readiness` and `release-watch` for preparation
and observation. Never manually tag, publish, push release intent directly to the
base, alter active release CI or re-trigger a release without explicit authority.
Verify the exact run/ref and accessible artifacts before claiming publication.
Native scheduling requires an explicit recurring-task request; otherwise poll
the same authoritative finite job with bounded backoff. Healthy ongoing work is
a verified wait, not a blocker. Reuse existing simulators and active job handles.

Treat secrets as opaque capabilities: consume via supported client/stdin paths,
verify presence without values, and never print/log/copy credentials or OAuth.
Routine reversible work within an accepted non-production contract is autonomous;
new production, destructive, payment, IAM, security-boundary or paid-spend authority
is a separate owner decision. Tool availability never grants that authority.
Preserve unknown/uncommitted state and protective controls; do not force-push,
reset/clean destructively, publish or pipe remote content into a shell implicitly.
External notifications require authorization even when a connector is available.

Discover native plugins/apps/MCPs before use; do not infer authentication from
configuration. `codebase-memory` owns graph discovery. Public library-doc lookup
is available through the opt-in `engineering-reference` profile; use actual tool
discovery and record rate/auth unavailability. Native document/Google skills own
format-specific work. `pdf-operations`, `morning-brief` and `memory-handoff`
cover the audited additional procedures; unavailable sources remain explicit.
<!-- coding-agent-environment:signal-first:end -->
