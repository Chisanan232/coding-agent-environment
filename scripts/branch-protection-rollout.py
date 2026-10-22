#!/usr/bin/env python3
"""Owner-run, hash-pinned rollout of server-side required status checks on one branch (SPE-118).

  branch-protection-rollout.py plan     --repo R --branch B --checks a,b [--no-enforce-admins]
  branch-protection-rollout.py apply    --repo R --branch B --checks a,b --expect-payload-sha SHA
  branch-protection-rollout.py verify   --repo R --branch B --checks a,b
  branch-protection-rollout.py rollback --repo R --branch B --checks a,b --expect-payload-sha SHA

`plan` and `verify` only read. `apply` and `rollback` change repository settings and are for the repository owner;
an agent session does not run them. Scope is deliberately narrow: it only creates protection on a branch that has
none (an existing protection or any ruleset coverage is reported and refused, never overwritten) and `rollback`
deletes only a protection that still equals the one `apply` wrote, so later edits are not silently lost. Every
step is read back from GitHub; a readback that differs is reported, never assumed.

The payload requires the named checks, forbids force pushes and deletion, and sets enforce_admins (default on) so
the required checks bind the repository owner's own identity, which is what makes the server-side check
meaningful for the merge runner. It sets no review requirement, restriction or linear-history rule.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

_SPEC = importlib.util.spec_from_file_location("omr", pathlib.Path(__file__).resolve().parent / "owner-merge-runner.py")
omr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(omr)

BACKUPS = os.path.join(os.path.expanduser("~"), ".claude", "backups")


def payload(checks, enforce_admins):
    return {"required_status_checks": {"strict": False, "contexts": sorted(checks)},
            "enforce_admins": bool(enforce_admins), "required_pull_request_reviews": None, "restrictions": None,
            "allow_force_pushes": False, "allow_deletions": False}


def payload_sha(p):
    return hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def protection_path(repo, branch):
    return f"repos/{repo}/branches/{branch}/protection"


def read_state(gh, repo, branch):
    """('absent', None) | ('present', GET body). Anything unreadable raises."""
    rc, out, err = gh(["api", protection_path(repo, branch)])
    if rc == 0:
        return "present", json.loads(out)
    if omr.http_status(err) == "404" and "not protected" in err.lower():
        return "absent", None
    raise RuntimeError(omr.gh_error(err, "protection-read"))


def covered_by_rulesets(gh, repo, branch):
    rc, out, err = gh(["api", f"repos/{repo}/rules/branches/{branch}"])
    if rc != 0:
        raise RuntimeError(omr.gh_error(err, "rules-read"))
    return [r.get("type") for r in json.loads(out)]


EXTRA_FLAGS = ("required_linear_history", "required_signatures", "required_conversation_resolution", "block_creations",
               "lock_branch", "allow_fork_syncing")
KNOWN_KEYS = {"url", "required_status_checks", "enforce_admins", "restrictions", "allow_force_pushes", "allow_deletions",
              *EXTRA_FLAGS}


def matches(body, p):
    """Readback equals what was asked for AND nothing else is set: any other protection field present or enabled
    (reviews, restrictions, signatures, linear history, a pinned check source...) makes this False, so rollback
    never deletes protection somebody strengthened after apply."""
    rsc = body.get("required_status_checks") or {}
    checks = rsc.get("checks") or []
    names = {c.get("context") for c in checks} | set(rsc.get("contexts") or [])
    flag = lambda k: (body.get(k) or {}).get("enabled")  # noqa: E731
    return (names == set(p["required_status_checks"]["contexts"]) and rsc.get("strict") is False
            and set(body) <= KNOWN_KEYS and set(rsc) <= {"url", "strict", "contexts", "checks", "contexts_url"}
            and flag("enforce_admins") is p["enforce_admins"] and flag("allow_force_pushes") is False
            and flag("allow_deletions") is False and not body.get("restrictions")
            and all(not flag(k) for k in EXTRA_FLAGS))


def normalize(body):
    """Canonical form of a protection readback without GitHub's own url fields."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if not k.endswith("url")}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return json.dumps(strip(body), sort_keys=True, separators=(",", ":"))


def record_applied(path, body):
    with open(path) as f:
        rec = json.load(f)
    rec["applied"] = normalize(body)
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)


def recorded_applied(repo, branch, sha):
    """The readback `apply` recorded for exactly this payload (newest first), or None."""
    prefix = f"branch-protection.{repo.replace('/', '_')}.{branch.replace('/', '_')}."
    try:
        names = sorted((n for n in os.listdir(BACKUPS) if n.startswith(prefix)), reverse=True)
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(BACKUPS, n)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("payload_sha256") == sha and isinstance(rec.get("applied"), str):
            return rec["applied"]
    return None


