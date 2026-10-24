# Cross-repo work

Moved verbatim from `.claude/CLAUDE.md`; that file keeps a short pointer.

## Cross-Repo Work

When a feature or bugfix requires changes across multiple repositories,
use `cross-repo-coordinator` skill instead of single-repo `task-decomposition`.

### When to use

A ticket requires cross-repo coordination when:
- An API changes in one repo and a consumer must be updated in another.
- A shared library is updated and dependent services need simultaneous bumps.
- A new feature spans a backend repo and a frontend repo.

### How it works

`cross-repo-coordinator` uses per-repo child tickets only when separate tracking
has concrete value (see `task-decomposition`); otherwise the parent owns the plan. It
tracks PR status across all repos, and gates all merges until:
1. Every per-repo sub-ticket has passed QA.
2. Integration tests pass (`CLAUDE_INTEGRATION_TEST_COMMAND`).

Only then does it coordinate the merge in dependency order and close the parent.

### Parent ticket as coordination anchor

Use the parent ticket ref for all cross-repo session notes:
```bash
bash ~/.claude/hooks/session-memory.sh append "[parent-ticket]" \
  "Cross-repo state" "Repo A: done | Repo B: in progress"
```

This allows any agent, in any repo session, to see the full cross-repo picture.

