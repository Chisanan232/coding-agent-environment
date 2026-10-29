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

## Retention and reclaim order (warm caches)

Keep a warm Cargo target when its cold rebuild is more expensive than the space it holds and the floor is not threatened.
When space must be recovered, take candidates in this order: reproducible with the cheapest rebuild first
(package caches, IDE caches), then foreign private targets idle longest with no handle, then superseded artifacts. Never
take the machine-wide shared target to hit a number, and never touch unique QA evidence or a frozen release candidate.

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
