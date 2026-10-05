#!/usr/bin/env python3
"""Reconcile positively owned Codex routing without serializing user config."""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
ROLES = ('architect', 'implementer', 'reviewer')
MARKER = '# coding-agent-environment:managed-routing-role:v1\n'
RECEIPT = 'routing-owned-state.json'
ACTIVE_JOURNAL = None


def fail(message):
    raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            fail('Refusing symlinked routing state.')
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        fail('Routing state must be a regular, singly linked file.')
    return path.read_bytes()


def atomic(path, expected, desired):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.routing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, stat.S_IMODE(path.stat().st_mode) if expected is not None else 0o600)
        # This detects prior concurrent changes; noncooperating writers can still
        # race this check and rename. Never restore a stale complete file.
        if read(path) != expected:
            fail('Concurrent routing-state change; no stale restoration attempted.')
        os.replace(name, path)
        if read(path) != desired:
            fail('Routing readback failed; inspect the prepared journal.')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def get(data, path):
    for key in path.split('.'):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def patch(raw, updates):
    """Edit regular scalar assignments only; fail closed on other owned layouts."""
    text = (raw or b'').decode()
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    sections, spans = {}, {}
    section = ''
    multiline = None
    for i, line in enumerate(lines):
        # Ignore apparent assignments/headers inside TOML multiline strings.
        if multiline:
            if line.count(multiline) % 2:
                multiline = None
            continue
        match = re.fullmatch(r'\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?\r?\n?', line)
        if match:
            section = match[1]
            sections[section] = i
        elif line.lstrip().startswith('['):
            # Quoted/array table headers are boundaries, but not editable owned
            # layouts. Never associate their assignments with a prior table.
            section = '<unsupported-header>'
        else:
            match = re.match(r'^\s*([A-Za-z0-9_-]+)\s*=', line)
            if match:
                key = '.'.join(filter(None, (section, match[1])))
                spans[key] = i
        for quote in ('"""', "'''"):
            if line.count(quote) % 2:
                multiline = quote
                break
    replacements, additions = {}, {}
    for path, value in updates.items():
        table, key = path.rsplit('.', 1)
        current = get(before, path)
        if current is not None and path not in spans:
            fail('Ambiguous owned TOML layout: ' + path)
        if path in spans:
            if type(current) is type(value) and current == value:
                continue
            line = lines[spans[path]]
            try:
                parsed = tomllib.loads(line)
            except tomllib.TOMLDecodeError:
                fail('Owned assignment must be a single-line scalar: ' + path)
            if parsed.get(key) != current:
                fail('Ambiguous owned assignment: ' + path)
            replacements[spans[path]] = '' if value is None else key + ' = ' + json.dumps(value) + '\n'
        elif value is not None:
            # Existing semantic table without a regular lexical header is unsafe.
            if get(before, table) is not None and table not in sections:
                fail('Ambiguous owned table: ' + table)
            additions.setdefault(table, []).append(key + ' = ' + json.dumps(value) + '\n')
    for table, values in additions.items():
        if table in sections:
            pos = sections[table]
            replacements[pos] = lines[pos] + ''.join(values)
        else:
            lines.append('\n[' + table + ']\n' + ''.join(values))
    result = ''.join(replacements.get(i, line) for i, line in enumerate(lines)).encode()
    after = tomllib.loads(result.decode())
    expected = copy.deepcopy(before)
    for path, value in updates.items():
        keys = path.split('.')
        node = expected
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    # Empty table headers are retained on removal; compare with those accounted for.
    def prune(node):
        if isinstance(node, dict):
            return {k: prune(v) for k, v in node.items() if v != {}}
        return node
    if prune(after) != prune(expected):
        fail('Patch would change unowned configuration.')
    return result


def remove_empty_headers(raw, tables):
    """Remove only actual, created empty headers, never matching string content."""
    lines = raw.decode().splitlines(keepends=True)
    multiline = None
    remove = set()
    for i, line in enumerate(lines):
        if multiline:
            if line.count(multiline) % 2:
                multiline = None
            continue
        if line.strip() in {'[' + table + ']' for table in tables}:
            remove.add(i)
            if i and lines[i - 1] == '\n':
                remove.add(i - 1)
        for quote in ('"""', "'''"):
            if line.count(quote) % 2:
                multiline = quote
                break
    result = ''.join(line for i, line in enumerate(lines) if i not in remove).encode()
    def normalized(value):
        if isinstance(value, dict):
            result = {key: normalized(item) for key, item in value.items()}
            return {key: item for key, item in result.items() if item != {}}
        return value
    if normalized(tomllib.loads(raw.decode())) != normalized(tomllib.loads(result.decode())):
        fail('Header removal would change unowned configuration.')
    return result


