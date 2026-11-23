# Release lines

Status: slices 1 to 3 and 6 of the SPE-180 design (SPE-181a registry and routing, SPE-182 successor train, SPE-183 forward-port, SPE-181b skill). This page documents what `scripts/release-line.py` implements today and lists
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

`successor ensure`, `train integrate` and `train converge` refuse `AUDIT_PENDING`; `guard` is advisory and does not. All four need `--clone` (a checkout whose `origin` is the product repo) and fetch `origin` first (a failed fetch refuses `FETCH_FAILED`, never proceeds on a stale clone),
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

`guard` (changes no remote state, but fetches into the clone; BLOCKED, exit 1, on a finding): a failed `rev-list` refuses `REV_LIST_FAILED` instead of reporting clean. `route` skips successors that are themselves frozen by an unshipped candidate and returns `NEEDS_SUCCESSOR` when every active successor is frozen. If a human resolves a `train integrate` conflict by pushing onto the `lineint/` head, open that PR manually; a rerun then reports `INTEGRATED` once it merges. `SUCCESSOR_ONLY_COMMITS` when a PR head into line P carries commits that exist only on an
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

## Forward-port (SPE-183)

```
python3 -I $R forward-port plan   (--merge SHA | --pr N | --range A..B) --repo OWNER/REPO --clone PATH
python3 -I $R forward-port run    (--merge SHA | --pr N | --range A..B) --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R forward-port status (--merge SHA | --pr N | --range A..B) --repo OWNER/REPO --clone PATH
```

A maintenance fix that landed on a line is accounted against every strictly newer line (major ascending, then the successor chain), including
successors chained from a temporary successor. A line is never a target when it is `support: eol`, `state: retired|converged`, or a publication
base; those are listed under `excluded:<src12>`. A source that landed straight on a publication base targets every line published from it and
every newer one. `--merge` takes a GitHub merge commit (the work is the second-parent range `M^1..M^2`, since GitHub merge commits carry no
trailers) or a plain commit; `--pr` resolves the merge commit through `gh pr view`; `--range` accounts for every first-parent commit.

Each target ends in one state: `forward-port` (a port commit naming the source is on the target), `equivalent`, `not-applicable`, `pending`
(`TO_PORT`, `HEAD_PUSHED_NO_PR`, `PR_OPEN`, `NOT_STARTED`) or `conflict` (`BLOCKED_CONFLICT`, `CONFLICT_EXISTING`, `STALE_HEAD_CONFLICT`,
`NO_BRANCH`). The evidence order per target is: source already an ancestor (`CONTAINED`); a `Release-Line-Source` trailer on the target
(`PORTED`, or an `equivalent`/`not-applicable` record written in a reviewed PR); equal patch-id since the merge base (`EQUIVALENT_PATCH`); an
open PR from the fixed head; otherwise a `git merge-tree` replay of the source's net change (conflict, `EQUIVALENT_CONTENT` when the replayed tree
equals the target tree, else `TO_PORT`). The `accounting:<src12>` step is recomputed on every run, never stored; `complete` is true only when every
target is terminal. Targets are independent: a conflict on one never stalls the others.

