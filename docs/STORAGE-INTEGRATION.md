# Shared Glomeris integration

The existing Glomeris product owns discovery, evidence, safety policy, typed
execution, execution locking and disk-pressure monitoring. Storage Autopilot
is the historical initiative behind that implementation. This configuration
repository owns deployment pins and thin host integration; it has no cleanup
engine or separate monitoring daemon.

`codex/storage-core.json` pins source revision and command contract, rather than
pretending a post-release development revision is a published release. Install
through the existing entry point:

```bash
scripts/profile-install.sh --storage --dry-run
scripts/profile-install.sh --storage --apply --checkout /path/to/glomeris
scripts/sync-check.sh --storage
```

Apply requires an exact clean checkout from the pinned repository. It builds
committed bytes in an isolated local clone with `cargo build --locked --release`
and an explicit private target directory. It never modifies an existing shared
Cargo target or installs another product's binaries. Existing Cargo registry
content can be read/reused; ordinary Cargo dependency fetching is the only build
network capability. No LLM calls or workstation evidence uploads are added.

The installer checks the actual dev/release version string against the source
identity and validates the numeric/status JSON contract. It publishes executable
and ownership receipt together into a private content-addressed package under
`~/.local/share/coding-agent-environment/storage/glomeris/<sha256>/bin/glomeris`.
The new private `storage` child leaves the established shared parent and its
visual skills unchanged. Writable or symlinked ancestors refuse installation.
The shared local `storage/storage-core.json` atomically records current and previous
deployment identities (source revision, executable digest and absolute path).
Interrupted upgrades preserve both identities and the prior rollback target.
Legacy owned receipts remain readable; successful upgrades migrate the selection
to schema 1 without deleting legacy state. Corrupt, unowned, symlinked or drifted state refuses; it is
never silently overwritten. Check exits nonzero for missing, stale or drifted
installation. Report/dry-run do not build or write configuration.

A nonblocking kernel lock spans build/verify/publication. Busy exits 75. Immutable
per-package ownership allows retry after interrupted receipt publication and
reapply after rollback. Repeat installation validates and reuses the existing
matching executable without rebuilding. `profile-install.sh --storage --rollback`
restores the verified rollback target, including when the selected binary is
damaged; repeated rollback is idempotent and packages remain available. Legacy
sidecars identify only a validated legacy rollback target, not proven historical
predecessor order. First installation
has no prior deployment to restore. Rollback never changes host configuration,
services, source trees or other products. Runtime receipts/build logs/paths are
private state excluded from Git. Unknown staging directories from a crashed
installer are preserved for diagnosis; no generic cache pruning is introduced.

Both host-native `engineering-runtime` skills export `ENGINEERING_STORAGE_BIN`
only when source pin, package ownership and SHA-256 agree. Materialize the shared
runtime through existing profile installation; inspect its status before use.
Use the established Glomeris interface directly:

```bash
"$ENGINEERING_STORAGE_BIN" status --json
"$ENGINEERING_STORAGE_BIN" detect --project-root /path/to/project --json
"$ENGINEERING_STORAGE_BIN" explain RESOURCE --json
"$ENGINEERING_STORAGE_BIN" clean --dry-run --project-root /path/to/project
"$ENGINEERING_STORAGE_BIN" daemon status --json
```

Native execution remains policy-gated. Preserve PROTECTED/ASK decisions, missing
probe failures, fresh fingerprints and busy/error exit codes; never fall back to
raw deletion. Shared/redirected Cargo targets are not preauthorizable. Host
executable dependencies inside caches protect their native clean scope. A bound
workflow/merged PR never supplies cache authority.

The installer changes no `settings.json`, `config.toml`, `hooks.json`, trust,
notification transport, statusline, auth, history or user overrides. It enables
no autopilot standing grant. Native `glomeris daemon install` can enable the one
read-only monitor after operator validation; service installation is not claimed
by executable deployment. Session start/end and PR-merge invocation remain
separate observable gates; graceful SessionEnd cannot cover crashes.