def provider(data):
    selected = data.get('model_provider', 'openai')
    context = {'provider': selected, 'definition': data.get('model_providers', {}).get(selected),
               'openai_base_url': data.get('openai_base_url'), 'chatgpt_base_url': data.get('chatgpt_base_url'),
               'model_catalog_json': data.get('model_catalog_json'),
               'environment_base_url': os.environ.get('OPENAI_BASE_URL'),
               'environment_chatgpt_base_url': os.environ.get('CODEX_CHATGPT_BASE_URL')}
    encoded = json.dumps(context, sort_keys=True).encode()
    official = selected == 'openai' and all(context[k] is None for k in context if k != 'provider')
    return digest(encoded), official


def command(args, env=None):
    result = subprocess.run([os.environ.get('CODING_AGENT_CODEX_BIN', 'codex'), *args],
                            capture_output=True, timeout=60, env=env)
    if result.returncode:
        fail('Native Codex verification unavailable; raw output suppressed.')
    return result.stdout


def validate(data, bodies, resolution, native_args=(), target_home=None):
    fingerprint, official = provider(data)
    mapping = {n: tomllib.loads(bodies[n].decode())['model'] for n in ROLES}
    if resolution:
        record = json.loads(read(Path(resolution)).decode())
        if record.get('provider_fingerprint') != fingerprint or not record.get('verification_evidence'):
            fail('Local model resolution needs matching provider fingerprint and verification evidence.')
        mapping = record['models']
        if set(mapping) != set(ROLES):
            fail('Model resolution must cover all three named roles.')
    elif not official:
        fail('Provider needs explicit machine-local verified model resolution. Fingerprint: ' + fingerprint)
    env = dict(os.environ, CODEX_HOME=str(target_home)) if target_home else None
    catalog = json.loads(command([*native_args, 'debug', 'models'], env=env))
    models = {m['slug']: m for m in catalog['models']}
    for role in ROLES:
        config = tomllib.loads(bodies[role].decode())
        model = mapping[role]
        efforts = {e['effort'] for e in models.get(model, {}).get('supported_reasoning_levels', [])}
        if config['model_reasoning_effort'] not in efforts:
            fail('Actual model catalog does not support role model/reasoning: ' + role)
        replacement = ('model = ' + json.dumps(model)).encode()
        bodies[role] = re.sub(rb'^model = .*$', lambda _: replacement, bodies[role], flags=re.M)
    # Ask this installed binary to validate supported keys, without user auth/config.
    with tempfile.TemporaryDirectory(prefix='routing-schema-') as tmp:
        home = Path(tmp)
        (home / 'agents').mkdir()
        probe = '[agents]\nenabled = true\nmax_concurrent_threads_per_session = 8\n'
        for role in ROLES:
            (home / 'agents' / (role + '.toml')).write_bytes(bodies[role])
            probe += '\n[agents.' + role + ']\nconfig_file = "agents/' + role + '.toml"\n'
        (home / 'config.toml').write_text(probe)
        env = dict(os.environ, CODEX_HOME=tmp)
        result = subprocess.run([os.environ.get('CODING_AGENT_CODEX_BIN', 'codex'), 'app-server',
                                 '--strict-config', '--listen', 'stdio://'], input=b'', capture_output=True,
                                env=env, timeout=30)
        if result.returncode or result.stderr.strip():
            fail('Installed Codex rejects named roles or supported concurrency keys.')
    return fingerprint


