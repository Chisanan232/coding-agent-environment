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