Offline installer tests cover receipt publication, failure/rollback/reapply,
symlink/drift/source-pin checks, contention and idempotency. They use controlled
subprocess fixtures and prove no real Glomeris execution. The staged exact-source
build and CLI smoke gate supply actual executable compatibility evidence; core
safety and fresh-host validation have separate proof boundaries.

## Disk-pressure admission (SPE-109, advisory, not wired)

`scripts/disk-admission.py` is a thin admission adapter over Glomeris, not a
cleanup engine, registry or daemon: it reads free space from the pinned Glomeris
core `status --json` (statvfs is a cross-check and fallback, the smaller reading
wins), computes the founder tier from free bytes (Glomeris pressure levels are
advisory input only) and answers `check --weight heavy|light`. Exit 0 admits;
every non-zero exit, including 77 and usage errors, means refuse.

**No host-wide protection exists.** Nothing calls the adapter until an operator
installs the gate hook. Entry-point inventory:

| Entry point | Status |
|---|---|
| Claude Code Bash heavy commands through `.claude/hooks/disk-admission-gate.py` (PreToolUse) | Supported only after the operator runs `scripts/hook-rollout.py apply`. Not installed by default; verified stage by stage (see the rollout below). Covers the heavy commands listed under the hook behavior, nothing else |
| Codex, launchers (`ca-*`), installer/sync, resource-lock, Glomeris callers | Not integrated, pending later stages |
| `make`, `just`, `npm run <script>`, `xargs`, direct binaries, a user's own `!` shell, IDE analysis (flycheck, rust-analyzer) | Unsupported blind spots the hook cannot see |
| `$(...)` inside double quotes, aliases, variable-expanded commands | Not detected (best-effort token parsing) |

State lives in `~/.cache/coding-agent-environment/admission/` (0700, files
0600, home from the account database, symlinked path or ancestors refused):

- `state.json`: latch and last measurement. A missing file below 50 GiB counts
  as latched; a self-contradicting or corrupt one is latched and refuses.
- `snapshot.json` (`snapshot --json`): `schema, ts, free_bytes, tier, latch,
  heavy_lanes_allowed, active_heavy_jobs, reserved_bytes, protection, blockers,
  source`. A consumer computes age from `ts` and must treat age over 120 s (or a
  future `ts`) as stale and refuse heavy work. Other sessions can call the
  read-only `snapshot --check-fresh [--max-age N]` (exit 0 fresh; 77 stale,
  missing or unreadable). N may only tighten the rule: values above 120 or
  below 1 are refused with exit 77. The gate does not use the snapshot: it always asks the
  adapter for a fresh `check`. `heavy_lanes_allowed` is 0 below 100 GiB or while
  latched and 1 in stable and wide until a second-lane analysis is approved.
  `protection` stays `advisory-not-wired` until the gate is proven wired.
- `reservations/<label>.json`: `reserve --bytes N --pid P --label L` admits a
  heavy job only if the tier and latch admit it, a lane is free, P is a live
  process of the current user, and free bytes minus live reservations minus N
  stay at or above 100 GiB. The hold lasts at most 4 h (`--ttl-s`), or up to 24 h
  with an explicit `--max-hold`. A reservation records pid, process start time
  and expiry; one whose process is gone, whose start time differs (pid reuse) or
  that expired is ignored and deleted. A process that cannot be inspected stays
  counted. Only well-formed files of this schema are ever deleted, and
  `release --label L` deletes just that one. An unreadable or damaged file blocks
  reservations and is never deleted. These are byte claims for admission only;
  native Glomeris leases own resource claims and reaping.
- `mode`: `warn` or `enforce`, edited only by an operator. Absent or invalid
  means warn.

### Keeping the snapshot fresh (`refresher-plist`)

