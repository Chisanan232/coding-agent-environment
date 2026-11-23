#!/usr/bin/env python3
"""Release-line registry, routing and audit CLI (SPE-180 design, slice SPE-181a).

  release-line.py registry validate|show        --repo O/R [--policy FILE]
  release-line.py registry propose --op add-successor|retire|eol|converge --repo O/R [--execute] [op flags]
  release-line.py route --kind feature|fix --version V --repo O/R --clone PATH
  release-line.py audit observe --repo O/R --clone PATH
  release-line.py audit import FILE --repo O/R [--execute]

Prints exactly one JSON envelope on stdout. Exit 0 ok/no-op, 1 refused/blocked, 2 usage, 3 invalid config.
The owner policy file (version-branches.json) is a trust anchor: it is read through owner-merge-gate.py's
read_owner_file and never written here. `propose` and `audit import` emit a candidate file, both sha256 digests and an
authority-record template into a proposals directory, and only with --execute; the owner installs the candidate.
Whether a branch may receive merges stays decided by `branches` alone; release_lines is validated here, ignored by the gate.
"""
import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("owner_merge_gate", HERE / "owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
LINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
RELEASE_SEGMENT = re.compile(r"(^|/)releases?(/|$)", re.I)
AUDIT_MAX_BYTES = 1 << 20
STATE_ROOT = "~/.local/state/coding-agent-environment/release-line"


class Refusal(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


class ConfigError(Exception):
    def __init__(self, code, detail="", errors=None):
        super().__init__(code)
        self.code, self.detail, self.errors = code, detail, errors or []


def now_utc():
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    t = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc) if epoch else datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(argv, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, type(e).__name__
    return p.returncode, p.stdout


def git(clone, *args):
    return run(["git", "-C", str(clone), *args])


def tool_revision():
    rc, out = git(HERE, "rev-parse", "HEAD")
    return out.strip() if rc == 0 and SHA_RE.fullmatch(out.strip()) else "unknown"


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def dump(obj):
    return (json.dumps(obj, indent=2) + "\n").encode()


# ---------------------------------------------------------------- registry

def find_entry(policy, repo):
    for key, entry in policy["repos"].items():
        if key.lower() == repo.lower():
            return key, entry
    return None, None


def validate_entry(entry):
    """Stricter checks for one repo entry's release_lines; returns a list of {line, code, detail}."""
    errs = []

    def err(line, code, detail):
        errs.append({"line": line, "code": code, "detail": detail})

    rl = entry.get("release_lines")
    if not isinstance(rl, dict):
        return [{"line": None, "code": "UNREGISTERED", "detail": "repo has no release_lines"}]
    branches = [b.get("branch") for b in entry.get("branches", []) if isinstance(b, dict)]
    if rl.get("schema") != 1 or type(rl.get("schema")) is not int:
        err(None, "BAD_SCHEMA", "release_lines.schema must be 1")
    audit = rl.get("audit")
    if not isinstance(audit, dict) or audit.get("status") not in ("PENDING", "COMPLETE"):
        err(None, "BAD_AUDIT", "audit.status must be PENDING or COMPLETE")
    elif audit["status"] == "COMPLETE" and not (isinstance(audit.get("source"), str) and re.fullmatch(r"[0-9a-f]{64}", str(audit.get("observed_sha256")))):
        err(None, "BAD_AUDIT", "a COMPLETE audit needs source and a 64-hex observed_sha256")
    if not isinstance(rl.get("channel"), str):
        err(None, "BAD_FIELD", "channel must be a string")
    if rl.get("publication_go") != "per-version":
        err(None, "BAD_FIELD", "publication_go must be per-version")
    if rl.get("milestone_tags") not in ("go-required", "pre-authorized"):
        err(None, "BAD_FIELD", "milestone_tags must be go-required or pre-authorized")
    if not (isinstance(rl.get("tag_format"), str) and "{version}" in rl["tag_format"]):
        err(None, "BAD_FIELD", "tag_format must contain {version}")
    cf = rl.get("candidate_format")
    if not (isinstance(cf, str) and "{version}" in cf and "{n}" in cf):
        err(None, "BAD_FIELD", "candidate_format must contain {version} and {n}")
    build = rl.get("build")
    if build is not None and not (isinstance(build, dict) and isinstance(build.get("argv"), list) and build["argv"]
                                  and all(isinstance(a, str) for a in build["argv"]) and isinstance(build.get("outputs"), list)):
        err(None, "BAD_FIELD", "build needs argv (non-empty list) and outputs (list)")
    if "promote/" not in (entry.get("future_head_prefixes") or []):
        err(None, "PROMOTE_PREFIX_MISSING", "promote/ must be listed in future_head_prefixes")
    lines = rl.get("lines")
    if not isinstance(lines, list) or not lines:
        err(None, "BAD_FIELD", "lines must be a non-empty list")
        return errs
    ids = [x.get("id") for x in lines if isinstance(x, dict)]
    majors = {}
    for ln in lines:
        lid = ln.get("id") if isinstance(ln, dict) else None
        if not isinstance(lid, str) or not LINE_ID_RE.fullmatch(lid) or ids.count(lid) > 1:
            err(lid, "BAD_LINE_ID", "line id missing, malformed or duplicated")
            continue
        kind = ln.get("kind")
        if kind not in ("major", "successor", "version"):
            err(lid, "BAD_KIND", "kind must be major, successor or version")
            continue
        if kind == "major":
            if type(ln.get("major")) is not int or ln["major"] < 0:
                err(lid, "BAD_FIELD", "major line needs an integer major")
            elif ln["major"] in majors:
                err(lid, "DUPLICATE_MAJOR", f"major {ln['major']} already has line {majors[ln['major']]}")
            else:
                majors[ln["major"]] = lid
        elif not (isinstance(ln.get("version"), str) and VERSION_RE.fullmatch(ln["version"])):
            err(lid, "BAD_FIELD", "version must be MAJOR.MINOR.PATCH")
        ib = ln.get("integration_branch")
        if not isinstance(ib, str):
            err(lid, "BAD_FIELD", "integration_branch missing")
        elif RELEASE_SEGMENT.search(ib):
            err(lid, "RELEASE_SEGMENT", f"{ib}: the gate refuses any branch with a release segment; use integration/v<major>.x")
        elif ib not in branches:
            err(lid, "NOT_MERGE_ELIGIBLE", f"{ib} is not listed in branches, so the gate would not merge into it")
        pb = ln.get("publication_base")
        if not (isinstance(pb, dict) and isinstance(pb.get("branch"), str) and pb.get("kind") in ("default", "maintenance")):
            err(lid, "BAD_FIELD", "publication_base needs branch and kind default|maintenance")
        elif pb["kind"] == "maintenance" and pb["branch"] in branches:
            err(lid, "MAINTENANCE_BASE_LISTED", f"{pb['branch']} must not be in branches (promotion never goes through the gate)")
        if ln.get("support") not in ("supported", "eol"):
            err(lid, "BAD_FIELD", "support must be supported or eol")
        if kind == "successor":
            pred = ln.get("predecessor")
            if not (isinstance(pred, dict) and pred.get("line") in ids and pred.get("line") != lid
                    and VERSION_RE.fullmatch(str(pred.get("version"))) and isinstance(pred.get("fork_ref"), str)
                    and SHA_RE.fullmatch(str(pred.get("fork_sha")))):
                err(lid, "BAD_PREDECESSOR", "predecessor needs an existing line, version, fork_ref and 40-hex fork_sha")
            state = ln.get("state")
            if state not in ("active", "converged", "retired"):
                err(lid, "BAD_STATE", "successor state must be active, converged or retired")
            elif state == "converged" and ln.get("converged_into") not in ids:
                err(lid, "BAD_STATE", "a converged line needs converged_into naming an existing line")
    return errs


def load_registry(path):
    """Read the owner policy through the trust-anchor reader; returns (policy dict, sha256 of the file bytes)."""
    try:
        policy = omg.read_owner_file(path, True, lambda obj: obj, "version policy")
    except FileNotFoundError:
        raise ConfigError("POLICY_MISSING", "owner policy file not found")
    except (ValueError, OSError) as e:
        raise ConfigError("POLICY_UNREADABLE", str(e))
    raw = pathlib.Path(os.path.realpath(os.path.expanduser(path))).read_bytes()
    if not isinstance(policy, dict) or not isinstance(policy.get("repos"), dict):
        raise ConfigError("POLICY_INVALID", "policy must be an object with a repos object")
    return policy, sha256_bytes(raw)


def check_policy(policy, repo):
    """Full validation of one repo: the gate's loader plus the stricter release_lines rules; raises ConfigError."""
    key, entry = find_entry(policy, repo)
    if entry is None or not isinstance(entry.get("release_lines"), dict):
        raise Refusal("UNREGISTERED", f"{repo} has no release_lines in the owner policy")
    errs = validate_entry(entry)
    try:
        omg.load_version_policy(policy)
    except ValueError as e:
        errs.append({"line": None, "code": "REJECTED_BY_GATE_LOADER", "detail": str(e)})
    if errs:
        raise ConfigError("REGISTRY_INVALID", f"{len(errs)} problem(s)", errs)
    return key, entry


def require_ready(entry):
    if entry["release_lines"]["audit"]["status"] != "COMPLETE":
        raise Refusal("AUDIT_PENDING", "run `audit import FILE` and have the owner install the candidate before mutating commands")


def line_by_id(entry, lid):
    for ln in entry["release_lines"]["lines"]:
        if ln["id"] == lid:
            return ln
    raise Refusal("UNKNOWN_LINE", f"no line {lid}")


# ---------------------------------------------------------------- proposals

def proposal_dir(args):
    d = pathlib.Path(os.path.expanduser(args.state_dir or f"{STATE_ROOT}/{args.repo.replace('/', '__')}")) / "proposals"
    return d


def emit_proposal(args, res, policy, baseline_sha, candidate, tag, summary):
    """Validate the candidate with both validators, then report digests (and with --execute write files). Never touches the owner file."""
    repo = args.repo
    try:
        check_policy(candidate, repo)
        loader = "PASS"
    except (ConfigError, Refusal) as e:
        detail = e.errors if isinstance(e, ConfigError) and e.errors else e.detail
        raise Refusal("INVALID_CANDIDATE", json.dumps(detail))
    body = dump(candidate)
    if len(body) > omg.POLICY_MAX_BYTES:
        raise Refusal("INVALID_CANDIDATE", f"candidate exceeds {omg.POLICY_MAX_BYTES} bytes")
    cand_sha = sha256_bytes(body)
    d = proposal_dir(args)
    stem = f"{repo.replace('/', '__')}-{tag}-{cand_sha[:12]}"
    cand_path, auth_path = d / f"{stem}.candidate.json", d / f"{stem}.authority.json"
    authority = {
        "schema": 1, "authority": "<owner: cite the founder instruction that approves this change; this template does not manufacture consent>",
        "scope": f"release_lines change for {repo}: {summary}", "baseline": os.path.expanduser(args.policy or omg.VERSION_POLICY_DEFAULT),
        "baseline_sha256": baseline_sha, "candidate": str(cand_path), "candidate_sha256": cand_sha, "source_commit": tool_revision(),
        "canonical_loader": loader, "independent_review": "<pending>", "created_utc": now_utc()}
    evidence = {"baseline_sha256": baseline_sha, "candidate_sha256": cand_sha, "canonical_loader": loader, "summary": summary,
                "candidate_path": str(cand_path), "authority_path": str(auth_path)}
    if args.execute:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
        for p, data in ((cand_path, body), (auth_path, dump(authority))):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        res.step("emit", "WRITTEN", evidence)
        res.next_action = f"owner: review {cand_path}, complete the authority record, install the candidate as the policy file"
    else:
        res.step("emit", "DRY_PLAN", evidence)
        res.status, res.next_action = "NOT_RUN", "re-run with --execute to write the candidate and authority template"
    res.step("authority_template", "TEMPLATE", authority)


def propose(args, res):
    policy, baseline_sha = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    key, entry = check_policy(policy, args.repo)
    require_ready(entry)
    res.step("registry", "VALID", {"baseline_sha256": baseline_sha})
    cand = copy.deepcopy(policy)
    centry = cand["repos"][key]
    rl = centry["release_lines"]
    op = args.op
    if op == "add-successor":
        need = ("version", "predecessor", "predecessor_version", "fork_ref", "fork_sha")
        missing = [n for n in need if not getattr(args, n)]
        if missing:
            raise Refusal("MISSING_FLAGS", "add-successor needs " + ", ".join("--" + m.replace("_", "-") for m in missing))
        pred = line_by_id(entry, args.predecessor)
        lid, branch = f"v{args.version}", args.branch or f"integration/v{args.version}"
        if any(x["id"] == lid for x in rl["lines"]):
            raise Refusal("LINE_EXISTS", f"line {lid} already registered")
        centry["branches"].append({"branch": branch, "version": args.version})
        centry.setdefault("future_head_prefixes", [])
        if f"v{args.version}/" not in centry["future_head_prefixes"]:
            centry["future_head_prefixes"].append(f"v{args.version}/")
        rl["lines"].append({
            "id": lid, "kind": "successor", "version": args.version, "integration_branch": branch,
            "predecessor": {"line": pred["id"], "version": args.predecessor_version, "fork_ref": args.fork_ref, "fork_sha": args.fork_sha},
            "publication_base": copy.deepcopy(pred["publication_base"]), "support": "supported", "state": "active"})
        summary = f"add successor {lid} on {branch} after {pred['id']}"
    else:
        if not args.line:
            raise Refusal("MISSING_FLAGS", f"{op} needs --line")
        ln = next((x for x in rl["lines"] if x["id"] == args.line), None)
        if ln is None:
            raise Refusal("UNKNOWN_LINE", f"no line {args.line}")
        if op == "eol":
            ln["support"] = "eol"
        elif ln["kind"] != "successor":
            raise Refusal("NOT_A_SUCCESSOR", f"{op} applies to successor lines only")
        elif op == "retire":
            ln["state"] = "retired"
        else:
            if not args.into:
                raise Refusal("MISSING_FLAGS", "converge needs --into")
            ln["state"], ln["converged_into"] = "converged", args.into
        summary = f"{op} {args.line}" + (f" into {args.into}" if op == "converge" else "")
    emit_proposal(args, res, policy, baseline_sha, cand, op, summary)


# ---------------------------------------------------------------- git facts

def fmt_re(fmt):
    pat = re.escape(fmt).replace(r"\{version\}", r"(?P<v>\d+\.\d+\.\d+)").replace(r"\{n\}", r"(?P<n>\d+)")
    return re.compile(pat + "$")


def branch_ref(clone, branch):
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if git(clone, "rev-parse", "--verify", "-q", ref + "^{commit}")[0] == 0:
            return ref
    return None


def tag_names(clone, *extra):
    rc, out = git(clone, "tag", "--list", *extra)
    return out.split() if rc == 0 else []


def unshipped_candidates(clone, rl, ln):
    """Candidate versions whose rc tag is on the line branch while no final tag exists anywhere."""
    ref = branch_ref(clone, ln["integration_branch"])
    if ref is None:
        return []
    final, rc = fmt_re(rl["tag_format"]), fmt_re(rl["candidate_format"])
    shipped = {m.group("v") for t in tag_names(clone) if (m := final.match(t))}
    on_branch = {m.group("v") for t in tag_names(clone, "--merged", ref) if (m := rc.match(t))}
    return sorted(on_branch - shipped, key=lambda v: tuple(map(int, v.split("."))))


def chain_of(rl, major_line):
    out = []
    for ln in rl["lines"]:
        cur, seen = ln, set()
        while cur["kind"] == "successor" and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = next(x for x in rl["lines"] if x["id"] == cur["predecessor"]["line"])
        if cur["id"] == major_line["id"] and ln["kind"] == "successor":
            out.append(ln)
    return out


def route(args, res):
    if not args.clone:
        raise SystemExit(usage_error("route needs --clone"))
    if not VERSION_RE.fullmatch(args.version):
        raise SystemExit(usage_error("--version must be MAJOR.MINOR.PATCH"))
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    rl = entry["release_lines"]
    res.step("audit", rl["audit"]["status"], {})
    major = int(args.version.split(".")[0])

    def usable(ln):
        if ln["support"] == "eol":
            raise Refusal("EOL_LINE", f"line {ln['id']} is end-of-life")
        if ln.get("state") == "retired":
            raise Refusal("LINE_RETIRED", f"line {ln['id']} is retired")
        if ln.get("state") == "converged":
            return usable(line_by_id(entry, ln["converged_into"]))
        return ln

    def done(ln, why):
        ln = usable(ln)
        res.step("route", "TARGET", {"branch": ln["integration_branch"], "line": ln["id"], "reason": why})
        res.next_action = f"open the PR into {ln['integration_branch']}"

    exact = next((x for x in rl["lines"] if x["kind"] != "major" and x.get("version") == args.version), None)
    if exact is not None:
        return done(exact, "line registered for this exact version")
    mline = next((x for x in rl["lines"] if x["kind"] == "major" and x["major"] == major), None)
    if mline is None:
        raise Refusal("NO_LINE", f"no release line for major {major}")
    frozen = unshipped_candidates(args.clone, rl, mline)
    if args.kind == "fix" or not frozen:
        return done(mline, "major line" + (" (fix for a frozen candidate)" if frozen else ""))
    active = [x for x in chain_of(rl, mline) if x["support"] == "supported" and x["state"] == "active"]
    if active:
        tip = max(active, key=lambda x: tuple(map(int, x["version"].split("."))))
        return done(tip, f"major line is frozen by unshipped candidate {frozen[-1]}; feature goes to the successor chain tip")
    res.step("route", "NEEDS_SUCCESSOR", {"line": mline["id"], "frozen_by": frozen[-1], "requested_version": args.version})
    res.status = "BLOCKED"
    res.next_action = (f"no successor exists after frozen candidate {frozen[-1]}; owner registers one with "
                       "`registry propose --op add-successor` (automated `successor ensure` is planned for SPE-182). Only this feature is blocked")


def observe(args, res):
    if not args.clone:
        raise SystemExit(usage_error("audit observe needs --clone"))
    clone = args.clone
    if git(clone, "rev-parse", "--git-dir")[0] != 0:
        raise Refusal("NOT_A_CLONE", "--clone is not a git checkout")
    rc, out = git(clone, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin")
    names = sorted({r.split("/", 2)[2] if r.startswith("refs/heads/") else r.split("/", 3)[3] for r in out.split()} - {"HEAD"})
    res.step("branches", "OBSERVED", {"count": len(names), "names": names[:200]})
    tags = tag_names(clone)
    kinds = {"rc": re.compile(r"-rc\.\d+$"), "final": re.compile(r"^v?\d+\.\d+\.\d+$"), "milestone": re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d")}
    groups = {k: [t for t in tags if p.search(t) and (k != "milestone" or not kinds["final"].match(t))] for k, p in kinds.items()}
    groups["other"] = [t for t in tags if not any(t in g for g in groups.values())]
    res.step("tags", "OBSERVED", {"count": len(tags), **{k: {"count": len(v), "examples": v[:10]} for k, v in groups.items()}})
    rc, out = git(clone, "ls-tree", "-r", "--name-only", "HEAD")
    doc_re = re.compile(r"(^|/)(RELEASING|RELEASE[-_A-Z]*|VERSIONING|BRANCHING|CHANGELOG)[^/]*\.md$|^docs/release[^/]*", re.I)
    res.step("doc_paths", "OBSERVED", {"paths": [p for p in out.splitlines() if doc_re.search(p)][:50]} if rc == 0 else {"error": "ls-tree failed"})
    rc, out = run(["gh", "pr", "list", "--repo", args.repo, "--state", "all", "--limit", "1000",
                   "--json", "number,baseRefName,headRefName,state,mergeCommit"])
    try:
        prs = json.loads(out) if rc == 0 else None
    except ValueError:
        prs = None
    if not isinstance(prs, list):
        res.step("pr_facts", "NOT_RUN", {"reason": "gh unavailable or returned no list; git facts above are still valid"})
        return
    bases = {}
    for p in prs:
        bases[p.get("baseRefName")] = bases.get(p.get("baseRefName"), 0) + 1
    promos = [{"number": p.get("number"), "base": p.get("baseRefName"), "head": p.get("headRefName")}
              for p in prs if str(p.get("headRefName", "")).startswith("promote/") and p.get("state") == "MERGED"]
    res.step("pr_base_counts", "OBSERVED", {"total": len(prs), "bases": dict(sorted(bases.items(), key=lambda kv: str(kv[0])))})
    res.step("promotion_merges", "OBSERVED", {"count": len(promos), "prs": promos[:50]})
    res.next_action = "write the audit JSON (docs/RELEASE-LINES.md) from these facts, then `audit import FILE`"


def audit_import(args, res):
    path = pathlib.Path(args.file)
    try:
        raw = path.read_bytes()
        if len(raw) > AUDIT_MAX_BYTES:
            raise ValueError("audit file is too large")
        audit = json.loads(raw, object_pairs_hook=omg._no_duplicate_keys)
    except (OSError, ValueError) as e:
        raise ConfigError("AUDIT_UNREADABLE", str(e))
    if not (isinstance(audit, dict) and audit.get("schema") == 1 and isinstance(audit.get("release_lines"), dict)
            and isinstance(audit.get("repo"), str) and audit["repo"].lower() == args.repo.lower()):
        raise ConfigError("AUDIT_INVALID", "audit needs schema 1, a repo equal to --repo and a release_lines object")
    obs_sha = sha256_bytes(raw)
    try:
        policy, baseline_sha = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    except ConfigError as e:
        if e.code != "POLICY_MISSING":
            raise
        policy, baseline_sha = {"schema": 1, "repos": {}}, "0" * 64
    cand = copy.deepcopy(policy)
    key, entry = find_entry(cand, args.repo)
    if entry is None:
        key, entry = args.repo, {"branches": [], "future_head_prefixes": []}
        cand["repos"][key] = entry
    for b in audit.get("branches", []):
        if isinstance(b, dict) and b.get("branch") not in [x.get("branch") for x in entry["branches"]]:
            entry["branches"].append(b)
    for pre in audit.get("future_head_prefixes", []):
        if pre not in entry.setdefault("future_head_prefixes", []):
            entry["future_head_prefixes"].append(pre)
    rl = copy.deepcopy(audit["release_lines"])
    rl["audit"] = {"status": "COMPLETE", "source": path.name, "observed_sha256": obs_sha}
    entry["release_lines"] = rl
    res.step("audit_file", "VALID", {"observed_sha256": obs_sha})
    emit_proposal(args, res, policy, baseline_sha, cand, "audit-import", f"register release_lines from audit {path.name}")


# ---------------------------------------------------------------- registry commands

def registry_validate(args, res):
    policy, sha = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    rl = entry["release_lines"]
    res.step("registry", "VALID", {"policy_sha256": sha, "lines": len(rl["lines"])})
    res.step("audit", rl["audit"]["status"], {})
    res.next_action = "none" if rl["audit"]["status"] == "COMPLETE" else "AUDIT_PENDING: mutating commands refuse until an audit is imported and installed"


def registry_show(args, res):
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    rl = entry["release_lines"]
    branches = {b["branch"] for b in entry["branches"]}
    res.step("audit", rl["audit"]["status"], dict(rl["audit"]))
    for ln in rl["lines"]:
        res.step(f"line:{ln['id']}", ln.get("state", "active"), {
            "kind": ln["kind"], "integration_branch": ln["integration_branch"], "merge_eligible": ln["integration_branch"] in branches,
            "support": ln["support"], "publication_base": ln["publication_base"],
            "predecessor": ln.get("predecessor"), "converged_into": ln.get("converged_into")})


# ---------------------------------------------------------------- envelope + CLI

class Result:
    def __init__(self, command, repo, key):
        self.env = {"schema": 1, "tool_revision": tool_revision(), "command": command, "repo": repo,
                    "idempotency_key": key, "status": "PASS", "steps": [], "next_action": "none"}

    status = property(lambda s: s.env["status"], lambda s, v: s.env.__setitem__("status", v))
    next_action = property(lambda s: s.env["next_action"], lambda s, v: s.env.__setitem__("next_action", v))
    steps = property(lambda s: s.env["steps"])

    def step(self, step, state, evidence):
        self.env["steps"].append({"step": step, "state": state, "evidence": evidence})


def usage_error(msg):
    print(msg, file=sys.stderr)
    return 2


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True, help="OWNER/REPO, the registry key")
    common.add_argument("--clone", help="local clone used for git facts")
    common.add_argument("--policy", help="owner policy file (default: the version-branches.json trust file)")
    common.add_argument("--go-dir", help="reserved for the release-go records of later slices")
    common.add_argument("--state-dir", help="journal/proposals root (default under ~/.local/state)")
    common.add_argument("--execute", action="store_true", help="write proposal files; without it, a dry plan")
    p = argparse.ArgumentParser(prog="release-line.py", description=__doc__.split("\n")[0])
    top = p.add_subparsers(dest="group", required=True)
    reg = top.add_parser("registry").add_subparsers(dest="cmd", required=True)
    reg.add_parser("validate", parents=[common])
    reg.add_parser("show", parents=[common])
    pr = reg.add_parser("propose", parents=[common])
    pr.add_argument("--op", required=True, choices=["add-successor", "retire", "eol", "converge"])
    for flag in ("version", "predecessor", "predecessor-version", "fork-ref", "fork-sha", "branch", "line", "into"):
        pr.add_argument("--" + flag)
    au = top.add_parser("audit").add_subparsers(dest="cmd", required=True)
    au.add_parser("observe", parents=[common])
    ai = au.add_parser("import", parents=[common])
    ai.add_argument("file")
    rt = top.add_parser("route", parents=[common])
    rt.add_argument("--kind", required=True, choices=["feature", "fix"])
    rt.add_argument("--version", required=True)
    return p


HANDLERS = {("registry", "validate"): registry_validate, ("registry", "show"): registry_show, ("registry", "propose"): propose,
            ("audit", "observe"): observe, ("audit", "import"): audit_import, ("route", None): route}


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    cmd = getattr(args, "cmd", None)
    name = f"{args.group} {cmd}" if cmd else args.group
    detail = "|".join(str(v) for k, v in sorted(vars(args).items()) if k not in ("execute", "state_dir", "go_dir", "clone", "policy") and v)
    key = f"{name.replace(' ', '-')}:{args.repo}:{hashlib.sha256(detail.encode()).hexdigest()[:12]}"
    res = Result(name, args.repo, key)
    code = 0
    try:
        HANDLERS[(args.group, cmd)](args, res)
        code = {"PASS": 0, "NOT_RUN": 0, "DEFERRED": 0}.get(res.status, 1)
    except Refusal as e:
        res.status, code = "BLOCKED", 1
        res.step("refused", e.code, {"detail": e.detail})
        res.next_action = "UNREGISTERED: add release_lines to the owner policy (`audit import`)" if e.code == "UNREGISTERED" else f"resolve {e.code}"
    except ConfigError as e:
        res.status, code = "FAIL", 3
        res.step("config", e.code, {"detail": e.detail})
        for x in e.errors:
            res.step(f"line:{x['line']}" if x["line"] else "registry", x["code"], {"detail": x["detail"]})
        res.next_action = "fix the owner policy or input file; agents never edit the owner policy"
    except SystemExit as e:
        return int(e.code or 2)
    print(json.dumps(res.env, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
