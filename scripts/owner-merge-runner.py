#!/usr/bin/env python3
"""Structurally validated owner pull-request merge runner (SPE-118, Option A).

  owner-merge-runner.py --repo OWNER/NAME --pr N --expect-head SHA40 --review-evidence PATH [--dry-run]

The allowlist (~/.claude/owner-merge-runner-allowlist.json) and the audit log
(~/.claude/audit/owner-merge-runner.jsonl) are fixed paths with no CLI override; only the module API
(used by tests) can pass others. The allowlist must be owned by the current user, not a symlink, not
group/world-writable, and outside any git worktree.

Structured arguments only. Every input is validated by a strict regex before use, gh is started with
fixed argv lists (shell=False), and the single mutating call is exactly:

  gh pr merge N --repo OWNER/NAME --merge --match-head-commit SHA

Never --admin, --squash, --rebase, --auto or --delete-branch. A refusal, denial or error is recorded and
the runner exits nonzero; it never retries, never tries another route, and never touches settings.
The review evidence file is produced by an independent review; the runner only reads it.

Exit codes: 0 success (or validated dry run), 2 refused by a guard, 3 denied by gh, 4 error,
5 unverified (merge ran but the read-back did not show MERGED), 6 final audit record not written
(the outcome is on stderr), 7 the PR is MERGED but its base differs from the reviewed base
(INCIDENT: POSSIBLE MERGE TO UNREVIEWED BASE; treat as a security incident), 8 unexpected internal
error (audited, fail closed; if a merge had been attempted the record says it may have executed), 64 usage error.
"""
import argparse
import calendar
import hashlib
import urllib.parse
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

EXIT_OK, EXIT_REFUSED, EXIT_DENIED, EXIT_ERROR, EXIT_UNVERIFIED, EXIT_AUDIT, EXIT_BASE_MISMATCH, EXIT_INTERNAL, \
    EXIT_USAGE = 0, 2, 3, 4, 5, 6, 7, 8, 64
