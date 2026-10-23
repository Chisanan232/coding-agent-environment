# Permission rule consolidation: inventory and minimal reduction (SPE-118)

Measured 2026-10-24, read-only, against `~/.claude/settings.json` (sha256 `1feb301d…5f1ef`, unchanged), Claude Code 2.1.306. **Scope: the user file only.** Effective rules are the union of user, project and local files; this repository also tracks `.claude/settings.json` (191 allow, 209 ask, 0 deny as counted by the reviewer; it contains the same two merge allows named below, plus `rtk proxy` merge asks) and `~/.claude/settings.local.json` has 5 allow rules. Those two files were not classified here, no managed settings file was found, and every count and "no coverage lost" statement below is about the user file.
**Nothing here is applied.** Editing `~/.claude/settings.json` is itself an ask rule (`Edit(~/.claude/settings.json)`) and an owner-only change; the reduction below is a proposal for the owner to apply.

## What the documentation says (code.claude.com permissions, permission-modes, hooks, settings pages; read by a docs lookup, not re-tested here)

- Order is deny, then ask, then allow; the first match decides and specificity does not change that. A matching ask prompts even when a narrower allow also matches.
- In `auto` mode, allow/ask/deny rules resolve before the classifier, and "explicit ask rules still force a prompt". The classifier only sees what no rule decided. Broad allows that grant arbitrary code execution (`Bash(*)`, wildcarded interpreters, `Agent`, `Monitor`) are dropped on entering auto mode; narrow rules stay.
- `Bash(x:*)` and `Bash(x *)` are the same trailing wildcard (it also matches the bare command). `Bash(x*)` without the space also matches `xfoo`.
- Compound commands (`&&`, `||`, `;`, `|`, `|&`, `&`, newline): every subcommand needs its own allow match; an ask or deny matches if **any** subcommand matches, including inside subshells and substitutions.
- A PreToolUse hook that rewrites input via `updatedInput` changes what rules see: rules are evaluated against the rewritten command, so `rtk` rewrites mean rules for the original spelling do not carry over. A hook's "allow" never overrides ask or deny.
- Not stated by the docs: whether duplicate or shadowed rules have any effect, and whether a rewrite without `updatedInput` matches the original string. Those were not assumed.

## Inventory (266 ask, 182 allow, 0 deny)

Heuristic classification of the 266 ask rules by pattern text (a reading aid, not a proof):

| Class | Count | Examples | Reduce? |
|---|---|---|---|
| Merge authority | 41 | every `gh pr merge` spelling, `--admin`, `gh api …/merge`, GraphQL merge and auto-merge, the GitHub MCP merge tool (plain and `rtk` twins) | no; this is the ASK-first acceptance |
| Destructive or irreversible | 100 | force push and delete refs, `reset --hard`, `git clean -f`, `kill`, `gh repo delete`, `gh api … DELETE`, `terraform apply/destroy`, worktree remove or prune, Neon, Cloudflare and GitHub MCP deletes, `docker push` | no |
| Security or authority change | 28 | edits and writes to settings, hooks, `~/.claude/bin`, `~/.claude.json`, plugins; secret reads; credential create and revoke | no |
| Development guard or other | 89 | IAM bindings, package publishing, `.mcp.json` edits, worktree add `-B`/`-f` and move guards, `git --output`/`--no-index` (code-execution or overwrite vectors), cloud and payment MCP execute tools | no; these are protected operations, not routine work |
| Read-only | 4 | `git --ext-diff` (runs external programs), `Read(**/.env)` and `.env.local` (secrets) | no |
| GitHub lifecycle (other) | 4 | `gh release create`, `gh release upload` | no |

Shape: 107 plain Bash, 108 `rtk` twins, 51 non-Bash (MCP, Edit, Write, Read).

**Finding: almost no ask rule guards routine work.** About 95 percent protect destructive, publishing, credential, authority or merge operations, which is the intent. Routine prompts do not come from the ask list. They come from (a) compound shell calls, where an ask on any one subcommand prompts the whole call, (b) commands no allow rule matches, which in auto mode fall to the classifier, and (c) the deliberate ASK on a direct merge.

## Redundant, shadowed and obsolete rules