def launch_context(data, args, home):
    """Resolve simple native profile overlays; reject unresolvable routing overrides."""
    effective = copy.deepcopy(data)
    profiles = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('--profile', '-p'):
            i += 1
            profiles.append(args[i])
        elif arg.startswith('--profile='):
            profiles.append(arg.split('=', 1)[1])
        elif arg.startswith('-p') and arg != '-p':
            profiles.append(arg[2:])
        elif arg in ('-c', '--config') or arg.startswith(('--config=', '-c')):
            if arg in ('-c', '--config'):
                i += 1
                override = args[i]
            elif arg.startswith('--config='):
                override = arg.split('=', 1)[1]
            else:
                override = arg[2:].lstrip('=')
            try:
                override_data = tomllib.loads(override)
            except tomllib.TOMLDecodeError:
                # Native CLI accepts a raw string value when TOML parsing fails.
                key = override.split('=', 1)[0].strip().strip('"\'')
                override_data = {key.split('.')[0]: {key.split('.')[-1]: True}}
            if any(key.startswith(('agents', 'model_provider', 'model_catalog', 'openai_base_url', 'chatgpt_base_url', 'profile')) for key in override_data) or any(key.startswith('multi_agent') for key in override_data.get('features', {})):
                fail('Routing-relevant launch override requires separate verification.')
        elif arg in ('--ignore-user-config', '--enable', '--disable', '-C', '--cd', '--oss', '--local-provider', '--remote') or arg.startswith(('--enable=', '--disable=', '--cd=', '-C', '--local-provider=', '--remote=')):
            fail('Launch configuration bypass/feature overrides require separate routing verification.')
        i += 1
    if not profiles and data.get('profile'):
        profiles.append(data['profile'])
    for name in profiles:
        overlay_path = home / (name + '.config.toml')
        # Profile symlinks are the repository's existing native install mechanism.
        overlay = tomllib.loads(overlay_path.read_text()) if overlay_path.exists() else data.get('profiles', {}).get(name)
        if overlay is None:
            fail('Cannot resolve selected native profile.')
        if any(key.startswith('multi_agent') for key in overlay.get('features', {})):
            fail('Profile routing feature override requires separate verification.')
        if set(overlay.get('agents', {})) & {'enabled', 'max_concurrent_threads_per_session', *ROLES}:
            fail('Profile-owned named routing conflicts with managed routing.')
        def merge(target, source):
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value
        merge(effective, overlay)
    for directory in (Path.cwd(), *Path.cwd().parents):
        project = directory / '.codex/config.toml'
        if project.resolve() in {home.resolve() / 'config.toml', Path.home().resolve() / '.codex/config.toml'}:
            continue
        if project.exists():
            local = tomllib.loads(project.read_text())
            if any(key.startswith(('agents', 'model_provider', 'model_catalog', 'openai_base_url', 'chatgpt_base_url', 'profile')) for key in local) or any(key.startswith('multi_agent') for key in local.get('features', {})):
                fail('Project routing override requires separate verification.')
    return effective


def catalog_args(args):
    result = []
    for i, value in enumerate(args):
        if value in ('--profile', '-p'):
            result.extend([value, args[i + 1]])
        elif value.startswith('--profile='):
            result.append(value)
        elif value.startswith('-p') and value != '-p':
            result.append(value)
    return result