INCIDENT = "POSSIBLE MERGE TO UNREVIEWED BASE"

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}")
PR_RE = re.compile(r"[1-9][0-9]{0,8}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
MAC_RE = re.compile(r"[0-9a-f]{64}")
HTTP_TOKEN_RE = re.compile(r"HTTP ([^\s:)]*)")
STATUS_RE = re.compile(r"[0-9]{3}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")
READ_PATHS = (re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/rules/branches/([A-Za-z0-9._/-]{1,200})"),
              re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/branches/([A-Za-z0-9._/-]{1,200})/protection"),
              re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/rulesets/([1-9][0-9]{0,11})"))
JOB_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/actions/runs/[0-9]{1,15}/job/([0-9]{1,15})")
CHECK_RUNS_RE = re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/commits/([0-9a-f]{40})/check-runs\?check_name=([A-Za-z0-9%._~-]{1,300})&per_page=100")
JOB_PATH_RE = re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/actions/jobs/([0-9]{1,15})")
# A required check may be exempted only if it never executed (see unavailable_checks); these are the verdicts a
# never-started hosted job shows. SKIPPED, TIMED_OUT, NEUTRAL, ACTION_REQUIRED and every other verdict stay blockers.
NOT_EXECUTED_VERDICTS = {"FAILURE", "STARTUP_FAILURE", "CANCELLED"}
LOCAL_CI_KEYS = {"classification", "head", "run_at", "checks"}
LOCAL_CI_CHECK_KEYS = {"name", "exit_code", "tests"}
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
REVIEWER_KINDS = {"human", "independent-agent"}
MAX_EVIDENCE_AGE_HOURS = 168
MIN_KEY_BYTES = 32
LOGIN_RE = re.compile(r"(app/)?[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

PR_FIELDS = ("state,isDraft,author,baseRefName,headRefName,headRefOid,isCrossRepository,mergeable,"
             "mergeStateStatus,reviewDecision,reviews,statusCheckRollup,files")
READBACK_FIELDS = "state,mergeCommit,baseRefName"

BOT_LOGINS = {"dependabot", "renovate"}
BOT_BRANCH = re.compile(r"(dependabot|renovate)/")
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
SECRET_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|(?i:\bauthorization)\s*[=:][^\r\n]*"  # the whole header value, scheme included
    r"|(?i:\b(?:bearer|basic)\s+)[^\s\"']+"
    r"|(?i:\b(?:token|password|passwd|secret|api[_-]?key|apikey|access[_-]?token)\b)\s*[=:]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]*)")  # quoted value (spaces included) or to end of line
DENIAL_RE = re.compile(r"permission|denied|forbidden|not authori[sz]ed|HTTP 40[13]|must have|"
                       r"protected branch|required status|review is required|not allowed", re.I)
DEFAULT_AUDIT = os.path.join(os.path.expanduser("~"), ".claude", "audit", "owner-merge-runner.jsonl")
DEFAULT_ALLOWLIST = os.path.join(os.path.expanduser("~"), ".claude", "owner-merge-runner-allowlist.json")
TRUSTED_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}


class Refused(Exception):
    """A guard refused; carries every reason found."""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def scrub(text):
    return SECRET_RE.sub("[redacted]", str(text))[:300]


def gh_error(err, stage):
    """The only thing ever stored about a gh failure: our own stage name, a parsed three-digit HTTP status
    and a fixed category. No stderr text, excerpt or digest of it is kept anywhere."""
    status = http_status(str(err))
    category = ("denied" if status in ("401", "403") else "not-found" if status == "404"
                else "error" if status else "unreadable")
    return f"gh failure stage={stage} status={status or 'unknown'} category={category}"


def merge_argv(repo, pr, sha):
    """The only mutating command this program can run."""
    return ["pr", "merge", pr, "--repo", repo, "--merge", "--match-head-commit", sha]


def pr_view_argv(repo, pr, fields):
    return ["pr", "view", pr, "--repo", repo, "--json", fields]


def rules_argv(repo, base):
    return ["api", f"repos/{repo}/rules/branches/{base}"]


def protection_argv(repo, base):
    return ["api", f"repos/{repo}/branches/{base}/protection"]


def ruleset_argv(repo, rid):
    return ["api", f"repos/{repo}/rulesets/{rid}"]


def check_argv(argv):
    """Fail closed: only the fixed read shapes and the single fixed merge shape may reach gh."""
    a = list(argv)
    if a == ["api", "user"]:
        return "user"
    if len(a) == 7 and a[:2] == ["pr", "view"] and a[3] == "--repo" and a[5] == "--json" \
            and a[6] in (PR_FIELDS, READBACK_FIELDS) \
            and PR_RE.fullmatch(a[2]) and REPO_RE.fullmatch(a[4]):
        return "view"
    if len(a) == 2 and a[0] == "api":
        for rx in READ_PATHS:
            m = rx.fullmatch(a[1])
            if m and REPO_RE.fullmatch(m.group(1)) and ".." not in m.group(2):
                return "protection"
    if len(a) == 2 and a[0] == "api":
        m = JOB_PATH_RE.fullmatch(a[1])
        if m and REPO_RE.fullmatch(m.group(1)):
            return "job"
    if len(a) == 2 and a[0] == "api":
        m = CHECK_RUNS_RE.fullmatch(a[1])
        if m and REPO_RE.fullmatch(m.group(1)):
            return "check-runs"
    if len(a) == 8 and a == merge_argv(a[4], a[2], a[7]) and PR_RE.fullmatch(a[2]) \
            and REPO_RE.fullmatch(a[4]) and SHA_RE.fullmatch(a[7]):
        return "merge"
    raise RuntimeError("refused gh argv outside the fixed shapes")


class SubprocessGh:
    """Runs gh with a fixed argv list and no shell. The path is resolved once by locate_gh."""

    def __init__(self, path):
        self.path = path

    def __call__(self, argv):
        env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
        r = subprocess.run([self.path, *argv], capture_output=True, text=True, timeout=120,
                           shell=False, stdin=subprocess.DEVNULL, env=env)
        return r.returncode, r.stdout, r.stderr


def locate_gh():
    path = shutil.which("gh")
    if not path or not os.path.isabs(path) or not os.access(path, os.X_OK):
        raise RuntimeError("gh not found as an absolute executable path")
    return SubprocessGh(path)


class Runner:
    """Wraps an injected `gh(argv) -> (rc, stdout, stderr)` and enforces check_argv on every call."""

    def __init__(self, gh):
        self.gh = gh

    def call(self, argv):
        check_argv(argv)
        return self.gh(list(argv))

    def json(self, argv):
        rc, out, err = self.call(argv)
        if rc != 0:
            raise RuntimeError(gh_error(err, "pr-view"))
        try:
            data = json.loads(out)
        except ValueError:
            raise RuntimeError("gh returned unparseable JSON")
        if not isinstance(data, dict):
            raise RuntimeError("gh returned unexpected JSON shape")
        return data


def norm_bot(login):
    login = (login or "").lower()
    if login.startswith("app/"):
        login = login[4:]
    return login[:-5] if login.endswith("[bot]") else login


def validate_args(repo, pr, head):
    bad = []
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        bad.append("--repo is not OWNER/NAME")
    if not isinstance(pr, str) or not PR_RE.fullmatch(pr):
        bad.append("--pr is not a positive integer")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        bad.append("--expect-head is not 40 lowercase hex")
    if bad:
        raise Refused(bad)


def in_git_worktree(path):
    d = os.path.dirname(os.path.realpath(path))
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def dir_chain_problems(directory):
    """Every ancestor up to / must be a non-symlink owned by root or us and not group/world-writable
    (a sticky root-owned directory such as /tmp is the only writable exception)."""
    bad = []
    cur = os.path.abspath(directory)
    if os.path.realpath(cur) != cur:
        bad.append("path contains a symlink")
    while True:
        try:
            st = os.lstat(cur)
        except OSError:
            return bad + [f"ancestor {cur} is not readable"]
        if stat.S_ISLNK(st.st_mode):
            bad.append(f"ancestor {cur} is a symlink")
        if st.st_uid not in (0, os.geteuid()):
            bad.append(f"ancestor {cur} is not owned by root or the current user")
        if st.st_mode & 0o022 and not (st.st_mode & stat.S_ISVTX and st.st_uid == 0):
            bad.append(f"ancestor {cur} is group/world-writable")
        parent = os.path.dirname(cur)
        if parent == cur:
            return bad
        cur = parent


def read_trusted_file(path, label, loose_mask, limit=65536):
    """Walk the ancestors, open with O_NOFOLLOW, fstat the descriptor and read from that descriptor.

    loose_mask is the permission bits that must be clear (0o022 for the allowlist, 0o077 for a key file).
    Returns bytes. Never includes file content in an error."""
    path = os.path.abspath(path)
    bad = dir_chain_problems(os.path.dirname(path))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO must not hang the open
    except OSError:
        try:
            link = stat.S_ISLNK(os.lstat(path).st_mode)
        except OSError:
            link = False
        raise Refused(bad + ([f"{label} is not a regular non-symlink file"] if link
                             else [f"{label} file missing or unparseable"]))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            bad.append(f"{label} is not a regular non-symlink file")
        if st.st_uid != os.geteuid():
            bad.append(f"{label} is not owned by the current user")
        if st.st_mode & loose_mask:
            bad.append(f"{label} is group/world-writable" if loose_mask == 0o022
                       else f"{label} is readable or writable by group or others")
        if in_git_worktree(path):
            bad.append(f"{label} lives inside a git worktree")
        if bad:
            raise Refused(bad)
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)


