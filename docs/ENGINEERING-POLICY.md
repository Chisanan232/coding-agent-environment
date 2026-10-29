# Engineering policy (revision 4, SPE-116)

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

## 2. Which concern is handled by which mechanism

| Concern | Mechanism | Deterministic? |
|---|---|---|
| Environment context for the classifier | `autoMode.environment`, short and factual | No (advisory) |
| Operational authorization | Built-in classifier defaults; custom `allow` only with a documented need | No |
| Mandatory owner approval | `permissions.ask` rules, `PreToolUse` hooks, managed settings | Yes, once deployed |
| Security hard denial | Built-in `hard_deny`; `permissions.deny`; managed settings | Yes / built-in |
| Repository conventions | CLAUDE.md, skills, CI | CI only |
| Merge safety | `owner-merge-gate.py` verdict plus GitHub protection plus host authorization | Gate yes; GitHub and host separate |
| Outbound data by machine profile | `workstation-policy.py` oracle, `workstation-policy-hook.py` adapter; managed settings and sandbox for real corporate enforcement | See section 3 |

## 3. What is actually enforced today

Status as of this revision. **Nothing introduced by SPE-116 is deployed**, so everything marked
"candidate" or "oracle" is not active on any machine. Synthetic unit tests prove the decision logic only.

| Approved behavior | State | What enforces it, exactly |
|---|---|---|
| Built-in Auto Mode rules, `hard_deny` for exfiltration | **Enforced now (host)** | Claude Code classifier defaults; non-deterministic by nature |
| Force push, `reset --hard`, `git clean -f`, terraform apply/destroy, IAM changes, secret access, destructive cloud MCP tools | **Enforced now, partly** | Static `permissions.ask` rules already in the live settings; PR #93 adds measured scoped asks; the candidate adds missing force-push, delete and publish forms with rtk twins |
| Remote branch deletion | **Partial** | Candidate asks `git push ... --delete`; `git push origin :branch` is NOT covered |
| Releases and publication | **Candidate** | Candidate asks `gh release create/upload`, `npm/pnpm/yarn/cargo/twine/uv publish`, `docker push`; other publishers are not covered |
| `gh pr merge --admin` | **Candidate** | Live settings allow `gh pr merge:*` and have no `--admin` ask, so an admin merge is ungated at the permission layer today; the candidate adds the ask |
| Class A/B/C merge rules | **Advisory (process)** | `owner-merge-gate.py` is deterministic but only when someone runs it; nothing forces a merge through it, GitHub has no protection or rulesets on these repositories, and the host allows `gh pr merge:*`. Class C "owner approval required" and "never --admin" are enforced only by the gate and agent discipline |
| Edits to settings, hooks, `~/.claude/bin`, `.mcp.json`, managed settings | **Candidate, Edit/Write tools only** | Candidate `Edit(...)`/`Write(...)` asks and `workstation-policy-hook.py` (not installed). Writes by shell redirect or `sed -i` are covered only by the built-in classifier rule, which is not deterministic |
| Personal sensitive outbound ASK with packet | **Oracle + partial hook** | Hook judges only curl/wget/scp/rsync/sftp/nc uploads (also inside `$()`, backticks, `sh -c`, wrappers). It does NOT see: git/gh/package-manager traffic, `ssh`, `aws`/`gcloud`/`rclone` uploads, interpreter-made requests, MCP tools, WebFetch, variable or command-substitution indirection of the tool name (`C=curl; $C`; quote tricks like `cu""rl` are normalized), or data carried in a GET URL or header. Shell parsing is best-effort: when quotes are unbalanced the hook falls back to a quote-blind split, and a deliberately constructed command (for example a separator hidden inside a quoted argument) can evade it. It guards against mistakes, not against an adversary |
| Corporate prohibited outbound DENY | **Not enforced** | Needs the organization's managed settings (`allowManagedHooksOnly`, `allowManagedPermissionRulesOnly`) and sandbox network limits (`sandbox.network.allowedDomains`, `allowManagedDomainsOnly`). The sandbox covers shell commands only; file tools, MCP servers and hooks run outside it. A hook that crashes or times out fails open unless the hook entry sets `onFailure: "block"` (the published hooks guide does not mention that field, so it is unverified). The adapter itself escalates, rather than ignores, its own errors on commands that name a network tool |
| Machine profile verification | **Weak by design** | A fingerprint detects a profile copied to the wrong machine, not a forged or rewritten one |
| Auto Mode candidate (`$defaults`, short environment) | **Candidate** | `docs/examples/candidate-settings-*.example.json`, rolled out only by `candidate-rollout.py` |

