# Proposal: self-hosted CI fallback for server-protected repositories (SPE-171)

Status: PROPOSED, not deployed, nothing here is installed or registered. It changes no merge gate and no acceptance criterion. `FULL_MERGE_AUTONOMY_READY` stays NO until the owner provisions this and a real merge proves it.

## Problem (evidence)
On a repository whose branch protection requires GitHub Actions checks, GitHub refuses the head-pinned `gh pr merge --merge --match-head-commit` unless each required check really ran and passed (measured refusal text: "base branch policy prohibits the merge", `OWNER-MERGE-AUTONOMY.md` section 12). Local-equivalent evidence is accepted by the runner only where there is no server-side binding, and cannot move the server. Since about 2026-11-16 the horonomy org creates no check runs at all (Fornax, Circinus), so server-protected repositories there cannot merge unattended during the outage. This repository's own protection requires `gitleaks`, `isolated-validation` and `native-resource-wire`, each bound to the GitHub Actions app (app id 15368) with `enforce_admins` on (read 2026-11-19).

## Required-check compatibility
A job that runs on a self-hosted runner is still published by GitHub Actions under the same check name and app id, so the server-side binding, the gate's CI evidence and the runner's `required_checks` see an ordinary result and the report stays `CI_PASS` from a non-hosted executor. Nothing in the gate or runner changes. What must stay identical: the three job ids and names, the trigger set (no path filter on `pull_request`, as the workflow comment already requires), and the commands.

## Design
1. `runs-on` becomes selectable by a repository variable per job, defaulting to each job's current hosted value: `isolated-validation` `ubuntu-24.04` (capability-validation.yml:90), `native-resource-wire` `macos-15` (:191), and `gitleaks` `ubuntu-latest` (secret-scan.yml:10, which is unpinned and, per the workflow's own comment, resolves to 26.04). For example `runs-on: ${{ fromJSON(vars.CI_RUNS_ON_ISOLATED || '"ubuntu-24.04"') }}`; gitleaks would default to `ubuntu-latest`, not 24.04, so its default image does not change. The owner flips the variable during an outage; the fallback is a manual, recorded switch, not an automatic failover. This is a CI-definition change and goes through the CI-definition gate (owner confirmation) in its own PR.
2. Runners are ephemeral and just-in-time registered per job (`--ephemeral`, JIT config), so no runner outlives a job or carries state between pull requests.
3. `isolated-validation` runs in a container image matching its `ubuntu-24.04` pin, on a Linux VM or container runtime on the host; `gitleaks` is unpinned today, so which Linux image it should use on the fallback is an open choice (the safest is the same 24.04 image, accepting that it then differs from its hosted `ubuntu-latest`). The macOS job (`native-resource-wire`) runs natively under a dedicated unprivileged macOS user.

## Security boundaries
- **Fork pull requests must never reach a self-hosted runner.** This repository is public, so a self-hosted runner here is unsafe unless the job is restricted to same-repository branches, or fork workflows need approval and the runner is confined; the safe default is to enable the fallback only for private repositories. The horonomy repositories' visibility and plan are not verified here.
- No secrets in these jobs, `GITHUB_TOKEN` read-only (`capability-validation.yml` declares `permissions: contents: read`; `secret-scan.yml` declares no `permissions` block and no timeout, so the fallback PR must add both). The `gitleaks` job also runs the third-party `gitleaks/gitleaks-action@v2` with `GITHUB_TOKEN` and `fetch-depth: 0`; on a self-hosted runner that action and its token exposure need the same confinement as the rest, or that job stays on hosted runners, the runner user has no access to `~/.claude`, `~/.codex`, keychains, cloud credentials or other repositories' worktrees, and the work directory is wiped per job.
- The runner registration token and any GitHub App key are owner-held credentials; the agent never reads them (they are used only through a stdin or pipe path).
- Admission: runs go through the existing resource admission wrapper and disk gate (100 GiB floor, lane limits unchanged); a refused admission means the check waits, it does not run unsafely.

## Resource needs (what is and is not measured)
Measured wall times on hosted runners today: `isolated-validation` about 6 minutes, `native-resource-wire` about 1.5 minutes, `gitleaks` seconds. CPU, memory and disk use of these jobs on the owner's workstation are NOT measured; measure them once on a private copy before the owner decides, and compare to the admission limits.

## Maintenance
Runner agent updates, image drift against `ubuntu-24.04` (the pin exists because `ubuntu-latest` 26.04 failed a `visudo` check, SPE-174), host uptime, runner and token rotation, and one more place to keep the Rust and Python toolchains current. An always-on host is required, and a LaunchAgent install was denied earlier, so it needs the owner.

## Rollback
Set `CI_RUNS_ON` back to the hosted value (or delete the variable), delete the runner registration, remove the runner user and image. The workflow's default is hosted, so rollback needs no revert PR.

## Falsifiers (any one means this does not solve the outage)
1. The outage class also stops job dispatch to self-hosted runners. If GitHub creates no workflow runs at all (the recorded state since 2026-11-16), nothing can run on any runner. Only the owner can read the cause (the billing API returns HTTP 410 to the agent token), and public documentation does not say what a payment lock does to self-hosted dispatch.
2. The Linux jobs cannot be reproduced faithfully on the host.
3. The target repository is public and cannot confine fork pull requests.

## Cheapest test before any build (owner action)
Register one ephemeral runner on a disposable private repository during the outage and see whether a trivial required job is dispatched to it. A pass resolves falsifier 1; a fail ends this proposal.

## Not claimed
This does not make any unmet acceptance criterion pass, does not substitute local evidence for a server check, and does not touch `--admin`, protection rules or the classifier. Fixing the Actions or billing state makes the proposal unnecessary.

## Decision needed
Whether the owner will (a) run the cheapest test, then (b) provision the runner host and approve the `runs-on` PR. Nothing proceeds without that.