def load_allowlist(path):
    """Return {"repos": {lowercased OWNER/NAME: raw config}, "evidence_key_file": str|None}."""
    try:
        text = read_trusted_file(path, "allowlist", 0o022).decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise Refused(["allowlist file missing or unparseable"])
    repos = data.get("repos") if isinstance(data, dict) else None
    key_file = data.get("evidence_key_file") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not repos or not all(isinstance(k, str) and REPO_RE.fullmatch(k) for k in repos):
        raise Refused(["allowlist must map at least one valid OWNER/NAME to a config"])
    if set(data) - {"repos", "evidence_key_file"}:
        raise Refused(["allowlist has an unknown top-level key"])
    if key_file is not None and (not isinstance(key_file, str) or not os.path.isabs(key_file) or "\0" in key_file):
        raise Refused(["allowlist evidence_key_file must be an absolute path string"])
    return {"repos": {k.lower(): v for k, v in repos.items()}, "evidence_key_file": key_file}


REPO_KEYS = {"required_checks", "reviewers", "require_server_side_protection", "accept_bypass_actors",
             "max_evidence_age_hours", "require_signed_evidence", "local_equivalent_ci",
             "independent_github_reviewers"}


def repo_config(allow, repo):
    """Validated per-repo config. Defaults: protection required, bypass actors not accepted, 24h evidence age,
    signed evidence required exactly when a key file is configured."""
    cfg = allow["repos"].get(repo.lower())
    if cfg is None:
        raise Refused(["repo is not on the allowlist"])
    key_file = allow["evidence_key_file"]
    ok = isinstance(cfg, dict) and set(cfg) <= REPO_KEYS
    for key in ("required_checks", "reviewers"):
        v = cfg.get(key) if ok else None
        ok = ok and isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.strip() and x == x.strip() for x in v)
    prot = cfg.get("require_server_side_protection", True) if ok else True
    bypass = cfg.get("accept_bypass_actors", False) if ok else False
    signed = cfg.get("require_signed_evidence", key_file is not None) if ok else True
    age = cfg.get("max_evidence_age_hours", 24) if ok else 24
    lci = cfg.get("local_equivalent_ci", False) if ok else False
    ind = cfg.get("independent_github_reviewers", []) if ok else []
    ok = ok and all(isinstance(x, bool) for x in (prot, bypass, signed, lci))
    ok = ok and isinstance(ind, list) and all(isinstance(x, str) and LOGIN_RE.fullmatch(x) for x in ind)
    ok = ok and isinstance(age, int) and not isinstance(age, bool) and 1 <= age <= MAX_EVIDENCE_AGE_HOURS
    ok = ok and not (signed and key_file is None)
    if not ok:
        raise Refused(["allowlist config for repo is invalid (needs non-empty required_checks and reviewers lists; "
                       "booleans must be booleans; max_evidence_age_hours 1-%d; require_signed_evidence needs an "
                       "evidence_key_file)" % MAX_EVIDENCE_AGE_HOURS])
    return {"required_checks": list(cfg["required_checks"]), "reviewers": list(cfg["reviewers"]),
            "require_server_side_protection": prot, "accept_bypass_actors": bypass,
            "max_evidence_age_hours": age, "require_signed_evidence": signed, "local_equivalent_ci": lci,
            "independent_github_reviewers": [x.lower() for x in ind]}


