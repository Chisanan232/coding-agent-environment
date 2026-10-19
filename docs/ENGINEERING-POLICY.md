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
| Personal sensitive outbound ASK with packet | **Oracle + partial hook** | Hook judges only curl/wget/scp/rsync/sftp/nc uploads (also inside `$()`, backticks, `sh -c`, wrappers). It does NOT see: git/gh/package-manager traffic, `ssh`, `aws`/`gcloud`/`rclone` uploads, interpreter-made requests, MCP tools, WebFetch, variable or command-substitution indirection of the tool name (`C=curl; $C`; quote tricks like `cu""rl` are normalized), or data carried in a GET URL or header. Shell parsing is best-effort |
| Corporate prohibited outbound DENY | **Not enforced** | Needs the organization's managed settings (`allowManagedHooksOnly`, `allowManagedPermissionRulesOnly`) and sandbox network limits (`sandbox.network.allowedDomains`, `allowManagedDomainsOnly`). The sandbox covers shell commands only; file tools, MCP servers and hooks run outside it. A hook that crashes or times out fails open unless the hook entry sets `onFailure: "block"` (check the minimum Claude Code version for that field in the hooks reference). The adapter itself escalates, rather than ignores, its own errors on commands that name a network tool |
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
(replaced by the short candidate; owner `soft_deny`/`hard_deny` entries kept), the union of `permissions.ask`,
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
