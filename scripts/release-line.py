#!/usr/bin/env python3
"""Release-line registry, routing, audit, successor-train and forward-port CLI (SPE-180 design, slices SPE-181a, SPE-182, SPE-183, SPE-184a).

  release-line.py registry validate|show        --repo O/R [--policy FILE]
  release-line.py registry propose --op add-successor|retire|eol|converge --repo O/R [--execute] [op flags]
  release-line.py route --kind feature|fix --version V --repo O/R --clone PATH
  release-line.py audit observe --repo O/R --clone PATH
  release-line.py audit import FILE --repo O/R [--execute]
  release-line.py successor ensure --predecessor V [--from REF] [--version V] --repo O/R --clone PATH [--execute]
  release-line.py guard --base BRANCH --head SHA --repo O/R --clone PATH
  release-line.py train integrate --from LINE --into LINE --repo O/R --clone PATH [--execute]
  release-line.py train converge --line LINE --repo O/R --clone PATH [--execute]
  release-line.py forward-port plan|run|status (--merge SHA | --pr N | --range A..B) --repo O/R --clone PATH [--execute]
  release-line.py release candidate-tag|promote|verify|final-tag --version V [--line L] --repo O/R --clone PATH [--execute]
  release-line.py protection snapshot|verify --branch B --repo O/R [--execute]

Prints exactly one JSON envelope on stdout. Exit 0 ok/no-op, 1 refused/blocked, 2 usage, 3 invalid config.
The owner policy file (version-branches.json) is a trust anchor: it is read through owner-merge-gate.py's
read_owner_file and never written here. `propose` and `audit import` emit a candidate file, both sha256 digests and an
authority-record template into a proposals directory, and only with --execute; the owner installs the candidate.
Whether a branch may receive merges stays decided by `branches` alone; release_lines is validated here, ignored by the gate.
"""
import argparse
import contextlib
import copy
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types

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


# Mirrors git check-ref-format: no "..", "//", empty or dot-leading components, ".lock" suffix, or trailing "/" or ".".
BRANCH_NAME_RE = re.compile(r"(?!.*(?:\.\.|//|/\.|\.lock(?:/|$)|[/.]$))[A-Za-z0-9][A-Za-z0-9._/-]{0,100}")


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


