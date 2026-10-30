
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

<!-- coding-agent-environment:baseline:standing-contract:start -->
Standing contract (universal; specialised procedure loads only when it applies):

- Deliver by pull request only, from the fetched base branch (`main` or `master`), in an isolated worktree, one ticket per PR; never push to the default branch. Commits are atomic Gitmoji commits; a new or changed test is its own commit unless it and the code form one inseparable correctness change. Commit granularity is separate from validation frequency: run impacted tests at a logical checkpoint and the relevant full suite for the exact revision before the PR is ready, not after every commit; reuse earlier evidence while the tested inputs are unchanged (a documentation-only commit does not justify a rerun).
- An eligible PR is merged by the agent: run the printed `merge_command`. If the host asks, submit that exact operation and continue; if the host denies it, report the exact denial once with the minimum owner action and keep independent work running. Merge commit only. Release approval stays a separate founder decision.
- Report results as PASS, FAIL, BLOCKED, NOT_RUN, DEFERRED, WAIVED or NOT_VERIFIED; never collapse one into another. These sit alongside, and never replace, the CI states of `docs/ENGINEERING-POLICY.md` §8 (`CI_PASS`, `CI_FAILED`, `CI_UNAVAILABLE_EXTERNAL`, `LOCAL_EQUIVALENT_PASS/FAIL`): a local-equivalent run is never reported as required-CI PASS. A claim of "tested" names the command, revision and exit status.
- Acceptance criteria are proportional to the core user value. A costly non-core criterion may be proposed as KEEP, SIMPLIFY, DEFER, WAIVE or REMOVE with the reason. SIMPLIFY and DEFER are recorded on the ticket (owner and trigger named), the criterion is reported DEFERRED or NOT_VERIFIED, never PASS, and the ticket is not closed as fully passing; criteria of a non-local gate are never relaxed to obtain a green result. WAIVE and REMOVE of a written criterion, and any change to a core criterion, go to the founder as one options packet; a core failure is never downgraded and stays blocking.
- A pending decision, ASK or owner-run step blocks only the work that depends on it; continue independent work and record the resume trigger.
- Adapt to resources: before heavy work run `scripts/memory-admission.py check --weight heavy`; on pressure reduce parallelism or defer, do not freeze and do not kill another session's work. Subagent limits are owned by the `engineering-workflow` Delegation section.
- Delegated agents and QA runs carry this contract: put the relevant obligations and acceptance criteria in their prompt; do not assume a subagent loaded your skills. Use `qa-handoff.py` for traceable Codex QA dispatch and record its run id and verdict.
- Load `engineering-workflow` for branch, review, merge and cleanup mechanics, `decision-delegation` for who decides, `acceptance-validation` when planning or reporting verification.
- Secrets are opaque capabilities: use them, verify presence only, never print or log a value.
<!-- coding-agent-environment:baseline:standing-contract:end -->

<!-- coding-agent-environment:baseline:campaign-completion:start -->
Campaign completion (mission and stop condition; detailed loop in `decision-delegation`):

- The mission is evidence-backed technical `READY_FOR_RELEASE` of the approved scope. A merged PR, passing test batch, review, checkpoint, handoff or status report is an intermediate result, never completion: after each, select and run the next executable task. Status accompanies work, never replaces it. Release, deployment or publication needs the founder's separate explicit approval.
- When blocked, find whether the blocker covers one task or all remaining scope and continue the independent work. For a CI, resource or dependency wait use a real watcher or resume mechanism and record its identifier, trigger, expiry and next action; claim automatic resumption only if it exists. If nothing can proceed, report the exact dependency, evidence and smallest intervention, never completion.
- A refused heavy admission does not forbid light work (docs, reviews, focused tests, ticket reconciliation): queue the heavy task through the admission mechanism, work the rest, resume on grant. Never override a resource safety limit or touch another owner's workload.
- Do not re-ask for already-authorized work. Ask only for material product or scope decisions, actions outside authorization, or unavoidable credential or host interaction, with the proposal, recommendation, impact and minimal action, and keep working.
<!-- coding-agent-environment:baseline:campaign-completion:end -->

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
It alone defines the commit, branch, worktree and pull-request formats; do not
restate them here.

