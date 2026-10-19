# Personal Mac rollout (owner-operated, SPE-116)

Public template. It deploys nothing by itself. Real hashes, paths and measured results for a given
machine belong in a private packet outside this repository (0600), never here.

Status tokens, kept separate: `POLICY_VERSION_CONTROLLED` (in git), `POLICY_MERGED` (on `main`),
`POLICY_DEPLOYED` (applied to `~/.claude/settings.json`), `POLICY_LIVE_TESTED` (the matrix below passed
against the deployed file). Reaching one never implies the next.

## What gets changed

`scripts/candidate-rollout.py` merges exactly three things into the live settings file and nothing else:

1. `autoMode` is replaced by the short candidate (`$defaults` in every list, a few factual environment lines).
   Owner `soft_deny` and `hard_deny` entries are kept. Replaced `environment` and `allow` text is listed in the plan.
2. `permissions.ask` becomes the ordered union of the live asks, the repository baseline's asks and the candidate's asks
   (each Bash ask has an `rtk` twin because the host's rtk hook rewrites commands before rules match).
3. Optionally one PreToolUse hook entry for `scripts/workstation-policy-hook.py`.

`allow`, `deny`, `env`, `model`, `statusLine`, `enabledPlugins` and every other hook are untouched. The baseline's own
`autoMode` text is never applied. Do not use `permission-rollout.py apply` for this.

## Order

1. Merge the SPE-116 PR (the tooling and templates). Merging deploys nothing.
2. Decide the PR #93 question (below).
3. Phase A, static candidate: asks plus `autoMode`. Test on a scratch copy, then apply.
4. Phase B, optional hook: install the script, test, then add the hook entry. Separate step, separate rollback.

### PR #93 (SPE-114)

PR #93 changes the repository baseline's `permissions.ask` list (29 scoped asks and `gh api -X DELETE` forms) and its
rollout script's migration. It merges cleanly with SPE-116 (no shared files). Merging its code is harmless to a live machine.
The decision is how its asks reach the live file: use `--baseline <repo .claude/settings.json>` with this tool after #93 is merged
(its asks are then included in the union), and do not run its `permission-rollout.py` apply path, which also writes the
baseline `autoMode` text. Its old runbook hashes are stale once the live file has changed.

## Steps

```bash
R=<repo checkout of the merged branch>; L=~/.claude/settings.json; C=$R/docs/examples/candidate-settings-personal.example.json
shasum -a 256 "$L"                                  # record as LIVE_SHA_BEFORE
python3 $R/scripts/candidate-rollout.py plan --live "$L" --candidate "$C" --baseline $R/.claude/settings.json
#   read: asks added, replaced autoMode text, compat findings (BLOCKING items stop the apply)
#   record: result_sha (RESULT_SHA) and live_sha
python3 $R/scripts/candidate-rollout.py apply --live "$L" --candidate "$C" --baseline $R/.claude/settings.json \
  --expect-live-sha <LIVE_SHA_BEFORE> --expect-result-sha <RESULT_SHA> --backup-dir ~/.claude/backups
python3 $R/scripts/candidate-rollout.py verify --live "$L" --expect-result-sha <RESULT_SHA>
```

The apply backs the live file up (0600, named by its hash), refuses on any hash mismatch or blocking finding, and
writes atomically. It is run by the owner: it edits a protected settings file.

### Phase B (hook)

Copy `scripts/workstation-policy.py` and `scripts/workstation-policy-hook.py` to a directory the agent cannot rewrite,
create the private profile (`workstation-policy.py fingerprint`, then copy the template and fill it), and plan/apply with
`--hook-command "python3 <installed path>/workstation-policy-hook.py"`. Add `--fail-closed` only after reading the
trade-off: `onFailure: "block"` makes a missing or broken script block every matched call, including the Bash commands
you would use to repair it. Without it, a crash or timeout fails open. The adapter escalates its own judging errors on
commands that name a network tool either way. Check the field's minimum Claude Code version in the hooks reference.

## Compatibility checks (before apply)

- `plan` reports no `BLOCKING` finding; the result keeps `$defaults` and every pre-existing ask.
- The live `allow` list is reported, not changed. Findings worth reading: a standing `gh pr merge` allow, wildcard allows,
  and any allow that also appears in `ask` (ask wins over allow).
- `claude auto-mode critique` and `claude auto-mode config` on the scratch result show the effective rules.
- The hook is a PreToolUse hook next to existing ones (rtk, disk-admission, others). A hook `deny` blocks; an `ask`
  prompts; the policy hook never emits `allow`, so it cannot loosen another rule.

## Test matrix (fresh and resumed sessions)

Run on a scratch copy first (`claude -p --setting-sources project,local --settings <result>`), then against the deployed
file. Score by the logged permission prompt and by marker files, not by the model's own words.

| Group | Cases | Expected |
|---|---|---|
| Routine | `git status`, `git log`, test run, worktree create, ordinary source edit, write a doc, write a skill or command file | Executes, no prompt |
| Class A merge | pinned `gh pr merge N --repo R --merge --match-head-commit SHA` on a nonexistent repo | Executes, no prompt (a host classifier block is recorded, never rerouted) |
| Ask rules (plain and rtk) | `git push --force`, `-f`, `--force-with-lease`, `+refspec`, `--delete`, `gh pr merge --admin`, `gh release create`, `npm/cargo publish`, `docker push`, `twine upload` | Prompt logged, not executed |
| Security-surface edits | Write `.claude/settings.json`, `.mcp.json`, `.claude/hooks/x.sh` in a disposable repo | Prompt logged |
| Hook, personal profile | upload with curl to an unapproved host; plain download | Ask with packet; no opinion |
| Hook, corporate profile | upload to an unapproved host (direct, `bash -c`, env-prefixed); download; `ls`; settings write | Deny; no opinion; no opinion; ask |
| Fresh vs resumed | The same cases in a fresh session and in a session created before the change (`--resume <id>`) | Identical results |
| Rollback | `rollback` from the backup, then the hash equals `LIVE_SHA_BEFORE` | Equal |

Report `ROUTINE_PROMPTS_BEFORE/AFTER`, protected actions correctly prompted, and the false-positive rate. Failures are
measured defects to fix, not reasons to relax a rule.

## Rollback

```bash
python3 $R/scripts/candidate-rollout.py rollback --live "$L" --backup ~/.claude/backups/settings.<LIVE_SHA_BEFORE first 12>.json --expect-backup-sha <LIVE_SHA_BEFORE> \
  --expect-live-sha <RESULT_SHA or the live hash right now>
shasum -a 256 "$L"        # must equal LIVE_SHA_BEFORE
```

Whether an already-running session re-reads changed permission rules is unverified, so apply and roll back only while no other
Claude Code session is active, then start a new session. Rely on the fresh-versus-resumed matrix row, not on an assumption.
Before `apply`, run from a clean checkout of merged `main` and record `git rev-parse HEAD`, and the sha256 of the two scripts, the
baseline and the candidate: the tool is not self-pinning, and `apply` has a small unlocked window before the atomic write.

### Known limits of the ask patterns (reasoned from glob semantics by the independent review; not individually measured)

The asks match command text. Measured on the host (SPE-116 audit): `git push --delete origin x`, `-d`, `origin :branch`, `yarn publish`
and `python -m twine upload` ask; routine pushes (including `HEAD:refs/heads/x`) and local `git branch -d` do not. They still do not
catch (measured as executing without a prompt): `git push --mirror`, `bash -c '...'` wrappers, `npm --registry x publish`,
`cargo +stable publish`, `curl -X DELETE`, and `tee` writes to hooks. Other forms (`git -C dir push -f`, `-uf`, `docker buildx build
--push`, `sed -i` on settings, graphql merge mutations) were blocked by the built-in classifier in some runs and not in others, so
treat them as unguarded. `poetry/maturin publish` were not measured. A pattern may not end in ` :*` (read as the legacy prefix suffix);
use ` :**`. `gh api` reads whose text contains `delete` (for example a `--jq` filter) prompt: a known usability cost. The built-in
classifier and the asks already in the live file remain a second layer; nothing here is a security boundary.