## 4. PR classes

See the table in `docs/OWNER-MERGE-WORKFLOW.md`. A owner-authored (admin or verified code owner),
B verified dependency bot (real GitHub APPROVE by the owner, one at a time), C everyone else
(owner's genuine approval on the exact head, never `--admin`). Always a regular merge commit pinned
with `--match-head-commit`. The gate never prints `--admin`; the normal Class A path does not need
it (these repositories have no branch protection). Keeping the `--admin` ask therefore does not
block the approved Class A workflow; it gates only the exception path (same-identity review
deadlock, unavailable CI) that the owner-only policy limits to real bypass authority. No `allow` rule
for admin merges is added. Host denial: record with `host-denied`, stop, report the blocked operation.

## 5. Workstation profiles

- Fill a private copy of the template outside this repository; pin it with
  `python3 scripts/workstation-policy.py fingerprint` (a hash, not the raw machine id).
- Personal: sensitive outbound (documents, logs, screenshots, recordings, structured data,
  internal config, personal data, unclassified payloads) asks per operation with data, source,
  destination, purpose, safer alternatives and risks. Routine source traffic to an approved
  private host is allowed.
- Corporate: protected data leaves only to destinations on the approved list (internal git,
  package registries, company LLM proxy); anything else is denied.
- Approved authentication hosts stay usable because clients authenticate from their own credential store.
- Anything unverified (template, placeholder, mismatched fingerprint, unknown kind) is AMBIGUOUS:
  protected data is denied, and a username is never used as evidence. Keep the filled profile where
  the agent cannot rewrite it.

### Open deviations from the owner-approved policy

Both are stricter than the approved policy, so they fail closed; they are listed, not hidden.

1. **Raw credentials on the personal profile.** The approved policy is per-operation ASK for sensitive
   outbound. The implementation DENIES raw credentials on every profile, because CLAUDE.md item 21 forbids
   an agent from reading or exposing a secret value. Proposed reconciliation: personal profile ASKs
   (an approval authorizes one stdin-to-consumer transfer, never exposing the value in context),
   corporate stays DENY, `auth_handshake` to approved hosts stays allowed. The edit was denied by the
   host's Auto Mode classifier (`[Security Weaken]`) during SPE-116 and is **not applied**; it needs the owner.
2. **Ordinary configuration maintenance.** The approved policy keeps skills, commands, agents, rules
   and conventions autonomous. The implementation treats the whole `~/.claude/` tree and project
   `.claude/{agents,skills,commands,rules,CLAUDE.md}` as protected. Proposed reconciliation: only
   `settings*.json`, `hooks`, `bin`, `plugins`, `~/.claude.json`, managed settings and `.mcp.json` ask;
   skills, commands and agents stay autonomous unless their frontmatter adds `hooks`, `allowed-tools`
   or `permissionMode`. Same host denial, same status.
3. **Personal upload with no stated purpose.** The hook builds the disclosure packet from the Bash `description` as the
   purpose. Without one the personal profile DENIES (`ASK_PACKET_INCOMPLETE`) instead of asking with a "purpose missing" note.
   Stricter than the approved per-operation ASK; the same DENY-to-ASK direction as item 1, so it is not changed without the owner.

## 6. Validation

```bash
for t in tests/test-pr-merge-policy.py tests/test-owner-merge-gate.py tests/test-owner-merge-gate-local.py \
         tests/test-workstation-policy.py tests/test-workstation-policy-hook.py tests/test-candidate-rollout.py; do python3 "$t"; done
python3 scripts/workstation-policy.py lint-settings docs/examples/candidate-settings-personal.example.json
```

Synthetic fixtures only (fake `gh`, disposable local git repositories, injected fingerprints, scratch settings
copies). Not covered: any live GitHub, host-classifier or deployed-settings behavior.

## 7. Migration, deployment and rollback (owner-operated)

Nothing below is performed by this change. Do **not** use `permission-rollout.py apply`: it applies the
repository baseline's `autoMode` block, which still contains the retired natural-language rules (including a
standing merge-authorization entry). Use `scripts/candidate-rollout.py`, which merges only `autoMode`
(replaced by the short candidate; owner `soft_deny`/`hard_deny` entries kept, and any `autoMode` key the candidate omits keeps its live value; the merged `autoMode` is linted and a forbidden preserved entry blocks apply), the union of `permissions.ask` (an existing ask lost in the merge is blocking),
and one optional PreToolUse hook entry, and never `allow`, `deny`, `env` or other keys.

