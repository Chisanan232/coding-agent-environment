#!/usr/bin/env python3
"""Install and inspect the repository-pinned Glomeris storage runtime."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "codex/storage-core.json"
RUNTIME = Path(".local/share/coding-agent-environment")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def manifest():
    value = json.loads(MANIFEST.read_text())
    if (value.get("schema") != 1 or value.get("repository") != "https://github.com/Chisanan232/glomeris"
            or not REVISION_RE.fullmatch(value.get("revision", ""))
            or value.get("version") != "0.2.0" or value.get("command_contract") != 1):
        raise ValueError("Invalid storage-core manifest")
    return value


def home_path():
    value = os.environ.get("CODING_AGENT_SYNC_HOME")
    path = Path(value).expanduser() if value else Path.home()
    if not path.is_absolute():
        raise ValueError("Selected home must be absolute")
    return path


def reject_symlink_ancestry(path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symlinked path: {current}")


def ensure_private_dir(path):
    reject_symlink_ancestry(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"Managed directory is not private: {path}")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_private_json(path):
    reject_symlink_ancestry(path)
    if path.is_symlink():
        raise ValueError("Refusing symlinked receipt")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Receipt must be a private user-owned regular file")
    value = json.loads(path.read_text())
    return value


def validate_receipt(value):
    if not isinstance(value, dict) or set(value) != {"source_revision", "binary_sha256", "path"}:
        raise ValueError("Unrecognized storage receipt")
    if not all(isinstance(value[key], str) for key in value) or not REVISION_RE.fullmatch(value["source_revision"]) or not re.fullmatch(r"[0-9a-f]{64}", value["binary_sha256"]):
        raise ValueError("Invalid storage receipt provenance")
    if not Path(value["path"]).is_absolute():
        raise ValueError("Storage receipt path must be absolute")
    return value


def read_receipt(path):
    value = read_private_json(path)
    return None if value is None else validate_receipt(value)


def read_selection(path):
    value = read_private_json(path)
    if value is None:
        return None, None
    if isinstance(value, dict) and set(value) == {"schema", "current", "previous"} and value["schema"] == 1:
        current = validate_receipt(value["current"])
        previous = None if value["previous"] is None else validate_receipt(value["previous"])
        return current, previous
    # Legacy owned deployments remain readable; only an explicit successful
    # upgrade publishes the transactional envelope. Preserve the old sidecar.
    current = validate_receipt(value)
    previous = read_receipt(path.parent / "storage-core.previous.json")
    return current, previous


def private_directory(path):
    reject_symlink_ancestry(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Managed directory is not private")


def artifact_state(receipt, base):
    if receipt is None:
        return "absent"
    binary = Path(receipt["path"])
    expected = base / "glomeris" / receipt["binary_sha256"] / "bin/glomeris"
    if binary != expected:
        raise ValueError("Storage receipt path is outside its owned versioned destination")
    for directory in (base, base / "glomeris", binary.parents[1], binary.parent):
        private_directory(directory)
    reject_symlink_ancestry(binary)
    if not binary.is_file() or binary.is_symlink():
        return "drift"
    info = binary.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("Untrusted storage executable permissions")
    if digest(binary) != receipt["binary_sha256"] or not os.access(binary, os.X_OK):
        return "drift"
    ownership = read_receipt(binary.parents[1] / "artifact.json")
    if ownership != receipt:
        return "drift"
    return "installed"


def receipt_state(receipt_path):
    current, _ = read_selection(receipt_path)
    return artifact_state(current, receipt_path.parent)


def atomic_json(path, value):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        reject_symlink_ancestry(path)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def acquire_lock(path):
    reject_symlink_ancestry(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Invalid integration lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise BlockingIOError("Another storage integration operation is active")
    except (OSError, ValueError):
        os.close(fd)
        raise
    return fd  # Keep this open through the entire mutation.


def run_checked(command, runner=subprocess.run, cwd=None):
    result = runner(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"{Path(command[0]).name} failed (exit {result.returncode}); previous installation preserved")
    return result.stdout.strip()


def validate_checkout(checkout, revision, runner):
    checkout = Path(checkout).expanduser().resolve(strict=True)
    if not checkout.is_dir():
        raise ValueError("Checkout must be a directory")
    origin = run_checked(["git", "remote", "get-url", "origin"], runner, checkout)
    allowed = {"https://github.com/Chisanan232/glomeris", "https://github.com/Chisanan232/glomeris.git",
               "git@github.com:Chisanan232/glomeris.git"}
    if origin not in allowed:
        raise ValueError("Checkout origin does not match the pinned repository")
    head = run_checked(["git", "rev-parse", "HEAD"], runner, checkout)
    if head != revision:
        raise ValueError("Checkout HEAD does not match the pinned revision")
    dirty = run_checked(["git", "status", "--porcelain", "--untracked-files=all"], runner, checkout)
    if dirty:
        raise ValueError("Checkout has modified or untracked files")
    return checkout


def expected_version(checkout, version, runner):
    description = run_checked(["git", "describe", "--tags", "--always"], runner, checkout)
    if description == "v" + version:
        return "glomeris " + version
    if not description or any(ord(c) < 32 for c in description):
        raise ValueError("Invalid source build identity")
    return f"glomeris {version} (dev build, rev {description})"


def verify_binary(binary, expected, runner):
    if run_checked([str(binary), "--version"], runner) != expected:
        raise ValueError("Built Glomeris version does not match the pinned source")
    try:
        report = json.loads(run_checked([str(binary), "status", "--json"], runner))
    except json.JSONDecodeError as exc:
        raise ValueError("Glomeris status --json did not return JSON") from exc
    if (not isinstance(report, dict)
            or report.get("pressure_state") not in {"HEALTHY", "WARN", "PRESSURED", "CRITICAL", "EMERGENCY"}
            or not all(type(report.get(k)) is int and report[k] >= 0 for k in ("total_bytes", "free_bytes"))
            or report["total_bytes"] <= 0 or report["free_bytes"] > report["total_bytes"]
            or type(report.get("used_percent")) not in (int, float)
            or not 0 <= report["used_percent"] <= 100):
        raise ValueError("Glomeris status does not satisfy command contract 1")


def build(checkout, revision, version, runner=subprocess.run):
    # Build committed bytes in an isolated clone: a live checkout can change
    # while Cargo runs. Never compile from those mutable working files.
    with tempfile.TemporaryDirectory(prefix="glomeris-build-") as scratch:
        source = Path(scratch) / "source"
        target_dir = Path(scratch) / "target"
        run_checked(["git", "clone", "--no-hardlinks", "--no-checkout", "--local", str(checkout), str(source)], runner)
        run_checked(["git", "checkout", "--detach", revision], runner, source)
        if run_checked(["git", "rev-parse", "HEAD"], runner, source) != revision:
            raise ValueError("Isolated source revision changed")
        expected = expected_version(source, version, runner)
        run_checked(["cargo", "build", "--locked", "--release", "--bin", "glomeris", "--target-dir", str(target_dir)], runner, source)
        binary = target_dir / "release/glomeris"
        if not binary.is_file() or binary.is_symlink():
            raise ValueError("Cargo did not produce a regular Glomeris executable")
        verify_binary(binary, expected, runner)
        return binary.read_bytes()


def install(home, checkout, runner=subprocess.run):
    spec = manifest()
    home = Path(home).expanduser().absolute()
    base = home / RUNTIME
    binaries = base / "glomeris"
    receipt_path = base / "storage-core.json"
    ensure_private_dir(base)
    ensure_private_dir(binaries)
    lock_fd = acquire_lock(base / "storage-core.lock")
    try:
        old, previous = read_selection(receipt_path)
        if old and receipt_state(receipt_path) != "installed":
            raise ValueError("Installed receipt has drift; refusing replacement")
        source = validate_checkout(checkout, spec["revision"], runner)
        expected = expected_version(source, spec["version"], runner)
        if old and old["source_revision"] == spec["revision"]:
            verify_binary(Path(old["path"]), expected, runner)
            return "installed"
        payload = build(source, spec["revision"], spec["version"], runner)
        checksum = hashlib.sha256(payload).hexdigest()
        destination_dir = binaries / checksum
        destination = destination_dir / "bin/glomeris"
        new = {"source_revision": spec["revision"], "binary_sha256": checksum, "path": str(destination)}
        reject_symlink_ancestry(destination)
        if destination_dir.exists():
            if read_receipt(destination_dir / "artifact.json") != new or not destination.is_file() or digest(destination) != checksum:
                raise ValueError("Refusing unowned or mismatched versioned binary")
        else:
            # Publish ownership and executable together. A crash after package
            # publication can recover by verifying artifact.json before reuse.
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=binaries))
            try:
                (stage / "bin").mkdir(mode=0o700)
                staged_binary = stage / "bin/glomeris"
                with staged_binary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged_binary.chmod(0o755)
                atomic_json(stage / "artifact.json", new)
                for folder in (stage / "bin", stage):
                    directory = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                os.rename(stage, destination_dir)
                directory = os.open(binaries, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)  # Only this invocation's private staging directory.
        verify_binary(destination, expected, runner)
        if old == new:
            return "installed"
        atomic_json(receipt_path, {"schema": 1, "current": new, "previous": old})
        return "installed"
    finally:
        os.close(lock_fd)


def rollback(home):
    base = Path(home).expanduser().absolute() / RUNTIME
    receipt_path = base / "storage-core.json"
    ensure_private_dir(base)
    lock_fd = acquire_lock(base / "storage-core.lock")
    try:
        current, previous = read_selection(receipt_path)
        if current is None:
            raise ValueError("Current storage receipt is absent")
        expected = base / "glomeris" / current["binary_sha256"] / "bin/glomeris"
        if Path(current["path"]) != expected:
            raise ValueError("Current storage receipt path is outside its owned destination")
        private_directory(base)
        if previous is None:
            raise ValueError("No previous owned storage receipt is available")
        if artifact_state(previous, base) != "installed":
            raise ValueError("Previous storage binary is missing or has drift")
        atomic_json(receipt_path, {"schema": 1, "current": previous, "previous": previous})
        # Retain the same rollback target for idempotent repeated recovery.
        return "rolled back"
    finally:
        os.close(lock_fd)


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--checkout", type=Path)
    args = parser.parse_args(argv)
    home = home_path()
    if args.apply:
        if args.checkout is None:
            parser.error("--apply requires --checkout PATH")
        print(install(home, args.checkout, runner))
        return 0
    if args.checkout:
        parser.error("--checkout is only valid with --apply")
    if args.rollback:
        print(rollback(home))
        return 0
    base = home / RUNTIME
    spec = manifest()
    state = receipt_state(base / "storage-core.json")
    receipt, _ = read_selection(base / "storage-core.json")
    if state == "installed" and receipt["source_revision"] != spec["revision"]:
        state = "outdated"
    if args.check:
        print(f"storage-core: {state}")
        return 0 if state == "installed" else 1
    action = "would install" if state == "absent" else ("refused: drift requires explicit diagnosis/restore" if state == "drift" else "would upgrade" if state == "outdated" else "already installed")
    print(f"storage-core: {state}; {action}")
    return 0 if state != "drift" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BlockingIOError:
        print("storage-core: busy", file=sys.stderr)
        raise SystemExit(75)
    except (OSError, ValueError, json.JSONDecodeError):
        print("storage-core: refused; invalid, drifted, incompatible or inaccessible state/build", file=sys.stderr)
        raise SystemExit(1)
