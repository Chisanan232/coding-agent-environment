# Release lines

Status: slice 1 of the SPE-180 design (SPE-181a). This page documents what `scripts/release-line.py` implements today and lists
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

## Planned (not implemented)

Later slices of SPE-180: successor ensure, guard, train integrate/converge (SPE-182); forward-port plan/run/status with trailers
(SPE-183); release candidate-tag/promote/verify/final-tag (SPE-184a); build/notes/publish (SPE-184b); protection snapshot/verify,
skill packaging and install (SPE-181b); acceptance from installed entry points (SPE-185). None of these commands exist yet.