def canonical_evidence(ev):
    body = {k: v for k, v in ev.items() if k != "hmac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def evidence_mac(key, ev):
    return hmac.new(key, canonical_evidence(ev), hashlib.sha256).hexdigest()


def load_key(path):
    key = read_trusted_file(path, "evidence key file", 0o077).rstrip(b"\r\n \t")
    if len(key) < MIN_KEY_BYTES or len(key) > 4096:
        raise Refused(["evidence key file has an unusable length"])
    return key


def fresh_ts(value, label, cfg, now):
    """Reasons a timestamp is not acceptable now: malformed, in the future, or older than the repo's age limit."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        return [f"{label} is not YYYY-MM-DDTHH:MM:SSZ"]
    try:
        ts = calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return [f"{label} is not a valid time"]
    if ts > now + 60:
        return [f"{label} is in the future"]
    if now - ts > cfg["max_evidence_age_hours"] * 3600:
        return [f"{label} is older than max_evidence_age_hours"]
    return []


def check_local_ci(lc, head, cfg, now):
    """Validate the optional local_ci block of the review evidence (founder LOCAL_EQUIVALENT_CI exception).
    Returns {check name: tests}; raises Refused on any defect. It is pinned to the exact head and as fresh as
    the review itself, and it only ever claims LOCAL_EQUIVALENT_PASS, never CI_PASS."""
    bad = []
    if not isinstance(lc, dict) or set(lc) != LOCAL_CI_KEYS:
        raise Refused(["local_ci evidence is malformed"])
    if lc["classification"] != "LOCAL_EQUIVALENT_PASS":
        bad.append("local_ci classification is not LOCAL_EQUIVALENT_PASS")
    if lc["head"] != head:
        bad.append("local_ci head does not equal the expected head")
    bad += fresh_ts(lc["run_at"], "local_ci run_at", cfg, now)
    out = {}
    entries = lc["checks"]
    if not isinstance(entries, list) or not entries:
        raise Refused(bad + ["local_ci checks missing or empty"])
    for e in entries:
        ok = isinstance(e, dict) and set(e) == LOCAL_CI_CHECK_KEYS and isinstance(e["name"], str) and e["name"]
        ok = ok and all(isinstance(e[k], int) and not isinstance(e[k], bool) for k in ("exit_code", "tests"))
        if not ok:
            raise Refused(bad + ["local_ci check entry is malformed"])
        if e["name"] in out:
            bad.append("local_ci lists a check name twice")
        if e["exit_code"] != 0 or e["tests"] < 0:
            bad.append(f"local_ci check {e['name']} did not pass (exit_code must be 0)")
        out[e["name"]] = e["tests"]
    if bad:
        raise Refused(bad)
    return out


def load_evidence(path, head, repo, pr, base, cfg, key, now):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # a FIFO or device must not hang the open or the read
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Refused(["review evidence is not a regular file"])
            with os.fdopen(fd, "rb") as f:
                fd = None
                raw = f.read(65537)
        finally:
            if fd is not None:
                os.close(fd)
        if len(raw) > 65536:
            raise Refused(["evidence file too large"])
        ev = json.loads(raw.decode("utf-8"))
    except Refused:
        raise
    except (OSError, ValueError):
        raise Refused(["review evidence missing or unparseable"])
    if not isinstance(ev, dict):
        raise Refused(["review evidence is not a JSON object"])
    bad = []
    for k in ("repo", "base", "head", "verdict", "reviewer", "reviewed_at", "reviewer_kind"):
        if not isinstance(ev.get(k), str) or not ev.get(k):
            bad.append(f"evidence field {k} missing or not a string")
    pr_v = ev.get("pr")
    if isinstance(pr_v, bool) or not isinstance(pr_v, (int, str)) or str(pr_v) != pr:
        bad.append("evidence pr missing or mismatched")
    if bad:
        raise Refused(bad)
    if ev["repo"].lower() != repo.lower():
        bad.append("evidence repo mismatch")
    if ev["base"] != base:
        bad.append("evidence base branch does not equal the PR's base branch")
    if ev["head"] != head:
        bad.append("evidence head does not equal the expected head")
    if ev["verdict"] != "PASS":
        bad.append("evidence verdict is not PASS")
    if ev["reviewer"] not in cfg["reviewers"]:
        bad.append("evidence reviewer is not in the allowlisted reviewers for this repo")
    if ev["reviewer_kind"] not in REVIEWER_KINDS:
        bad.append("evidence reviewer_kind is not recognised")
    bad += fresh_ts(ev["reviewed_at"], "evidence reviewed_at", cfg, now)
    if cfg["require_signed_evidence"]:
        mac = ev.get("hmac")
        if key is None or not isinstance(mac, str) or not MAC_RE.fullmatch(mac) \
                or not hmac.compare_digest(mac, evidence_mac(key, ev)):
            bad.append("evidence signature missing or invalid")
    local = check_local_ci(ev["local_ci"], head, cfg, now) if cfg["local_equivalent_ci"] and "local_ci" in ev else None
    if bad:
        raise Refused(bad)
    return hashlib.sha256(raw).hexdigest(), local


def require(p, key, kind):
    if key not in p or not isinstance(p[key], kind) or (kind is str and not p[key]):
        raise Refused([f"PR field {key} missing or malformed"])
    return p[key]


def check_entry(c):
    """Return (name, verdict) for a well-formed rollup entry; raise Refused for a malformed one."""
    if not isinstance(c, dict):
        raise Refused(["malformed check entry"])
    if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
        name, verdict = c.get("context"), c.get("state")
    elif c.get("__typename") == "CheckRun" or "status" in c:
        name, status = c.get("name"), c.get("status")
        if not isinstance(status, str) or not status:
            raise Refused(["malformed check entry: no status"])
        verdict = c.get("conclusion") if status == "COMPLETED" else status
    else:
        raise Refused(["malformed check entry: unrecognised shape"])
    if not isinstance(name, str) or not name or not isinstance(verdict, str) or not verdict:
        raise Refused(["malformed check entry: missing name or conclusion/state"])
    return name, verdict


def summarize_checks(rollup, required, exempt=frozenset()):
    """Every named required check must be present and SUCCESS; every other entry must also be SUCCESS.
    A name in `exempt` (verified never executed, covered by local evidence) may have non-SUCCESS entries."""
    seen, bad = {}, []
    for c in rollup:
        name, verdict = check_entry(c)
        seen.setdefault(name, []).append(verdict)
    for name in required:
        if name not in seen:
            bad.append(f"required check {name} is missing")
    for name, verdicts in seen.items():
        for v in verdicts:
            if v != "SUCCESS" and name not in exempt:
                bad.append(f"check {name} is {v}")
    return {"total": len(rollup), "required": len(required),
            "success": sum(v == "SUCCESS" for vs in seen.values() for v in vs), "not_success": len(bad)}, bad


def parse_rule_contexts(rules):
    """(context names, ruleset ids) from required_status_checks rules; empty or malformed definitions refuse."""
    names, ids = set(), set()
    for r in rules:
        if not isinstance(r, dict):
            raise Refused(["server-side rules response is malformed"])
        if r.get("type") != "required_status_checks":
            continue
        params = r.get("parameters")
        lst = params.get("required_status_checks") if isinstance(params, dict) else None
        rid = r.get("ruleset_id")
        if not isinstance(lst, list) or not lst or isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0 \
                or not all(isinstance(c, dict) and isinstance(c.get("context"), str) and c["context"] for c in lst):
            raise Refused(["server-side required_status_checks rule is empty or malformed"])
        names.update(c["context"] for c in lst)
        ids.add(rid)
    return names, ids


def parse_protection_contexts(prot):
    if not isinstance(prot, dict):
        raise Refused(["branch protection response is malformed"])
    rsc = prot.get("required_status_checks")
    if rsc is None:
        return set()
    ctx, checks = (rsc.get("contexts", []), rsc.get("checks", [])) if isinstance(rsc, dict) else (None, None)
    if not isinstance(ctx, list) or not isinstance(checks, list) \
            or not all(isinstance(x, str) and x for x in ctx) \
            or not all(isinstance(c, dict) and isinstance(c.get("context"), str) and c["context"] for c in checks):
        raise Refused(["branch protection required_status_checks is malformed"])
    names = set(ctx) | {c["context"] for c in checks}
    if not names:
        raise Refused(["branch protection required_status_checks is empty"])
    return names


def _read_json(runner, argv, what):
    rc, out, err = runner.call(argv)
    if rc != 0:
        return rc, None, err
    try:
        return 0, json.loads(out), err
    except ValueError:
        raise RuntimeError(f"{what} returned unparseable JSON")


def http_status(err):
    """The first `HTTP <token>` in gh's error, only if the whole token is exactly three digits."""
    m = HTTP_TOKEN_RE.search(err)
    return m.group(1) if m and STATUS_RE.fullmatch(m.group(1)) else None


def check_server_protection(runner, repo, base, cfg):
    """Require a non-empty server-side set of required status checks that covers every allowlisted
    required check, and refuse when a readable ruleset lists a bypass actor that could apply to us."""
    rc, rules, err = _read_json(runner, rules_argv(repo, base), "gh rules read")
    if rc != 0:
        raise RuntimeError(gh_error(err, "rules-read"))
    if not isinstance(rules, list):
        raise RuntimeError("gh rules read returned unexpected JSON shape")
    names, ids = parse_rule_contexts(rules)
    rc, prot, err = _read_json(runner, protection_argv(repo, base), "gh branch protection read")
    admins, waivers = "n/a", []
    if rc == 0:
        names |= parse_protection_contexts(prot)
        prot_state = "read"
        ea = prot.get("enforce_admins")
        admins = isinstance(ea, dict) and ea.get("enabled") is True
        if not admins and not cfg["accept_bypass_actors"]:
            raise Refused(["branch protection does not enforce its rules for administrators (enforce_admins); "
                           "set accept_bypass_actors to accept"])
        if not admins:
            admins = "false-accepted"
            waivers.append("enforce_admins_false")
    elif http_status(err) == "404":
        prot_state = "absent"  # only a real HTTP 404 status (the first status token) means no classic protection
    else:
        prot_state = "unreadable"
    if not names:
        if prot_state == "unreadable":
            raise RuntimeError(gh_error(err, "protection-read"))
        raise Refused(["no server-side required status checks on the base branch"])
    if prot_state == "unreadable" and not cfg["accept_bypass_actors"]:
        raise Refused(["branch protection could not be read, so enforce_admins is unverified; "
                       "set accept_bypass_actors to accept"])
    if prot_state == "unreadable":
        admins = "unverified-accepted"
        waivers.append("protection_unreadable")
    missing = [n for n in cfg["required_checks"] if n not in names]
    if missing:
        raise Refused([f"required check {n} is not required server-side" for n in missing])
    risky, unreadable = [], False
    for rid in sorted(ids):
        rc, detail, _ = _read_json(runner, ruleset_argv(repo, rid), "gh ruleset read")
        actors = detail.get("bypass_actors") if isinstance(detail, dict) else None
        if rc != 0 or not isinstance(actors, list) or not all(isinstance(a, dict) for a in actors):
            unreadable = True
            continue
        risky += [str(a.get("actor_type")) for a in actors]  # any listed actor is a bypass; only [] is none
    if unreadable and not cfg["accept_bypass_actors"]:
        raise Refused(["ruleset bypass actors are unreadable or malformed; set accept_bypass_actors to accept"])
    if risky and not cfg["accept_bypass_actors"]:
        raise Refused([f"ruleset allows bypass by {sorted(set(risky))}; set accept_bypass_actors to accept"])
    if risky:
        waivers.append("bypass_actors_present:" + ",".join(sorted(set(risky))))
    if unreadable:
        waivers.append("bypass_unreadable")
    # `bypass` is "none" only when no waiver of any kind was exercised; the waivers list names each one.
    bypass = "none"
    if waivers:
        bypass = "accepted" if risky else ("unreadable-accepted" if unreadable else "protection-waiver-accepted")
    return {"contexts": sorted(names), "protection": prot_state, "enforce_admins": admins,
            "bypass": bypass, "waivers": waivers}


def job_never_ran(runner, repo, job_id, name, head):
    """True only when GitHub reports a completed job for this exact head and name with no steps and no runner."""
    rc, out, err = runner.call(["api", f"repos/{repo}/actions/jobs/{job_id}"])
    if rc != 0:
        raise RuntimeError(gh_error(err, "job-read"))
    try:
        job = json.loads(out)
    except ValueError:
        raise RuntimeError("gh returned unparseable JSON")
    if not isinstance(job, dict):
        raise RuntimeError("gh returned unexpected JSON shape")
    return (job.get("name") == name and job.get("head_sha") == head and job.get("status") == "completed"
            and job.get("steps") == [] and job.get("runner_id") in (None, 0) and not job.get("runner_name"))


def only_actions_runs(runner, repo, head, name):
    """Every check run of this name at this head belongs to the GitHub Actions app. A checks-writing integration
    could otherwise publish an executed failure under the same name and details URL as a never-started job."""
    path = f"repos/{repo}/commits/{head}/check-runs?check_name={urllib.parse.quote(name, safe='')}&per_page=100"
    rc, out, err = runner.call(["api", path])
    if rc != 0:
        raise RuntimeError(gh_error(err, "check-runs-read"))
    try:
        data = json.loads(out)
    except ValueError:
        raise RuntimeError("gh returned unparseable JSON")
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list) or data.get("total_count") != len(runs) or not runs or len(runs) > 100:
        return False
    return all(isinstance(r, dict) and r.get("name") == name and r.get("head_sha") == head
               and isinstance(r.get("app"), dict) and r["app"].get("slug") == "github-actions" for r in runs)


