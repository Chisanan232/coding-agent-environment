# ADR 0014: Engineering autonomy, PR merge classes and workstation profiles

Status: accepted as repository policy and tooling (SPE-116). **Not deployed.** Nothing here
changes live `~/.claude/settings.json`, managed settings or hooks.

## Context

The owner reset Claude Code Auto Mode to the official built-in defaults. The long
natural-language `autoMode` rule set that grew around it (ADR 0012) gave no deterministic
enforcement, was repeatedly contradicted by the classifier, and one of its entries (a
standing merge authorization) was denied by the host. The owner then fixed the revised
policy: which work is autonomous, which needs the owner, how pull requests are merged by
class, and how personal and corporate machines treat outbound data.

## Decision

1. **Baseline is the official defaults.** Candidates keep `"$defaults"` in every
   `autoMode` list, keep `environment` to a few factual lines, and add custom `allow`,
   `soft_deny` or `hard_deny` entries only with an independently documented need. Operational
   authorization, owner-approval gates, hard denials and repository conventions are separate
   concerns and live in separate mechanisms (see the matrix in `docs/ENGINEERING-POLICY.md`).
2. **Three PR classes**, decided by `scripts/owner-merge-gate.py` from GitHub facts, never
   from a PR's own text: A owner-authored, B verified dependency bot, C everything else. An
   unverifiable author is refused. Details and limits are in `docs/OWNER-MERGE-WORKFLOW.md`.
   A repository with a bot-looking but failing identity, branch, commit author or scope is class
   C, so a spoof earns the stricter path.
3. **A PASS is evidence, not authority.** It is not a GitHub approval, not host authorization
   and not a permission change (ADR 0013). A host denial is recorded with `host-denied` and
   stands; no alternate route exists.
4. **Workstation profiles are verified, not inferred.** `scripts/workstation-policy.py`
   evaluates an outbound or policy-change action against a profile pinned to a hashed machine
   fingerprint. Personal: sensitive disclosure asks per operation with a complete packet.
   Corporate: anything protected outside the approved list is denied. A missing, templated or
   mismatched profile is AMBIGUOUS and fails closed. Raw credentials never leave.
5. **The tooling decides and lints; the host enforces.** The repository ships decision
   functions, a settings linter and reviewable candidate settings. Deterministic enforcement is
   `permissions.ask/deny`, hooks and managed settings that the owner deploys. Natural-language
   Auto Mode text is context for the classifier, not a security control.
6. **Nothing is recreated.** The denied standing-authorization `autoMode.allow` entry is not
   in any file, template or migration, and the lint rejects its wording.

## Reconciliation with open work

- PR #94 (SPE-115) introduced the read-only gate and ADR 0013. This change is stacked on it
  and extends the same script; it adds no second merge tool.
- PR #93 (SPE-114) changes the baseline `permissions.ask` list and is deliberately held until
  the Step 2 deployment is verified. This change does not touch `.claude/settings.json`,
  `docs/examples/automode-trusted-sources.example.json` or `scripts/permission-rollout.py`, so
  it cannot move the baseline hash pinned in the operator runbook.
- The existing baseline `autoMode.allow` text is left as is. Replacing it with the minimal
  candidate is a protected-settings change and a separate owner decision (see Migration).

## Consequences

- Third-party PRs cannot be merged on agent evidence alone; the owner's real approval is
  required and `--admin` is never printed for them.
- The gate cannot tell whether the owner or the agent (same token) typed an approval. This is
  stated, mitigated by policy and the host's Self-Approval rule, and not hidden.
- "CI green" is every reported check passing, not the required set; the CI-unavailable
  exception is bounded (ADR 0013).
- The CI-unavailable exception applies only when GitHub reports no checks; billing exhaustion that records failed runs stays `CI_FAILED`, which fails closed.
- Candidate `permissions.ask` patterns are unverified against the live host. They need the
  fresh and resumed matrix (see `docs/ENGINEERING-POLICY.md`) before any deployment.

## Version history

| Revision | Date | Change |
|---|---|---|
| 1 | 2026-10-18 | ADR 0012 natural-language rule set (superseded as the direction; file kept) |
| 2 | 2026-10-19 | ADR 0013 read-only owner merge gate (SPE-115) |
| 3 | 2026-10-19 | This ADR: PR classes, workstation profiles, minimal candidates (SPE-116) |
| 4 | 2026-10-19 | Consistency review against the owner's requirements; hook adapter, rollout tool, enforcement status (SPE-116) |

The repository has no release version; revisions are tracked by ADR number and date. No
release or tag is created by this change.

## Revision 4: consistency review against the approved requirements

The implementation was compared with the owner's written requirements, not with its own tests.

**A. `gh pr merge --admin` ask: kept, and it does not block Class A.** The approved Class A path is a regular merge commit
pinned with `--match-head-commit`; the gate never prints `--admin` and these repositories have no branch protection, so the
approved workflow never needs it. The ask fires only on the exception path (same-identity review deadlock, unavailable CI) that
the owner-only policy restricts to real bypass authority, which is a material authority boundary a permission rule cannot
evaluate. Measured fact: the owner's live settings already allow `gh pr merge:*` and have no `--admin` ask, so today an admin merge
is ungated at the permission layer; the candidate adds the ask. No `allow` rule for admin merges is added, and the lint rejects one.

**B. Raw credentials: the implementation is stricter than the approved policy.** Approved: per-operation ASK for personal sensitive
outbound, ordinary authentication stays usable. Implemented: raw credentials are denied on every profile, derived from CLAUDE.md
item 21 (an agent never reads or exposes a secret value), without the owner having chosen that for the personal profile. Proposed
reconciliation: personal ASKs with the full packet (an approval covers one stdin-to-consumer transfer and never puts the value in
context), corporate stays DENY, `auth_handshake` to approved hosts stays ALLOW. The change was denied by the host's Auto Mode
classifier (`[Security Weaken]`) and is not applied; the owner decides.

**C. Ordinary configuration maintenance: the implementation is stricter than the approved policy.** A hardening round protected the
whole `~/.claude/` tree and project agents, skills, commands, rules and CLAUDE.md. Approved: those stay autonomous; only
permissions, security hooks, sandbox and trust boundaries need approval. Proposed reconciliation is in
`docs/ENGINEERING-POLICY.md` section 5. The measured permission layer is already consistent with it (the candidate asks cover
settings, hooks, bin and `.mcp.json` only). The oracle and hook adapter are the stricter parts, are not deployed, and carry the
same pending host denial.

**D. Enforcement.** See `docs/ENGINEERING-POLICY.md` section 3 for the exact table. Summary: built-in classifier rules and the
static ask rules are enforceable today; the gate is deterministic but voluntary; the workstation oracle is a design and test aid;
the hook adapter covers a defined subset and is not installed; corporate DENY is not enforced without the organization's managed
settings and sandbox network limits.

**Deployment path.** `permission-rollout.py apply` writes the repository baseline's `autoMode` text, which is the retired rule set,
and its pinned hashes predate the owner's reset. The candidate is rolled out only with `scripts/candidate-rollout.py`
(`docs/ROLLOUT-PERSONAL-MAC.md`), which merges just `autoMode`, the ask union and one optional hook entry. PR #93 shares no files with
this change and can merge independently; its asks reach a machine through `--baseline`, not through its own apply path.
