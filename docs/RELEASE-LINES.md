# Release lines

Status: slices 1 and 2 of the SPE-180 design (SPE-181a registry and routing, SPE-182 successor train). This page documents what `scripts/release-line.py` implements today and lists
later slices as planned. Nothing here changes the merge gate.

## Why

A product can ship an old major while the next version is already in development, and a late fix to a predecessor must reach
every newer line. The release-line registry names those lines, says which branch each one integrates on and which branch it
publishes from, and lets tools route work and (in later slices) account for forward-ports and promotions. It is data plus
validation; the owner stays the only author of the trust file.

## Data model

`release_lines` is an optional key on a repo entry of the owner policy
`~/.config/coding-agent-environment/local/version-branches.json`. `scripts/owner-merge-gate.py` reads only `branches` and
`future_head_prefixes` and ignores every other key (pinned by a gate test). Whether a branch can receive merges is decided by
`branches` alone. See `docs/examples/version-branches-release-lines.example.json`.

Repo level: `schema` (1), `audit {status PENDING|COMPLETE, source, observed_sha256}`, `channel` (`github-release`),
`publication_go` (`per-version`), `milestone_tags` (`go-required` or `pre-authorized`), `tag_format` (e.g. `v{version}`, or
`MVP-{version}` for MVP products), `candidate_format` (e.g. `v{version}-rc.{n}`), optional `build {argv, outputs}`, and `lines`.

Line kinds:

- `major`: one per major (`major` integer), `predecessor: null`.
- `successor`: a version developed ahead of its predecessor. Has `version`, `predecessor {line, version, fork_ref, fork_sha}`,
  and `state` `active`, `converged` (with `converged_into`) or `retired`.
- `version`: legacy per-version integration branches (`mvp-4.0/HORO-2046/integration`, `integration/mvp-5.0`).

Every line has `integration_branch`, `publication_base {branch, kind default|maintenance}` and `support` `supported|eol`.

Validator rules (registry validate, and every candidate the tool emits):

- every `integration_branch` must be listed in `branches`, otherwise the line is `NOT_MERGE_ELIGIBLE`;
- a `maintenance` publication base must not be in `branches` (promotion never goes through the gate);
- `promote/` must be in `future_head_prefixes`;
- no branch may have a `release` path segment: the gate refuses those and one such entry fails the whole policy file to load;
- a repo without `release_lines` is `UNREGISTERED`; nothing is inferred from names;
- `audit.status: PENDING` makes every mutating command refuse `AUDIT_PENDING`; read-only commands still work.

## Decisions

- D1 naming: the default integration branch is `integration/v<major>.x`, not `release/v<major>.x`, because the gate refuses any
  `release` segment. Legacy per-version names stay valid through `kind: "version"`.
- D2 successor eligibility: each successor listing is its own owner approval in the policy file. There is no gate change and no
  name inference.
- D3 milestone tags: `milestone_tags` defaults to `go-required`. Final tags, promotion and publication always need a separate GO
  record.

## Authority split

- Merge eligibility is the gate's job and comes only from `branches` in the owner policy.
- Publication authority is a separate per-version GO record under
  `~/.config/coding-agent-environment/local/release-go/<owner>__<repo>__<version>.json`, read through the same trust-anchor
  reader. Fields: `repo`, `version`, `candidate_tag`, `candidate_sha`, `publication_base`, `scopes`
  (`candidate_tag|promote|final_tag|publish`), `authority` (citation), `created_utc`, optional `expires_utc`. A record whose repo
  or SHA mismatches is refused. GO records and the promotion/publication commands belong to later slices; `--go-dir` is accepted
  now and unused.
- Agents never write the owner files. `registry propose` and `audit import` emit a candidate policy file, the sha256 of the
  baseline and of the candidate, and an authority-record template (same shape as `nda-v004-version-authority.json`:
  `authority`, `scope`, `baseline`, `baseline_sha256`, `candidate`, `candidate_sha256`, `source_commit`, `canonical_loader`,
  `independent_review`, `created_utc`). The owner cites the founder instruction, reviews and installs the candidate.

