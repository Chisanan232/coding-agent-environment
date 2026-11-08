# Shared development resource lifecycle (SPE-130)

Personal Horonom hosts only. Reconciles SPE-109 (admission, reservations, orphan cleanup), SPE-117 (memory admission) and
SPE-103 (Glomeris). It adds the one thing none of them records: who owns a directory, whether anything uses it now, and
whether it holds unique evidence. It deliberately adds no daemon and no rival deletion engine: Glomeris keeps discovery,
policy and typed deletions (`docs/STORAGE-INTEGRATION.md`), and shared or redirected Cargo targets stay outside any
preauthorized scope (SPE-109, ADR-0001).

## Tool

`scripts/resource-lifecycle.py` (tests: `tests/test-resource-lifecycle.py`, synthetic fixtures only).

| Command | Effect |
|---|---|
| `inventory --self-session ID[,ID]` | Read-only. Measures every known consumer, classifies it, writes a report and one manifest per class under `~/.local/state/coding-agent-environment/gc/` (0700, files 0600, never `/tmp`). |
| `verify --manifest F` | Read-only. Re-measures each entry and reports any whose class changed. |
| `apply --manifest F --self-session ID [--ids a,b] [--dry-run]` | Deletes class A entries, or the named class B entries. Class C is refused. |

Each record has: logical bytes, allocated bytes, newest modification time, open handles and working directories (one `lsof`),
owner session and product, rebuild cost, protective markers and the receipt. Logical size is never reported as reclaimable.
`allocated_bytes` comes from `du` block counts, so APFS clones and shared blocks make it an **upper bound**; snapshots and
purgeable space are not in it at all. The only reclaim figure the tool prints is the measured change in filesystem free space
after each batch (`df` ground truth).

A target directory is only inventoried when a configured consumer glob matches it (`default_consumers`; `--consumers FILE` replaces the
list). Name patterns that are not in the list are invisible, however large: the 2026-11-02 audit found ~50 GiB of
`~/.cargo/qual-*`, `private-target-*` and `pkg-repro-target-*` outside every glob, and `~/.cargo/shared-target` (about 640 GiB) is one
unit whose `du` exceeds the 90 s limit, so it is reported with no size and no sub-units. Run `inventory --only cargo_target` as its own
pass; a full inventory also walks worktrees and caches and can exceed the load gate on a busy host.

## Classes

| Class | Meaning | Action |
|---|---|---|
| A | Owner receipt (`.ca-owner.json`) names this session, claims reproducible, no handle, no marker, no escaping link. A Cargo target also needs `"private": true`; the machine-wide shared target is never A. | `apply` deletes after revalidation. |
| B | Shared or foreign reproducible resources; scratch of sessions that are not alive; clean, reconciled worktrees. | Exact-scope manifest. `apply --ids` removes only the named entries, and the host permission prompt for that command is the ASK. |
| C | Credential-like names, unique-evidence names, `.ca-keep`, escaping symlinks, open handles, a live session, a dirty or unpushed worktree, an incomplete scan, unknown handles, unknown kind, protected roots. | Never deleted. |

Age alone is never a reason. Unknowns fail closed to C. A receipt may waive only the name-based evidence guess
(`"evidence_free": true`); credentials, keep markers and escaping links can never be waived.

Receipt (written by whoever creates a resource it will want reclaimed, e.g. a rehearsal or build wrapper):
`{"session_id": "<owner session id>", "reproducible": true, "private": true, "evidence_free": true}`.

## Revalidation and deletion

