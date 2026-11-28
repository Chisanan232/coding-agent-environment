#!/usr/bin/env python3
"""Reconcile pinned visual skills for Claude Code and Codex."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


OWNER = "coding-agent-environment.visual-skills.v1"
MARKETPLACE = "visual-explainer-marketplace"
PLUGIN_ID = f"visual-explainer@{MARKETPLACE}"
PACKAGE_PATHS = {
    "archify": ["LICENSE", "archify"],
    "visual-explainer": ["LICENSE", ".claude-plugin", "plugins/visual-explainer"],
}


class VisualSkillsError(Exception):
    pass


def digest_tree(path: str | Path) -> str:
    """Hash regular files using sorted relative paths, normalized modes and bytes."""
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise VisualSkillsError(f"Tree root must be a regular directory: {root}")
    entries: list[tuple[str, Path, int]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            child = current_path / name
            if child.is_symlink():
                raise VisualSkillsError(f"Tree contains a symlink: {child.relative_to(root)}")
        for name in files:
            child = current_path / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise VisualSkillsError(f"Tree contains a non-regular file: {child.relative_to(root)}")
            mode = 0o755 if info.st_mode & 0o111 else 0o644
            entries.append((child.relative_to(root).as_posix(), child, mode))
    hasher = hashlib.sha256()
    for relative, file_path, mode in sorted(entries, key=lambda row: row[0]):
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(f"{mode:04o}".encode("ascii"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(file_path.read_bytes()).digest())
        hasher.update(b"\0")
    return hasher.hexdigest()


def reject_symlink_chain(path: Path, boundary: Path) -> None:
    """Reject symlinked managed ancestors between path and the selected home."""
    try:
        relative = path.absolute().relative_to(boundary.absolute())
    except ValueError as exc:
        raise VisualSkillsError(f"Managed path escapes selected home: {path}") from exc
    current = boundary.absolute()
    if current.is_symlink():
        raise VisualSkillsError(f"Refusing symlinked managed home: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise VisualSkillsError(f"Refusing symlinked managed path: {current}")


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VisualSkillsError(f'Duplicate key in managed JSON: {key}')
        result[key] = value
    return result


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique_keys)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise VisualSkillsError(f"Cannot read valid JSON from {path}: {exc}") from exc


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".visual-skills-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_manifest(root: Path) -> dict[str, dict[str, Any]]:
    manifest_path = root / "codex/subtraction-skills.json"
    data = read_json(manifest_path, None)
    packages = (data or {}).get("visuals", {}).get("packages")
    if not isinstance(packages, dict) or set(packages) != set(PACKAGE_PATHS):
        raise VisualSkillsError(f"{manifest_path} must declare exactly Archify and Visual Explainer packages.")
    for name, expected_paths in PACKAGE_PATHS.items():
        spec = packages[name]
        if not isinstance(spec, dict):
            raise VisualSkillsError(f"Invalid visual package metadata: {name}")
        if spec.get("paths") != expected_paths:
            raise VisualSkillsError(f"Unexpected consumed paths for {name}; expected {expected_paths}.")
        for field in ("repository", "tag", "revision", "integrity"):
            if not isinstance(spec.get(field), str) or not spec[field]:
                raise VisualSkillsError(f"Visual package {name} is missing {field}.")
        if len(spec["revision"]) != 40 or any(c not in "0123456789abcdef" for c in spec["revision"].lower()):
            raise VisualSkillsError(f"Visual package {name} must pin a full Git revision.")
        if not spec["integrity"].startswith("sha256:") or len(spec["integrity"]) != 71:
            raise VisualSkillsError(f"Visual package {name} must pin a SHA-256 tree digest.")
    return packages


def validate_paths(paths: list[str]) -> None:
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(part in ("", ".", "..", ".git") for part in value.split("/")):
            raise VisualSkillsError(f"Unsafe package path: {value}")


def run_git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        suffix = f" ({detail[-1]})" if detail else ""
        raise VisualSkillsError(f"Git operation failed{suffix}; verify the pinned repository and network access.")
    return result.stdout.strip()


def fetch_snapshot(name: str, spec: dict[str, Any], snapshots: Path) -> Path:
    paths = spec["paths"]
    validate_paths(paths)
    expected = spec["integrity"].removeprefix("sha256:")
    target = snapshots / name / spec["revision"]
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise VisualSkillsError(f"Pinned snapshot path is occupied by an unowned object: {target}")
        actual = digest_tree(target)
        if actual != expected:
            raise VisualSkillsError(f"Pinned snapshot is corrupt: {target} (expected {expected}, found {actual}).")
        return target

    snapshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{name}-fetch-", dir=snapshots) as temporary:
        temp_root = Path(temporary)
        checkout = temp_root / "checkout"
        run_git(["init", "--quiet", "--bare", str(checkout)])
        ref = f"refs/tags/{spec['tag']}" if spec.get("tag") else spec["revision"]
        run_git(["remote", "add", "origin", spec["repository"]], cwd=checkout)
        fetch = ["fetch", "--quiet", "--depth=1", "--no-tags"]
        if not spec.get("tag"):
            # The creative collection contains large demo assets outside the reviewed roots.
            run_git(["config", "remote.origin.promisor", "true"], cwd=checkout)
            run_git(["config", "remote.origin.partialclonefilter", "blob:none"], cwd=checkout)
            fetch.append("--filter=blob:none")
        run_git([*fetch, "origin", ref], cwd=checkout)
        revision = run_git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=checkout)
        if revision != spec["revision"]:
            raise VisualSkillsError(f"Fetch does not resolve to pinned revision for {name}.")
        tree = temp_root / "snapshot"
        tree.mkdir()
        # Read Git blobs without checking out filters, hooks or submodules.
        listing = run_git(["ls-tree", "-rz", revision, "--", *paths], cwd=checkout)
        for entry in listing.rstrip("\0").split("\0"):
            if not entry:
                continue
            header, relative = entry.split("\t", 1)
            mode, kind, oid = header.split()
            validate_paths([relative])
            if kind != "blob" or mode not in ("100644", "100755"):
                raise VisualSkillsError(f"Pinned source contains an unsupported file: {name}/{relative}")
            target_file = tree / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            with target_file.open("xb") as output:
                subprocess.run(["git", "cat-file", "blob", oid], cwd=checkout, stdout=output, check=True)
            target_file.chmod(0o755 if mode == "100755" else 0o644)
        if any(not (tree / root).exists() for root in paths):
            raise VisualSkillsError(f"Missing consumed root for {name}.")
        actual = digest_tree(tree)
        if actual != expected:
            raise VisualSkillsError(f"Fetched {name} tree digest mismatch (expected {expected}, found {actual}).")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(tree, target)
        except FileExistsError:
            if digest_tree(target) != expected:
                raise VisualSkillsError(f"Concurrent snapshot creation produced a corrupt tree: {target}")
        for current, dirs, files in os.walk(target):
            directory = Path(current)
            for filename in files:
                item = directory / filename
                item.chmod(0o555 if item.stat().st_mode & 0o111 else 0o444)
            directory.chmod(0o555)
    return target


def claude_env(sync_home: Path) -> dict[str, str]:
    config_dir = (sync_home / ".claude").absolute()
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured and Path(configured).absolute() != config_dir:
        raise VisualSkillsError("CLAUDE_CONFIG_DIR must match CODING_AGENT_SYNC_HOME/.claude for visual skill operations.")
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home and Path(codex_home).absolute() != (sync_home / ".codex").absolute():
        raise VisualSkillsError("CODEX_HOME must match CODING_AGENT_SYNC_HOME/.codex for visual skill operations.")
    environment = os.environ.copy()
    environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return environment


_CLAUDE_ENV: dict[str, str] | None = None


def claude_json(args: list[str]) -> Any:
    try:
        result = subprocess.run(["claude", *args], text=True, capture_output=True, env=_CLAUDE_ENV)
    except FileNotFoundError as exc:
        raise VisualSkillsError("Claude CLI is required to inspect or manage the user marketplace.") from exc
    if result.returncode:
        raise VisualSkillsError(f"Claude CLI failed: claude {' '.join(args)} (exit {result.returncode}). {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VisualSkillsError(f"Claude CLI returned invalid JSON for {' '.join(args)}.") from exc


def native_state() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plugins = claude_json(["plugin", "list", "--json"])
    marketplaces = claude_json(["plugin", "marketplace", "list", "--json"])
    if not isinstance(plugins, list) or not isinstance(marketplaces, list):
        raise VisualSkillsError("Claude plugin list commands returned unexpected JSON shapes.")
    return plugins, marketplaces


def marketplace_path(entry: dict[str, Any]) -> Path | None:
    raw = entry.get("path") or entry.get("installLocation")
    return Path(raw).absolute() if isinstance(raw, str) else None


def link_state(target: Path, expected: Path) -> bool:
    if not os.path.lexists(target):
        return False
    if not target.is_symlink():
        raise VisualSkillsError(f"Refusing to replace a foreign managed skill path: {target}")
    current = Path(os.readlink(target))
    current_abs = (target.parent / current).absolute() if not current.is_absolute() else current.absolute()
    if current_abs != expected.absolute():
        raise VisualSkillsError(f"Managed skill path points elsewhere; refusing to replace it: {target} -> {current}")
    return True


def owned_link_state(target: Path, allowed: set[Path]) -> Path | None:
    """Return a managed symlink's target only when its owner recorded that target."""
    if not os.path.lexists(target):
        return None
    if not target.is_symlink():
        raise VisualSkillsError(f"Refusing to replace a foreign managed skill path: {target}")
    raw = Path(os.readlink(target))
    current = (target.parent / raw).absolute() if not raw.is_absolute() else raw.absolute()
    if current not in {item.absolute() for item in allowed}:
        raise VisualSkillsError(f"Managed skill path points outside recorded snapshots: {target} -> {raw}")
    return current