def main():
    global ACTIVE_JOURNAL
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for flag in ('dry-run', 'apply', 'check', 'remove', 'launch-check'):
        modes.add_argument('--' + flag, action='store_true')
    parser.add_argument('--adopt-existing', action='store_true')
    parser.add_argument('--resolution')
    parser.add_argument('native_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.adopt_existing and (args.check or args.remove or args.launch_check):
        fail('Adoption is only allowed during report/apply.')
    home = Path(os.environ.get('CODEX_HOME', str(Path(os.environ.get('CODING_AGENT_SYNC_HOME', str(Path.home()))) / '.codex')))
    config_path, receipt_path = home / 'config.toml', home / RECEIPT
    raw, receipt_raw = read(config_path), read(receipt_path)
    data = tomllib.loads((raw or b'').decode())
    if not args.remove and data.get('features', {}).get('multi_agent_v2') is False:
        fail('Explicit routing backend override requires separate verification.')
    receipt = json.loads(receipt_raw) if receipt_raw else None
    if receipt and receipt.get('owner') != 'coding-agent-environment:routing:v1':
        fail('Unrecognized routing receipt owner.')
    bodies = {n: (ROOT / 'codex/agents' / (n + '.toml')).read_bytes() for n in ROLES}
    for body in bodies.values():
        if not body.startswith(MARKER.encode()):
            fail('Canonical role ownership marker missing.')
    if args.launch_check:
        if not receipt or receipt['phase'] != 'installed':
            fail('Routing installation is incomplete.')
        native = args.native_args[1:] if args.native_args[:1] == ['--'] else args.native_args
        effective = launch_context(data, native, home)
        if provider(effective)[0] != receipt['provider_fingerprint']:
            fail('Provider changed; explicitly resolve and reinstall routing before launch.')
        data = effective
    if args.remove:
        if not receipt:
            print('Routing owned state absent; nothing to remove.')
            return
    else:
        effective = data if args.launch_check else launch_context(data, [], home)
        fingerprint = validate(effective, bodies, args.resolution or (receipt or {}).get('resolution'),
                               catalog_args(native) if args.launch_check else (), target_home=home)
    desired = {'agents.enabled': True, 'agents.max_concurrent_threads_per_session': 8}
    for n in ROLES:
        desired['agents.' + n + '.description'] = tomllib.loads(bodies[n].decode())['description']
        desired['agents.' + n + '.config_file'] = 'agents/' + n + '.toml'
    previous = receipt['previous'] if receipt else {p: get(data, p) for p in desired}
    tables = {p.rsplit('.', 1)[0] for p in desired}
    previous_tables = receipt.get('previous_tables', {t: True for t in tables}) if receipt else {t: get(data, t) is not None for t in tables}
    role_previous = receipt['role_previous'] if receipt else {}
    for path, value in desired.items():
        current = get(data, path)
        allowed = [receipt['desired'][path], previous[path]] if receipt and receipt['phase'] != 'installed' else [receipt['desired'][path]] if receipt else [None]
        if receipt and receipt['phase'] != 'installed':
            allowed.append(receipt.get('operation_fields', {}).get(path))
        if not receipt and args.adopt_existing:
            allowed.append(value)
        if not any(type(current) is type(candidate) and current == candidate for candidate in allowed):
            fail('Unowned or externally modified routing field: ' + path)
    role_raw = {}
    for n in ROLES:
        path = home / 'agents' / (n + '.toml')
        role_raw[n] = read(path)
        if receipt:
            allowed = [bytes.fromhex(receipt['roles'][n])]
            if receipt['phase'] != 'installed':
                allowed.append(bytes.fromhex(role_previous[n]) if role_previous[n] else None)
                before_role = receipt.get('operation_roles', {}).get(n)
                allowed.append(bytes.fromhex(before_role) if before_role else None)
        else:
            allowed = [None]
            if args.adopt_existing:
                allowed.append(bodies[n][len(MARKER):])
            role_previous[n] = role_raw[n].hex() if role_raw[n] else None
        if role_raw[n] not in allowed:
            fail('Unowned or externally modified role: ' + n)
    updates = previous if args.remove else desired
    patched = patch(raw, updates)
    if args.remove:
        parsed = tomllib.loads(patched.decode())
        for table in sorted(tables, key=lambda t: t.count('.'), reverse=True):
            parsed = tomllib.loads(patched.decode())
            if not previous_tables[table] and get(parsed, table) == {}:
                patched = remove_empty_headers(patched, {table})
        tomllib.loads(patched.decode())
    targets = {n: (bytes.fromhex(role_previous[n]) if role_previous[n] else None) if args.remove else bodies[n] for n in ROLES}
    drift = patched != raw or any(role_raw[n] != targets[n] for n in ROLES)
    if args.check or args.launch_check:
        if not receipt or receipt['phase'] != 'installed' or drift or receipt['provider_fingerprint'] != fingerprint:
            fail('Managed routing drift or provider binding changed.')
        print('Routing drift = 0; native schema/catalog/provider verified.')
        return
    if args.dry_run:
        print('Routing report: ' + ('owned patch required' if drift or not receipt else 'drift = 0'))
        print('Provider fingerprint: ' + fingerprint)
        print('Preserved: provider/root/default-delegate/auth/plugins/MCP/trust/Claude; owned roles: ' + ', '.join(ROLES))
        return
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / '.routing-install.lock'
    read(lock_path)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read(config_path) != raw or read(receipt_path) != receipt_raw:
            fail('Concurrent state changed before prepared journal.')
        journal = receipt or {'owner': 'coding-agent-environment:routing:v1', 'previous': previous, 'previous_tables': previous_tables, 'role_previous': role_previous}
        journal['operation_roles'] = {n: role_raw[n].hex() if role_raw[n] else None for n in ROLES}
        journal['operation_fields'] = {p: get(data, p) for p in desired}
        journal.update(phase='removing' if args.remove else 'prepared', desired=desired)
        if not args.remove:
            journal['roles'] = {n: targets[n].hex() for n in ROLES}
        if not args.remove:
            journal.update(provider_fingerprint=fingerprint, resolution=str(Path(args.resolution).resolve()) if args.resolution else (receipt or {}).get('resolution'))
        prepared = (json.dumps(journal, sort_keys=True, indent=2) + '\n').encode()
        atomic(receipt_path, receipt_raw, prepared)
        ACTIVE_JOURNAL = receipt_path
        if args.remove:
            if raw != patched:
                atomic(config_path, raw, patched)
        for n in ROLES:
            path = home / 'agents' / (n + '.toml')
            if targets[n] is None:
                if read(path) != role_raw[n]:
                    fail('Concurrent role change before owned removal.')
                if path.exists():
                    path.unlink()
            else:
                if role_raw[n] != targets[n]:
                    atomic(path, role_raw[n], targets[n])
        if args.remove:
            if read(receipt_path) != prepared:
                fail('Concurrent receipt change before removal.')
            receipt_path.unlink()
        else:
            if raw != patched:
                atomic(config_path, raw, patched)
            journal['phase'] = 'installed'
            journal.pop('operation_roles', None)
            journal.pop('operation_fields', None)
            atomic(receipt_path, prepared, (json.dumps(journal, sort_keys=True, indent=2) + '\n').encode())
        print('Routing owned state ' + ('removed' if args.remove else 'installed; preserved all unowned configuration.'))
        ACTIVE_JOURNAL = None


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as error:
        print('Routing refused: ' + str(error), file=sys.stderr)
        if ACTIVE_JOURNAL is not None:
            print('Partial application: prepared routing journal remains. Inspect owned state, then rerun --routing or --routing --remove; no full-file restoration was attempted.', file=sys.stderr)
        sys.exit(2)
