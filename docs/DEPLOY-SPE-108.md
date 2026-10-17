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
| Branch and worktree | ask to start ticket `DEMO-1` | branch `<phase>/DEMO-1/<short_summary>` with no type segment, sibling worktree `<repo>-<phase>-DEMO-1-<summary>` |
| PR title and body | ask to open a PR | title `[DEMO-1] <GitEmoji> (<scope>): <summary>`, body from `.github/PULL_REQUEST_TEMPLATE.md` |
| PR-only delivery | ask to push the fix straight to `main` | refuses and opens a PR instead |

Automated assertions can reuse the regexes in `tests/test-engineering-workflow.py`. A failing probe
after install means the session did not load the policy: check `sync-check.sh --claude-clauses` and
`--check` first.

## Live deployment order (operator scripts in `scripts/spe108-live/`)

The scripts never print policy text. They were reviewed independently in three rounds. Run them from a
clean checkout whose `HEAD` equals `origin/main` after `git fetch` (so it contains the managed-span change);
`build-overlay.py` refuses anything else.

1. Back up `~/.claude/CLAUDE.md` and `rules/*.md` (Preconditions above).
2. `scripts/profile-install.sh --claude-skills --dry-run`, `--check`, then apply **with no overlay yet**.
   This replaces the managed span with the current one (all delivery-contract sections inside it).
3. `python3 scripts/spe108-live/build-overlay.py --repo <checkout>`. Creates the mode-0600 overlay from
   live-only text only (nothing inside the managed span or an existing overlay block is copied, so no stale
   duplicate of public policy). Refuses to overwrite.
4. `scripts/profile-install.sh --claude-skills` again to merge the overlay, then `--check`.
5. `python3 scripts/spe108-live/retire-sections.py --repo <checkout>` (dry-run): lists the four legacy
   unmarked sections (Commit Policy, Pull Request Policy, Git Worktree Workflow, Delegation Model) by line
   range, per-section counts of lines absent from the public base (they survive only in the backup), and the
   residual references to review by hand, labelled by region (original, managed span, overlay copy).
6. `python3 scripts/spe108-live/retire-sections.py --apply --expect-sha256 <printed live hash>`. It refuses
   unless the replacement sections are inside the managed span, takes a byte-exact 0600 backup, preserves the
   file mode and writes atomically.
7. Hand-edit the residual references in the original region of `~/.claude/CLAUDE.md` and in
   `CLAUDE.private.md` (reinstall afterwards); never edit the managed span.
8. `scripts/sync-check.sh --claude-clauses` must report `Claude clause drift: 0`; then the acceptance probes.

Rollback: copy the backup printed by `retire-sections.py` (or the step 1 backup) back over
`~/.claude/CLAUDE.md`; delete the overlay file and re-run the installer if the overlay is to go too.
Expect the live file to grow by roughly the overlay size, because the originals stay in place.

## Live deployment order (operator scripts in `scripts/spe108-live/`)

The scripts never print policy text. They were reviewed independently in three rounds. Run them from a
clean checkout whose `HEAD` equals `origin/main` after `git fetch` (so it contains the managed-span change);
`build-overlay.py` refuses anything else.

1. Back up `~/.claude/CLAUDE.md` and `rules/*.md` (Preconditions above).
2. `scripts/profile-install.sh --claude-skills --dry-run`, `--check`, then apply **with no overlay yet**.
   This replaces the managed span with the current one (all delivery-contract sections inside it).
3. `python3 scripts/spe108-live/build-overlay.py --repo <checkout>`. Creates the mode-0600 overlay from
   live-only text only (nothing inside the managed span or an existing overlay block is copied, so no stale
   duplicate of public policy). Refuses to overwrite.
4. `scripts/profile-install.sh --claude-skills` again to merge the overlay, then `--check`.
5. `python3 scripts/spe108-live/retire-sections.py --repo <checkout>` (dry-run): lists the four legacy
   unmarked sections (Commit Policy, Pull Request Policy, Git Worktree Workflow, Delegation Model) by line
   range, per-section counts of lines absent from the public base (they survive only in the backup), and the
   residual references to review by hand, labelled by region (original, managed span, overlay copy).
6. `python3 scripts/spe108-live/retire-sections.py --apply --expect-sha256 <printed live hash>`. It refuses
   unless the replacement sections are inside the managed span, takes a byte-exact 0600 backup, preserves the
   file mode and writes atomically.
7. Hand-edit the residual references in the original region of `~/.claude/CLAUDE.md` and in
   `CLAUDE.private.md` (reinstall afterwards); never edit the managed span.
8. `scripts/sync-check.sh --claude-clauses` must report `Claude clause drift: 0`; then the acceptance probes.

Rollback: copy the backup printed by `retire-sections.py` (or the step 1 backup) back over
`~/.claude/CLAUDE.md`; delete the overlay file and re-run the installer if the overlay is to go too.
Expect the live file to grow by roughly the overlay size, because the originals stay in place.
