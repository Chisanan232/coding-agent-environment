#!/usr/bin/env python3
"""Read-only gate for autonomous owner pull-request merges (SPE-115).

  owner-merge-gate.py check  --repo OWNER/REPO --pr N --owners a,b[,c] [--ci-unavailable]
  owner-merge-gate.py verify --repo OWNER/REPO --pr N --head SHA

`check` evaluates the exact PR head and prints one JSON verdict (PASS, REFUSE or WAIT) plus the
merge command to run. It never merges, comments, approves or posts a status: the merge stays a
separate, visible `gh pr merge` that the host classifier and the repository judge on their own.
`verify` is the read-only post-merge check (merge commit exists, has the reviewed head as a parent
and is on the base branch).

Review evidence is a PR comment written by the authenticated user, one line:
  OWNER-MERGE-REVIEW head=<40-hex> verdict=PASS blocking=0 reviewer=<independent reviewer id>
It pins the reviewed head, so any later push invalidates it. It records an independent review; it is
never a GitHub approval and the gate never presents it as one. External-CI-unavailable exception:
  OWNER-MERGE-CI-UNAVAILABLE head=<40-hex> cause=<billing|quota|outage> local_equivalent=PASS
and only together with --ci-unavailable and only when no check genuinely failed.
"""
import argparse
import json
import re
import subprocess
import sys

REVIEW_RE = re.compile(r"^OWNER-MERGE-REVIEW head=([0-9a-f]{40}) verdict=(\w+) blocking=(\d+) reviewer=(\S+)\s*$", re.M)
CI_RE = re.compile(r"^OWNER-MERGE-CI-UNAVAILABLE head=([0-9a-f]{40}) cause=(billing|quota|outage) local_equivalent=(\w+)\s*$", re.M)
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
FAIL = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR", "STALE"}
OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
DEPLOY_BASES = re.compile(r"(^|/)(prod|production|release|releases|gh-pages|deploy)([-/].*)?$", re.I)

# One entry per refusal reason so callers and tests can match on a stable code.
PR_FIELDS = ("number,state,isDraft,baseRefName,headRefName,headRefOid,author,mergeable,mergeStateStatus,"
             "reviewDecision,reviews,comments,statusCheckRollup,isCrossRepository,mergeCommit")
THREADS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
           "{reviewThreads(first:100){nodes{isResolved isOutdated}}}}}")


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"gh {args[0]} {args[1]} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def _json(run, *args):
    return json.loads(run(*args))