Method: textual glob comparison of each rule against the others (the same `*` semantics as above), then manual review. Matching is textual, so path rules were not treated as redundant: `Edit(~/.claude/settings.json)` vs `Edit(**/.claude/settings.json)` look nested but `~` and `**/` are not guaranteed to resolve the same, so both stay.

| Kind | Rules | Effect of removing |
|---|---|---|
| Exact duplicate spelling (`:*` and ` *`), ask | `Bash(git push -f:*)`, `Bash(rtk git push -f:*)`, `Bash(gh repo delete:*)`, `Bash(rtk gh repo delete:*)`, `Bash(gh pr merge:*)`, `Bash(rtk gh pr merge:*)`. The ` *` twin of each stays. | none: same matches |
| Dead allow, shadowed by an ask of the same text | `Bash(gh pr merge:*)`, `mcp__github__merge_pull_request` (allow side) | none today (ask wins). User-scope only: the same two allows also exist in the tracked project `.claude/settings.json`, so they stay inert only while the user-file merge asks survive. Dropping them from the project file would be its own reviewed change, not proposed here |
| Subset asks kept on purpose | 18 narrower asks inside a broader one, for example the `--admin` merge forms inside `gh pr merge`, `git push --force-with-lease` inside `--force*`, `gh api -X DELETE` inside `gh api *DELETE*` | none, so they are kept as defense in depth: they stay protective if a broad rule is later narrowed |

Net minimal reduction: ask 266 → 260, allow 182 → 180. **No routine prompt disappears from this**; rule count is the only gain. The broad allows `Bash(gh pr *)` and `Bash(rtk gh pr *)` also cover `gh pr merge` and are neutralized only by the merge asks; they are left alone, and no new allow rule is added.

## `rtk`

`rtk` is a rewrite hook, so rules see the rewritten command. That is why every protected rule has an `rtk` twin and why the twins must stay: dropping a twin would leave the rewritten spelling unguarded. `Bash(rtk:*)` as an allow would not override an ask (ask outranks allow), but it would resolve before the auto-mode classifier for every rewritten command no ask matches, which is a blanket pre-emption of the host's security classification, so it is not proposed. The trusted runner is invoked by absolute path, which the rewrite does not touch, so its single allow rule is evaluated on the literal text.

## What runs without a prompt, and what still needs authorization

Observed this session (agent side; I cannot see the prompt UI, only denials): `gh pr checks/view/list`, `git fetch`, the gate's `check` and `verify`, a standalone runner dry run, and read-only `gh api` calls ran with no denial. Compound or piped merge calls prompted. A single `gh pr merge … --match-head-commit` prompts by design (ASK).

Still requires host or owner authorization: every direct merge spelling; all destructive, publish, IAM, credential and settings operations above; and the runner's review evidence, which the host will not let the agent write for its own PR (`[Self-Approval]`). Those denials stand and are not worked around here.

## Apply and roll back

Owner applies, in an editor, outside an agent session:

1. `cp -p ~/.claude/settings.json ~/.claude/settings.json.pre-consolidation` (the rollback copy).
2. Remove from `permissions.ask` only: `Bash(git push -f:*)`, `Bash(rtk git push -f:*)`, `Bash(gh repo delete:*)`, `Bash(rtk gh repo delete:*)`, `Bash(gh pr merge:*)`, `Bash(rtk gh pr merge:*)`. Remove from `permissions.allow` only: `Bash(gh pr merge:*)`, `mcp__github__merge_pull_request`. The same two strings exist in both arrays, so edit each array separately. Keep every ` *` twin and every other rule byte-identical.
3. Parse the file as JSON and open `/permissions` to confirm 260 ask and 180 allow, and that `permissions.ask` still contains `Bash(gh pr merge *)`, `Bash(rtk gh pr merge *)` and `mcp__github__merge_pull_request`.
4. Validate with one standalone harmless command, and check that a merge still asks using a non-existent PR number (for example `gh pr merge 999999 --repo <repo> --merge --match-head-commit <sha>`), so a missed prompt fails harmlessly instead of merging.

Rollback: copy the saved file back. Original sha256 is `1feb301d42005b7402024431793e852a6f32b7b6456619eea3ce55eac0a5f1ef`.
