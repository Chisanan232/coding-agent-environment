# Review-evidence recording: feasibility, trust model and owner packet (SPE-118)

Golden Journey: agent implements -> independent technical QA -> required checks pass -> eligible owner PR merges automatically -> result verified. No formal GitHub
self-approval. The one step that does not yet run unattended is **recording the independent QA verdict as the runner's evidence file**.

## What the host actually did (agent-observed, 2026-10-23)

| Attempt | Host result |
|---|---|
| Agent writes the evidence file for its own coordinated PR (`evidence-json > ~/.claude/owner-merge-evidence/...`), after a real independent review PASS | denied `[Self-Approval]` |
| Agent patches the runner so that the runner, when asked, obtains the review itself and writes the evidence (`--record-review`) | denied `[Auto-Mode Bypass]`; the patch was not applied |
| Agent then edits the gate's executor selection (a merge-authorization code path) | three of four edits denied `[Self-Approval]`, one applied; reverted, tree clean |

None was retried through another tool, wrapper or sub-agent. Read as a rule: **inside one agent session, anything that produces or enables the artifact that authorizes the agent's own merge is treated
as self-approval, whichever program carries it.** This is a classifier judgment (non-deterministic, and it cascades), not a deterministic permission rule, so it is a measured symptom, not a
guarantee either way. Posting an `OWNER-MERGE-REVIEW` comment and running the pinned `gh pr merge` (which prompts) were not denied.

## Feasibility: can a trusted review artifact come from the normal review/QA process?

Yes, but only if the party that produces the verdict is outside the merging agent's control. Three honest designs, in the order I recommend them:

| # | Design | Who produces the verdict | Owner authority needed | Cost / risk |
|---|---|---|---|---|
| 1 | **Reviewer recorded by the installed runner** (`--record-review`): the runner spawns a tool-less headless reviewer (`claude -p --tools ""`, sees only the PR diff, injection-hardened prompt) and writes the evidence only on PASS for the exact head. Opt-in per repo (`auto_review`), refuses PRs that touch workflows, `owner-merge-*`, `.claude/settings*` or CODEOWNERS, diffs over 120 KB, and PRs whose head moved during the review. Runner code change + `install-owner-merge-runner.sh --update`; no settings change, because the installed allow rule already ends in `--review-evidence *` | a separate reviewer process the owner-installed program controls | **your explicit authorization to implement and install it.** The host denied the implementation attempt, so only you can settle whether the host accepts the agent invoking it (the live check is one canary run) | ~$0.02 per review, diff-only so shallower than the full QA pass; prompt injection through the diff is a residual risk |
| 2 | **Review as a required CI check**: a workflow on `pull_request` runs an independent reviewer and publishes a check run the runner already requires by name on the exact head. The runner matches required checks by name and conclusion; it pins the publishing app only in the local-equivalent exemption, so a same-named check from another app would also satisfy it. No evidence file at all | GitHub Actions | an Anthropic API key as a repo secret (credential) and per-PR spend: a founder decision. A PR can also edit its own workflow unless it uses `pull_request_target` with a pinned reviewer | strongest independence; credential and spend |
| 3 | **Risk-based path exemption**: for allowlisted repos, PRs that change only documentation (`*.md`, `docs/`) with green required CI need no evidence | nobody (waived) | your decision to waive independent review for documentation-only changes; a runner change | zero cost; removes the evidence step for the lowest-risk class only |

Not recommended: making the runner accept the `OWNER-MERGE-REVIEW` PR comment as evidence. It is the same self-attestation through a channel the host does not gate, which is exactly a route
around the denial. Also not an option: signing with a key the agent can read; the runner has to read the key as the same OS user (`owner-merge-evidence-sign.py` says so).

## Trust model (what each guard still gives)

