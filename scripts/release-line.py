#!/usr/bin/env python3
"""Release-line registry, routing, audit and successor-train CLI (SPE-180 design, slices SPE-181a and SPE-182).

  release-line.py registry validate|show        --repo O/R [--policy FILE]
  release-line.py registry propose --op add-successor|retire|eol|converge --repo O/R [--execute] [op flags]
  release-line.py route --kind feature|fix --version V --repo O/R --clone PATH
  release-line.py audit observe --repo O/R --clone PATH
  release-line.py audit import FILE --repo O/R [--execute]
  release-line.py successor ensure --predecessor V [--from REF] [--version V] --repo O/R --clone PATH [--execute]
  release-line.py guard --base BRANCH --head SHA --repo O/R --clone PATH
  release-line.py train integrate --from LINE --into LINE --repo O/R --clone PATH [--execute]
  release-line.py train converge --line LINE --repo O/R --clone PATH [--execute]

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
import stat
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("owner_merge_gate", HERE / "owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

VERSION_PAT = r"\d+\.\d+(?:\.\d+)?"  # 3-part, or 2-part for MVP products and legacy per-version branches
VERSION_RE = re.compile(VERSION_PAT, re.ASCII)
LINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
RELEASE_SEGMENT = re.compile(r"(^|/)releases?(/|$)", re.I)
AUDIT_MAX_BYTES = 1 << 20
STATE_ROOT = "~/.local/state/coding-agent-environment/release-line"


class Refusal(Exception):
    def __init__(self, code, detail="", status="BLOCKED"):
        super().__init__(code)
        self.code, self.detail, self.status = code, detail, status


class ConfigError(Exception):
    def __init__(self, code, detail="", errors=None):
        super().__init__(code)
        self.code, self.detail, self.errors = code, detail, errors or []


def now_utc():
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    t = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc) if epoch else datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(argv, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, type(e).__name__
    return p.returncode, p.stdout


def git(clone, *args):
    # The clone is agent-supplied: never run its fsmonitor or hooks, never page.
    return run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(clone), *args])


def vkey(v):
    parts = [int(x) for x in v.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


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


def format_ok(fmt, placeholders):
    rest = fmt
    for ph in placeholders:
        if rest.count(ph) != 1:
            return False
        rest = rest.replace(ph, "")
    return "{" not in rest and "}" not in rest and bool(fmt) and not fmt.startswith("-")


def fmt_re(fmt):
    """Regex for a validated tag format; literal parts are escaped, so this cannot raise re.error."""
    out = [re.escape(x) for x in re.split(r"\{version\}|\{n\}", fmt)]
    holes = re.findall(r"\{version\}|\{n\}", fmt)
    pat = out[0]
    for hole, lit in zip(holes, out[1:]):
        pat += (f"(?P<v>{VERSION_PAT})" if hole == "{version}" else r"(?P<n>\d+)") + lit
    return re.compile(pat, re.ASCII)


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
    tf, cf = rl.get("tag_format"), rl.get("candidate_format")
    if not (isinstance(tf, str) and format_ok(tf, ("{version}",))):
        err(None, "BAD_FIELD", "tag_format must contain exactly one {version} and nothing else templated")
    if not (isinstance(cf, str) and format_ok(cf, ("{version}", "{n}"))):
        err(None, "BAD_FIELD", "candidate_format must contain exactly one {version} and one {n} and nothing else templated")
    if isinstance(tf, str) and isinstance(cf, str) and format_ok(tf, ("{version}",)) and format_ok(cf, ("{version}", "{n}")) \
            and (fmt_re(tf).fullmatch(cf.replace("{version}", "1.2.3").replace("{n}", "1")) or fmt_re(cf).fullmatch(tf.replace("{version}", "1.2.3"))):
        err(None, "BAD_FIELD", "a candidate tag must never match tag_format (formats are ambiguous)")
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
    # A publication base of any line is protected: the gate must never merge feature work straight into it.
    bases = {x["publication_base"]["branch"] for x in lines if isinstance(x, dict) and isinstance(x.get("publication_base"), dict)
             and isinstance(x["publication_base"].get("branch"), str)} | {"main", "master"}
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
            if ln.get("predecessor") is not None:
                err(lid, "BAD_FIELD", "a major line has predecessor null")
        elif not (isinstance(ln.get("version"), str) and VERSION_RE.fullmatch(ln["version"])):
            err(lid, "BAD_FIELD", "version must be MAJOR.MINOR or MAJOR.MINOR.PATCH")
        ib = ln.get("integration_branch")
        if not isinstance(ib, str):
            err(lid, "BAD_FIELD", "integration_branch missing")
        elif RELEASE_SEGMENT.search(ib):
            err(lid, "RELEASE_SEGMENT", f"{ib}: the gate refuses any branch with a release segment; use integration/v<major>.x")
        elif ib in bases:
            err(lid, "PROTECTED_BRANCH", f"{ib} is a publication base or default branch and cannot be an integration branch")
        elif ib not in branches:
            err(lid, "NOT_MERGE_ELIGIBLE", f"{ib} is not listed in branches, so the gate would not merge into it")
        pb = ln.get("publication_base")
        if not (isinstance(pb, dict) and isinstance(pb.get("branch"), str) and pb.get("kind") in ("default", "maintenance")):
            err(lid, "BAD_FIELD", "publication_base needs branch and kind default|maintenance")
        elif pb["kind"] == "maintenance" and pb["branch"] in branches:
            err(lid, "MAINTENANCE_BASE_LISTED", f"{pb['branch']} must not be in branches (promotion never goes through the gate)")
        elif pb["kind"] == "default" and pb["branch"] in branches:
            err(lid, "DEFAULT_BASE_LISTED", f"{pb['branch']} is the publication base and must not be listed as an integration branch")
        if ln.get("support") not in ("supported", "eol"):
            err(lid, "BAD_FIELD", "support must be supported or eol")
        if kind == "successor":
            pred = ln.get("predecessor")
            fr = pred.get("fork_ref") if isinstance(pred, dict) else None
            if not (isinstance(pred, dict) and pred.get("line") in ids and pred.get("line") != lid
                    and VERSION_RE.fullmatch(str(pred.get("version"))) and isinstance(fr, str)
                    and omg.VERSION_REF_RE.fullmatch(fr) and ".." not in fr and not omg.bad_ref_parts(fr) and not fr.endswith("/")
                    and SHA_RE.fullmatch(str(pred.get("fork_sha")))):
                err(lid, "BAD_PREDECESSOR", "predecessor needs an existing line, version, a valid fork_ref and 40-hex fork_sha")
            elif isinstance(ln.get("version"), str) and VERSION_RE.fullmatch(ln["version"]) and vkey(pred["version"]) >= vkey(ln["version"]):
                err(lid, "BAD_PREDECESSOR", "predecessor.version must be lower than the line's version")
            state = ln.get("state")
            if state not in ("active", "converged", "retired"):
                err(lid, "BAD_STATE", "successor state must be active, converged or retired")
            elif state == "converged":
                tgt = next((x for x in lines if isinstance(x, dict) and x.get("id") == ln.get("converged_into")), None)
                if tgt is None or tgt.get("id") == lid:
                    err(lid, "BAD_STATE", "a converged line needs converged_into naming a different existing line")
                elif tgt.get("state") in ("converged", "retired"):
                    err(lid, "BAD_STATE", "converged_into must name a line that is not itself converged or retired")
    cross_checks(lines, err)
    return errs


def cross_checks(lines, err):
    by_id = {x["id"]: x for x in lines if isinstance(x, dict) and isinstance(x.get("id"), str)}
    for field, code in (("integration_branch", "DUPLICATE_BRANCH"), ("version", "DUPLICATE_VERSION")):
        seen = {}
        for x in by_id.values():
            v = x.get(field)
            if isinstance(v, str) and not (field == "version" and x.get("kind") == "major"):
                if v in seen:
                    err(x["id"], code, f"{field} {v} is also used by line {seen[v]}")
                seen[v] = x["id"]
    for x in by_id.values():
        if x.get("kind") != "successor":
            continue
        cur, seen = x, set()
        while isinstance(cur, dict) and cur.get("kind") == "successor" and cur["id"] not in seen:
            seen.add(cur["id"])
            pred = cur.get("predecessor")
            cur = by_id.get(pred.get("line")) if isinstance(pred, dict) else None
        if not (isinstance(cur, dict) and cur.get("kind") == "major"):
            err(x["id"], "BAD_PREDECESSOR", "predecessor chain must be acyclic and rooted at a major line")


def load_registry(path):
    """Read the owner policy through the trust-anchor reader; returns (policy dict, sha256 of the file bytes)."""
    try:
        policy = omg.read_owner_file(path, True, lambda obj: obj, "version policy")
    except FileNotFoundError:
        raise ConfigError("POLICY_MISSING", "owner policy file not found")
    except (ValueError, OSError) as e:
        raise ConfigError("POLICY_UNREADABLE", str(e))
    if not isinstance(policy, dict) or not isinstance(policy.get("repos"), dict):
        raise ConfigError("POLICY_INVALID", "policy must be an object with a repos object")
    # The trust-anchor reader returns only the parsed object. Digest bytes are read once more through a no-follow descriptor and
    # accepted only if they parse to exactly the object it validated, so the digest cannot describe different content.
    try:
        fd = os.open(os.path.realpath(os.path.expanduser(path)), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            raw = f.read(omg.POLICY_MAX_BYTES + 1)
        same = len(raw) <= omg.POLICY_MAX_BYTES and json.loads(raw, object_pairs_hook=omg._no_duplicate_keys) == policy
    except (OSError, ValueError):
        same = False
    if not same:
        raise ConfigError("POLICY_CHANGED_DURING_READ", "owner policy changed while it was being read; retry")
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
    """Proposals directory; never a symlink and never inside the owner-config root (a proposal must not land beside a trust file)."""
    base = pathlib.Path(os.path.expanduser(args.state_dir or f"{STATE_ROOT}/{args.repo.replace('/', '__')}"))
    d = base / "proposals"
    owner_root = os.path.realpath(os.path.expanduser(os.path.dirname(omg.VERSION_POLICY_DEFAULT)))
    if os.path.islink(base) or os.path.islink(d):
        raise Refusal("STATE_DIR_UNSAFE", "the state directory and its proposals directory must not be symlinks")
    real = os.path.realpath(d)
    if os.path.commonpath([owner_root, real]) == owner_root:
        raise Refusal("STATE_DIR_UNSAFE", f"the state directory must not be inside {owner_root}")
    return d


def write_private(d, files):
    """Write (name, bytes) files into d: directory 0700 through a no-follow descriptor, each file via temp + rename at 0600."""
    d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    try:
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Refusal("STATE_DIR_UNSAFE", "proposals directory is not a plain directory")
    try:
        if os.fstat(dfd).st_uid != os.getuid():
            raise Refusal("STATE_DIR_UNSAFE", "proposals directory is not owned by the caller")
        os.fchmod(dfd, 0o700)
        for name, data in files:
            tmp = f".{name}.{os.getpid()}.tmp"
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def emit_proposal(args, res, policy, baseline_sha, candidate, tag, summary):
    """Validate the candidate with both validators, then report digests (and with --execute write files). Never touches the owner file."""
    repo = args.repo
    try:
        check_policy(candidate, repo)
        loader = "PASS"
    except (ConfigError, Refusal) as e:
        detail = e.errors if isinstance(e, ConfigError) and e.errors else e.detail
        raise Refusal("INVALID_CANDIDATE", json.dumps(detail))
    adds = gate_additions(policy, candidate, repo)
    if adds["branches"] or adds["prefixes"]:
        summary += f"; GATE-RELEVANT ADDITIONS branches={adds['branches']} prefixes={adds['prefixes']}"
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
    evidence = {"gate_relevant_additions": adds, "baseline_sha256": baseline_sha, "candidate_sha256": cand_sha, "canonical_loader": loader, "summary": summary,
                "candidate_path": str(cand_path), "authority_path": str(auth_path)}
    if args.execute:
        write_private(d, ((cand_path.name, body), (auth_path.name, dump(authority))))
        res.step("emit", "WRITTEN", evidence)
        res.next_action = f"owner: review {cand_path}, complete the authority record, install the candidate as the policy file"
    else:
        res.step("emit", "DRY_PLAN", evidence)
        res.status, res.next_action = "NOT_RUN", "re-run with --execute to write the candidate and authority template"
    res.step("authority_template", "TEMPLATE", authority)


def gate_additions(old, new, repo):
    """Branches and head prefixes the candidate adds to the repo entry: the only part of a packet that changes what the gate approves."""
    _, o = find_entry(old, repo)
    _, n = find_entry(new, repo)
    ob = {b.get("branch") for b in (o or {}).get("branches", [])}
    op = set((o or {}).get("future_head_prefixes", []))
    return {"branches": [b["branch"] for b in n.get("branches", []) if b.get("branch") not in ob],
            "prefixes": [x for x in n.get("future_head_prefixes", []) if x not in op]}


def apply_add_successor(centry, pred, version, pred_version, fork_ref, fork_sha, branch=None):
    """Edit one candidate repo entry in place: list the successor's branch and head prefix and add its line; returns a summary."""
    if pred["support"] == "eol" or pred.get("state", "active") != "active":
        raise Refusal("PREDECESSOR_NOT_ACTIVE", f"line {pred['id']} is eol, retired or converged and cannot take a successor")
    rl = centry["release_lines"]
    lid, branch = f"v{version}", branch or f"integration/v{version}"
    if any(x["id"] == lid for x in rl["lines"]):
        raise Refusal("LINE_EXISTS", f"line {lid} already registered")
    centry["branches"].append({"branch": branch, "version": version})
    centry.setdefault("future_head_prefixes", [])
    if f"v{version}/" not in centry["future_head_prefixes"]:
        centry["future_head_prefixes"].append(f"v{version}/")
    rl["lines"].append({
        "id": lid, "kind": "successor", "version": version, "integration_branch": branch,
        "predecessor": {"line": pred["id"], "version": pred_version, "fork_ref": fork_ref, "fork_sha": fork_sha},
        "publication_base": copy.deepcopy(pred["publication_base"]), "support": "supported", "state": "active"})
    return f"add successor {lid} on {branch} after {pred['id']}"


