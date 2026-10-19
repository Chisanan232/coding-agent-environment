# Minimal user-level `~/.claude/CLAUDE.md` rollout (SPE-117, owner-run)

Delivers the SPE-117 policy (memory-aware admission, `gh` first, GitHub MCP retired, post-reboot recovery) to a live
`~/.claude` through the **existing marker-aware installer**. Nothing here edits `settings.json`, permissions, Auto Mode,
hooks, sandbox or managed settings, and nothing is applied by this repository's CI or by an agent. The owner runs it.
Do not `cp` the repository's `.claude/CLAUDE.md` over the live file (see `README.md`).

## What changes

| Where | Change |
|---|---|
| Managed span of `~/.claude/CLAUDE.md` (between the `coding-agent-environment:engineering-workflow` markers) | One new compact section, "Resource Admission and GitHub Operations" |
| Shared skills under `~/.claude/skills` | `engineering-workflow` (GitHub tool selection rewritten; new "Resource admission and concurrency" and "Post-reboot recovery" sections), `pr-feedback-response`, `pr-health-check`, `release-watch` (the "GitHub MCP optional" wording removed) |
| Original region of `~/.claude/CLAUDE.md` (outside the span; installer never touches it) | **One sentence, by hand** (below) |

Not changed: `settings.json`, `settings.local.json`, hooks, `autoMode`, permission rules, `~/.claude.json`, MCP servers (see
`docs/GITHUB-MCP-RETIREMENT.md` for that separate owner step).

## Steps

1. Back up and record hashes (mode 0600): `cp -p ~/.claude/CLAUDE.md ~/.claude/backups/CLAUDE.md.$(date +%Y%m%d-%H%M%S)` and
   `shasum -a 256 ~/.claude/CLAUDE.md`.
2. From a clean checkout of `main`, run `scripts/profile-install.sh --claude-skills --dry-run` and, as a separate command,
   `scripts/profile-install.sh --claude-skills --check` (`--dry-run` and `--check` cannot be combined, and `--check` needs `--claude-skills`). Expect drift only in the
   rows above. **Stop and review** if it lists any other skill or file; the installer overwrites every shared skill file.
3. Apply: `scripts/profile-install.sh --claude-skills` (it saves its own backup under `~/.claude/backups/coding-agent-environment/`).
4. Edit one sentence in the original region of `~/.claude/CLAUDE.md` (the installer does not touch it). Under "MCP-Backed Systems":
   - old: ``**Active vs opt-in servers:** `fetch` and `github` are active by default.``
   - new: ``**Active vs opt-in servers:** `fetch` is active by default; the `github` MCP is retired, use `gh`.``
5. Verify: `scripts/sync-check.sh --claude-clauses` reports `Claude clause drift: 0`, then start a **fresh** session and a **resumed**
   session and ask: "Which tool do you use for GitHub, and what do you check before a heavy build?" Expected: `gh` (never an
   `mcp__github__` tool) and `scripts/memory-admission.py check --weight heavy`.

## Rollback

Copy the backup from step 1 back over `~/.claude/CLAUDE.md` and restore the skills from the installer's backup directory (or re-run the
installer from the previous commit). Existing sessions keep what they loaded at start; use a new session after any change.

## Notes

- `scripts/memory-admission.py` and `scripts/mcp-scope-inspect.py` are run from the repository checkout; they are not installed into `~/.claude`.
- The proposed memory thresholds are a separate owner decision (`docs/RESOURCE-ADMISSION.md`, section 7); the policy text points to the
  tool and does not hard-code numbers.
