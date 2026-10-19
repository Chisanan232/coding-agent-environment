#!/usr/bin/env python3
"""Read-only gate for autonomous owner pull-request merges (SPE-115).

  owner-merge-gate.py check  --repo OWNER/REPO --pr N --owners a,b[,c] [--ci-unavailable]
  owner-merge-gate.py verify --repo OWNER/REPO --pr N --head SHA
  owner-merge-gate.py queue  --repo OWNER/REPO --prs 5,6 --owners a,b      (print-only plan)
  owner-merge-gate.py evidence-line --head SHA --reviewer ID               (prints the record line)

`check` evaluates the exact PR head and prints one JSON verdict (PASS, REFUSE or WAIT) plus the
merge command to run. It never merges, comments, approves or posts a status: the merge stays a
separate, visible `gh pr merge` that the host classifier and the repository judge on their own.
`verify` is the read-only post-merge check (merge commit exists, has the reviewed head as a parent
and is on the base branch).

Review evidence (self-attested by the authenticated identity) is a PR comment written by the authenticated user, one line:
  OWNER-MERGE-REVIEW head=<40-hex> verdict=PASS blocking=0 reviewer=<independent reviewer id>
It pins the reviewed head, so any later push invalidates it. It records an independent review; it is
never a GitHub approval and the gate never presents it as one. External-CI-unavailable exception:
  OWNER-MERGE-CI-UNAVAILABLE head=<40-hex> cause=<billing|quota|outage> local_equivalent=PASS
and only together with --ci-unavailable and only when no check genuinely failed.
"""
import argparse
import base64
import json
import re
import subprocess
import sys

REVIEW_RE = re.compile(r"^OWNER-MERGE-REVIEW head=([0-9a-f]{40}) verdict=(\w+) blocking=(\d+) reviewer=(\S+)\s*$", re.M)
CI_RE = re.compile(r"^OWNER-MERGE-CI-UNAVAILABLE head=([0-9a-f]{40}) cause=(billing|quota|outage) local_equivalent=(\w+)\s*$", re.M)
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")
FAIL = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR", "STALE"}
OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
DEPLOY_BASES = re.compile(r"(^|/)(prod|production|release|releases|gh-pages|deploy)([-/].*)?$", re.I)

# One entry per refusal reason so callers and tests can match on a stable code.
PR_FIELDS = ("number,state,isDraft,baseRefName,headRefName,headRefOid,author,mergeable,mergeStateStatus,"
             "reviewDecision,reviews,comments,statusCheckRollup,isCrossRepository,mergeCommit,files,commits")
THREADS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
           "{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated}}}}}")


_RO = r"repos/[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}/)[A-Za-z0-9._-]{1,100}"
_REF = r"(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,200}"
API_READS = [re.compile(x) for x in (
    _RO + rf"/compare/{_REF}\.\.\.{_REF}", _RO + r"/commits/[0-9a-f]{40}", _RO + r"/pulls/\d{1,9}/files",
    _RO + r"/contents/(\.github/CODEOWNERS|CODEOWNERS|docs/CODEOWNERS)",
    r"orgs/[A-Za-z0-9][A-Za-z0-9-]{0,38}/memberships/[A-Za-z0-9][A-Za-z0-9-]{0,38}")]
SAFE_FLAGS = {"--json", "--jq", "--repo"}
GRAPHQL_VARS = {"query", "o", "r", "n"}


def read_only(args):
    """Fail closed: the gate may only read. Anything else raises before gh is started.

    An exact allowlist, not a blacklist: besides the verb, the only flags are --json/--jq/--repo
    (plus -f/-F query variables for one GraphQL read). Attached short forms (-XPOST), headers,
    --hostname, --input, --paginate and every other flag are refused.
    """
    args = [str(x) for x in args]
    verb = tuple(args[:2])
    ok_api = verb[0] == "api" and len(args) > 1 and (verb[1] in ("user", "graphql") or any(r.fullmatch(verb[1]) for r in API_READS))
    if verb not in (("repo", "view"), ("pr", "view")) and not ok_api:
        raise RuntimeError(f"refused non-read-only gh call: {' '.join(verb)}")
    if "mutation" in " ".join(args).lower():
        raise RuntimeError("refused gh call containing a mutation")
    i = 2
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            if verb == ("api", "graphql") and arg in ("-f", "-F") and i + 1 < len(args):
                key, _, val = args[i + 1].partition("=")
                if key not in GRAPHQL_VARS or val.startswith("@") or (key == "query" and not val.lstrip().startswith("query")):
                    raise RuntimeError("refused graphql variable")
                i += 2
                continue
            if arg not in SAFE_FLAGS:
                raise RuntimeError(f"refused gh flag {arg.split('=')[0]}")
            if arg == "--jq" and i + 1 < len(args) and re.search(r"\benv\b|\$ENV|\binput|\bdebug|@sh|\bgetpath|\$__", args[i + 1]):
                raise RuntimeError("refused jq expression that can read the environment or input")
            i += 2 if i + 1 < len(args) else 1
            continue
        i += 1