def noop(res, why):
    res.step("propose", "NO_OP", {"reason": why})
    res.next_action = "none: the registry already has this state; no packet emitted"


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
        summary = apply_add_successor(centry, line_by_id(entry, args.predecessor), args.version, args.predecessor_version,
                                      args.fork_ref, args.fork_sha, args.branch)
    else:
        if not args.line:
            raise Refusal("MISSING_FLAGS", f"{op} needs --line")
        ln = next((x for x in rl["lines"] if x["id"] == args.line), None)
        if ln is None:
            raise Refusal("UNKNOWN_LINE", f"no line {args.line}")
        if op != "eol" and ln["kind"] != "successor":
            raise Refusal("NOT_A_SUCCESSOR", f"{op} applies to successor lines only")
        if op in ("eol", "retire"):
            deps = [x["id"] for x in rl["lines"] if x["kind"] == "successor" and x["state"] == "active" and x["predecessor"]["line"] == ln["id"]]
            if deps:
                raise Refusal("HAS_ACTIVE_DEPENDENTS", f"active line(s) {deps} depend on {ln['id']} as predecessor")
        if op == "eol":
            if ln["support"] == "eol":
                return noop(res, f"{ln['id']} is already eol")
            ln["support"] = "eol"
        elif ln["kind"] != "successor":
            raise Refusal("NOT_A_SUCCESSOR", f"{op} applies to successor lines only")
        elif op == "retire":
            if ln["state"] == "retired":
                return noop(res, f"{ln['id']} is already retired")
            ln["state"] = "retired"
        else:
            if not args.into:
                raise Refusal("MISSING_FLAGS", "converge needs --into")
            if ln["state"] == "converged" and ln.get("converged_into") == args.into:
                return noop(res, f"{ln['id']} is already converged into {args.into}")
            ln["state"], ln["converged_into"] = "converged", args.into
        summary = f"{op} {args.line}" + (f" into {args.into}" if op == "converge" else "")
    emit_proposal(args, res, policy, baseline_sha, cand, op, summary)


