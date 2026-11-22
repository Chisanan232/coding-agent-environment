#!/usr/bin/env python3
"""Recorder for the local-first CI basis of owner-merge-gate.py (SPE-135).

  owner-merge-local-ci.py record --repo OWNER/REPO --pr N --base BRANCH --worktree PATH [--evidence-host {claude,codex}] [--expect-head SHA] [--config FILE]

Runs the owner-configured checks for the repository (the trust file ~/.config/coding-agent-environment/local/local-first-ci.json,
which this script only reads) in the worktree, with no shell, and prints exactly one unfenced line for the PR comment:
  OWNER-MERGE-LOCAL-CI {json}
The worktree must be clean before and after the run, so the record describes the committed head (tracked and untracked files; git-ignored files are not seen). A failing command
is still recorded (exit_code non-zero) and the script exits 1; the gate denies on any recorded failure. Full logs stay on this machine
(mode 0600, ~/.claude/owner-merge-evidence/); only their SHA-256 digests are recorded. It never posts, merges or writes the trust file.
Checks run with a scrubbed environment (ENV_KEEP plus the owner's `env`) and a fixed hosted-equivalent context the recorder derives itself, never from
the caller's environment: PULL_BRANCH and PR_TITLE (read from the PR with `gh pr view`, which must report the worktree HEAD), GIT_BASE (the merge base) and
CARGO_TARGET_DIR (<worktree>/target, so a host-wide cargo `build.target-dir` is not used). The detached worktree and the host's shared target dir would
otherwise make a check differ from hosted CI. The owner's `env` map and the three PR-derived values ("context") are part of the record (the gate compares
them with the trust file and the PR) and all kept names are printed to stderr. The clean-tree test uses `--untracked-files=all`, which does not see git-ignored files: ignored content (a venv, a cache, a
build directory) can still influence a check and is not covered by the record.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import unicodedata

_spec = importlib.util.spec_from_file_location("owner_merge_gate", pathlib.Path(__file__).resolve().with_name("owner-merge-gate.py"))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
_rspec = importlib.util.spec_from_file_location("owner_merge_runner", pathlib.Path(__file__).resolve().with_name("owner-merge-runner.py"))
runner = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(runner)

EVIDENCE_DIR = "~/.claude/owner-merge-evidence"
CODEX_EVIDENCE_DIR = "~/.codex/owner-merge-evidence"
EVIDENCE_HOSTS = ("claude", "codex")
COMMAND_TIMEOUT = 6 * 3600
# Allowlist, not a blocklist: anything not named here (PYTHONPATH, NODE_PATH, VIRTUAL_ENV, PIP_*, LD_*, tokens ...) never reaches a check.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "TZ")
RESOURCE_LIMIT_KEYS = ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS")
RESOURCE_LIMIT_RE = re.compile(r"[0-9]{1,1024}")
# Credential-looking argv would land in the PR comment, so it is refused before anything runs.
SECRET_RE = re.compile(r"(?i)(token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential|authorization)\s*[=:]"
                       r"|gh[opusr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN ")


def stamp(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def git(worktree, *args):
    # Ambient GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_CONFIG* ... could point git at another repository than --worktree.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, timeout=120, env=env)
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


def resource_caps(inherited, configured):
    """Validate the three admitted resource limits and return their non-increasing effective values."""
    if not isinstance(configured, dict):
        raise ValueError("configured environment must be an object")
    effective = {}
    for key in RESOURCE_LIMIT_KEYS:
        values = []
        for source, env in (("inherited", inherited), ("configured", configured)):
            if key not in env:
                continue
            value = env[key]
            if not isinstance(value, str) or not RESOURCE_LIMIT_RE.fullmatch(value):
                raise ValueError(f"{source} {key} must be positive ASCII decimal from 1 to 1024")
            number = int(value)
            if not 1 <= number <= 1024:
                raise ValueError(f"{source} {key} must be positive ASCII decimal from 1 to 1024")
            values.append(number)
        if values:
            effective[key] = str(min(values))
    return effective


def check_env(extra, context=None, caps=None):
    configured = {key: value for key, value in extra.items() if key not in RESOURCE_LIMIT_KEYS}
    return {**{k: os.environ[k] for k in ENV_KEEP if k in os.environ}, **configured,
            **(context or {}), **(caps or {})}


def pr_context(repo, pr, head):
    """PULL_BRANCH and PR_TITLE from the PR itself; raises ValueError unless gh answers, the PR head is `head` and both fields are well formed."""
    try:
        info = json.loads(gate.gh("pr", "view", str(pr), "--repo", repo, "--json", "headRefName,headRefOid,title"))
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
        raise ValueError(f"cannot read the PR from gh: {type(e).__name__}") from None
    ref, title = (info.get("headRefName"), info.get("title")) if isinstance(info, dict) else (None, None)
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref) \
            or not isinstance(title, str) or not 1 <= len(title) <= 500 or any(unicodedata.category(c) == "Cc" for c in title):
        raise ValueError("the PR head ref or title reported by gh is malformed")
    if info.get("headRefOid") != head:
        raise ValueError("the PR head on GitHub is not the worktree HEAD; push or check out the PR head first")
    return {"PULL_BRANCH": ref, "PR_TITLE": title}


def log_path(logs, name, head, tag):
    return os.path.join(logs, f"local-ci-{tag}-{head[:12]}-{gate.log_slug(name)}.log")


def record_path(logs, head, tag):
    return os.path.join(logs, f"local-ci-{tag}-{head[:12]}.record.json")


def write_private(path, text):
    """Persist a file atomically with mode 0600 (exclusive temp file, then rename); a link or non-file at `path` is refused."""
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
        raise RuntimeError(f"{path} is not a regular file")
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def open_log(path):
    """Open (never follow a link) a log file that is a regular file owned by the caller with mode exactly 0600, creating it when absent."""
    import stat
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        os.close(fd)
        raise RuntimeError(f"log file {path} is not a regular file of the caller with mode 0600")
    os.ftruncate(fd, 0)
    return os.fdopen(fd, "wb")


def prepare_dir(logs):
    """The evidence directory must be a real directory (not a link) owned by the caller; it is made 0700."""
    if os.path.islink(logs):
        raise RuntimeError(f"{logs} is a symlink")
    os.makedirs(logs, mode=0o700, exist_ok=True)
    if os.stat(logs).st_uid != os.getuid():
        raise RuntimeError(f"{logs} is not owned by the caller")
    os.chmod(logs, 0o700)


def prepare_codex_dir(logs):
    """Require the fixed private Codex evidence location before any configured check runs."""
    root = os.path.expanduser("~/.codex")
    expected = os.path.join(root, "owner-merge-evidence")
    if os.path.abspath(logs) != os.path.abspath(expected):
        raise RuntimeError("Codex evidence path differs from the fixed host location")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        root_info = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
                or stat.S_IMODE(root_info.st_mode) & 0o022):
            raise RuntimeError("Codex host root must be caller-owned and not group/world writable")
        try:
            evidence_fd = os.open("owner-merge-evidence", flags, dir_fd=root_fd)
        except FileNotFoundError:
            os.mkdir("owner-merge-evidence", 0o700, dir_fd=root_fd)
            evidence_fd = os.open("owner-merge-evidence", flags, dir_fd=root_fd)
        try:
            evidence_info = os.fstat(evidence_fd)
            if (not stat.S_ISDIR(evidence_info.st_mode) or evidence_info.st_uid != os.getuid()
                    or stat.S_IMODE(evidence_info.st_mode) != 0o700):
                raise RuntimeError("Codex evidence directory must be caller-owned with mode 0700")
        finally:
            os.close(evidence_fd)
    finally:
        os.close(root_fd)


def run_check(worktree, name, argv, logs, head, tag, env=None, context=None, caps=None):
    """Run one argv without a shell; the combined output goes to a private log and only its digest is returned."""
    started = time.time()
    path = log_path(logs, name, head, tag)
    try:
        r = subprocess.run(list(argv), cwd=worktree, capture_output=True, timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL,
                           env=check_env(env or {}, context, caps))
        code, data = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        code, data = 124, (e.stdout or b"") + (e.stderr or b"")
    except OSError as e:   # the program cannot start: that is a failed check, not a crash
        code, data = 127, str(e).encode()
    with open_log(path) as f:
        f.write(data)
    return {"name": name, "argv": list(argv), "exit_code": code, "started_at": stamp(started), "finished_at": stamp(),
            "log_sha256": hashlib.sha256(data).hexdigest()}, path


def record(repo, pr, base, worktree, entry, logs, expect_head=None, evidence_host="claude"):
    """Run the configured checks and print the record. Returns the process exit code: 0 all passed, 1 a check failed, 2 refused."""
    def refuse(why):
        print(f"owner-merge-local-ci: refused: {why}", file=sys.stderr)
        return 2
    inherited_limits = {key: os.environ[key] for key in RESOURCE_LIMIT_KEYS if key in os.environ}
    try:
        effective_caps = resource_caps(inherited_limits, entry.get("env") or {})
    except ValueError as e:
        return refuse(str(e))
    if not gate.REPO_RE.fullmatch(repo) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(pr)) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base):
        return refuse("repo, pr or base is malformed")
    if evidence_host not in EVIDENCE_HOSTS:
        return refuse("evidence host is unsupported")
    if len({gate.log_slug(n) for n, _ in entry["checks"]}) != len(entry["checks"]):
        return refuse("two checks would share one log file")
    for name, argv in entry["checks"]:
        if any(SECRET_RE.search(a) for a in argv):
            return refuse(f"check {name!r} has a credential-shaped argument")
    logs = os.path.expanduser(logs)
    try:
        if evidence_host == "codex":
            prepare_codex_dir(logs)
    except (OSError, RuntimeError) as e:
        return refuse(str(e))
    try:
        if os.path.realpath(git(worktree, "rev-parse", "--show-toplevel")) != os.path.realpath(worktree):
            return refuse("the worktree is not the top level of its git repository")
        head = git(worktree, "rev-parse", "HEAD")
        if expect_head and head != expect_head:
            return refuse(f"worktree HEAD {head[:12]} is not the expected {expect_head[:12]}")
        if not clean(worktree):
            return refuse("the worktree is not clean; commit or remove changes so the record describes the committed head")
        base_sha = git(worktree, "merge-base", f"origin/{base}", "HEAD")
    except RuntimeError as e:
        return refuse(str(e))
    try:
        context = {**pr_context(repo, pr, head), "GIT_BASE": base_sha}
    except ValueError as e:
        return refuse(str(e))
    check_context = {**context, "CARGO_TARGET_DIR": os.path.join(os.path.realpath(worktree), "target")}
    tag = f"{repo.replace('/', '__')}-{pr}"
    try:
        if evidence_host == "claude":
            prepare_dir(logs)
        else:
            prepare_codex_dir(logs)
        for name, _ in entry["checks"]:   # check every log file before anything runs, so a bad one cannot be found after the work is done
            path = log_path(logs, name, head, tag)
            if os.path.lexists(path):
                open_log(path).close()
        rpath = record_path(logs, head, tag)
        if os.path.lexists(rpath) and (os.path.islink(rpath) or not os.path.isfile(rpath)):
            return refuse(f"record file {rpath} is not a regular file")
    except (OSError, RuntimeError) as e:
        return refuse(str(e))
    child_env = check_env(entry.get("env") or {}, check_context, effective_caps)
    print("owner-merge-local-ci: check environment: " + ",".join(sorted(child_env)), file=sys.stderr)
    caps_note = ",".join(f"{key}={value}" for key, value in sorted(effective_caps.items())) or "none (standalone behavior)"
    print("owner-merge-local-ci: effective resource caps: " + caps_note, file=sys.stderr)
    commands = []
    for name, argv in entry["checks"]:
        result, path = run_check(worktree, name, argv, logs, head, tag, entry.get("env"), check_context, effective_caps)
        commands.append(result)
        print(f"owner-merge-local-ci: {name} exit {result['exit_code']} log {path}", file=sys.stderr)
    failed = any(c["exit_code"] != 0 for c in commands)
    try:
        moved = git(worktree, "rev-parse", "HEAD") != head or not clean(worktree)
    except RuntimeError as e:
        moved = str(e)
    if moved and not failed:
        return refuse("the checks changed the worktree or HEAD; no record is produced")
    if moved:   # a failing record is still printed (the gate denies on it); a pass is never produced from a changed tree
        print("owner-merge-local-ci: the checks also changed the worktree; printing the failing record only", file=sys.stderr)
    out = {"schema": 1, "repo": repo, "pr": int(pr), "base": base, "base_sha": base_sha, "head": head,
           "env": dict(entry.get("env") or {}), "context": context, "commands": commands}
    line = json.dumps(out, separators=(",", ":"))
    try:   # the persisted file is what `evidence` consumes; it must equal the printed line
        write_private(record_path(logs, head, tag), line + "\n")
    except (OSError, RuntimeError) as e:
        return refuse(f"cannot persist the record: {e}")
    print("OWNER-MERGE-LOCAL-CI " + line)
    return 0 if all(c["exit_code"] == 0 for c in commands) else 1


class EvidenceRefused(Exception):
    pass


def read_private(path, label, limit=1 << 28):
    """Bytes of a regular file owned by the caller with no group/other access, opened without following a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise EvidenceRefused(f"{label} cannot be opened ({type(e).__name__})") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise EvidenceRefused(f"{label} is not a regular file of the caller with mode 0600")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise EvidenceRefused(f"{label} is too large")
    return data


