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
(the outcome is on stderr), 64 usage error.
"""
import argparse
import calendar
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

EXIT_OK, EXIT_REFUSED, EXIT_DENIED, EXIT_ERROR, EXIT_UNVERIFIED, EXIT_AUDIT, EXIT_USAGE = 0, 2, 3, 4, 5, 6, 64

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}")
PR_RE = re.compile(r"[1-9][0-9]{0,8}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")
READ_PATHS = (re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/rules/branches/([A-Za-z0-9._/-]{1,200})"),
              re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/branches/([A-Za-z0-9._/-]{1,200})/protection"),
              re.compile(r"repos/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/rulesets/([1-9][0-9]{0,11})"))
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
REVIEWER_KINDS = {"human", "independent-agent"}
RISKY_BYPASS = {"OrganizationAdmin", "RepositoryRole", "Team"}
MAX_EVIDENCE_AGE_HOURS = 168
MIN_KEY_BYTES = 32
LOGIN_RE = re.compile(r"(app/)?[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

PR_FIELDS = ("state,isDraft,author,baseRefName,headRefName,headRefOid,isCrossRepository,mergeable,"
             "mergeStateStatus,reviewDecision,reviews,statusCheckRollup,files")
READBACK_FIELDS = "state,mergeCommit"

BOT_LOGINS = {"dependabot", "renovate"}
BOT_BRANCH = re.compile(r"(dependabot|renovate)/")
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|Bearer\s+\S+|"
                       r"(?i:token|password|secret|authorization)\s*[=:]\s*\S+)")
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
            raise RuntimeError(f"gh {argv[0]} {argv[1]} failed: {scrub(err)}")
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
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
             "max_evidence_age_hours", "require_signed_evidence"}


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
    ok = ok and all(isinstance(x, bool) for x in (prot, bypass, signed))
    ok = ok and isinstance(age, int) and not isinstance(age, bool) and 1 <= age <= MAX_EVIDENCE_AGE_HOURS
    ok = ok and not (signed and key_file is None)
    if not ok:
        raise Refused(["allowlist config for repo is invalid (needs non-empty required_checks and reviewers lists; "
                       "booleans must be booleans; max_evidence_age_hours 1-%d; require_signed_evidence needs an "
                       "evidence_key_file)" % MAX_EVIDENCE_AGE_HOURS])
    return {"required_checks": list(cfg["required_checks"]), "reviewers": list(cfg["reviewers"]),
            "require_server_side_protection": prot, "accept_bypass_actors": bypass,
            "max_evidence_age_hours": age, "require_signed_evidence": signed}


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


def load_evidence(path, head, repo, pr, base, cfg, key, now):
    try:
        if os.path.getsize(path) > 65536:
            raise Refused(["evidence file too large"])
        with open(path, "r", encoding="utf-8") as f:
            ev = json.load(f)
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
    if not TS_RE.fullmatch(ev["reviewed_at"]):
        bad.append("evidence reviewed_at is not YYYY-MM-DDTHH:MM:SSZ")
    else:
        try:
            ts = calendar.timegm(time.strptime(ev["reviewed_at"], "%Y-%m-%dT%H:%M:%SZ"))
            if ts > now + 60:
                bad.append("evidence reviewed_at is in the future")
            elif now - ts > cfg["max_evidence_age_hours"] * 3600:
                bad.append("evidence is older than max_evidence_age_hours")
        except ValueError:
            bad.append("evidence reviewed_at is not a valid time")
    if cfg["require_signed_evidence"]:
        mac = ev.get("hmac")
        if key is None or not isinstance(mac, str) or not hmac.compare_digest(mac, evidence_mac(key, ev)):
            bad.append("evidence signature missing or invalid")
    if bad:
        raise Refused(bad)


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


def summarize_checks(rollup, required):
    """Every named required check must be present and SUCCESS; every other entry must also be SUCCESS."""
    seen, bad = {}, []
    for c in rollup:
        name, verdict = check_entry(c)
        seen.setdefault(name, []).append(verdict)
    for name in required:
        if name not in seen:
            bad.append(f"required check {name} is missing")
    for name, verdicts in seen.items():
        for v in verdicts:
            if v != "SUCCESS":
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


def check_server_protection(runner, repo, base, cfg):
    """Require a non-empty server-side set of required status checks that covers every allowlisted
    required check, and refuse when a readable ruleset lists a bypass actor that could apply to us."""
    rc, rules, err = _read_json(runner, rules_argv(repo, base), "gh rules read")
    if rc != 0:
        raise RuntimeError(f"gh rules read failed: {scrub(err)}")
    if not isinstance(rules, list):
        raise RuntimeError("gh rules read returned unexpected JSON shape")
    names, ids = parse_rule_contexts(rules)
    rc, prot, err = _read_json(runner, protection_argv(repo, base), "gh branch protection read")
    if rc == 0:
        names |= parse_protection_contexts(prot)
        prot_state = "read"
    elif re.search(r"404|Not Found", err):
        prot_state = "absent"
    else:
        prot_state = "unreadable"
    if not names:
        if prot_state == "unreadable":
            raise RuntimeError(f"gh branch protection read failed: {scrub(err)}")
        raise Refused(["no server-side required status checks on the base branch"])
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
        risky += [a.get("actor_type") for a in actors if a.get("actor_type") in RISKY_BYPASS]
    if risky and not cfg["accept_bypass_actors"]:
        raise Refused([f"ruleset allows bypass by {sorted(set(risky))}; set accept_bypass_actors to accept"])
    return {"contexts": sorted(names), "protection": prot_state,
            "bypass": "accepted" if risky else ("unreadable" if unreadable else "none")}


def decide(p, repo, pr, head, me, cfg):
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
        who = ((r or {}).get("author") or {}).get("login")
        st = (r or {}).get("state")
        if not isinstance(who, str) or not isinstance(st, str):
            raise Refused(["review entry malformed"])
        if st in DECISIVE:
            latest[who.lower()] = (st, ((r.get("commit") or {}).get("oid")), r.get("authorAssociation"))
    if decision == "CHANGES_REQUESTED" or any(v[0] == "CHANGES_REQUESTED" for v in latest.values()):
        bad.append("unresolved CHANGES_REQUESTED")
    checks, check_bad = summarize_checks(rollup, cfg["required_checks"])
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
    if bad:
        raise Refused(bad)
    return pr_class, checks


def evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock):
    """One complete read-and-decide pass: fresh PR facts, evidence (bound to head and base), every guard,
    server-side protection. Returns (observed head, class, checks, server-side info)."""
    facts = runner.json(pr_view_argv(repo, pr, PR_FIELDS))
    observed = facts.get("headRefOid") if isinstance(facts.get("headRefOid"), str) else None
    base = facts.get("baseRefName") if isinstance(facts.get("baseRefName"), str) else ""
    pr_class, checks = decide(facts, repo, pr, head, me, cfg)
    load_evidence(evidence, head, repo, pr, base, cfg, key, clock())
    info = {"waived": True}
    if cfg["require_server_side_protection"]:
        info = check_server_protection(runner, repo, base, cfg)
    return observed, pr_class, checks, info


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
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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
        rec["observed_head"], rec["class"], rec["checks"], rec["server_side"] = \
            evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock)
        rec["decision"] = "APPROVE"
        # Full re-evaluation immediately before the one mutating call; any change refuses.
        try:
            observed2, class2, _, rec["server_side"] = evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock)
        except Refused as e:
            raise Refused(["changed before execution: " + x for x in e.reasons])
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
            mrc, _, merr = runner.call(merge_argv(repo, pr, head))
            if mrc != 0:
                rec["reasons"].append(scrub(merr))
                rec["outcome"], code = ("DENIED", EXIT_DENIED) if DENIAL_RE.search(merr) else ("ERROR", EXIT_ERROR)
            else:
                try:
                    back = runner.json(pr_view_argv(repo, pr, READBACK_FIELDS))
                    mc = (back.get("mergeCommit") or {}).get("oid") if isinstance(back.get("mergeCommit"), dict) else None
                except RuntimeError:
                    back, mc = {}, None
                if back.get("state") == "MERGED" and isinstance(mc, str) and SHA_RE.fullmatch(mc):
                    rec["outcome"], code = "MERGED", EXIT_OK
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
