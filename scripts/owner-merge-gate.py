#!/usr/bin/env python3
"""Read-only gate for autonomous owner pull-request merges (SPE-115).

  owner-merge-gate.py check  --repo OWNER/REPO --pr N --owners a,b[,c] [--ci-unavailable] [--settings settings.json]
  (check and queue also take [--version-policy FILE]: founder-approved version integration branches, see docs/OWNER-MERGE-WORKFLOW.md)
  owner-merge-gate.py verify --repo OWNER/REPO --pr N --head SHA
  owner-merge-gate.py queue  --repo OWNER/REPO --prs 5,6 --owners a,b      (print-only plan)
  owner-merge-gate.py evidence-line --head SHA --reviewer ID               (prints the record line)
  owner-merge-gate.py evidence-json --repo R --pr N --head SHA --reviewer ID   (prints the JSON file the owner-merge-runner reads)

`check` evaluates the exact PR head and prints one JSON verdict (PASS, REFUSE or WAIT) plus the
merge command to run, and a `decision` (ALLOW, ASK or DENY across five separate dimensions; see
docs/OWNER-MERGE-OPERATOR-GUIDE.md). It never merges, comments, approves or posts a status: the merge stays a
separate, visible `gh pr merge` that the host classifier and the repository judge on their own.
`verify` is the read-only post-merge check (merge commit exists, has the reviewed head as a parent
and is on the base branch).

Review evidence (self-attested by the authenticated identity) is a PR comment written by the authenticated user, one line:
  OWNER-MERGE-REVIEW head=<40-hex> verdict=PASS blocking=0 reviewer=<independent reviewer id>
It pins the reviewed head, so any later push invalidates it. It records an independent review; it is
never a GitHub approval and the gate never presents it as one. External-CI-unavailable exception:
  OWNER-MERGE-CI-UNAVAILABLE head=<40-hex> cause=<billing|quota|outage> local_equivalent=PASS
and only together with --ci-unavailable and only when no check genuinely failed.
No-hosted-CI exception (a repository that defines no CI workflow at all, founder-approved per PR; not a CI pass):
  OWNER-MERGE-CI-EXCEPTION head=<40-hex> basis=no-hosted-ci local_equivalent=PASS
and only together with --ci-exception, only when the head has zero checks, only when the repository's .github/workflows
directory does not exist (404; any other answer counts as 'has workflows'), and only with a clean review record. It records
that the local validation evidence stands in for CI; the comment must state that evidence.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

REVIEW_RE = re.compile(r"^OWNER-MERGE-REVIEW head=([0-9a-f]{40}) verdict=(\w+) blocking=(\d+) reviewer=(\S+)\s*$", re.M)
CI_RE = re.compile(r"^OWNER-MERGE-CI-UNAVAILABLE head=([0-9a-f]{40}) cause=(billing|quota|outage) local_equivalent=(\w+)\s*$", re.M)
NOCI_RE = re.compile(r"^OWNER-MERGE-CI-EXCEPTION head=([0-9a-f]{40}) basis=no-hosted-ci local_equivalent=(\w+)\s*$", re.M)
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")
FAIL = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR", "STALE"}
OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
DEPLOY_BASES = re.compile(r"(^|/)(prod|production|release|releases|gh-pages|deploy)([-/].*)?$", re.I)

# Environment and release names that must never become an approved integration base, even when an owner lists them.
# Environment words: a path segment that merely starts with one is refused (stagingeu, betafix, stg2, qa2, livefix), as
# is any such word as a whole token anywhere in the name (mvp-staging, x-beta, mvp-qa, prod). Deliberately over-broad and fail-closed; an owner renames a legitimate branch instead of listing it.
POLICY_FORBIDDEN = re.compile(
    r"(^|/)(staged?|staging|stg|beta|alpha|canary|hotfix|uat|preprod|qa|live)"
    r"|(^|[-/._])(staged?|staging|stg|beta|alpha|canary|hotfix|uat|preprod|qa|live|prd|prod|production|release|releases|deploy|"
    r"sandbox)([-/._0-9]|$)", re.I)
POLICY_MAX_BYTES = 65536


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("version policy has a duplicate JSON key")
    return dict(pairs)


VERSION_POLICY_DEFAULT = "~/.config/coding-agent-environment/local/version-branches.json"
VERSION_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def bad_ref_parts(name, prefix=False):
    """True when a path component could not be part of a valid git ref (empty, leading dot, trailing dot, `.lock`).
    A prefix may end with one slash."""
    parts = name.split("/")
    if prefix and parts[-1] == "":
        parts = parts[:-1]
    return any(p == "" or p.startswith(".") or p.endswith(".") or p.endswith(".lock") for p in parts)


def load_version_policy(obj):
    """Validate the owner-governed approved-version-branch policy and return {repo.lower(): {branches, prefixes}}.

    Development-Ahead / Sequential Release: `main` is the protected cumulative release baseline, but a founder-approved
    version integration branch may receive eligible feature PRs before that version ships. The list is explicit and lives
    outside every product repository, so a PR cannot authorize its own base. A deployment/release branch can never be listed."""
    if not isinstance(obj, dict) or type(obj.get("schema")) is not int or obj["schema"] != 1 or not isinstance(obj.get("repos"), dict):
        raise ValueError("version policy must be an object with schema 1 and a repos object")
    out = {}
    for repo, entry in obj["repos"].items():
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or not isinstance(entry, dict):
            raise ValueError(f"version policy repo {repo!r} is malformed")
        branches, prefixes = set(), []
        if not isinstance(entry.get("branches"), list):
            raise ValueError(f"version policy branches for {repo} must be a list")
        for b in entry["branches"]:
            name = b.get("branch") if isinstance(b, dict) else None
            if not isinstance(name, str) or not VERSION_REF_RE.fullmatch(name) or ".." in name or bad_ref_parts(name) or name.endswith("/"):
                raise ValueError(f"version policy branch {name!r} for {repo} is malformed")
            if DEPLOY_BASES.search(name) or POLICY_FORBIDDEN.search(name):
                raise ValueError(f"version policy branch {name!r} for {repo} is a deployment/release base and cannot be approved")
            branches.add(name)
        pres = entry.get("future_head_prefixes", [])
        if not isinstance(pres, list):
            raise ValueError(f"version policy future_head_prefixes for {repo} must be a list")
        for pre in pres:
            if not isinstance(pre, str) or not VERSION_REF_RE.fullmatch(pre) or ".." in pre or bad_ref_parts(pre, prefix=True):
                raise ValueError(f"version policy prefix {pre!r} for {repo} is malformed")
            prefixes.append(pre)
        if repo.lower() in out:
            raise ValueError(f"version policy lists {repo} more than once (repository names are case-insensitive)")
        out[repo.lower()] = {"branches": branches, "prefixes": tuple(x.lower() for x in prefixes)}
    return out


def read_version_policy(path, required, root=None):
    """Load the policy file. A missing default file means no policy (old behaviour); an explicit path must load.

    The policy is the trust anchor for which bases are mergeable, so it is accepted only from the fixed owner-config
    directory: the real path must sit under `root`, the file and every directory from `root` down must be owned by the
    caller and not group/world-writable, and it is opened without following a symlink and judged on the open descriptor."""
    import stat
    root = os.path.realpath(os.path.expanduser(root or os.path.dirname(VERSION_POLICY_DEFAULT)))
    full = os.path.expanduser(path)
    if os.path.islink(full):
        raise ValueError("version policy must be the file itself, not a symlink")
    real = os.path.realpath(full)
    if os.path.commonpath([root, real]) != root:
        raise ValueError(f"version policy must live under {root}")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise
        return None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise ValueError("version policy file must be a regular file owned by the caller and not group/world-writable")
        d = os.path.dirname(real)
        while True:
            ds = os.stat(d)
            if ds.st_uid != os.getuid() or ds.st_mode & 0o022:
                raise ValueError(f"version policy directory {d} must be owned by the caller and not group/world-writable")
            if d == root:
                break
            d = os.path.dirname(d)
        if st.st_size > POLICY_MAX_BYTES:
            raise ValueError(f"version policy file is larger than {POLICY_MAX_BYTES} bytes")
        return load_version_policy(json.loads(f.read(POLICY_MAX_BYTES + 1), object_pairs_hook=_no_duplicate_keys))


# One entry per refusal reason so callers and tests can match on a stable code.
PR_FIELDS = ("number,state,isDraft,baseRefName,headRefName,headRefOid,author,mergeable,mergeStateStatus,"
             "reviewDecision,reviews,comments,statusCheckRollup,isCrossRepository,mergeCommit,files,commits")
THREADS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
           "{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated}}}}}")


_RO = r"repos/[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}/)[A-Za-z0-9._-]{1,100}"
_REF = r"(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,200}"
API_READS = [re.compile(x) for x in (
    _RO + rf"/compare/{_REF}\.\.\.{_REF}", _RO + r"/commits/[0-9a-f]{40}", _RO + r"/pulls/\d{1,9}/files",
    _RO + r"/contents/(\.github/CODEOWNERS|CODEOWNERS|docs/CODEOWNERS)", _RO + r"/contents/\.github/workflows",
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
    if any(seg == ".." for a in args[:2] for seg in a.split("/")) or any(seg == ".." for seg in args[1].split("/")):
        raise RuntimeError("refused path traversal segment")
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
    if "*" in last and last not in ("*", "**") and not re.fullmatch(r"\*\.[A-Za-z0-9_.-]+", last):
        return None  # e.g. /docs*: the exact CODEOWNERS semantics are unclear, so fail closed
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


def has_workflows(run, repo):
    """True when the repository defines hosted CI workflows, False only when the API says the directory does not exist (404).
    Any other answer or error is treated as 'has workflows' (fail closed): the no-hosted-CI exception must never cover a
    repository whose CI merely has not reported yet."""
    try:
        return bool(run("api", f"repos/{repo}/contents/.github/workflows", "--jq", "length").strip() not in ("", "0"))
    except RuntimeError as error:
        return not ("404" in str(error) or "Not Found" in str(error))


def evaluate(run, repo, pr, owners, ci_unavailable=False, bots=None, version_policy=None, ci_exception=False):
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
    default_branch = meta["defaultBranchRef"]["name"]
    pol = (version_policy or {}).get(repo.lower())
    head_ref = p.get("headRefName") or ""
    if pol and base == default_branch and (head_ref.lower() in {b.lower() for b in pol["branches"]} or head_ref.lower().startswith(pol["prefixes"])):
        reasons.append(f"FUTURE_VERSION_INTO_MAIN: head {head_ref!r} is future-version work; it reaches {base!r} only through a "
                       "release step, never through this gate")
    if DEPLOY_BASES.search(base) or (base != default_branch and not (pol and base in pol["branches"])) \
            or (base != default_branch and POLICY_FORBIDDEN.search(base)):
        reasons.append(f"BASE_NOT_DEFAULT_OR_DEPLOY: base {base!r}; deployment, release and non-default bases need their own authorization")
    elif base != default_branch:
        facts["base_policy"] = "approved version integration branch"
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
        wait.append("AWAITING_BOT_APPROVAL: all other gates passed or pending; the owner must submit the real GitHub APPROVE (approve_command), then re-run check")
        facts["needs_approval"] = True
        facts["approve_actor"] = "owner personally; the agent never submits or simulates this approval"
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
        noci = [m for t in texts for m in NOCI_RE.findall(t) if m[0] == head and m[1] == "PASS"]
        if ci_unavailable and ci and not checks:
            facts["ci_exception"] = ci[-1][1]
        elif ci_exception and noci and not checks and not has_workflows(run, repo):
            facts["ci_exception"] = "no-hosted-ci"
        else:
            reasons.append("NO_CI_EVIDENCE: no completed checks on this head and no qualified CI_UNAVAILABLE_EXTERNAL record")
    facts["checks"] = len(checks)
    facts["formal_github_approval"] = "APPROVED" in latest.values()  # any reviewer; see owner_approved for the one that counts
    facts["owner_approved"] = owner_approved
    if reasons or [w for w in wait if not w.startswith("AWAITING_BOT_APPROVAL")]:
        facts.pop("approve_command", None)  # approve only after every other gate has passed
        facts.pop("needs_approval", None)
        facts.pop("approve_actor", None)
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


# --- Canonical executor (SPE-118): one trusted way to run an eligible merge ------------------------------------------------
# The runner (scripts/owner-merge-runner.py, installed at ~/.claude/bin/owner-merge-runner) re-validates the PR itself and is the
# only command the single Trusted ALLOW rule names. check offers it when it is installed, the repo is in the owner's runner
# allowlist and the host allows exactly that command; otherwise it offers the direct pinned command, which the host ASKs for.
# A runner refusal is binding: no direct-merge fallback is ever printed alongside it.
RUNNER_REL = os.path.join(".claude", "bin", "owner-merge-runner")
ALLOWLIST_REL = os.path.join(".claude", "owner-merge-runner-allowlist.json")
EVIDENCE_REL = os.path.join(".claude", "owner-merge-evidence")


def evidence_path(facts, home):
    return os.path.join(home, EVIDENCE_REL, f"{facts['repo'].replace('/', '__')}-{facts['pr']}-{facts['head'][:12]}.json")


def runner_command(facts, home):
    return (f"{os.path.join(home, RUNNER_REL)} --repo {facts['repo']} --pr {facts['pr']} --expect-head {facts['head']} "
            f"--review-evidence {evidence_path(facts, home)}")


def runner_available(home):
    """(installed, allowlisted repo names lower-cased). Reads only the allowlist's repo keys; any problem means not available."""
    runner = os.path.join(home, RUNNER_REL)
    installed = os.path.isfile(runner) and not os.path.islink(runner) and os.access(runner, os.X_OK)
    try:
        with open(os.path.join(home, ALLOWLIST_REL)) as fh:
            repos = {k.lower() for k in json.load(fh)["repos"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        repos = set()
    return installed, repos


def select_executor(settings, facts, home, installed, repos):
    """Pure: the command to run for a PASS and the host permission that judges it. Runner only when it is host-allowed."""
    if installed and facts["repo"].lower() in repos:
        command = runner_command(facts, home)
        host = host_permission(settings, command, rtk_twin=False)
        # The twin is never an authorization, but an explicit deny aimed at it still turns the runner off (the baseline rtk asks are not a signal).
        if host == "allow" and host_permission(settings, command) != "deny":
            return {"executor": "owner-merge-runner", "command": command, "host": host, "evidence_path": evidence_path(facts, home)}
    command = merge_command(facts)
    return {"executor": "direct-gh", "command": command, "host": host_permission(settings, command)}


# --- Three-outcome decision (SPE-118): ALLOW / ASK / DENY -----------------------------------------------------------------
# The gate verdict (PASS/WAIT/REFUSE) is only the engineering and GitHub dimensions. A decision also needs the host tool
# permission, and a PASS in one dimension is never a PASS in all. DENY is reserved for constraints nobody here may waive;
# everything a human, the author or the agent can resolve is ASK, so a missing approval is a request, not a dead end.
# (outcome, resolver, dimension) per reason code; resolver says who clears it.
DENY, ASK = "DENY", "ASK"
REASON_TABLE = {
    "UNAUTHORIZED_REPO": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"), "NOT_CANONICAL_REPO": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "FORK_REPO": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"), "CROSS_REPOSITORY_HEAD": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "NOT_OPEN": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"), "BLOCKED_BY_REPOSITORY_RULES": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "NOT_REPO_ADMIN": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"), "NO_MERGE_PERMISSION": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "OWNER_NOT_ADMINISTERED_BY_VIEWER": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "OWNER_IDENTITY_NOT_VERIFIED": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "CI_FAILED": (DENY, "author_fixes_ci", "ENGINEERING_GATE"),  # a genuinely failed check is not waivable here; remediate
    "DRAFT": (ASK, "author_marks_ready", "GITHUB_MERGE_ELIGIBILITY"), "CONFLICTS": (ASK, "author_resolves_conflicts", "GITHUB_MERGE_ELIGIBILITY"),
    "STALE_BRANCH": (ASK, "agent_revalidates", "GITHUB_MERGE_ELIGIBILITY"),
    "MERGE_STATE_NOT_CLEAN": (ASK, "agent_revalidates", "GITHUB_MERGE_ELIGIBILITY"),
    "BASE_NOT_DEFAULT_OR_DEPLOY": (ASK, "founder_authorizes_target", "GITHUB_MERGE_ELIGIBILITY"),
    "FUTURE_VERSION_INTO_MAIN": (ASK, "founder_authorizes_release", "GITHUB_MERGE_ELIGIBILITY"),
    "MERGEABILITY_UNKNOWN": (ASK, "agent_revalidates", "GITHUB_MERGE_ELIGIBILITY"),
    "MERGE_STATE_UNKNOWN": (ASK, "agent_revalidates", "GITHUB_MERGE_ELIGIBILITY"),
    "THIRD_PARTY_NEEDS_OWNER_APPROVAL": (ASK, "owner_github_approval", "GITHUB_APPROVAL_REQUIREMENT"),
    "OWNER_APPROVAL_STALE": (ASK, "owner_github_approval", "GITHUB_APPROVAL_REQUIREMENT"),
    "AWAITING_BOT_APPROVAL": (ASK, "owner_github_approval", "GITHUB_APPROVAL_REQUIREMENT"),
    "FORMAL_APPROVAL_REQUIRED": (ASK, "owner_github_approval", "GITHUB_APPROVAL_REQUIREMENT"),
    "UNVERIFIED_AUTHOR": (ASK, "owner_review", "GITHUB_APPROVAL_REQUIREMENT"),
    "CHANGES_REQUESTED": (ASK, "author_addresses_review", "GITHUB_APPROVAL_REQUIREMENT"),
    "NO_REVIEW_EVIDENCE": (ASK, "agent_runs_independent_review", "ENGINEERING_GATE"),
    "REVIEW_STALE": (ASK, "agent_runs_independent_review", "ENGINEERING_GATE"),
    "REVIEW_NOT_INDEPENDENT": (ASK, "agent_runs_independent_review", "ENGINEERING_GATE"),
    "REVIEW_NOT_CLEAN": (ASK, "author_fixes_findings", "ENGINEERING_GATE"),
    "UNRESOLVED_REVIEW_THREADS": (ASK, "author_resolves_threads", "ENGINEERING_GATE"),
    "REVIEW_THREADS_TRUNCATED": (ASK, "owner_review", "ENGINEERING_GATE"),
    "EVIDENCE_TRUNCATED": (ASK, "owner_review", "ENGINEERING_GATE"),
    "CI_PENDING": (ASK, "agent_revalidates", "ENGINEERING_GATE"),
    "NO_CI_EVIDENCE": (ASK, "founder_risk_decision", "ENGINEERING_GATE"),
}
SEVERITY = {"ALLOW": 0, ASK: 1, DENY: 2}
HOST_OUTCOME = {"allow": "ALLOW", "ask": ASK, "deny": DENY, "classifier": ASK, "not_evaluated": ASK}
_HOST_RANK = {"deny": 3, "ask": 2, "classifier": 1, "allow": 0}


