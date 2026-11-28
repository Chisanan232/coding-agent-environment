"""Apply the shared-profile installer's selected files under one recoverable backup."""
import ctypes
import datetime
import hashlib
import json
import os
import re
import stat
import sys

ID = re.compile(r'[0-9]{8}T[0-9]{12}Z')
# The sole receipt-less changed leaf authorized for migration from fbb8fdad.
LEGACY_ACCEPTANCE = 'e239775f2b6d2f44f5ee7145eba2540300aa0bf20cf778f53460b25797ba5960'


def publish_new(source, target):
    """Atomic publication without replacement on macOS and Linux."""
    libc = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(target)
    if sys.platform == 'darwin':
        result = libc.renamex_np(src, dst, 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise SystemExit('This host lacks an atomic exclusive rename operation.')
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), target)


def info(path):
    try:
        detail = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(detail.st_mode) or detail.st_uid != os.getuid() or detail.st_nlink != 1:
        raise SystemExit(f'Refusing unowned or non-regular file: {path.name}')
    if detail.st_mode & 0o7000:
        raise SystemExit(f'Refusing special permission bits: {path.name}')
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(detail.st_mode)


def selected(live, host, skills, managed, restore, dry, check, guard, write, *, count_only=False):
    backups = live / ('.' + host) / 'backups/coding-agent-environment'
    guard(backups)
    files = {str(p.relative_to(live)): data for p, data, _ in managed}
    expected = {key: hashlib.sha256(value).hexdigest() for key, value in files.items()}
    current = {}
    for name in skills:
        tree = live / ('.' + host) / 'skills' / name
        guard(tree)
        for path in tree.rglob('*') if tree.is_dir() else ():
            guard(path)
            if not path.is_dir() and str(path.relative_to(live)) not in files:
                raise SystemExit(f'Unexpected file in selected skill tree: {name}/{path.relative_to(tree)}')
    for key in files:
        guard(live / key)
        current[key] = info(live / key)

    history = {}
    receipts = {}
    pending = None
    for folder in sorted(backups.iterdir()) if backups.is_dir() else ():
        if not ID.fullmatch(folder.name):
            continue
        receipt = folder / 'shared-restore.json'
        guard(receipt)
        if not receipt.exists():
            continue
        if folder.stat().st_uid != os.getuid() or stat.S_IMODE(folder.stat().st_mode) != 0o700 or info(receipt)[1] != 0o600:
            raise SystemExit('Unsafe shared-skill backup permissions.')
        record = json.loads(receipt.read_bytes())
        if record.get('schema') not in (1, 2) or record.get('host') != host:
            raise SystemExit('Invalid shared-skill receipt.')
        if record.get('state') in ('pending', 'restoring'):
            if pending:
                raise SystemExit('Multiple pending operations require inspection.')
            pending = folder.name
        receipts[folder.name] = record
        if record.get('state') != 'restored':
            for key, change in record['changes'].items():
                history[key] = (change['installed'], change['mode'] if change['mode'] is not None else 0o644)

    choice = restore or pending
    if pending and pending != choice:
        raise SystemExit('Resolve the pending shared operation first.')
    if choice:
        record = receipts.get(choice, {})
        if record.get('skills') != sorted(skills) or not set(record['changes']).issubset(files):
            raise SystemExit('Use the original selection and repository revision for this backup.')
        if restore and record.get('state') == 'restored':
            print('Selected backup already restored.')
            return 0
        if restore and any(newer > choice and other.get('state') != 'restored'
                           and set(other['changes']).intersection(record['changes'])
                           for newer, other in receipts.items()):
            raise SystemExit('Restore newer overlapping shared backups first.')
        if record.get('state') == 'restoring' and not restore:
            raise SystemExit('Repeat the pending restore using its original backup ID.')
        backup = backups / choice
        changes = record['changes']
        if not restore and any(expected[k] != v['installed'] for k, v in changes.items()):
            raise SystemExit('Pending operation needs its creating revision or explicit restore.')
    else:
        changes = {}
        for key, (prior, mode) in current.items():
            if prior == expected[key]:
                if key in history and history[key][0] == prior and mode != history[key][1]:
                    raise SystemExit(f'Refusing a modified permission mode: {key}')
                if key in history:
                    continue
                # Adopt exact canonical bytes already present without changing them.
                changes[key] = {'previous': prior, 'mode': mode, 'installed': expected[key]}
                continue
            if prior is not None and history.get(key) != (prior, mode):
                legacy = key == f'.{host}/skills/acceptance-validation/SKILL.md' and prior == LEGACY_ACCEPTANCE
                if not legacy:
                    raise SystemExit(f'Refusing to overwrite unowned or modified shared file: {key}')
            changes[key] = {'previous': prior, 'mode': mode, 'installed': expected[key]}

    if not restore:
        for key in files.keys() - changes.keys():
            if current[key][0] != expected[key] or (key in history and current[key] != history[key]):
                raise SystemExit(f'Shared file changed outside the pending write set: {key}')
    plan = []
    for key, change in changes.items():
        old, new = (change['previous'], change['mode']), (change['installed'], change['mode'] if change['mode'] is not None else 0o644)
        if choice:
            saved = backup / key
            guard(saved)
            if info(saved)[0] != old[0]:
                raise SystemExit('Saved bytes differ from the backup receipt.')
        source, target = (new, old) if restore else (old, new)
        if current[key] == target:
            continue
        if current[key] != source:
            raise SystemExit(f'Refusing a modified file or mode: {key}')
        plan.append((key, source, target))

    label = 'Managed Codex drift' if host == 'codex' else 'Managed Claude practice drift'
    if dry or check:
        for key, _, _ in plan:
            print(f'DIFFERS {key}')
        drift = len(plan) + int(bool(pending))
        if not choice:
            drift += sum(change['previous'] == change['installed'] for change in changes.values())
        if count_only:
            return drift
        print(f'{label}: {drift}')
        return int(bool(check and drift))
    if not choice and not changes:
        print(f'{label}: 0')
        return 0

    if not choice:
        backup = backups / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        guard(backup)
        backup.mkdir(mode=0o700, parents=True)
        record = {'schema': 2, 'host': host, 'skills': sorted(skills), 'state': 'pending', 'changes': changes}
        for key, change in changes.items():
            if change['previous'] is not None:
                data = (live / key).read_bytes()
                if hashlib.sha256(data).hexdigest() != change['previous']:
                    raise SystemExit('Shared file changed before backup.')
                write(backup / key, data, 0o600)
        write(backup / 'shared-restore.json', json.dumps(record, sort_keys=True).encode(), 0o600)

    if restore:
        record.update(schema=2, state='restoring')
        write(backup / 'shared-restore.json', json.dumps(record, sort_keys=True).encode(), 0o600)
    print(f'Prior state: {backup}', flush=True)
    for key, source, target in plan:
        path = live / key
        guard(path)
        if info(path) != source:
            raise SystemExit(f'Shared file changed during update: {key}')
        if target[0] is None:
            path.unlink()
        else:
            data = (backup / key).read_bytes() if restore else files[key]
            if hashlib.sha256(data).hexdigest() != target[0]:
                raise SystemExit('Source changed during update.')
            write(path, data, target[1], stage=backup, publish=publish_new if source[0] is None else None)
        if info(path) != target:
            raise SystemExit('Shared update did not verify.')
    if not restore:
        for key, digest in expected.items():
            guard(live / key)
            mode = changes[key]['mode'] if key in changes else current[key][1]
            if info(live / key) != (digest, mode if mode is not None else 0o644):
                raise SystemExit(f'Shared installation verification failed: {key}')
    record.update(schema=2, state='restored' if restore else 'applied')
    write(backup / 'shared-restore.json', json.dumps(record, sort_keys=True).encode(), 0o600)
    print(f'{label}: 0')
    return 0
