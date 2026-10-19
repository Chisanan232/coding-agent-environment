# GitHub: `gh` first, GitHub MCP retired (SPE-117)

## Policy

GitHub work uses the authenticated `gh` CLI (`gh pr`, `gh issue`, `gh run`, `gh release`, `gh api`) and local `git`. The
failed GitHub MCP server is retired on the personal workstation: do not reconnect, rebuild, restart or repair its container, and
do not call `mcp__github__*` tools. This changes the interface only. Host authorization, the PR class A/B/C rules, required
checks, reviews and the independent merge gate apply exactly as before, and a host denial stays binding: it is never rerouted
through another tool. The `mcp__github__*` ask rules in `.claude/settings.json` stay in place as defense in depth should a server
ever be re-enabled. All unrelated MCP integrations are preserved.

## Inspect the active scopes (read-only)

```bash
python3 scripts/mcp-scope-inspect.py --project-root ~ --project-root <each repo root you use>
```

It lists every configured server by scope (user, local, project, managed), marks GitHub entries and prints the exact command that
would remove only that entry. It never prints arguments, environment values, headers or URLs, because those carry tokens, and it
starts no server, so it cannot touch the failed container. Servers supplied by plugins are not listed; disabling a plugin to drop one
server would also drop its other servers, so that is an owner decision.

Measured on the owner's workstation (2026-10-19): 23 entries; exactly one GitHub entry, at **user scope** (`github`, http); 22
unrelated entries (cloud, database, browser, memory, tracker integrations) at user, local and project scope.

## Retire it (owner-run: this edits `~/.claude.json`, a protected settings file)

```bash
cp -p ~/.claude.json ~/.claude/backups/claude.json.$(date +%Y%m%d-%H%M%S) && chmod 600 ~/.claude/backups/claude.json.*
claude mcp remove 'github' -s user
python3 scripts/mcp-scope-inspect.py     # must now report: GitHub entries: 0, same number of other entries minus one
```

Refresh the redacted live snapshot so `sync-check.sh` does not report drift: copy this repository's
`.claude/mcp-servers.runtime.json` over `~/.claude/mcp-servers.runtime.json` after reviewing `diff` (the only change is the removed
`github` entry). The repository snapshot already omits it.

Rollback: re-add the server with `claude mcp add` using your own notes (the repository does not hold its credentials). Restoring the
whole backed-up `~/.claude.json` is a last resort: it also reverts unrelated state Claude Code wrote since the backup (history, project data).

## What this change does not do

It does not edit any live settings or `~/.claude.json`, does not restart anything, does not touch managed settings, and does not
remove any other server.
