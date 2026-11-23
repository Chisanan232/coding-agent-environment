# release-line algorithms

Authoritative detail lives in `docs/RELEASE-LINES.md` of the maintained checkout. This is the working summary.

## Routing
- The target is the line registered for the exact version, else the major line.
- A major line with an unshipped candidate (rc tag merged, no final tag) is frozen for features. Fixes still go to the major line; features go to the newest active successor that is not itself frozen, else `NEEDS_SUCCESSOR` (BLOCKED, only that work).
- Converged lines route to `converged_into`. Retired and EOL lines refuse.
- `guard` findings: `SUCCESSOR_ONLY_COMMITS`, `EOL_BASE`, `CONVERGE_PRECONDITION` (block); `PUBLICATION_BASE_DIRECT_CHANGE` (advisory); `CONVERGE_ALLOWED` for the tip-into-major converge.

## Successor train
- `successor ensure --predecessor V`: forks `integration/v<next>` from the predecessor's candidate tag. Outcomes `CREATED`, `REUSED`, `CONFLICT_EXISTING`. The registry entry stays `PENDING_OWNER_LISTING` until the owner installs the emitted packet; gated merges into the new branch need that listing.
- `train integrate --from A --into B`: needs A's final tag; opens or reuses one PR `lineint/<from_version>/<into_line>` into B. A rerun after the merge reports `INTEGRATED`. Out-of-order integration is `OUT_OF_ORDER`.
- Successor candidate tags cut before integration are `PROMOTION_BLOCKED`.
- `train converge --line L`: needs every chain version shipped and integrated in order (`CONVERGE_PRECONDITION` lists gaps). After the merge it reports `CONVERGED` and emits an owner packet marking the chain converged.
- Pushes are new refs only and verified with `ls-remote`. Retries recompute from remote and create nothing twice.

## Forward-port accounting
Every maintenance fix is accounted against every strictly newer line. Each target ends in one state:
- `forward-port`: a port commit with `Release-Line-Source` is on the target.
- `equivalent` / `not-applicable`: recorded in a reviewed PR (empty commit with `Release-Line-Kind` and `Release-Line-Reason`).
- `pending`: `TO_PORT`, `HEAD_PUSHED_NO_PR`, `PR_OPEN`, `NOT_STARTED`.
- `conflict`: `BLOCKED_CONFLICT`, `CONFLICT_EXISTING`, `STALE_HEAD_CONFLICT`, `NO_BRANCH`.
Evidence order: already an ancestor, trailer on target, equal patch-id, open PR from `fwdport/<target_line>/<src12>`, then a `merge-tree` replay. The accounting is recomputed on every run; `complete` is true only when every target is terminal. A conflict on one target never stalls the others. Loop prevention: a source carrying `Release-Line-Source`, and integration merges, are not sources.

## GO records and owner pin files
- Merge eligibility comes only from `branches` in the owner policy; the tool never edits it.
- Publication authority is a per-version GO record under `~/.config/coding-agent-environment/local/release-go/<owner>__<repo>__<version>.json` with `repo`, `version`, `candidate_tag`, `candidate_sha`, `publication_base`, `scopes` (`candidate_tag|promote|final_tag|publish`), `authority`, `created_utc`, optional `expires_utc`. A repo or SHA mismatch is refused. The owner writes it; an agent never does. (Consumed by the release commands of later slices; no command on this base reads it.)
- Proposals (`registry propose`, `audit import`) are written under `~/.local/state/coding-agent-environment/release-line/<owner>__<repo>/proposals/` as `.candidate.json` plus `.authority.json`; the owner reviews and installs them.

## Zero-spend rule
The tool and this skill spend nothing: no paid services, no workflow dispatch, no new hosted runs. If `gh` is unavailable, report `NOT_RUN` for the dependent step and continue with git facts. A hosted-CI outage is `CI_UNAVAILABLE_EXTERNAL`, not a reason to skip a gate.

## Owner steps
Installing a candidate policy, listing a successor, writing a GO record, and approving tags, promotion or publication are owner steps. Record the pending step and its resume trigger, then continue independent work.
