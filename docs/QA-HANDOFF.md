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

## Automated hop (SPE-131)

`scripts/qa-handoff.py` automates only the step that used to need the founder to copy a prompt between sessions. It is a
single CLI over local files and one headless `codex exec`: no daemon, database, broker or cloud service, and it replaces
no existing workflow.

```
qa-handoff.py prepare --repo PATH --sha SHA --ticket T --product P --criteria FILE [--producer-evidence FILE] [--qa-notes FILE]
qa-handoff.py run     --run-dir DIR [--timeout SECONDS]     # hand off, wait, validate, print the state
qa-handoff.py verdict --run-dir DIR                         # re-evaluate the saved files (idempotent)
```

The producer's own session runs `run` (in the foreground, or in the background and read when it completes) and continues
by exit code: 0 `QA_PASS`, 10 `QA_FAIL` (fix, new SHA, requalify), 11 `QA_INCOMPLETE`, 12 `QA_INVALID`, 75
`WAITING_RESOURCE` (admission refused; Codex was not started; not a QA failure). `RESULT.json` records the state, reasons,
SHA and hashes. The verdict schema is AASM's worker result contract (`STATUS / BASELINE / VERIFIED / SUSPECTED_FINDINGS /
UNTESTED_OR_BLOCKED / CONFIDENCE`), mapped to these states; it is the same shape the release-QA workers already return.

### What is proven, implemented, planned or assumed

| Mechanism | Status |
|---|---|
| Headless `codex exec` with explicit sandbox flags, `exec resume`, `--output-schema`, process-group timeout kill | Proven (behavior matrix 2026-10-22 and 2026-10-29, codex-cli 0.163.0; schema output and a real QA run in the SPE-131 demonstration) |
| AASM release QA by Claude sub-agents (`/release-qa-gate`, `qa-*` roles, worker result contract, `resource-lock.py`, watchdog) with a committed sign-off enforced by `release-readiness.sh` | Proven for rc.7 (sign-off and attempt evidence in the repository) |
| Independent Codex QA campaigns (`.qa-campaigns/`: pinned private clones, isolated HOME/TMPDIR/CARGO_TARGET_DIR, offline, command packets, HOLD/admit against the disk floor, verdicts as Jira comments) | Proven in use, but driven by an interactive Codex primary that the founder feeds; not callable |
| Campaign coordinator (Jira-DAG resume of Claude sessions, tmux injection) | Proven live for resuming Claude sessions; injection into an interactive pane is not a supported worker interface and is not used here |
| `SendMessage` to a peer session | Delivers, but a peer in prompting mode holds the message for its user's approval, so it is a notification and not an automatic worker |
| An automated producer to Codex hop in AASM | **Assumed, not found**: the AASM repository contains no Codex invocation; the Codex references are the product's own Codex adapter |
| This document, before SPE-131 | Implemented as a contract, never run |

### What "independent" means here, and what it does not

Independent means a separate Codex process with a fresh `--ephemeral` model context that sees only the packet and a clean
clone at the exact commit, never the producer's transcript, with producer evidence labelled as an unverified claim. It is
**not** filesystem or configuration isolation: the sandbox can still read files this user can read, and the run inherits
the user's Codex configuration (hooks, MCP servers) because no credential is copied into a private home. The wrapper
therefore checks facts instead of trusting the verdict text: the clone's `.git/HEAD` and every tracked file match a
manifest taken before the run (no git is run against the clone afterwards, because the QA process can write its
`.git/config`), commands were really executed in the event stream, and the event stream and verdict are hashed at exit.
A run in which no command executed is `QA_INCOMPLETE`, never a pass. Every call carries explicit `-s workspace-write`
and no model override, because the live Codex configuration is full-access with approvals off.

### Boundaries

- A Codex verdict is QA evidence, not merge evidence. It is never written as an owner-merge review record.
- Admission is checked with `disk-admission.py check --weight light`; heavy commands inside the QA run must use the
  product's own resource wrapper, and contention is reported as blocked, not failed.
- Product release flows (`/release-qa-gate`, frozen candidates, release evidence) are not touched by this tool.
- Adoption is per product and opt-in: a pilot is shown only when the product's own session runs the hop and acts on the
  verdict. No general migration, managed skill or global instruction change is part of this.