An audit file may not carry `branches` or `future_head_prefixes` (refused, status FAIL, exit 1, nothing written) and no line may use a
`release` branch segment. The only gate-relevant additions an import proposes are the imported lines' unlisted `integration_branch`
values and `promote/`. Every proposal lists its additions explicitly (`gate_relevant_additions` in the step evidence, the summary
and the authority-record `scope`) so the owner sees what the approval changes.

## Owner packets

Proposals are written only with `--execute` (without it the command prints a dry plan with the digests) to
`~/.local/state/coding-agent-environment/release-line/<owner>__<repo>/proposals/` (0700 directory, 0600 files), or `--state-dir`.
A packet is `<repo>-<op>-<sha12>.candidate.json` plus `.authority.json`. `canonical_loader: PASS` in the template means the
candidate loaded in the real `load_version_policy` and passed the release_lines validator.

## Running the CLI

Call it by absolute path from the maintained checkout. It is stdlib only and clean under `python3 -I`.

```
R=/Users/bryant/Bryant-Developments/coding-agent-environment-main-view/scripts/release-line.py
python3 -I $R registry validate --repo OWNER/REPO
python3 -I $R registry show --repo OWNER/REPO
python3 -I $R registry propose --op add-successor --repo OWNER/REPO --version 0.5.0 --predecessor v0.4.0 \
    --predecessor-version 0.4.0 --fork-ref v0.4.0-rc.1 --fork-sha <40hex> [--branch integration/v0.5.0] --execute
python3 -I $R registry propose --op retire|eol|converge --line ID [--into ID] --repo OWNER/REPO --execute
python3 -I $R route --kind feature|fix --version 0.5.0 --repo OWNER/REPO --clone PATH
python3 -I $R audit observe --repo OWNER/REPO --clone PATH
python3 -I $R audit import audit.json --repo OWNER/REPO --execute
```

