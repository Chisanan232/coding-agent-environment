#!/usr/bin/env python3
"""Resolve the pinned installed storage core; read-only, no host config reads."""
import hashlib
import json
import os
from pathlib import Path
import stat


def resolve(home, pin):
    try:
        base = Path(home) / ".local/share/coding-agent-environment/storage"
        receipt_path = base / "storage-core.json"
        def regular(path, private=True):
            for part in (path, *path.parents):
                if part.is_symlink():
                    raise ValueError("symlinked storage state")
                if part.is_dir():
                    ancestor = part.stat()
                    if ancestor.st_mode & 0o022 and not ancestor.st_mode & stat.S_ISVTX:
                        raise ValueError("writable storage ancestry")
            info = path.stat()
            forbidden = 0o077 if private else 0o022
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & forbidden:
                raise ValueError("unowned storage state")
        regular(receipt_path)
        receipt = json.loads(receipt_path.read_text())
        if isinstance(receipt, dict) and set(receipt) == {"schema", "current", "previous"} and receipt["schema"] == 1:
            receipt = receipt["current"]
        if not isinstance(receipt, dict) or set(receipt) != {"source_revision", "binary_sha256", "path"}:
            raise ValueError("invalid receipt")
        if receipt["source_revision"] != pin["revision"]:
            return "", "outdated"
        checksum = receipt["binary_sha256"]
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("invalid digest")
        binary = base / "glomeris" / checksum / "bin/glomeris"
        if receipt["path"] != str(binary):
            raise ValueError("unowned path")
        if base.parent.stat().st_uid != os.getuid():
            raise ValueError("unowned shared container")
        for directory in (base, base / "glomeris", binary.parents[1], binary.parent):
            info = directory.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("non-private managed directory")
        regular(binary, private=False)
        ownership = binary.parents[1] / "artifact.json"
        regular(ownership)
        if json.loads(ownership.read_text()) != receipt or not os.access(binary, os.X_OK):
            raise ValueError("unowned executable")
        digest = hashlib.sha256()
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError("digest drift")
        return str(binary), "verified_installation"
    except FileNotFoundError:
        return "", "missing"
    except (OSError, ValueError, KeyError, TypeError):
        return "", "refused"