def backup(repo, branch, p, prior):
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    path = os.path.join(BACKUPS, f"branch-protection.{repo.replace('/', '_')}.{branch.replace('/', '_')}.{time.time_ns()}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"repo": repo, "branch": branch, "prior": prior, "payload_sha256": payload_sha(p)}, f)
    return path


def run(cmd, repo, branch, checks, enforce_admins, expect_sha, gh, out):
    p = payload(checks, enforce_admins)
    sha = payload_sha(p)
    state, body = read_state(gh, repo, branch)
    if cmd == "plan":
        out({"state": state, "rulesets_on_branch": covered_by_rulesets(gh, repo, branch), "payload": p, "payload_sha256": sha,
             "can_apply": state == "absent",
             "owner_effect": "enforce_admins=true: required checks bind the owner too; the owner can still merge a PR "
                             "whose checks pass, and `rollback` (or deleting the protection) restores today's behavior"
                             if enforce_admins else "enforce_admins=false: the owner can bypass the checks, so the "
                             "runner's server-side requirement is not met for the owner's own identity"})
        return 0
    if cmd == "verify":
        ok = state == "present" and matches(body, p)
        runner_ok = None
        if ok:
            try:
                omr.check_server_protection(omr.Runner(gh), repo, branch, {
                    "required_checks": list(checks), "accept_bypass_actors": False})
                runner_ok = True
            except omr.Refused as e:
                runner_ok = False
                out({"runner_refusal": e.reasons})
        out({"state": state, "matches_payload": ok, "runner_server_side_check": runner_ok})
        return 0 if ok and runner_ok else 5
    if expect_sha != sha:
        out({"error": "expected payload sha does not match this plan", "payload_sha256": sha})
        return 2
    if cmd == "apply":
        if state != "absent" or covered_by_rulesets(gh, repo, branch):
            out({"error": "branch already has protection or ruleset coverage; refusing to overwrite", "state": state})
            return 2
        state, _ = read_state(gh, repo, branch)  # re-read right before the write: GitHub offers no compare-and-swap
        if state != "absent" or covered_by_rulesets(gh, repo, branch):
            out({"error": "protection appeared after the plan read; refusing to overwrite", "state": state})
            return 2
        path = backup(repo, branch, p, None)
        rc, _, err = gh(["api", "-X", "PUT", protection_path(repo, branch), "--input", "-"], stdin=json.dumps(p))
        state2, body2 = read_state(gh, repo, branch)
        applied = rc == 0 and state2 == "present" and matches(body2, p)
        if applied:
            record_applied(path, body2)
        out({"applied": applied, "backup": path, "put": "ok" if rc == 0 else omr.gh_error(err, "protection-put"),
             "readback_matches": bool(state2 == "present" and matches(body2, p)),
             "rollback": f"{sys.argv[0]} rollback --repo {repo} --branch {branch} --checks {','.join(sorted(checks))} "
                         f"--expect-payload-sha {sha}"})
        return 0 if applied else 5
    if cmd == "rollback":
        if state == "absent":
            out({"rolled_back": True, "note": "already absent"})
            return 0
        wrote = recorded_applied(repo, branch, sha)
        if not matches(body, p) or wrote is None or normalize(body) != wrote:
            out({"error": "live protection is not exactly what apply wrote (or no apply record exists); refusing to delete "
                          "later edits"})
            return 2
        rc, _, err = gh(["api", "-X", "DELETE", protection_path(repo, branch)])
        state2, _ = read_state(gh, repo, branch)
        out({"rolled_back": state2 == "absent", "delete": "ok" if rc == 0 else omr.gh_error(err, "protection-delete")})
        return 0 if state2 == "absent" else 5
    raise AssertionError(cmd)


class Gh:
    def __init__(self):
        self.base = omr.locate_gh()

    def __call__(self, argv, stdin=None):
        if stdin is None:
            return self.base(argv)
        r = subprocess.run([self.base.path, *argv], input=stdin, capture_output=True, text=True, timeout=120, shell=False,
                           env=dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1"))
        return r.returncode, r.stdout, r.stderr


def main(argv=None, gh=None, out=None):
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("cmd", choices=["plan", "apply", "verify", "rollback"])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--checks", required=True)
    ap.add_argument("--no-enforce-admins", action="store_true")
    ap.add_argument("--expect-payload-sha")
    a = ap.parse_args(argv)
    checks = [c for c in a.checks.split(",") if c]
    if not (omr.REPO_RE.fullmatch(a.repo) and omr.BRANCH_RE.fullmatch(a.branch) and ".." not in a.branch and checks):
        sys.stderr.write("error: bad --repo, --branch or --checks\n")
        return 64
    out = out or (lambda d: print(json.dumps(d, indent=2, sort_keys=True)))
    try:
        return run(a.cmd, a.repo, a.branch, checks, not a.no_enforce_admins, a.expect_payload_sha, gh or Gh(), out)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"error: {omr.scrub(e)}\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
