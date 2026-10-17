# Campaign resume coordinator (SPE-110)

`scripts/campaign-resume.py` lets a campaign that is waiting on an external event
continue after its Claude Code session has ended. It is plain code: no model call
happens until an event is observed.

## Flow

1. The agent finishes what it can, then records a checkpoint:
   `campaign-resume.py register --campaign <name> --session-id <uuid> --cwd <abs path> --pr-merged owner/repo#N [--not-before <UTC>]`.
2. A launchd user agent runs `campaign-resume.py tick --execute` every 300 s.
3. `tick` reads each checkpoint, asks GitHub for PR state (or compares the clock),
   and for a newly satisfied event runs `claude --resume <uuid> -p <fixed prompt>`
   in the recorded `cwd` with a budget cap.
4. The resumed session re-reads live state, continues, and registers its next wait
   (`register` declares the wait; an event whose content (PR or time) is unchanged stays consumed, a changed one is new). If it exits 0 without registering, the campaign
   returns to `WAIT_DEPENDENCY` with nothing pending. If it fails, times out or cannot
   launch, the campaign becomes `WAITING_OPERATOR`, a receipt is written and nothing
   resumes it until the operator runs `campaign-resume.py reset --campaign <name>`
   (which restores the budget and clears consumed events, so an already-satisfied pending event resumes on the next tick). `status --json` shows every state.

## Events

`--pr-merged owner/repo#N` (authoritative `gh pr view` state), `--not-before <UTC>`,
`--file-exists <abs path>` (operator-done marker or QA artifact). A campaign is
woken when ANY registered event fires, so one blocked dependency cannot freeze it;
the resumed agent registers what it still waits for.

## Safety properties (each has a regression test)

| Property | Behavior |
|---|---|
| Deduplication | Events are keyed by content (`repo#N` or time). A consumed event never resumes again, even if re-registered; a different PR or time is a new event. |
| Budget and cooldown | `max_resumes` is a LIFETIME cap per campaign name, kept across re-registers and spent before launch, so crashes and refused launches count. At the limit the campaign becomes `WAITING_OPERATOR` until `reset`. `cooldown_s` spaces attempts. |
| Lease | An unexpired lease whose coordinator pid or resumed child pid is alive blocks concurrent ticks. A dead or expired lease is recovered with a receipt and no extra spend. Timestamps use the time of each claim. |
| Open session | A session whose transcript changed in the last 120 s, or that is not found, is not resumed (`session_active` / `session_not_found`, cooldown, no budget). Only the file's mtime is read. |
| Bounded files | `receipts.jsonl` and `<name>.log` keep one previous generation beyond 1 MiB. |
| Locking | Events (`gh`, 20 s each) and admission are evaluated with no lock held and re-verified under it, so `register` is never blocked behind them. |
| Isolation | One campaign's error is receipted (`tick_error`) and the others still run. An unreadable `gh` is receipted as `github_unreadable`, at most hourly. |
| Admission | A resume starts only when `disk-admission.py check --weight light` exits 0. Denial spends no budget. Heavy automatic resumes are DISABLED: admission is a check, and a SPE-109 reservation needs a byte size and pid this tool cannot know. Heavy work in the resumed session is still subject to the SPE-109 gate hook (WARN until an operator enables ENFORCE). Registering `heavy` is refused. |
| Timeout | The run has a hard timeout; its process group is killed. |
| No bypass | The argv has no permission flag. The prompt is a constant; checkpoints are never executed or used as prompt text. |
| Receipts | One JSONL line per decision in `~/.cache/coding-agent-environment/campaigns/receipts.jsonl` (mode 0600). |

## Verified Claude Code behavior (2026-10-17)

In a throwaway directory, `claude -p ... --session-id <uuid>` followed by
`claude --resume <uuid> -p ...` continued the same session non-interactively.
An unknown id fails with `No conversation found with session ID`. Sessions are
looked up from the working directory, so the checkpoint records `cwd`.

## Codex

`codex exec resume <SESSION_ID> [PROMPT]` exists in codex-cli 0.160.0 help. Its
behavior is not verified here, so this tool does not support Codex and no
cross-harness claim is made.

## Limits

- It resumes only while the launchd agent is loaded. Without it nothing resumes.
- Installing the agent is an operator action; `campaign-resume.py plist` prints
  the definition and installs nothing:

  ```
  python3 scripts/campaign-resume.py plist > ~/Library/LaunchAgents/com.coding-agent-environment.campaign-resume.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.coding-agent-environment.campaign-resume.plist
  ```

  Run `plist` from the merged checkout so the embedded script path is the installed one.
- The agent has no `KeepAlive`, so a failure cannot respawn in a loop. Its plist sets `HOME` and a `PATH` covering `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`; if `gh` or `claude` live elsewhere, edit the plist before loading it.
- `<name>.log` and `receipts.jsonl` are append-only and not rotated. The log holds the resumed session's raw output and may contain whatever it printed.
- A resumed grandchild that calls `setsid` escapes the process-group kill at the timeout.
- A crash between `Popen` and recording the child pid leaves only the coordinator pid in the lease.
- `tick_error` receipts are not rate limited.
- Nothing coordinates with a session you still have open interactively; do not register a session id you are actively using.
- Admission is checked, not reserved, so a heavy resume has a time-of-check window.
- Only a trivial prompt was verified end to end. A headless resume without permission flags acts only within the settings it inherits.
- The checkpoint directory is advisory state, not a boundary against a same-user attacker.

## Rollback

```
launchctl bootout gui/$(id -u)/com.coding-agent-environment.campaign-resume
rm ~/Library/LaunchAgents/com.coding-agent-environment.campaign-resume.plist
```

Checkpoints and receipts under `~/.cache/coding-agent-environment/campaigns/` are inert once the agent is gone.

## SPE-110 MVP scope against the ticket

| Ticket item | MVP |
|---|---|
| Checkpoint per campaign, atomic, 0600 | Done. Holds session, cwd, events, budget, lease. The ticket DAG, owners and SHAs are NOT stored; the resumed agent re-reads live state from Jira and GitHub. |
| Events: PR merged, marker file, QA artifact path | Done (`pr_merged`, `file_exists`, `not_before`). Admission-tier and ticket-state events are deferred. |
| Lock, lease, duplicate prevention, one reclaim with audit | Done. |
| Bounded `claude --resume -p`, no permission bypass | Done. One attempt per event, lifetime budget plus cooldown (stricter than N per day). |
| Heavy-lane reservation and queue | Deferred; heavy automatic resumes are refused (acceptance cases 3 and 4 depend on it). |
| Receipts | Done, keyed by event content rather than a set hash. |
| Operator-installed launchd with rollback | Packet above; not installed. |
