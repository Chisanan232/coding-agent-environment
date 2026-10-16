# ADR-0012: Autonomous-first permission governance (Auto Mode `allow`/`ask`/`deny`)

## Status
Accepted (SPE-95); related defect tracked separately (SPE-83, unresolved)

## Context

Two live sessions (2026-10-03, 2026-10-10) audited why routine engineering
work was repeatedly interrupted for approval, and iteratively built a
least-privilege `permissions` profile directly on a live machine's
`~/.claude/settings.json`. This ADR captures the verified facts and the
resulting design before the portable subset is lost to session history.

### The originally reported problem

"Claude Code repeatedly asks for approval for ordinary development
operations" despite an expectation of near-zero interruptions for routine
work (git, gh, cargo, pytest, terraform plan, Jira/GitHub MCP reads and
writes).

### Verified root causes

Investigated, in order, with evidence — not assumed:

1. **No `bypassPermissions` was ever persistently configured.** Searched
   every settings file on the audited machine; none set
   `defaultMode: "bypassPermissions"`. `claude doctor` confirmed no
   managed/remote policy either (Pro account — managed settings require
   Enterprise/Team). A claim of "I already use bypassPermissions" did not
   match any discoverable config; the actual baseline was, and remains,
   `defaultMode: "auto"`.
2. **Auto Mode's classifier judges anything with no matching `allow`/`ask`/
   `deny` rule.** This is the documented, intended behavior — not a
   malfunction. The lever is which actions get a deterministic rule versus
   which stay under classifier judgment, not "is auto mode broken."