def run(argv, cwd=None, extra_env=None, stdin=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", **(extra_env or {}))
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", timeout=60, input=stdin)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, type(e).__name__
    return p.returncode, p.stdout


def git(clone, *args, env=None, stdin=None):
    # The clone is agent-supplied: never run its fsmonitor or hooks, never page.
    return run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(clone), *args],
               extra_env=env, stdin=stdin)


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
    newest_frozen = None
    for tip in sorted(active, key=lambda x: vkey(x["version"]), reverse=True):
        if branch_ref(args.clone, tip["integration_branch"]) is None:
            raise Refusal("BRANCH_NOT_IN_CLONE", f"{tip['integration_branch']} is not in the clone; fetch it first")
        # A successor forks from its predecessor's candidate, so only candidates of its own version freeze it.
        tip_frozen = [v for v in unshipped_candidates(args.clone, rl, tip) if v == tip["version"]]
        if not tip_frozen:
            return done(tip, f"major line is frozen by unshipped candidate {frozen[-1]}; feature goes to the newest unfrozen successor")
        newest_frozen = newest_frozen or tip_frozen[-1]  # the newest tip is the one a new successor would follow
    res.step("route", "NEEDS_SUCCESSOR", {"line": mline["id"], "frozen_by": newest_frozen or frozen[-1], "requested_version": args.version})
    res.status = "BLOCKED"
    res.next_action = (f"no successor exists after frozen candidate {newest_frozen or frozen[-1]}; owner registers one with "
                       "`successor ensure` then `registry propose --op add-successor`. Only this feature is blocked")


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
    # Explicit refspecs: a plain --tags fetch exits non-zero when a local tag differs, which must not be mistaken for an outage.
    rc, out = git(clone, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
    if rc != 0:
        raise Refusal("FETCH_FAILED", "git fetch origin failed; a stale clone must not decide freeze, guard or train state")


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


def ensure_pr(res, args, head, base, title, body, refuse_merged=False, step="pr"):
    """Reuse the one open PR head -> base or open it (--execute only). The merge itself belongs to the normal gate."""
    pr, merged = find_prs(args.repo, head, base)
    if pr is None and merged and refuse_merged:
        raise Refusal("NOT_INTEGRATED", f"PR #{merged[0]['number']} merged but the expected commit is not in {base}", "FAIL")
    if pr is not None:
        res.step(step, "REUSED", {"number": pr["number"], "url": pr.get("url")})
        return pr
    if not args.execute:
        res.step(step, "DRY_PLAN", {"head": head, "base": base})
        res.status = "NOT_RUN"
        return None
    rc, _ = run(["gh", "pr", "create", "--repo", args.repo, "--base", base, "--head", head, "--title", title, "--body", body])
    pr = find_prs(args.repo, head, base)[0] if rc == 0 else None
    if pr is None:
        raise Refusal("GH_PR_CREATE_FAILED", f"could not open {head} -> {base}")
    res.step(step, "CREATED", {"number": pr["number"], "url": pr.get("url")})
    return pr


def trailers(kind, source_line, target_line, source, reason=None):
    return (f"Release-Line-Source: {source}\nRelease-Line-Source-Line: {source_line}\n"
            f"Release-Line-Target-Line: {target_line}\nRelease-Line-Kind: {kind}\n"
            + (f"Release-Line-Reason: {reason}\n" if reason else ""))


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
    if listed is not None and args.branch and args.branch != listed["integration_branch"]:
        raise Refusal("BRANCH_MISMATCH", f"line {listed['id']} is registered on {listed['integration_branch']}; --branch {args.branch} would push an unregistered ref")
    branch = args.branch or (listed or {}).get("integration_branch") or f"integration/v{version}"
    if not BRANCH_NAME_RE.fullmatch(branch):
        raise Refusal("BAD_BRANCH", f"{branch!r} is not a valid integration branch name")
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
    def commits(rng):
        rc, out = git(args.clone, "rev-list", rng)
        if rc != 0:
            raise Refusal("REV_LIST_FAILED", f"git rev-list {rng} failed; refusing to report a clean guard")
        return set(out.split())
    mine = commits(f"{base_ref}..{head}")
    for succ in descendants(rl, line):
        shared = sorted(mine & commits(f"{base_ref}..{need_ref(args.clone, succ['integration_branch'])}"))
        if shared:
            res.step("successor_commits", "SUCCESSOR_ONLY_COMMITS", {"successor": succ["id"], "commits": shared[:20], "count": len(shared)})
            res.status, res.next_action = "BLOCKED", f"{args.head} carries commits that exist only on {succ['id']}; they reach {line['id']} only through `train converge`"
            return
    res.step("successor_commits", "NONE", {"line": line["id"]})


# ---------------------------------------------------------------- forward-port (SPE-183)
# Accounting is recomputed from git and GitHub on every run; the local journal is an audit trail, never the source of truth.
# Port commits are built with plumbing (merge-tree + commit-tree), so the clone's working tree and index are never touched.

TRAILER_RE = re.compile(r"Release-Line-(Source|Source-Line|Target-Line|Kind|Reason): (.*)", re.ASCII)
PORT_KINDS = {"forward-port": "PORTED", "equivalent": "TRAILER_EQUIVALENT", "not-applicable": "TRAILER_NOT_APPLICABLE"}
TERMINAL = ("forward-port", "equivalent", "not-applicable")
REV_RE = re.compile(r"[^-\s.][^\s]*")
RANGE_MAX = 200


class Journal:
    """Append-only audit trail under the confined state directory, guarded by a flock so two runs never interleave."""

    def __init__(self, args):
        self.args, self.lfd, self.jfd = args, None, None

    def __enter__(self):
        base = proposal_dir(self.args).parent
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            dfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise Refusal("STATE_DIR_UNSAFE", "the state directory is not a plain directory")
        try:
            if os.fstat(dfd).st_uid != os.getuid():
                raise Refusal("STATE_DIR_UNSAFE", "the state directory is not owned by the caller")
            os.fchmod(dfd, 0o700)
            self.lfd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                fcntl.flock(self.lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refusal("LOCKED", "another release-line run holds the state lock")
            self.jfd = os.open("journal.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        except BaseException:
            self.__exit__()
            raise
        finally:
            os.close(dfd)
        return self

    def __exit__(self, *exc):
        for fd in (self.jfd, self.lfd):
            if fd is not None:
                os.close(fd)
        self.jfd = self.lfd = None

    def append(self, **rec):
        os.write(self.jfd, (json.dumps({"ts": now_utc(), **rec}) + "\n").encode())


def line_keys(rl):
    """Order of lines: major ascending, then depth in the successor chain. A line is strictly newer when its key is greater."""
    by_id = {x["id"]: x for x in rl["lines"]}
    keys = {}
    for ln in rl["lines"]:
        depth, cur = 0, ln
        while cur["kind"] == "successor":
            depth, cur = depth + 1, by_id[cur["predecessor"]["line"]]
        keys[ln["id"]] = (cur["major"], depth)
    return keys


def parse_trailers(msg):
    """Release-Line trailers from the last paragraph of a message (where git itself reads trailers); first value wins."""
    out = {}
    for row in msg.strip().split("\n\n")[-1].splitlines():
        m = TRAILER_RE.fullmatch(row.rstrip())
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def log_messages(clone, *revargs):
    rc, out = git(clone, "log", "--format=%H%x1f%B%x1e", *revargs)
    if rc != 0:
        raise Refusal("GIT_LOG_FAILED", "git log failed in the clone")
    rows = []
    for chunk in out.split("\x1e"):
        sha, _, msg = chunk.strip("\n").partition("\x1f")
        if SHA_RE.fullmatch(sha.strip()):
            rows.append((sha.strip(), msg))
    return rows


def trailer_hit(clone, tip, source):
    """The first commit reachable from tip whose trailers name `source` as Release-Line-Source with a known kind."""
    for sha, msg in log_messages(clone, "-F", f"--grep=Release-Line-Source: {source}", tip):
        t = parse_trailers(msg)
        if t.get("Source") == source and t.get("Kind") in PORT_KINDS:
            return {"commit": sha, **t}
    return None


def merged_pr_commit(repo, number):
    rc, out = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", "number,state,mergeCommit,baseRefName"])
    try:
        pr = json.loads(out) if rc == 0 else None
    except ValueError:
        pr = None
    if not isinstance(pr, dict):
        raise Refusal("GH_UNAVAILABLE", f"gh pr view {number} failed")
    oid = (pr.get("mergeCommit") or {}).get("oid")
    if pr.get("state") != "MERGED" or not (isinstance(oid, str) and SHA_RE.fullmatch(oid)):
        raise Refusal("PR_NOT_MERGED", f"PR #{number} is not merged (state {pr.get('state')})")
    return oid, pr.get("baseRefName")


def fp_revs(args, res):
    """The source revisions to account for plus an optional branch hint (the PR's base)."""
    if args.pr is not None:
        sha, base = merged_pr_commit(args.repo, args.pr)
        return [sha], base
    if args.merge:
        if not REV_RE.fullmatch(args.merge):
            raise SystemExit(usage_error("--merge must be a commit id"))
        return [args.merge], None
    a, sep, b = args.range.partition("..")
    if not (sep and REV_RE.fullmatch(a) and REV_RE.fullmatch(b) and ".." not in b):
        raise SystemExit(usage_error("--range must be A..B"))
    rc, out = git(args.clone, "rev-list", "--first-parent", "--reverse", f"{a}..{b}")
    revs = out.split() if rc == 0 else []
    if rc != 0:
        raise Refusal("BAD_RANGE", f"{args.range} is not a range in the clone")
    if len(revs) > RANGE_MAX:
        raise Refusal("RANGE_TOO_LARGE", f"{len(revs)} commits; at most {RANGE_MAX} per run")
    return revs, None


def load_source(clone, rev):
    sha = commit_of(clone, rev)
    if sha is None:
        raise Refusal("BAD_SOURCE", f"{rev} is not a commit in the clone")
    parts = git(clone, "rev-list", "--parents", "-n", "1", sha)[1].split()
    if len(parts) < 2 or len(parts) > 3:
        raise Refusal("BAD_SOURCE", f"{sha[:12]} is a root or octopus commit")
    merge = len(parts) == 3
    # A GitHub merge commit carries no trailers of its own: the work is the second-parent range M^1..M^2.
    own = log_messages(clone, f"{parts[1]}..{parts[2]}") if merge else []
    return types.SimpleNamespace(sha=sha, merge=merge, net_base=parts[1], tip=parts[2] if merge else None,
                                 range_commits=[s for s, _ in own] or [sha], range_msgs=own,
                                 msg=git(clone, "log", "-1", "--format=%B", sha)[1])


def source_origin(ctx, src, hint):
    """(label, floor key, branch): where the source landed and which lines are strictly newer."""
    rl, clone = ctx.rl, ctx.clone
    if hint:
        ln = next((x for x in rl["lines"] if x["integration_branch"] == hint), None)
        if ln is not None:
            ref = branch_ref(clone, hint)
            if ref is None or not is_ancestor(clone, src.sha, ref):
                raise Refusal("SOURCE_NOT_ON_BRANCH", f"{src.sha[:12]} is not on {hint}")
            return ln["id"], ctx.keys[ln["id"]], hint, False
        if hint not in ctx.bases:
            raise Refusal("SOURCE_BRANCH_UNKNOWN", f"{hint} is neither a registered integration branch nor a publication base")
    else:
        found = [x for x in rl["lines"] if (ref := branch_ref(clone, x["integration_branch"])) and is_ancestor(clone, src.sha, ref)]
        if found:
            ln = min(found, key=lambda x: ctx.keys[x["id"]])
            return ln["id"], ctx.keys[ln["id"]], ln["integration_branch"], False
        hint = next((b for b in sorted(ctx.bases) if (ref := branch_ref(clone, b)) and is_ancestor(clone, src.sha, ref)), None)
    if hint is None:
        raise Refusal("SOURCE_NOT_ON_A_LINE", f"{src.sha[:12]} is on no registered integration branch or publication base")
    owned = [ctx.keys[x["id"]] for x in rl["lines"] if x["publication_base"]["branch"] == hint]
    # A change landed straight on a publication base targets every line that publishes from it, and every newer one.
    return f"base:{hint}", min(owned) if owned else (10 ** 6, 0), hint, True


def loop_reason(ctx, src, own_branch):
    """Why this commit must not be forward-ported again, or None. Ports, integrate, converge and promote merges are never sources."""
    for sha, msg in [(src.sha, src.msg)] + src.range_msgs:
        t = parse_trailers(msg)
        if SHA_RE.fullmatch(t.get("Source", "")):
            return "LOOP_PREVENTED", f"{sha[:12]} carries Release-Line-Source {t['Source'][:12]}: it is itself a port or equivalence record"
    if src.merge:
        others = [x["integration_branch"] for x in ctx.rl["lines"]] + sorted(ctx.bases)
        for b in others:
            ref = branch_ref(ctx.clone, b)
            if b != own_branch and ref and is_ancestor(ctx.clone, src.tip, ref) and not is_ancestor(ctx.clone, src.sha, ref):
                return "INTEGRATION_MERGE", f"second parent is already on {b} without this merge: an integrate, converge or promote merge, not a fix"
    return None


def net_patch_id(clone, src):
    rc, diff = git(clone, "diff", "--no-ext-diff", "--no-textconv", src.net_base, src.sha)
    toks = git(clone, "patch-id", "--stable", stdin=diff)[1].split() if rc == 0 and diff.strip() else []
    return toks[0] if toks else None


def patch_equivalent(clone, src, pid, tsha):
    """A commit on the target since the merge base whose own patch-id equals the source's net patch-id."""
    rc, mb = git(clone, "merge-base", src.sha, tsha)
    if rc != 0 or not pid:
        return None
    rc, log = git(clone, "log", "-p", "--no-ext-diff", "--no-textconv", "--no-merges", "-n", "1000", f"{mb.strip()}..{tsha}")
    for row in (git(clone, "patch-id", "--stable", stdin=log)[1].splitlines() if rc == 0 else []):
        got, _, commit = row.partition(" ")
        if got == pid:
            return commit.strip()
    return None


def predict_port(clone, src, tsha):
    """Replay the source's net change on the target without a working tree: ('clean', tree) or ('conflict', paths)."""
    rc, out = git(clone, "merge-tree", "--write-tree", "--name-only", f"--merge-base={src.net_base}", tsha, src.sha)
    rows = out.splitlines()
    if rc == 0 and rows and SHA_RE.fullmatch(rows[0]):
        return "clean", rows[0]
    if rc == 1:
        paths = []
        for row in rows[1:]:
            if not row:
                break
            paths.append(row)
        return "conflict", paths
    raise Refusal("MERGE_TREE_FAILED", "git merge-tree could not evaluate the port")


def port_message(src, ln, label, clone):
    rows = [r for r in src.msg.strip().splitlines()]
    if src.merge:  # GitHub puts the PR title right after the generated subject
        title = next((r for r in rows[1:] if r.strip()), rows[0] if rows else src.sha[:12])
        head = f"{title} (forward-port to {ln['id']})"
    else:
        head = "\n".join(rows) if rows else src.sha[:12]
    reason = f"maintenance change {src.sha[:12]} from {label} applies to {ln['id']}"
    return f"{head}\n\n(cherry picked from commit {src.sha})\n\n{trailers('forward-port', label, ln['id'], src.sha, reason)}"


def commit_tree(clone, tree, parents, message, author_of=None):
    env = None
    if author_of:
        rc, out = git(clone, "log", "-1", "--format=%an%x1f%ae%x1f%aI", author_of)
        name, _, rest = out.strip().partition("\x1f")
        email, _, date = rest.partition("\x1f")
        if rc == 0 and name and email:
            env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}
    argv = ["commit-tree", tree]
    for p in parents:
        argv += ["-p", p]
    rc, out = git(clone, *argv, "-F", "-", env=env, stdin=message)
    if rc != 0 or not SHA_RE.fullmatch(out.strip()):
        raise Refusal("COMMIT_FAILED", "git commit-tree failed (is a committer identity configured?)")
    return out.strip()


def open_pr(ctx, head, base):
    try:
        return find_prs(ctx.repo, head, base)[0]
    except Refusal:
        if ctx.execute:
            raise
        ctx.gh_ok = False
        return None


def refresh_head(ctx, head, remote, tsha, branch, out):
    """A port head behind its target is brought up to date by merging the target into it: a fast-forward push, never a force."""
    clone = ctx.clone
    if commit_of(clone, remote) is None:
        raise Refusal("CONFLICT_EXISTING", f"{head} is at {remote}, which is not in the fetched clone")
    if is_ancestor(clone, tsha, remote):
        return remote
    out["head_stale"] = True
    if not ctx.execute:
        return remote
    rc, o = git(clone, "merge-tree", "--write-tree", remote, tsha)
    if rc != 0:
        raise Refusal("STALE_HEAD_CONFLICT", f"{head} cannot be updated from {branch} without a conflict; resolve by hand")
    new = commit_tree(clone, o.split()[0], [remote, tsha], f"Merge {branch} into {head}\n")
    if not push_new_ref(clone, new, f"refs/heads/{head}"):
        raise Refusal("CONFLICT_EXISTING", f"{head} does not show {new} on origin after the update")
    out["head_refreshed"] = True
    return new


def fp_target(ctx, src, ln, label, pid):
    """Account for one target line; returns {line, branch, key, head, state, code, ...}. state: forward-port|equivalent|not-applicable|pending|conflict."""
    clone, branch, lid = ctx.clone, ln["integration_branch"], ln["id"]
    head = f"fwdport/{lid}/{src.sha[:12]}"
    out = {"line": lid, "branch": branch, "key": f"fp:{ctx.repo}:{src.sha}:{lid}", "head": head}

    def done(state, code, **ev):
        out.update(state=state, code=code, **ev)
        return out

    tref = branch_ref(clone, branch)
    if tref is None:
        return done("conflict", "NO_BRANCH")
    tsha = commit_of(clone, tref)
    if any(p and is_ancestor(clone, p, tsha) for p in (src.sha, src.tip)):
        return done("equivalent", "CONTAINED")
    hit = trailer_hit(clone, tsha, src.sha)
    if hit:
        return done(hit["Kind"], PORT_KINDS[hit["Kind"]], commit=hit["commit"], reason=hit.get("Reason"))
    matched = patch_equivalent(clone, src, pid, tsha)
    if matched:
        return done("equivalent", "EQUIVALENT_PATCH", matched_commit=matched)
    pr = open_pr(ctx, head, branch)
    remote = ls_remote(clone, f"refs/heads/{head}")
    if pr is not None:
        if remote:
            refresh_head(ctx, head, remote, tsha, branch, out)
        return done("pending", "PR_OPEN", pr=pr["number"], url=pr.get("url"), head_sha=remote)
    if remote is not None:  # the head was pushed but the PR is missing (an interrupted run): reuse it, never rebuild
        if commit_of(clone, remote) is None or not trailer_hit(clone, remote, src.sha):
            raise Refusal("CONFLICT_EXISTING", f"{head} exists at {remote} but does not carry this source's port commit")
        refresh_head(ctx, head, remote, tsha, branch, out)
        if not ctx.execute:
            return done("pending", "HEAD_PUSHED_NO_PR", head_sha=remote)
        return fp_open_pr(ctx, src, ln, label, out, done, remote)
    if not ctx.predict:
        return done("pending", "NOT_STARTED")
    verdict, data = predict_port(clone, src, tsha)
    if verdict == "conflict":
        return done("conflict", "BLOCKED_CONFLICT", paths=data[:50])
    if data == git(clone, "rev-parse", "--verify", "-q", tsha + "^{tree}")[1].strip():
        return done("equivalent", "EQUIVALENT_CONTENT")
    if not ctx.execute:
        return done("pending", "TO_PORT", would_push=f"refs/heads/{head}")
    new = commit_tree(clone, data, [tsha], port_message(src, ln, label, clone), author_of=src.sha)
    if not push_new_ref(clone, new, f"refs/heads/{head}"):
        raise Refusal("CONFLICT_EXISTING", f"{head} does not show {new} on origin after the push")
    return fp_open_pr(ctx, src, ln, label, out, done, new)


def fp_open_pr(ctx, src, ln, label, out, done, head_sha):
    body = (f"Forward-port of {src.sha[:12]} from {label} to {ln['id']} (merge commit through the normal gate; never squash or rebase).\n\n"
            + trailers("forward-port", label, ln["id"], src.sha, f"maintenance change {src.sha[:12]} from {label} applies to {ln['id']}"))
    pr = ensure_pr(ctx.res, ctx.args, out["head"], ln["integration_branch"], f"Forward-port {src.sha[:12]} to {ln['id']}", body,
                   step=f"pr:{ln['id']}")
    return done("pending", "PR_OPEN", pr=pr["number"], url=pr.get("url"), head_sha=head_sha)


def fp_source(ctx, rev, hint):
    res, clone = ctx.res, ctx.clone
    src = load_source(clone, rev)
    s12 = src.sha[:12]
    label, floor, own_branch, base_mode = source_origin(ctx, src, hint)
    pid = net_patch_id(clone, src)
    evidence = {"sha": src.sha, "line": label, "branch": own_branch, "merge": src.merge, "patch_id": pid, "pr": ctx.pr,
                "range": f"{src.net_base}..{src.tip}" if src.merge else None, "range_commits": src.range_commits[:50]}
    res.step(f"source:{s12}", "SOURCE", evidence)
    skip = loop_reason(ctx, src, own_branch)
    if skip:
        res.step(f"source:{s12}", skip[0], {"detail": skip[1]})
        return []
    targets, excluded = [], []
    for ln in sorted(ctx.rl["lines"], key=lambda x: ctx.keys[x["id"]]):
        k = ctx.keys[ln["id"]]
        if not (k >= floor if base_mode else k > floor):
            continue
        why = ("PUBLICATION_BASE" if ln["integration_branch"] in ctx.bases else "EOL" if ln["support"] == "eol"
               else f"STATE_{ln['state'].upper()}" if ln.get("state", "active") != "active" else None)
        (excluded.append({"line": ln["id"], "reason": why}) if why else targets.append(ln))
    if excluded:
        res.step(f"excluded:{s12}", "EXCLUDED", {"lines": excluded})
    results = []
    for ln in targets:
        try:
            out = fp_target(ctx, src, ln, label, pid)
        except Refusal as e:
            if e.code == "GH_UNAVAILABLE":
                raise
            out = {"line": ln["id"], "branch": ln["integration_branch"], "key": f"fp:{ctx.repo}:{src.sha}:{ln['id']}",
                   "state": "conflict", "code": e.code, "detail": e.detail}
        res.step(f"target:{s12}:{ln['id']}", out["state"], out)
        if ctx.journal:
            ctx.journal.append(key=out["key"], state=out["state"], code=out["code"])
        results.append(out)
    complete = all(r["state"] in TERMINAL for r in results)
    res.step(f"accounting:{s12}", "COMPLETE" if complete else "INCOMPLETE", {
        "schema": 1, "repo": ctx.repo, "source": {"merge_sha": src.sha, "pr": ctx.pr, "line": label, "patch_id": pid},
        "targets": [{k: r.get(k) for k in ("line", "branch", "key", "state", "code")} for r in results],
        "excluded": excluded, "complete": complete})
    return results


def forward_port(args, res):
    if not args.clone:
        raise SystemExit(usage_error(f"forward-port {args.cmd} needs --clone"))
    require_clone(args.clone)
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    require_ready(entry)
    rl = entry["release_lines"]
    fetch(args.clone)
    revs, hint = fp_revs(args, res)
    ctx = types.SimpleNamespace(
        res=res, args=args, clone=args.clone, repo=args.repo, rl=rl, keys=line_keys(rl), gh_ok=True, journal=None, pr=args.pr,
        bases={x["publication_base"]["branch"] for x in rl["lines"]} | {"main", "master"},
        execute=args.cmd == "run" and args.execute, predict=args.cmd != "status")
    if len(revs) == 1:
        res.env["idempotency_key"] = f"fp:{args.repo}:{commit_of(args.clone, revs[0]) or revs[0]}:*"
    results = []
    journal = Journal(args) if ctx.execute else None
    try:
        ctx.journal = journal.__enter__() if journal else None
        for rev in revs:
            results += fp_source(ctx, rev, hint)
    finally:
        if journal:
            journal.__exit__()
    if not ctx.gh_ok:
        res.step("pr_lookup", "NOT_RUN", {"reason": "gh unavailable; open pull requests could not be checked"})
    pending = [r for r in results if r["state"] == "pending"]
    if any(r["state"] == "conflict" for r in results):
        res.status, res.next_action = "BLOCKED", "resolve the conflicting targets by hand (each target is independent; the others proceed)"
    elif pending:
        res.status = "DEFERRED" if ctx.execute or args.cmd == "status" else "NOT_RUN"
        res.next_action = ("merge the forward-port PRs through the normal gate, then re-run to account for them" if ctx.execute or args.cmd == "status"
                           else "re-run `forward-port run --execute` to push the heads and open the PRs")


# ---------------------------------------------------------------- release: candidate-tag, promote, verify, final-tag (SPE-184a)
# Publication approval is a per-version GO record read through the owner trust-anchor reader; the tool never writes one and never
# changes branch protection. Pushes create new refs only. The final SHA is captured from the promotion merge and used for every later step.

GO_DEFAULT_DIR = "~/.config/coding-agent-environment/local/release-go"
GO_SCOPES = ("candidate_tag", "promote", "final_tag", "publish")
GIT_MERGE_TREE = (2, 38)  # merge-tree --write-tree
PROMOTION_SCAN = 5000
FAILED_CHECK = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
PENDING_CHECK = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}


def git_at_least(clone, need):
    rc, out = git(clone, "--version")
    m = re.search(r"(\d+)\.(\d+)", out) if rc == 0 else None
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= need


def need_merge_tree(clone):
    if not git_at_least(clone, GIT_MERGE_TREE):
        raise Refusal("GIT_TOO_OLD", "promotion prediction needs git 2.38 or newer (git merge-tree --write-tree)")


def go_file(args, version):
    return os.path.join(os.path.expanduser(args.go_dir or GO_DEFAULT_DIR), f"{args.repo.replace('/', '__')}__{version}.json")


def read_go(args, rl, ln, version):
    """The validated GO record for this repo and version; never echoes its contents (only the fields the caller already knows)."""
    try:
        rec = omg.read_owner_file(go_file(args, version), True, lambda o: o, "release GO record")
    except FileNotFoundError:
        raise Refusal("GO_MISSING", f"no GO record for {args.repo} {version}; the owner records publication approval per version")
    except (ValueError, OSError) as e:
        raise Refusal("GO_INVALID", f"GO record unreadable or untrusted: {type(e).__name__}")
    ok = (isinstance(rec, dict) and all(isinstance(rec.get(k), str) and rec[k] for k in ("repo", "version", "candidate_tag", "candidate_sha", "publication_base", "authority", "created_utc"))
          and isinstance(rec.get("scopes"), list) and all(s in GO_SCOPES for s in rec["scopes"]) and SHA_RE.fullmatch(rec["candidate_sha"] or "") is not None)
    if not ok:
        raise Refusal("GO_INVALID", "GO record needs repo, version, candidate_tag, candidate_sha, publication_base, scopes, authority, created_utc")
    if rec["repo"].lower() != args.repo.lower() or rec["version"] != version or rec["publication_base"] != ln["publication_base"]["branch"]:
        raise Refusal("GO_MISMATCH", "GO record repo, version or publication base differs from this request")
    m = fmt_re(rl["candidate_format"]).fullmatch(rec["candidate_tag"])
    if not (m and m.group("v") == version):
        raise Refusal("GO_MISMATCH", "GO candidate_tag does not match candidate_format for this version")
    if "expires_utc" in rec:
        try:
            expired = datetime.datetime.strptime(rec["expires_utc"], "%Y-%m-%dT%H:%M:%SZ") <= datetime.datetime.strptime(now_utc(), "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            raise Refusal("GO_INVALID", "expires_utc must be YYYY-MM-DDTHH:MM:SSZ")
        if expired:
            raise Refusal("GO_EXPIRED", "the GO record has expired")
    return rec


def need_scope(go, scope, sha=None):
    if scope not in go["scopes"]:
        raise Refusal("GO_SCOPE", f"GO record does not carry the {scope} scope")
    if sha is not None and go["candidate_sha"] != sha:
        raise Refusal("GO_MISMATCH", f"GO record approves candidate {go['candidate_sha'][:12]}, not {sha[:12]}")


def release_ctx(args, res):
    if not args.clone:
        raise SystemExit(usage_error(f"{args.group} {args.cmd} needs --clone"))
    if not VERSION_RE.fullmatch(args.version):
        raise SystemExit(usage_error("--version must be MAJOR.MINOR or MAJOR.MINOR.PATCH"))
    require_clone(args.clone)
    policy, _ = load_registry(args.policy or omg.VERSION_POLICY_DEFAULT)
    _, entry = check_policy(policy, args.repo)
    require_ready(entry)
    rl = entry["release_lines"]
    fetch(args.clone)
    if args.line:
        ln = line_by_id(entry, args.line)
        if (ln["kind"] == "major" and ln["major"] != int(args.version.split(".")[0])) or (ln["kind"] != "major" and ln.get("version") != args.version):
            raise Refusal("LINE_VERSION_MISMATCH", f"line {ln['id']} does not carry version {args.version}")
    else:
        ln = next((x for x in rl["lines"] if x["kind"] != "major" and x.get("version") == args.version), None) \
            or next((x for x in rl["lines"] if x["kind"] == "major" and x["major"] == int(args.version.split(".")[0])), None)
        if ln is None:
            raise Refusal("NO_LINE", f"no release line carries version {args.version}")
    if ln["support"] == "eol":
        raise Refusal("EOL_LINE", f"line {ln['id']} is end-of-life")
    if ln.get("state", "active") != "active":
        raise Refusal("LINE_NOT_ACTIVE", f"line {ln['id']} is {ln['state']}")
    return types.SimpleNamespace(entry=entry, rl=rl, ln=ln, version=args.version, clone=args.clone,
                                 base=ln["publication_base"]["branch"], line_ref=need_ref(args.clone, ln["integration_branch"]))


def remote_tag(clone, name):
    """(tag object sha, peeled commit sha) on origin, or (None, None)."""
    rc, out = git(clone, "ls-remote", "origin", f"refs/tags/{name}", f"refs/tags/{name}^{{}}")
    if rc != 0:
        raise Refusal("REMOTE_UNREACHABLE", "git ls-remote origin failed")
    obj = peeled = None
    for row in out.splitlines():
        sha, _, ref = row.partition("\t")
        if ref == f"refs/tags/{name}":
            obj = sha
        elif ref == f"refs/tags/{name}^{{}}":
            peeled = sha
    return obj, peeled or obj


def push_tag(res, args, name, sha, message, step):
    """Create an annotated tag at sha and push it as a new ref; an existing tag is reused at the same commit, never moved."""
    obj, peeled = remote_tag(args.clone, name)
    if obj is not None:
        if peeled != sha:
            raise Refusal("CONFLICT_EXISTING", f"tag {name} exists at {peeled}, not {sha}; tags are never moved or deleted")
        res.step(step, "REUSED", {"tag": name, "sha": sha})
        return False
    if not args.execute:
        res.step(step, "DRY_PLAN", {"would_push": f"refs/tags/{name}", "sha": sha})
        res.status = "NOT_RUN"
        return False
    local = commit_of(args.clone, f"refs/tags/{name}")
    if local not in (None, sha):
        raise Refusal("CONFLICT_EXISTING", f"a local tag {name} points at {local}, not {sha}")
    created = local is None
    if created and git(args.clone, "tag", "-a", "-m", message, name, sha)[0] != 0:
        raise Refusal("TAG_FAILED", f"could not create tag {name} (is a committer identity configured?)")
    tag_obj = git(args.clone, "rev-parse", f"refs/tags/{name}")[1].strip()
    git(args.clone, "push", "origin", f"{tag_obj}:refs/tags/{name}")
    if remote_tag(args.clone, name) != (tag_obj, sha):
        if created:
            git(args.clone, "tag", "-d", name)
        raise Refusal("CONFLICT_EXISTING", f"origin does not show tag {name} at {sha} after the push")
    res.step(step, "CREATED", {"tag": name, "sha": sha, "tag_object": tag_obj})
    return True


def release_candidate_tag(args, res):
    c = release_ctx(args, res)
    rl, ln = c.rl, c.ln
    tip = commit_of(args.clone, c.line_ref)
    sha = tip if not args.sha else commit_of(args.clone, args.sha)
    if sha is None or not is_ancestor(args.clone, sha, c.line_ref):
        raise Refusal("SHA_NOT_ON_LINE", f"{args.sha or 'the line tip'} is not reachable from {ln['integration_branch']}")
    pre = rl["milestone_tags"] == "pre-authorized"
    try:
        go = read_go(args, rl, ln, c.version)
    except Refusal as e:
        if not (pre and e.code == "GO_MISSING"):
            raise
        go = None
    if go is not None:
        if not (pre and "candidate_tag" not in go["scopes"]):  # a pre-authorized repo needs no scope, but a record that is present must still match
            need_scope(go, "candidate_tag", sha)
        elif go["candidate_sha"] != sha:
            raise Refusal("GO_MISMATCH", f"GO record approves candidate {go['candidate_sha'][:12]}, not {sha[:12]}")
        name = go["candidate_tag"]
    else:
        rc_re = fmt_re(rl["candidate_format"])
        seen = [int(m.group("n")) for t in tag_names(args.clone) if (m := rc_re.fullmatch(t)) and m.group("v") == c.version]
        n = args.n if args.n else (max(seen) + 1 if seen else 1)
        name = rl["candidate_format"].replace("{version}", c.version).replace("{n}", str(n))
    if go is not None and args.n and name != rl["candidate_format"].replace("{version}", c.version).replace("{n}", str(args.n)):
        raise Refusal("GO_MISMATCH", f"--n {args.n} differs from the GO record's candidate tag {name}")
    res.env["idempotency_key"] = f"tag:{args.repo}:{name}"
    res.step("go", "ACCEPTED" if go else "PRE_AUTHORIZED", {"scope": "candidate_tag", "tag": name})
    with (Journal(args) if args.execute else contextlib.nullcontext()) as j:
        push_tag(res, args, name, sha, f"Release candidate {name} for {c.version} on line {ln['id']}", "tag")
        if j and any(s["step"] == "tag" and s["state"] == "CREATED" for s in res.steps):
            j.append(key=res.env["idempotency_key"], state="candidate-tag", tag=name, sha=sha, line=ln["id"], version=c.version)
    if res.status == "PASS":
        res.next_action = "none" if any(s["step"] == "tag" and s["state"] == "REUSED" for s in res.steps) else f"verify {name}, then `release promote` once the owner records the promote scope"
    else:
        res.next_action = "re-run with --execute to push the candidate tag"


def scan_promotion(clone, base_ref, cand):
    """The merge commit on the base's first-parent chain whose second parent is the candidate, or None."""
    rc, out = git(clone, "log", "--first-parent", "--merges", "-n", str(PROMOTION_SCAN), "--format=%H %P", base_ref)
    if rc != 0:
        raise Refusal("GIT_LOG_FAILED", "git log failed while looking for the promotion merge")
    for row in out.splitlines():
        p = row.split()
        if len(p) == 3 and p[2] == cand:
            return p[0]
    return None


def promotion_prs(args, version, base, strict=False):
    try:
        return find_prs(args.repo, f"promote/{version}", base)
    except Refusal:
        if strict:
            raise
        return None, []


def check_promotion(clone, final, cand):
    """final^2 must be the candidate and final^{tree} must be what merge-tree predicts for final^1 + candidate. Returns (predicted tree, base SHA)."""
    p = git(clone, "rev-list", "--parents", "-n", "1", final)[1].split()
    if len(p) != 3 or p[2] != cand:
        raise Refusal("PROMOTION_ASSERTION_FAILED", f"{final[:12]} is not a merge commit whose second parent is the candidate {cand[:12]}", "FAIL")
    rc, out = git(clone, "merge-tree", "--write-tree", p[1], cand)
    predicted = out.split()[0] if rc == 0 and out.split() else None
    if predicted is None or predicted != git(clone, "rev-parse", final + "^{tree}")[1].strip():
        raise Refusal("PROMOTION_ASSERTION_FAILED", f"{final[:12]}'s tree differs from the predicted merge of {p[1][:12]} and the candidate", "FAIL")
    return predicted, p[1]


def accounting_complete(args, c, cand, base_ref):
    """Forward-port accounting for base-only changes (cand..base), with the SPE-183 per-source logic, read-only."""
    rc, out = git(args.clone, "rev-list", "--first-parent", "--reverse", f"{cand}..{base_ref}")
    if rc != 0:
        raise Refusal("BAD_RANGE", "git rev-list failed while accounting for base-only changes")
    revs = out.split()
    if len(revs) > RANGE_MAX:
        raise Refusal("RANGE_TOO_LARGE", f"{len(revs)} base-only commits; account for them with forward-port --range first")
    scratch = Result("scratch", args.repo, "")
    ctx = types.SimpleNamespace(res=scratch, args=args, clone=args.clone, repo=args.repo, rl=c.rl, keys=line_keys(c.rl), gh_ok=True,
                                journal=None, pr=None, bases={x["publication_base"]["branch"] for x in c.rl["lines"]} | {"main", "master"},
                                execute=False, predict=True)
    bad = []
    for rev in revs:
        try:
            for r in fp_source(ctx, rev, None):
                if r["state"] not in TERMINAL:
                    bad.append({"source": rev[:12], "line": r["line"], "code": r["code"]})
        except Refusal as e:
            bad.append({"source": rev[:12], "line": None, "code": e.code})
    return len(revs), bad


def final_tags(clone, rl, major):
    final = fmt_re(rl["tag_format"])
    return [(vkey(m.group("v")), m.group("v"), t) for t in tag_names(clone) if (m := final.fullmatch(t)) and m.group("v").split(".")[0] == major]


def hosted_ci(res, args, number, cand):
    """CI_PASS, CI_FAILED (refuses), CI_PENDING (defers) or NOT_RUN, which needs a passing local-equivalent record for the candidate."""
    rc, out = run(["gh", "pr", "view", str(number), "--repo", args.repo, "--json", "statusCheckRollup"])
    try:
        rollup = json.loads(out).get("statusCheckRollup") if rc == 0 else None
    except (ValueError, AttributeError):
        rollup = None
    states = [str((x.get("conclusion") or x.get("state") or x.get("status") or "")).upper() for x in rollup if isinstance(x, dict)] if isinstance(rollup, list) else []
    if any(s in FAILED_CHECK for s in states):
        res.step("hosted_ci", "CI_FAILED", {"checks": len(states)})
        raise Refusal("CI_FAILED", "a required hosted check reported a failure; this tool never bypasses it", "FAIL")
    if any(s in PENDING_CHECK or s == "" for s in states):
        res.step("hosted_ci", "CI_PENDING", {"checks": len(states)})
        res.status, res.next_action = "DEFERRED", "wait for the hosted checks, then re-run"
        return False
    if states:
        res.step("hosted_ci", "CI_PASS", {"checks": len(states)})
        return True
    res.step("hosted_ci", "NOT_RUN", {"reason": "no hosted check ran or gh could not report them (outage, quota or none configured)"})
    logs = os.path.expanduser(args.evidence_dir or omg.LOCAL_EVIDENCE_DIRS["claude"])
    path = os.path.join(logs, omg.local_ci_evidence_filename(f"{args.repo.replace('/', '__')}-{number}", cand))
    try:
        with open(path, "rb") as f:
            rec = json.loads(f.read(1 << 20))
        cmds = rec["commands"]
        good = (rec["repo"].lower() == args.repo.lower() and rec["pr"] == number and rec["head"] == cand and cmds
                and all(c["exit_code"] == 0 for c in cmds))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        good = False
    if not good:
        raise Refusal("LOCAL_EVIDENCE_REQUIRED", f"hosted CI is NOT_RUN and no passing local-equivalent record exists for {cand[:12]} on PR #{number}; "
                      "run owner-merge-local-ci.py record at the PR head first")
    res.step("local_equivalent", "PASS", {"record": os.path.basename(path), "commands": len(cmds)})
    return True


def protection_get(args, branch):
    rc, out = run(["gh", "api", f"repos/{args.repo}/branches/{branch}/protection"])
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message") == "Branch not protected":
        return {"protected": False}
    if rc != 0 or not isinstance(body, dict):
        raise Refusal("GH_UNAVAILABLE", "gh api branch protection failed")

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if not k.endswith("url")}
        return [strip(v) for v in x] if isinstance(x, list) else x
    return {"protected": True, "settings": strip(body)}


def protection_file(args, branch):
    return proposal_dir(args).parent / "protection" / (branch.replace("/", "%2F") + ".json")


def protection_compare(args, branch):
    """(snapshot digest, current digest, drifted top-level keys) or None when no snapshot was recorded."""
    p = protection_file(args, branch)
    if not p.exists():
        return None
    snap = json.loads(p.read_text())
    cur = protection_get(args, branch)
    keys = sorted(k for k in set(snap.get("settings", {})) | set(cur.get("settings", {})) if snap.get("settings", {}).get(k) != cur.get("settings", {}).get(k))
    if snap.get("protected") != cur.get("protected"):
        keys.append("protected")
    return sha256_bytes(dump(snap)), sha256_bytes(dump(cur)), keys


ROLLBACK = "scripts/branch-protection-rollout.py rollback (owner-run, hash-pinned)"


def protection_cmd(args, res):
    if not BRANCH_NAME_RE.fullmatch(args.branch):
        raise SystemExit(usage_error("--branch is not a valid branch name"))
    if args.cmd == "snapshot":
        cur = protection_get(args, args.branch)
        digest = sha256_bytes(dump(cur))
        if not args.execute:
            res.step("snapshot", "DRY_PLAN", {"branch": args.branch, "protected": cur["protected"], "sha256": digest})
            res.status, res.next_action = "NOT_RUN", "re-run with --execute to record the snapshot"
            return
        with Journal(args) as j:
            write_private(protection_file(args, args.branch).parent, ((protection_file(args, args.branch).name, dump(cur)),))
            j.append(key=f"protection:{args.repo}:{args.branch}", state="snapshot", sha256=digest)
        res.step("snapshot", "RECORDED", {"branch": args.branch, "protected": cur["protected"], "sha256": digest})
        return
    cmp_ = protection_compare(args, args.branch)
    if cmp_ is None:
        raise Refusal("NO_SNAPSHOT", f"no protection snapshot recorded for {args.branch}; run `protection snapshot --execute` first")
    if cmp_[2]:
        res.step("verify", "PROTECTION_DRIFT", {"branch": args.branch, "snapshot_sha256": cmp_[0], "current_sha256": cmp_[1], "changed": cmp_[2]})
        res.status, res.next_action = "FAIL", f"restore the protection with {ROLLBACK}; this tool never changes protection"
        return
    res.step("verify", "UNCHANGED", {"branch": args.branch, "sha256": cmp_[1]})


def release_promote(args, res):
    c = release_ctx(args, res)
    rl, ln, clone, version = c.rl, c.ln, args.clone, c.version
    go = read_go(args, rl, ln, version)
    need_scope(go, "promote")
    cand, tag = go["candidate_sha"], go["candidate_tag"]
    res.env["idempotency_key"] = f"promote:{args.repo}:{version}:{cand}"
    res.step("go", "ACCEPTED", {"scope": "promote", "candidate_tag": tag, "candidate_sha": cand})
    if commit_of(clone, cand) != cand or not is_ancestor(clone, cand, c.line_ref):
        raise Refusal("CANDIDATE_NOT_ON_LINE", f"{cand[:12]} is not reachable from {ln['integration_branch']}")
    if commit_of(clone, f"refs/tags/{tag}") != cand:
        raise Refusal("GO_MISMATCH", f"tag {tag} does not point at the approved candidate {cand[:12]}")
    base_ref = need_ref(clone, c.base)
    head, bp = f"promote/{version}", ln["publication_base"]
    done = scan_promotion(clone, base_ref, cand)
    snap = protection_file(args, c.base).exists()
    with (Journal(args) if args.execute else contextlib.nullcontext()) as j:
        drift = []
        try:
            if snap and args.execute:
                before = protection_compare(args, c.base)
                res.step("protection_before", "DRIFT" if before[2] else "UNCHANGED", {"changed": before[2]})
                if before[2]:
                    raise Refusal("PROTECTION_DRIFT", f"{c.base} protection differs from its snapshot; restore it ({ROLLBACK})", "FAIL")
            promote_body(args, res, c, go, cand, tag, base_ref, head, bp, done, j)
        finally:
            if snap and args.execute:
                try:
                    after = protection_compare(args, c.base)
                    res.step("protection_after", "DRIFT" if after[2] else "UNCHANGED", {"changed": after[2]})
                    drift = after[2]
                except Refusal as e:
                    res.step("protection_after", "NOT_RUN", {"reason": e.code})
        if drift:
            res.status, res.next_action = "FAIL", f"PROTECTION_DRIFT: restore {c.base} protection with {ROLLBACK}"
    if not snap:
        res.step("protection", "NOT_RUN", {"reason": "no snapshot recorded for the publication base; run `protection snapshot --execute` before an admin step"})


def promote_body(args, res, c, go, cand, tag, base_ref, head, bp, done, j):
    rl, ln, clone, version = c.rl, c.ln, args.clone, c.version
    if done is None:
        if not ln["support"] == "supported":
            raise Refusal("LINE_NOT_SUPPORTED", f"line {ln['id']} is not supported")
        cur = ln
        while cur["kind"] == "successor":
            pred = line_by_id(c.entry, cur["predecessor"]["line"])
            try:
                link = integration_link(clone, rl, pred, cur)
            except Refusal as e:
                raise Refusal("PREDECESSOR_NOT_SHIPPED", f"{pred['id']} {cur['predecessor']['version']} is not shipped ({e.code}: {e.detail})")
            if not link["integrated"]:
                raise Refusal("PREDECESSOR_NOT_INTEGRATED", f"{link['final_tag']} is not integrated into {cur['id']}; run `train integrate` first")
            if not is_ancestor(clone, link["candidate"], cand):
                raise Refusal("STALE_CANDIDATE", f"candidate {cand[:12]} was cut before {link['final_tag']} was integrated; cut a new candidate")
            cur = pred
        res.step("predecessors", "SHIPPED_AND_INTEGRATED" if ln["kind"] == "successor" else "NONE", {})
        if bp["kind"] == "maintenance":
            others = sorted((x for x in final_tags(clone, rl, version.split(".")[0]) if x[1] != version), reverse=True)
            if others:
                latest = others[0]
                if not is_ancestor(clone, latest[2] + "^{commit}", base_ref):
                    raise Refusal("BASE_MISSING_FINAL", f"{bp['branch']} does not contain the latest same-major final tag {latest[2]}")
                if vkey(version) <= latest[0]:
                    raise Refusal("VERSION_NOT_GREATER", f"{version} is not greater than the shipped {latest[1]} within major {version.split('.')[0]}")
            res.step("maintenance_base", "OK", {"base": bp["branch"], "latest_final": others[0][2] if others else None})
        n, bad = accounting_complete(args, c, cand, base_ref)
        if bad:
            res.step("forward_port_accounting", "INCOMPLETE", {"sources": n, "incomplete": bad[:50]})
            raise Refusal("FORWARD_PORT_INCOMPLETE", f"{len(bad)} base-only change(s) are not forward-port accounted; run `forward-port run` first")
        res.step("forward_port_accounting", "COMPLETE", {"sources": n})
    need_merge_tree(clone)
    base_tip = commit_of(clone, base_ref)
    if done is None:
        if is_ancestor(clone, cand, base_tip):
            raise Refusal("CANDIDATE_ALREADY_ON_BASE", f"candidate {cand[:12]} is already on {bp['branch']} without a promotion merge of its own")
        rc, out = git(clone, "merge-tree", "--write-tree", base_tip, cand)
        if rc == 1:
            raise Refusal("MERGE_CONFLICT", f"merging {cand[:12]} into {bp['branch']} conflicts; nothing was pushed")
        if rc != 0 or not out.split():
            raise Refusal("MERGE_TREE_FAILED", "git merge-tree could not evaluate the promotion")
        predicted = out.split()[0]
        carry = predicted == git(clone, "rev-parse", cand + "^{tree}")[1].strip()
        res.step("prediction", "CLEAN", {"base_tip": base_tip, "predicted_tree": predicted, "verification": "CARRIED_OVER" if carry else "REQUIRED"})
    remote = ls_remote(clone, f"refs/heads/{head}")
    if remote is not None and remote != cand:
        raise Refusal("CONFLICT_EXISTING", f"{head} exists at {remote}, not the approved candidate {cand}")
    if done is None and remote is None:
        if not args.execute:
            res.step("head", "DRY_PLAN", {"would_push": f"{cand}:refs/heads/{head}"})
        elif push_new_ref(clone, cand, f"refs/heads/{head}"):
            res.step("head", "CREATED", {"sha": cand})
        else:
            raise Refusal("CONFLICT_EXISTING", f"{head} does not show {cand} on origin after the push")
    elif remote is not None:
        res.step("head", "REUSED", {"sha": cand})
    if done is None:
        pr = ensure_pr(res, args, head, bp["branch"], f"Promote {version} to {bp['branch']}",
                       f"Promote approved candidate {tag} ({cand}) to {bp['branch']} as a merge commit. The merge is made only by "
                       "`release-line.py release promote --execute` under a recorded GO.", refuse_merged=False) if remote is not None or args.execute else None
        if pr is None:
            res.status, res.next_action = "NOT_RUN", "re-run with --execute to push the head, open the PR and merge"
            return
        if not args.execute:
            res.status, res.next_action = "NOT_RUN", f"re-run with --execute to merge PR #{pr['number']} (merge commit pinned to {cand[:12]})"
            return
        if not hosted_ci(res, args, pr["number"], cand):
            return
        rc, _ = run(["gh", "pr", "merge", str(pr["number"]), "--merge", "--match-head-commit", cand, "--repo", args.repo])
        final, _ = merged_pr_commit(args.repo, pr["number"]) if rc == 0 or promotion_prs(args, c.version, bp["branch"])[1] else (None, None)
        if final is None:
            raise Refusal("MERGE_FAILED", f"gh pr merge #{pr['number']} did not merge; a failing required check or protection rule is never bypassed")
        fetch(clone)
        res.step("merge", "MERGED", {"pr": pr["number"], "final_sha": final})
        pr_number = pr["number"]
    else:
        final, pr_number = done, (promotion_prs(args, c.version, bp["branch"])[1] or [{}])[0].get("number")
        res.step("merge", "ALREADY_PROMOTED", {"final_sha": final, "pr": pr_number})
    if commit_of(clone, final) != final:
        fetch(clone)
    tree, first_parent = check_promotion(clone, final, cand)
    carry = tree == git(clone, "rev-parse", cand + "^{tree}")[1].strip()
    res.step("assertions", "PASS", {"final_sha": final, "second_parent": cand, "tree": tree, "first_parent": first_parent})
    res.step("verification", "CARRIED_OVER" if carry else "REQUIRED", {"candidate_tree_equals_final": carry})
    if j:
        j.append(key=res.env["idempotency_key"], state="promoted", final_sha=final, candidate_sha=cand, pr=pr_number, line=c.ln["id"], version=version)
    res.next_action = ("`release verify`" if not carry else "none for verification (candidate tree carries over)") + "; then `release final-tag` under a GO with the final_tag scope"


def release_verify(args, res):
    c = release_ctx(args, res)
    clone, version, bp = args.clone, c.version, c.ln["publication_base"]
    base_ref = need_ref(clone, c.base)
    pr, merged = promotion_prs(args, version, bp["branch"], strict=True)
    if not merged:
        raise Refusal("PROMOTION_NOT_FOUND", f"no merged promote/{version} PR into {bp['branch']}")
    final, _ = merged_pr_commit(args.repo, merged[0]["number"])
    if commit_of(clone, final) != final:
        raise Refusal("FINAL_NOT_FETCHED", f"promotion merge {final[:12]} is not in the fetched clone")
    cand = git(clone, "rev-parse", final + "^2")[1].strip()
    res.env["idempotency_key"] = f"verify:{args.repo}:{version}:{final}"
    if not is_ancestor(clone, final, base_ref):
        raise Refusal("FINAL_NOT_ON_BASE", f"{final[:12]} is not on {bp['branch']}")
    tree, first = check_promotion(clone, final, cand)
    carry = tree == git(clone, "rev-parse", cand + "^{tree}")[1].strip()
    res.step("promotion", "FOUND", {"pr": merged[0]["number"], "final_sha": final, "candidate_sha": cand})
    if carry:
        res.step("verification", "CARRIED_OVER", {"reason": "the final tree equals the candidate tree, so candidate verification applies"})
        return
    if not args.execute:
        res.step("local_checks", "NOT_RUN", {"reason": "the final tree differs from the candidate tree; re-run with --execute to run the local-first checks at the final SHA"})
        res.status, res.next_action = "NOT_RUN", "re-run with --execute to run the owner's local-first checks in a detached worktree"
        return
    ci = importlib.util.module_from_spec(importlib.util.spec_from_file_location("owner_merge_local_ci", HERE / "owner-merge-local-ci.py"))
    ci.__spec__.loader.exec_module(ci)
    try:
        entry = ci.load_entry(args.repo, args.local_first)
    except (ValueError, OSError) as e:
        res.step("local_checks", "NOT_RUN", {"reason": f"local-first config unavailable: {type(e).__name__}"})
        res.status, res.next_action = "NOT_RUN", "the owner installs the local-first CI config; verification stays NOT_RUN until then"
        return
    logs = os.path.expanduser(args.evidence_dir or omg.LOCAL_EVIDENCE_DIRS["claude"])
    tag = f"{args.repo.replace('/', '__')}-release-{version}"
    wt = tempfile.mkdtemp(prefix="release-verify-")
    results = []
    with Journal(args) as j:
        try:
            if git(clone, "worktree", "add", "--detach", wt, final)[0] != 0:
                raise Refusal("WORKTREE_FAILED", "could not create the detached verification worktree")
            ci.prepare_dir(logs)
            context = {"PULL_BRANCH": f"promote/{version}", "PR_TITLE": f"Promote {version} to {bp['branch']}", "GIT_BASE": first,
                       "CARGO_TARGET_DIR": os.path.join(wt, "target")}
            for name, argv in entry["checks"]:
                r, _ = ci.run_check(wt, name, argv, logs, final, tag, entry.get("env"), context)
                results.append({"name": name, "exit_code": r["exit_code"], "log_sha256": r["log_sha256"]})
            dirty = git(wt, "status", "--porcelain", "--untracked-files=all")[1].strip() != ""
        except (RuntimeError, OSError) as e:
            raise Refusal("VERIFY_ERROR", f"{type(e).__name__}: {e}")
        finally:
            git(clone, "worktree", "remove", "--force", wt)
            shutil.rmtree(wt, ignore_errors=True)
        ok = all(r["exit_code"] == 0 for r in results) and not dirty
        j.append(key=res.env["idempotency_key"], state="verified" if ok else "verify-failed", final_sha=final, checks=results)
    res.step("local_checks", "PASS" if ok else "FAIL", {"final_sha": final, "checks": results, "worktree_changed": dirty})
    if not ok:
        res.status, res.next_action = "FAIL", "a local-first check failed at the final SHA; do not tag or publish"


def release_final_tag(args, res):
    c = release_ctx(args, res)
    rl, ln, clone, version = c.rl, c.ln, args.clone, c.version
    go = read_go(args, rl, ln, version)
    need_scope(go, "final_tag")
    cand = go["candidate_sha"]
    base_ref = need_ref(clone, c.base)
    final = scan_promotion(clone, base_ref, cand)
    if final is None:
        raise Refusal("NOT_PROMOTED", f"no promotion merge of {cand[:12]} on {c.base}; run `release promote` first")
    pr, merged = promotion_prs(args, version, c.base)
    if merged:
        oid, _ = merged_pr_commit(args.repo, merged[0]["number"])
        if oid != final:
            raise Refusal("PROMOTION_MISMATCH", f"PR #{merged[0]['number']} reports merge {oid[:12]}, the base history shows {final[:12]}")
    check_promotion(clone, final, cand)
    if not is_ancestor(clone, final, base_ref):
        raise Refusal("FINAL_NOT_ON_BASE", f"{final[:12]} is not on {c.base}")
    name = rl["tag_format"].replace("{version}", version)
    res.env["idempotency_key"] = f"tag:{args.repo}:{name}"
    res.step("go", "ACCEPTED", {"scope": "final_tag", "candidate_sha": cand})
    res.step("final", "CAPTURED", {"final_sha": final, "base_tip_not_used": commit_of(clone, base_ref)})
    with (Journal(args) if args.execute else contextlib.nullcontext()) as j:
        created = push_tag(res, args, name, final, f"Release {version} ({name}) from line {ln['id']}", "tag")
        if j:
            j.append(key=res.env["idempotency_key"], state="final-tag", tag=name, source_branch=ln["integration_branch"], release_line=ln["id"],
                     version=version, promotion_pr=(merged[0]["number"] if merged else None), candidate_sha=cand, final_sha=final, created=created)
    res.next_action = "re-run with --execute to push the final tag" if res.status == "NOT_RUN" else "none; build, notes and publish are the next slice"


def release_cmd(args, res):
    {"candidate-tag": release_candidate_tag, "promote": release_promote, "verify": release_verify, "final-tag": release_final_tag}[args.cmd](args, res)


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
    common.add_argument("--go-dir", help="release-go records directory (default under ~/.config/coding-agent-environment/local/release-go; must stay under the owner config root)")
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
    fp = top.add_parser("forward-port").add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run", "status"):
        sub = fp.add_parser(name, parents=[common])
        grp = sub.add_mutually_exclusive_group(required=True)
        grp.add_argument("--merge", help="a landed fix: a GitHub merge commit (its second-parent range is inspected) or a plain commit")
        grp.add_argument("--pr", type=int, help="a merged PR number (resolved through gh)")
        grp.add_argument("--range", help="A..B: every first-parent commit in the range is a source")
    rel = top.add_parser("release").add_subparsers(dest="cmd", required=True)
    for name in ("candidate-tag", "promote", "verify", "final-tag"):
        sub = rel.add_parser(name, parents=[common])
        sub.add_argument("--version", required=True, help="the version being released, e.g. 0.3.0")
        sub.add_argument("--line", help="release line id (default: the line carrying the version, else its major line)")
        if name == "candidate-tag":
            sub.add_argument("--sha", help="candidate commit (default: the integration branch tip; must be reachable from it)")
            sub.add_argument("--n", type=int, help="candidate number when no GO record names the tag")
        if name in ("promote", "verify"):
            sub.add_argument("--local-first", help="local-first CI trust file (default: the owner file)")
            sub.add_argument("--evidence-dir", help="local CI evidence directory (default: the host evidence directory)")
    prot = top.add_parser("protection").add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "verify"):
        prot.add_parser(name, parents=[common]).add_argument("--branch", required=True)
    return p


HANDLERS = {("registry", "validate"): registry_validate, ("registry", "show"): registry_show, ("registry", "propose"): propose,
            ("audit", "observe"): observe, ("audit", "import"): audit_import, ("route", None): route,
            ("successor", "ensure"): successor_ensure, ("guard", None): guard, ("train", "integrate"): train_integrate,
            ("train", "converge"): train_converge,
            ("forward-port", "plan"): forward_port, ("forward-port", "run"): forward_port, ("forward-port", "status"): forward_port,
            **{("release", c): release_cmd for c in ("candidate-tag", "promote", "verify", "final-tag")},
            ("protection", "snapshot"): protection_cmd, ("protection", "verify"): protection_cmd}


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