def host_permission(settings_paths, command, rtk_twin=True):
    """HOST_TOOL_PERMISSION for the exact pinned command, from the permission rules of one or more settings files.

    Pass every layer you have (user, project, local, managed): rules are pooled and deny > ask > allow, and both the plain and the
    rtk-rewritten spelling are judged (the live hook rewrites commands). Only the layers passed are read, so `allow` means "allowed by
    these files", not "allowed by the host". Anything unparseable fails closed to `ask`: a non-string rule, a malformed `Bash(`
    rule, or a bare `Bash` in ask. A bare `Bash` deny is a deny. No rule at all means the Auto Mode classifier decides, which is not
    an authorization, so it is `classifier`. Nothing from the files is printed.
    """
    if not settings_paths:
        return "not_evaluated"
    if isinstance(settings_paths, str):
        settings_paths = [settings_paths]
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("permission_rollout", pathlib.Path(__file__).resolve().with_name("permission-rollout.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pooled, broken = {"deny": [], "ask": [], "allow": []}, False
    for path in settings_paths:
        perms = (json.load(open(path)).get("permissions") or {})
        for bucket in pooled:
            for r in perms.get(bucket, []):
                if not isinstance(r, str) or (r.startswith("Bash(") and not r.endswith(")")):
                    broken = True
                elif r == "Bash" and bucket == "deny":
                    return "deny"
                elif r == "Bash" and bucket == "ask":
                    broken = True
                else:
                    pooled[bucket].append(r)
    # rtk_twin=False judges only the literal spelling: the rewrite hook only touches commands rtk knows (gh, git, ...), never an absolute-path program
    spellings = (command, "rtk " + command) if rtk_twin else (command,)
    result = max((mod.static_decision(c, pooled) for c in spellings), key=lambda d: _HOST_RANK[d])
    return "ask" if broken and _HOST_RANK[result] < _HOST_RANK["ask"] else result


def decide(verdict, reasons, facts, host="not_evaluated", command=None):
    """Combine the dimensions into ALLOW / ASK / DENY. Pure: no I/O, so every cell of the matrix is testable."""
    dims = {"PR_OWNER_CLASS": facts.get("class", "UNKNOWN"), "ENGINEERING_GATE": "PASS", "GITHUB_APPROVAL_REQUIREMENT": "SATISFIED",
            "GITHUB_MERGE_ELIGIBILITY": "ELIGIBLE", "HOST_TOOL_PERMISSION": host}
    if facts.get("class") == "A":
        dims["GITHUB_APPROVAL_REQUIREMENT"] = "NOT_REQUIRED"
    outcome, asks, denies = "ALLOW", [], []
    if verdict == "ERROR":
        return {"outcome": DENY, "dimensions": dims, "asks": [], "denies": ["ERROR: the gate could not evaluate; fail closed"]}
    for text in reasons:
        code = text.split(":", 1)[0].strip()
        o, resolver, dim = REASON_TABLE.get(code, (ASK, "owner_review", "ENGINEERING_GATE"))  # an unknown reason is never ALLOW
        state = "BLOCKED" if o == DENY else "PENDING"
        if dim == "ENGINEERING_GATE":
            state = "FAIL" if o == DENY else "PENDING"
        elif dim == "GITHUB_APPROVAL_REQUIREMENT":
            state = "OWNER_APPROVAL_MISSING" if resolver == "owner_github_approval" else "PENDING"
        if dims[dim] in ("PASS", "SATISFIED", "NOT_REQUIRED", "ELIGIBLE") or state in ("BLOCKED", "FAIL"):
            dims[dim] = state  # a hard state is never softened by a later, milder reason
        (denies if o == DENY else asks).append({"reason": text, "resolver": resolver})
        if SEVERITY[o] > SEVERITY[outcome]:
            outcome = o
    if outcome == "ALLOW":  # every engineering/GitHub dimension passed; the host tool permission is the last, separate gate
        outcome = HOST_OUTCOME[host]
        if outcome == ASK:
            asks.append({"reason": f"HOST_TOOL_PERMISSION {host}: the host does not allow this merge to run without a prompt",
                         "resolver": "host_prompt"})
        elif outcome == DENY:
            denies.append({"reason": "HOST_TOOL_PERMISSION deny: a host rule denies this merge", "resolver": "none"})
    out = {"outcome": outcome, "dimensions": dims, "asks": asks, "denies": denies}
    if outcome in ("ALLOW", ASK) and verdict == "PASS":  # an approved ASK covers this one operation at this one head, nothing wider
        out["approval_scope"] = "exactly the pinned operation: " + (command or merge_command(facts))
    return out


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


def evidence_json(repo, pr, base, head, reviewer, kind="independent-agent", now=None):
    """The review-evidence record the validating runner reads (see owner-merge-runner.py load_evidence). Print-only; unsigned.

    It records that an independent review of this exact head passed. It is never a GitHub approval, and the runner (not this
    record) enforces the head pin, the checks and the class rules.
    """
    import time
    if not REPO_RE.fullmatch(repo) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(pr)) or not re.fullmatch(r"[0-9a-f]{40}", head) \
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,64}", reviewer) \
            or kind not in ("human", "independent-agent"):
        raise ValueError("repo, pr, base, head (40-hex), reviewer and kind (human|independent-agent) must be well formed")
    return {"repo": repo, "pr": int(pr), "base": base, "head": head, "verdict": "PASS", "reviewer": reviewer, "reviewer_kind": kind,
            "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}


def queue_report(run, repo, prs, owners, ci_unavailable=False, settings=None, version_policy=None, ci_exception=False):
    """Text plan for the owner: gate verdict per PR, in the order given. Print-only."""
    lines, first = [], True
    for n in prs:
        try:
            verdict, reasons, facts = evaluate(run, repo, n, owners, ci_unavailable, version_policy=version_policy, ci_exception=ci_exception)
        except Exception as e:
            lines.append(f"#{n} ERROR  {type(e).__name__}: {str(e)[:200]}")
            continue
        sel = select_executor(settings, facts, os.path.expanduser("~"), *runner_available(os.path.expanduser("~"))) if verdict == "PASS" else None
        decision = decide(verdict, reasons, facts, sel["host"] if sel else "not_evaluated", sel and sel["command"])
        lines.append(f"#{n} {verdict} [{facts.get('class', '?')}] decision={decision['outcome']}" + (f"  {'; '.join(reasons)}" if reasons else ""))
        if verdict == "WAIT" and facts.get("approve_command"):
            lines.append("  owner action (the agent must not run this), then re-run check: " + facts["approve_command"])
        if verdict == "PASS" and decision["outcome"] == DENY:
            lines.append("  not runnable: " + "; ".join(d["reason"] for d in decision["denies"]))
        elif verdict == "PASS":
            head = "  run: " if first else "  after the previous merge, re-run check (this branch will then be behind): "
            lines.append(head + sel["command"])
            if sel.get("evidence_path"):
                lines.append("  (write the review-evidence file named above only after an independent review PASS at this exact head)")
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
    sub.choices["check"].add_argument("--ci-exception", action="store_true",
                                      help="accept an OWNER-MERGE-CI-EXCEPTION record for a repository with no hosted CI workflows")
    SETTINGS_HELP = "settings.json whose permission rules are read (never printed) to judge HOST_TOOL_PERMISSION; repeat for every layer (user, project, local, managed)"
    sub.choices["check"].add_argument("--settings", action="append", help=SETTINGS_HELP)
    POLICY_HELP = ("approved version-branch policy (JSON, schema 1, owner-governed, outside every product repo); "
                   f"default {VERSION_POLICY_DEFAULT} when it exists")
    sub.choices["check"].add_argument("--version-policy", help=POLICY_HELP)
    sub.choices["verify"].add_argument("--head", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--repo", required=True)
    q.add_argument("--prs", required=True, help="comma-separated PR numbers, in the order to consider")
    q.add_argument("--owners", required=True)
    q.add_argument("--ci-unavailable", action="store_true")
    q.add_argument("--ci-exception", action="store_true")
    q.add_argument("--settings", action="append", help=SETTINGS_HELP)
    q.add_argument("--version-policy", help=POLICY_HELP)
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
    ce = sub.add_parser("ci-exception-line")
    ce.add_argument("--head", required=True)
    ej = sub.add_parser("evidence-json")
    ej.add_argument("--repo", required=True)
    ej.add_argument("--pr", required=True)
    ej.add_argument("--base", default="main")
    ej.add_argument("--head", required=True)
    ej.add_argument("--reviewer", required=True)
    ej.add_argument("--kind", default="independent-agent")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "evidence-json":
            print(json.dumps(evidence_json(a.repo, a.pr, a.base, a.head, a.reviewer, a.kind), indent=1))
            return 0
        if a.cmd == "evidence-line":
            print(evidence_line(a.head, a.reviewer, a.verdict, a.blocking))
            return 0
        if a.cmd == "ci-exception-line":
            if not re.fullmatch(r"[0-9a-f]{40}", a.head):
                raise ValueError("bad head")
            print(f"OWNER-MERGE-CI-EXCEPTION head={a.head} basis=no-hosted-ci local_equivalent=PASS")
            return 0
        if a.cmd == "host-denied":
            print(json.dumps(host_denied(a.repo, a.pr, a.head, a.message), indent=1))
            return 1
        if a.cmd == "queue":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.fullmatch(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            policy = read_version_policy(a.version_policy or VERSION_POLICY_DEFAULT, bool(a.version_policy))
            text = queue_report(run, a.repo, [int(x) for x in a.prs.split(",")], owners, a.ci_unavailable, a.settings, policy, a.ci_exception)
            print(text)
            return 0 if all(re.match(r"#\d+ PASS ", l) for l in text.splitlines() if l.startswith("#")) else 1
        if a.cmd == "check":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.fullmatch(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            policy = read_version_policy(a.version_policy or VERSION_POLICY_DEFAULT, bool(a.version_policy))
            verdict, reasons, facts = evaluate(run, a.repo, a.pr, owners, a.ci_unavailable, version_policy=policy, ci_exception=a.ci_exception)
            out = {"verdict": verdict, "reasons": reasons, **facts}
            sel = None
            if verdict == "PASS":
                sel = select_executor(a.settings, facts, os.path.expanduser("~"), *runner_available(os.path.expanduser("~")))
            host = sel["host"] if sel else "not_evaluated"
            out["decision"] = decide(verdict, reasons, facts, host, sel and sel["command"])
            if sel and out["decision"]["outcome"] != DENY:  # a host deny leaves nothing to run
                out["merge_command"], out["executor"] = sel["command"], sel["executor"]
                if sel.get("evidence_path"):
                    out["evidence_path"] = sel["evidence_path"]
                    out["evidence_note"] = ("write this file only after an independent review PASS at this exact head "
                                            "(owner-merge-gate.py evidence-json); the runner refuses without it")
            print(json.dumps(out, indent=1))
            if out["decision"]["outcome"] == DENY and verdict == "PASS":
                return 1
            return {"PASS": 0, "REFUSE": 1, "WAIT": 2}[verdict]
        out = verify(run, a.repo, a.pr, a.head)
        print(json.dumps(out, indent=1))
        return 0 if out["verdict"] == "VERIFIED" else 1
    except Exception as e:  # any failure is an ERROR (exit 3), never read as a REFUSE or a WAIT
        print(json.dumps({"verdict": "ERROR", "reasons": [f"{type(e).__name__}: {str(e)[:300]}"],
                          "decision": {"outcome": DENY, "denies": ["ERROR: the gate could not evaluate; fail closed"]}}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