def local_ci_comment_line(run, repo, pr, head):
    """Fallback source for a recording that predates the persisted record file: the newest unfenced OWNER-MERGE-LOCAL-CI line of the
    authenticated user's comments for this head. It is the recorder's own printed line, posted unedited as the gate requires."""
    me = run("api", "user", "--jq", ".login").strip().lower()
    p = gate._json(run, "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid,comments")
    if p["headRefOid"] != head:
        raise EvidenceRefused("the PR is no longer at this head")
    comments, rest_n, err = gate.merged_comments(run, repo, pr, p.get("comments"))
    if err or (rest_n or 0) >= 100 or len(p.get("comments") or []) >= 100:
        raise EvidenceRefused("the PR comments could not be read completely")
    lines = [m for c in comments if (c.get("author") or {}).get("login", "").lower() == me
             for m in gate.LOCAL_RE.findall(re.sub(r"```.*?```", "", c.get("body") or "", flags=re.S))]
    for raw in reversed(lines):
        try:
            rec = json.loads(raw, object_pairs_hook=gate._no_dup)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("head") == head:
            return rec
    raise EvidenceRefused("no OWNER-MERGE-LOCAL-CI comment by the authenticated user for this head")


def build_evidence(run, a, now=None):
    """Review evidence with a local_ci block built only from a recorder-written record and the log files it names. Raises EvidenceRefused."""
    now = time.time() if now is None else now
    if not gate.REPO_RE.fullmatch(a.repo) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(a.pr)) or not re.fullmatch(r"[0-9a-f]{40}", a.head):
        raise EvidenceRefused("repo, pr and head (40-hex) must be well formed")
    logs = os.path.expanduser(CODEX_EVIDENCE_DIR if a.evidence_host == "codex" else EVIDENCE_DIR)
    if os.path.islink(logs) or not os.path.isdir(logs) or os.stat(logs).st_uid != os.getuid() or os.stat(logs).st_mode & 0o077:
        raise EvidenceRefused("the evidence directory must be a real directory of the caller with mode 0700")
    tag = f"{a.repo.replace('/', '__')}-{a.pr}"
    if a.from_pr_comment:
        rec = local_ci_comment_line(run, a.repo, a.pr, a.head)
    else:
        path = a.record or record_path(logs, a.head, tag)
        try:
            rec = json.loads(read_private(path, "record file").decode("utf-8"), object_pairs_hook=gate._no_dup)
        except (ValueError, UnicodeDecodeError):
            raise EvidenceRefused("record file is not valid JSON") from None
    try:
        finished = gate.parse_local_record(rec)
        started = [gate._stamp(c["started_at"]) for c in rec["commands"]]
    except (ValueError, KeyError, TypeError) as e:
        raise EvidenceRefused(f"record is malformed ({e})") from None
    if rec["repo"].lower() != a.repo.lower() or rec["pr"] != int(a.pr) or rec["head"] != a.head:
        raise EvidenceRefused("the record is for another repo, PR or head")
    try:
        pr = json.loads(run("pr", "view", str(a.pr), "--repo", a.repo, "--json", "headRefOid,baseRefName,headRefName"))
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
        raise EvidenceRefused("the live PR cannot be read from gh") from None
    if pr.get("headRefOid") != a.head:
        raise EvidenceRefused("the live PR head is not the recorded head")
    if pr.get("baseRefName") != rec["base"] or (a.base and a.base != rec["base"]):
        raise EvidenceRefused("the record base does not match the live PR base")
    if pr.get("headRefName") != rec["context"]["PULL_BRANCH"]:
        raise EvidenceRefused("the record was made for a different PR head ref")
    try:
        entry = load_entry(a.repo, a.config)
    except (OSError, ValueError) as e:
        raise EvidenceRefused(f"trust file: {type(e).__name__}: {str(e)[:150]}") from None
    if [(c["name"], tuple(c["argv"])) for c in rec["commands"]] != list(entry["checks"]) or rec["env"] != dict(entry.get("env") or {}):
        raise EvidenceRefused("the recorded checks or env differ from the owner's trust file (extra, missing, renamed or reordered)")
    bad = [c["name"] for c in rec["commands"] if c["exit_code"] != 0]
    if bad:
        raise EvidenceRefused("recorded check did not pass: " + ", ".join(bad))
    required, max_age, reviewers, need_sig = list(a.require or []), 24, None, False
    try:
        cfg = runner.repo_config(runner.load_allowlist(a.allowlist or os.path.expanduser("~/.claude/owner-merge-runner-allowlist.json")), a.repo)
        required = list(dict.fromkeys(cfg["required_local_checks"] + required))
        max_age, reviewers, need_sig = cfg["max_evidence_age_hours"], cfg["reviewers"], cfg["require_signed_evidence"]
    except runner.Refused as e:
        if not a.require:
            raise EvidenceRefused("allowlist unusable and no --require given: " + "; ".join(e.args[0])[:200]) from None
    names = [c["name"] for c in rec["commands"]]
    if [n for n in required if n not in names]:
        raise EvidenceRefused("record lacks required local checks: " + ", ".join(n for n in required if n not in names))
    age = min(max_age, entry["max_age_hours"]) * 3600
    run_at = min(started)
    if now - run_at > age or max(finished) > now + 60 or run_at > now + 60:
        raise EvidenceRefused("the record is stale or in the future")
    for c in rec["commands"]:
        digest = hashlib.sha256(read_private(log_path(logs, c["name"], a.head, tag), f"log of {c['name']}")).hexdigest()
        if digest != c["log_sha256"]:
            raise EvidenceRefused(f"log of {c['name']} does not match the recorded SHA-256")
    try:
        at, reviewer = gate.review_comment(run, a.repo, a.pr, a.head)
    except (ValueError, KeyError) as e:
        raise EvidenceRefused(f"review comment: {str(e)[:200]}") from None
    if a.reviewer and a.reviewer != reviewer:
        raise EvidenceRefused("--reviewer differs from the reviewer in the authenticated user's OWNER-MERGE-REVIEW comment")
    if reviewers is not None and reviewer not in reviewers:
        raise EvidenceRefused("the review comment's reviewer is not in the allowlisted reviewers for this repo")
    try:
        ev = gate.evidence_json(a.repo, a.pr, rec["base"], a.head, reviewer, a.kind, now=now, reviewed_at=at)
    except ValueError as e:
        raise EvidenceRefused(str(e)) from None
    ev["local_ci"] = {"classification": "LOCAL_EQUIVALENT_PASS", "head": a.head, "run_at": stamp(run_at),
                      "checks": [{"name": n, "exit_code": 0, "tests": 0} for n in names]}
    if a.key_file:
        try:
            ev["hmac"] = runner.evidence_mac(runner.load_key(a.key_file), ev)
        except runner.Refused as e:
            raise EvidenceRefused("key file: " + "; ".join(e.args[0])[:150]) from None
    elif need_sig:
        raise EvidenceRefused("the allowlist requires signed evidence; pass --key-file")
    return logs, f"{tag}-{a.head[:12]}.json", ev


