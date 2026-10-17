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
   (or leaves the checkpoint; it then returns to `WAIT_DEPENDENCY`).

## Safety properties (each has a regression test)

| Property | Behavior |
|---|---|
| Deduplication | Each event id is consumed once; a repeated or stale event never resumes again. |
| Budget and cooldown | `max_resumes` is spent before launch, so crashes and refused launches count. At the limit the campaign becomes `WAITING_OPERATOR`. `cooldown_s` spaces attempts. |
| Lease | A live pid with an unexpired lease blocks concurrent ticks. A dead or expired lease is recovered with a receipt and no extra spend. |
| Admission | A resume starts only when `disk-admission.py check --weight <light/heavy>` exits 0. Denial spends no budget. |
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
- The agent has no `KeepAlive`, so a failure cannot respawn in a loop.
- The checkpoint directory is advisory state, not a boundary against a same-user attacker.
