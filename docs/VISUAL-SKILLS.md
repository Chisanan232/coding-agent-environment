# Pinned visual skills

This repository pins two upstream visual skills in
[`codex/subtraction-skills.json`](../codex/subtraction-skills.json). That
manifest is the only versioned source declaration; skill bodies, plugin
metadata, renderers, and other upstream assets remain owned by their upstream
repositories and are materialized only in machine-local immutable snapshots.

| Package | Pin | Consumed source | Use |
|---|---|---|---|
| Archify | `v3.0.1` at [`2ab3cae7ac2c2a55d7386ca789d03c4fcd31816c`](https://github.com/tt-a1i/archify/tree/2ab3cae7ac2c2a55d7386ca789d03c4fcd31816c/archify) | `archify/` and `LICENSE` | Turn an idea, plan, or codebase into an interactive visual diagram. |
| Visual Explainer | `v0.12.0` at [`6376b7727bb9b1d5d37ad3e4d96d969a6499917b`](https://github.com/nicobailon/visual-explainer/tree/6376b7727bb9b1d5d37ad3e4d96d969a6499917b/plugins/visual-explainer) | `.claude-plugin/`, `plugins/visual-explainer/`, and `LICENSE` | Generate diagrams and visual reviews, recaps, fact checks, plans, and slides. |

The manifest digest covers each complete consumed tree using sorted relative
POSIX paths, normalized file modes, and each file's SHA-256 content digest. It
protects command files, templates, plugin metadata,
references, licenses, and renderer assets along with `SKILL.md`. A release tag
is descriptive; the full commit and digest are the integrity checks. Do not
float these references to a branch or update one package by editing its
materialized copy.

For UI/UX design work (as opposed to diagrams), see
[`UI-UX-POLICY.md`](UI-UX-POLICY.md), which records which design skills were
verified and which are not deployed.

## Host entry points

Codex receives Archify at `~/.agents/skills/archify` as a symlink into the
persistent immutable snapshot under
`$CODING_AGENT_SYNC_HOME/.local/share/coding-agent-environment/visual-skills/<name>/<SHA>`
(`$HOME` by default). Claude's `~/.claude/skills/archify` is also a symlink to
that snapshot.

Codex receives Visual Explainer as a **physical generated copy** of the full
upstream leaf directory at
`$CODING_AGENT_SYNC_HOME/.agents/skills/visual-explainer`. The copy preserves
the verified snapshot's plugin metadata and files without editing upstream
bytes. The bundled Node quick-render CLI compares `import.meta.url` with its
argument path; invoked through a symlink, that check exits successfully without
writing HTML. A physical directory makes the upstream command work, so this
managed leaf must remain a real directory. The unrelated upstream
`skills/visual-explainer` symlink is excluded from the consumed tree.
Codex 0.160 discovers the native skill as
`$visual-explainer:visual-explainer`. Claude's slash-command names below are
not Codex commands.

Claude receives Archify through `~/.claude/skills/archify`. Visual Explainer
remains an unmodified native plugin, backed by the persistent local snapshot
through Claude's local marketplace. Its supported namespace is
`/visual-explainer`; available commands are:

- `/visual-explainer:generate-web-diagram`
- `/visual-explainer:diff-review`
- `/visual-explainer:plan-review`
- `/visual-explainer:project-recap`
- `/visual-explainer:fact-check`
- `/visual-explainer:generate-slides`
- `/visual-explainer:generate-visual-plan`

See the upstream [Visual Explainer plugin](https://github.com/nicobailon/visual-explainer/tree/6376b7727bb9b1d5d37ad3e4d96d969a6499917b/plugins/visual-explainer)
for command behavior and [Claude's plugin reference](https://code.claude.com/docs/en/plugins-reference#standard-layout)
for native plugin layout. Use the plugin's documented `SKILL.md` as the
Codex interface; the Claude slash commands remain Claude-specific.

The local marketplace is registered as `visual-explainer-marketplace` and
points into the verified persistent Visual Explainer snapshot. Its ownership
receipt is stored beside the snapshots at
`$CODING_AGENT_SYNC_HOME/.local/share/coding-agent-environment/visual-skills/receipt.json`.
The helper manages that native registration through Claude's user-scope plugin
CLI; it does not rewrite Claude settings files or include marketplace paths,
plugin state, or credentials in Git.

## Install and check

Preview and apply the pinned packages with the focused installer, then check
the managed Archify symlinks, Codex Visual Explainer physical copy, and native
Claude plugin registration:

```bash
scripts/profile-install.sh --visuals --dry-run
scripts/profile-install.sh --visuals
scripts/profile-install.sh --visuals --check
scripts/sync-check.sh --visuals
```

Dry-run and check are offline and read-only: they do not fetch source or
install/update the Claude plugin. They query local marketplace/plugin state
through the installed `claude` CLI. Apply verifies the tag/commit and
complete-tree digest, stages a new immutable snapshot, then reconciles the
managed Archify symlinks, Codex Visual Explainer copy, and native Claude
Visual Explainer marketplace/plugin install.
Bootstrap is idempotent;
repeating apply at the same pins leaves the materialized state unchanged.
Start a new Codex session after applying. For Claude, restart/reload the host
and confirm the plugin skill and commands are enumerated. A successful install
is not session discovery proof. Visual Explainer's local marketplace source
and native user-scope plugin registration both refer to the verified
persistent snapshot; plugin files remain at their upstream paths.

The installer refuses foreign files or directories at managed targets,
unexpected marketplace ownership, symlinked managed ancestors, and modified
or corrupt snapshots. It does not adopt a same-named foreign installation.
Fetch or verification failure leaves the active installation unchanged.
Codex Visual Explainer copy activation stages the full physical leaf and uses
two atomic directory renames: the prior managed directory is retained before
the staged copy becomes active. A receipt records `codex_copy` ownership and
allows interrupted rename recovery to resume. The pair of renames is
recoverable; the whole upgrade is not atomic. Native Claude CLI failure is
reported with completed steps for recovery, and the two hosts are not a
cross-host atomic transaction. Old verified snapshots are retained across
upgrades. No generated skill bodies, marketplace paths, machine-local
configuration, or authentication state belong in Git.

## Updating a pin

Resolve a release tag to a full commit SHA, verify the tag resolves to that
commit, then export only the consumed roots into a temporary directory. For
example, replace the tag and SHA below with the reviewed Archify release, and
repeat with the Visual Explainer repository and its three consumed roots:

```bash
visual_pin_tmp="$(mktemp -d)"
trap 'rm -rf "$visual_pin_tmp"' EXIT
mkdir -p "$visual_pin_tmp/archify-repo" "$visual_pin_tmp/archify-tree"
git -C "$visual_pin_tmp/archify-repo" init
git -C "$visual_pin_tmp/archify-repo" remote add origin https://github.com/tt-a1i/archify.git
git -C "$visual_pin_tmp/archify-repo" fetch --depth 1 origin refs/tags/v3.0.1
git -C "$visual_pin_tmp/archify-repo" show -s --format=%H 'FETCH_HEAD^{commit}'
git -C "$visual_pin_tmp/archify-repo" archive 'FETCH_HEAD^{commit}' archify LICENSE | tar -x -C "$visual_pin_tmp/archify-tree"
python3 scripts/visual-skills.py --print-digest "$visual_pin_tmp/archify-tree"
```

Confirm the printed commit equals the intended release SHA before using that
SHA and digest in `codex/subtraction-skills.json`. For Visual Explainer, fetch
`https://github.com/nicobailon/visual-explainer.git` at the reviewed tag and
archive only `.claude-plugin`, `plugins/visual-explainer`, and `LICENSE` to its
own temporary tree before running `--print-digest` on that tree. For each file,
the helper frames the relative path, normalized mode (`0644` or `0755`), and
fixed 32-byte SHA-256 content digest with NUL separators, then hashes the
sorted stream. The exported consumed tree contains no `.git` metadata. The helper prints the raw hex tree
digest. Store it in the manifest as `sha256:<printed-hex>` in that package's
`integrity` field.

Review the exact upstream diff—including commands, plugin metadata, references,
and bundled assets—then update the package tag, commit, and digest together.
Apply and check both hosts, review the local plugin's resolved `installPath`,
and verify Codex discovers `$visual-explainer:visual-explainer` and Claude
resolves the new pin after restart/reload. Preserve the previous snapshot and
prior Codex copy through activation. Never replace a pin with a floating
branch or claim both hosts updated solely because the fetch succeeded.

## Acceptance evidence

Acceptance requires checks for installer dry-run, offline check, source integrity,
foreign-target refusal, upgrade behavior, and Claude/Codex discovery. A human
acceptance pass should also render one small Archify diagram and one Visual
Explainer quick render from the installed paths to prove their bundled assets
resolve. Record native session discovery and those render results separately;
repository installation checks alone do not prove a running host loaded the
skills. Renderer behavior may depend on model/session availability or external
quota, so record those as runtime conditions rather than installer failures.

Verified on 2026-10-06 with Claude Code 2.1.274, Codex 0.160.0, and Node
23.11.0: fresh native catalogs enumerated both skills; Claude also enumerated
all seven Visual Explainer commands. Archify doctor and its tiny architecture
finalize gates (including browser checks) passed. The unchanged Visual Explainer
Node command created a nonempty report whose diagram and table passed browser
DOM checks. The 15 offline integration tests, existing profile/routing/workflow
regressions, secret scanning, repeat apply, and managed drift checks passed.
Claude model execution remained externally blocked by the weekly quota until
2026-10-08 02:00 Asia/Taipei; native discovery and renderer evidence are separate
from that unavailable model-session check.

## Frontend design skills

The separate `visuals.frontend` section in the same external-source manifest controls design skills. Each entry names the role, activation state, upstream commit, license, consumed paths, skill directory and complete-tree digest. The `--frontend` selection operates independently of the diagram packages, which have different native host layouts. Both selections use the same Git snapshot and integrity verifier. There is no requirement to install a whole marketplace.

```sh
scripts/profile-install.sh --frontend --dry-run
scripts/profile-install.sh --frontend
scripts/sync-check.sh --frontend
scripts/profile-install.sh --frontend --rollback
```

Set `CODING_AGENT_SYNC_HOME` to a disposable directory for acceptance. If set, host-specific home overrides must point into that same home. Dry-run and check read only local state. Apply fetches the pinned Git objects for a missing active snapshot and verifies its digest before linking the same skill leaf into the Claude and shared agent skill directories. A quarantined entry has no managed discovery link. Changing an entry to quarantined removes only links recorded in the installer receipt and retains the verified snapshot. A same-named foreign entry blocks reconciliation.

The private frontend receipt under the visual snapshot store records the previous active pins before the links change. A link upgrade can briefly leave one host without the selected name. Reapply the same manifest after an interruption to fill that gap. For an interrupted rollback, repeat rollback. Rollback verifies the previous snapshots offline and restores the preceding link set, including an empty first-install baseline. A check against a newer manifest can legitimately show drift afterward. To roll back beyond one successful pin change, apply the reviewed earlier manifest, retaining quarantined declarations for retired names. Third-party bodies and receipts stay outside the repository.

Use a fresh Claude SDK command catalog or CLI session and a fresh Codex app-server `skills/list` to verify native discovery. Keep plugin enablement, skill discovery, invocation and rendered evidence separate. Use supported host controls for plugin overlaps. Record the prior setting before changing it and restore only that setting on rollback. The portable Codex reference suppresses only `build-web-apps:frontend-app-builder` with a qualified `[[skills.config]]` name selector. A native `skills/config/write` request for that name can change an installed profile without replacing its TOML. Generation 1 materializes the same reviewed Anthropic skill from an immutable pin on both hosts. After verifying the replacement, retire its duplicate Claude marketplace route with `claude plugin disable frontend-design@claude-plugins-official --scope user`. Restore an originally enabled plugin with the corresponding enable operation. An absent optional plugin needs no uninstall. Preserve browser, accessibility and engineering skills in multi-capability plugins.

### First-party creative workflow

`creative-frontend-engineering` is original instruction-only material. Its four local references cover art direction and product architecture, motion and input, scenes and assets, and rendered critique. Version 3 retains the implementation and verification role. Its five-file SHA-256 is `9f40f74a6b2765d62b7d12a326f5990748289f138514252b446a534ba1d30333`. Git tracks the authored tree; the shared installer owns its host copies. The two manifests serve different sources: authored files in `codex/shared-skills.json`, external snapshots in `codex/subtraction-skills.json`.

Install or check the two first-party entries without synchronizing unrelated profiles. Save the backup ID printed by each changed host. Use the corresponding ID to restore only that host's changed files. Use the creating repository revision for recovery. The host directory lock serializes profile updates. A pending journal retains the original rollback boundary across an interrupted apply. Restore checks ownership, file contents and modes before changing selected files. Repeat an interrupted restore with the same ID. A foreign same-name file requires manual reconciliation.

```sh
selection=--shared-skills=creative-frontend-engineering,acceptance-validation
scripts/profile-install.sh --claude-skills "$selection"
scripts/profile-install.sh --global "$selection"
scripts/profile-install.sh --claude-skills "$selection" --check
scripts/profile-install.sh --global "$selection" --check
# If reverting a changed installation:
scripts/profile-install.sh --claude-skills "$selection" --restore-shared="$claude_backup_id"
scripts/profile-install.sh --global "$selection" --restore-shared="$codex_backup_id"
```

For each relevant brief, use the [capability coverage](FRONTEND-COVERAGE.md), [specialist source review](FRONTEND-REFERENCES.md) and [bounded evaluation matrix](../evals/frontend/MATRIX.md) as needed.

### Configuration history

The frontend configuration uses an integer generation under `visuals.frontend.version`. A verified generation may receive the annotated tag `frontend-skills/v<N>` on the reviewed merge commit. Increase the generation when changing the active set, its pins or the versioned authored instructions it delivers. Preserve earlier tags. Generation 1 activates the pinned Anthropic primary across both hosts and the first-party creative engineering references. It introduces scoped installation, quarantine, offline rollback and the B01–B14 evaluation matrix. See the [capability coverage](FRONTEND-COVERAGE.md) and [technical reference review](FRONTEND-REFERENCES.md) for per-capability evidence. The [source review](FRONTEND-SOURCE-REVIEW.md) records candidate-specific evidence. The [task policy](UI-UX-POLICY.md) defines fallback behavior for vacant design roles.

Generation 2 delivers version 3 of the authored recipe and its matched-playback policy through the scoped selector above. External package pins and primary routing carry forward from generation 1.