# ---------------------------------------------------------------- git facts

def require_clone(clone):
    if not clone or not os.path.isdir(clone) or git(clone, "rev-parse", "--git-dir")[0] != 0:
        raise Refusal("NOT_A_CLONE", "--clone is not a git checkout")


def branch_ref(clone, branch):
    # Prefer the fetched remote-tracking ref: a stale local head must not decide routing.
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if git(clone, "rev-parse", "--verify", "-q", ref + "^{commit}")[0] == 0:
            return ref
    return None


def tag_names(clone, *extra):
    rc, out = git(clone, "tag", "--list", *extra)
    if rc != 0:
        # An unreadable tag list would make every line look unfrozen and route work onto a shipped line.
        raise Refusal("TAG_LIST_FAILED", "git tag --list failed in the clone; fetch tags and retry")
    return out.split()


def unshipped_candidates(clone, rl, ln):
    """Candidate versions whose rc tag is on the line branch while no final tag exists anywhere."""
    ref = branch_ref(clone, ln["integration_branch"])
    if ref is None:
        return []
    final, rc = fmt_re(rl["tag_format"]), fmt_re(rl["candidate_format"])
    shipped = {m.group("v") for t in tag_names(clone) if (m := final.fullmatch(t))}
    on_branch = {m.group("v") for t in tag_names(clone, "--merged", ref) if (m := rc.fullmatch(t))}
    return sorted(on_branch - shipped, key=vkey)


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
        raise SystemExit(usage_error("--version must be MAJOR.MINOR or MAJOR.MINOR.PATCH"))
    require_clone(args.clone)
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    rl = entry["release_lines"]
    res.step("audit", rl["audit"]["status"], {})
    major = int(args.version.split(".")[0])

    seen = set()

    def usable(ln):
        if ln["support"] == "eol":
            raise Refusal("EOL_LINE", f"line {ln['id']} is end-of-life")
        if ln.get("state") == "retired":
            raise Refusal("LINE_RETIRED", f"line {ln['id']} is retired")
        if ln.get("state") == "converged":
            if ln["id"] in seen:
                raise Refusal("CONVERGED_CYCLE", f"converged_into loops at {ln['id']}")
            seen.add(ln["id"])
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
    if branch_ref(args.clone, mline["integration_branch"]) is None:
        raise Refusal("BRANCH_NOT_IN_CLONE", f"{mline['integration_branch']} is not in the clone; fetch it first, a missing branch is not 'unfrozen'")
    frozen = unshipped_candidates(args.clone, rl, mline)
    if args.kind == "fix" or not frozen:
        return done(mline, "major line" + (" (fix for a frozen candidate)" if frozen else ""))
    active = [x for x in chain_of(rl, mline) if x["support"] == "supported" and x["state"] == "active"]
    if active:
        tip = max(active, key=lambda x: vkey(x["version"]))
        return done(tip, f"major line is frozen by unshipped candidate {frozen[-1]}; feature goes to the successor chain tip")
    res.step("route", "NEEDS_SUCCESSOR", {"line": mline["id"], "frozen_by": frozen[-1], "requested_version": args.version})
    res.status = "BLOCKED"
    res.next_action = (f"no successor exists after frozen candidate {frozen[-1]}; owner registers one with "
                       "`registry propose --op add-successor` (automated `successor ensure` is planned for SPE-182). Only this feature is blocked")


