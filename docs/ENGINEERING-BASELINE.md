# Engineering baseline

Single source of truth for the universal obligations that every Claude Code and Codex
session must carry. Each `block` below is rendered **verbatim** by
`scripts/build-baseline.py` between the matching
`coding-agent-environment:baseline:<name>:start/end` markers in `.claude/CLAUDE.md` and
`codex/AGENTS.md`. Edit blocks here, run `python3 scripts/build-baseline.py write`, never
edit between the markers in a host file (`check` and CI fail on drift).

Everything in a block is advisory text the model reads, except what
`docs/ENGINEERING-POLICY.md` §3 lists as enforced. A block does not implement scheduling,
dispatch, enforcement or recovery; it points at the reviewed tool that does.
Specialised procedure lives in skills and loads only when it applies
(`engineering-workflow`, `decision-delegation`, `acceptance-validation`).
Size budget: all blocks together ≤ 8 KiB, checked by the build script.

<!-- block:standing-contract -->
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
<!-- /block:standing-contract -->

<!-- block:campaign-completion -->
Campaign completion (mission and stop condition; detailed loop in `decision-delegation`):

- The mission is evidence-backed technical `READY_FOR_RELEASE` of the approved scope. A merged PR, passing test batch, review, checkpoint, handoff or status report is an intermediate result, never completion: after each, select and run the next executable task. Status accompanies work, never replaces it. Release, deployment or publication needs the founder's separate explicit approval.
- When blocked, find whether the blocker covers one task or all remaining scope and continue the independent work. For a CI, resource or dependency wait use a real watcher or resume mechanism and record its identifier, trigger, expiry and next action; claim automatic resumption only if it exists. If nothing can proceed, report the exact dependency, evidence and smallest intervention, never completion.
- A refused heavy admission does not forbid light work (docs, reviews, focused tests, ticket reconciliation): queue the heavy task through the admission mechanism, work the rest, resume on grant. Never override a resource safety limit or touch another owner's workload.
- Do not re-ask for already-authorized work. Ask only for material product or scope decisions, actions outside authorization, or unavoidable credential or host interaction, with the proposal, recommendation, impact and minimal action, and keep working.
<!-- /block:campaign-completion -->

<!-- block:merge-sequence -->
Merge an eligible PR only with the `merge_command` that `scripts/owner-merge-gate.py check --settings` prints (the validating `owner-merge-runner` when offered, else the pinned `gh pr merge`, which asks); never choose another merge spelling or interface (including the GitHub MCP merge tool) to avoid a prompt or a refusal. The merge is its own tool call: never combine it with a review comment, CI polling, a readback (`gh pr view`, `gh api`), a pipe, `head`/`tail`, `&&`, `;` or any other chaining. The order is: independent review, check the exact PR head, verify required CI, run `owner-merge-gate.py check`, write the review evidence at the printed `evidence_path` (its own call; only by an authorized path, never self-approved), run the printed `merge_command` alone, then verify the merged state with `owner-merge-gate.py verify` in a separate call. A compound shell call matches no merge rule, so the host asks about the whole thing; that is a workflow error, not a runner failure.
<!-- /block:merge-sequence -->

<!-- block:decision-delegation -->
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
<!-- /block:decision-delegation -->
