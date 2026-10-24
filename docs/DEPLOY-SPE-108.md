# Operator packet: deploy the SPE-108 development contract

Scope: apply the merged contract (commit/branch/PR formats, PR-only delivery, seven-subagent
maximum, compact `CLAUDE.md`) to a live machine through the existing marker-aware installer.
Nothing here replaces a live file wholesale, and no live policy text is published.

## Layers

| Layer | Where | Published |
|---|---|---|
| Public base | `.claude/CLAUDE.md`, shared skills in this repo | yes |
| Private overlay | `$CODING_AGENT_LOCAL_BASELINE_DIR/claude/CLAUDE.private.md` (default `~/.config/coding-agent-environment/local/baseline/claude/`), mode 0600, owned by you | never (gitignored) |
| Live file | `~/.claude/CLAUDE.md` | no |

Live-only safety clauses (secret handling, CI classification, auto-merge conditions, non-production
autonomy and any other rule the public base does not carry) belong in the private overlay, not in this
repo. The commit, PR, branch, worktree and delegation rules are delivered by the managed span.

## Preconditions

1. Back up `~/.claude/CLAUDE.md` and any `~/.claude/rules/*.md` byte-exactly (`cp -p`, mode 0600,
   timestamped, under `~/.claude/backups/`) and record only their `shasum -a 256`.
2. Put the live-only clauses in the private overlay file (mode 0600). Optionally add a line
   `<!-- clause-keywords: keyword one, keyword two -->` listing words the live policy must contain.
3. `scripts/sync-check.sh --claude-clauses` must print `UNTRACKED` only for clauses you intend to
   drop. It compares headings and numbered rules by hash and never prints clause text.

## Evidence from a scratch HOME (fresh home, synthetic live file)

| Command | Result |
|---|---|
| `scripts/profile-install.sh --claude-skills --dry-run` | `Managed Claude practice drift: 62` (1 policy block + 61 skill files), exit 0 |
| `scripts/profile-install.sh --claude-skills --check` | same count, exit 1 until applied |
| `scripts/profile-install.sh --global --dry-run` | `Managed Codex drift: 60`, exit 0 |
| `scripts/profile-install.sh --claude-skills` then `--check` | `drift: 0`, exit 0 |

On a real machine expect the Claude count to be 1 plus the number of skill files that differ from the
repo copy. Against the real live policy and no overlay, the clause report currently shows 128 live
clauses absent from the public base (a count; text not shown) which the overlay must cover.
Codex trees additionally report `STALE host-specific file` for Claude-only references installed
earlier; they are reported, never deleted.

## Apply

1. `scripts/profile-install.sh --claude-skills --dry-run`, then `--check`; review the drift list.
2. `scripts/profile-install.sh --claude-skills`. It (a) replaces only the
   `coding-agent-environment:engineering-workflow` span and the `private-overlay` span in
   `~/.claude/CLAUDE.md`, appending them if absent, and leaves unmarked text untouched; (b) overwrites
   every shared skill file under `~/.claude/skills` with the repository copy; (c) saves replaced files
   under `~/.claude/backups/coding-agent-environment/<UTC timestamp>/`.
3. `scripts/profile-install.sh --global` for Codex (same dry-run/check first). Delete reported stale
   host-specific files by hand after review.
4. `scripts/sync-check.sh --claude-clauses` must report `Claude clause drift: 0`.

## Rollback

Restore the corresponding paths from the printed `Prior state:` backup directory, or from the
byte-exact backups of step 1, and compare hashes. Removing the overlay file and re-running does not
delete the live overlay span; edit the markers out by hand if required.

## Acceptance probes (harness design; not run by the installer)

Run each probe in a fresh session and in a resumed session, on both Claude Code and Codex, with a
disposable repository and a stub ticket. Record transcripts only after secret redaction.

| Probe | Prompt | Pass condition |
|---|---|---|
| Subagent cap | ask for eight parallel independent tasks | no more than seven concurrent subagents, one owner per scope |
| Commit format | ask for a commit of a two-file change | one logically small commit per concern, subject `<GitEmoji> (<scope>): <summary>` |
| Branch and worktree | ask to start ticket `DEMO-1` | branch `<phase>/DEMO-1/<type>/<short_summary>`, sibling worktree `<repo>-<phase>-DEMO-1-<type>-<summary>` |
| PR title and body | ask to open a PR | title `[DEMO-1] <GitEmoji> (<scope>): <summary>`, body from `.github/PULL_REQUEST_TEMPLATE.md` |
| PR-only delivery | ask to push the fix straight to `main` | refuses and opens a PR instead |

Automated assertions can reuse the regexes in `tests/test-engineering-workflow.py`. A failing probe
after install means the session did not load the policy: check `sync-check.sh --claude-clauses` and
`--check` first.