def gh(*args):
    read_only(args)
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"gh {args[0]} {args[1]} failed: {r.stderr.strip()[:200]}")
    return r.stdout


# SPE-116 PR classes. A = owner-authored, B = verified dependency bot, C = any other author.
BOT_LOGINS = {"dependabot", "renovate"}
BOT_BRANCH = re.compile(r"(dependabot|renovate)/")
MERGE_PERMS = {"ADMIN", "MAINTAIN", "WRITE"}
DEP_FILES = re.compile(
    r"(^|.*/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|"
    r"pyproject\.toml|requirements[\w.-]*\.txt|Pipfile(\.lock)?|Cargo\.(toml|lock)|go\.(mod|sum)|Gemfile(\.lock)?|"
    r"composer\.(json|lock)|pom\.xml|build\.gradle(\.kts)?|gradle\.lockfile|mise\.toml|\.pre-commit-config\.yaml|Dockerfile)")
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def norm_bot(login):
    login = (login or "").lower()
    if login.startswith("app/"):
        login = login[4:]
    return login[:-5] if login.endswith("[bot]") else login


def _co_regex(pat):
    """Subset of CODEOWNERS globbing; None means unsupported syntax, which fails closed."""
    if not pat or any(c in pat for c in "[]!\\?#") or " " in pat:
        return None
    anchored = pat.startswith("/") or "/" in pat.rstrip("/")
    dir_only = pat.endswith("/")
    body = pat.strip("/")
    out, i = "", 0
    last = body.rsplit("/", 1)[-1]
    while i < len(body):
        if body.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif body.startswith("**", i):
            out, i = out + ".*", i + 2
        elif body[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(body[i]), i + 1
    return re.compile(("" if anchored else "(?:.*/)?") + out + ("/.*" if dir_only else ("" if "*" in last else "(?:/.*)?")))


def codeowners_cover(text, files, me):
    """True only if the last matching rule for every file lists @me (teams never count)."""
    rules = []
    for line in text.splitlines():
        line = re.split(r"\s#", line)[0].strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        rx = _co_regex(parts[0])
        if rx is None:
            return False
        rules.append((rx, [o.lower() for o in parts[1:]]))
    for f in files:
        owners = None
        for rx, o in rules:
            if rx.fullmatch(f):
                owners = o
        if owners is None or f"@{me.lower()}" not in owners:
            return False
    return True


def bot_provenance(p, bots):
    """Reasons a PR from an allowlisted bot identity is NOT verified dependency-bot provenance."""
    bad = []
    if not BOT_BRANCH.match(p["headRefName"]):
        bad.append("branch is not a bot branch")
    if p["isCrossRepository"]:
        bad.append("head is in another repository")
    commits = p.get("commits") or []
    if len(commits) >= 100:
        bad.append("commit list may be truncated")
    if not commits:
        bad.append("no commit authorship to verify")
    for c in commits:
        for a in c.get("authors") or []:
            if norm_bot(a.get("login")) not in bots:
                bad.append("a commit on the bot branch was authored by a non-bot")
                break
    files = p.get("files") or []
    if not files or len(files) >= 100:
        bad.append("changed files unknown or truncated")
    out_of_scope = [f["path"] for f in files if not DEP_FILES.fullmatch(f["path"])]
    if out_of_scope:
        bad.append(f"changes outside dependency manifests: {out_of_scope[:3]}")
    return bad


def _json(run, *args):
    return json.loads(run(*args))


def evaluate(run, repo, pr, owners, ci_unavailable=False, bots=None):
    """Return (verdict, reasons, facts). `run` executes gh and returns stdout (injected in tests)."""
    bots = {norm_bot(b) for b in (bots or BOT_LOGINS)}
    reasons, wait = [], []
    owner = repo.split("/")[0].lower()
    if not REPO_RE.fullmatch(repo) or owner not in {o.lower() for o in owners}:
        return "REFUSE", [f"UNAUTHORIZED_REPO: {repo} is not owned by a trusted owner"], {}
    meta = _json(run, "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef,isFork,viewerPermission,owner")
    if meta["nameWithOwner"].lower() != repo.lower() or meta["owner"]["login"].lower() not in {o.lower() for o in owners}:
        reasons.append("NOT_CANONICAL_REPO: repository identity does not match the target")
    if meta.get("isFork"):
        reasons.append("FORK_REPO: target is a fork, not the canonical repository")
    me = run("api", "user", "--jq", ".login").strip()
    perm = meta.get("viewerPermission")
    p = _json(run, "pr", "view", str(pr), "--repo", repo, "--json", PR_FIELDS)
    head, base = p["headRefOid"], p["baseRefName"]
    facts = {"repo": repo, "pr": pr, "head": head, "base": base, "viewer": me}
    author = p.get("author") or {}
    login, is_bot = author.get("login") or "", bool(author.get("is_bot"))
    if not login or login.lower() in ("ghost", "unknown"):
        klass = "UNVERIFIED"
        reasons.append("UNVERIFIED_AUTHOR: PR author cannot be verified; fail closed and request owner review")
    elif not is_bot and login.lower() == me.lower():
        klass = "A"
    elif is_bot and norm_bot(login) in bots:
        bad = bot_provenance(p, bots)
        klass = "C" if bad else "B"
        if bad:
            facts["bot_provenance_failed"] = bad
    else:
        klass = "C"
    facts["class"] = klass
    if klass == "A":
        if perm != "ADMIN":
            co = ""
            if perm in ("WRITE", "MAINTAIN") and 0 < len(p.get("files") or []) < 30 \
                    and not any(f["path"] in CODEOWNERS_PATHS for f in p["files"]):
                renamed = run("api", f"repos/{repo}/pulls/{pr}/files", "--jq",
                              "[.[] | select(.previous_filename != null or .status == \"renamed\")] | length").strip()
                if renamed == "0":
                    for path in CODEOWNERS_PATHS:
                        try:
                            co = base64.b64decode(run("api", f"repos/{repo}/contents/{path}", "--jq", ".content")).decode()
                            break
                        except RuntimeError as ex:
                            if "404" not in str(ex) and "Not Found" not in str(ex):
                                raise  # only a missing file may fall through to a lower-precedence one
            if co and codeowners_cover(co, [f["path"] for f in p["files"]], me):
                facts["authority"] = "verified code owner with write permission"
            else:
                reasons.append(f"NOT_REPO_ADMIN: viewer permission {perm}, and not a verified code owner of every changed file")
        else:
            facts["authority"] = "repository administrator"
            if owner != me.lower():  # an org is trusted only if the authenticated user administers it
                role = run("api", f"orgs/{owner}/memberships/{me}", "--jq", ".role + \" \" + .state").strip()
                if role != "admin active":
                    reasons.append(f"OWNER_NOT_ADMINISTERED_BY_VIEWER: {owner!r} is neither the authenticated account nor an org it administers")
    elif klass in ("B", "C"):
        if perm not in MERGE_PERMS:
            reasons.append(f"NO_MERGE_PERMISSION: viewer permission {perm}")
        # The approval that counts must be the owner's personally, not just any collaborator's with write access.
        if owner != me.lower():
            role = run("api", f"orgs/{owner}/memberships/{me}", "--jq", ".role + \" \" + .state").strip()
            if role != "admin active":
                reasons.append(f"OWNER_IDENTITY_NOT_VERIFIED: {me!r} is neither the repository owner account nor an administrator of org {owner!r}")
    if len(p["comments"]) >= 100 or len(p["reviews"]) >= 100:
        reasons.append("EVIDENCE_TRUNCATED: 100 or more comments or reviews; later records may be hidden")
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
    mss = p["mergeStateStatus"]
    if mss == "BLOCKED" and p["reviewDecision"] != "REVIEW_REQUIRED":
        reasons.append("BLOCKED_BY_REPOSITORY_RULES: mergeStateStatus BLOCKED (branch protection or ruleset)")
    elif mss == "UNKNOWN":
        wait.append("MERGE_STATE_UNKNOWN: GitHub has not computed the merge state yet")
    elif mss not in ("CLEAN", "HAS_HOOKS", "BLOCKED", "DIRTY", "BEHIND"):
        reasons.append(f"MERGE_STATE_NOT_CLEAN: mergeStateStatus {mss!r} (unstable or unrecognized state fails closed)")
    if p["reviewDecision"] == "CHANGES_REQUESTED":
        reasons.append("CHANGES_REQUESTED")
    # A genuine GitHub approval counts only when it is for the exact current head (no commit id = not provable).
    approved_by = set()
    for r in p["reviews"]:
        who = r["author"]["login"].lower()
        if r["state"] == "APPROVED" and (r.get("commit") or {}).get("oid") == head:
            approved_by.add(who)
        elif r["state"] in ("CHANGES_REQUESTED", "DISMISSED", "APPROVED"):
            approved_by.discard(who)
    owner_approved = me.lower() in approved_by
    if klass == "C":
        facts["admin_merge"] = "never for third-party PRs"
        if not owner_approved:
            stale = any(r["state"] == "APPROVED" and r["author"]["login"].lower() == me.lower() for r in p["reviews"])
            reasons.append("OWNER_APPROVAL_STALE: the owner's approval is for a different head or was withdrawn" if stale else
                           "THIRD_PARTY_NEEDS_OWNER_APPROVAL: a genuine GitHub approval by the owner on this exact head is required; agent comments never substitute")
    if p["reviewDecision"] == "REVIEW_REQUIRED" and (klass in ("A", "UNVERIFIED") or (klass in ("B", "C") and owner_approved)):
        reasons.append("FORMAL_APPROVAL_REQUIRED: the repository requires a formal approving review; the gate never substitutes for it")
    if klass == "B" and not owner_approved:
        wait.append("AWAITING_BOT_APPROVAL: all other gates passed or pending; submit the real GitHub APPROVE (approve_command), then re-run check")
        facts["needs_approval"] = True
        facts["approve_command"] = (f"gh pr review {pr} --repo {repo} --approve --body "
                                    f"\"Dependency review recorded for head {head[:12]} in the OWNER-MERGE-REVIEW comment.\"")
    latest = {}
    for r in p["reviews"]:
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            at = (r.get("commit") or {}).get("oid")
            latest[r["author"]["login"]] = r["state"] + ("" if at in (None, head) or r["state"] != "APPROVED" else "@stale")
    if "CHANGES_REQUESTED" in latest.values():
        reasons.append("CHANGES_REQUESTED: an outstanding REQUEST_CHANGES review")
    o, r_ = repo.split("/")
    threads = _json(run, "api", "graphql", "-f", f"query={THREADS}", "-F", f"o={o}", "-F", f"r={r_}", "-F", f"n={pr}")
    rt = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
    if rt["pageInfo"]["hasNextPage"]:
        reasons.append("REVIEW_THREADS_TRUNCATED: more than 100 review threads, cannot prove none is unresolved")
    unresolved = [t for t in rt["nodes"] if not t["isResolved"]]
    if unresolved:
        reasons.append(f"UNRESOLVED_REVIEW_THREADS: {len(unresolved)}")
    texts = [re.sub(r"```.*?```", "", (c["body"] or ""), flags=re.S) for c in p["comments"]
             if (c.get("author") or {}).get("login", "").lower() == me.lower()]
    found = [m for t in texts for m in REVIEW_RE.findall(t)]
    at_head = [m for m in found if m[0] == head]
    if not found:
        reasons.append("NO_REVIEW_EVIDENCE: no OWNER-MERGE-REVIEW record by the authenticated user")
    elif not at_head:
        reasons.append("REVIEW_STALE: the recorded review is for a different head; the PR changed after review")
    else:
        bad = [m for m in at_head if m[1] != "PASS" or m[2] != "0"]
        if bad:
            reasons.append("REVIEW_NOT_CLEAN: recorded review has blocking findings or a non-PASS verdict")
        facts["reviewer"] = at_head[-1][3]
        facts["review_kind"] = "recorded technical review, self-attested by the authenticated identity (not a GitHub approval)"
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
    elif not checks or not all((c.get("conclusion") or c.get("state") or "").upper() in OK for c in checks) \
            or not any((c.get("conclusion") or c.get("state") or "").upper() == "SUCCESS" for c in checks):
        ci = [m for t in texts for m in CI_RE.findall(t) if m[0] == head and m[2] == "PASS"]
        if ci_unavailable and ci and not checks:
            facts["ci_exception"] = ci[-1][1]
        else:
            reasons.append("NO_CI_EVIDENCE: no completed checks on this head and no qualified CI_UNAVAILABLE_EXTERNAL record")
    facts["checks"] = len(checks)
    facts["formal_github_approval"] = "APPROVED" in latest.values()
    if reasons or [w for w in wait if not w.startswith("AWAITING_BOT_APPROVAL")]:
        facts.pop("approve_command", None)  # approve only after every other gate has passed
        facts.pop("needs_approval", None)
    if reasons:
        return "REFUSE", reasons + wait, facts
    if wait:
        return "WAIT", wait, facts
    return "PASS", [], facts


def verify(run, repo, pr, head):
    if not REPO_RE.fullmatch(repo) or not re.fullmatch(r"[0-9a-f]{40}", head):
        return {"verdict": "ERROR", "reasons": ["BAD_REPO_OR_HEAD"]}
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


def host_denied(repo, pr, head, message):
    """Print-only record of a host refusal. A denial stands: no alternate route is ever offered."""
    if not REPO_RE.fullmatch(repo) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("bad repo or head")
    text = re.sub(r"[\r\n]+", " ", message)[:300]
    return {"status": "HARNESS_PERMISSION_BLOCK", "action": "stop", "alternate_route_allowed": False,
            "blocked_operation": f"gh pr merge {pr} --repo {repo} --merge --match-head-commit {head}",
            "host_message": text,
            "owner_action": "Run the blocked_operation yourself (prefix ! in the session), or approve it from /permissions > Recently denied. Then run verify."}


def evidence_line(head, reviewer, verdict="PASS", blocking=0):
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,64}", reviewer):
        raise ValueError("head must be a full 40-hex sha and reviewer a plain identifier")
    if verdict not in ("PASS", "FAIL") or blocking < 0:
        raise ValueError("verdict must be PASS or FAIL and blocking >= 0")
    return f"OWNER-MERGE-REVIEW head={head} verdict={verdict} blocking={blocking} reviewer={reviewer}"


