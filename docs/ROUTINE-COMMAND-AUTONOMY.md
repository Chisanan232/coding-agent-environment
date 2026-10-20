# Routine command autonomy (SPE-119)

Goal: long unattended sessions do not stop to ask about ordinary development, while protected operations still ask. This is a measured result for one host, not a guarantee, and it does not weaken any protection. Apply procedure: `docs/ROLLOUT-RTK-PROXY-SCOPING.md`.

## 1. Evidence, not one command at a time

**Transcript mining (local, aggregated, nothing sensitive kept).** 30 days of Claude Code transcripts on this workstation: 80,876 Bash calls in 2,552 sessions. Only program plus subcommand signatures and denial counts were kept; arguments, paths and values were never stored.

| Finding | Number |
|---|---|
| Bash calls denied or blocked | 498 (0.6%) |
| of which Auto Mode classifier | 319 |
| of which ask or permission prompt | 80 |
| of which merge-without-review / self-approval (the SPE-118 problem) | 66 / 33 |
| Denied shapes | mostly compound or piped; 82 simple |
| Denied read-only verb with no allow rule | `git ls-tree` about 20 times |

Interactive approvals leave no trace in a transcript, so these counts under-report prompts; the host measurement below is the authority.

**ROUTINE_COMMAND_CORPUS** (`docs/permission-corpus/routine-command-corpus.json`, 155 commands): every family listed by the owner (source inspection, read-only git and GitHub, development toolchains, normal source control, build and QA, automation wrappers: `rtk proxy`, environment prefixes, pipelines, compound commands). Harmless variants only (`--help`, `--version`, a fixture repository, a nonexistent GitHub repository). Each is classed:

| Class | Expectation | Count |
|---|---|---|
| AUTO_SAFE | runs with no prompt | 120 |
| AUTO_WITH_RESOURCE_ADMISSION | runs with no prompt (heavy builds also go through `scripts/memory-admission.py`) | 2 |
| OWNER_ASK | must prompt (force push, remote branch delete, release, publish, admin merge, destructive and process-kill forms, wrapped and abbreviated spellings) | 24 |
| DENY_OR_HOST_PROTECTED | must not run (secret access, IAM, outbound data, shell re-entry) | 5 |
| UNKNOWN_REQUIRES_REVIEW | measured, no expectation (trusted local scripts, wrapped interpreters) | 4 |

A tool is not treated as safe because it usually is: `rg` is not allowed through the wrapper because `rg --pre CMD` runs CMD, `git cat-file` is not allowed because `--textconv` runs helpers, `cargo nextest` is allowed only as `nextest run`.

## 2. Measured on the real host

`scripts/permission-host-matrix.py` runs each command through the installed Claude Code (`claude -p --permission-mode auto`) in a throwaway fixture repository. Ground truth comes from hooks it injects (a `PermissionRequest` record means a prompt; a `PostToolUse` or `PostToolUseFailure` record means the command ran), not from the model's reply. (An earlier pass that read the model's reply wording mislabelled about a fifth of the cases; it was replaced.)

| | Live file today | Canary asks + SPE-119 candidate |
|---|---|---|
| Routine commands (122) running with no prompt | 113 (92.6%) | **122 (100%)** |
| Routine commands that prompted | 9, all `rtk proxy` forms | **0** |
| Protected commands (29) held | 28 (a remote branch delete ran unprompted) | **29 of 29** |
| UNKNOWN (4) | 2 ran, 2 prompted | 4 ran |

Other modes, one routine and one protected command each, on the candidate:

| Mode | Routine | Protected (force push) |
|---|---|---|
| Fresh session | ran, no prompt | prompted |
| Resumed (`--resume`) | ran, no prompt | prompted |
| Background Bash | ran, no prompt | prompted |
| Subagent | ran, no prompt | did not run; no prompt record, so the mechanism is not observed (a rule ask would have recorded one) |

Limits: one host and Claude Code version; headless auto mode (an interactive default-mode session may differ); the fixture is not the owner's repositories; resumed means `--resume` with the candidate passed as settings, not an edit of the settings file under a running session (documentation does not say whether a running session reloads rules, so start a new session after applying). The classifier's verdict for commands with no matching rule cannot be fixed by user settings.

## 3. What the policy is

AUTO: routine reads, searches, tests and builds, owned-branch commits and pushes, and PR create or update, by named scoped allows or by the classifier. OWNER_ASK: force push, remote branch deletion, release, publish, production and destructive infrastructure, protected settings. No `Bash(*)`, no `python3(*)`, no `rtk proxy *`, no all-`gh` allow, no restored verbose Auto Mode text. Prefer the native Read, Grep, Glob and Edit tools and single commands; chains and pipes are still handled correctly but add judgment points.

## 4. Re-measure

```bash
python3 scripts/permission-host-matrix.py --corpus docs/permission-corpus/routine-command-corpus.json --live --out before.json
python3 scripts/permission-host-matrix.py --corpus docs/permission-corpus/routine-command-corpus.json --candidate <full settings file> --out after.json
```

Each run spends about 155 short model calls and takes 10 to 15 minutes. It edits no settings file. Run it after the owner applies the change, with `--live`, to record `POLICY_LIVE_TESTED`.