1. `candidate-rollout.py plan --live ~/.claude/settings.json --candidate <candidate> --baseline <repo .claude/settings.json> [--hook-command ... --fail-closed]`
   prints the pinned live and result hashes, the asks added, replaced `autoMode` text and compatibility findings. Read-only.
2. Test the result on a scratch copy with the fresh and resumed matrix (`docs/ROLLOUT-PERSONAL-MAC.md`) before any apply.
3. After explicit owner approval, the owner runs `apply` with both hashes pinned. It backs up the live file (0600),
   refuses on any hash mismatch or blocking finding, and writes atomically.
4. `verify` confirms the result hash; `rollback` restores the backup after checking both its hash and that the live file still
   has the hash `apply` produced, so edits made since apply are never silently lost.
5. Corporate machines: use the organization's managed settings; see `docs/ROLLOUT-CORPORATE-PROFILE.md`.
   This repository never carries corporate hostnames or controls.

## 8. Private-repository CI acceptance and bounded validation (founder policy, 2026-10-22)

Applies to every product and session. This section is the single recorded statement; other documents link here
and do not restate it.

**CI states, never merged into one.** `CI_PASS` hosted CI executed and passed. `CI_UNAVAILABLE_EXTERNAL` hosted
jobs never executed because of a verified billing or quota restriction. `LOCAL_EQUIVALENT_PASS` the corresponding
commands ran locally and passed against the exact revision. `LOCAL_EQUIVALENT_FAIL` they ran and found defects.
`NOT_VERIFIED` no valid evidence. Unavailable CI is never reported as `CI_PASS`. A check that executed and failed
is `CI_FAILED` and is never bypassed.

**Gate.** For a verified billing or quota outage only, `LOCAL_EQUIVALENT_PASS` satisfies the corresponding
engineering or release gate when the local run is sufficiently equivalent. Record the exact evidence in the PR and
Jira; do not claim hosted CI succeeded; do not weaken branch protection; do not ask the founder again for the same
known exception.

**Equivalence.** Inspect the workflow's real commands, run them locally with matching runtime, dependencies,
features and configuration, record exit codes and test counts, preserve sanitized logs, pin to the exact
commit or tree, and document platform or coverage differences. Reuse evidence while the tested inputs are
unchanged; a documentation-only commit does not justify rerunning a full suite. SonarCloud may stay unavailable;
no other scanner stands in for it.

**Not replaceable by local runs.** Real macOS GUI or accessibility acceptance, code signing and notarization, Linux
kernel security mechanisms, native sandbox confinement, real provider or model acceptance, independent security QA
when required, and any mandatory real-hardware or real-daemon evidence. Such a gate stays `BLOCKED` or
`NOT_VERIFIED`; its acceptance criteria are never relaxed to obtain a green result.