def evaluate(run, repo, pr, owners, ci_unavailable=False):
    """Return (verdict, reasons, facts). `run` executes gh and returns stdout (injected in tests)."""
    reasons, wait = [], []
    owner = repo.split("/")[0].lower()
    if not REPO_RE.match(repo) or owner not in {o.lower() for o in owners}:
        return "REFUSE", [f"UNAUTHORIZED_REPO: {repo} is not owned by a trusted owner"], {}
    meta = _json(run, "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef,isFork,viewerPermission,owner")
    if meta["nameWithOwner"].lower() != repo.lower() or meta["owner"]["login"].lower() not in {o.lower() for o in owners}:
        reasons.append("NOT_CANONICAL_REPO: repository identity does not match the target")
    if meta.get("isFork"):
        reasons.append("FORK_REPO: target is a fork, not the canonical repository")
    if meta.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
        reasons.append(f"NO_WRITE_ACCESS: viewer permission {meta.get('viewerPermission')}")
    me = run("api", "user", "--jq", ".login").strip()
    p = _json(run, "pr", "view", str(pr), "--repo", repo, "--json", PR_FIELDS)
    head, base = p["headRefOid"], p["baseRefName"]
    facts = {"repo": repo, "pr": pr, "head": head, "base": base, "viewer": me}
    if p["state"] != "OPEN":
        reasons.append(f"NOT_OPEN: state {p['state']}")
    if p["isDraft"]:
        reasons.append("DRAFT")
    if p["isCrossRepository"]:
        reasons.append("CROSS_REPOSITORY_HEAD: head lives in another repository")
    if base != meta["defaultBranchRef"]["name"] or DEPLOY_BASES.search(base):
        reasons.append(f"BASE_NOT_DEFAULT_OR_DEPLOY: base {base!r}; deployment, release and non-default bases need their own authorization")
    if p["mergeable"] == "CONFLICTING" or p["mergeStateStatus"] == "DIRTY":
        reasons.append("CONFLICTS")
    elif p["mergeable"] == "UNKNOWN":
        wait.append("MERGEABILITY_UNKNOWN: GitHub has not computed mergeability yet")
    behind = int(run("api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".behind_by").strip() or 0)
    if behind:
        reasons.append(f"STALE_BRANCH: head is {behind} commit(s) behind {base}; rebase or merge the base and revalidate")
    if p["mergeStateStatus"] == "BLOCKED" and p["reviewDecision"] != "REVIEW_REQUIRED":
        reasons.append("BLOCKED_BY_REPOSITORY_RULES: mergeStateStatus BLOCKED (branch protection or ruleset)")
    if p["reviewDecision"] == "CHANGES_REQUESTED":
        reasons.append("CHANGES_REQUESTED")
    if p["reviewDecision"] == "REVIEW_REQUIRED":
        reasons.append("FORMAL_APPROVAL_REQUIRED: the repository requires a formal approving review; the gate never substitutes for it")
    latest = {}
    for r in p["reviews"]:
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["author"]["login"]] = r["state"]
    if "CHANGES_REQUESTED" in latest.values():
        reasons.append("CHANGES_REQUESTED: an outstanding REQUEST_CHANGES review")
    o, r_ = repo.split("/")
    threads = _json(run, "api", "graphql", "-f", f"query={THREADS}", "-F", f"o={o}", "-F", f"r={r_}", "-F", f"n={pr}")
    unresolved = [t for t in threads["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
                  if not t["isResolved"] and not t["isOutdated"]]
    if unresolved:
        reasons.append(f"UNRESOLVED_REVIEW_THREADS: {len(unresolved)}")
    texts = [c["body"] for c in p["comments"] if c["author"]["login"].lower() == me.lower()]
    found = [m for t in texts for m in REVIEW_RE.findall(t)]
    at_head = [m for m in found if m[0] == head]
    if not found:
        reasons.append("NO_REVIEW_EVIDENCE: no OWNER-MERGE-REVIEW record by the authenticated user")
    elif not at_head:
        reasons.append("REVIEW_STALE: the recorded review is for a different head; the PR changed after review")
    else:
        bad = [m for m in at_head if m[1] != "PASS" or m[2] != "0"]
        if bad or any(m[1] == "FAIL" or m[2] != "0" for m in found if m[0] == head):
            reasons.append("REVIEW_NOT_CLEAN: recorded review has blocking findings or a non-PASS verdict")
        facts["reviewer"] = at_head[-1][3]
        if at_head[-1][3].lower() in (me.lower(), "self", "author", "coordinator"):
            reasons.append("REVIEW_NOT_INDEPENDENT: reviewer id is the author or coordinator")
    checks = p["statusCheckRollup"] or []
    failed = [c.get("name") or c.get("context") for c in checks
              if (c.get("conclusion") or c.get("state") or "").upper() in FAIL]
    pending = [c.get("name") or c.get("context") for c in checks
               if c.get("status") not in (None, "COMPLETED") or (c.get("state") or "").upper() in ("PENDING", "EXPECTED")]
    if failed:
        reasons.append(f"CI_FAILED: {sorted(set(failed))[:5]}")
    elif pending:
        wait.append(f"CI_PENDING: {sorted(set(pending))[:5]}")
    elif not checks or not all((c.get("conclusion") or c.get("state") or "").upper() in OK for c in checks):
        ci = [m for t in texts for m in CI_RE.findall(t) if m[0] == head and m[2] == "PASS"]
        if ci_unavailable and ci and not checks:
            facts["ci_exception"] = ci[-1][1]
        else:
            reasons.append("NO_CI_EVIDENCE: no completed checks on this head and no qualified CI_UNAVAILABLE_EXTERNAL record")
    facts["checks"] = len(checks)
    if reasons:
        return "REFUSE", reasons + wait, facts
    if wait:
        return "WAIT", wait, facts
    return "PASS", [], facts


def verify(run, repo, pr, head):
    p = _json(run, "pr", "view", str(pr), "--repo", repo, "--json", "state,mergeCommit,baseRefName")
    out, ok = {"repo": repo, "pr": pr, "reviewed_head": head}, True
    if p["state"] != "MERGED" or not p.get("mergeCommit"):
        return {**out, "verdict": "NOT_MERGED", "state": p["state"]}
    mc = p["mergeCommit"]["oid"]
    parents = _json(run, "api", f"repos/{repo}/commits/{mc}", "--jq", "[.parents[].sha]")
    on_base = run("api", f"repos/{repo}/compare/{mc}...{p['baseRefName']}", "--jq", ".status").strip()
    out.update(merge_commit=mc, head_is_parent=head in parents, merge_commit_on_base=on_base in ("identical", "ahead"))
    ok = out["head_is_parent"] and out["merge_commit_on_base"] and len(parents) == 2
    out["merge_commit_has_two_parents"] = len(parents) == 2
    out["verdict"] = "VERIFIED" if ok else "MISMATCH"
    return out


def merge_command(facts):
    return (f"gh pr merge {facts['pr']} --repo {facts['repo']} --merge --match-head-commit {facts['head']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "verify"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--pr", type=int, required=True)
    sub.choices["check"].add_argument("--owners", required=True)
    sub.choices["check"].add_argument("--ci-unavailable", action="store_true")
    sub.choices["verify"].add_argument("--head", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.match(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            verdict, reasons, facts = evaluate(gh, a.repo, a.pr, owners, a.ci_unavailable)
            out = {"verdict": verdict, "reasons": reasons, **facts}
            if verdict == "PASS":
                out["merge_command"] = merge_command(facts)
            print(json.dumps(out, indent=1))
            return {"PASS": 0, "REFUSE": 1, "WAIT": 2}[verdict]
        out = verify(gh, a.repo, a.pr, a.head)
        print(json.dumps(out, indent=1))
        return 0 if out["verdict"] == "VERIFIED" else 1
    except (RuntimeError, KeyError, ValueError) as e:
        print(json.dumps({"verdict": "ERROR", "reasons": [str(e)[:300]]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
