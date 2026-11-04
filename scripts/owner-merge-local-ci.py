#!/usr/bin/env python3
"""Recorder for the local-first CI basis of owner-merge-gate.py (SPE-135).

  owner-merge-local-ci.py record --repo OWNER/REPO --pr N --base BRANCH --worktree PATH [--expect-head SHA] [--config FILE]

Runs the owner-configured checks for the repository (the trust file ~/.config/coding-agent-environment/local/local-first-ci.json,
which this script only reads) in the worktree, with no shell, and prints exactly one unfenced line for the PR comment:
  OWNER-MERGE-LOCAL-CI {json}
The worktree must be clean before and after the run, so the record describes the committed head and nothing else. A failing command
is still recorded (exit_code non-zero) and the script exits 1; the gate denies on any recorded failure. Full logs stay on this machine
(mode 0600, ~/.claude/owner-merge-evidence/); only their SHA-256 digests are recorded. It never posts, merges or writes the trust file.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_spec = importlib.util.spec_from_file_location("owner_merge_gate", pathlib.Path(__file__).resolve().with_name("owner-merge-gate.py"))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

EVIDENCE_DIR = "~/.claude/owner-merge-evidence"
COMMAND_TIMEOUT = 6 * 3600
# Credential-looking argv would land in the PR comment, so it is refused before anything runs.
SECRET_RE = re.compile(r"(?i)(token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential|authorization)\s*[=:]"
                       r"|gh[opusr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN ")


def stamp(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def git(worktree, *args):
    r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def clean(worktree):
    return git(worktree, "status", "--porcelain", "--untracked-files=all") == ""


def load_entry(repo, path=None, root=None):
    """The owner's entry for this repo; raises ValueError when the file is untrusted, unreadable or does not list the repo."""
    cfg = gate.read_owner_file(path or gate.LOCAL_FIRST_DEFAULT, True, gate.load_local_first, "local-first CI config",
                               root or os.path.dirname(os.path.expanduser(gate.LOCAL_FIRST_DEFAULT)))
    if repo.lower() not in cfg:
        raise ValueError(f"{repo} is not listed in the local-first trust file")
    return cfg[repo.lower()]


def run_check(worktree, name, argv, logs, head, tag):
    """Run one argv without a shell; the combined output goes to a private log and only its digest is returned."""
    started = time.time()
    try:
        r = subprocess.run(list(argv), cwd=worktree, capture_output=True, timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
        code, data = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        code, data = 124, (e.stdout or b"") + (e.stderr or b"")
    except OSError as e:   # the program cannot start: that is a failed check, not a crash
        code, data = 127, str(e).encode()
    path = os.path.join(logs, f"local-ci-{tag}-{head[:12]}-{re.sub(r'[^A-Za-z0-9._-]', '_', name)}.log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return {"name": name, "argv": list(argv), "exit_code": code, "started_at": stamp(started), "finished_at": stamp(),
            "log_sha256": hashlib.sha256(data).hexdigest()}, path


def record(repo, pr, base, worktree, entry, logs, expect_head=None):
    """Run the configured checks and print the record. Returns the process exit code: 0 all passed, 1 a check failed, 2 refused."""
    def refuse(why):
        print(f"owner-merge-local-ci: refused: {why}", file=sys.stderr)
        return 2
    if not gate.REPO_RE.fullmatch(repo) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(pr)) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base):
        return refuse("repo, pr or base is malformed")
    for name, argv in entry["checks"]:
        if any(SECRET_RE.search(a) for a in argv):
            return refuse(f"check {name!r} has a credential-shaped argument")
    try:
        head = git(worktree, "rev-parse", "HEAD")
        if expect_head and head != expect_head:
            return refuse(f"worktree HEAD {head[:12]} is not the expected {expect_head[:12]}")
        if not clean(worktree):
            return refuse("the worktree is not clean; commit or remove changes so the record describes the committed head")
        base_sha = git(worktree, "merge-base", f"origin/{base}", "HEAD")
    except RuntimeError as e:
        return refuse(str(e))
    os.makedirs(os.path.expanduser(logs), mode=0o700, exist_ok=True)
    tag = f"{repo.replace('/', '__')}-{pr}"
    commands = []
    for name, argv in entry["checks"]:
        result, path = run_check(worktree, name, argv, os.path.expanduser(logs), head, tag)
        commands.append(result)
        print(f"owner-merge-local-ci: {name} exit {result['exit_code']} log {path}", file=sys.stderr)
    try:
        if git(worktree, "rev-parse", "HEAD") != head or not clean(worktree):
            return refuse("the checks changed the worktree or HEAD; no record is produced")
    except RuntimeError as e:
        return refuse(str(e))
    out = {"schema": 1, "repo": repo, "pr": int(pr), "base": base, "base_sha": base_sha, "head": head, "commands": commands}
    print("OWNER-MERGE-LOCAL-CI " + json.dumps(out, separators=(",", ":")))
    return 0 if all(c["exit_code"] == 0 for c in commands) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", required=True)
    r.add_argument("--base", required=True)
    r.add_argument("--worktree", required=True)
    r.add_argument("--expect-head")
    r.add_argument("--config", help=f"owner trust file; must live under the owner-config directory (default {gate.LOCAL_FIRST_DEFAULT})")
    a = ap.parse_args(argv)
    if a.expect_head is not None and not re.fullmatch(r"[0-9a-f]{40}", a.expect_head):
        print("owner-merge-local-ci: refused: --expect-head must be a full 40-hex sha", file=sys.stderr)
        return 2
    try:
        entry = load_entry(a.repo, a.config)
    except (OSError, ValueError) as e:
        print(f"owner-merge-local-ci: refused: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 2
    return record(a.repo, a.pr, a.base, os.path.realpath(a.worktree), entry, EVIDENCE_DIR, a.expect_head)


if __name__ == "__main__":
    sys.exit(main())
