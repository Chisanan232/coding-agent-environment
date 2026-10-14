#!/usr/bin/env python3
"""Synchronize only reviewed launcher files in the managed local runtime."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('ca-claude', 'ca-codex', 'coding-agent-resource', 'coding-agent-profile', 'coding-agent-profile-explain')
ASSETS = tuple(('bin/' + name, 0o755) for name in NAMES) + (('scripts/codex-routing.py', 0o755),) + tuple(('codex/agents/' + role + '.toml', 0o600) for role in ('architect', 'implementer', 'reviewer'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--adopt', action='store_true')
    args = parser.parse_args()
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('statusline_install', ROOT / 'scripts/statusline-install.py')
    install = module_from_spec(spec)
    spec.loader.exec_module(install)
    home = args.home.resolve()
    directory = home / '.local/lib/coding-agent-environment'
    for component in (directory, *directory.parents):
        if component.is_symlink():
            raise ValueError('Refusing redirected runtime directory')
    changes = []
    for relative, mode in ASSETS:
        target = directory / relative
        for component in target.parents:
            if component.is_symlink():
                raise ValueError('Refusing redirected runtime asset')
        install.regular(target)
        content = (ROOT / relative).read_bytes()
        if not target.exists() or target.read_bytes() != content:
            changes.append((target, content, mode, target.read_bytes() if target.exists() else None))
            if target.exists() and not args.adopt and not args.check:
                raise ValueError('Existing runtime differs; review then --adopt')
    if args.check:
        print('launcher drift:', len(changes))
        return int(bool(changes))
    for target, content, mode, expected in changes:
        install.replace(target, content, mode, home / '.local/state/coding-agent-environment/backups', expected=expected)
    print('launcher drift: 0')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
