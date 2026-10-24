# Owner-merge onboarding matrix and the repeated-prompt root cause (SPE-118)

The repository matrix was measured 2026-10-23 with read-only `gh api` calls against main `c97fc85`. **No repository is opted in by this document.** The runner allowlist
(`~/.claude/owner-merge-runner-allowlist.json`) is owner-controlled; today it holds `Chisanan232/coding-agent-environment` and a disposable canary only.

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

## Acceptance still open

Not demonstrated, because each depends on the owner steps above: autonomous no-prompt merge on a real host with a recorded review, and the live stale-head, failed-check, missing-evidence and non-owner cases through a trusted executor on a real host. Existing evidence is in `OWNER-MERGE-ACCEPTANCE-LEDGER.md` (L1 and L3 canaries by the founder, unit and mutation tests). Unsupported or unavailable automatic authorization stays **ASK**.