3. **A real RTK permission-rule-shadowing defect** (filed separately —
   [SPE-83](https://lightning-dust-mite.atlassian.net/browse/SPE-83)):
   RTK's `rtk hook claude` PreToolUse hook performs its own internal
   pre-check against the configured `allow` list and, for `git push`
   specifically, matches at `git <subcommand>` granularity — ignoring
   trailing flags. Reproduced: an `allow` rule as narrow as the exact bare
   string `Bash(git push)` caused RTK to self-approve
   `git push --force-with-lease`, bypassing a configured `ask` rule
   entirely, because RTK's own `permissionDecision: "allow"` in its hook
   response is authoritative and short-circuits Claude Code's native rule
   evaluation. Confirmed via a control test: with no `git push`-matching
   `allow` rule present, the same force-push correctly fell through to the
   native engine and the `ask` rule fired. **Consequence for this repo: no
   `allow` rule matching `git push` is tracked here — see Decision.**
   A second, narrower instance of the same bug class (ordinary
   literal-text matching, not RTK's shortcut) was found and fixed directly:
   `gh api repos/<path> -X DELETE` (method flag placed after the
   positional path — valid `gh` syntax) matched a naively-written
   `gh api repos/*` allow rule, bypassing a `-X DELETE`-first-ordered `ask`
   rule. Fixed by widening the `ask` patterns to `gh api * -X DELETE*` /
   `gh api * --method DELETE*` (wildcards on both sides) and not tracking
   a broad `gh api` allow rule at all.
   A third instance — found by an automated security review of this ADR's
   own first commit, then independently reproduced — hit the **deny**
   side instead of `ask`: a global flag placed *before* the subcommand
   (`terraform -chdir=/tmp destroy`, `gcloud --project=evil secrets
   versions access latest`) breaks the contiguous-prefix match
   `Bash(terraform destroy:*)` / `Bash(gcloud secrets versions access *)`
   require, since the flag now sits between `terraform`/`gcloud` and the
   dangerous subcommand. Fixed the same way: added
   `Bash(terraform * destroy*)`, `Bash(terraform * apply *-destroy*)` (and
   `--destroy`/`rtk`-prefixed variants), and `Bash(gcloud * secrets
   versions access*)` — wildcards covering the flag position, anchored
   precisely enough that `terraform plan -destroy` (read-only regardless
   of the flag — `plan` never mutates) is deliberately **not** caught,
   verified by `tests/test-permission-governance.py`'s
   `GlobalFlagReorderingDenyRegression`.
4. **The Auto Mode classifier itself can fail closed on transient API
   errors.** A real logged event (`automode-unavailable`, a `429` rate-limit
   error from the classifier's own backing call, found in a local session
   log) shows a routine read-only Jira fetch held back purely because the
   classifier's own request was rate-limited — not because the action was
   risky. This is Auto Mode's documented fail-closed safety behavior under
   classifier unavailability, correctly implemented, not a configuration
   defect. No setting in this repo changes this; the correct response to a
   transient failure is retrying the action, never relaxing the fail-closed
   default.
5. **No hook silently gates permissions beyond RTK's documented
   self-decision (point 3).** Every other `PreToolUse`-wired hook on the
   audited machine (`cbm-code-discovery-gate`, `fornax-hook-claude`) was
   invoked directly with synthetic payloads and confirmed to always exit 0
   with no `hookSpecificOutput` — never blocks, never asks.

### Why 60 `ask` rules became 47

The first pass (2026-10-03) added 60 `ask` rules by category (anything
matching "preserve explicit approval" from the original request). A second
pass (2026-10-10), explicitly instructed to classify by actual risk rather
than preserve all 60 by default, removed two overbroad categories:

- **Blanket `rm -rf *`** — was ask-gating the agent's own scratch/temp/
  build-artifact cleanup, which is routine. Removed; replaced with
  `autoMode.environment` guidance so the classifier judges by *authorized
  scope* (this task's own worktree/temp dirs vs. shared/ancestor/unfamiliar
  paths) instead of a hard block on the command shape.
- **`gh api * -X PATCH/PUT/POST*` (12 rules)** — treated every non-GET
  `gh api` call as equally dangerous regardless of target. A PR comment and
  a branch-protection change were gated identically. Removed; `-X DELETE`
  stayed gated (unambiguous regardless of target); `autoMode.environment`
  gained explicit classification guidance (content change vs.
  settings/security change) so the classifier — which can actually inspect
  the call's target and semantics — makes this distinction instead of a
  text rule that cannot.

## Decision

1. **Auto Mode (`defaultMode: "auto"`) remains the default.** Not
   `bypassPermissions` — `ask` rules are a deliberate override mechanism
   that `bypassPermissions` would make meaningless (per Claude Code's
   documented precedence, `allow` rules have no effect in
   `bypassPermissions` mode, but so would carefully-scoped `ask` rules lose
   their only point: forcing a prompt specifically where blanket bypass
   would not).
2. **Track the portable subset only.** Of the audited machine's 142
   `allow` / 47 `ask` / 12 `deny` rules, all 47 `ask` and all 12 `deny`
   rules were already portable (generic tool/command names, no embedded
   paths or project identifiers). Three `allow` entries were excluded as
   machine/corporate-specific (an absolute local script path; a
   `--project=<private-gcp-project>`-scoped `gcloud secrets` rule) — see
   `docs/SECURITY.md`. 139 of 142 `allow` rules ship here.
3. **No `allow` rule matches `git push` in any form.** Per root cause 3,
   this is the only way to guarantee RTK's self-decision shortcut cannot
   bypass the force-push `ask` rules. Ordinary pushes fall to the Auto Mode
   classifier instead — accepted as a determinism/latency cost, not a
   safety cost. Do not add one back without first re-verifying SPE-83's
   reproduction against the installed RTK version.
4. **`ask`, not `allow` or `deny`, is the mechanism for "important but not
   always wrong."** Force-push, `git reset --hard`/`clean -f`,
   `kill -9`/`pkill`/`killall`, `gh repo delete`, `gh api … DELETE`,
   `terraform apply` (can mutate/replace real infrastructure, including
   production, without needing `destroy`), arbitrary `gcloud` execution,
   production-tier Polar/payment mutation, and every Neon/Cloudflare
   delete-class tool stay `ask`-gated. These are the rules this repo
   considers **mandatory** — see `docs/SECURITY.md` for the authoritative
   list and rationale per item.
5. **`deny` is reserved for actions with no legitimate interactive use in
   this workflow**: `terraform destroy` (and `apply -destroy` variants),
   `rtk proxy` (documented debug-only raw-execution bypass — see SPE-83),
   `gcloud secrets versions access` (plaintext secret read), and
   `mcp__neon__get_connection_string` (an MCP tool whose return value *is*
   the secret — no settings-file mechanism can scope a parameter match
   here, only a full deny).
6. **`autoMode.environment` carries judgment, not more rules**, for cases a
   text pattern genuinely cannot classify correctly (deletion-scope,
   `gh api` method semantics, merge strategy, RTK's rewrite behavior). This
   is deliberately generic — organization names, trusted-repo paths, and
   other instance-specific trust context belong in a private profile
   overlay (`docs/PROFILES.md`), never this file.

## Consequences

- **Verified vs. assumed, stated explicitly**: everything in this ADR's
  root-cause list was reproduced with a command and its output (RTK
  self-decision bypass, `gh api` flag-order bypass, hook no-op behavior,
  absence of any `bypassPermissions` config) or is a real logged artifact
  (the classifier rate-limit event) — not inferred from documentation
  alone. What was **not** independently verified: behavior inside a
  genuinely separate live Claude Code session, Remote Control session
  launch behavior, or any session actually configured with
  `bypassPermissions` (none was found to exist). Tests added under this
  ADR are explicitly labeled synthetic for this reason — see
  `tests/test-permission-governance.py`.
- **SPE-83 remains open and unresolved.** This ADR's mitigation (no `git
  push` allow rule) is a permanent-until-fixed workaround on the
  **consuming** side (this repo's own settings), not a fix to RTK itself.
  Anyone adopting this repo's `settings.json` inherits the mitigation
  automatically, but re-adding a `git push` allow rule — here or in a
  private profile overlay — silently reintroduces the bypass. The
  `autoMode.environment` entry on command rewriting exists specifically to
  flag this for future editors.
- **`scripts/sync-check.sh` needed a semantic upgrade.** Its existing
  `.claude/settings.json` comparison required exact equality on each
  top-level key's full value — correct for small, fixed objects like
  `env`, wrong for `permissions.allow/ask/deny`, which a live machine
  legitimately extends with private, machine-specific entries (the three
  excluded `allow` rules above being exactly that case). Upgraded to a
  subset check: every rule this repo tracks must be present live; extra
  live-only rules are not drift. See the script and
  `tests/test-permission-governance.py` for the exact semantics.
- **Net rule count is not monotonically "more guardrails are safer."**
  Removing 13 `ask` rules (`rm -rf`, 12 `gh api` method variants) was a
  deliberate precision improvement, not a weakening — each removed rule
  was replaced by narrative guidance aimed at the one thing actually
  capable of judging target/semantics (the classifier), and no rule
  matching this ADR's "mandatory" list (Decision §4) was touched.

## Amendment 1 (SPE-105, 2026-10-15)

Founder-approved scoped refinements; Decisions 1-6 stand.

- **Merge**: `Bash(gh pr merge:*)` was already a static allow, yet Auto Mode's
  built-in `soft_deny` "Merge Without Review" still refused merges (a
  classifier judgment, not a rule). `autoMode.allow` now carries one
  evidence-gated entry (merge commit, pinned head SHA, green or documented
  CI-unavailable, no conflicts, owned/administered repo, authorized). It is
  prose to the classifier, not a blanket; `hard_deny` is untouched. Whether the
  classifier honors it is observed behavior, not a static guarantee.
- **Read-only git** (`status/log/diff/show/rev-parse/rev-list/ls-files/remote -v`)
  are static allows. `git * --output*` is `ask` (diff/log/show can write files).
  RTK's hook was verified not to self-approve any `ask`-shaped variant.
- **Worktrees**: blanket `git worktree *` allow replaced by
  list/add/repair/lock/unlock. `remove` with force in any flag position, `prune`
  and `move` are `ask`; plain `remove` falls to the classifier plus the
  ownership guidance in `autoMode.environment` (HORO-1700).
- **Secrets**: Decision 2's exclusion of project-scoped rules is retained. The
  public baseline does NOT gain `--project=<private>` rules; a tested template
  lives at `docs/examples/secret-metadata-overlay.example.json` for the
  private overlay. All four DENY families are unchanged; there is still no
  `git push` allow (Decision 3).
- **Review hardening (independent security + regression review)**: git accepts
  unambiguous long-option prefixes, so force and `--upload-pack` gates use
  `--f*` / `--upl*`; global-option forms (`git -C x worktree remove`) are `ask`;
  `worktree unlock`, `add -B`/force, `fetch --upload-pack` (executes a command)
  and `diff --no-index` (reads arbitrary files) are `ask`; `worktree repair` is
  bare-only. The secret-overlay template also asks on fully-qualified
  `projects/<other>/...` names, impersonation and `create --data-file`.
- **Known limit**: `Bash(gh pr merge:*)` / `mcp__github__merge_pull_request`
  remain static allows (unchanged); a static allow is decided before the
  classifier, so the merge prose only governs merge forms the classifier
  actually evaluates. It does not narrow those allows and is not claimed to.
  Pre-existing and out of scope: `git branch *` permits `-D`.
- **Glob limits (accepted, documented)**: ask globs are space-free (`git*--output*`)
  so position and shell quoting of a flag cannot hide it, and `worktree add` is
  allowed only for `-b`/`--detach` shapes. Text globs still cannot stop
  deliberate obfuscation (`--out"put"`), clustered short options after an
  allowed `-b` (`add -b x -qf`), implicit no-index reads (`git diff /dev/null
  <file outside the repo>`, equivalent to the already-allowed Read tool), or
  `git fetch ext::...` helpers (pre-existing `git fetch *` allow; a trailing
  `::*` is parsed as a prefix rule so no safe ask glob exists). These rules
  guard accidental misuse; adversarial obfuscation remains the classifier's job.

## Amendment 2 (SPE-106, 2026-10-16) — zero static deny and Auto Mode environment repair

**Decision (founder, 2026-10-16).** The personal Claude Code policy carries no
user-controlled static `deny`: `permissions.deny` is `[]`. Each former deny
pattern is an `ask`. This is personal policy; managed/organization settings are
never read or written, and the platform classifier defaults (the single
`hard_deny` "Data Exfiltration" and 72 `soft_deny` categories, including
"Merge Without Review", "Permission Grant", "Self-Modification" and "Credential
Materialization") cannot be removed through personal settings. Only `allow`
exceptions to `soft_deny` are possible; the one added here is the narrow,
transcript-verifiable pull-request merge entry below (an explicit exception to
"Merge Without Review"); the optional cross-repository personal-development entry (below, applied only with `--dev-dirs`) is the only other. No exception is added for the remaining categories.

**Why `ask` is a safe replacement (documented, not assumed).** `ask` prompts in
every permission mode, `bypassPermissions` included; `deny` also stays enforced
there. Unanswered, an `ask` in a non-interactive run is auto-denied, so
unattended sessions fail closed. Decision 5 of this ADR ("deny is reserved for
no legitimate interactive use") is superseded: the cost is a prompt where a
silent block used to be.

**Root causes found (evidence in SPE-106).**
1. The live `autoMode.environment` was a static paste of a generated config from
   one earlier session (working directory a non-git folder). It declared that
   folder the only trusted repo, "no remotes", "unknown" protected branches and
   no organization, and it omitted `"$defaults"`, which replaces the built-in
   section. Every session in every repository inherited it. It also called
   `git worktree remove --force` / `prune` routine, contradicting the
   ownership line and the ask rules. `claude auto-mode critique` flags the same
   mismatch.
2. SPE-105's `git worktree add*-f*` and `*-B*` matched any path containing
   `-f`/`-B` (ticket worktree paths contain `-full-`): 89 historical commands.
   Flags are now space-anchored (`add -f*`, `add * -f*`, `--f*` long prefixes).
3. `rtk proxy *` was hit 76 times by agents following RTK.md. It stays an `ask`
   because `rtk proxy` is the one command rtk does not rewrite and it bypasses
   rule matching; the friction fix is to stop recommending it (RTK.md is
   rtk-managed and outside this repo).
4. **Correction to Amendment 1.** The permission engine evaluates the
   rtk-rewritten command, not the typed one: a fresh-session test with an ask
   rule on only the plain form executed the command, while the `rtk` form
   blocked. The `rtk` twins are therefore required, not redundant. Commands rtk
   does not rewrite (`terraform init|validate|fmt`, `git checkout -b`) need no
   twin; `rtk terraform plan` did lack a bare-form match and is now `plan:*`.
5. Correction to Root Cause 1 of this ADR: "no `bypassPermissions` was ever
   configured" no longer holds for launches. Long-running sessions start with
   `--allow-dangerously-skip-permissions`, which adds `bypassPermissions` to the
   mode cycle (transcripts alternate `auto` and `bypassPermissions`). The
   default mode stays `auto`; the launcher is the owner's, not this repo's.

**Implemented.** Deny to ask, with the subsumed `apply -destroy` variants folded
into `terraform apply:*` / `terraform -* apply*` (the latter also asks when a
global flag precedes `apply`; anchoring on a leading `-` keeps
`terraform plan -out apply.tfplan` unprompted); `"$defaults"` first in
`autoMode.environment`; the merge allow restated in terms the classifier can
verify in the transcript (a user message naming the PR or branch and authorizing
the merge, `--merge` with `--match-head-commit`, the agent's own `gh pr checks`
output, an owner under the trusted GitHub owners entry; `--admin` only when the
user says so). Trusted GitHub owners stay out of the public baseline and are
supplied at rollout from `docs/examples/automode-trusted-sources.example.json`
under names that explicitly supersede the default "Organization"/"Source
control" entries. `scripts/permission-rollout.py` merges the baseline into a live
file: it hashes the exact bytes it merges, requires both expected hashes,
refuses a symlinked live file, writes exclusive-create backups, replaces the file
atomically through a private fsynced temp file with the original mode, and its
`rollback` validates the backup and saves the current file first. `apply` also
refuses any baseline that is not a tracked, committed file whose HEAD is contained
in `origin/main` (`--unmerged-ok` exists for rehearsals on copies only), and `verify
--baseline` checks that every baseline ask rule is present and that no catch-all
allow rule outranks the classifier. Global-option
`worktree remove` stays `ask` (ownership protection); global-option `worktree add`
force flags are `ask`.

**Review findings kept as limits.** (a) Quoted or clustered flags
(`worktree remove "-f"`, `add -qf`) and env-prefixed, absolute-path, `tofu` or
`terragrunt` spellings of the terraform commands are not matched by text globs
and fall to the classifier, as they did under the old deny rules. (b) The merge
entry cannot be proven to bind: `Bash(gh pr merge:*)`, `Bash(gh pr *)` and the
MCP merge tool remain static allows (accepted breadth, SECURITY.md); a static
allow should precede the classifier, yet earlier merge commands were
classifier-denied, so the entry is kept and its effect is unverified. It is an
explicit carve-out from "Merge Without Review", and its "no PR-body text" clause
only constrains what the classifier counts as evidence. (c) `terraform -* apply*`
still prompts on a few read-only commands when a global flag precedes it and an
argument contains the word (for example `terraform -chdir=x plan -out apply.tfplan`,
`terraform -chdir=x output apply_url`, `terraform -chdir=x workspace select
destroy-x`); accepted as the cost of covering `-chdir=` forms. `gcloud secrets`
metadata reads whose arguments contain `projects/` are no longer gated by the
baseline (the narrower `gcloud secrets*projects/*` ask lives in the private overlay).

**Personal-machine trust (founder clarification, 2026-10-16).** Trust is global to
the personal Claude Code installation, not to the directory a session started in.
It is applied at rollout from private parameters, never committed: `--owners`
(verified GitHub owners) fills the trusted-owner entries, and `--dev-dirs` (explicit
personal development roots) fills (a) one `autoMode.environment` line declaring
repositories and worktrees under those roots, existing and future, trusted
development context, (b) one narrow `autoMode.allow` exception so work in another
repository under the same roots is not "wandering into other repos", and (c)
`permissions.additionalDirectories`, the documented file-access grant (Read/Edit/
Write/Glob/Grep, recursive, same permission rules as the working directory, not
the Bash cwd). The tool resolves each root (`~`, symlinks, case) before judging it and accepts only
a directory strictly under the home directory (not a hidden, Library or
Applications folder, and not on a credential path) or `/Volumes/<name>/<dir>`;
`.`/`..` segments, `~user`, backticks and control characters are rejected. A later,
narrower `--dev-dirs` replaces the earlier grant instead of adding to it. Because
`additionalDirectories` makes reads under the roots silent, the tool also adds
`ask` rules for `Read(**/.env)` and `Read(**/.env.local)` (verified to prompt while
`.env.example` reads freely). The trust text excludes credential and key files,
`.env` files, the agent's own configuration (`.claude/`, CLAUDE.md, AGENTS.md), git
hooks and config, shell startup files, system paths, production resources,
irreversible deletion and uploads of private data. The documentation names no "trusted local directory" slot and does not
specify how auto mode weighs `additionalDirectories`, so (a) and (b) are
free-form prose whose effect is sampled, not proven. No external volume was
mounted when this was written; `/Volumes/...` roots are accepted by `--dev-dirs`.

**Known limits (disclosed).** Classifier verdicts are not statically testable;
fresh-session probes sample them. A statically allowed command can still be
blocked in a long-lived session whose transcript shows a denied attempt (the
classifier's documented preemptive block on read-only scouting); fresh sessions
did not reproduce the 13 denials seen after the SPE-105 rollout, and the cause
was not proven. Plain `git worktree remove` (no force) is left to the classifier
and the ownership environment line. `pkill`/`kill -9` asks (pre-existing) are the
largest source of ordinary prompts (230 historical matches).
