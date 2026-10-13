#!/usr/bin/env python3
"""Shared workflow receipts; never grant resource deletion authority.

A held kernel lock spans the caller's observe/act/verify sequence. Requests on
stdin only record verified outcomes; interrupted actions must be re-observed.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit

STEPS = {"ticket_closed", "worktree_removed", "remote_branch_removed",
         "local_branch_removed", "context_cleared", "reporter_handled"}


def binding_from(path):
    data = json.loads(Path(path).read_text())
    expected = {"repository", "pr", "head_sha", "merge_sha", "ticket",
                "git_common_dir", "worktree", "branch", "base_branch", "remote"}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("Unsupported or incomplete closeout binding")
    repo = data["repository"]
    if not isinstance(repo, str):
        raise ValueError("Invalid repository identity")
    if repo.startswith("git@"):
        host, _, name = repo[4:].partition(":")
        repo = "https://" + host + "/" + name
    url = urlsplit(repo)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Use a credential-free HTTPS repository identity")
    repo_path = url.path.rstrip("/").removesuffix(".git")
    if len(repo_path.strip("/").split("/")) < 2 or any(x in {"", ".", ".."} for x in repo_path.strip("/").split("/")):
        raise ValueError("Invalid repository path")
    host = url.hostname.lower()
    if host == "github.com":
        repo_path = repo_path.lower()
    authority = host if url.port in (None, 443) else f"{host}:{url.port}"
    data["repository"] = "https://" + authority + repo_path
    if type(data["pr"]) is not int or data["pr"] <= 0:
        raise ValueError("Invalid PR number")
    for key in ("head_sha", "merge_sha"):
        if not isinstance(data[key], str) or not re.fullmatch(r"[0-9a-f]{40,64}", data[key]):
            raise ValueError("Invalid commit identity")
    for key in ("ticket", "branch", "base_branch", "remote"):
        if not isinstance(data[key], str) or not data[key] or any(ord(c) < 32 for c in data[key]):
            raise ValueError("Invalid workflow identity")
    for key in ("git_common_dir", "worktree"):
        value = data[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Workflow paths must be absolute")
        data[key] = os.path.realpath(value)
    return data


def reject_links(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Refusing state through a symlink")
        if part.exists() and part.stat().st_uid != os.getuid() and part == path:
            raise ValueError("State is owned by another user")


def key_for(binding):
    identity = binding["repository"] + "#" + str(binding["pr"])
    return hashlib.sha256(identity.encode()).hexdigest()


@contextmanager
def locked(root, key):
    reject_links(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    reject_links(root)
    if not root.is_dir() or root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("Closeout state directory must be private and user-owned")
    path = root / (key + ".lock")
    reject_links(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Invalid closeout lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def read_record(path, binding):
    reject_links(path)
    if not path.exists():
        return {"schema": 1, "binding": binding, "steps": {}, "complete": False}
    if not path.is_file():
        raise ValueError("Invalid closeout receipt")
    if path.stat().st_size > 65536 or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Oversized or non-private receipt")
    record = json.loads(path.read_text())
    if (not isinstance(record, dict) or set(record) != {"schema", "binding", "steps", "complete"} or record.get("schema") != 1
            or record.get("binding") != binding or not isinstance(record.get("steps"), dict)
            or type(record.get("complete")) is not bool):
        raise ValueError("Corrupt, unsupported or conflicting closeout receipt")
    if not set(record["steps"]).issubset(STEPS):
        raise ValueError("Unknown closeout outcome")
    for step in record["steps"].values():
        if not isinstance(step, dict) or step.get("verified") is not True or not isinstance(step.get("evidence"), str) or not step["evidence"].strip():
            raise ValueError("Corrupt closeout outcome")
    if record["complete"] and set(record["steps"]) != STEPS:
        raise ValueError("Incomplete terminal receipt")
    return record


def save_record(path, record):
    reject_links(path)
    payload = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode()
    if len(payload) > 65536:
        raise ValueError("Oversized receipt")
    fd, name = tempfile.mkstemp(prefix=".closeout-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("status", "hold"))
    parser.add_argument("--binding", required=True, help="Private JSON operation identity; no credentials")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/coding-agent-environment/closeout")
    args = parser.parse_args()
    binding = binding_from(args.binding)
    key = key_for(binding)
    path = args.state_dir / (key + ".json")
    with locked(args.state_dir, key):
        record = read_record(path, binding)
        if args.mode == "status":
            emit({"exists": path.exists(), "record": record})
            return
        save_record(path, record)
        emit({"status": "locked", "record": record})
        for line in sys.stdin:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Invalid receipt request")
            op = request.get("op")
            if op == "status":
                emit({"record": record})
            elif op == "mark":
                step, evidence = request.get("step"), request.get("evidence")
                if step not in STEPS or not isinstance(evidence, str) or not evidence.strip() or len(evidence.encode()) > 4096:
                    raise ValueError("A known outcome and bounded verification evidence are required")
                if not record["complete"] and step not in record["steps"]:
                    record["steps"][step] = {"verified": True, "evidence": evidence}
                    save_record(path, record)
                emit({"status": "recorded", "step": step})
            elif op == "finish":
                if set(record["steps"]) != STEPS:
                    raise ValueError("Closeout outcomes remain pending")
                record["complete"] = True
                save_record(path, record)
                emit({"status": "complete"})
            elif op == "release":
                emit({"status": "released"})
                break
            else:
                raise ValueError("Unknown receipt request")


if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        emit({"status": "busy", "reason": "another closeout owns this operation"})
        raise SystemExit(75)
    except (ValueError, OSError, json.JSONDecodeError):
        # Never echo a malformed binding/config containing private data.
        emit({"status": "refused", "reason": "invalid, conflicting or inaccessible closeout state"})
        raise SystemExit(2)
