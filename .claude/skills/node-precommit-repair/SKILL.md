# SKILL.md — node-precommit-repair

> **Language**: TypeScript / JavaScript (Node.js projects). This skill is specific
> to pre-commit hooks in Node/TypeScript repos.
> For other languages, see `~/.claude/skills/README.md`.

## Purpose
Repair pre-commit hook failures in Node.js / TypeScript projects without using
`--no-verify`. Identifies which hook failed and routes to the appropriate fixing
skill.

## Type
Auto-used. Claude Code invokes this skill when `pre-commit run --all-files`
fails in a Node/TypeScript project.

## Do Not Assume
- Do not assume all hooks failed — read the output to identify which specific
  hook(s) failed.
- Do not use `--no-verify` to bypass failures — fix the root cause.
- Do not re-run pre-commit without first fixing the identified issue.

## Steps

### Phase 1 — Identify the failing hook
1. Run pre-commit and capture full output:
   ```bash
   pre-commit run --all-files 2>&1 | tee /tmp/precommit-output.txt
   ```
2. Read the output. Identify which hook(s) are marked `Failed`.
3. Map the failing hook to the appropriate repair action:

   | Failing hook | Repair action |
   |---|---|
   | `eslint` / `eslint-fix` | Use `typescript-eslint-fixing` skill |
   | `tsc` / `typescript` | Use `typescript-tsc-debugging` skill |
   | `prettier` | Run `npx prettier --write .` then re-check |
   | `jest` / `vitest` | Fix the failing tests before committing |
   | `lint-staged` | Run the underlying linter/formatter directly to see the full error |
   | `commitlint` | Fix the commit message format to match the configured convention |

### Phase 2 — Repair
4. Apply the fix using the appropriate skill or direct command.
5. Stage any files modified during repair:
   ```bash
   git add -p   # review changes before staging
   ```
6. Re-run pre-commit for the specific failing hook only:
   ```bash
   pre-commit run <hook-id> --all-files
   ```

### Phase 3 — Verify
7. Run `pre-commit run --all-files` — all hooks must pass before committing.
8. Confirm working tree is clean (all fixes are staged).

## Safe-Fix Guidance
- Never use `--no-verify`. If a hook is persistently broken (e.g., a config
  error in the hook itself, not the code), report it to the engineer
  and do not proceed with the commit.
- If `lint-staged` is configured, run the full linter directly (not via
  lint-staged) to see all violations — lint-staged only checks staged files.