def unavailable_checks(runner, repo, facts, head, cfg, local):
    """Required checks the LOCAL_EQUIVALENT_CI exception may stand in for: every non-SUCCESS entry of the check
    is a hosted CheckRun that GitHub itself reports as never started (CI_UNAVAILABLE_EXTERNAL), and the local
    evidence lists the check as passed at this head. A check that executed and failed is CI_FAILED and is never
    exempted; a required check missing from the rollup is never exempted."""
    if local is None or not cfg["local_equivalent_ci"]:
        return frozenset()
    rollup = facts.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return frozenset()
    exempt = set()
    for name in cfg["required_checks"]:
        if name not in local:
            continue
        bad = []
        for c in rollup:
            n, verdict = check_entry(c)
            if n == name and verdict != "SUCCESS":
                bad.append((c, verdict))
        if not bad:
            continue
        ok = only_actions_runs(runner, repo, head, name)
        for c, verdict in bad:
            m = JOB_URL_RE.fullmatch(c.get("detailsUrl") or "") if c.get("__typename") == "CheckRun" else None
            ok = ok and verdict in NOT_EXECUTED_VERDICTS and m is not None and m.group(1).lower() == repo.lower() \
                and job_never_ran(runner, repo, m.group(2), name, head)
        if ok:
            exempt.add(name)
    return frozenset(exempt)


