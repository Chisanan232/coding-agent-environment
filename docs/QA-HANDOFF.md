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
by exit code: 0 `QA_PASS`, 10 `QA_FAIL` (fix, new SHA, requalify), 11 `QA_INCOMPLETE` (also: Codex could not be started,
or exited non-zero before writing any verdict, for example an auth or rate-limit failure; retry with a new attempt), 12
`QA_INVALID` (integrity: tampering or an unreadable record), 64 `REFUSED` (the run directory was already run or is locked;
a usage refusal, `RESULT.json` is left untouched), 75 `WAITING_RESOURCE` or `NOT_RUN` (Codex was not started; not a QA
failure, and `verdict` never rewrites either as `QA_INVALID`). `RESULT.json` records the state, reasons,
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
manifest taken before the run, and so does a digest of the git control files (`config`, `hooks/`, `info/attributes`, `objects/info/alternates`, `commondir`; a rewritten config, hook, attributes or alternates file is `QA_INVALID`; no git is run
against the clone afterwards, because the QA process can write it), commands were really executed in the event stream, and the event stream and verdict are hashed at exit.
What is persisted: the wrapper's own Codex launch arguments (`RUNMETA.json`) and the worker's executed commands (`events.jsonl`), as evidence. No host process listing is read or stored, and the worker environment is built from an allowlist so credentials in the producer's environment never reach it. A run in which no command executed is `QA_INCOMPLETE`, never a pass. Every call carries explicit `-s workspace-write`
and no model override, because the live Codex configuration is full-access with approvals off.

### Boundaries

- A Codex verdict is QA evidence, not merge evidence. It is never written as an owner-merge review record.
- Admission is checked with `disk-admission.py check --weight light`; heavy commands inside the QA run must use the
  product's own resource wrapper, and contention is reported as blocked, not failed.
- Product release flows (`/release-qa-gate`, frozen candidates, release evidence) are not touched by this tool.
- Adoption is per product and opt-in: a pilot is shown only when the product's own session runs the hop and acts on the
  verdict. No general migration, managed skill or global instruction change is part of this.

### Demonstration (2026-10-29, real Codex, disposable repository)

A scripted producer that acts only on the exit code, with a defect seeded where the producer's own tests do not look
(`subtract` returned an absolute value and the visible tests checked only positive results):

| Round | SHA | Result | Notes |
|---|---|---|---|
| 1 | defective commit | `QA_FAIL`, exit 10 | AC2 failed: `subtract(2, 5)` returned 3; one High finding; 5 commands executed |
| (producer) | | fix and regression test, new commit | no human copied a prompt or a verdict |
| 2 | fixed commit | `QA_PASS`, exit 0 | 6 commands executed, no finding, nothing untested |

The same run showed that real Codex accepts the pinned sandbox settings and that the private shallow clone works for a
non-tip commit. The offline suite (`tests/test-qa-handoff.py`) covers the wrapper's guarantees with a fake `codex`.
This demonstrates the mechanism, not any product's adoption: a product counts as adopted only when its own session runs
the hop and acts on the verdict.

### Known gaps (stated, not closed)

- A `QA_PASS` does not prove every criterion was covered. Criteria are free text and nothing maps a verified check to a
  criterion id: a model that verifies one thing and lists the rest nowhere can still pass if it clears the floor
  (valid schema, baseline, clean tree, no failing check or Medium+ finding, two successful commands, clean exit,
  COMPLETE, not LOW). Structured criteria with required per-id results are a possible later schema change, not part of
  this slice; until then a reviewer compares `criteria_sha256` and the verified checks with the ticket.
- Reads are not confined: `-s workspace-write` restricts writes, so a QA command can read this user's real files,
  including the Codex login the run needs (`CODEX_HOME`) and other home secrets, and could quote them in evidence. A
  separate OS user or an allowlist-read profile is needed to prevent that; it is not implemented here. Treat the evidence
  text as possibly containing anything this user can read.
- SIGTERM, SIGINT and SIGHUP to the wrapper are handled: codex's group is killed and a hashed interrupted record is
  written, so `verdict` reports `QA_INCOMPLETE` and a later edit is `QA_INVALID`. A signal that lands in the few bytecodes
  between the child being forked and `Popen` returning can leave codex unmonitored; the wrapper's exit then closes codex's
  stdin pipe, so it reads an empty prompt. SIGKILL of the wrapper cannot be handled at all and leaves no record.
- After a clean codex exit the wrapper sends SIGKILL to the codex process group. If that group were fully gone and its id
  recycled by an unrelated group leader within the same moment, that group would be signalled; this is negligible in
  practice and not defended against.
- `--criteria` and `--qa-notes` are files the producer chooses. The prompt labels them producer-supplied and `RESULT.json`
  records `criteria_sha256`, but the wrapper does not verify them against the ticket; a producer that narrows the criteria
  narrows what a `QA_PASS` covers. A reviewer compares the hash and text with the ticket.
- A `QA_PASS` is the QA process's own report, bound to a hashed event stream a reviewer can audit. It is not a
  cryptographic attestation, and the producer and the QA process run as the same OS user.
- The sandbox can read files this user can read (including Codex's own login directory) and the run inherits the user's
  Codex configuration (hooks, MCP servers). Sandbox network and writable roots are pinned per call, but read access is not
  restricted.
- QA-controlled text reaches the producer through `RESULT.json` and the console. It is stripped, capped and marked
  untrusted, but the producer must still treat it as data.
- The event stream is the `codex exec` process's own stdout. The wrapper counts only `command_execution` completions that carry a unique id (a model cannot write to that stream; text it prints stays inside an escaped string field on the verified codex version), but a process that can write the stream itself, such as a replaced or compromised codex binary, can forge items and obtain `QA_PASS`. Codex QA reproduced this only with the offline fake codex, which is that process. Defending against it needs an attestation the producer's own user cannot create; that is the open evidence-independence decision, not something this wrapper can close.
- A sandboxed command that detaches into its own session (`setsid`) can outlive the timeout kill.
- Untracked files that QA creates in the clone are reported (count and sample), not forbidden: builds and caches are
  legitimate, so a reviewer decides whether they matter.
- The two-command floor is a floor, not proof: a no-op command satisfies it. The command count is recomputed from the
  hashed event stream (not from the saved run record), and the evidence text and stream remain for a reviewer.
- Your Codex hooks and MCP servers run unsandboxed inside the Codex client during a QA run, as this user. A write-capable
  one could act on the run directory. The sandbox pins cover the QA commands, not those components; whether the
  system `/tmp` is writable to QA was not verified.
- The saved run record and result files are wrapper-owned and protected from the QA process, but not from the same user.
  Re-run `verdict` just before relying on a pass (a straggler process could still write to the clone after `run` returns).
- On codex-cli 0.163.0, text printed by a QA command stays inside an escaped field of the event stream and cannot forge
  an event line. This was observed on that version, not guaranteed for others.