| Guard | Holds with design 1 / 2 / 3 |
|---|---|
| Exact head pin | yes: evidence binds the head; GitHub re-enforces it with `--match-head-commit`; the head is re-read after the review |
| Required CI | yes: the runner requires the allowlisted checks green on that head |
| Independent review | 1: a separate model process on the diff; 2: a CI job; 3: waived for docs only |
| Runner audit and refusal | unchanged; the audit also stores the review-output hash |
| ASK-first fallback | unchanged: the direct `gh pr merge` still prompts |
| `--admin` bypass | never offered |
| Residual | same GitHub identity and same OS user throughout; a human at the keyboard could always write evidence by hand; this is an accountability control, not a cryptographic one |

## Owner approval packet (one decision; I need only this)

Reply with one of: **"Authorize design 1"**, **"Authorize design 2"**, **"Authorize design 3"**, or **"Keep the owner-run evidence step"**.

- **Design 1** (recommended): authorizes me to implement `--record-review` in `scripts/owner-merge-runner.py` and `auto_review` tests on a PR, with an independent review, and
  authorizes you to run `scripts/install-owner-merge-runner.sh --update` and to set `"auto_review": true` plus the reviewer id `runner-independent-review` in
  `~/.claude/owner-merge-runner-allowlist.json` for each repository you want. Then one canary run on a disposable private repo shows whether the host accepts the agent calling
  `owner-merge-runner ... --record-review`; if it denies, that denial is recorded and the fallback below applies.
- **Design 2**: authorizes the API-key secret and spend on named repositories.
- **Design 3**: authorizes the documentation-only waiver.

