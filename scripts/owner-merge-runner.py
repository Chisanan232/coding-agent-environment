#!/usr/bin/env python3
"""Structurally validated owner pull-request merge runner (SPE-118, Option A).

  owner-merge-runner.py --repo OWNER/NAME --pr N --expect-head SHA40 --review-evidence PATH
                        [--allowlist PATH] [--audit-log PATH] [--dry-run]

Structured arguments only. Every input is validated by a strict regex before use, gh is started with
fixed argv lists (shell=False), and the single mutating call is exactly:

  gh pr merge N --repo OWNER/NAME --merge --match-head-commit SHA

Never --admin, --squash, --rebase, --auto or --delete-branch. A refusal, denial or error is recorded and
the runner exits nonzero; it never retries, never tries another route, and never touches settings.
The review evidence file is produced by an independent review; the runner only reads it.

Exit codes: 0 success (or validated dry run), 2 refused by a guard, 3 denied by gh, 4 error,
5 unverified (merge ran but the read-back did not show MERGED), 64 usage error.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

EXIT_OK, EXIT_REFUSED, EXIT_DENIED, EXIT_ERROR, EXIT_UNVERIFIED, EXIT_USAGE = 0, 2, 3, 4, 5, 64

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}")
PR_RE = re.compile(r"[1-9][0-9]{0,8}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
LOGIN_RE = re.compile(r"(app/)?[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

PR_FIELDS = ("state,isDraft,author,baseRefName,headRefName,headRefOid,isCrossRepository,mergeable,"
             "mergeStateStatus,reviewDecision,reviews,statusCheckRollup,files")
READBACK_FIELDS = "state,mergeCommit"
HEAD_FIELDS = "state,headRefOid"

BOT_LOGINS = {"dependabot", "renovate"}
BOT_BRANCH = re.compile(r"(dependabot|renovate)/")
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|Bearer\s+\S+|"
                       r"(?i:token|password|secret|authorization)\s*[=:]\s*\S+)")
DENIAL_RE = re.compile(r"permission|denied|forbidden|not authori[sz]ed|HTTP 40[13]|must have|"
                       r"protected branch|required status|review is required|not allowed", re.I)
DEFAULT_AUDIT = os.path.join(os.path.expanduser("~"), ".claude", "audit", "owner-merge-runner.jsonl")
DEFAULT_ALLOWLIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "docs", "owner-merge-runner-allowlist.json")


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


def check_argv(argv):
    """Fail closed: only the fixed read shapes and the single fixed merge shape may reach gh."""
    a = list(argv)
    if a == ["api", "user"]:
        return "user"
    if len(a) == 7 and a[:2] == ["pr", "view"] and a[3] == "--repo" and a[5] == "--json" \
            and a[6] in (PR_FIELDS, READBACK_FIELDS, HEAD_FIELDS) \
            and PR_RE.fullmatch(a[2]) and REPO_RE.fullmatch(a[4]):
        return "view"
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


def load_allowlist(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise Refused(["allowlist file missing or unparseable"])
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list) or not repos or not all(isinstance(x, str) and REPO_RE.fullmatch(x) for x in repos):
        raise Refused(["allowlist must list at least one valid OWNER/NAME"])
    return {x.lower() for x in repos}


def load_evidence(path, head, repo, pr):
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
    if ev.get("head") != head:
        bad.append("evidence head does not equal the expected head")
    if ev.get("verdict") != "PASS":
        bad.append("evidence verdict is not PASS")
    if "repo" in ev and str(ev["repo"]).lower() != repo.lower():
        bad.append("evidence repo mismatch")
    if "pr" in ev and str(ev["pr"]) != pr:
        bad.append("evidence pr mismatch")
    if bad:
        raise Refused(bad)


def require(p, key, kind):
    if key not in p or not isinstance(p[key], kind):
        raise Refused([f"PR field {key} missing or malformed"])
    return p[key]


def summarize_checks(rollup):
    """Return (summary, failing reasons). Every entry must be a completed SUCCESS; none missing."""
    if not rollup:
        return {"total": 0}, ["no status checks reported"]
    bad, ok = [], 0
    for c in rollup:
        if not isinstance(c, dict):
            bad.append("malformed check entry")
            continue
        name = str(c.get("name") or c.get("context") or "?")
        if c.get("__typename") == "StatusContext" or "state" in c and "conclusion" not in c:
            verdict = c.get("state")
        else:
            verdict = c.get("conclusion") if c.get("status") == "COMPLETED" else (c.get("status") or "PENDING")
        if verdict == "SUCCESS":
            ok += 1
        else:
            bad.append(f"check {name} is {verdict or 'PENDING'}")
    return {"total": len(rollup), "success": ok, "not_success": len(bad)}, bad


def decide(p, repo, pr, head, me, allow):
    """All guards against one PR fact set. Returns (pr_class, checks_summary); raises Refused."""
    bad = []
    if repo.lower() not in allow:
        bad.append("repo is not on the allowlist")
    state = require(p, "state", str)
    draft = require(p, "isDraft", bool)
    observed = require(p, "headRefOid", str)
    author = require(p, "author", dict).get("login")
    mergeable = require(p, "mergeable", str)
    mss = require(p, "mergeStateStatus", str)
    require(p, "baseRefName", str)
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
            latest[who.lower()] = (st, ((r.get("commit") or {}).get("oid")))
    if decision == "CHANGES_REQUESTED" or any(st == "CHANGES_REQUESTED" for st, _ in latest.values()):
        bad.append("unresolved CHANGES_REQUESTED")
    checks, check_bad = summarize_checks(rollup)
    bad += check_bad
    a, me_l = author.lower(), me.lower()
    if a == me_l:
        pr_class = "A"
    elif norm_bot(author) in BOT_LOGINS:
        pr_class = "B"
        if not BOT_BRANCH.match(branch) or cross:
            bad.append("bot PR lacks verified bot branch provenance")
        if latest.get(me_l) != ("APPROVED", head):
            bad.append("class B needs an APPROVED review by the authenticated user at the expected head")
    else:
        pr_class = "C"
        if not any(who != a and st == "APPROVED" and oid == head for who, (st, oid) in latest.items()):
            bad.append("class C needs an APPROVED review by a non-author at the expected head")
    if bad:
        raise Refused(bad)
    return pr_class, checks


def write_audit(path, record):
    """Append one JSONL record, file mode 0600. Raises OSError, which callers treat as fail closed."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
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
        facts = runner.json(pr_view_argv(repo, pr, PR_FIELDS))
        rec["observed_head"] = facts.get("headRefOid") if isinstance(facts.get("headRefOid"), str) else None
        load_evidence(evidence, head, repo, pr)
        rec["class"], rec["checks"] = decide(facts, repo, pr, head, me, allow)
        rec["decision"] = "APPROVE"
        # Re-verify the head immediately before the one mutating call.
        recheck = runner.json(pr_view_argv(repo, pr, HEAD_FIELDS))
        if recheck.get("state") != "OPEN" or recheck.get("headRefOid") != head:
            rec["observed_head"] = recheck.get("headRefOid")
            raise Refused(["head moved or PR closed between check and execution"])
        if dry_run:
            rec["outcome"], code = "DRY_RUN", EXIT_OK
        else:
            rec["outcome"] = "PENDING"
            write_audit(audit, dict(rec))  # fail closed: no audit trail, no merge
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
    except OSError:
        rec["reasons"].append("audit write failed")
        if code == EXIT_OK:
            code = EXIT_ERROR
    return code, rec


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def main(argv=None, gh=None):
    ap = _Parser(description="Guarded owner merge runner (merge commit only).", allow_abbrev=False)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--expect-head", required=True)
    ap.add_argument("--review-evidence", required=True)
    ap.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    ap.add_argument("--audit-log", default=DEFAULT_AUDIT)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if gh is None:
        try:
            gh = locate_gh()
        except RuntimeError as e:
            sys.stderr.write(f"error: {e}\n")
            return EXIT_ERROR
    code, rec = run(a.repo, a.pr, a.expect_head, a.review_evidence, a.allowlist, a.audit_log, a.dry_run, gh)
    print(json.dumps({k: rec[k] for k in ("repo", "pr", "class", "decision", "outcome", "reasons")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