The snapshot is stale after 120 s, so something has to republish it. `disk-admission.py refresher-plist` prints a
launchd user agent (`com.coding-agent-environment.admission-snapshot`, every 60 s, `RunAtLoad`, no `KeepAlive`,
output to `/dev/null` so no log can grow); it installs nothing. Operator steps, run from the **stable merged
checkout** (never a worktree or temporary clone, and do not copy the script alone: the Glomeris reading is loaded
from the sibling `storage-integration.py`, and without it the snapshot silently falls back to `statvfs` only):

```bash
python3 scripts/disk-admission.py refresher-plist > ~/Library/LaunchAgents/com.coding-agent-environment.admission-snapshot.plist
plutil -lint ~/Library/LaunchAgents/com.coding-agent-environment.admission-snapshot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.coding-agent-environment.admission-snapshot.plist
python3 scripts/disk-admission.py snapshot --check-fresh        # exit 0 = fresh
# rollback
launchctl bootout gui/$(id -u)/com.coding-agent-environment.admission-snapshot && rm ~/Library/LaunchAgents/com.coding-agent-environment.admission-snapshot.plist
```

The interpreter and script paths are baked in when the plist is printed: a Homebrew Python upgrade that removes that
interpreter, or moving the checkout, makes the agent fail without any output. Diagnose with
`launchctl print gui/$(id -u)/com.coding-agent-environment.admission-snapshot` (last exit status) and by running
`snapshot --json` by hand. A fresh snapshot is not permission: it may report `volume-unreadable` or zero heavy lanes,
which consumers must read as refuse.

### Gate hook behavior

The hook is deny-only and never rewrites a command. It classifies the command
word of each simple command (newlines and heredoc bodies handled; env prefixes,
`cd x &&`, compound statements, `nice`/`sudo`/`timeout`/`watch`/`rtk proxy`
wrappers and `bash -c`/`eval` looked through, the latter marked best-effort).
In warn mode it allows and adds a context warning; in enforce mode it exits 2
when admission refuses or cannot be evaluated. A command that cannot be parsed
or exceeds 256 KiB is scanned for heavy tool names: enforce treats a match as
heavy, warn only advises. The hook consults tier and latch, not reservations.

**Enforce is a guardrail, not a security boundary.** The governed agent runs as
the same user as the operator, so it can edit the mode file or the state
directory, and software cannot prevent that. The hook flags any Bash command
that appears to write, move or remove under the admission directory
(redirects, tee, mv, rm, cp, `sed -i`, interpreters, `chmod`/`chflags`) as
tamper-suspect: denied in enforce, advisory in warn. A line that mentions the directory is
cleared only when every simple command on it is provably read-only (`cat`, `ls`, `tail`, `grep`,
`jq`, `find` without `-delete`/`-exec`, `sed` without `-i`, ... with output redirected to a literal
path elsewhere, `2>/dev/null` and `2>&1` included). Everything else stays suspect: other programs
that name it, variables, globs, `cd`/`pushd`, `xargs`, heredocs, process substitution, backticks,
interpreters (`python`, `perl`, `node`, ...) and unparsable lines. Reading the directory through an
interpreter is therefore still flagged (a known false positive). That detection is
best-effort. Operator hardening: `chmod 0444` and `chflags uchg` on the `mode`
file (`chflags nouchg` before an edit); leave `state.json` writable, because the
adapter updates it.

### Gate audit trail

The hook appends one JSON line per heavy or tamper-suspect classification to
`gate-audit.jsonl` in the admission directory (0600, rotated at 1 MiB keeping
one `.1` backup): `ts, mode, classification, tool, confidence, decision
(allow|warn|deny), tier, free_bytes, cmd_sha12`. It never stores the command
text, only the first 12 hex characters of its SHA-256 and the matched tool
name. Benign commands are not logged. An audit write failure never changes the
decision.

### Operator-set admission directory