def queue_report(run, repo, prs, owners, ci_unavailable=False):
    """Text plan for the owner: gate verdict per PR, in the order given. Print-only."""
    lines, first = [], True
    for n in prs:
        try:
            verdict, reasons, facts = evaluate(run, repo, n, owners, ci_unavailable)
        except Exception as e:
            lines.append(f"#{n} ERROR  {type(e).__name__}: {str(e)[:200]}")
            continue
        lines.append(f"#{n} {verdict} [{facts.get('class', '?')}]" + (f"  {'; '.join(reasons)}" if reasons else ""))
        if verdict == "WAIT" and facts.get("approve_command"):
            lines.append("  approve (only after the gate passes), then re-run check: " + facts["approve_command"])
        if verdict == "PASS":
            lines.append(("  run: " if first else "  after the previous merge, re-run check (this branch will then be behind): ")
                         + merge_command(facts))
            first = False
    return "\n".join(lines)


def main(argv=None, run=None):
    run = run or gh
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--pr", type=int, required=True)
    sub.choices["check"].add_argument("--owners", required=True)
    sub.choices["check"].add_argument("--ci-unavailable", action="store_true")
    sub.choices["verify"].add_argument("--head", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--repo", required=True)
    q.add_argument("--prs", required=True, help="comma-separated PR numbers, in the order to consider")
    q.add_argument("--owners", required=True)
    q.add_argument("--ci-unavailable", action="store_true")
    d = sub.add_parser("host-denied")
    d.add_argument("--repo", required=True)
    d.add_argument("--pr", type=int, required=True)
    d.add_argument("--head", required=True)
    d.add_argument("--message", default="")
    e = sub.add_parser("evidence-line")
    e.add_argument("--head", required=True)
    e.add_argument("--reviewer", required=True)
    e.add_argument("--verdict", default="PASS")
    e.add_argument("--blocking", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "evidence-line":
            print(evidence_line(a.head, a.reviewer, a.verdict, a.blocking))
            return 0
        if a.cmd == "host-denied":
            print(json.dumps(host_denied(a.repo, a.pr, a.head, a.message), indent=1))
            return 1
        if a.cmd == "queue":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.fullmatch(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            text = queue_report(run, a.repo, [int(x) for x in a.prs.split(",")], owners, a.ci_unavailable)
            print(text)
            return 0 if all(re.match(r"#\d+ PASS ", l) for l in text.splitlines() if l.startswith("#")) else 1
        if a.cmd == "check":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.fullmatch(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            verdict, reasons, facts = evaluate(run, a.repo, a.pr, owners, a.ci_unavailable)
            out = {"verdict": verdict, "reasons": reasons, **facts}
            if verdict == "PASS":
                out["merge_command"] = merge_command(facts)
            print(json.dumps(out, indent=1))
            return {"PASS": 0, "REFUSE": 1, "WAIT": 2}[verdict]
        out = verify(run, a.repo, a.pr, a.head)
        print(json.dumps(out, indent=1))
        return 0 if out["verdict"] == "VERIFIED" else 1
    except Exception as e:  # any failure is an ERROR (exit 3), never read as a REFUSE or a WAIT
        print(json.dumps({"verdict": "ERROR", "reasons": [f"{type(e).__name__}: {str(e)[:300]}"]}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