def observe(args, res):
    if not args.clone:
        raise SystemExit(usage_error("audit observe needs --clone"))
    clone = args.clone
    require_clone(clone)
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
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ValueError("audit file must be a regular file")
            raw = f.read(AUDIT_MAX_BYTES + 1)
        if len(raw) > AUDIT_MAX_BYTES:
            raise ValueError("audit file is too large")
        audit = json.loads(raw, object_pairs_hook=omg._no_duplicate_keys)
    except (OSError, ValueError) as e:
        raise ConfigError("AUDIT_UNREADABLE", str(e))
    if not (isinstance(audit, dict) and audit.get("schema") == 1 and isinstance(audit.get("release_lines"), dict)
            and isinstance(audit.get("repo"), str) and audit["repo"].lower() == args.repo.lower()):
        raise ConfigError("AUDIT_INVALID", "audit needs schema 1, a repo equal to --repo and a release_lines object")
    smuggled = [k for k in ("branches", "future_head_prefixes") if k in audit]
    if smuggled:
        raise Refusal("AUDIT_CARRIES_GATE_KEYS", f"audit file must not carry {smuggled}; an import adds only the imported lines' integration branches and promote/", "FAIL")
    lines = audit["release_lines"].get("lines")
    bad = [str(x.get("integration_branch")) for x in lines if isinstance(x, dict) and RELEASE_SEGMENT.search(str(x.get("integration_branch")))] if isinstance(lines, list) else []
    if bad:
        raise Refusal("RELEASE_SEGMENT", f"{bad}: the gate refuses any branch with a release segment; use integration/v<major>.x", "FAIL")
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
    rl = copy.deepcopy(audit["release_lines"])
    listed = [x.get("branch") for x in entry["branches"]]
    for ln in rl.get("lines") if isinstance(rl.get("lines"), list) else []:
        ib = ln.get("integration_branch") if isinstance(ln, dict) else None
        if isinstance(ib, str) and ib not in listed:
            listed.append(ib)
            entry["branches"].append({"branch": ib, "version": ln.get("version") or f"{ln.get('major')}.x"})
    if "promote/" not in entry.setdefault("future_head_prefixes", []):
        entry["future_head_prefixes"].append("promote/")
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