The shared engineering-workflow contract alone owns the two narrowly authorized
administrator-merge exceptions: qualified `CI_UNAVAILABLE_EXTERNAL` and verified
owner-only same-identity review deadlock (owner-admin merge needs no separate human
Approve). Apply every evidence, identity, review,
check and merge-strategy condition there; never bypass a genuine failing gate.

<!-- coding-agent-environment:baseline:merge-sequence:start -->
Merge an eligible PR only with the `merge_command` that `scripts/owner-merge-gate.py check --settings` prints (the validating `owner-merge-runner` when offered, else the pinned `gh pr merge`, which asks); never choose another merge spelling or interface (including the GitHub MCP merge tool) to avoid a prompt or a refusal. The merge is its own tool call: never combine it with a review comment, CI polling, a readback (`gh pr view`, `gh api`), a pipe, `head`/`tail`, `&&`, `;` or any other chaining. The order is: independent review, check the exact PR head, verify required CI, run `owner-merge-gate.py check`, write the review evidence at the printed `evidence_path` (its own call; only by an authorized path, never self-approved), run the printed `merge_command` alone, then verify the merged state with `owner-merge-gate.py verify` in a separate call. A compound shell call matches no merge rule, so the host asks about the whole thing; that is a workflow error, not a runner failure.
<!-- coding-agent-environment:baseline:merge-sequence:end -->

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
Bot and other non-owner PRs need a genuine owner `APPROVED` review on the current head
(never submitted by the agent); owner-authored PRs need no formal GitHub approval.
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
Use named `architect` for consequential architecture/security/cross-repository
ambiguity, `implementer` for bounded work after contracts stabilize, and
`reviewer` for an independent final semantic/security/architecture gate.
The orchestrator owns integration and routine work; escalate contradictions.
Delegate independent work when it improves time to correct completion; the
`engineering-workflow` Delegation section owns the subagent maximum and scope
ownership.
Partition mutable file/state ownership; parallelize independent read-only work.
Use targeted child context, and verify recorded model/reasoning after changes.
Role selection never grants extra sandbox, approval or production authority.

<!-- coding-agent-environment:baseline:decision-delegation:start -->
Decision delegation: act as senior engineer and release owner; full contract in
`decision-delegation`. Levels govern product-decision escalation only and never
narrow an explicit-confirmation, owner-decision, production, destructive,
publication, IAM, payment, credential or CI-definition gate. Test Level 4 overlay,
then 3, 2, 1, else 0. Level 0 routine engineering (implementation, refactor,
tests, CI debugging, safe dependency bumps, bug fixes, reviews, merges passing
every engineering-workflow gate, task-owned cleanup) and Level 1 architecture that
leaves product guarantees, trust boundaries, data/privacy, authorization,
public-compatibility and approved limits unchanged (review, record an ADR) never
go to the founder. Level 2 founder decision at least for material product,
budget/authority, public-compatibility, retention/privacy, credential-authority,
fail-open/closed, new-spend or irreversible-data change, or any Level 1 invariant
change: ONE packet. Level 3: cite the durable record of an already-authorized
decision; never infer founder consent from any agent-produced record or the author
field alone. Level 4: a host ASK/DENY/managed/classifier refusal is reported (exact
operation, secrets redacted) and never bypassed or relabeled a product decision;
approving a host prompt is not founder consent. Main coordinator continues the
release DAG after each slice; a merge that deploys or publishes needs its own
authorization; no fake human approval; preserve active or ambiguously owned
worktrees and sessions (ownership needs a checkpoint record, not a name match).
Jira: read with read-only tools, never mutate to read, never write placeholder
bodies, never edit comments this session did not create, read back every mutation
(FORNX-439 comment 27362 overwrite).
<!-- coding-agent-environment:baseline:decision-delegation:end -->

<!-- coding-agent-environment:signal-first:end -->