**Bounded review.** At most two substantive independent review/fix cycles per coherent candidate: cycle 1 full
review, remediation and targeted regression tests; cycle 2 exact-head or affected-delta review and closure. Later
findings: fix Critical and High defects and re-review only the affected area; file Medium and Low hardening as
follow-ups with a rationale. The limit never authorizes merging a known Critical or High security defect. A
blocker that persists becomes one decision packet.

**Merge.** Eligible PRs merge automatically when the authenticated identity, GitHub rules and host policy allow it.
Self-review and independent agent review count as engineering validation but never as a manufactured GitHub
approval. On a self-approval, branch-protection or host denial: record it once, do not retry through aliases or
other syntax, and hand over the PR URL, head SHA, local evidence, review verdict and the head-pinned owner merge
command, then continue other work.

**Release readiness.** Eight states stay separate, each reported with its own recorded evidence. A skipped or
unavailable check is never PASS. `ENGINEERING_COMPLETE` and `INDEPENDENT_QA_SIGNED_OFF` replace the earlier
`DEVELOPMENT_COMPLETE` and `QA_SIGNED_OFF`, which are no longer valid in reports. Read an older record under the
new name only if it also carries the stricter evidence below (merged work, post-merge CI at the exact SHA, and both
QA states).

| State | Holds when | Evidence |
|---|---|---|
| `ENGINEERING_COMPLETE` | Scoped PRs merged; post-merge main CI (or an approved local equivalent) passed at the exact SHA | producer evidence: PRs, SHAs, CI runs |
| `FUNCTIONAL_QA_PASS` | Independent QA exercised the acceptance criteria at that SHA | QA report, `docs/QA-HANDOFF.md` |
| `SECURITY_QA_PASS` | Independent QA ran a real negative control for every security-critical claim and verified artifact provenance | QA report with positive and negative control results |
| `INDEPENDENT_QA_SIGNED_OFF` | Both QA states PASS at one SHA and artifact set, no open Critical or High finding | signed-off QA verdict naming SHA and digests |
| `RELEASE_CHECKLIST_PASS` | The `release-readiness` skill checklist is complete for that SHA | checklist with results |
| `READY_FOR_RELEASE` | All earlier states hold, every other in-scope integration, dogfooding and cleanup gate is satisfied by live evidence, and every owner acceptance item is recorded | the records of those states and gates; if only a founder-only gate remains, report `RELEASE_BLOCKED_BY_OPERATOR` |
| `RELEASE_APPROVED` | A durable founder GO record names the exact version, SHA and artifact digests | the record, not an agent summary |
| `RELEASED` | The authorized path tagged and published, and the published digests match the approved ones | release record |

Any new commit or artifact change invalidates every state above it until requalified. `RELEASE_APPROVED` is
never inferred from an agent-produced record, an author field, or an approval of a different version, SHA or
artifact. An unavailable hosted workflow alone does not keep `READY_FOR_RELEASE` false when an approved local
equivalent satisfies that requirement; missing platform evidence, owner acceptance or required security QA still
blocks it. Tagging, publication and production deployment need separate authorization, and ordinary engineering
work never performs them.

**Executable form in this repository.** Two forms of this one policy, neither a second policy:
`scripts/owner-merge-gate.py --ci-unavailable` (a posted `OWNER-MERGE-CI-UNAVAILABLE` record, see
`docs/OWNER-MERGE-WORKFLOW.md`) and, per repository and off by default, `local_equivalent_ci` in the owner-merge
runner allowlist (`docs/OWNER-MERGE-AUTONOMY.md` section 9).

**Merge guarantees are stated, never implied.** A merge's audit record says what backed it: head pin (always GitHub-enforced),
server-side protection (`server-enforced`, or `private-free-app-level` where the plan makes protection unavailable, opt-in per
repository), review basis (a GitHub approval from a second identity, or local evidence that is **not** a GitHub approval) and CI
class. A host denial is classified (GitHub authorization, branch protection, permission rule, auto-mode soft or hard denial, hook,
runner refusal), reported once and never re-routed; the measured inconsistency between sessions and the Private Free contract are in
`docs/OWNER-MERGE-AUTONOMY.md` sections 13 and 14.