def decide(p, repo, pr, head, me, cfg, exempt=frozenset()):
    """All guards against one PR fact set. Returns (pr_class, checks_summary); raises Refused."""
    bad = []
    state = require(p, "state", str)
    draft = require(p, "isDraft", bool)
    observed = require(p, "headRefOid", str)
    author = require(p, "author", dict).get("login")
    mergeable = require(p, "mergeable", str)
    mss = require(p, "mergeStateStatus", str)
    base = require(p, "baseRefName", str)
    if not BRANCH_RE.fullmatch(base) or ".." in base:
        raise Refused(["PR field baseRefName missing or malformed"])
    branch = require(p, "headRefName", str)
    cross = require(p, "isCrossRepository", bool)
    reviews = require(p, "reviews", list)
    rollup = require(p, "statusCheckRollup", list)
    require(p, "files", list)
    decision = p.get("reviewDecision")
    if "reviewDecision" not in p or not (decision is None or isinstance(decision, str)):
        raise Refused(["PR field reviewDecision missing or malformed"])
    if not isinstance(author, str) or not LOGIN_RE.fullmatch(author):
        raise Refused(["PR author missing or malformed"])
    if state != "OPEN":
        bad.append(f"PR is {state}, not OPEN")
    if draft:
        bad.append("PR is a draft")
    if observed != head:
        bad.append("observed head differs from expected head")
    if mergeable != "MERGEABLE":
        bad.append(f"mergeable is {mergeable}")
    if mss != "CLEAN":
        bad.append(f"mergeStateStatus is {mss}")
    latest = {}
    for r in reviews:
        if not isinstance(r, dict) or not isinstance(r.get("author"), dict) or not isinstance(r.get("state"), str) \
                or not isinstance(r["author"].get("login"), str) \
                or not (r.get("commit") is None or isinstance(r.get("commit"), dict)):
            raise Refused(["review entry malformed"])
        who, st = r["author"]["login"], r["state"]
        if st in DECISIVE:
            latest[who.lower()] = (st, (r.get("commit") or {}).get("oid"), r.get("authorAssociation"))
    if decision == "CHANGES_REQUESTED" or any(v[0] == "CHANGES_REQUESTED" for v in latest.values()):
        bad.append("unresolved CHANGES_REQUESTED")
    checks, check_bad = summarize_checks(rollup, cfg["required_checks"], exempt)
    bad += check_bad
    a, me_l = author.lower(), me.lower()
    if a == me_l:
        pr_class = "A"
    elif norm_bot(author) in BOT_LOGINS:
        pr_class = "B"
        if not BOT_BRANCH.match(branch) or cross:
            bad.append("bot PR lacks verified bot branch provenance")
        if latest.get(me_l, (None, None))[:2] != ("APPROVED", head):
            bad.append("class B needs an APPROVED review by the authenticated user at the expected head")
    else:
        pr_class = "C"
        mine = latest.get(me_l, (None, None))[:2] == ("APPROVED", head)
        trusted = any(who != a and st == "APPROVED" and oid == head and assoc in TRUSTED_ASSOC
                      for who, (st, oid, assoc) in latest.items())
        if cross and not mine:
            bad.append("class C cross-repository PR needs an APPROVED review by the authenticated user at the expected head")
        elif not trusted:
            bad.append("class C needs an APPROVED review at the expected head by a non-author OWNER, MEMBER or COLLABORATOR")
    ind = set(cfg["independent_github_reviewers"])
    if ind and not any(who in ind and who not in (a, me_l) and st == "APPROVED" and oid == head and assoc in TRUSTED_ASSOC
                       for who, (st, oid, assoc) in latest.items()):
        bad.append("needs a GitHub APPROVED review at the expected head from an allowlisted independent reviewer "
                   "who is neither the PR author nor the authenticated user")
    if bad:
        raise Refused(bad)
    return pr_class, checks


def evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock, pin=None):
    """One complete read-and-decide pass. The base branch comes from the first PR read of the first pass and is
    pinned: every later PR read, the protection reads, the evidence and the read-back must agree with it, and the
    evidence file must be byte-identical across passes. Returns (head, class, checks, server-side info, pin)."""
    facts = runner.json(pr_view_argv(repo, pr, PR_FIELDS))
    observed = facts.get("headRefOid") if isinstance(facts.get("headRefOid"), str) else None
    base = facts.get("baseRefName") if isinstance(facts.get("baseRefName"), str) else ""
    if pin is not None and base != pin["base"]:
        raise Refused(["base branch changed since the first read"])
    # With the exception enabled, check verdicts can only be judged after the local evidence is read, so every
    # other fact is validated first; a required check is never skipped by this pre-pass in the real decision below.
    decide(facts, repo, pr, head, me, cfg, frozenset(cfg["required_checks"]) if cfg["local_equivalent_ci"] else frozenset())
    digest, local = load_evidence(evidence, head, repo, pr, base, cfg, key, clock())
    exempt = unavailable_checks(runner, repo, facts, head, cfg, local)
    pr_class, checks = decide(facts, repo, pr, head, me, cfg, exempt)
    if pin is not None and digest != pin["evidence"]:
        raise Refused(["evidence file changed since the first read"])
    info = {"waived": True}
    if cfg["require_server_side_protection"]:
        info = check_server_protection(runner, repo, base, cfg)  # for the pinned base
    after = runner.json(pr_view_argv(repo, pr, PR_FIELDS))
    if after.get("baseRefName") != base or after.get("headRefOid") != head:
        raise Refused(["base branch or head changed while server-side state was being read"])
    exempt_after = unavailable_checks(runner, repo, after, head, cfg, local)
    if exempt_after != exempt:
        raise Refused(["required-check availability changed while server-side state was being read"])
    final_class, _ = decide(after, repo, pr, head, me, cfg, exempt_after)  # every blocking fact in the last read refuses
    if final_class != pr_class:
        raise Refused(["PR class changed while server-side state was being read"])
    checks = dict(checks, ci_class="CI_UNAVAILABLE_EXTERNAL" if exempt else "CI_PASS",
                  local_equivalent=sorted(exempt))  # an exempted check is never reported as CI_PASS
    return observed, pr_class, checks, info, {"base": base, "evidence": digest}


class AuditError(OSError):
    pass


def write_all(fd, data):
    view, total = memoryview(data), 0
    while total < len(data):
        n = os.write(fd, view[total:])
        if n <= 0:
            raise AuditError("audit write made no progress")
        total += n
    if total != len(data):
        raise AuditError("audit write length mismatch")


