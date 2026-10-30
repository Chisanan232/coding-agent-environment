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
qa-handoff.py run     --run-dir DIR --qa-identity NAME --exchange DIR [--require-env-isolation]   # as the QA OS user (SPE-142)
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
**not** an OS-user boundary in the default mode: the QA process runs as this user (see `--qa-identity` below for the
mode that is). File reads are confined by a per-run Codex permission
profile (below), and the run uses a private `CODEX_HOME`, so the user's own Codex configuration, hooks and MCP servers are
not loaded. The wrapper therefore checks facts instead of trusting the verdict text: the clone's `.git/HEAD` and every tracked file match a
manifest taken before the run, and so does a digest of the git control files (`config`, `hooks/`, `info/attributes`, `objects/info/alternates`, `commondir`; a rewritten config, hook, attributes or alternates file is `QA_INVALID`; no git is run
against the clone afterwards, because the QA process can write it), commands were really executed in the event stream, and the event stream and verdict are hashed at exit.
What is persisted: the wrapper's own Codex launch arguments (`RUNMETA.json`) and the worker's executed commands (`events.jsonl`), as evidence. No host process listing is read or stored, and the worker environment is built from an allowlist so credentials in the producer's environment never reach it. A run in which no command executed is `QA_INCOMPLETE`, never a pass. No model override is passed. The private home carries only the permission profile and a link to the login, so the live
full-access configuration is never inherited.

### Read confinement (SPE-136)

Each run gets `<run>/codexhome/` with a `config.toml` that sets `default_permissions = "qa_confined"`: reads are an allowlist
(`:minimal`, `/usr`, `/bin`, `/opt/homebrew`, the Command Line Tools, plus any `--read-root DIR` the producer adds for a
toolchain), writes only the clone, `out`, `tmp`, `home` and `cargo-target`, network disabled, and the credential
directories under HOME (`~/.codex`, `~/.ssh`, `~/.aws`, `~/.config`, `~/.claude`, `~/.gnupg`, `~/.docker`, `~/.kube`, `~/.netrc`,
`~/.npmrc`, `Library/Keychains`, cargo credentials) and the real Codex home are denied explicitly. No `-s` or `--add-dir`
is passed (those select the legacy write-only sandbox). The login reaches the Codex client, which is not sandboxed, only as
a link to the real `auth.json`; no credential is copied, and the link is unreadable to the QA tool commands.
The clone is deliberately not marked trusted and `exec` also selects the profile explicitly (`-c default_permissions=...`):
the clone is the commit under test, and a trusted project lets its own `.codex/config.toml` widen the profile (measured:
a planted allowlist entry turned a denied canary read into ALLOWED).
Before codex starts, the wrapper proves the profile with the exact private home through `codex sandbox -P` (no model): it
plants a synthetic canary under HOME and requires that reading it, listing the real Codex home, opening the real login file
(open only, no content is read), reading the canary from a scratch clone whose own `.codex/config.toml` tries to allow it, and
writing outside the roots are all denied with EPERM or EACCES (ENOENT or any other text proves nothing and fails), while
reading the clone and writing `out` still work. A control that is mis-reported or allowed ends the run as `QA_INVALID`
(`read confinement not established`) with codex never started; a probe that could not run at all (no output, timeout, codex
sandbox unavailable) is `QA_INCOMPLETE` (`read confinement could not be checked`, retry). The controls are recorded in
`CONFINEMENT.json` and summarised in `RESULT.json`. A missing `codex` binary is `QA_INCOMPLETE`. `--read-root` refuses HOME and
its ancestors, credential directories, the real Codex home, the QA state and canary roots, and the run directory, its
siblings under the same product/ticket and any directory containing it. The profile is
allowlist-based under HOME; it does not cover `/etc`, `/private/var/tmp` or unlisted directories under `/private/tmp`, so
never keep a secret there. Evidence the QA agent must cite has to be inside its read allowlist (the clone, `out`, or a
`--read-root`), or the criterion must be left out of its verdict (AASM, ASK #3: a criterion verified elsewhere was reported NOT_RUN by
the model, as it should).

### Run as a dedicated QA identity (SPE-142)

