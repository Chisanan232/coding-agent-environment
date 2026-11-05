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
and only together with --ci-unavailable and only when no check genuinely failed. With cause=billing, a failed or cancelled
GitHub Actions job is not 'genuinely failed' when it executed zero steps and its check-run annotation carries GitHub's billing text
(or it is a matrix job cancelled because such a billing-blocked sibling failed); everything else (a job that ran a step, a non-Actions
check, an unknown annotation, any API error) still refuses. Waived jobs are listed in facts.ci_unavailable_checks and are never a CI pass.
No-hosted-CI exception (a repository that defines no CI workflow at all, founder-approved per PR; not a CI pass):
  OWNER-MERGE-CI-EXCEPTION head=<40-hex> basis=no-hosted-ci local_equivalent=PASS
and only together with --ci-exception, only when the head has zero checks, only when the repository's .github/workflows
directory does not exist (404; any other answer counts as 'has workflows'), and only with a clean review record. It records
that the local validation evidence stands in for CI; the comment must state that evidence.
Local-first basis (a private repository whose own head commit carries a GitHub skip directive, so no hosted CI exists for it; not a CI pass):
  OWNER-MERGE-LOCAL-CI {json}   (written by scripts/owner-merge-local-ci.py, posted unedited by the authenticated user)
only together with --ci-exception, only for a repository in the owner's trust file (VERSION-style, outside every product repo,
default ~/.config/coding-agent-environment/local/local-first-ci.json, see docs/OWNER-MERGE-WORKFLOW.md), only when the rollup is
empty and GitHub reports no Actions run for the head, and only when every owner-fixed command ran at this exact head and base and
exited 0. Hosted checks are reported NOT_RUN, never as a pass.
"""
import argparse
import base64
import calendar
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time

REVIEW_RE = re.compile(r"^OWNER-MERGE-REVIEW head=([0-9a-f]{40}) verdict=(\w+) blocking=(\d+) reviewer=(\S+)\s*$", re.M)
CI_RE = re.compile(r"^OWNER-MERGE-CI-UNAVAILABLE head=([0-9a-f]{40}) cause=(billing|quota|outage) local_equivalent=(\w+)\s*$", re.M)
NOCI_RE = re.compile(r"^OWNER-MERGE-CI-EXCEPTION head=([0-9a-f]{40}) basis=no-hosted-ci local_equivalent=(\w+)\s*$", re.M)
LOCAL_RE = re.compile(r"^OWNER-MERGE-LOCAL-CI (.*?)\s*$", re.M)
# GitHub's documented spellings for "do not run workflows on this commit": one of five bracketed phrases anywhere in the message, or a
# `skip-checks: true` / `skip-checks:true` as the final line after a blank line. Matched case-sensitively and exactly; when unsure the gate treats it as no directive.
SKIP_RE = re.compile(r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]")


def has_skip_directive(message):
    lines = message.rstrip().split("\n")
    trailer = len(lines) >= 3 and lines[-1] in ("skip-checks: true", "skip-checks:true") and lines[-2].strip() == "" and any(x.strip() for x in lines[:-2])
    return bool(SKIP_RE.search(message)) or trailer


SHA_RE = re.compile(r"[0-9a-f]{40}")
STAMP_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
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
        raise ValueError("owner config file has a duplicate JSON key")
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


def read_owner_file(path, required, validator, label, root=None):
    """Load an owner-authored config file and return validator(parsed JSON). A missing default file means None (old behaviour);
    an explicit path must load.

    The file is a trust anchor (which bases are mergeable, which commands stand in for CI), so it is accepted only from the fixed
    owner-config directory: the real path must sit under `root`, the file and every directory from `root` down must be owned by the
    caller and not group/world-writable, and it is opened without following a symlink and judged on the open descriptor."""
    import stat
    root = os.path.realpath(os.path.expanduser(root or os.path.dirname(VERSION_POLICY_DEFAULT)))
    full = os.path.expanduser(path)
    if os.path.islink(full):
        raise ValueError(f"{label} must be the file itself, not a symlink")
    real = os.path.realpath(full)
    if os.path.commonpath([root, real]) != root:
        raise ValueError(f"{label} must live under {root}")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise
        return None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise ValueError(f"{label} file must be a regular file owned by the caller and not group/world-writable")
        d = os.path.dirname(real)
        while True:
            ds = os.stat(d)
            if ds.st_uid != os.getuid() or ds.st_mode & 0o022:
                raise ValueError(f"{label} directory {d} must be owned by the caller and not group/world-writable")
            if d == root:
                break
            d = os.path.dirname(d)
        if st.st_size > POLICY_MAX_BYTES:
            raise ValueError(f"{label} file is larger than {POLICY_MAX_BYTES} bytes")
        return validator(json.loads(f.read(POLICY_MAX_BYTES + 1), object_pairs_hook=_no_duplicate_keys))


def read_version_policy(path, required, root=None):
    return read_owner_file(path, required, load_version_policy, "version policy", root)


LOCAL_FIRST_DEFAULT = "~/.config/coding-agent-environment/local/local-first-ci.json"
def log_slug(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


LOCAL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
ENV_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
# Known variables that redirect what git, an interpreter, a build tool or the dynamic loader runs, and the names the recorder passes
# itself; an owner cannot fix these. A denylist of known redirect keys, not a proof that no other variable can matter.
ENV_FORBIDDEN = re.compile(r"(GIT_|PYTHON|NODE|LD_|DYLD_|PERL|RUBY|PIP_|UV_|PYTEST_|NPM_|BUNDLE_|GEM_|CARGO_|RUSTC|RUSTFLAGS|LC_|"
                           r"VIRTUAL_ENV|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|JDK_JAVA_OPTIONS|CLASSPATH|"
                           r"(?:BASH_ENV|ENV|PATH|HOME|USER|LOGNAME|LANG|TMPDIR|TERM|TZ)$)")


def load_local_first(obj):
    """Validate the owner-authored local-first CI trust file and return {repo.lower(): entry}.

    The agent must not write this file (same OS user, so the rule and the owner's review are the only barrier): the commands listed here are what stands in for a hosted CI run, so whoever can edit them can
    weaken the gate. Each repo lists the exact checks (name + argv) the recorder must run, how old a record may be, paths whose
    change is never covered (they could rewrite what a command runs) and what the local run does not cover."""
    if not isinstance(obj, dict) or type(obj.get("schema")) is not int or obj["schema"] != 1 or not isinstance(obj.get("repos"), dict):
        raise ValueError("local-first config must be an object with schema 1 and a repos object")
    out = {}
    for repo, entry in obj["repos"].items():
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or not isinstance(entry, dict) \
                or set(entry) - {"checks", "max_age_hours", "pinned_paths", "not_covered", "env"}:
            raise ValueError(f"local-first config repo {repo!r} is malformed")
        checks, names = [], set()
        if not isinstance(entry.get("checks"), list) or not entry["checks"]:
            raise ValueError(f"local-first checks for {repo} must be a non-empty list")
        for c in entry["checks"]:
            ok = isinstance(c, dict) and set(c) == {"name", "argv"} and isinstance(c["name"], str) and LOCAL_NAME_RE.fullmatch(c["name"]) \
                and isinstance(c["argv"], list) and c["argv"] and all(isinstance(a, str) and 0 < len(a) <= 500 for a in c["argv"])
            if not ok or log_slug(c["name"]) in names:   # two names with one log file would overwrite each other's evidence
                raise ValueError(f"local-first check for {repo} is malformed or repeated")
            names.add(log_slug(c["name"]))
            checks.append((c["name"], tuple(c["argv"])))
        hours = entry.get("max_age_hours", 24)
        if type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError(f"local-first max_age_hours for {repo} must be an integer from 1 to 168")
        lists = {}
        for key in ("pinned_paths", "not_covered"):
            lists[key] = entry.get(key, [])
            if not isinstance(lists[key], list) or not all(isinstance(x, str) and x for x in lists[key]):
                raise ValueError(f"local-first {key} for {repo} must be a list of non-empty strings")
        env = entry.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and ENV_KEY_RE.fullmatch(k) and not ENV_FORBIDDEN.match(k)
                                                and isinstance(v, str) and len(v) <= 500 for k, v in env.items()):
            raise ValueError(f"local-first env for {repo} must map plain variable names to strings and may not redirect an interpreter or loader")
        if repo.lower() in out:
            raise ValueError(f"local-first config lists {repo} more than once (repository names are case-insensitive)")
        out[repo.lower()] = {"checks": checks, "max_age_hours": hours, "pinned_paths": tuple(lists["pinned_paths"]),
                             "not_covered": list(lists["not_covered"]), "env": dict(env)}
    return out


# One entry per refusal reason so callers and tests can match on a stable code.
PR_FIELDS = ("number,state,isDraft,title,baseRefName,headRefName,headRefOid,author,mergeable,mergeStateStatus,"
             "reviewDecision,reviews,comments,statusCheckRollup,isCrossRepository,mergeCommit,files,commits")
THREADS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
           "{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated}}}}}")


_RO = r"repos/[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}/)[A-Za-z0-9._-]{1,100}"
_REF = r"(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]{1,200}"
API_READS = [re.compile(x) for x in (
    _RO + rf"/compare/{_REF}\.\.\.{_REF}", _RO + r"/commits/[0-9a-f]{40}", _RO + r"/pulls/\d{1,9}/files",
    _RO + r"/contents/(\.github/CODEOWNERS|CODEOWNERS|docs/CODEOWNERS)", _RO + r"/contents/\.github/workflows\?ref=(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,199}",
    # The billing waiver (--ci-unavailable) reads one check run, its job and its annotations by numeric id.
    _RO + r"/check-runs/\d{1,15}", _RO + r"/actions/jobs/\d{1,15}", _RO + r"/check-runs/\d{1,15}/annotations\?per_page=100",
    # The local-first basis asks GitHub whether any Actions run exists for the head (a count only).
    _RO + r"/actions/runs\?head_sha=[0-9a-f]{40}&per_page=1", _RO + r"/pulls/\d{1,9}/files\?per_page=100",
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


def has_workflows(run, repo, ref):
    """True unless GitHub says, for this exact ref (the base branch or the head commit), that the repository has no
    `.github/workflows` directory (HTTP 404 in the error text gh reports, after its own `failed:` prefix so that a repository
    name cannot imitate it). Every other outcome counts as 'has workflows' (fail closed): a successful answer, empty output,
    a rate limit, a 5xx, an unreadable reply. The no-hosted-CI exception must never cover a repository whose CI merely has
    not reported yet. It speaks for GitHub Actions only: an external CI app is invisible here."""
    try:
        run("api", f"repos/{repo}/contents/.github/workflows?ref={ref}", "--jq", "length")
    except RuntimeError as error:
        detail = str(error).partition(" failed: ")[2]
        return not re.search(r"\bHTTP 404\b", detail)
    return True


def no_hosted_ci(run, repo, base, head, files, cross_repository=False):
    """True only when no workflow exists at the base or at the reviewed head and the PR changes none (a PR that adds a
    workflow which has not run yet must not use the exception). Unknown or truncated file lists refuse."""
    if cross_repository or not files or len(files) >= 100:   # a fork's head commit is not readable through the base repository
        return False
    if any(".github/workflows" in str(f.get("path", "")).casefold() for f in files):
        return False
    return not has_workflows(run, repo, base) and not has_workflows(run, repo, head)


BILLING_TEXT = "recent account payments have failed or your spending limit needs to be increased"
SIBLING_CANCEL_RE = re.compile(r'The strategy configuration was canceled because "([^"]+)" failed')
WAIVABLE_CONCLUSIONS = {"FAILURE", "CANCELLED"}


def _norm_job(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _billing_job_facts(run, repo, head, name, run_id, job_id):
    """Read-only look at one GitHub Actions job. Returns (billing_annotated, canceled_because_of, zero_steps).
    Any unexpected shape, truncation or API error raises: the caller treats that as 'not waivable'."""
    check = _json(run, "api", f"repos/{repo}/check-runs/{job_id}")
    if not isinstance(check, dict) or str(check.get("id")) != job_id or check.get("head_sha") != head \
            or (check.get("app") or {}).get("slug") != "github-actions" or check.get("name") != name:
        raise ValueError("check run is not this head's GitHub Actions job")
    job = _json(run, "api", f"repos/{repo}/actions/jobs/{job_id}")
    if not isinstance(job, dict) or str(job.get("id")) != job_id or str(job.get("run_id")) != run_id \
            or job.get("head_sha") != head or not isinstance(job.get("steps"), list):
        raise ValueError("job record does not match the check run or the steps are unknown")
    zero_steps = all(isinstance(s, dict) and s.get("conclusion") == "skipped" for s in job["steps"])
    notes = _json(run, "api", f"repos/{repo}/check-runs/{job_id}/annotations?per_page=100")
    if not isinstance(notes, list) or not notes or len(notes) >= 100 or not all(isinstance(n, dict) for n in notes):
        raise ValueError("annotations missing, malformed or possibly truncated")
    messages = [str(n.get("message") or "") for n in notes]
    billing = any(BILLING_TEXT in m.lower() for m in messages)
    sibling = None
    if not billing:
        found = {m.group(1) for t in messages for m in [SIBLING_CANCEL_RE.search(t)] if m}
        if len(found) == 1:
            sibling = found.pop()
    return billing, sibling, zero_steps


def billing_waiver(run, repo, head, checks):
    """Waive failed checks only when each is a GitHub Actions job that executed zero steps and is either annotated with
    GitHub's billing text or is a matrix job cancelled because one billing-blocked sibling of the same workflow run failed.
    Returns (waived entries, []) when every failed check qualifies, otherwise (None, reasons). Never presents any of them as passing."""
    url_re = re.compile(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/(\d+)/job/(\d+)", re.I)
    failed = [c for c in checks if (c.get("conclusion") or c.get("state") or "").upper() in FAIL]
    info, bad = [], []
    for c in failed:
        name = c.get("name") or ""
        m = url_re.fullmatch(c.get("detailsUrl") or "")
        conclusion = (c.get("conclusion") or "").upper()
        if c.get("__typename") != "CheckRun" or not m or not name or conclusion not in WAIVABLE_CONCLUSIONS \
                or c.get("status") != "COMPLETED":
            bad.append(f"{name or '?'}: not a completed failed/cancelled GitHub Actions job")
            continue
        try:
            billing, sibling, zero = _billing_job_facts(run, repo, head, name, m.group(1), m.group(2))
        except Exception as ex:  # fail closed on any API or parsing problem
            bad.append(f"{name}: evidence unreadable ({type(ex).__name__})")
            continue
        if not zero:
            bad.append(f"{name}: the job executed steps")
        elif not billing and not (sibling and conclusion == "CANCELLED"):
            bad.append(f"{name}: no recognised billing annotation")
        else:
            info.append({"name": name, "conclusion": conclusion, "run_id": m.group(1), "job_id": m.group(2),
                         "billing": billing, "sibling": sibling})
    for e in info:
        if e["billing"]:
            e["reason"] = "billing-annotated job, zero steps executed"
            continue
        direct = [d for d in info if d["billing"] and d["run_id"] == e["run_id"] and _norm_job(d["name"]) == _norm_job(e["sibling"])]
        if len(direct) == 1:
            e["reason"] = f"matrix sibling cancelled because billing-blocked job {direct[0]['name']!r} failed, zero steps executed"
        else:
            bad.append(f"{e['name']}: cancelled sibling not tied to exactly one billing-blocked job of the same run")
    if bad or not info or len(info) != len(failed):
        return None, bad or ["no failed checks to waive"]
    return [{"name": e["name"], "conclusion": e["conclusion"], "job_id": e["job_id"], "reason": e["reason"]} for e in info], []


def _stamp(text):
    if not isinstance(text, str) or not STAMP_RE.fullmatch(text):
        raise ValueError("bad timestamp")
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def _no_dup(pairs):
    return _no_duplicate_keys(pairs)


def _int(x):
    return type(x) is int


# What the recorder derives from the PR so a detached local run sees what hosted CI would (head ref, title, merge-base).
LOCAL_CONTEXT_KEYS = {"PULL_BRANCH", "PR_TITLE", "GIT_BASE"}


def parse_local_record(rec):
    """Strictly validate one OWNER-MERGE-LOCAL-CI record and return its commands' finish times; raises ValueError on any deviation."""
    top = {"schema", "repo", "pr", "base", "base_sha", "head", "env", "context", "commands"}
    ok = isinstance(rec, dict) and set(rec) == top and rec["schema"] == 1 and _int(rec["schema"]) and isinstance(rec["repo"], str) \
        and _int(rec["pr"]) and isinstance(rec["base"], str) and isinstance(rec["base_sha"], str) and SHA_RE.fullmatch(rec["base_sha"]) \
        and isinstance(rec["head"], str) and SHA_RE.fullmatch(rec["head"]) and isinstance(rec["commands"], list) and rec["commands"] \
        and isinstance(rec["env"], dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in rec["env"].items()) \
        and isinstance(rec["context"], dict) and set(rec["context"]) == LOCAL_CONTEXT_KEYS and all(isinstance(v, str) for v in rec["context"].values())
    if not ok:
        raise ValueError("record shape")
    finished = []
    for c in rec["commands"]:
        keys = {"name", "argv", "exit_code", "started_at", "finished_at", "log_sha256"}
        if not isinstance(c, dict) or set(c) != keys or not isinstance(c["name"], str) or not isinstance(c["argv"], list) \
                or not all(isinstance(a, str) for a in c["argv"]) or not _int(c["exit_code"]) \
                or not isinstance(c["log_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", c["log_sha256"]):
            raise ValueError("command shape")
        start, end = _stamp(c["started_at"]), _stamp(c["finished_at"])
        if end < start:
            raise ValueError("command ends before it starts")
        finished.append(end)
    return finished


def local_first_basis(run, repo, pr, base, head, p, meta, me, texts_meta, raw_bodies, cfg, now):
    """Third CI basis. Returns (reasons, wait, facts). Only called for an empty rollup, --ci-exception and a repo in the owner's
    trust file. Every uncertainty refuses; a pass is never a CI pass (hosted checks stay NOT_RUN)."""
    reasons, wait, facts = [], [], {}
    if meta.get("isPrivate") is not True:
        return ["NOT_PRIVATE: the local-first basis exists only for private repositories"], wait, facts
    files = p.get("files") or []

    def covered(path):   # a workflow, or a path the owner pinned; case-insensitive and over-broad on purpose
        path = str(path or "").casefold()
        return ".github/workflows" in path or any(fnmatch.fnmatchcase(path, g.casefold()) for g in cfg["pinned_paths"])
    # `pr view --json files` lists only the new path of a rename, so a workflow moved out of its directory is read from the REST list too.
    listed = _json(run, "api", f"repos/{repo}/pulls/{pr}/files?per_page=100", "--jq", "[.[] | {filename, previous_filename, status}]") \
        if files and len(files) < 100 else []
    consistent = isinstance(listed, list) and all(isinstance(f, dict) for f in listed) and len(listed) == len(files) \
        and {str(f.get("filename")) for f in listed} == {str(f.get("path")) for f in files}
    if p.get("isCrossRepository") or not files or len(files) >= 100 or not consistent \
            or any(f.get("status") == "renamed" or f.get("previous_filename") or covered(f.get("previous_filename")) for f in listed) \
            or any(covered(f.get("path")) for f in files):
        reasons.append("LOCAL_FIRST_SCOPE: the file list is unknown, truncated or inconsistent, or the PR renames a file or changes a workflow or an owner-pinned path")
    if int(run("api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=1", "--jq", ".total_count").strip()):
        wait.append("CI_PENDING: GitHub Actions has a run for this head; the hosted result decides, not the local-first basis")
    elif not has_skip_directive(run("api", f"repos/{repo}/commits/{head}", "--jq", ".commit.message")):
        wait.append("CI_PENDING: the empty rollup is not explained by a skip directive in the head commit; hosted CI may not have started")
    items = []   # in comment order: ("rec", record, comment) or ("bad", None, comment)
    for c, text in texts_meta:
        for m in LOCAL_RE.finditer(re.sub(r"~~~.*?~~~", "", text, flags=re.S)):
            raw = m.group(1)
            try:
                rec = json.loads(raw, object_pairs_hook=_no_dup)
            except ValueError:
                rec = None
                if head in raw:
                    items.append(("bad", None, c))
                continue
            if isinstance(rec, dict) and rec.get("head") == head:
                items.append(("rec", rec, c))
            elif head in raw:
                items.append(("bad", None, c))
    # A failing run at this head stands wherever it appears in a comment body (fenced, quoted, indented, in a minimized comment), and
    # however its exit code is spelled: anything other than the integer 0 is a failure, and a later pass never overrides it. A line at
    # this head that cannot be read (bad JSON, duplicate keys, oversized) is treated as a failure too. An edit replaces a comment's body,
    # so text removed by an edit is not visible here; that is a residual risk, not something this scan can see.
    for body in raw_bodies:
        for line in body.splitlines():
            line = line.lstrip(" \t>")
            if not line.startswith("OWNER-MERGE-LOCAL-CI "):
                continue
            raw = line[len("OWNER-MERGE-LOCAL-CI "):].strip()
            try:
                if len(raw) > POLICY_MAX_BYTES:
                    raise ValueError("oversized")
                rec = json.loads(raw, object_pairs_hook=_no_dup)
            except ValueError:
                if head in raw:
                    return reasons + ["LOCAL_CI_FAILED: a record line at this head cannot be read; treated as a failed run"], wait, facts
                continue
            cmds = rec.get("commands") if isinstance(rec, dict) and rec.get("head") == head else None
            if isinstance(cmds, list) and any(isinstance(x, dict) and not (_int(x.get("exit_code")) and x["exit_code"] == 0) for x in cmds):
                return reasons + ["LOCAL_CI_FAILED: a recorded local run at this head did not exit 0; fix it and record a new run"], wait, facts
    trusted = [i for i in items if i[2].get("includesCreatedEdit") is False and i[2].get("isMinimized") is False]
    if not trusted:
        return reasons + ["LOCAL_CI_MISSING: no unedited, visible OWNER-MERGE-LOCAL-CI record for this head by the authenticated user"], wait, facts
    kind, rec, comment = trusted[-1]
    try:
        if kind == "bad":
            raise ValueError("unparsable")
        finished = parse_local_record(rec)
        created = _stamp(comment.get("createdAt"))
    except ValueError:
        return reasons + ["LOCAL_CI_MALFORMED: the latest record for this head is not exactly the recorder's schema"], wait, facts
    mismatch = rec["repo"].lower() != repo.lower() or rec["pr"] != pr or rec["base"] != base or rec["head"] != head
    if not mismatch:
        mismatch = rec["base_sha"] != run("api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".merge_base_commit.sha").strip()
    if mismatch or max(finished) > now + 60 or max(finished) > created:
        return reasons + ["LOCAL_CI_MISMATCH: the record is for another repo, PR, base, base commit or head, or claims to finish after it was posted"], wait, facts
    if [(c["name"], tuple(c["argv"])) for c in rec["commands"]] != cfg["checks"] or rec["env"] != cfg["env"]:
        return reasons + ["LOCAL_CI_COMMANDS_MISMATCH: the recorded commands or environment are not exactly the owner's configured checks, in order"], wait, facts
    if rec["context"] != {"PULL_BRANCH": p.get("headRefName"), "PR_TITLE": p.get("title"), "GIT_BASE": rec["base_sha"]}:
        return reasons + ["LOCAL_CI_CONTEXT_MISMATCH: the record's PR branch, title or base differ from the PR now; rerun the local checks"], wait, facts
    if now - max(finished) > cfg["max_age_hours"] * 3600:
        return reasons + [f"LOCAL_CI_STALE: the record is older than {cfg['max_age_hours']} hours; rerun the local checks"], wait, facts
    if not reasons and not wait:
        facts.update(ci_exception="local-first", hosted_checks="NOT_RUN",
                     ci_verdict="NOT_A_CI_PASS: LOCAL_EQUIVALENT_PASS; hosted checks never ran for this head",
                     local_ci_digest=hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                     not_covered=cfg["not_covered"])
    return reasons, wait, facts


def evaluate(run, repo, pr, owners, ci_unavailable=False, bots=None, version_policy=None, ci_exception=False, local_first=None, now=None):
    """Return (verdict, reasons, facts). `run` executes gh and returns stdout (injected in tests)."""
    bots = {norm_bot(b) for b in (bots or BOT_LOGINS)}
    reasons, wait = [], []
    owner = repo.split("/")[0].lower()
    if not REPO_RE.fullmatch(repo) or owner not in {o.lower() for o in owners}:
        return "REFUSE", [f"UNAUTHORIZED_REPO: {repo} is not owned by a trusted owner"], {}
    meta = _json(run, "repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef,isFork,isPrivate,viewerPermission,owner")
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
    unstable_reason = None
    if mss == "BLOCKED" and p["reviewDecision"] != "REVIEW_REQUIRED":
        reasons.append("BLOCKED_BY_REPOSITORY_RULES: mergeStateStatus BLOCKED (branch protection or ruleset)")
    elif mss == "UNKNOWN":
        wait.append("MERGE_STATE_UNKNOWN: GitHub has not computed the merge state yet")
    elif mss not in ("CLEAN", "HAS_HOOKS", "BLOCKED", "DIRTY", "BEHIND"):
        reasons.append(f"MERGE_STATE_NOT_CLEAN: mergeStateStatus {mss!r} (unstable or unrecognized state fails closed)")
        unstable_reason = reasons[-1] if mss == "UNSTABLE" else None
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
    mine = [c for c in p["comments"] if (c.get("author") or {}).get("login", "").lower() == me.lower()]
    texts = [re.sub(r"```.*?```", "", (c["body"] or ""), flags=re.S) for c in mine]
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
    waived = None
    if failed and ci_unavailable:
        recs = [m for t in texts for m in CI_RE.findall(t) if m[0] == head and m[2] == "PASS"]
        if recs and recs[-1][1] == "billing":  # quota and outage keep the old behaviour: a failed check still refuses
            waived, why = billing_waiver(run, repo, head, checks)
            if waived is None:
                facts["ci_unavailable_refused"] = why[:10]
    if failed and waived is None:
        reasons.append(f"CI_FAILED: {sorted(set(failed))[:5]}")
    elif pending:
        wait.append(f"CI_PENDING: {sorted(set(pending))[:5]}")
    elif waived is not None:
        done = [c for c in checks if (c.get("conclusion") or c.get("state") or "").upper() not in FAIL]
        if all((c.get("conclusion") or c.get("state") or "").upper() in OK for c in done):
            facts["ci_exception"] = "billing"
            facts["ci_unavailable_checks"] = waived
            if unstable_reason in reasons:
                # GitHub reports UNSTABLE while a non-required check has failed. Here every failed check is one the billing
                # waiver covers and nothing is pending, so the state is explained; BLOCKED, DIRTY, BEHIND and others still refuse.
                reasons.remove(unstable_reason)
                facts["merge_state"] = "UNSTABLE, explained by the billing-waived checks (not a CI pass)"
            facts["ci_verdict"] = ("NOT_A_CI_PASS: these Actions jobs were blocked by billing and never ran; the recorded "
                                   "local-equivalent evidence stands in for them, they are not green")
        else:
            reasons.append("NO_CI_EVIDENCE: a non-failed check is not in a successful state alongside the billing-blocked jobs")
    elif not checks or not all((c.get("conclusion") or c.get("state") or "").upper() in OK for c in checks) \
            or not any((c.get("conclusion") or c.get("state") or "").upper() == "SUCCESS" for c in checks):
        ci = [m for t in texts for m in CI_RE.findall(t) if m[0] == head and m[2] == "PASS"]
        noci = [m for t in texts for m in NOCI_RE.findall(t) if m[0] == head and m[1] == "PASS"]
        if ci_unavailable and ci and not checks:
            facts["ci_exception"] = ci[-1][1]
        elif ci_exception and noci and not checks and no_hosted_ci(run, repo, base, head, p.get("files") or [], bool(p.get("isCrossRepository"))):
            facts["ci_exception"] = "no-hosted-ci"
        elif ci_exception and not checks and (local_first or {}).get(repo.lower()):
            lf_reasons, lf_wait, lf_facts = local_first_basis(run, repo, pr, base, head, p, meta, me, list(zip(mine, texts)), [c["body"] or "" for c in mine],
                                                              local_first[repo.lower()], time.time() if now is None else now)
            reasons.extend(lf_reasons)
            wait.extend(lf_wait)
            facts.update(lf_facts)
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
    "NOT_PRIVATE": (DENY, "none", "GITHUB_MERGE_ELIGIBILITY"),
    "LOCAL_CI_FAILED": (DENY, "author_fixes_ci", "ENGINEERING_GATE"),  # a local run that executed and failed is never overridden by a later pass
    "LOCAL_CI_MISSING": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"), "LOCAL_CI_STALE": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"),
    "LOCAL_CI_MISMATCH": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"), "LOCAL_CI_MALFORMED": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"),
    "LOCAL_CI_COMMANDS_MISMATCH": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"),
    "LOCAL_CI_CONTEXT_MISMATCH": (ASK, "agent_reruns_local_ci", "ENGINEERING_GATE"),
    "LOCAL_FIRST_SCOPE": (ASK, "founder_risk_decision", "ENGINEERING_GATE"),
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


def queue_report(run, repo, prs, owners, ci_unavailable=False, settings=None, version_policy=None, ci_exception=False, local_first=None):
    """Text plan for the owner: gate verdict per PR, in the order given. Print-only."""
    lines, first = [], True
    for n in prs:
        try:
            verdict, reasons, facts = evaluate(run, repo, n, owners, ci_unavailable, version_policy=version_policy, ci_exception=ci_exception, local_first=local_first)
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


def read_local_first(a):
    """The trust file is read only when the owner passed --ci-exception; an explicit path or an unreadable file is an error, not a skip."""
    if not a.ci_exception:
        return None
    return read_owner_file(a.local_first_config or LOCAL_FIRST_DEFAULT, bool(a.local_first_config), load_local_first,
                           "local-first CI config", os.path.dirname(os.path.expanduser(LOCAL_FIRST_DEFAULT)))


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
    LOCAL_HELP = f"owner trust file for the local-first CI basis (JSON, schema 1; only read with --ci-exception); default {LOCAL_FIRST_DEFAULT} when it exists"
    sub.choices["check"].add_argument("--local-first-config", help=LOCAL_HELP)
    sub.choices["verify"].add_argument("--head", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--repo", required=True)
    q.add_argument("--prs", required=True, help="comma-separated PR numbers, in the order to consider")
    q.add_argument("--owners", required=True)
    q.add_argument("--ci-unavailable", action="store_true")
    q.add_argument("--ci-exception", action="store_true")
    q.add_argument("--settings", action="append", help=SETTINGS_HELP)
    q.add_argument("--version-policy", help=POLICY_HELP)
    q.add_argument("--local-first-config", help=LOCAL_HELP)
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
            text = queue_report(run, a.repo, [int(x) for x in a.prs.split(",")], owners, a.ci_unavailable, a.settings, policy, a.ci_exception,
                                read_local_first(a))
            print(text)
            return 0 if all(re.match(r"#\d+ PASS ", l) for l in text.splitlines() if l.startswith("#")) else 1
        if a.cmd == "check":
            owners = [o.strip() for o in a.owners.split(",") if o.strip()]
            if not owners or not all(OWNER_RE.fullmatch(o) for o in owners):
                print(json.dumps({"verdict": "REFUSE", "reasons": ["BAD_OWNERS"]}))
                return 1
            policy = read_version_policy(a.version_policy or VERSION_POLICY_DEFAULT, bool(a.version_policy))
            verdict, reasons, facts = evaluate(run, a.repo, a.pr, owners, a.ci_unavailable, version_policy=policy, ci_exception=a.ci_exception,
                                               local_first=read_local_first(a))
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