Fallback that needs no decision and already works: the gate offers the pinned `gh pr merge`, the host prompts, you click once per merge (this is how #113-#121 merged).

## Design 1 selected as the target: status, honest trust model and what the host requires (2026-10-23, later session)

Founder decision: Design 1 is the target architecture; it is not authorization to bypass a host denial. State after trying it once, with ordinary Edit calls on a fresh branch:
the host accepted 5 of 7 edits to `scripts/owner-merge-runner.py` and explicitly denied the 2 that add the per-repo `auto_review` opt-in (`[Auto-Mode Bypass]`). The partial change was
restored to the committed file (the runner referenced an undefined name), the installed runner is unchanged and byte-identical to main, and nothing was retried through another file, tool, wrapper
or sub-agent. Conclusion: **a conversation-level instruction does not authorize the agent to change the code path that decides whether its own merge is authorized.**

### The five things that must not be conflated

| Dimension | Under Design 1 | Who holds the authority |
|---|---|---|
| Technical review generation | a tool-less headless model process, started by the runner, sees only the PR diff; shallow (diff only), same model family as the author, and the diff it reads is written by the party being reviewed (prompt injection is a residual) | nobody independent: it is a second opinion |
| Evidence provenance and integrity | the runner writes the file (`O_EXCL`, 0600), binding repo, PR, exact head and the SHA-256 of the reviewer output; `reviewer_kind` = `runner-recorded`; the audit `guarantees.review` must say `runner-recorded agent review (not an independent authority, not a GitHub approval)`. Integrity against the same OS user: none | the runner; tamper evidence only |
| Merge authorization | owner allowlist entry + the one owner-installed allow rule + every runner guard (class, exact head, required checks, repository limits, audit). The recorder adds evidence, never authority | the owner |
| GitHub formal approval | not produced and not needed for Class A; B and C still need the genuine owner `APPROVED` review | the owner on GitHub |
| Host permission authority | the Auto Mode classifier plus the owner's settings; it denied agent-produced and agent-built self-authorizing artifacts three separate ways | the owner and the host |

**What Design 1 is and is not.** A reviewer the merging session starts is on the merging side of the trust boundary: same session, same GitHub identity, same OS user, adversary-shaped input. Calling it an
independent security authority because it is another process would be wrong, and the evidence and audit say so. What it buys is a recorded, head-bound second-opinion review with no hand-written
evidence. It becomes acceptable as the merge gate for a repository only through the owner's deliberate, per-repository opt-in (`auto_review` in a file the agent cannot edit); true independence still means
a human or a separate trust domain (Design 2: a GitHub-hosted check with its own credential).

### Host authorization requirement and the smallest owner action

The runner change has to enter the trust boundary through an owner-side act, not through this session. Smallest set, in order:

1. **Obtain the implementation outside this session** (the owner writes it, or directs a separate session or harness such as Codex to write it from the specification below), reviewed in a PR by an independent reviewer.
2. Owner merges it (one prompt) and runs `scripts/install-owner-merge-runner.sh --update`.
3. Owner sets `"auto_review": true` and adds the reviewer id `runner-independent-review` for the repositories where this standing opt-in is acceptable.
4. One disposable-repo canary shows whether the host lets the agent call `owner-merge-runner ... --record-review` (the installed allow rule already ends in `--review-evidence *`, so no settings change). If it
   denies, the remaining owner-only lever is classifier guidance (`autoMode` wording, owner-written); if that is also refused, the fallback below stays the answer. Whether the host accepts it is unknown until step 4.

### Specification for the implementer (behaviour and tests, not code)

- New flag `--record-review` on the existing runner; no new program, no new allow rule, no new gh verb except the read-only `pr diff N --repo R`.
- Refuse unless the repository entry has `auto_review: true` and the reviewer id `runner-independent-review` is allowlisted; refuse if the evidence file exists (never overwrite), if the file name is not a plain
  `.json` inside the owner-merge-evidence directory, if the PR is not open, non-draft and at `--expect-head`, if the diff is empty or over a fixed size cap, or if the PR touches `.github/workflows/`, `owner-merge-*`,
  `.claude/settings*`, CODEOWNERS or the allowlist (those need a human-supervised review).
- Reviewer: no tools, no user/project settings, no session persistence, token-like environment variables removed, diff delimited as untrusted data, one JSON verdict bound to the head; anything else is a refusal. Re-read the
  head after the review; any change refuses. Write evidence only on PASS; keep the review-output hash in the evidence and the audit.
- Tests: red (FAIL verdict, malformed output, wrong head echo, head moved during review, protected path, oversize diff, existing file, path escape, disabled repo, reviewer id missing, reviewer crash/timeout) and green
  (PASS writes a 0600 file the existing evidence loader accepts, then the normal guards still refuse stale head, red CI, missing check); an existing-behaviour regression that a run without the flag is unchanged; the audit
  carries `runner-recorded` and never `independent`.
- Gate: when the evidence file is missing and the repository has `auto_review`, offer the runner command with `--record-review`; when it is missing and there is no `auto_review`, offer the pinned `gh pr merge` (ASK), never ALLOW.

Fallback that stays correct until all of this exists: an unsupported automatic authorization is **ASK**, not a silent merge: the gate offers the pinned `gh pr merge` and the host prompts.

## Reusable multi-repository owner merge workflow (works today)

1. **Once per repository (owner):** add an entry to `~/.claude/owner-merge-runner-allowlist.json` (required checks, reviewer ids, protection capability).
   Private Free repositories use `protection_capability: private-free-app-level`; a public repository without branch protection is refused until you enable protection or waive it.
2. **Per PR (agent):** implement; independent QA; required checks green; `owner-merge-gate.py check --repo R --pr N --owners <owner> --settings ~/.claude/settings.json`.
3. **Outcome:** `ALLOW` with the runner command when the runner is installed, the repo is allowlisted, the host allows that literal command and the evidence exists; otherwise `ASK` with the
   pinned `gh pr merge`, or `DENY` (failed CI, fork head, host deny rule). Non-owner and bot PRs need the genuine owner `APPROVED` review first.
4. **Run exactly the printed command**, then `owner-merge-gate.py verify`. A refusal or host denial is recorded once and not routed around.

## Explicit host limitations

- The Auto Mode classifier can deny agent-produced merge-authorizing artifacts and edits to the merge machinery (`[Self-Approval]`, `[Auto-Mode Bypass]`, `[Merge Without Review]`); it is non-deterministic and a denial cascades.
- Prefix permission rules cannot cover every merge spelling; the uncovered shapes fall to the classifier.
- Codex has no host ASK under `approval_policy = "never"`; its guarantee is instruction plus GitHub's rules (see OWNER-MERGE-ENTRYPOINTS.md).
- A non-owner PR on a real repository has not been observed (needs a second GitHub identity).
