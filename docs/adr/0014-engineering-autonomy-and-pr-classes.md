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

The repository has no release version; revisions are tracked by ADR number and date. No
release or tag is created by this change.