def write_evidence(logs, name, ev):
    """Atomically write the evidence file (0600). Idempotent for identical content; refuses a link or a file for another repo/PR/head."""
    path = os.path.join(logs, name)
    text = json.dumps(ev, indent=1, sort_keys=True) + "\n"
    status = "written"
    if os.path.lexists(path):
        old = read_private(path, "existing evidence file")
        try:
            prev = json.loads(old.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise EvidenceRefused("existing evidence file is not JSON; not overwritten") from None
        if not isinstance(prev, dict) or (prev.get("repo", "").lower(), prev.get("pr"), prev.get("head")) != (ev["repo"].lower(), ev["pr"], ev["head"]):
            raise EvidenceRefused("existing evidence file is for another repo, PR or head; not overwritten")
        if old == text.encode():
            return path, hashlib.sha256(old).hexdigest(), "unchanged"
        status = "replaced"
    try:
        write_private(path, text)
    except (OSError, RuntimeError) as e:
        raise EvidenceRefused(f"cannot write the evidence file: {e}") from None
    return path, hashlib.sha256(text.encode()).hexdigest(), status


def main(argv=None, run=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", required=True)
    r.add_argument("--base", required=True)
    r.add_argument("--worktree", required=True)
    r.add_argument("--expect-head")
    r.add_argument("--evidence-host", choices=EVIDENCE_HOSTS, default="claude",
                   help="host-owned private evidence directory (default: claude)")
    r.add_argument("--config", help=f"owner trust file; must live under the owner-config directory (default {gate.LOCAL_FIRST_DEFAULT})")
    e = sub.add_parser("evidence", help="build the runner's review-evidence file, with its local_ci block, from a recorded run")
    e.add_argument("--repo", required=True)
    e.add_argument("--pr", required=True)
    e.add_argument("--head", required=True)
    e.add_argument("--base", help="must equal the recorded and live PR base when given")
    e.add_argument("--record", help="record file written by `record` (default: the one named for this repo, PR and head)")
    e.add_argument("--from-pr-comment", action="store_true",
                   help="fallback for a recording made before records were persisted: use the OWNER-MERGE-LOCAL-CI line the authenticated user posted")
    e.add_argument("--reviewer", help="optional; must equal the reviewer in the OWNER-MERGE-REVIEW comment (which is the source)")
    e.add_argument("--kind", default="independent-agent")
    e.add_argument("--require", action="append", help="required local check name (repeatable); added to the allowlist's required_local_checks")
    e.add_argument("--allowlist", help="runner allowlist (default ~/.claude/owner-merge-runner-allowlist.json), read only")
    e.add_argument("--key-file", help="sign the evidence with this key (needed when the allowlist requires signed evidence)")
    e.add_argument("--evidence-host", choices=EVIDENCE_HOSTS, default="claude")
    e.add_argument("--config", help="owner trust file (default as for record)")
    a = ap.parse_args(argv)
    if a.cmd == "evidence":
        try:
            logs, name, ev = build_evidence(run or gate.gh, a)
            path, digest, status = write_evidence(logs, name, ev)
        except EvidenceRefused as e:
            print(f"owner-merge-local-ci: refused: {e}", file=sys.stderr)
            return 2
        print(f"owner-merge-local-ci: evidence {status} {path} sha256 {digest}")
        return 0
    if a.expect_head is not None and not re.fullmatch(r"[0-9a-f]{40}", a.expect_head):
        print("owner-merge-local-ci: refused: --expect-head must be a full 40-hex sha", file=sys.stderr)
        return 2
    try:
        entry = load_entry(a.repo, a.config)
    except (OSError, ValueError) as e:
        print(f"owner-merge-local-ci: refused: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 2
    evidence_dir = EVIDENCE_DIR if a.evidence_host == "claude" else CODEX_EVIDENCE_DIR
    return record(a.repo, a.pr, a.base, os.path.realpath(a.worktree), entry, evidence_dir,
                  a.expect_head, evidence_host=a.evidence_host)


if __name__ == "__main__":
    sys.exit(main())
