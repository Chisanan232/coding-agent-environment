# Engineering policy (revision 3, SPE-116)

Safe, public statement of the policy. Machine-specific and corporate values live in private
profiles that are never committed (`docs/examples/workstation-profile-*.example.json` are
templates). See ADR 0014 for the decision and `docs/OWNER-MERGE-WORKFLOW.md` for merges.

## 1. Shared autonomy

| Autonomous | Owner approval required |
|---|---|
| Read, search and analyze authorized repositories | Production deployments and destructive migrations |
| Implement, build, test, run QA and security checks | Force pushes and remote branch deletion |
| Create isolated worktrees; small Gitmoji commits | Releases and irreversible publication |
| Push owned development branches and open PRs | Changes to effective Auto Mode, permission policy, security hooks, sandbox enforcement or trust boundaries |
| Independent technical reviews; PR merges under the class rules | |
| Sequential trusted dependency-bot PRs | |
| Clean inactive caches, build output and worktrees after confirming ownership and inactivity | |
| Alpha/Beta deployments within existing cost, scope and reversibility limits | |

No host, GitHub or organization policy is bypassed.

## 2. Where each concern is enforced

| Concern | Mechanism | Deterministic? |
|---|---|---|
| Environment context for the classifier | `autoMode.environment`, short and factual | No (advisory) |
| Operational authorization | Built-in classifier defaults; custom `allow` only with a documented need | No |
| Mandatory owner approval | `permissions.ask` rules, `PreToolUse` hooks, managed settings | Yes, once the owner deploys them |
| Security hard denial | Built-in `hard_deny`; `permissions.deny`; managed settings | Yes / built-in |
| Repository conventions | CLAUDE.md, skills, CI | CI only |
| Merge safety | `owner-merge-gate.py` verdict plus GitHub protection plus host authorization | Gate yes; host and GitHub separate |
| Outbound data by machine profile | `workstation-policy.py evaluate` as design and test oracle; host rules for enforcement | Oracle only until deployed |

## 3. PR classes

See the table in `docs/OWNER-MERGE-WORKFLOW.md`. In short: A owner-authored (admin or verified
code owner), B verified dependency bot (real GitHub APPROVE, one at a time), C everyone else
(owner's genuine approval on the exact head, never `--admin`). Always a regular merge commit
pinned with `--match-head-commit`. `--admin` is used only on a documented owner-only exception
with real bypass authority; the gate does not print it. Host denial: record with `host-denied`,
stop, report the exact blocked operation.

## 4. Workstation profiles

- Fill a private copy of the template outside this repository; pin it with
  `python3 scripts/workstation-policy.py fingerprint` (a hash, not the raw machine id).
- Personal: sensitive outbound (documents, logs, screenshots, recordings, structured data,
  internal config, personal data) asks per operation with data, source, destination, purpose,
  safer alternatives and risks. Routine source traffic to an approved private host is allowed.
- Corporate: protected data leaves only to destinations on the approved list (internal git,
  package registries, company LLM proxy); anything else is denied, even if the owner would
  approve a personal equivalent.
- Raw credentials are denied everywhere; approved authentication hosts stay usable because
  clients authenticate from their own credential store.
- Anything unverified (template, placeholder, mismatched fingerprint, unknown kind) is
  AMBIGUOUS: protected data is denied, and a username is never used as evidence.

## 5. Validation

```bash
python3 tests/test-pr-merge-policy.py
python3 tests/test-owner-merge-gate.py
python3 tests/test-owner-merge-gate-local.py
python3 tests/test-workstation-policy.py
python3 scripts/workstation-policy.py lint-settings docs/examples/candidate-settings-personal.example.json
```

Synthetic fixtures only (fake `gh`, disposable local git repositories, injected fingerprints).
Not covered: any live GitHub, host-classifier or deployed-settings behavior.

## 6. Migration, deployment and rollback (owner-operated)

Nothing below is performed by this change.

1. Review the candidate (`docs/examples/candidate-settings-<kind>.example.json`); render a
   fresh one with `workstation-policy.py render-settings --kind <kind> --out <scratch path>`.
2. Test the candidate without touching live settings, using the existing matrix tooling
   (`claude -p --setting-sources project,local --settings <candidate>`; fresh and resumed
   sessions), and compare routine prompts and protected-action blocking with the current baseline.
3. Deploy only after explicit owner approval, after PR #93 and the pending Step 1 and Step 2
   operator steps, by merging the `autoMode` and `permissions` blocks into `~/.claude/settings.json`
   by hand (or with `permission-rollout.py plan/apply`, which backs up and verifies hashes).
   Record the new hash; do not change the pinned hashes in the runbook beforehand.
4. Rollback: restore the backup the apply step wrote under `~/.claude/backups`, or
   `permission-rollout.py rollback`; confirm the file hash equals the recorded pre-change hash.
5. Corporate machines: use managed settings through the corporate channel; this repository
   never carries corporate hostnames or controls.
