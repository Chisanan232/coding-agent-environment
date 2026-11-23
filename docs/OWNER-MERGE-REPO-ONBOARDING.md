# Owner-merge onboarding matrix and the repeated-prompt root cause (SPE-118)

The repository matrix was measured 2026-10-23 with read-only `gh api` calls against main `c97fc85`. **No repository is opted in by this document.** The runner allowlist
(`~/.claude/owner-merge-runner-allowlist.json`) is owner-controlled; as read on 2026-11-16 it holds seven entries: `Chisanan232/coding-agent-environment`, a disposable canary, `horonomy/fornax-core`, `horonomy/libra-governor`, `horonomy/circinus`, `horonomy/local-agent` and `AI-agent-assembly/agent-assembly`. This document opts nothing in; each entry was added by the owner. Repositories with evidence files but no entry (`horonomy/fornax-cloud`, `ai-agent-assembly/official-website`) merge through the pinned `gh pr merge` (ASK), not the runner.

## Why a Libra Governor owner PR (#205) still showed "Allow once"

Three candidate causes, each sufficient on its own. **The #205 transcript was not inspected;** cause 1 is the founder's description of the call, and causes 2 and 3 are inferred from gate logic and from the host denial observed on a different PR (this repository's own coordinated PR), not observed on #205. Only the first is fixable by an ordinary engineering change.

| # | Cause | Fixable here? |
|---|---|---|
| 1 | Per the founder's report, the merge ran inside one compound shell call (`gh pr comment`, `gh pr merge`, `gh pr view`, `gh api`). No allow rule matches a compound command, so the host asks about the whole call. | **Partly**: instruction text only (`.claude/CLAUDE.md`, `codex/AGENTS.md`, `engineering-workflow`); not enforced, and not yet observed on a live host. The ask rules remain the backstop. |
| 2 | `horonomy/libra-governor` is not in the runner allowlist, so the gate offers the pinned `gh pr merge` (ASK), never the runner; by gate logic even a clean single call would prompt (not host-observed). | No: allowlist entry is owner-only (below). |
| 3 | The runner needs valid head-bound review evidence, and on this repository's own PR the host denied the agent writing it (`[Self-Approval]`); the same would be expected elsewhere. | No: Design 1 needs an owner-side installation (`OWNER-MERGE-RECORDER-APPROVAL-PACKET.md`). |

Fixing 1 alone removes the avoidable prompt bundling (comment, polling and readback no longer ride on the merge). A routine eligible owner PR will still ask once until 2 and 3 are done by the owner.

## Per-repository matrix

| Repo | Visibility, branch | Required checks enforced by GitHub | Candidate checks (last merged PR) | Server-side protection | CODEOWNERS | Runner today | Why ASK today |
|---|---|---|---|---|---|---|---|
| `horonomy/libra-governor` | public org, `main` | **none** (branch not protected, no rules) | CodeQL, SonarCloud Code Analysis, check-identity, cargo clippy, cargo fmt, cargo build & test (macos-latest, ubuntu-latest), Analyze (actions, python, rust) | none | none | not allowlisted | no allowlist entry; no GitHub-required checks, so the owner must name them |
| `horonomy/circinus` | **private** org, `main` | unavailable: HTTP 403 "Upgrade to GitHub Pro" | SonarCloud Code Analysis, test (ubuntu-latest), test (macos-latest) | unavailable on this plan, so runner mode `private-free-app-level` | present | not allowlisted | no entry; protection API forbidden by plan |
| `horonomy/fornax-core` | public org, `main` | `rust` | lint, rust, rust-macos-sandbox-e2e, security, changes, CodeQL | protection present, no rulesets | present | not allowlisted | no entry |
| `Chisanan232/glomeris` | public user, `main` | **none** (not protected) | test, xcodeproj-drift, app-icon-drift, no-policy-in-swift, cli-identity-is-reported and release jobs (names truncated in the read) | none | none | not allowlisted | no entry; release jobs mixed into the PR check set need a deliberate selection |
| `ai-agent-assembly/agent-assembly` | public org, `main` (a local `origin` fork also exists; use this repo explicitly) | `CI Success` | CI Success plus release and downstream jobs | protection present, no active rulesets (one exists, disabled) | present | not allowlisted | no entry; the home-root notes record squash merges for this repo (not re-verified against the repo setting), which would conflict with the runner's merge-commit-only call |

