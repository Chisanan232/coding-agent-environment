#!/usr/bin/env python3
"""Install/check the statusline without replacing unrelated Claude settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
FILES = ("statusline-compositor.py", "statusline.py")


def regular(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Refusing non-regular target: {path}")


def replace(path, content, mode, backups):
    regular(path)
    if path.exists() and path.read_bytes() == content:
        return
    if path.exists():
        backups.mkdir(parents=True, mode=0o700, exist_ok=True)
        backup = backups / (path.name + "." + str(time.time_ns()))
        with backup.open("xb") as stream:
            os.chmod(backup, 0o600)
            stream.write(path.read_bytes())
        if backup.read_bytes() != path.read_bytes():
            raise ValueError("Backup verification failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".statusline-install-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--adopt", action="store_true", help="Back up and adopt differing existing scripts/command")
    args = parser.parse_args()
    home = args.home.resolve()
    directory = home / ".claude"
    if directory.is_symlink():
        raise ValueError("Refusing symlinked Claude directory")
    settings = directory / "settings.json"
    regular(settings)
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object")
    probe = subprocess.run([args.python, "-c", "import json,select,signal,dataclasses,pathlib;import sys;print(sys.executable)"],
                           capture_output=True, text=True, check=True, timeout=5)
    interpreter = Path(probe.stdout.strip()).resolve()
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise ValueError("Interpreter must resolve to an absolute binary")
    # Pin the actual interpreter, not a version-manager shim.
    subprocess.run([str(interpreter), "-c", "import sys;assert sys.version_info >= (3,9)"], check=True, timeout=5)
    command = shlex.join([str(interpreter), str(directory / FILES[0])])
    status = data.get("statusLine", {})
    if not isinstance(status, dict):
        raise ValueError("statusLine must be an object")
    changed_command = status.get("command") != command
    changes = []
    for name in FILES:
        target = directory / name
        regular(target)
        content = (ROOT / ".claude" / name).read_bytes()
        if not target.exists() or target.read_bytes() != content:
            changes.append((target, content))
            if target.exists() and not args.adopt and not args.check:
                raise ValueError(f"Existing script differs; review then use --adopt: {name}")
    if args.check:
        print(json.dumps({"synchronized": not changes and not changed_command,
                          "scripts_differ": [p.name for p, _ in changes], "command_differs": changed_command}))
        return int(bool(changes or changed_command))
    if changed_command and status.get("command") and not args.adopt:
        raise ValueError("Existing statusline command differs; review then use --adopt")
    backups = directory / "backups" / "coding-agent-environment"
    for target, content in changes:
        replace(target, content, 0o755, backups)
    if changed_command:
        data["statusLine"] = dict(status, type="command", command=command)
        replace(settings, (json.dumps(data, indent=2) + "\n").encode(), settings.stat().st_mode & 0o777 if settings.exists() else 0o600, backups)
    print(json.dumps({"synchronized": True, "sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES}}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
