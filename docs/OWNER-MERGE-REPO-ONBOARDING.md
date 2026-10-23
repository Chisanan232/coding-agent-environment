# Owner-merge onboarding matrix and the repeated-prompt root cause (SPE-118)

Measured 2026-10-23 with read-only `gh api` calls against main `c97fc85`. **No repository is opted in by this document.** The runner allowlist
(`~/.claude/owner-merge-runner-allowlist.json`) is owner-controlled; today it holds `Chisanan232/coding-agent-environment` and a disposable canary only.

## Why a Libra Governor owner PR (#205) still showed "Allow once"

Three independent causes, each sufficient. Only the first is fixable by an ordinary engineering change.

| # | Cause | Fixable here? |
|---|---|---|
| 1 | The merge ran inside one compound shell call (`gh pr comment`, `gh pr merge`, `gh pr view`, `gh api`). No allow rule matches a compound command, so the host asks about the whole call. | **Yes**: the merge is its own call (rule added to `.claude/CLAUDE.md`, `codex/AGENTS.md`, `engineering-workflow`). |
| 2 | `horonomy/libra-governor` is not in the runner allowlist, so the gate offers the pinned `gh pr merge` (ASK), never the runner. Even a clean single call would prompt. | No: allowlist entry is owner-only (below). |
| 3 | The runner needs valid head-bound review evidence, and the host denied the agent writing it (`[Self-Approval]`). | No: Design 1 needs an owner-side installation (`OWNER-MERGE-RECORDER-APPROVAL-PACKET.md`). |

Fixing 1 alone removes the avoidable prompt bundling (comment, polling and readback no longer ride on the merge). A routine eligible owner PR will still ask once until 2 and 3 are done by the owner.

## Per-repository matrix

| Repo | Visibility, branch | Required checks enforced by GitHub | Candidate checks (last merged PR) | Server-side protection | CODEOWNERS | Runner today | Why ASK today |
|---|---|---|---|---|---|---|---|
| `horonomy/libra-governor` | public org, `main` | **none** (branch not protected, no rules) | CodeQL, SonarCloud Code Analysis, check-identity, cargo clippy, cargo fmt, cargo build & test (macos-latest, ubuntu-latest), Analyze (actions, python, rust) | none | none | not allowlisted | no allowlist entry; no GitHub-required checks, so the owner must name them |
| `horonomy/circinus` | **private** org, `main` | unavailable: HTTP 403 "Upgrade to GitHub Pro" | SonarCloud Code Analysis, test (ubuntu-latest), test (macos-latest) | unavailable on this plan, so runner mode `private-free-app-level` | present | not allowlisted | no entry; protection API forbidden by plan |
| `horonomy/fornax-core` | public org, `main` | `rust` | lint, rust, rust-macos-sandbox-e2e, security, changes, CodeQL | protection present, no rulesets | none | not allowlisted | no entry |
| `Chisanan232/glomeris` | public user, `main` | **none** (not protected) | test, xcodeproj-drift, app-icon-drift, no-policy-in-swift, cli-identity-is-reported and release jobs (names truncated in the read) | none | none | not allowlisted | no entry; release jobs mixed into the PR check set need a deliberate selection |
| `ai-agent-assembly/agent-assembly` | public org, `main` (a local `origin` fork also exists; use this repo explicitly) | `CI Success` | CI Success plus release and downstream jobs | protection present, no rulesets | present | not allowlisted | no entry; merges are squash in this repo's own policy, which conflicts with the runner's merge-commit-only call |

Findings that need an owner decision, not an agent guess:

- **Libra Governor and Glomeris have no GitHub-enforced required checks**, so there is no server-side backstop; the runner's required-checks list would be the only gate. Either enable protection (free on public repos) or accept the runner-only mode knowingly.
- **AASM squash-merges.** The runner only issues `gh pr merge --merge`; onboarding AASM needs an explicit owner decision about that mismatch, so it is deliberately not proposed.
- A check set must be picked per repo; the lists above are candidates observed on one merged PR, not a policy.

## Minimal owner-reviewable allowlist entries (proposal, not applied)

Same shape as the existing entry. Add one only after deciding the check names, and only for a repo whose merge method is a merge commit:

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
For `horonomy/circinus` also set `"protection_capability": "private-free-app-level"`.

## Acceptance still open

Not demonstrated, because each depends on the owner steps above: autonomous no-prompt merge on a real host with a recorded review, and the live stale-head, failed-check, missing-evidence and non-owner cases through a trusted executor on a real host. Existing evidence is in `OWNER-MERGE-ACCEPTANCE-LEDGER.md` (L1 and L3 canaries by the founder, unit and mutation tests). Unsupported or unavailable automatic authorization stays **ASK**.
