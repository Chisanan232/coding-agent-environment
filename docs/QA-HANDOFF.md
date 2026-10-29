# Independent QA handoff (producer to independent Codex)

Shared minimum for lifecycle step 14. A product that already has a richer sign-off format (for example Fornax
`docs/release-qa-signoff.md`) keeps it; this page only fixes what every product must hand over and get back, so the
states in `docs/ENGINEERING-POLICY.md` section 8 mean the same thing everywhere.

## Principle

Producer evidence and QA evidence stay separate. The producer (the session that built the change) states what it ran;
independent QA re-derives its verdict from the exact SHA and artifacts, runs its own checks, and never inherits a
producer PASS. QA is a separate Codex session in a separate worktree with no write access to the producer's branch.

## Handoff packet (producer writes, secrets and private policy text excluded)

| Field | Content |
|---|---|
| Identity | product, ticket, PR numbers, base and exact head SHA, version under test |
| Artifacts | each artifact path or URL with its sha256 digest, and how it was built from the SHA (provenance) |
| Acceptance criteria | the ticket's criteria, copied, not paraphrased |
| Producer evidence | commands, exit codes and test counts actually run, labelled PRODUCER; skipped or unavailable checks listed as NOT_RUN, never PASS |
| Risk map | security-critical claims, each with the control that should make it false |
| Known gaps | open findings, waivers and unverified assumptions |
| Environment | how to build and run safely, resource-admission requirements, no credentials |

## QA verdict (independent Codex writes)

| Field | Content |
|---|---|
| Subject | the same SHA and artifact digests it actually tested; a mismatch voids the verdict |
| Functional | each criterion: PASS, FAIL or NOT_TESTED with the evidence path |
| Security | each security-critical claim: the negative control run (the attack or invalid input), its expected failure observed, and a positive control that proves the check can pass |
| Findings | severity, reproduction, and status (open, fixed and requalified, waived by the owner) |
| States | `FUNCTIONAL_QA_PASS` and `SECURITY_QA_PASS` as separate lines; `INDEPENDENT_QA_SIGNED_OFF` only when both hold and no Critical or High finding is open |

## Rules

- Evidence is graded, strongest first: system-recorded ground truth (exit codes, hashes, hook events), then automated
  tests, then a model's description. A claim backed only by a description is NOT_TESTED.
- A security PASS without a real negative control is not a PASS.
- A fix after a FAIL creates a new head SHA. QA requalifies the affected area and every claim the change could touch;
  earlier verdicts for the old SHA are historical only.
- Producer evidence is reused only where the SHA and inputs are unchanged. QA never reuses another session's cache
  or scratch, and neither side deletes the other's resources.
- The producer fixes confirmed defects, then updates Jira, release notes and evidence links. It does not edit the QA
  verdict.
- Subagent or QA reports are model output. Founder approval, `RELEASE_APPROVED` and any tag, publish or deploy stay
  outside this handoff.