`run --qa-identity NAME --exchange DIR` runs the same hop as the unprivileged OS user that
`scripts/qa-identity-setup.py` created, so a QA tool command can no longer read the environment of the producer's
processes (a same-uid sysctl the Codex profile cannot deny). `--require-env-isolation` refuses any run without an
identity, for callers that must never fall back to the same-user mode. `--read-root` is refused with an identity (it names
directories of the producer's HOME).

**Launch it from your own Terminal.** Every hop uses `sudo -n` (never a password prompt) through the setup script's
terminal-detaching hop (`sudo -n -u NAME -H env -i HOME=/Users/NAME PATH=... python3 -I -c DETACH ...`). sudo's
credential cache belongs to the terminal that typed the password, so an agent session cannot use it. The owner runs:

```
sudo -v && python3 scripts/qa-handoff.py run --run-dir DIR --qa-identity NAME --exchange /Users/Shared/NAME-qa
```

No sudoers change is made or needed. A `sudo -n` refusal is `QA_INCOMPLETE` with codex not started.

Three hops, each through `qa_argv`, with the QA side running a producer-owned copy of this script under `/usr/bin/python3 -I`:

| Hop | Runs as | What happens | What the producer trusts |
|---|---|---|---|
| sweep | QA user | `kill -KILL -1` as that user, then `pgrep -U uid` (ids only): no QA process from an earlier run survives | the survivor count |
| 0 env canary | producer + QA user | producer holds a synthetic sentinel in a sibling's environment and proves it can read it itself (positive control); the setup script's probe runs as the QA user and must not read it, plus the uid, groups, tty, `~/.codex` mode and exchange checks | the probe record, emitted before any model; `ISOLATION.json` |
| 1 `qa-stage` | QA user | creates the outbox attempt `O`, copies the inbox clone to `O/source`, a private Codex home under `/Users/NAME/.local/state/coding-agent-environment/qa/<attempt>/codexhome`, and runs the confinement probe there; prints exactly one JSON record | the observed control results only: `established` is re-derived from the expected control set, never taken from the QA side; `CONFINEMENT.json` |
| 2 `qa-exec` | QA user | moves its stderr to `O/stderr.log`, runs `codex exec` with the prompt from the inbox file, kills codex's group on timeout or signal (exit 124 / 130) | the codex stdout captured through the pipe into `R/events.jsonl` |
| seal | producer | sweep again, `lstat` the exchange, outbox, `O` and `O/source` (real directories, expected owners), copy `O/verdict.json` only if a single-link regular file owned by the QA user and at most 5 MiB, compare `O/source` with the producer's manifest once | the sealed values in `RUNMETA.json` (`qa_identity`, `stragglers`, `path_ok`, `tree_problems`, `untracked`) |

Paths: `X` exchange, `R` the producer's run directory, attempt `A=<product>-<ticket>-<R name>-<8 hex>`, inbox
`I=X/inbox/A` (producer-owned, read-only to the QA user through the inherited ACE: script copy, packet, prompt, and a copy
of the verified clone that is deleted after hop 1), outbox `O=X/outbox/A` (QA-written, untrusted). `R` keeps only
producer truth: `ISOLATION.json`, `CONFINEMENT.json`, `events.jsonl`, `stderr.log` (sudo's), the sealed `verdict.json`
copy, `RUNMETA.json` (with the script copy's digest) and `RESULT.json`. `verdict` never reads `O` again, so editing or
deleting the outbox later changes nothing. `X/inbox/RUN.lock` is the identity lock: one run per QA user at a time
(the sweep kills every process of that user). A run killed with SIGKILL can leave it behind; remove it by hand once no QA
run is active.

Trust boundary: the producer trusts its own pipe capture (hashed and recounted as before), the hop 0 and hop 1 output
(emitted before any model runs, after the sweep), and the values it sealed. It never trusts `O` after the seal, codex's
stderr, or the QA side's own `established` flag.

| Condition | State |
|---|---|
| `--require-env-isolation` without an identity; identity with `--read-root`; identity is root or your own uid; bad name or exchange | `REFUSED` (64), nothing written |
| identity lock held by another run | `WAITING_RESOURCE` (75) |
| exchange owners or modes wrong; `sudo -n` refused; sweep could not run; a hop printed nothing; codex missing on the QA side; confinement could not be checked | `QA_INCOMPLETE`, codex not started |
| env probe ran and failed (leak, detector invalid, tty, uid, groups, `~/.codex` mode) | `QA_INVALID`, codex not started |
| confinement failed, malformed, incomplete or not exactly one record | `QA_INVALID`, codex not started |
| survivors after the seal's sweep; linked or foreign-owned outbox path; unreadable or linked verdict; sealed clone problems; `ISOLATION.json` or `CONFINEMENT.json` missing or not established | `QA_INVALID` |
| timeout (inner exit 124), interrupt, crash | the existing paths (`QA_INCOMPLETE`) |

What is verified offline: the producer's mapping and sealing, through the real hop code with an injected fake `sudo`
that runs the rest of the command as the test user (`tests/test-qa-handoff.py`, `IdentityTests`). Not verified by the
tests: a real QA account, real `sudo`, real ACL inheritance into the outbox, signal relay through setuid `sudo`, and real
codex as the QA user.

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
- File reads are confined by the permission profile (see Read confinement). In the same-user mode **environments of
  other processes of the same OS user stay readable** to a QA tool command (a sysctl the seatbelt cannot deny): a secret
  held in the environment of any same-user process, including your own sessions, is reachable and could be quoted in
  evidence. `--qa-identity` closes this (the QA run is a different OS user, proven per run by the environment canary);
  without it, do not run QA where an environment variable carries a secret you cannot afford to expose, and use
  `--require-env-isolation` where that must never happen.
- SIGTERM, SIGINT and SIGHUP to the wrapper are handled: codex's group is killed and a hashed interrupted record is
  written, so `verdict` reports `QA_INCOMPLETE` and a later edit is `QA_INVALID`. An interrupt only removes the chance of a
  pass: if the verdict codex had already written records a failing check or a Medium-or-higher finding, the state stays
  `QA_FAIL`, because a recorded defect is more useful to the producer than a retry. A signal that lands in the few bytecodes
  between the child being forked and `Popen` returning can leave codex unmonitored; the wrapper's exit then closes codex's
  stdin pipe, so it reads an empty prompt. SIGKILL of the wrapper cannot be handled at all and leaves no record.
  The run record itself is written with these signals blocked. Any signal received before the record is complete marks the
  run interrupted (never `QA_PASS`); one that arrives after it is complete leaves a finished run finished.
- After a clean codex exit the wrapper sends SIGKILL to the codex process group. If that group were fully gone and its id
  recycled by an unrelated group leader within the same moment, that group would be signalled; this is negligible in
  practice and not defended against.
- `--criteria` and `--qa-notes` are files the producer chooses. The prompt labels them producer-supplied and `RESULT.json`
  records `criteria_sha256`, but the wrapper does not verify them against the ticket; a producer that narrows the criteria
  narrows what a `QA_PASS` covers. A reviewer compares the hash and text with the ticket.
- A `QA_PASS` is the QA process's own report, bound to a hashed event stream a reviewer can audit. It is not a
  cryptographic attestation. In the same-user mode the producer and the QA process run as the same OS user; with
  `--qa-identity` they do not, but the run directory `R` is still writable by the producer's own user (and by any process
  of it), so the record protects against the QA process, not against the producer.
- The permission profile was measured on codex-cli 0.163.0 (macOS seatbelt, directory rules only, no globs). Another
  version or platform can behave differently; the pre-run probe is what detects that, so a failed probe is a stop, not a
  warning. Codex's own client (not sandboxed) still writes to the private home.
- QA-controlled text reaches the producer through `RESULT.json` and the console. It is stripped, capped and marked
  untrusted, but the producer must still treat it as data.
- The event stream is the `codex exec` process's own stdout. The wrapper counts only `command_execution` completions that carry a unique id (a model cannot write to that stream; text it prints stays inside an escaped string field on the verified codex version), but a process that can write the stream itself, such as a replaced or compromised codex binary, can forge items and obtain `QA_PASS`. Codex QA reproduced this only with the offline fake codex, which is that process. Defending against it needs an attestation the producer's own user cannot create; that is the open evidence-independence decision, not something this wrapper can close.
- In the same-user mode a sandboxed command that detaches into its own session (`setsid`) can outlive the timeout kill.
  With `--qa-identity` the seal kills every process of the QA user and a survivor is `QA_INVALID`. On a real hidden macOS
  account, per-user system agents (if any appear for that uid) would also count as survivors, so such a run could never
  pass; this has not been checked on a real account.
- Identity mode leaves every attempt's outbox in place: the producer cannot delete files the QA user owns, so outbox disk
  use grows until the owner removes old attempts (for example as the QA user, or through `--rollback` of the setup).
- Identity mode still trusts the codex binary the QA user runs: a replaced codex can forge its own event stream, exactly
  as in the same-user mode.
- The outbox read access is cooperative: the producer reads `O` through the ACE the outbox passes on. If the QA user
  strips it, the seal cannot read the clone or the verdict and the result fails closed (`QA_INVALID` or
  `QA_INCOMPLETE`), never a pass.
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