The hook accepts `--admission-dir PATH` (absolute) on its command line in
`settings.json`; the default is `~/.cache/coding-agent-environment/admission`.
The mode file, state and audit file all live there, so acceptance runs use a
sandbox directory without touching the real state. Only the operator edits the
hook command in `settings.json`; the agent's Bash commands cannot change it
(an edit to `settings.json` is itself a protected operator action).

### Staged rollout: WARN, verify, ENFORCE

Install is the existing protected operator process, modeled on
`permission-rollout.py`: `scripts/hook-rollout.py plan|apply|verify|rollback`.

1. `plan` prints the live settings hash and hook file hash; nothing is written.
2. `apply --expect-live-sha H1 --expect-hook-sha H2` installs the merged hook
   file (0755, backup if one exists) and appends exactly one
   `PreToolUse` group `{matcher: Bash, command: python3
   ~/.claude/hooks/disk-admission-gate.py}`. Every other hook and key is kept,
   existing groups keep their order, and a receipt records the hash-exact
   backups. It refuses an unmerged hook file, a symlink, a wrong hash, a
   different existing gate entry, a formatting-changing rewrite without
   `--allow-reformat`, and an `enforce` mode file without `--allow-enforce`.
   Re-running it is a no-op.
3. WARN stage (default, absent mode file): the hook only adds context and
   writes audit lines. `verify` checks the hook is present once, the file hash,
   the mode file state and that no deny was added.
4. Observe `gate-audit.jsonl` over real use: confirm heavy commands show up with
   plausible `decision` values and no false positives on benign work.
5. ENFORCE stage: the operator writes `enforce` into the mode file (then
   `chmod 0444` and `chflags uchg` it). Re-run `verify --allow-enforce`.
6. `rollback --receipt FILE` restores the settings and any previous hook file
   hash-exact.

Post-install acceptance step (required): the real-session harness runs the hook
from the checkout with a sandbox `--admission-dir`, so it proves the
classification and decision logic (H1, H2, R1a, M1e) but **not** the installed
file path (`~/.claude/hooks/disk-admission-gate.py`, the settings command and
the production subprocess adapter). After `apply`, run one heavy command in a
real Claude session and confirm the **first WARN audit line** appears in
`gate-audit.jsonl` with the expected `tool`, `mode` and `cmd_sha12`; until that
line exists the install is not verified. The harness's opt-in P1 case
(`--include-production-path`) exercises the production subprocess path from the
checkout; the adapter resolves the home directory from the account database,
not `$HOME`, so P1 cannot be sandboxed and writes the real state file and one
audit line.

A hook argument other than a valid absolute `--admission-dir` (a typo, a
relative or `~` path) is a configuration error: the hook still never crashes,
but it advises loudly, records a `config-error` audit line and, in enforce,
refuses heavy commands. `hook-rollout.py` exit codes: 2 conflicting entry, 3 hash
mismatch, 4 symlink, 5 concurrent change, 7 unmerged, 8 reformat, 9 enforce
mode file, 10 settings it cannot edit safely (null `hooks`, root array,
non-UTF-8), 11 another apply running.

`--hook-dest` must be a normalised absolute path ending in
`disk-admission-gate.py` (exit 12 otherwise, nothing written): a relative path
would make Claude Code run repository-controlled code from each session's working
directory, and a `~` path would create a literal `~` directory.

Residual race: `apply` and `rollback` re-compare the live settings immediately
before the rename, but no lock that other writers honour exists, so a
microsecond lost-update window remains between that final compare and the rename.
Mitigation: apply only when no session is rewriting settings, and run `verify`
afterwards.

What "verified" means at each stage: after `apply`, file and settings state
match the pinned hashes (offline tests on fixture settings); after WARN use, the
audit shows the hook fires on real sessions; ENFORCE is verified only for the
supported scope and only with free space below the floor (the deny path).
`tests/acceptance/gate-acceptance.py` drives real `claude -p` sessions against a
sandbox; it is not part of CI. The admitted path (free space at or above
100 GiB) is not yet verifiable while the host is below that floor. Nothing here
claims host-wide coverage.