Manifests are accepted only from the private state directory (0700) as 0600 files owned by the user, and every entry must be
exactly what the inventory would have produced: the id is recomputed from the path, the real path must match, and the
(kind, path) pair must be re-discovered under the configured consumers, so an edited or stale manifest cannot point
`apply` at another directory. A deletion root may never equal or contain a protected root (`$HOME`, `~/.claude`,
`~/.codex`, `~/.ssh`, `~/Library`, the developer tree, ...). `apply` then re-measures and re-classifies each entry
immediately before deleting it. It skips the entry if the path is gone or
is now a symlink, the inode changed, a handle or live session appeared, a marker appeared, or the top levels changed since
the manifest (any class: a changed entry needs a fresh manifest). Deletion uses the fd-based symlink-safe remover and never follows a link. Every decision is appended to
`gc-audit.jsonl` (0600). A manifest generated for a different owner is refused.

## Hardening from the independent review (PR #138)

- Open-handle detection compares the root both as listed and resolved, case-folded (`/tmp` is `/private/tmp`).
- The protected-root guard compares case-folded, NFC-normalised paths and inodes (APFS is case-insensitive).
- Removal opens the resolved parent without following a link, looks the leaf up relative to that descriptor, requires the
  manifest's device and inode, and removes from the descriptor. A second `lsof` runs immediately before each deletion.
- Unknown session liveness (missing or unreadable sessions directory, an unparseable record) and unattributable scratch are
  class C, not "owner not alive".
- A receipt is read only when it is a small regular file owned by this user that is not group- or world-writable
  (not `umask 002` output); a forged receipt is ignored, so it cannot promote a directory to A.
- Worktrees are scanned six levels deep for credential and evidence names (dependency and build trees skipped), because
  `git status` does not show ignored local files such as `.env.local`. A failing `git status` is `unknown`, and git runs
  with `--no-optional-locks`.
- Known limits: nested mount points are not detected before `rmtree`; submodule checkouts look like worktrees; class B is
  always an exact-path ASK.

## Retention and reclaim order (warm caches)

Keep a warm Cargo target when its cold rebuild is more expensive than the space it holds and the floor is not threatened.
When space must be recovered, take candidates in this order: reproducible with the cheapest rebuild first
(package caches, IDE caches), then foreign private targets idle longest with no handle, then superseded artifacts. Never
take the machine-wide shared target to hit a number, and never touch unique QA evidence or a frozen release candidate.

## Cargo build-artifact lifecycle (SPE-161)

One policy for every Rust project on the host. It reuses admission classes, the 100 GiB floor and `apply`; it adds no hook, no
gate before commit or push, and no mandatory `cargo clean`.

**Status.** Implemented: the `CARGO_INCREMENTAL=0` default for `build-cold-isolated` (`disk-admission.py run`), whole-private-target removal
through `apply`, whole `<profile>/incremental` directories as a class B `cargo_incremental` kind (explicit `--ids`; `apply` takes Cargo's
build locks first and skips a target whose lock is held; a missing lock file fails closed; never `deps/`, never the target root), an
owner receipt written by `disk-admission.py run` and the queue claim into an isolated target that call itself creates (a `build-cold-isolated`
job with an explicit `CARGO_TARGET_DIR`; never the shared target, a symlinked path or an existing directory), `scripts/cargo-deps-reclaim.py`
for shared `debug/deps` (below), and the existing disk tiers. Nothing here runs automatically: every reclaim is an explicit command.

**Shared `debug/deps` reclaim (`cargo-deps-reclaim.py`).** `classify` is read-only, resumable by hash-prefix partition and time-bounded
(default 300 s per run; the full measured `deps/` of about 2.9 million files takes two runs). `apply` is a dry run unless `--execute`.
Attribution is the artifact's own recorded `CARGO_MANIFEST_DIR`; it removes only `rcgu.o`, `.o`, `.rlib` and `.rmeta` files whose root is
gone under a prefix that still exists, never `.d` metadata, executables or libraries (a QA ledger may reference a test binary). It holds
Cargo's build locks for the run (a lock it cannot take means a build is active and nothing is deleted), refuses a `.ca-keep` marker,
unlinks an inode only when every hard link is in the deletion set, re-checks inode, link count, mtime, age and the root (absent, no symlink on the way, prefix still mounted) right before each batch is
unlinked, and stops when a batch frees under 30% of its estimate. Estimates are hard-link-deduplicated and printed apart from the measured
free-space change, which is the only reclaim figure. Measured limit of the evidence: in the 2026-11-08 run only about 4% of the bytes
(about 9 GiB of 305 GiB) carried a recorded root that was gone; about 280 GiB had no recorded root and stays.