Common flags: `--repo`, `--clone`, `--policy` (must live under the owner config directory), `--go-dir`, `--state-dir`, `--execute`.
Stdout is one JSON object: `schema`, `tool_revision` (HEAD of the tool's checkout), `command`, `repo`, `idempotency_key`,
`status` (`PASS|FAIL|BLOCKED|NOT_RUN|DEFERRED`), `steps [{step, state, evidence}]`, `next_action`.
Exit codes: 0 ok or no-op (a dry plan is `NOT_RUN`, exit 0), 1 refused or blocked, 2 usage, 3 invalid config.

`route`: the target is the line registered for the exact version, else the major line. A major line with an unshipped candidate
(rc tag merged into its branch and no final tag) is frozen for features: fixes still go to the major line, features go to the
newest active successor, or the result is `NEEDS_SUCCESSOR` (status BLOCKED, only that work is blocked). Converged lines route to
`converged_into`; retired and eol lines refuse.

`audit observe` is read-only: branches, tag patterns, PR base counts and merged `promote/` PRs (via `gh pr list`; without a
usable `gh` that step is `NOT_RUN` and the git facts still stand), and release-related doc paths. `audit import FILE` takes an
audit JSON `{schema: 1, repo, release_lines}`, validates it, and emits a candidate whose
`audit` is `COMPLETE` with `source` and the sha256 of the audit file. That is what ends `AUDIT_PENDING`, once the owner
installs the candidate.

## Successor train (SPE-182)

All four commands need `--clone` (a checkout whose `origin` is the product repo), refuse `AUDIT_PENDING`, fetch `origin` first,
default to a dry plan, and only with `--execute` push or open PRs. Pushes create new refs only (`git push origin <sha>:refs/heads/<name>`,
never force) and are verified with `ls-remote`. Durable state is git and GitHub: branch names, tags and PR heads; retries recompute it
and create nothing twice. The tool creates no commits (merges are GitHub merge commits made by the normal gate); the PR bodies it opens
carry `Release-Line-Kind: integrate|converge` plus source/target line trailers.

```
python3 -I $R successor ensure --predecessor 0.3.0 [--from REF] [--version 0.4.0] [--branch B] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R guard --base BRANCH --head SHA --repo OWNER/REPO --clone PATH
python3 -I $R train integrate --from LINE --into LINE --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R train converge --line LINE --repo OWNER/REPO --clone PATH [--execute]
```

`successor ensure` (key `succ:<repo>:<version>`): the predecessor line is the line carrying that version (else the major line); the
successor version defaults to the next minor. The fork is `--from REF` or the predecessor's highest candidate tag, and must be reachable
from the predecessor integration branch (so v0.5.0 forks from the v0.4.0 candidate it needs). The target is `integration/v<version>` or the
registered name. `ls-remote` absent: push the fork SHA, `CREATED`; present and containing the fork: `REUSED`; otherwise `CONFLICT_EXISTING`
(status BLOCKED, nothing pushed). Unless the line is already registered, it also emits an owner packet through the `registry propose`
emitter (branch for `branches`, the line with `fork_ref`/`fork_sha` and predecessor, the `v<version>/` head prefix). The registry state stays
`PENDING_OWNER_LISTING` until the owner installs it; the new branch cannot receive gated merges before that.

`guard` (read-only; BLOCKED, exit 1, on a finding): `SUCCESSOR_ONLY_COMMITS` when a PR head into line P carries commits that exist only on an
active successor of P (transitively); `EOL_BASE`; `CONVERGE_PRECONDITION`; `PUBLICATION_BASE_DIRECT_CHANGE` is advisory (status stays PASS).
The one exception is a converge: a head equal to the tip of the major line's successor chain, into the major line, is `CONVERGE_ALLOWED` when
every version was shipped and integrated, else `CONVERGE_PRECONDITION`.

`train integrate --from LINE --into LINE` (key `int:<repo>:<from>:<into>:<from_final_tag>`): `into` must be a successor of `from`. Needs the final tag
of the predecessor version (`NO_FINAL_TAG` otherwise); the promoted candidate is the final tag commit's second parent (or the commit itself when
not a merge) and must be on `from`'s branch. If the predecessor line is itself a successor, its own predecessor must already be integrated
(`OUT_OF_ORDER`). The head `lineint/<from_version>/<into_line>` is pinned at the candidate SHA (a different existing SHA is
`CONFLICT_EXISTING`), a clean merge into the successor is predicted with `git merge-tree` (`MERGE_CONFLICT` pushes nothing), and one PR into the
successor branch is opened or reused (status DEFERRED: the merge commit goes through the normal gate, never through this tool). A rerun after the
merge verifies the candidate is an ancestor of the successor (`INTEGRATED`). Rule recorded as `stale_candidates`: any successor candidate tag cut
before integration is `PROMOTION_BLOCKED` and must not be promoted; promotion itself is a later slice.

`train converge --line LINE` (key `conv:<repo>:<line>`): needs every version of the chain from the frozen major candidate through the tip's
predecessor to have a final tag and to be integrated, in order (`CONVERGE_PRECONDITION` lists the gaps; the tip's own version need not have shipped). It
opens or reuses the PR from the tip successor branch into the major integration branch (DEFERRED), and after the merge reports `CONVERGED` and emits one packet
that marks every chain line `converged` with `converged_into` the major line.

## Planned (not implemented)

Later slices of SPE-180: forward-port plan/run/status with trailers (SPE-183); release candidate-tag/promote/verify/final-tag (SPE-184a);
build/notes/publish (SPE-184b); protection snapshot/verify, skill packaging and install (SPE-181b); acceptance from installed entry points
(SPE-185). None of these commands exist yet.