# ---------------------------------------------------------------- successor train (SPE-182)
# Durable state is git + GitHub only. Pushes create new refs and never force; every push is verified with ls-remote.

def fetch(clone):
    git(clone, "fetch", "-q", "--tags", "origin")


def ls_remote(clone, ref):
    rc, out = git(clone, "ls-remote", "origin", ref)
    if rc != 0:
        raise Refusal("REMOTE_UNREACHABLE", "git ls-remote origin failed")
    for row in out.splitlines():
        sha, _, name = row.partition("\t")
        if name == ref and SHA_RE.fullmatch(sha):
            return sha
    return None


def commit_of(clone, ref):
    rc, out = git(clone, "rev-parse", "--verify", "-q", ref + "^{commit}")
    return out.strip() if rc == 0 else None


def is_ancestor(clone, a, b):
    return git(clone, "merge-base", "--is-ancestor", a, b)[0] == 0


def push_new_ref(clone, sha, ref):
    git(clone, "push", "origin", f"{sha}:{ref}")
    return ls_remote(clone, ref) == sha


def find_prs(repo, head, base):
    rc, out = run(["gh", "pr", "list", "--repo", repo, "--head", head, "--base", base, "--state", "all", "--limit", "50",
                   "--json", "number,state,headRefName,baseRefName,url"])
    try:
        prs = json.loads(out) if rc == 0 else None
    except ValueError:
        prs = None
    if not isinstance(prs, list):
        raise Refusal("GH_UNAVAILABLE", "gh pr list failed; nothing was opened")
    prs = [p for p in prs if p.get("headRefName") == head and p.get("baseRefName") == base]
    return next((p for p in prs if p["state"] == "OPEN"), None), [p for p in prs if p["state"] == "MERGED"]


def ensure_pr(res, args, head, base, title, body, refuse_merged=False):
    """Reuse the one open PR head -> base or open it (--execute only). The merge itself belongs to the normal gate."""
    pr, merged = find_prs(args.repo, head, base)
    if pr is None and merged and refuse_merged:
        raise Refusal("NOT_INTEGRATED", f"PR #{merged[0]['number']} merged but the expected commit is not in {base}", "FAIL")
    if pr is not None:
        res.step("pr", "REUSED", {"number": pr["number"], "url": pr.get("url")})
        return pr
    if not args.execute:
        res.step("pr", "DRY_PLAN", {"head": head, "base": base})
        res.status = "NOT_RUN"
        return None
    rc, _ = run(["gh", "pr", "create", "--repo", args.repo, "--base", base, "--head", head, "--title", title, "--body", body])
    pr = find_prs(args.repo, head, base)[0] if rc == 0 else None
    if pr is None:
        raise Refusal("GH_PR_CREATE_FAILED", f"could not open {head} -> {base}")
    res.step("pr", "CREATED", {"number": pr["number"], "url": pr.get("url")})
    return pr


def trailers(kind, source_line, target_line, source):
    return (f"Release-Line-Source: {source}\nRelease-Line-Source-Line: {source_line}\n"
            f"Release-Line-Target-Line: {target_line}\nRelease-Line-Kind: {kind}\n")


def predecessor_line(rl, version):
    ln = next((x for x in rl["lines"] if x["kind"] != "major" and x.get("version") == version), None)
    ln = ln or next((x for x in rl["lines"] if x["kind"] == "major" and x["major"] == int(version.split(".")[0])), None)
    if ln is None:
        raise Refusal("NO_PREDECESSOR_LINE", f"no line carries predecessor version {version}")
    return ln


def active_chain(rl, line):
    out, cur = [], line
    while True:
        nxt = [x for x in rl["lines"] if x["kind"] == "successor" and x["state"] == "active" and x["predecessor"]["line"] == cur["id"]]
        if len(nxt) > 1:
            raise Refusal("AMBIGUOUS_CHAIN", f"{cur['id']} has several active successors: {[x['id'] for x in nxt]}")
        if not nxt:
            return out
        out.append(nxt[0])
        cur = nxt[0]


def descendants(rl, line):
    out, todo = [], [line["id"]]
    while todo:
        pid = todo.pop()
        for x in rl["lines"]:
            if x["kind"] == "successor" and x["state"] == "active" and x["predecessor"]["line"] == pid and x not in out:
                out.append(x)
                todo.append(x["id"])
    return out