**Ownership.**

| Target | Owner and rule |
|---|---|
| Machine-wide shared target (`build.target-dir` in `~/.cargo/config.toml`) | Serial, interactive builds of compatible projects only (same toolchain, same profile and feature set, else fingerprints churn and the cache thrashes). Never cleaned wholesale, never deleted to hit a number, no per-artifact deletion by age (see below). |
| Isolated target (`CARGO_TARGET_DIR` under a private root, `build-cold-isolated`) | Owned by the task that made it. Class A only with a receipt (`.ca-owner.json` with `"private": true` and the applying session's id, see above); otherwise `apply` returns ASK. Removed by that task through `apply` when it finishes.  |
| QA evidence or release-candidate target | Unique output: keep it, mark it (`.ca-keep`), never reclaim it without the exact-path ASK. |
| Another project's target (`nda-targets`, `private-target-*`) | Foreign. Inventory only; class B ASK. |

Two projects never share one target when their toolchain, profile or feature set differ. A job that needs a concurrent build uses an
isolated target (admission refuses two jobs on one `cargo-target:` key, and `docs/RESOURCE-ADMISSION.md` sizes the reservation).

**Incremental compilation** is the measured source of runaway growth (357 GiB of session directories in the shared target,
19,352 entries, 264 GiB older than 30 days, SPE-109). Policy by workload:

| Workload | Policy |
|---|---|
| `build-cold-isolated` (one-shot QA, release, per-ticket cold build) | `CARGO_INCREMENTAL=0`, exported by `disk-admission.py run` when the job leaves it unset (`CLASS_ENV`). A job that sets it keeps its value. |
| Interactive iteration in the shared target | Keep Cargo's default; warm rebuild speed is the point. Old incremental sessions are swept by a bounded, reviewed one-off (see Status), never by `cargo clean`. |
| Real debugging that needs full symbols | Opt in per run (`CARGO_PROFILE_DEV_DEBUG=2`), not as the default. |

Reduced default debug information (`CARGO_PROFILE_DEV_DEBUG=line-tables-only`) and `split-debuginfo` are **not** changed
globally: they change fingerprints (one cold rebuild per flip) and need measured regression evidence per project first. Do not
edit `~/.cargo/config.toml`, a workspace `[profile]` or the toolchain for this policy.

**Reclaim scope.** Prefer, in order: (1) whole private targets the task owns; (2) Cargo's own scopes on a target the caller
owns (`cargo clean -p NAME`, `--profile NAME`, `--target TRIPLE`, `--dry-run` first; `--workspace` or no scope empties the
target); (3) whole `incremental` directories through the `cargo_incremental` kind of `resource-lifecycle.py` (manifest, explicit `--ids`, audit line); (4) `cargo-deps-reclaim.py` for the shared `debug/deps` with its recorded-root proof. Not allowed:
deleting files from the shared `debug/deps` by age, by crate-name prefix or by guessed `.o` ownership; touching `.fingerprint`
or `*.d` metadata; any shared-target deletion without proof of orphaning and a bounded plan. `.fingerprint` presence is not proof (measured: only 0.0025 GiB of
`deps/` lacked an entry). Accepted proof is the artifact's own recorded `CARGO_MANIFEST_DIR` (from its `.d` file) no longer existing on disk,
re-checked at deletion time, the file unchanged since the scan and older than a day, and every hard link of the inode removed (a partial
unlink frees nothing). Never during a running build of the same workspace. Uncertain artifacts are preserved and listed.

**Measuring.** `deps/` holds about a million tiny `*.rcgu.o` objects that Cargo hard-links (up to dozens of links per inode), so a per-entry
`st_blocks` sum over-counts (measured 82.5 GiB per entry versus 51.8 GiB of unique inodes in the same sample). Size and expected
reclaim are computed per unique inode; `du` or a shell glob over `deps/` does not finish in a reasonable time, so use one streaming
`scandir` pass with a five-minute cap and a TSV, never one subprocess per artifact.

**Thresholds.** The existing disk tiers apply unchanged: below 100 GiB heavy work is refused (`HEAVY_FLOOR`), the emergency latch sets below 20 GiB and releases
at 50 GiB. Warning is the `low` tier. A target larger than its class estimate (`disk_gib`) is a signal to
sweep or isolate, never an automatic deletion. Measure free space on the Data volume before and after every cleanup batch and stop on
any inconsistency.

**Cost trade-off.** Dropping the shared `debug/deps` cost is a cold rebuild of every dependency for every project that uses it
(minutes to tens of minutes per workspace on this host), so the cache is kept unless it is proven orphaned or the floor is at risk.
`CARGO_INCREMENTAL=0` costs warm rebuild speed only on isolated one-shot targets, where there is none to lose.

**Cleanup after a ticket or worktree.** The task lists the private targets it created in its closeout and removes them through
`apply` (class A); `post-merge-close` still removes only the worktree.

## Cleanup triggers

| Trigger | Action |
|---|---|
| Task or PR finished | The creator removes only what it made and receipted, through `apply`. Worktrees: owned, inactive, clean, fully reconciled only (never a broad prune). |
| Obsolete worktree | Appears as class B (clean, upstream-reconciled) in the next inventory; removed on an ASK for that exact entry. |
| Expired QA scratch | Scratch of a dead session is class B and may hold unique evidence. Evidence worth keeping belongs under `~/.local/state/` or in the repository, not `/private/tmp`. |
| Pressure tier `low` or lower | Run `inventory`, use the class B manifest for the ASK, never improvise `rm`. |

## Why `/private/tmp` is not a safe home for evidence

During this campaign a ledger and an owner packet written under `/private/tmp/claude-*/.../scratchpad` vanished, and several
scratch directories show one identical modification time. macOS ships a temp-file reaper (`/usr/libexec/dirhelper`) that
removes files unused for days. The deletion is consistent with that but unproven (there are no logs). Treat anything under
`/private/tmp` as disposable by the OS, not only by other sessions.

## Why the disk keeps refilling (measured on the personal host, 2026-10-29)

- The SPE-109 gate hook is installed but in **warn** mode (no `mode` file): the audit log holds about 1,100 heavy
  classifications, none ever denied, 73 of them while the tier was `low`.
- The snapshot refresher is not installed (the snapshot is days old) and no reservation exists. Nothing reserves bytes.
- Callers pick their own `CARGO_TARGET_DIR` (per-ticket private targets accumulate in `$HOME` and the developer tree).
- The gate sees Claude's Bash tool only. Codex, launchers, `make`/`just`/`npm run`, IDE analysis and product scripts bypass it.

## Operator decisions (not taken by the agent)

1. **Enforce mode** (create the `mode` file with `enforce`). At the current tier this immediately refuses all heavy work in
   every session, including release qualification, so the timing is the founder's decision.
2. **Install the snapshot refresher** (`disk-admission.py refresher-plist`, steps in `docs/STORAGE-INTEGRATION.md`).
3. **Per-product wrappers**: each product repo (AASM, Fornax, NDA, ...) runs heavy commands as
   `disk-admission.py run --bytes N --label L -- COMMAND`, and uses a bounded, named target directory. Each is a ticket and
   PR in that repository.
4. **Class B manifests** are deleted only after the founder's exact-path ASK.