## Live deployment order (operator scripts in `scripts/spe108-live/`)

The scripts never print policy text. Run them from a clean checkout whose `HEAD` equals `origin/main` after
`git fetch` (`build-overlay.py` refuses anything else). Retiring the legacy sections is **optional**: zero
managed drift and a correct effective contract do not need it, and it never runs without a lossless proof.

### The disposition ledger (local only)

`$CODING_AGENT_LOCAL_BASELINE_DIR/claude/policy-ledger.tsv` (default
`~/.config/coding-agent-environment/local/baseline/claude/`), mode 0600, never committed. One tab-separated
row per live policy line: `L:<12 hex hash>  DISPOSITION  citation  [replacement]`. It holds hashes, never
text; the optional replacement is a PUBLIC canonical line. Dispositions (a line is classified by meaning,
never by text similarity):

| Disposition | Meaning | Citation |
|---|---|---|
| `SEMANTICALLY_PRESERVED` | the approved rule states the same obligation | `repo/path:line` in the pinned checkout |
| `PRESERVE_IN_PRIVATE_OVERLAY` | unique founder text; must still exist after migration | the nearest canonical line |
| `SUPERSEDED_BY_APPROVED_RULE` | an approved rule replaces it (a replacement may rewrite the line) | the approving line |
| `REAL_CONFLICT_REQUIRING_OWNER_DECISION` | live and approved rules disagree; fails every gate until the owner records a decision by changing the row to another disposition with citation `owner-decision:<id>` | the approved rule |

Every tool fails closed on a malformed row, a missing file mode, a symlink, a missing citation target or an
open conflict. `claude-clause-drift.py` counts a dispositioned clause as covered and reports each open
conflict by line number.

### Rehearse first (scratch HOME, writes nothing real)

`scripts/spe108-live/rehearse.sh --repo <checkout> --ledger <ledger> [--mode keep|retire]` copies the real
instruction files and skill trees into a temporary HOME and runs the whole migration there: Claude install,
overlay, install, line rewrite (keep) or retirement (retire), Codex install, the effective-file checks, a second
apply that must change nothing, and a rollback that must restore every pre-migration byte.

### Apply

1. Back up `~/.claude/CLAUDE.md`, `~/.claude/rules/*.md` and `~/.codex/AGENTS.md` byte-exactly (Preconditions).
2. `scripts/profile-install.sh --claude-skills --dry-run`, `--check`, then apply **with no overlay yet**.
3. `python3 scripts/spe108-live/build-overlay.py --repo <checkout> --ledger <ledger>`. Creates the mode-0600
   overlay from live-only text; with `--ledger` it also copies the `PRESERVE_IN_PRIVATE_OVERLAY` lines of the
   legacy sections. Refuses to overwrite a different overlay; identical output is a no-op.
4. `scripts/profile-install.sh --claude-skills` again to merge the overlay, then `--check`.
5. **Keep path (default):** `python3 scripts/spe108-live/rewrite-lines.py --file ~/.claude/CLAUDE.md --ledger <ledger>
   --expect-sha256 <sha>` (dry run, prints line numbers and hashes), then again with `--apply`. It replaces only the
   ledger-named superseded lines with their approved canonical line, after a byte-exact backup.
   **Retire path (optional):** `python3 scripts/spe108-live/retire-sections.py --repo <checkout> --ledger <ledger>`
   (the lossless gate must print PASS), then `--apply --expect-sha256 <sha>`. `--apply` refuses without `--ledger` and
   `--repo`, with any unledgered or conflicting line, or when an overlay line is not already present elsewhere.
6. `scripts/profile-install.sh --global --dry-run`, `--check`, apply, `--check` for Codex.
7. `python3 scripts/spe108-live/verify-effective.py --host claude --before <byte-exact backup of CLAUDE.md>
   --after ~/.claude/CLAUDE.md --skills-dir ~/.claude/skills --ledger <ledger> --repo <checkout>`, and the same with
   `--host codex` for `AGENTS.md`. It inspects the final instruction files and skills, not file counts: nothing
   unaccounted for, no superseded format active, the approved formats and gates present, no open conflict.
8. `scripts/sync-check.sh --claude-clauses` and the acceptance probes above.

Rollback (every step is backed up): copy the byte-exact backups back over `CLAUDE.md` and `AGENTS.md`, restore the
installer `Prior state:` directories newest first (their paths are relative to HOME), then delete files that did not
exist before (`rehearse.sh` proves this restores every byte). Removing the overlay file does not remove the live
overlay span; the span is restored by the `CLAUDE.md` backup.

Known cost: after the overlay is merged the original region and the overlay copy both exist, so the live
file roughly doubles; `verify-effective.py` prints the duplicate-title count. Removing the 23 original duplicates is
a separate owner decision.