def set_link(target: Path, expected: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".visual-skills-{os.getpid()}"
    if os.path.lexists(temp):
        raise VisualSkillsError(f"Temporary managed link path is occupied: {temp}")
    os.symlink(expected, temp)
    os.replace(temp, target)


def verify_plugin_content(plugin: dict[str, Any], snapshot: Path) -> bool:
    path = plugin.get("installPath")
    if not isinstance(path, str):
        return False
    try:
        return digest_tree(path) == digest_tree(snapshot / "plugins/visual-explainer")
    except (OSError, VisualSkillsError):
        return False


def receipt_for(path: Path) -> dict[str, Any] | None:
    data = read_json(path, None)
    if data is None and not path.exists():
        return None
    if not isinstance(data, dict) or data.get("owner") != OWNER or data.get("version") != 1:
        raise VisualSkillsError(f"Visual skill receipt is not owned by this installer: {path}")
    return data


def run_claude(args: list[str]) -> None:
    result = subprocess.run(["claude", *args], text=True, capture_output=True, env=_CLAUDE_ENV)
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        suffix = f" {detail[-1]}" if detail else ""
        raise VisualSkillsError(f"Claude CLI failed: claude {' '.join(args)} (exit {result.returncode}).{suffix}")


def frontend_catalog(root: Path) -> dict:
    manifest = read_json(root / 'codex/subtraction-skills.json', {})
    catalog = manifest['visuals']['frontend']['packages']
    shared = read_json(root / 'codex/shared-skills.json', {})
    shared_names = shared.get('skills', [])
    if set(catalog).intersection(shared_names):
        raise VisualSkillsError('An external skill duplicates an authored shared skill.')
    authored = shared.get('frontend', {})
    if not set(authored).issubset(shared_names) or any(role not in {'general', 'creative', 'verification', 'recipe'} for role in authored.values()):
        raise VisualSkillsError('Invalid authored frontend routing declaration.')
    primary_roles = [role for role in authored.values() if role in {'general', 'creative'}]
    active_primaries = set(primary_roles)
    if len(active_primaries) != len(primary_roles):
        raise VisualSkillsError('Multiple active primary skills in the shared profile.')
    for name, spec in catalog.items():
        role = spec.get('role')
        if role not in {'general', 'creative', 'recipe'}:
            raise VisualSkillsError(f'Unknown frontend role for {name}.')
        if role != 'recipe' and spec['state'] == 'active':
            if role in active_primaries:
                raise VisualSkillsError(f'Multiple active primary skills for {role}.')
            active_primaries.add(role)
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name):
            raise VisualSkillsError('Invalid frontend skill name.')
        if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+\.git', spec['repository']):
            raise VisualSkillsError(f'Invalid public GitHub source for {name}.')
        validate_paths(spec['paths'] + [spec['skill_path']])
        if (spec['state'] not in {'active', 'quarantined'}
                or not re.fullmatch(r'[0-9a-f]{40}', spec['revision'])
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', spec['integrity'])
                or Path(spec['skill_path']).name != name
                or not any(p in {spec['skill_path'], spec['skill_path'] + '/SKILL.md'} for p in spec['paths'])):
            raise VisualSkillsError(f'Invalid frontend pin or state for {name}.')
    return catalog