Findings that need an owner decision, not an agent guess:

- **Libra Governor and Glomeris have no GitHub-enforced required checks**, so there is no server-side backstop; the runner's required-checks list would be the only gate. Either enable protection (free on public repos) or accept the runner-only mode knowingly.
- **AASM squash-merges.** The runner only issues `gh pr merge --merge`; onboarding AASM needs an explicit owner decision about that mismatch, so it is deliberately not proposed.
- A check set must be picked per repo; the lists above are candidates observed on one merged PR, not a policy.

## Minimal owner-reviewable allowlist entries (proposal, not applied)

Same shape as the existing entry. **Warning:** the Libra template below sets `require_server_side_protection` false, which makes the runner's required-checks list the only gate (no GitHub backstop); enable protection first or accept that knowingly. Add an entry only after deciding the check names, and only for a repo whose merge method is a merge commit:

```json
"horonomy/libra-governor": {
  "required_checks": ["<owner chooses from the candidates above>"],
  "reviewers": ["runner-recorded-review"],
  "require_server_side_protection": false,
  "accept_bypass_actors": false,
  "max_evidence_age_hours": 24,
  "require_signed_evidence": false
}
```

`reviewers` stays the id the owner trusts: use `runner-recorded-review` only after the Design 1 runner is installed; until then it is a human or a separate reviewer's id.
For `horonomy/circinus` use `"require_server_side_protection": true` together with `"protection_capability": "private-free-app-level"`; the runner rejects that capability when protection is false (the live canary entry is the precedent).

## Protected integration base with a local gate while hosted CI is unavailable

Use when a product (for example a public repository such as `horonomy/local-agent`) must merge eligible owner PRs with no host "Allow once" prompt while the organisation's Actions budget is exhausted, so PR heads show an empty rollup and no required status check can be configured. Reusable for any product; nothing below names a repository in code.

**Why a dedicated base.** `release/*` (and `prod`, `production`, `releases`, `gh-pages`, `deploy`) are `DEPLOY_BASES` in `owner-merge-gate.py`: the gate never approves a merge into them. The pinned `gh pr merge` always prompts, because a permission `ask` outranks `allow` and hooks cannot allow past an `ask`; do not add blanket allow rules to get around it. The prompt-free path is the validating runner, and it needs a base that is not a deploy base: `integration/<name>`.

**Two opt-in switches (both default off, owner-controlled, per repository):**

- `public_ok` (boolean) in the repo entry of the owner trust file `local-first-ci.json`: skips only the `NOT_PRIVATE` refusal of the local-first basis for that repo. Every other local-first rule still applies (owner-authored class A, skip directive or no Actions run, `pinned_paths` / controlled script rules, owner-posted unedited `OWNER-MERGE-LOCAL-CI` record, freshness, independent review bound to the head). The check output carries `public_repo_local_first: true`. The standing admin-merge path keeps its own private-only check and is not widened. A non-boolean value or any unknown key makes the file invalid.
- `local_gate_only` (boolean) in the runner allowlist entry: replaces the "server-side required status checks" requirement with a stricter base-branch protection requirement. The entry is invalid (the runner refuses) unless `required_checks` is `[]`, `required_local_checks` is non-empty, `require_server_side_protection` is true, `protection_capability` is `server-enforced` and `local_equivalent_ci` is true. At run time the runner reads the exact PR base and refuses unless classic branch protection is readable and shows `enforce_admins` enabled, `allow_force_pushes` disabled and `allow_deletions` disabled, and no ruleset applying to the branch has a bypass actor (unreadable or malformed reads refuse). `accept_bypass_actors` does not waive any of these in this mode. The audit records `server_side: local-gate-protected-base` with the facts read.

**Owner steps, in order:**