Idempotency key `fp:<repo>:<source_sha>:<target_line>` (per target, in the evidence; the envelope carries `fp:<repo>:<source_sha>:*`), head
branch `fwdport/<target_line>/<src12>`. Only `run --execute` acts: it builds one port commit on the target tip with `merge-tree` and `commit-tree`
(the clone's working tree and index are never touched, so a conflict leaves nothing dirty and nothing pushed), keeps the original author,
pushes the new ref (`git push origin <sha>:refs/heads/<head>`, never force), requires `ls-remote` to show that SHA (else `CONFLICT_EXISTING`),
and opens the PR with `gh pr create` (merge commit through the normal gate; the merge itself is never done here). Trailers on the port commit and
PR body: `Release-Line-Source` (always the original source sha), `Release-Line-Source-Line`, `Release-Line-Target-Line`,
`Release-Line-Kind: forward-port`, `Release-Line-Reason`. A retry recomputes from origin: an existing head carrying the port is reused (a missing PR is
opened), and a head that fell behind its target is brought up to date by merging the target into it (a fast-forward push). A head that does not carry
this source's port is `CONFLICT_EXISTING`.

Loop prevention: a commit is not a source (state `LOOP_PREVENTED`) when it or any commit of `M^1..M^2` carries a `Release-Line-Source` trailer, and a
merge whose second parent is already on another registered branch without the merge itself is `INTEGRATION_MERGE` (integrate, converge or promote), so
ports never ping-pong between lines. Inapplicability needs a reviewed PR with an empty commit carrying `Release-Line-Kind: not-applicable` and
`Release-Line-Reason`; a hand-written equivalent fix carries `Release-Line-Kind: equivalent`.

Exit codes as everywhere: a dry plan with pending work is `NOT_RUN` (0); `run --execute` with PRs awaiting the gate is `DEFERRED` (0); nothing left
is `PASS` (0); any `conflict` is `BLOCKED` (1). `plan` and `status` never act (`--execute` is ignored); `status` skips the conflict replay and so
reports unstarted targets as `NOT_STARTED`. `run --execute` takes an flock and appends to `journal.jsonl` in the confined state directory (0700/0600,
never a symlink, never under the owner config root); the journal is an audit trail only. If `gh` is unavailable `plan` and `status` report
`pr_lookup: NOT_RUN` and `run` refuses. Needs git 2.40 or newer (`merge-tree --merge-base`).

`docs/templates/workflows/release-line.yml` is a template for the product repository (not active here): on a merged PR into an integration or
maintenance branch it runs `forward-port run --execute --pr N`, posts the accounting to the job summary and the PR, reads the owner mirror from the
Actions variable `RELEASE_LINES_JSON` and reports `NOT_RUN` when it is missing. It has no publish, tag or merge step and does not dispatch workflows.

## Release: candidate-tag, promote, verify, final-tag, protection (SPE-184a)

```
python3 -I $R release candidate-tag --version V [--line L] [--sha SHA] [--n N] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release promote       --version V [--line L] [--local-first FILE] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release verify        --version V [--line L] [--local-first FILE] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release final-tag     --version V [--line L] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R protection snapshot|verify --branch B --repo OWNER/REPO [--execute]
```

All default to a dry plan and refuse `AUDIT_PENDING`. Publication approval is a per-version GO record,
`~/.config/coding-agent-environment/local/release-go/<owner>__<repo>__<version>.json` (`--go-dir` must stay under the owner config root), read through the
trust-anchor reader: `repo`, `version`, `candidate_tag`, `candidate_sha`, `publication_base`, `scopes` (`candidate_tag|promote|final_tag|publish`),
`authority`, `created_utc`, optional `expires_utc`. Refusals: `GO_MISSING`, `GO_INVALID` (untrusted or malformed), `GO_MISMATCH` (repo, version,
publication base, candidate tag or SHA differ), `GO_EXPIRED`, `GO_SCOPE`. The tool never writes a record and never prints its contents. With
`milestone_tags: pre-authorized`, `candidate-tag` needs no record.

`candidate-tag` (key `tag:<repo>:<tag>`): an annotated tag per `candidate_format` on the integration tip, or `--sha` (must be reachable from it). Only a new
ref is pushed (verified with `ls-remote`); the same tag at the same commit is `REUSED`, at another commit `CONFLICT_EXISTING`. Tags are never moved or deleted.

`promote` (key `promote:<repo>:<version>:<candidate_sha>`; needs git 2.38+ for `merge-tree --write-tree`): preconditions are a supported, active line; every
predecessor shipped and integrated, and the candidate cut after that integration (`PREDECESSOR_NOT_SHIPPED`, `PREDECESSOR_NOT_INTEGRATED`, `STALE_CANDIDATE`);
for a maintenance base the latest same-major final tag is on the base and the version is greater (`BASE_MISSING_FINAL`, `VERSION_NOT_GREATER`); forward-port
accounting complete for every base-only change (`FORWARD_PORT_INCOMPLETE`); and the GO `promote` scope. It predicts `merge-tree` (conflict is `MERGE_CONFLICT`,
BLOCKED, nothing pushed), pushes `promote/<version>` at the candidate SHA (any other SHA is `CONFLICT_EXISTING`), opens or reuses the PR to the
publication base, checks hosted CI, then with `--execute` runs `gh pr merge N --merge --match-head-commit <candidate_sha> --repo OWNER/REPO` as its own call (never
`--admin`, `--squash`, `--rebase` or `--auto`). The final SHA is the PR's `mergeCommit.oid`; the tool asserts `final^2 == candidate` and `final^{tree}` equals the
`merge-tree` result for `final^1` and the candidate (`PROMOTION_ASSERTION_FAILED` is FAIL). A rerun after the merge reports `ALREADY_PROMOTED` and merges nothing.
Verification is `CARRIED_OVER` when the final tree equals the candidate tree, otherwise `REQUIRED`.

Hosted CI: `CI_PASS` needs every check required by the owner-pinned protection snapshot (`required_status_checks` contexts and checks) to be `SUCCESS` in the PR's rollup, or, when none are required, at least one `SUCCESS`; an empty rollup, an all-`SKIPPED`/`NEUTRAL` rollup or a missing required check is `NOT_RUN`. A forged commit status with a required name is a residual risk the tool cannot see. `CI_PASS` proceeds, a failing check is `CI_FAILED` (FAIL, never bypassed), pending checks are `DEFERRED`. When no hosted check ran (outage, quota, none
configured) the step is recorded `NOT_RUN` and promotion needs a local-equivalent record that passes the gate's own `local_first_basis` (private repo, the
unedited and unminimized `OWNER-MERGE-LOCAL-CI` comment of the authenticated user on the promotion PR, strict `parse_local_record` schema, repo/PR/base/base
commit/head/context match, commands and `env` equal to the owner's local-first entry, not finishing after it was posted or after now+60s, within `max_age_hours`;
as in the gate, an unexplained empty rollup also needs a skip directive on the head commit and no Actions run). The posted record must equal the private recorder
evidence, which the owner must also pin: `release-verification.json` `records.<repo>.<PR head sha>` holds its sha256 (`OWNER_PIN_MISSING` otherwise), because a file in the evidence directory is something the agent can write. The evidence is read only from the fixed evidence directories and re-verified with `verify_standing_local_record` (owner, mode, single link, log digests); there is no
caller-chosen evidence directory (`LOCAL_EVIDENCE_REQUIRED` otherwise, with the gate's reason codes). No workflow is dispatched or rerun.

`verify`: `CARRIED_OVER` when trees match; otherwise it needs `--execute` and runs the owner's local-first checks in a detached worktree at the final SHA
(`PASS`, `FAIL`, or `NOT_RUN` when the trust file is unavailable). It writes the recorder-format record for the final SHA (PR number, base, merge base `final^1`) into the fixed
private evidence directory; that record, not the journal, is what `final-tag` trusts. A failing or tree-changing run replaces or removes any earlier pass.

`final-tag`: GO `final_tag`; the final SHA is found from the promotion merge (cross-checked with the PR's merge commit), never the base tip, and must be on the
publication base. When the final tree differs from the candidate tree it re-checks the private record for that exact SHA the way `promote` does (fixed directories, log digests,
owner commands and env, freshness) and additionally requires the owner to have pinned the record's sha256 under `records.<repo>.<final_sha>` in
`~/.config/coding-agent-environment/local/release-verification.json` (read through the trust-anchor reader; `release verify --execute` prints the digest), so a file the agent can write
is never enough (`FINAL_NOT_VERIFIED` otherwise). Journal rows are an audit trail and never authorize anything. Every freshness, staleness and GO-expiry decision uses the real wall
clock; `SOURCE_DATE_EPOCH` only affects reproducible output stamps. An annotated
`tag_format` tag is pushed as a new ref. The journal records source branch, release line, version, promotion PR, candidate and final SHAs.

`protection snapshot` records the publication base's protection (read-only `gh api`, URL fields removed) in the state directory; the snapshot is trusted only
when the owner pins its sha256 under `snapshots.<repo>.<branch>` in `~/.config/coding-agent-environment/local/release-protection.json` (`SNAPSHOT_UNPINNED`,
`NO_SNAPSHOT`, `SNAPSHOT_TAMPERED`). `protection verify` reports `PROTECTION_DRIFT` (FAIL) with the owner-run `branch-protection-rollout.py` rollback; `promote --execute`
refuses without a pinned snapshot and compares before and after the merge. The tool never changes protection.

Further guards (SPE-184a review): every release command ties the clone to `--repo` (`ORIGIN_MISMATCH`): all fetch and push URLs of `origin` must be github.com/`<repo>` and are parsed, not pattern-matched: `https://` or `ssh://` with the host exactly `github.com` (lowercase, no port,
no query or fragment, userinfo limited to `[A-Za-z0-9._~%!$&'()*+,;=:-]`, no whitespace, control character or backslash anywhere), or the scp form `git@github.com:owner/repo` (the colon is required: `git@github.com/o/r` is a local path to git). Anything else, including `ext::` URLs, is refused before git runs; a local path is accepted only the owner listed for the repo in `~/.config/coding-agent-environment/local/release-origins.json` (`{"schema":1,"origins":{"<repo>":["/abs/path"]}}`, trust-anchor
reader; that file is also how the test harness admits its bare origin, there is no flag or environment override). The check runs in every handler that uses `--clone` against origin: `route`, `audit observe`, `successor ensure`, `guard`, `train integrate`, `train converge`, `forward-port`, and all `release` commands (`registry propose` and `audit import` never touch a clone). `git` is also run with `GIT_CONFIG_NOSYSTEM=1` and defense-in-depth `-c` overrides (proxy, ssh command, credential helpers, askpass, `ext` transport, TLS verification, CA file and path, extra headers, resolve overrides, upload/receive-pack programs, gpg program and signing, `log.showSignature`, `merge.renormalize`). The clone's own config is not trusted at all: `require_clone` reads `git config --local --list -z` first (before any other git call, system and global config neutralised) and refuses with `CLONE_CONFIG_HOSTILE` any key outside a short allowlist: `core.repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks`, `remote.<name>.url|fetch|pushurl`, `branch.<name>.remote|merge|rebase|vscode-merge-base|github-pr-owner-number`, and `user.name|email`. Everything else (hooks, `core.pager|editor|fsmonitor|sshCommand|askPass`, `merge.*.driver`, `diff.*`, `filter.*`, gpg and signing keys, every `http.*`, `credential.*`, `alias.*`, `url.*.insteadOf`, `include*`, `extensions.*`, `gc.*`, `pull.*`) is refused, so a clone with git-lfs filters or personal settings must be cleaned or a fresh clone used. Every remote's `url` and `pushurl`, not only origin's, must pass the same origin check. The user's global `~/.gitconfig` is intentionally trusted (it carries the committer identity, and is owner-controlled, unlike the clone). Every git call runs with `--no-replace-objects` and `GIT_NO_REPLACE_OBJECTS=1`, and a clone that carries any `refs/replace/*` or an `info/grafts` file is refused (`CLONE_CONFIG_HOSTILE`), so ancestry and promotion-merge checks cannot be forged from the clone. The child environment of every git/gh call is built from an allowlist (`LANG`, `LC_*`, `TMPDIR`, `TZ`, `SSH_AUTH_SOCK`, `GH_TOKEN`, `GITHUB_TOKEN`) plus the tool's forced values (`PATH=/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin`, `GH_HOST=github.com`, `HOME` from the account database, `GH_CONFIG_DIR=<home>/.config/gh`, `XDG_CONFIG_HOME=<home>/.config`, `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_NO_REPLACE_OBJECTS=1`); every other inherited `GIT_*` variable (`GIT_SSH_COMMAND`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`, `GIT_EXTERNAL_DIFF`, `GIT_DIR`, `GIT_ASKPASS`, `GIT_CONFIG_COUNT`, ...) and `SSH_ASKPASS` are dropped, so committer identity must come from the global git config. `GH_HOST`, `GH_CONFIG_DIR`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` are never passed on and `GH_HOST` is forced to `github.com`, so a caller-controlled host or `hosts.yml` cannot send gh to a server that forges merged-PR answers. The caller's `PATH` never decides which binary runs: `git` and `gh` are resolved once to absolute paths from the fixed trusted PATH (first executable match) and run by those paths; the gate logic's own `gh` helper is replaced by the same pinned runner. Home and config trust: the CLI takes the home directory from `pwd.getpwuid(os.getuid())`, never from `$HOME`, and at startup sets its own `HOME` to it and removes `XDG_CONFIG_HOME` and `GH_CONFIG_DIR`, so every owner-file path (policy, GO records and directory default, release-origins, release-verification, release-protection, the env hook, the gate module's evidence directories) and every child's `~/.gitconfig`, `~/.ssh/config` and gh `hosts.yml` come from the real home; a caller-set `HOME`, `GH_CONFIG_DIR` or `XDG_CONFIG_HOME` is ignored. Tool trust: the resolved `git` or `gh` (as found and after `realpath`) must be a regular file owned by root or the current user and not group- or world-writable, and so must every parent directory up to `/`; otherwise the command is refused with `TOOL_PATH_UNTRUSTED` naming the tool and the offending path (user-owned Homebrew directories pass, group- or world-writable ones do not, and the first match is not skipped in favour of a later one). The only extension point is the owner file `release-line-test-env.json` (`{"schema":1,"pass":["NAME", ...],"tools":{"git":"/abs/wrapper","gh":"/abs/fake"},"trusted_path":["/abs/dir"]}`, trust-anchor reader), which the test harness uses for its fake gh and git wrappers and to inject a search path (variables, absolute tool paths and directories only; wrappers named here are owner-chosen and not trust-checked). Tests reach the CLI through `tests/release-line-test-launcher.py`, which patches the account lookup to the fixture home; nothing in `scripts/` reads an environment switch for it. `final-tag` requires GitHub to answer the promotion-PR query (`GH_UNAVAILABLE` otherwise), like `verify`.  the tool's `credential.helper` reset means https pushes need an ssh origin or credentials supplied outside git config. Predecessor and same-major final tags are confirmed with `ls-remote` on origin; a tag that exists only
in the clone is not a shipped version. A rerun of `promote` after the merge and `final-tag` recompute that accounting against the promotion's first parent from git and GitHub facts, so `BASE_MOVED_UNACCOUNTED` stays FAIL until the new changes are accounted (the journal is never the authority). After the merge the tool asserts `final^1` equals the evaluated base tip; if the base moved it re-runs the forward-port accounting against `final^1` and
reports `BASE_MOVED_UNACCOUNTED` (FAIL, the merge has happened) when new changes are unaccounted. If the after-merge protection comparison cannot run, promote ends `FAIL` with "verify protection manually".
Limits: protection checks cover classic branch protection only (not rulesets) and compare only before and after the promote call. `release verify` is bound to the GO candidate (the promotion merge of that
SHA and the PR reporting it) and refuses to overwrite a record whose digest the owner has pinned (`VERIFY_RECORD_PINNED`). Candidate and final tags the tool pushes are annotated (a local lightweight tag is replaced; a lightweight tag already on origin at the commit is `CONFLICT_EXISTING`, never reused). Residual risk: tags created by others (the predecessor and same-major final tags read from origin, which the train and promote preconditions consult) are accepted as lightweight or annotated, since pre-existing releases may predate this tool; `--n` must be an integer >= 1 and `--sha` a commit id.

## Skill (SPE-181b)

The `release-line` skill (`.claude/skills/release-line/`) wraps this CLI for both hosts. It is listed in `codex/shared-skills.json`;
`scripts/profile-install.sh --claude-skills` installs it to `~/.claude/skills/release-line/` and `--global` to `~/.codex/skills/release-line/`,
and `--check` reports drift. It calls the CLI by absolute path from the maintained checkout and restates the safety rules: no force-push, no
`gh workflow run`, no admin-merge outside the owner-merge exception, and tags, promotion or publication only under a recorded GO.

## Release: build, notes, publish, run (SPE-184b)

```
python3 -I $R release build   --version V [--line L] [--build-timeout S] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release notes   --version V [--line L] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release publish --version V [--line L] [--backend gh] --repo OWNER/REPO --clone PATH [--execute]
python3 -I $R release run --until candidate-tag|promote|verify|final-tag|build|notes|publish --version V [--line L] --repo OWNER/REPO --clone PATH [--execute]
```

All three use the key `release:<repo>:<version>:<final_sha>` and default to a dry plan. The final SHA is recomputed every time from git and GitHub, never from
the journal or the state directory: the `tag_format` tag must exist on origin and peel to a promotion merge on the publication base (`final^2` is the candidate,
`final^{tree}` is the predicted tree, cross-checked with the promotion PR; the PR lookup is strict, so a GitHub outage is `GH_UNAVAILABLE`, never a silently skipped cross-check), and a final tree that differs from the candidate tree needs the same owner-pinned
verification record as `final-tag` (`FINAL_TAG_MISSING`, `PROMOTION_ASSERTION_FAILED`, `FINAL_NOT_ON_BASE`, `PROMOTION_MISMATCH`, `FINAL_NOT_VERIFIED`).

`build` runs the owner's `build.argv` from the registry (no shell, never taken from the clone) in a clean detached worktree at the final SHA with a scrubbed
environment (`PATH`, `HOME`/`TMPDIR` in a fresh temp directory, `LANG`, `LC_ALL`, `TZ`, `SOURCE_DATE_EPOCH` = the commit time; no variable of the caller, no
token) and a timeout (default 1800 s, at most 7200 s, the process group is killed on timeout and on normal exit alike). The worktree shares the clone's `.git`, so after the build the clone is re-validated (`CLONE_CONFIG_HOSTILE` if the build planted config keys or an `info/attributes` driver) before any git call, and git children never receive `GH_TOKEN`/`GITHUB_TOKEN` (only `gh` does); `release verify` re-validates the same way after its checks. It fails closed, FAIL and nothing recorded: `BUILD_FAILED` (non-zero exit),
`BUILD_TIMEOUT`, `BUILD_OUTPUT_MISSING` (an `outputs` glob matched no file), `BUILD_OUTPUT_UNSAFE` (symlink, special file or outside the tree),
`BUILD_OUTPUT_DUPLICATE`/`BUILD_OUTPUT_NAME`/`BUILD_OUTPUT_PATTERN`, `BUILD_MUTATED_TREE` (a tracked file changed). Outputs are copied by file name to
`releases/<version>/` and their sha256 recorded in `releases/<version>.json` and the journal. A rerun with intact copies is `REUSED`; damaged copies are rebuilt.

`notes` renders `releases/<version>.notes.md` deterministically from git, GitHub and the build manifest: source branch, release line, version, promotion PR,
candidate tag and SHA, final tag and SHA, publication base, forward-port accounting for the base-only changes before the promotion, and the artifact checksums as
`sha256sum` lines. It prints the path, its sha256 and `publish_digest`; without `--execute` it only reports them. `BUILD_MISSING` or `ARTIFACT_TAMPERED` when the
build is absent or its copies no longer match the manifest.

`publish --backend gh` needs a GO record with the `publish` scope whose `candidate_sha` equals the promotion's candidate (`GO_MISSING`, `GO_SCOPE`, `GO_EXPIRED`,
`GO_MISMATCH`), and, because the state directory is agent-writable, the owner's GO record must also pin `publish_digest`: the sha256 of the canonical JSON of the
repo, version, final SHA, notes digest and every artifact digest, as printed by `release notes` (`GO_DIGEST_UNPINNED` names the value to pin, `GO_DIGEST_MISMATCH`).
Publish re-hashes every artifact against the manifest (`ARTIFACT_TAMPERED`, `ARTIFACT_MISSING`), regenerates the notes and requires the stored file to be
identical (`NOTES_MISSING`, `NOTES_TAMPERED`), and uploads the bytes it just verified from a private staging directory. With `--execute` it runs
`gh release create <tag> --repo OWNER/REPO --verify-tag --title <tag> --notes-file <notes> <files...>` for the **existing** final tag: it never creates or moves a tag,
never uses `--clobber`, never calls `gh release upload`, and dispatches no workflow. It then downloads the assets and verifies bytes, notes and `ls-remote` of the
tag; a difference is FAIL. The journal records `created` immediately after `gh release create` succeeds, before verification; a verification error (gh or git outage, failed download) is a FAIL with `publish-unverified`, not a BLOCKED, so a public release never goes unjournaled. A failed create is journaled `publish-failed`, a reused release `reused`. An existing release with identical notes and asset bytes is `REUSED`; any difference, a draft, or a failed create leaving a partial
release is `CONFLICT_EXISTING` or `PUBLISH_FAILED` and is never overwritten or deleted. Other backends are `UNSUPPORTED_BACKEND`; a registry `channel` other than
`github-release` is `DEFERRED`.

Zero Actions spend holds only if the target repository has no workflow triggered by `release` events or by tag pushes: creating a GitHub release or pushing a
candidate or final tag can start `on: release` / `on: push: tags` workflows there (indirect Actions spend), which this tool neither dispatches nor can prevent.
Check the target repo's workflows before relying on the zero-spend property.

`run --until STEP` runs `candidate-tag`, `promote`, `verify`, `final-tag`, `build`, `notes`, `publish` in order up to STEP. Each step does its own gating, takes its own
GO scope and lock, and recomputes its facts from git and GitHub, so a rerun resumes after an interruption and completed steps report `PASS`/`REUSED`. It stops at the
first step that is not `PASS` (a dry run stops at the first step that would act), reports that step's status, and an exception is reported as a `REFUSED` step with
its code and the run's status BLOCKED or FAIL. Without `--execute` nothing is changed.

## Planned (not implemented)

Later slices of SPE-180: acceptance from installed entry points (SPE-185).