def need_ref(clone, branch):
    """The fetched origin tip; local branches in a working clone go stale, so they are only a fallback."""
    ref = next((r for r in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}") if commit_of(clone, r)), None)
    if ref is None:
        raise Refusal("NO_BRANCH", f"{branch} is not present in the clone (fetched from origin)")
    return ref


def successor_ensure(args, res):
    if not args.clone:
        raise SystemExit(usage_error("successor ensure needs --clone"))
    if not VERSION_RE.fullmatch(args.predecessor):
        raise SystemExit(usage_error("--predecessor must be MAJOR.MINOR.PATCH"))
    policy, baseline_sha = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    key, entry = check_policy(policy, args.repo)
    require_ready(entry)
    rl = entry["release_lines"]
    major, minor, _ = args.predecessor.split(".")
    version = args.version or f"{major}.{int(minor) + 1}.0"
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(usage_error("--version must be MAJOR.MINOR.PATCH"))
    res.env["idempotency_key"] = f"succ:{args.repo}:{version}"
    pred = predecessor_line(rl, args.predecessor)
    listed = next((x for x in rl["lines"] if x["kind"] == "successor" and x.get("version") == version), None)
    if listed is not None and listed["predecessor"]["line"] != pred["id"]:
        raise Refusal("LINE_EXISTS", f"line {listed['id']} is registered with predecessor {listed['predecessor']['line']}")
    branch = args.branch or (listed or {}).get("integration_branch") or f"integration/v{version}"
    fetch(args.clone)
    pred_ref = need_ref(args.clone, pred["integration_branch"])
    fork_ref = args.from_ref
    if not fork_ref:
        rc_re = fmt_re(rl["candidate_format"])
        rcs = sorted((t for t in tag_names(args.clone, "--merged", pred_ref) if (m := rc_re.match(t)) and m.group("v") == args.predecessor),
                     key=lambda t: int(rc_re.match(t).group("n")))
        if not rcs:
            raise Refusal("NO_FORK_POINT", f"no candidate tag for {args.predecessor} on {pred['integration_branch']}; pass --from REF")
        fork_ref = rcs[-1]
    fork_sha = commit_of(args.clone, fork_ref)
    if fork_sha is None or not is_ancestor(args.clone, fork_sha, pred_ref):
        raise Refusal("FORK_NOT_ON_PREDECESSOR", f"{fork_ref} must be reachable from {pred['integration_branch']}")
    res.step("fork", "RESOLVED", {"fork_ref": fork_ref, "fork_sha": fork_sha, "predecessor_branch": pred["integration_branch"], "branch": branch})
    cand = None
    if listed is None:
        cand = copy.deepcopy(policy)
        apply_add_successor(cand["repos"][key], pred, version, args.predecessor, fork_ref, fork_sha, branch)
        try:  # a candidate the owner could not install must not cost a pushed branch
            check_policy(cand, args.repo)
        except (ConfigError, Refusal) as e:
            raise Refusal("INVALID_CANDIDATE", json.dumps(e.errors if isinstance(e, ConfigError) and e.errors else e.detail))
    remote = ls_remote(args.clone, f"refs/heads/{branch}")
    if remote is None:
        if not args.execute:
            res.step("branch", "DRY_PLAN", {"would_push": f"{fork_sha}:refs/heads/{branch}"})
            res.status = "NOT_RUN"
        elif push_new_ref(args.clone, fork_sha, f"refs/heads/{branch}"):
            res.step("branch", "CREATED", {"sha": fork_sha})
        else:
            raise Refusal("CONFLICT_EXISTING", f"{branch} does not show {fork_sha} on origin after the push")
    elif is_ancestor(args.clone, fork_sha, remote):
        res.step("branch", "REUSED", {"sha": remote})
    else:
        raise Refusal("CONFLICT_EXISTING", f"{branch} exists at {remote}, which does not contain fork {fork_sha}")
    if cand is None:
        res.step("registry", "LISTED", {"line": listed["id"]})
        return
    res.step("registry", "PENDING_OWNER_LISTING", {"add_branch": branch, "head_prefix": f"v{version}/"})
    emit_proposal(args, res, policy, baseline_sha, cand, "add-successor", f"add successor v{version} on {branch} after {pred['id']}")
    if res.status == "PASS":
        res.next_action = f"owner installs the packet so {branch} becomes merge-eligible; state stays PENDING_OWNER_LISTING until then"


def integration_link(clone, rl, frm, into):
    """Facts for integrating frm's shipped version into its successor: final tag, promoted candidate SHA, whether it is already there."""
    if into["kind"] != "successor" or into["predecessor"]["line"] != frm["id"]:
        raise Refusal("NOT_SUCCESSOR", f"{into['id']} is not a successor of {frm['id']}")
    version = into["predecessor"]["version"]
    tag = rl["tag_format"].replace("{version}", version)
    final = commit_of(clone, "refs/tags/" + tag)
    if final is None:
        raise Refusal("NO_FINAL_TAG", f"{tag} does not exist; {frm['id']} has not shipped {version}")
    rc, out = git(clone, "rev-list", "--parents", "-n", "1", final)
    parts = out.split()
    cand = parts[2] if len(parts) >= 3 else final
    if not is_ancestor(clone, cand, need_ref(clone, frm["integration_branch"])):
        raise Refusal("CANDIDATE_NOT_ON_LINE", f"promoted candidate {cand} is not on {frm['integration_branch']}")
    into_ref = need_ref(clone, into["integration_branch"])
    return {"from_version": version, "final_tag": tag, "candidate": cand, "into_ref": into_ref, "integrated": is_ancestor(clone, cand, into_ref)}


def train_integrate(args, res):
    if not args.clone:
        raise SystemExit(usage_error("train integrate needs --clone"))
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    require_ready(entry)
    rl = entry["release_lines"]
    frm, into = line_by_id(entry, args.from_line), line_by_id(entry, args.into)
    fetch(args.clone)
    link = integration_link(args.clone, rl, frm, into)
    cand, version = link["candidate"], link["from_version"]
    res.env["idempotency_key"] = f"int:{args.repo}:{frm['id']}:{into['id']}:{link['final_tag']}"
    res.step("preconditions", "MET", {"final_tag": link["final_tag"], "candidate_sha": cand})
    if frm["kind"] == "successor":
        try:
            prev = integration_link(args.clone, rl, line_by_id(entry, frm["predecessor"]["line"]), frm)
        except Refusal as e:
            raise Refusal("OUT_OF_ORDER", f"{frm['id']}'s own predecessor is not shipped and integrated first ({e.code}); integrate in dependency order")
        if not prev["integrated"]:
            raise Refusal("OUT_OF_ORDER", f"{prev['final_tag']} is not yet integrated into {frm['id']}; integrate in dependency order")
    stale = [t for t in tag_names(args.clone, "--merged", link["into_ref"])
             if (m := fmt_re(rl["candidate_format"]).match(t)) and m.group("v") == into.get("version") and not is_ancestor(args.clone, cand, t + "^{commit}")]
    res.step("stale_candidates", "PROMOTION_BLOCKED" if stale else "NONE", {
        "tags": stale, "rule": f"a {into['id']} candidate cut before {link['final_tag']} was integrated must not be promoted; cut a new one"})
    if link["integrated"]:
        res.step("integration", "INTEGRATED", {"candidate_sha": cand, "into": into["integration_branch"]})
        return
    predicted = git(args.clone, "merge-tree", "--write-tree", link["into_ref"], cand)[0]
    if predicted != 0:
        raise Refusal("MERGE_CONFLICT", f"merging {cand} into {into['integration_branch']} conflicts; resolve by hand, nothing was pushed")
    head = f"lineint/{version}/{into['id']}"
    remote = ls_remote(args.clone, f"refs/heads/{head}")
    if remote is not None and remote != cand:
        raise Refusal("CONFLICT_EXISTING", f"{head} exists at {remote}, not the promoted candidate {cand}")
    if remote is None and not args.execute:
        res.step("head", "DRY_PLAN", {"would_push": f"{cand}:refs/heads/{head}"})
        res.status = "NOT_RUN"
        return
    if remote is None:
        if not push_new_ref(args.clone, cand, f"refs/heads/{head}"):
            raise Refusal("CONFLICT_EXISTING", f"{head} does not show {cand} on origin after the push")
        res.step("head", "CREATED", {"sha": cand})
    else:
        res.step("head", "REUSED", {"sha": cand})
    pr = ensure_pr(res, args, head, into["integration_branch"], f"Integrate {link['final_tag']} into {into['id']}",
                   f"Integrate the promoted {frm['id']} candidate into the successor (merge commit, normal gate).\n\n"
                   + trailers("integrate", frm["id"], into["id"], cand), refuse_merged=True)
    if pr is not None:
        res.status = "DEFERRED"
        res.next_action = f"merge PR #{pr['number']} into {into['integration_branch']} as a merge commit through owner-merge-gate, then re-run to verify"


def converge_problems(clone, rl, entry, major, chain):
    problems = []
    for frm, into in zip([major] + chain, chain):
        try:
            if not integration_link(clone, rl, frm, into)["integrated"]:
                problems.append({"from": frm["id"], "into": into["id"], "problem": "NOT_INTEGRATED"})
        except Refusal as e:
            problems.append({"from": frm["id"], "into": into["id"], "problem": e.code})
    return problems


def train_converge(args, res):
    if not args.clone:
        raise SystemExit(usage_error("train converge needs --clone"))
    policy, baseline_sha = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    key, entry = check_policy(policy, args.repo)
    require_ready(entry)
    rl = entry["release_lines"]
    major = line_by_id(entry, args.line)
    if major["kind"] != "major":
        raise Refusal("NOT_A_MAJOR", "converge targets a major line")
    res.env["idempotency_key"] = f"conv:{args.repo}:{major['id']}"
    chain = active_chain(rl, major)
    if not chain:
        raise Refusal("NO_SUCCESSORS", f"{major['id']} has no active successor to converge")
    fetch(args.clone)
    problems = converge_problems(args.clone, rl, entry, major, chain)
    if problems:
        res.step("precondition", "CONVERGE_PRECONDITION", {"problems": problems})
        res.status, res.next_action = "BLOCKED", "ship and integrate every version in dependency order (`train integrate`), then re-run"
        return
    res.step("precondition", "MET", {"chain": [x["id"] for x in chain]})
    tip, base = chain[-1]["integration_branch"], major["integration_branch"]
    tip_sha = ls_remote(args.clone, f"refs/heads/{tip}")
    if tip_sha is None:
        raise Refusal("NO_BRANCH", f"{tip} is not on origin")
    if not is_ancestor(args.clone, tip_sha, need_ref(args.clone, base)):
        pr = ensure_pr(res, args, tip, base, f"Converge {chain[-1]['id']} into {major['id']}",
                       "Converge the successor train into the major line (merge commit, normal gate).\n\n"
                       + trailers("converge", chain[-1]["id"], major["id"], tip_sha))
        if pr is not None:
            res.status = "DEFERRED"
            res.next_action = f"merge PR #{pr['number']} into {base} as a merge commit through owner-merge-gate, then re-run"
        return
    res.step("convergence", "CONVERGED", {"tip_sha": tip_sha, "into": base})
    cand = copy.deepcopy(policy)
    for ln in cand["repos"][key]["release_lines"]["lines"]:
        if ln["id"] in {x["id"] for x in chain}:
            ln["state"], ln["converged_into"] = "converged", major["id"]
    emit_proposal(args, res, policy, baseline_sha, cand, "converge", f"converge {', '.join(x['id'] for x in chain)} into {major['id']}")


def guard(args, res):
    if not args.clone:
        raise SystemExit(usage_error("guard needs --clone"))
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    rl = entry["release_lines"]
    fetch(args.clone)
    head = commit_of(args.clone, args.head)
    if head is None:
        raise Refusal("BAD_HEAD", f"{args.head} is not a commit in the clone")
    targets = [x for x in rl["lines"] if x["integration_branch"] == args.base]
    pubs = [x["id"] for x in rl["lines"] if x["publication_base"]["branch"] == args.base]
    if pubs:
        res.step("base", "PUBLICATION_BASE_DIRECT_CHANGE", {"advisory": True, "lines": pubs,
                 "detail": "a change merged straight into a publication base bypasses the integration branches and needs a forward-port"})
    if not targets:
        res.step("base", "NOT_AN_INTEGRATION_BASE", {"base": args.base})
        return
    line = targets[0]
    if line["support"] == "eol":
        res.step("base", "EOL_BASE", {"line": line["id"]})
        res.status, res.next_action = "BLOCKED", f"{line['id']} is end-of-life; target a supported line"
        return
    base_ref = need_ref(args.clone, args.base)
    chain = active_chain(rl, line) if line["kind"] == "major" else []
    if chain and head == ls_remote(args.clone, f"refs/heads/{chain[-1]['integration_branch']}"):
        problems = converge_problems(args.clone, rl, entry, line, chain)
        if problems:
            res.step("converge", "CONVERGE_PRECONDITION", {"problems": problems})
            res.status, res.next_action = "BLOCKED", "converge only after every version shipped and was integrated in order"
        else:
            res.step("converge", "CONVERGE_ALLOWED", {"tip": chain[-1]["id"]})
        return
    mine = set(git(args.clone, "rev-list", f"{base_ref}..{head}")[1].split())
    for succ in descendants(rl, line):
        shared = sorted(mine & set(git(args.clone, "rev-list", f"{base_ref}..{need_ref(args.clone, succ['integration_branch'])}")[1].split()))
        if shared:
            res.step("successor_commits", "SUCCESSOR_ONLY_COMMITS", {"successor": succ["id"], "commits": shared[:20], "count": len(shared)})
            res.status, res.next_action = "BLOCKED", f"{args.head} carries commits that exist only on {succ['id']}; they reach {line['id']} only through `train converge`"
            return
    res.step("successor_commits", "NONE", {"line": line["id"]})


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
    se = top.add_parser("successor").add_subparsers(dest="cmd", required=True).add_parser("ensure", parents=[common])
    se.add_argument("--predecessor", required=True, help="predecessor version, e.g. 0.3.0")
    se.add_argument("--from", dest="from_ref", help="fork point (default: the predecessor's highest candidate tag)")
    se.add_argument("--version", help="successor version (default: next minor of the predecessor)")
    se.add_argument("--branch", help="integration branch (default: the registered one, else integration/v<version>)")
    gd = top.add_parser("guard", parents=[common])
    gd.add_argument("--base", required=True)
    gd.add_argument("--head", required=True)
    tr = top.add_parser("train").add_subparsers(dest="cmd", required=True)
    ti = tr.add_parser("integrate", parents=[common])
    ti.add_argument("--from", dest="from_line", required=True)
    ti.add_argument("--into", required=True)
    tr.add_parser("converge", parents=[common]).add_argument("--line", required=True)
    return p


HANDLERS = {("registry", "validate"): registry_validate, ("registry", "show"): registry_show, ("registry", "propose"): propose,
            ("audit", "observe"): observe, ("audit", "import"): audit_import, ("route", None): route,
            ("successor", "ensure"): successor_ensure, ("guard", None): guard, ("train", "integrate"): train_integrate,
            ("train", "converge"): train_converge}


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not omg.REPO_RE.fullmatch(args.repo):
        return usage_error("--repo must be OWNER/REPO (no host, no extra path segments)")
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
        res.status, code = e.status, 1
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
    except Exception as e:  # unexpected policy/input shapes must still produce an envelope, never a traceback
        res.status, code = "FAIL", 3
        res.step("config", "UNEXPECTED_SHAPE", {"detail": type(e).__name__})
        res.next_action = "the owner policy or input has an unexpected shape; fix it and re-run"
    print(json.dumps(res.env, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