def frontend_target(base: Path, home: Path, name: str, record: dict, verify: bool, allow_damaged: bool = False) -> Path:
    revision, integrity, leaf = (record[key] for key in ('revision', 'integrity', 'skill_path'))
    validate_paths([leaf])
    if (not re.fullmatch(r'[a-f0-9]{40}', revision) or Path(leaf).name != name
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', integrity)):
        raise VisualSkillsError(f'Malformed frontend receipt for {name}.')
    snapshot = base / name / revision
    reject_symlink_chain(snapshot / leaf, home)
    if not allow_damaged and (verify or snapshot.exists()):
        if digest_tree(snapshot) != integrity[7:] or not (snapshot / leaf / 'SKILL.md').is_file():
            raise VisualSkillsError(f'Frontend snapshot integrity failure for {name}.')
    return snapshot / leaf


def frontend_skills(args: argparse.Namespace, home: Path, base: Path) -> int:
    catalog = frontend_catalog(args.repo_root)
    receipt_path = base / 'receipt.json'
    reject_symlink_chain(receipt_path, home)
    if args.check or args.dry_run:
        return reconcile_frontend(args, home, base, catalog, receipt_path)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / '.apply.lock'
    reject_symlink_chain(lock_path, home)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VisualSkillsError('Another frontend installation holds the lock.') from exc
        return reconcile_frontend(args, home, base, catalog, receipt_path)


def reconcile_frontend(args: argparse.Namespace, home: Path, base: Path, catalog: dict, receipt_path: Path) -> int:
    initial_bytes = receipt_path.read_bytes() if receipt_path.exists() else None
    receipt = receipt_for(receipt_path) or {'owner': OWNER, 'version': 1, 'active': {}}
    if not isinstance(receipt.get('active'), dict):
        raise VisualSkillsError('Malformed frontend ownership receipt.')
    current = receipt['active']
    pending = receipt.get('pending')
    desired = {name: {key: spec[key] for key in ('revision', 'integrity', 'skill_path')}
               for name, spec in catalog.items() if spec['state'] == 'active'}
    if args.rollback:
        desired = receipt.get('previous', current)
    if pending is not None and pending != desired:
        raise VisualSkillsError('Finish the pending operation with its original manifest and mode.')
    recorded = [current, pending or {}]
    for group in [*recorded, desired]:
        if not isinstance(group, dict) or group.keys() - catalog.keys():
            raise VisualSkillsError('Unknown frontend receipt entry. Keep removed packages declared as quarantined.')
    drift = pending is not None or current != desired
    changes = []
    for name in catalog:
        owned = {frontend_target(base, home, name, group[name], True, allow_damaged=args.rollback)
                 for group in recorded if name in group}
        wanted = frontend_target(base, home, name, desired[name], args.rollback) if name in desired else None
        for host in ('.agents', '.claude'):
            target = home / host / 'skills' / name
            reject_symlink_chain(target.parent, home)
            before = owned_link_state(target, owned)
            if before != wanted:
                changes.append((target, owned, before, wanted))
                drift = True
            print(f"{'DIFFERS' if before != wanted else 'IDENTICAL'} {host}/skills/{name}")
        if os.path.lexists(home / '.codex/skills' / name):
            raise VisualSkillsError(f'Duplicate unmanaged Codex skill for {name}.')
    if args.check or args.dry_run or not drift:
        print(f'Managed frontend skill drift: {int(drift)}')
        return int(args.check and drift)
    for name in desired:
        if not args.rollback:
            fetch_snapshot(name, catalog[name], base)
        frontend_target(base, home, name, desired[name], True)
    # Retain the pre-change pins in a private journal before switching either host.
    journal = {**receipt, 'pending': desired}
    reject_symlink_chain(receipt_path, home)
    if (receipt_path.read_bytes() if receipt_path.exists() else None) != initial_bytes:
        raise VisualSkillsError('Frontend ownership receipt changed while staging.')
    atomic_json(receipt_path, journal)
    for target, owned, before, wanted in changes:
        reject_symlink_chain(target.parent, home)
        if owned_link_state(target, owned) != before:
            raise VisualSkillsError('Frontend target changed while staging. Preserving it.')
        if wanted is None:
            target.unlink()  # Only the verified, receipt-owned link.
        elif before is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(wanted)
        else:
            set_link(target, wanted)
    for name in catalog:
        wanted = frontend_target(base, home, name, desired[name], True) if name in desired else None
        for host in ('.agents', '.claude'):
            target = home / host / 'skills' / name
            if owned_link_state(target, {wanted} if wanted else set()) != wanted:
                raise VisualSkillsError(f'Frontend read-back failed for {name}.')
        if os.path.lexists(home / '.codex/skills' / name):
            raise VisualSkillsError(f'Duplicate unmanaged Codex skill for {name}.')
    reject_symlink_chain(receipt_path, home)
    if read_json(receipt_path, None) != journal:
        raise VisualSkillsError('Frontend ownership journal changed during activation.')
    final = {'owner': OWNER, 'version': 1, 'active': desired}
    if not args.rollback:
        final['previous'] = current if current != desired else receipt.get('previous', current)
    atomic_json(receipt_path, final)
    print('Verified frontend snapshots and both host entry points.')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--check', action='store_true')
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--print-digest', type=Path)
    parser.add_argument('--frontend', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if args.print_digest:
        print(digest_tree(args.print_digest))
        return 0
    home = Path(os.environ.get('CODING_AGENT_SYNC_HOME', str(Path.home()))).absolute()
    global _CLAUDE_ENV
    _CLAUDE_ENV = claude_env(home)
    base = home / '.local/share/coding-agent-environment/visual-skills'
    if args.frontend:
        return frontend_skills(args, home, base / 'frontend')
    if args.rollback:
        parser.error('--rollback requires --frontend')
    specs = load_manifest(args.repo_root)
    receipt_path = base / 'receipt.json'
    reject_symlink_chain(receipt_path, home)
    reject_symlink_chain(home / '.claude', home)
    receipt = receipt_for(receipt_path)
    initial_receipt_bytes = receipt_path.read_bytes() if receipt else None
    snapshots = {name: base / name / spec['revision'] for name, spec in specs.items()}
    packages = receipt.get('packages', {}) if receipt else {}
    pending = receipt.get('pending', {}) if receipt else {}
    pending_packages = pending.get('packages', {})
    if pending:
        identity_fields = ('repository', 'tag', 'revision', 'paths', 'integrity')
        if set(pending_packages) != set(specs) or any(
            pending_packages[name].get(field) != specs[name].get(field)
            for name in specs for field in identity_fields
        ):
            raise VisualSkillsError('Complete the pending pinned operation before changing manifest versions.')
    recorded = [*packages.items(), *pending_packages.items()]
    allowed_roots = {name: {path} for name, path in snapshots.items()}
    for name, package in recorded:
        if name not in specs or not isinstance(package, dict):
            raise VisualSkillsError('Malformed owned package receipt.')
        revision = package.get('revision', '')
        expected = base / name / revision
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise VisualSkillsError('Invalid receipt revision.')
        if package.get('snapshot') != str(expected):
            raise VisualSkillsError('Receipt snapshot is outside its owned package path.')
        reject_symlink_chain(expected, home)
        if not expected.is_dir() or digest_tree(expected) != package.get('integrity', '').removeprefix('sha256:'):
            raise VisualSkillsError(f'Previously owned snapshot is corrupt: {expected}')
        allowed_roots[name].add(expected)
    for name, snapshot in snapshots.items():
        reject_symlink_chain(snapshot, home)
        if snapshot.exists() and digest_tree(snapshot) != specs[name]['integrity'].removeprefix('sha256:'):
            raise VisualSkillsError(f'Pinned snapshot is corrupt: {snapshot}')
    links = {
        home / '.claude/skills/archify': ('archify', 'archify'),
        home / '.agents/skills/archify': ('archify', 'archify'),
        home / '.agents/skills/visual-explainer': ('visual-explainer', 'plugins/visual-explainer'),
    }
    desired = {target: snapshots[name] / relative for target, (name, relative) in links.items()}
    allowed = {target: {path / relative for path in allowed_roots[name]} for target, (name, relative) in links.items()}
    copy_target = home / '.agents/skills/visual-explainer'
    prior_copy = pending.get('copy', {})
    prior_stage = prior_copy.get('stage')
    if prior_stage:
        staged = Path(prior_stage)
        if staged.parent != base or not staged.name.startswith('.codex-copy-'):
            raise VisualSkillsError('Pending generated-copy staging path is outside owned storage.')
        reject_symlink_chain(staged, home)
        if staged.exists() and digest_tree(staged) != prior_copy.get('to', {}).get('integrity'):
            raise VisualSkillsError('Pending generated-copy staging changed; preserving foreign state.')
    copy_records = [r for r in [receipt.get('codex_copy') if receipt else None,
                                pending.get('copy', {}).get('to')] if isinstance(r, dict)]
    def active_state(target):
        if target == copy_target and target.is_dir() and not target.is_symlink():
            for record in copy_records:
                source = Path(record.get('source', ''))
                if record.get('target') == str(target) and source in allowed[target] and digest_tree(target) == record.get('integrity') and digest_tree(source) == record.get('integrity'):
                    return source
            raise VisualSkillsError(f'Refusing an unmanaged or modified generated skill directory: {target}')
        return owned_link_state(target, allowed[target])
    before_links = {}
    for target in links:
        reject_symlink_chain(target.parent, home)
        before_links[target] = active_state(target)
        if before_links[target] is not None and not receipt:
            raise VisualSkillsError(f'Refusing an existing skill link without ownership receipt: {target}')
    plugins, markets = native_state()
    def relevant(rows, field, value):
        matches = [row for row in rows if row.get(field) == value]
        if len(matches) > 1:
            raise VisualSkillsError(f'Ambiguous native ownership: {value}')
        return matches[0] if matches else None
    market = relevant(markets, 'name', MARKETPLACE)
    plugin = relevant(plugins, 'id', PLUGIN_ID)
    if not receipt and (market or plugin):
        raise VisualSkillsError('Refusing to adopt an unmanaged Visual Explainer marketplace/plugin.')
    old_market = receipt.get('marketplace') if receipt else None
    authorized_markets = {Path(m['snapshot']) for m in [old_market, pending.get('from'), pending.get('to')] if isinstance(m, dict) and m.get('name') == MARKETPLACE and m.get('pluginId') == PLUGIN_ID and m.get('snapshot')}
    if market and (market.get('source') != 'directory' or marketplace_path(market) not in authorized_markets):
        raise VisualSkillsError('Visual Explainer marketplace differs from owned local registration.')
    if plugin and (plugin.get('scope') != 'user' or not market):
        raise VisualSkillsError('Visual Explainer plugin has ambiguous or missing marketplace ownership.')
    if plugin and old_market and not pending and plugin.get('installPath') != old_market.get('installPath'):
        raise VisualSkillsError('Native plugin install path changed outside the installer.')
    wanted_market = snapshots['visual-explainer']
    plugin_ok = bool(plugin and plugin.get('enabled') is True and verify_plugin_content(plugin, wanted_market))
    market_ok = bool(market and marketplace_path(market) == wanted_market)
    receipt_ok = bool(old_market and old_market.get('snapshot') == str(wanted_market) and not pending)
    copy_ok = bool(copy_target.is_dir() and not copy_target.is_symlink() and receipt and (receipt.get('codex_copy') or {}).get('source') == str(desired[copy_target]))
    drift = (not copy_ok or any(before_links[p] != desired[p] for p in links) or not (plugin_ok and market_ok and receipt_ok) or any(not p.is_dir() for p in snapshots.values()))
    if args.check or args.dry_run:
        for target in links:
            print(f"{'IDENTICAL' if before_links[target] == desired[target] else 'DIFFERS'} {target}")
        print(f"{'IDENTICAL' if plugin_ok and market_ok and receipt_ok else 'DIFFERS'} Claude marketplace {MARKETPLACE}")
        for name, snapshot in snapshots.items():
            print(f"{'IDENTICAL' if snapshot.is_dir() else 'WOULD FETCH' if args.dry_run else 'MISSING'} snapshot {name}@{specs[name]['revision']}")
        print(f'Managed visual skill drift: {int(drift)}')
        return int(drift and args.check)
    if not drift:
        print('Managed visual skill drift: 0')
        return 0
    # Serialize our reconciler; native registries are independently re-read after fetching.
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / '.apply.lock'
    reject_symlink_chain(lock_path, home)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VisualSkillsError('Another visual installation owns the apply lock.') from exc
        for name, spec in specs.items():
            fetch_snapshot(name, spec, base)
        for target in links:
            reject_symlink_chain(target.parent, home)
            if active_state(target) != before_links[target]:
                raise VisualSkillsError('Managed skill link changed during staging; no activation performed.')
        current_bytes = receipt_path.read_bytes() if receipt_path.exists() else None
        if current_bytes != initial_receipt_bytes:
            raise VisualSkillsError('Ownership receipt changed during staging; no activation performed.')
        staged_plugins, staged_markets = native_state()
        if relevant(staged_plugins, 'id', PLUGIN_ID) != plugin or relevant(staged_markets, 'name', MARKETPLACE) != market:
            raise VisualSkillsError('Native plugin state changed during staging; no activation performed.')
        package_records = {name: {**spec, 'snapshot': str(snapshots[name])} for name, spec in specs.items()}
        stage = Path(tempfile.mkdtemp(prefix='.codex-copy-', dir=base))
        stage.rmdir()
        shutil.copytree(desired[copy_target], stage)
        # macOS requires a writable directory for publication by rename.
        for folder, _, _ in os.walk(stage):
            Path(folder).chmod(0o755)
        source_digest = digest_tree(desired[copy_target])
        if digest_tree(stage) != source_digest:
            raise VisualSkillsError('Generated Codex skill staging differs from pinned source.')
        copy_target.parent.mkdir(parents=True, exist_ok=True)
        if stage.stat().st_dev != copy_target.parent.stat().st_dev:
            shutil.rmtree(stage)
            raise VisualSkillsError('Generated copy requires same-filesystem staged renames.')
        retained = base / ('retained-' + stage.name.removeprefix('.'))
        desired_copy = {'target': str(copy_target), 'source': str(desired[copy_target]),
                        'revision': specs['visual-explainer']['revision'], 'integrity': source_digest}
        previous_copy = pending.get('copy', {})
        journal = {'owner': OWNER, 'version': 1, 'packages': packages, 'codex_copy': receipt.get('codex_copy') if receipt else None,
                   'pending': {'from': pending.get('from', old_market),
                               'to': {'name': MARKETPLACE, 'pluginId': PLUGIN_ID, 'snapshot': str(wanted_market)},
                               'copy': {'to': desired_copy, 'stage': str(stage), 'retained': str(retained), 'previous': previous_copy},
                               'packages': package_records, 'links': {str(p): str(v) for p, v in desired.items()}}}
        atomic_json(receipt_path, journal)
        steps = []
        try:
            for target in links:
                if active_state(target) != before_links[target]:
                    raise VisualSkillsError('Managed link changed before activation; preserving it.')
                if target == copy_target:
                    if not copy_ok or before_links[target] != desired[target]:
                        if os.path.lexists(target):
                            os.rename(target, retained)
                            steps.append(f'retained previous Codex skill at {retained}')
                        os.rename(stage, target)
                        steps.append('published generated Codex Visual Explainer skill')
                elif before_links[target] != desired[target]:
                    set_link(target, desired[target])
                    steps.append(f'linked {target}')
            if not (market_ok and plugin_ok):
                if market:
                    run_claude(['plugin', 'marketplace', 'remove', MARKETPLACE, '--scope', 'user'])
                    steps.append('removed previous owned marketplace')
                run_claude(['plugin', 'marketplace', 'add', str(wanted_market), '--scope', 'user'])
                steps.append('registered pinned marketplace')
                run_claude(['plugin', 'install', PLUGIN_ID, '--scope', 'user'])
                steps.append('installed native plugin')
            copy_records.append(desired_copy)
            final_plugins, final_markets = native_state()
            final_market = relevant(final_markets, 'name', MARKETPLACE)
            final_plugin = relevant(final_plugins, 'id', PLUGIN_ID)
            if not final_market or final_market.get('source') != 'directory' or marketplace_path(final_market) != wanted_market:
                raise VisualSkillsError('Native marketplace read-back differs from the pin.')
            if not final_plugin or final_plugin.get('scope') != 'user' or final_plugin.get('enabled') is not True or not verify_plugin_content(final_plugin, wanted_market):
                raise VisualSkillsError('Native plugin read-back is disabled or differs from the pin.')
            if not copy_target.is_dir() or copy_target.is_symlink():
                raise VisualSkillsError('Codex Visual Explainer must remain a physical generated directory.')
            for target, expected in desired.items():
                if active_state(target) != expected:
                    raise VisualSkillsError('Managed skill read-back differs from the pin.')
            for name, snapshot in snapshots.items():
                if digest_tree(snapshot) != specs[name]['integrity'].removeprefix('sha256:'):
                    raise VisualSkillsError('Pinned snapshot changed during native installation.')
            if read_json(receipt_path, None) != journal:
                raise VisualSkillsError('Ownership journal changed; preserving concurrent state.')
            atomic_json(receipt_path, {'owner': OWNER, 'version': 1, 'packages': package_records, 'codex_copy': desired_copy,
                        'marketplace': {'name': MARKETPLACE, 'pluginId': PLUGIN_ID,
                                        'snapshot': str(wanted_market), 'installPath': final_plugin['installPath']}})
            if stage.exists():
                if stage.is_symlink() or digest_tree(stage) != source_digest:
                    raise VisualSkillsError('Generated staging changed; preserving it.')
                shutil.rmtree(stage)
            prior_stage = previous_copy.get('stage')
            if prior_stage and Path(prior_stage).parent == base and Path(prior_stage).name.startswith('.codex-copy-') and Path(prior_stage).is_dir() and not Path(prior_stage).is_symlink():
                if digest_tree(prior_stage) != previous_copy.get('to', {}).get('integrity'):
                    raise VisualSkillsError('Prior staging changed; preserving it.')
                shutil.rmtree(prior_stage)
        except Exception as exc:
            raise VisualSkillsError(f'Activation incomplete: {exc}. Completed: {steps}. '
                                    f'Recovery: preserve {receipt_path} and re-run --visuals; no rollback overwrote shared state.') from exc
    print('Installed pinned visual skills for Claude Code and Codex.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except VisualSkillsError as exc:
        print(f'visual-skills: ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