1. Create `integration/<name>` from the tip of the version branch.
2. Protect it (`PUT /repos/OWNER/REPO/branches/integration/<name>/protection`): `enforce_admins: true`, `allow_force_pushes: false`, `allow_deletions: false`, no required status checks (`required_status_checks: null`), `required_pull_request_reviews: null`, `restrictions: null`. Add no ruleset bypass actors.
3. Add the owner version policy entry for the base so the gate accepts `integration/<name>` as a target.
4. Add the repo to `local-first-ci.json`: `checks` (exact `name` and `argv`, no shell), optional `env`, `max_age_hours`, `pinned_paths`, `not_covered` (what the local run does not cover, for example other OS legs), and `"public_ok": true`.
5. Add the allowlist entry:

```json
"OWNER/REPO": {
  "required_checks": [],
  "required_local_checks": ["<every check name from local-first-ci.json>"],
  "local_equivalent_ci": true,
  "local_gate_only": true,
  "require_server_side_protection": true,
  "protection_capability": "server-enforced",
  "accept_bypass_actors": false,
  "reviewers": ["<trusted reviewer id>"]
}
```

6. While no Actions run can exist, every PR head commit message ends with `[skip actions]` (the gate's skip directive; the local-first basis requires either that directive or proof that no Actions run exists for the head).

**Per PR (each step its own call, all at the same full head SHA):**

1. Independent review; post the PASS comment bound to the full head SHA (`owner-merge-gate.py evidence-line`, i.e. `OWNER-MERGE-REVIEW head=<sha> verdict=PASS blocking=0 reviewer=<id>`).
2. `owner-merge-local-ci.py record` in a DISPOSABLE detached worktree at the exact PR head (`git worktree add --detach <path> <head>`), run inside the heavy lane (`scripts/heavy-run` or the host's admission wrapper). The recorder forces `CARGO_TARGET_DIR=<worktree>/target`, so each recording builds a full per-worktree target; remove the worktree immediately after the record is posted (`git worktree remove --force <path>`), otherwise the per-worktree target-dir disk incident recurs (686 GB, see `/Users/bryant/CLAUDE.md`). Cap the load with the owner-configurable resource keys `CARGO_BUILD_JOBS`, `RUST_TEST_THREADS`, `NEXTEST_TEST_THREADS` (the only three the recorder admits besides the owner `env`; positive decimal 1 to 1024; the effective value is the minimum of the inherited and the configured one).
3. Post the printed `OWNER-MERGE-LOCAL-CI` line as a PR comment as the owner.
4. `owner-merge-local-ci.py evidence` to write the runner evidence file.
5. `owner-merge-gate.py check --repo R --pr N --owners O --settings ...`.
6. Run the printed `merge_command` alone (first with `--dry-run`), then `owner-merge-gate.py verify` in a separate call.

A changed head, missing or stale review or local evidence, evidence for another head, or a different base each refuse.

**Revert when hosted CI recovers:** restore the hosted `required_checks` in the allowlist entry; remove `required_local_checks`, `local_gate_only` and (if no longer wanted) `local_equivalent_ci`; remove `public_ok` and the repo's `local-first-ci.json` entry; re-add the required status check contexts to the branch protection; stop appending `[skip actions]`.

**Residual risks (accept knowingly):** the agent-written local evidence is the gate, so whoever can run the recorder and post comments as the owner can attest a pass; the checks are only those in `local-first-ci.json`, and `not_covered` stays uncovered. The server-side backstop is only admins-enforced, no force-push and no deletion: it does not require any check or review. The repository is public, so the gate decisions, records and logs' digests are visible to anyone.

## Acceptance still open

Not demonstrated, because each depends on the owner steps above: autonomous no-prompt merge on a real host with a recorded review, and the live stale-head, failed-check, missing-evidence and non-owner cases through a trusted executor on a real host. Existing evidence is in `OWNER-MERGE-ACCEPTANCE-LEDGER.md` (L1 and L3 canaries by the founder, unit and mutation tests). Unsupported or unavailable automatic authorization stays **ASK**.
