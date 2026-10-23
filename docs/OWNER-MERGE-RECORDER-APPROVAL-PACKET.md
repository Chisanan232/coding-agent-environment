# Owner approval packet: runner-recorded review (Design 1) (SPE-118)

Status: **prepared for the owner; nothing in this packet is applied, installed or authorized.** Written 2026-10-23 against main `d286f93`.
Behaviour and test specification: `OWNER-MERGE-EVIDENCE-RECORDER.md`, section "Design 1 selected as the target". This packet adds the authority delta, the change
boundary, the security review, installation and rollback.

## 1. Two separate changes, two separate gates

| | Change A: ordinary engineering | Change B: merge-authority change |
|---|---|---|
| What | docs, gate selection tests, skill and instruction text | `scripts/owner-merge-runner.py` (decides whether a merge is authorized), the installed copy, the allowlist, `~/.claude/settings.json` |
| Who may author | the agent, through the normal PR flow | not the agent in this session: the host denied it twice (`[Self-Approval]`, `[Auto-Mode Bypass]`) |
| Who authorizes | the PR gate; merge by the pinned ASK command | the owner, outside this session |
| Installation | merged to main only | owner runs `scripts/install-owner-merge-runner.sh --update`, owner edits the allowlist |

Main already carries every Change A item. Change B is the whole remaining gap. Those host denials are binding, and the packet does not route around them: no patch file,
second session, other harness or wrapper is used to apply Change B on the owner's behalf. The owner decides who writes it (the owner personally, or a session the owner
directs and reviews); the agent in this session does not apply it.

## 2. New authority granted, stated exactly

Today the runner merges only when valid, head-bound review evidence already exists, written by something other than the merging agent. Design 1 adds:

1. The runner starts a review of the PR diff by itself and, on PASS, writes the evidence file itself. The merge call and the evidence author become the same process.
2. That evidence says `reviewer_kind: runner-recorded`, reviewer id `runner-recorded-review`. It is a head-bound second opinion. It is **not** an independent authority and
   **not** a GitHub approval; the audit line must say so.
3. Per repository opt-in (`auto_review: true`) in the owner's allowlist. Default off. A repository without it keeps today's behaviour (ASK).

Not granted: Class B and C PRs (they still need a genuine owner `APPROVED` review at the exact head), protected paths (refused), bypassing required checks, any
new merge method, any other command.

## 3. Change boundary (hunk map, not a diff)

An exact diff is deliberately not drafted here: the denied edit was exactly that diff, and reproducing it as text in this packet would hand the same change to a
different channel. The reviewer instead checks the owner's implementation against this map. Allowed hunks in `scripts/owner-merge-runner.py`:

| Hunk | Constraint |
|---|---|
| `REPO_KEYS`, `REVIEWER_KINDS` | add `auto_review`; add kind `runner-recorded`; no other key or kind |
| arg parsing in `main` | add `--record-review`; keep `--review-evidence` required so the installed allow rule still matches |
| new reviewer step | tool-less headless reviewer (`claude -p --tools "" --setting-sources "" --no-session-persistence`), diff size cap, timeout, PASS only on an explicit machine-readable verdict; any other output is a refusal |
| evidence write | only after PASS; head re-read after the review and compared again; path inside the evidence directory; mode 0600 |
| audit | PENDING record before the review, kind recorded as `runner-recorded`, text "not an independent authority, not a GitHub approval" |

Everything else in the file must be byte-identical. Any hunk touching `decide`, class detection, the required-check match, `--match-head-commit`, or the merge argv is
out of boundary and fails the review.

## 4. Settings fact the owner must weigh

The one Trusted ALLOW rule ends in `--review-evidence *`. A glob there also matches anything written after the evidence path, so a new flag is covered by the
existing rule without any settings change. That keeps the installation small, and it also means the rule's scope silently grows when the runner grows. The packet therefore
requires the runner to refuse any flag combination other than the documented ones, and the owner may choose to tighten the rule (owner-only edit, not made here).

## 5. Security review (design-level; the implementation still needs its own)

| Threat | Control required | Residual |
|---|---|---|
| The author controls the reviewed diff and can write text meant to steer the reviewer (prompt injection) | reviewer sees diff as data; protected paths refused; PASS needs explicit verdict; required CI still runs | a weak reviewer can be fooled; this is a second opinion only |
| PR edits `ci.yml` so checks pass | protected-path refusal covers `.github/`, the runner, the gate, settings and allowlist | check named `ci` can still be a commit status; documented earlier |
| Head moves after review | head re-read after the review; `--match-head-commit` at merge | none beyond GitHub's guarantee |
| Evidence tampering | evidence written by the runner only, 0600, in the owner evidence directory; `require_signed_evidence` unchanged | same OS user can still read the HMAC key; not an independent boundary |
| Runner edited then installed | installed copy is separate; only the owner's `--update` changes it | the install step is the real authorization |
| Spend or hang | per-call cost about $0.02, timeout, diff cap | negligible |
| Self-approval optics | audit and evidence label it `runner-recorded`, never `independent-agent` | host may still deny the agent calling `--record-review` (see 7) |

## 6. Test coverage required before the owner installs

Wire into `capability-validation.yml`; use scratch HOME fixtures and a stub reviewer, no live model:

PASS records evidence and merges; FAIL, malformed, empty or timed-out reviewer output refuses; head moves mid-review refuses; protected path refuses; `auto_review`
absent refuses; unknown flag refuses; evidence kind `runner-recorded` rejected when `auto_review` is false; Class B and C PRs still need the owner approval; every existing
runner test unchanged and green; audit line text asserted; a mutation test that removes the head recheck must fail.

## 7. Installation, host mechanism and rollback

Owner steps, in order:

1. Choose the author for Change B and have it reviewed as a normal PR against section 3. The merge of that PR is one ASK prompt.
2. Run `scripts/install-owner-merge-runner.sh --update`. The previous installed file is kept as the rollback copy.
3. In `~/.claude/owner-merge-runner-allowlist.json` set `"auto_review": true` and add reviewer id `runner-recorded-review` for each opted-in repository.
4. Run one disposable-repo canary: an owner PR, real CI, then `owner-merge-runner … --record-review`, no prompt, `verify` reads back the merge commit.

Host mechanism (what is documented and what is not): the supported persistent mechanisms are the owner's `permissions.allow` rule, which already exists for the exact
runner spelling, and owner-written `autoMode.allow` wording. Both are owner-only edits and the agent does not author them. Whether the Auto Mode classifier accepts the agent
calling `--record-review` is **unmeasured**; only the canary in step 4 can show it. A denial there is recorded once and the journey falls back to ASK.

Rollback: restore the saved installed copy, set `auto_review` false or remove it from the allowlist (default off), and the system is exactly today's behaviour. Settings are unchanged
by the install, so there is nothing to roll back there.

## 8. Residual risks, plainly

The reviewer is on the merging side of the boundary; evidence is self-attested unless signed with a key the same user can read; all parties share one GitHub identity and
OS user. Design 1 removes manual evidence writing, not the need for trust in the owner's own tooling. True independence still needs a human or the CI-check design.
Unsupported automatic authorization stays **ASK**, never a silent merge.