def write_audit(path, record):
    """Append one complete JSONL line, durably. Every failure raises AuditError (an OSError)."""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        bad = dir_chain_problems(d)
        if bad:
            raise AuditError("audit directory untrusted: " + "; ".join(bad))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except AuditError:
        raise
    except OSError as e:
        raise AuditError(f"audit open failed: {scrub(e)}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600:
            raise AuditError("audit file is not a regular, user-owned, single-link 0600 file")
        write_all(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    except AuditError:
        raise
    except OSError as e:
        raise AuditError(f"audit write failed: {scrub(e)}")
    finally:
        os.close(fd)


def run(repo, pr, head, evidence, allowlist, audit, dry_run, gh, clock=time.time):
    """Execute the full guarded flow. Returns (exit_code, record). `gh` is `gh(argv) -> (rc, out, err)`."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())), "repo": scrub(repo), "pr": scrub(pr),
           "expected_head": scrub(head), "observed_head": None, "checks": None, "class": None,
           "decision": "REFUSE", "reasons": [], "outcome": "REFUSED", "dry_run": bool(dry_run)}
    code = EXIT_REFUSED
    try:
        validate_args(repo, pr, head)
        runner = Runner(gh)
        rc, out, err = runner.call(["api", "user"])
        try:
            me = json.loads(out).get("login") if rc == 0 else None
        except (ValueError, AttributeError):
            me = None
        if not isinstance(me, str) or not LOGIN_RE.fullmatch(me):
            raise RuntimeError("authenticated gh user unavailable")
        allow = load_allowlist(allowlist)
        cfg = repo_config(allow, repo)
        key = load_key(allow["evidence_key_file"]) if cfg["require_signed_evidence"] else None
        rec["signed_evidence"] = cfg["require_signed_evidence"]
        rec["observed_head"], rec["class"], rec["checks"], rec["server_side"], pin = \
            evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock)
        rec["base"] = pin["base"]
        rec["decision"] = "APPROVE"
        # Full re-evaluation immediately before the one mutating call; any change refuses.
        try:
            observed2, class2, checks2, rec["server_side"], _ = evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock, pin)
        except Refused as e:
            raise Refused(["changed before execution: " + x for x in e.reasons])
        if checks2 != rec["checks"]:
            raise Refused(["changed before execution: check results or CI classification differ"])
        if observed2 != head or class2 != rec["class"]:
            rec["observed_head"] = observed2
            raise Refused(["changed before execution: head or PR class differs"])
        if dry_run:
            rec["outcome"], code = "DRY_RUN", EXIT_OK
        else:
            rec["outcome"] = "PENDING"
            try:
                write_audit(audit, dict(rec))  # durable before exec; no audit trail, no merge
            except OSError as e:
                raise Refused(["audit record could not be written, refusing before execution: " + scrub(e)])
            rec["merge_attempted"] = True
            mrc, _, merr = runner.call(merge_argv(repo, pr, head))
            if mrc != 0:
                rec["reasons"].append(gh_error(merr, "merge"))
                rec["outcome"], code = ("DENIED", EXIT_DENIED) if DENIAL_RE.search(merr) else ("ERROR", EXIT_ERROR)
            else:
                try:
                    back = runner.json(pr_view_argv(repo, pr, READBACK_FIELDS))
                    mc = (back.get("mergeCommit") or {}).get("oid") if isinstance(back.get("mergeCommit"), dict) else None
                except RuntimeError:
                    back, mc = {}, None
                merged = back.get("state") == "MERGED" and isinstance(mc, str) and SHA_RE.fullmatch(mc)
                if merged and back.get("baseRefName") == pin["base"]:
                    rec["outcome"], code = "MERGED", EXIT_OK
                elif merged:
                    rec["incident"] = INCIDENT
                    rec["reasons"].append(f"{INCIDENT}: read-back base differs from the reviewed base")
                    rec["outcome"], code = "UNVERIFIED", EXIT_BASE_MISMATCH
                    sys.stderr.write(f"INCIDENT: {INCIDENT}: PR {scrub(repo)}#{scrub(pr)} is MERGED but its base is not "
                                     f"the reviewed base {scrub(pin['base'])}\n")
                else:
                    rec["reasons"].append("read-back did not show MERGED with a merge commit")
                    rec["outcome"], code = "UNVERIFIED", EXIT_UNVERIFIED
    except Refused as e:
        rec["reasons"] += [scrub(x) for x in e.reasons]
        if rec["decision"] == "APPROVE":
            rec["decision"] = "REFUSE"
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        rec["decision"] = "ERROR" if rec["decision"] != "APPROVE" else "APPROVE"
        rec["reasons"].append(scrub(e))
        rec["outcome"], code = "ERROR", EXIT_ERROR
    except Exception as e:  # fail closed: any unexpected error is audited and exits distinctly, never a traceback
        rec["reasons"].append(f"unexpected {type(e).__name__}: " + scrub(e))
        if rec.get("merge_attempted"):
            rec["reasons"].append("the merge command had been started: the PR may already be merged")
        rec["outcome"], code = "ERROR", EXIT_INTERNAL
    try:
        write_audit(audit, rec)
    except OSError as e:
        rec["reasons"].append("final audit record not written")
        sys.stderr.write(f"error: final audit record for outcome {rec['outcome']} was not written: {scrub(e)}\n")
        code = EXIT_AUDIT
    return code, rec


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def main(argv=None, gh=None, allowlist=None, audit=None, clock=time.time):
    ap = _Parser(description="Guarded owner merge runner (merge commit only).", allow_abbrev=False)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--expect-head", required=True)
    ap.add_argument("--review-evidence", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if gh is None:
        try:
            gh = locate_gh()
        except RuntimeError as e:
            sys.stderr.write(f"error: {e}\n")
            return EXIT_ERROR
    code, rec = run(a.repo, a.pr, a.expect_head, a.review_evidence,
                    allowlist or DEFAULT_ALLOWLIST, audit or DEFAULT_AUDIT, a.dry_run, gh, clock=clock)
    print(json.dumps({k: rec[k] for k in ("repo", "pr", "class", "decision", "outcome", "reasons")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
