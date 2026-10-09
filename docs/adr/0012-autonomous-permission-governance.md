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
