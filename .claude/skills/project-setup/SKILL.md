# SKILL.md — project-setup

## Purpose
Onboard a new repository to the global Claude Code configuration by scaffolding
a project-level `.claude/CLAUDE.md`, adding the gitignore entry, and verifying
the hook chain is functional. Reduces per-project setup friction to under 5 minutes.

## Type
Command-like. Invoked explicitly via `/project-setup` when beginning work on a
new or previously unconfigured repository.

## When to use
- First time Claude Code is used in a repository.
- When a repo has no `.claude/CLAUDE.md` (only the global `~/.claude/CLAUDE.md` applies).
- When onboarding an existing project that previously had no AI configuration.

## When not to use
Do not re-run on repositories that already have a `.claude/CLAUDE.md` unless
explicitly refreshing stale configuration — it will overwrite existing content.

## Steps

### Phase 1 — Detect project context
1. Check if `.claude/CLAUDE.md` already exists in the repository root.
   If it does:
   - Warn the engineer: "`.claude/CLAUDE.md` already exists. Re-running project-setup
     will overwrite the existing configuration. Confirm with 'yes, overwrite' to proceed."
   - Do not proceed to scaffold until the engineer explicitly confirms.
   - If the engineer does not confirm, exit and suggest `/workflow-resume` instead.
2. Read the repository root to identify:
   - **Primary language**: check for `pyproject.toml`/`setup.py` (Python),
     `package.json` (Node/TypeScript/JavaScript), `go.mod` (Go),
     `Cargo.toml` (Rust), `pom.xml`/`build.gradle` (Java/Kotlin),
     `Gemfile` (Ruby), `*.csproj` (C#).
   - **Package manager**: `uv`/`poetry`/`pip-tools` (Python), `npm`/`yarn`/`pnpm`/`bun`
     (Node), `go mod` (Go), `cargo` (Rust), `maven`/`gradle` (Java).
   - **Test runner**: infer from the package manager and lock file (pytest, jest,
     go test, cargo test, rspec, etc.).
   - **Linter/formatter**: check for `.ruff.toml`, `.eslintrc*`, `.golangci.yml`,
     `clippy.toml`, etc.
   - **Type checker**: mypy/pyright (Python), tsc (TypeScript), etc.
   - **Pre-commit**: check for `.pre-commit-config.yaml`.
   - **CI system**: check for `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`.
   - **Issue tracker**: check for `GITHUB_TOKEN` env var or GitHub remote URL →
     default to `github`. Otherwise leave blank for manual fill-in.
3. Summarize detections to the engineer and ask for confirmation before writing.

### Phase 2 — Scaffold `.claude/CLAUDE.md`
4. Create the `.claude/` directory if it does not exist.
5. Write `.claude/CLAUDE.md` with the following sections pre-filled from detections:

````markdown
# CLAUDE.md — Project Configuration

> Project-specific overrides for this repository.
> Extends the global ~/.claude/CLAUDE.md — only sections that differ per project
> are defined here. Global behavioral rules apply unless overridden below.

## Repository Identity
- **Repository**: [REPO-NAME — fill in]
- **Purpose**: [One sentence — fill in]
- **Owner**: [Team or individual — fill in]
- **Primary language**: [detected or fill in]
- **Runtime target**: [e.g., Docker, AWS Lambda, CLI — fill in]

## Architecture Constraints
[Fill in key design rules specific to this repo, e.g.:]
- [All DB access through the repository layer]
- [No business logic in HTTP handlers]

## Package, Build, and Run Commands
```bash
# Install
[detected command or fill in]

# Test (impacted)
[detected command or fill in]

# Test (full suite)
[detected command or fill in]

# Lint
[detected command or fill in]

# Type check
[detected command or fill in, if applicable]

# Pre-commit
[pre-commit run --all-files, if .pre-commit-config.yaml detected]
```

## Testing Tooling
- Runner: [detected]
- Coverage: [fill in, e.g., pytest-cov / nyc / go test -coverprofile]
- Threshold: [fill in, e.g., 80%]

## Source-of-Truth Systems
- Issue tracker: [detected GitHub URL or fill in]
- Wiki/docs: [fill in if applicable]

## Merge Strategy
- [additional protected-branch or code-owner merge gates — fill in]

## Language-Specific Repair Skills
[Fill in the applicable skills from ~/.claude/skills/ or create new ones:]
- [e.g., python-ruff-fixing, typescript-eslint-fixing, go-golangci-fixing]
````

6. Open the scaffolded file and ask the engineer to review and fill in the
   bracketed placeholders before proceeding. Do not proceed to Phase 3 while
   `[fill in]` markers remain.

### Phase 3 — Gitignore and hook verification
7. Add session state files to `.gitignore` if not already present:
   ```bash
   grep -q "^\.claude/\.current-ticket" .gitignore 2>/dev/null \
     || echo ".claude/.current-ticket" >> .gitignore
   grep -q "^\.claude/\.current-worktree" .gitignore 2>/dev/null \
     || echo ".claude/.current-worktree" >> .gitignore
   grep -q "^\.claude/\.current-release" .gitignore 2>/dev/null \
     || echo ".claude/.current-release" >> .gitignore
   ```
8. Verify the hook chain is reachable:
   ```bash
   ls -la ~/.claude/hooks/*.sh 2>/dev/null | wc -l
   ```
   If fewer than 5 hook scripts are found, warn: "Global hooks may not be installed.
   Run the global install steps from GETTING-STARTED.md."
9. Verify hooks are executable:
   ```bash
   ls -la ~/.claude/hooks/*.sh | grep -v "^-rwx"
   ```
   If any hooks are not executable, run: `chmod +x ~/.claude/hooks/*.sh`

### Phase 4 — Confirm and summarise
10. Report to the engineer:
   - What was detected (language, tools, CI).
   - What was scaffolded.
   - Which `[fill in]` markers still need attention.
   - Whether any language-specific repair skills are missing from `~/.claude/skills/`
     and should be created (reference the Language Repair Skills Guide).
11. Suggest the next step: fill in the placeholders, then run `/ticket-intake`
    or use the quick-fix path depending on the first task.

## Output
A `.claude/CLAUDE.md` exists in the repo root with project identity pre-filled
from detected context. Gitignore is updated. Hook chain is verified executable.

## Safe-Fix Guidance
- Do not modify `.gitignore` entries other than adding `.claude/.current-ticket`.
- Do not overwrite an existing `.claude/CLAUDE.md` without explicit confirmation.
- If language detection is ambiguous (e.g., monorepo with multiple languages),
  ask the engineer to specify the primary language before scaffolding.
