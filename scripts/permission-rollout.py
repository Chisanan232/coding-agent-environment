#!/usr/bin/env python3
"""Merge this repo's permission baseline into a live Claude Code settings file.

Modes (nothing is written except by `apply` and `rollback`):
  plan      print the exact change set and the sha256 of both inputs
  apply     back up, merge, write atomically; requires BOTH expected hashes
  verify    check the live file against the zero-static-deny policy
  rollback  restore a backup written by `apply`

The merge is additive and conservative: live-only rules and unrelated keys are
preserved; only entries named in MIGRATION (superseded by this baseline) are
removed. No plan file is written. `apply` reads each input once, hashes those
exact bytes, merges them, and writes through a private (0600, exclusive-create)
sibling temp file that is fsynced and atomically renamed; it refuses a symlinked
live file and re-checks the bytes immediately before the rename (a residual race
with a concurrent writer remains between that check and the rename). Managed
(organization) settings are never read or written.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".claude/settings.json"
LIVE = Path.home() / ".claude/settings.json"
TEMPLATE = ROOT / "docs/examples/automode-trusted-sources.example.json"

# Rules superseded by the baseline. Retired only when present verbatim.
MIGRATION = {
    # `rtk terraform plan *` does not match a bare `rtk terraform plan`; the baseline carries `plan:*`.
    "retire_allow": ["Bash(git worktree *)", "Bash(rtk git worktree *)", "Bash(rtk terraform plan *)"],
    "retire_ask": [
        # SPE-105 globs that matched paths containing "-f"/"-B" (e.g. "...-full-...").
        "Bash(git worktree add*-B*)", "Bash(rtk git worktree add*-B*)",
        "Bash(git worktree add*-f*)", "Bash(rtk git worktree add*-f*)",
        "Bash(git worktree remove -f*)", "Bash(rtk git worktree remove -f*)",
        "Bash(git worktree remove * -f*)", "Bash(rtk git worktree remove * -f*)",
        "Bash(git worktree remove --f*)", "Bash(rtk git worktree remove --f*)",
        "Bash(git worktree remove * --f*)", "Bash(rtk git worktree remove * --f*)",
        "Bash(gcloud*projects/*)", "Bash(rtk gcloud*projects/*)",
    ],
    # Retired rule -> narrower replacement kept in live (private overlay rules live there).
    "replace_ask": {
        "Bash(gcloud*projects/*)": "Bash(gcloud secrets*projects/*)",
        "Bash(rtk gcloud*projects/*)": "Bash(rtk gcloud secrets*projects/*)",
    },
    # Former deny rules fully covered by an ask rule in the baseline
    # (`terraform apply:*` and `terraform * apply*` ask on every apply form).
    "deny_subsumed": [
        "Bash(terraform apply -destroy*)", "Bash(terraform apply --destroy*)",
        "Bash(terraform apply * -destroy*)", "Bash(terraform apply * --destroy*)",
        "Bash(terraform * apply *-destroy*)", "Bash(terraform * apply *--destroy*)",
        "Bash(rtk terraform apply -destroy*)", "Bash(rtk terraform apply --destroy*)",
        "Bash(rtk terraform apply * -destroy*)", "Bash(rtk terraform apply * --destroy*)",
        "Bash(rtk terraform * apply *-destroy*)", "Bash(rtk terraform * apply *--destroy*)",
        "Bash(rtk terraform destroy *)",
        # Broad forms that matched "destroy" inside arguments; real destroys are covered by
        # `destroy:*` and the leading-dash `terraform -* destroy*` form.
        "Bash(terraform * destroy*)", "Bash(rtk terraform * destroy*)",
    ],
    # Environment lines retired unconditionally: stale session snapshots, lines that
    # contradict the ownership policy, and older copies of lines the baseline carries.
    "retire_env_always": [
        "### ", "routine under ", "**Trusted repo**", "**Worktree cleanup**",
        "**git -C convention**",
        "**Force-push**: every force-push (--force,", "**Command rewriting**: a PreToolUse hook (`rtk hook claude`)",
        "**File/directory deletion (2026-10-10)**", "**gh api method classification (2026-10-10)**",
    ],
    # Copies of the built-in defaults (restored by "$defaults"). Retired ONLY when the
    # text still carries default wording, so a curated line with the same key survives.
    "retire_env_default_copies": [
        "**Repository visibility**", "**Internal sharing", "**Sensitive data locations",
        "**Sensitive remote targets**", "**Protected deployment namespaces", "**Protected IaC scopes**",
        "**Primary use of Claude Code**",
    ],
    "default_copy_signatures": [
        "assume private unless the remote host and repo name indicate otherwise",
        "treat public paste/gist services as outside the trust boundary",
        "any file or store holding personal data, confidential business data",
        "any namespace, host, or container whose name carries `prod`",
        "IAM, RBAC, networking, quota, and node-pool resources",
        ": software development",
    ],
    # Content markers of a generated, session-specific snapshot.
    "stale_env_markers": ["None configured", "no remotes", "local-only checkout", "origin/HEAD unset", "The trusted repo only"],
    "retire_allow_prefixes": ["**Evidence-gated pull request merge**"],
}

# Commands that were hard-denied before the zero-deny policy; each must now ask.
FORMER_DENY_COMMANDS = [
    "terraform destroy", "terraform -chdir=x destroy", "terraform apply -destroy",
    "terraform apply --destroy -auto-approve", "terraform -chdir=x apply -destroy",
    "gcloud secrets versions access latest --secret=s",
    "gcloud --project=p secrets versions access latest --secret=s",
    "rtk proxy git log",
]


def baseline_not_merged(path):
    """Return a reason string unless `path` is a tracked, committed file of this repo
    whose HEAD is contained in origin/main (fetch first; the check uses the local ref)."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(ROOT)
    except ValueError:
        return f"baseline {p} is outside the repository {ROOT}"

    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)

    if git("ls-files", "--error-unmatch", str(rel)).returncode != 0:
        return "baseline is not tracked by git"
    if git("diff", "--quiet", "HEAD", "--", str(rel)).returncode != 0:
        return "baseline has uncommitted changes"
    if git("rev-parse", "--verify", "-q", "origin/main").returncode != 0:
        return "origin/main is unknown; run `git fetch origin main` first"
    if git("merge-base", "--is-ancestor", "HEAD", "origin/main").returncode != 0:
        return "HEAD is not contained in origin/main (the baseline is not merged yet)"
    return None


# IAM mutations and credential minting must prompt (SPE-106 follow-up).
IAM_COMMANDS = [
    "gcloud projects add-iam-policy-binding p --member=m --role=r",
    "gcloud --project=p projects add-iam-policy-binding p --member=m --role=r",
    "gcloud iam service-accounts add-iam-policy-binding s --member=m --role=r",
    "gcloud projects remove-iam-policy-binding p --member=m --role=r",
    "gcloud projects set-iam-policy p f.json",
    "gcloud iam service-accounts keys create k.json --iam-account=s",
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rule_regex(rule):
    import re
    body = rule[len("Bash("):-1]
    if body.endswith(":*"):
        return re.compile("^" + re.escape(body[:-2]) + "(?: .*)?$", re.S)
    return re.compile("^" + ".*".join(re.escape(x) for x in body.split("*")) + "$", re.S)


def static_decision(command, perm):
    for bucket in ("deny", "ask", "allow"):
        for rule in perm.get(bucket, []):
            if rule.startswith("Bash(") and rule_regex(rule).match(command):
                return bucket
    return "classifier"


def rewrite_env_line(line):
    """Keep live-only guidance, drop only the sentence that is no longer true."""
    if line.startswith("**Secrets**: never materialize"):
        return re.sub(r"\s*[^.]*denied by rule[^.]*\.?", "", line).rstrip()
    return line


def is_stale_env(line):
    if line.startswith(tuple(MIGRATION["retire_env_always"])) or any(m in line for m in MIGRATION["stale_env_markers"]):
        return True
    return line.startswith(tuple(MIGRATION["retire_env_default_copies"])) and any(s in line for s in MIGRATION["default_copy_signatures"])


# Entries written by this tool are replaced (not duplicated) on every apply.
OWNER_PREFIXES = ("**Trusted GitHub owners", "**Personal source-control scope")
DEV_ENV_PREFIXES = ("**Personal development machine",)
DEV_ALLOW_PREFIXES = ("**Cross-repository personal development",)
HIDDEN_OR_SYSTEM_HOME_DIRS = {"library", "applications"}
SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", "keychains", ".claude", ".kube", ".docker", ".netrc", ".npmrc", "gcloud"}
ENV_READ_ASKS = ["Read(**/.env)", "Read(**/.env.local)"]
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def normalize_dev_dir(d):
    """Return (resolved absolute path, None) or (None, reason).

    Resolves `~`, symlinks and case before judging, so `~/.`, `~/dev/../..`,
    `/users/me` and symlinks into credential directories cannot slip through.
    The root must sit strictly under the home directory (not in a hidden or
    system top-level folder) or be a directory under /Volumes/<name>/.
    """
    if not d or any(c in d for c in "`\n\r\t"):
        return None, f"dev dir {d!r} is empty or has control/backtick characters"
    if d.startswith("~") and not (d == "~" or d.startswith("~/")):
        return None, f"dev dir {d!r} uses ~user, which is not allowed"
    if any(seg in (".", "..") for seg in d.split("/")):
        return None, f"dev dir {d!r} contains a '.' or '..' segment"
    real = os.path.realpath(os.path.expanduser(d)).rstrip("/") or "/"
    home = os.path.realpath(str(Path.home())).rstrip("/")
    low, hlow = real.casefold(), home.casefold()
    if low == hlow:
        return None, f"dev dir {d!r} is the home directory"
    if low.startswith(hlow + "/"):
        rel = real[len(home) + 1:]
        first = rel.split("/")[0]
        if first.startswith(".") or first.casefold() in HIDDEN_OR_SYSTEM_HOME_DIRS:
            return None, f"dev dir {d!r} is inside a hidden, Library or Applications folder"
    elif not re.match(r"^/Volumes/[^/]+/[^/]+", real, re.I):
        return None, f"dev dir {d!r} resolves to {real!r}: must be under the home directory or /Volumes/<name>/<dir>"
    rel_parts = {p.casefold() for p in real.split("/")}
    if rel_parts & SENSITIVE_PARTS:
        return None, f"dev dir {d!r} overlaps a credential or protected path"
    return real, None


def check_dev_dir(d):
    return normalize_dev_dir(d)[1]


def _fill(lines, owners, dev_dirs):
    out = []
    for line in lines:
        if "<owners>" in line:
            line = line.replace("<owners>", ", ".join(f"`{o}`" for o in owners))
        if "<dev-dirs>" in line:
            line = line.replace("<dev-dirs>", ", ".join(f"`{d}`" for d in dev_dirs))
        out.append(line)
    return out


def template_lines(owners, dev_dirs):
    tpl = json.loads(TEMPLATE.read_text())["autoMode"]
    env = [l for l in tpl["environment"]
           if (("<owners>" in l and owners) or ("<dev-dirs>" in l and dev_dirs))]
    allow = tpl.get("allow", []) if dev_dirs else []
    return _fill(env, owners, dev_dirs), _fill(allow, owners, dev_dirs)


def _bad_dir(d):
    raise ValueError(f"invalid dev dir: {check_dev_dir(d)}")


def merge(live, base, owners, dev_dirs=()):
    new = copy.deepcopy(live)
    perm = new.setdefault("permissions", {})
    for k in ("allow", "ask", "deny"):
        perm.setdefault(k, [])
    perm["allow"] = [r for r in perm["allow"] if r not in MIGRATION["retire_allow"]]
    replaced = [MIGRATION["replace_ask"][r] for r in perm["ask"] if r in MIGRATION["replace_ask"]]
    perm["ask"] = [r for r in perm["ask"] if r not in MIGRATION["retire_ask"]]
    for k in ("allow", "ask"):
        perm[k] += [r for r in base["permissions"][k] if r not in perm[k]]
    perm["ask"] += [r for r in replaced if r not in perm["ask"]]
    # A deny rule from either input becomes ask (never dropped, never allowed).
    denied = perm["deny"] + [r for r in base["permissions"].get("deny", []) if r not in perm["deny"]]
    moved = [r for r in denied if r not in MIGRATION["deny_subsumed"] and r not in perm["ask"]]
    perm["ask"] += moved
    perm["deny"] = []
    dev_dirs = list(dict.fromkeys(normalize_dev_dir(d)[0] or _bad_dir(d) for d in dev_dirs))

    am = new.setdefault("autoMode", {})
    previous_dev_dirs = []
    for e in am.get("environment", []):
        if e.startswith(DEV_ENV_PREFIXES):
            previous_dev_dirs += [x for x in re.findall(r"`([^`]+)`", e) if x.startswith(("/", "~"))]
    kept = [rewrite_env_line(e) for e in am.get("environment", [])
            if e != "$defaults" and not is_stale_env(e)]
    wanted = [e for e in base["autoMode"]["environment"] if e != "$defaults"]
    am["environment"] = ["$defaults"] + [e for e in wanted if e not in kept] + kept
    env_add, allow_add = template_lines(owners, dev_dirs)
    if owners:
        am["environment"] = [e for e in am["environment"] if not e.startswith(OWNER_PREFIXES)]
    if dev_dirs:
        am["environment"] = [e for e in am["environment"] if not e.startswith(DEV_ENV_PREFIXES)]
    am["environment"] += [e for e in env_add if e not in am["environment"]]
    wanted_allow = [e for e in base["autoMode"]["allow"] if e != "$defaults"]
    old_allow = [e for e in am.get("allow", []) if e != "$defaults" and e not in wanted_allow
                 and not e.startswith(tuple(MIGRATION["retire_allow_prefixes"]))]
    if dev_dirs:
        old_allow = [e for e in old_allow if not e.startswith(DEV_ALLOW_PREFIXES)]
    am["allow"] = ["$defaults"] + wanted_allow + old_allow + [e for e in allow_add if e not in old_allow]
    if dev_dirs:
        # Replace what a previous apply granted (read back from the old prose) so a
        # narrower --dev-dirs narrows the access grant instead of only growing it.
        have = [d for d in perm.get("additionalDirectories", []) if d not in previous_dev_dirs]
        perm["additionalDirectories"] = have + [d for d in dev_dirs if d not in have]
        perm["ask"] += [r for r in ENV_READ_ASKS if r not in perm["ask"]]
    return new


CATCH_ALL_ALLOWS = {"Bash", "Bash(*)", "Bash(:*)", "*", "Edit", "Write", "Read(*)", "mcp__*"}


def problems(live, base=None):
    out = []
    p = live.get("permissions", {})
    if base is not None:
        missing = [r for r in base["permissions"]["ask"] if r not in p.get("ask", [])]
        if missing:
            out.append(f"baseline ask rules missing from the result: {missing[:5]}")
    catch = [r for r in p.get("allow", []) if r in CATCH_ALL_ALLOWS]
    if catch:
        out.append(f"catch-all allow rule(s) would outrank the classifier: {catch}")
    if p.get("deny"):
        out.append(f"permissions.deny is not empty: {len(p['deny'])} entries")
    if p.get("defaultMode") != "auto":
        out.append(f"defaultMode is {p.get('defaultMode')!r}, expected 'auto'")
    if "bypassPermissions" in json.dumps(live.get("permissions", {})) or live.get("permissions", {}).get("defaultMode") == "bypassPermissions":
        out.append("bypassPermissions is configured")
    for cmd in FORMER_DENY_COMMANDS:
        for pre in ("", "rtk "):
            if static_decision(pre + cmd, p) != "ask" and not (pre == "rtk " and cmd.startswith("rtk ")):
                out.append(f"former-deny command does not ask: {pre + cmd!r} -> {static_decision(pre + cmd, p)}")
    for cmd in IAM_COMMANDS:
        for pre in ("", "rtk "):
            if static_decision(pre + cmd, p) != "ask":
                out.append(f"IAM command does not ask: {pre + cmd!r} -> {static_decision(pre + cmd, p)}")
    if "mcp__neon__get_connection_string" not in p.get("ask", []):
        out.append("mcp__neon__get_connection_string is not ask-gated")
    if any(r.startswith(("Bash(git push", "Bash(rtk git push")) for r in p.get("allow", [])):
        out.append("an allow rule matches git push (SPE-83)")
    if any("versions access" in r for r in p.get("allow", [])):
        out.append("an allow rule grants secret payload reads")
    am = live.get("autoMode", {})
    env = am.get("environment", [])
    if "$defaults" not in env:
        out.append('autoMode.environment lacks "$defaults" (replaces the built-in environment)')
    if "$defaults" not in am.get("allow", []):
        out.append('autoMode.allow lacks "$defaults"')
    stale = [e[:60] for e in env if e != "$defaults" and is_stale_env(e)]
    if stale:
        out.append(f"stale environment lines remain: {stale}")
    for d in p.get("additionalDirectories", []):
        why = check_dev_dir(d)
        if why:
            out.append(f"permissions.additionalDirectories: {why}")
    for k in ("soft_deny", "hard_deny"):
        if k in am:
            out.append(f"autoMode.{k} is overridden; platform defaults must stay intact")
    return out


def summary(live, new):
    lines = []
    for k in ("allow", "ask", "deny"):
        a, b = set(live["permissions"].get(k, [])), set(new["permissions"].get(k, []))
        lines.append(f"permissions.{k}: {len(a)} -> {len(b)} (+{len(b - a)} -{len(a - b)})")
    a, b = live.get("autoMode", {}), new.get("autoMode", {})
    for k in ("environment", "allow"):
        x, y = set(a.get(k, [])), set(b.get(k, []))
        lines.append(f"autoMode.{k}: {len(x)} -> {len(y)} (+{len(y - x)} -{len(x - y)})")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["plan", "apply", "verify", "rollback"])
    ap.add_argument("--baseline", default=str(BASELINE))
    ap.add_argument("--live", default=str(LIVE))
    ap.add_argument("--owners", default="", help="comma-separated trusted GitHub owners (account + administered orgs)")
    ap.add_argument("--dev-dirs", default="", help="comma-separated personal development roots, e.g. ~/Bryant-Developments")
    ap.add_argument("--unmerged-ok", action="store_true",
                    help="rehearsal only: allow a baseline that is not merged to origin/main (never for the real live file)")
    ap.add_argument("--expect-live-sha")
    ap.add_argument("--expect-baseline-sha")
    ap.add_argument("--backup", help="backup file to restore (rollback)")
    a = ap.parse_args(argv)
    a.baseline_given = any(x == "--baseline" or x.startswith("--baseline=") for x in (argv if argv is not None else sys.argv[1:]))
    owners = [o.strip() for o in a.owners.split(",") if o.strip()]
    dev_dirs = [d.strip() for d in a.dev_dirs.split(",") if d.strip()]
    for o in owners:
        if not OWNER_RE.match(o):
            print(f"REFUSING: owner {o!r} is not a valid GitHub login")
            return 6
    for d in dev_dirs:
        why = check_dev_dir(d)
        if why:
            print("REFUSING: " + why)
            return 6
    live_p = Path(a.live)

    if a.mode == "rollback":
        return rollback(live_p, a.backup)
    if a.mode == "verify":
        base_for_verify = json.loads(Path(a.baseline).read_text()) if a.baseline_given else None
        found = problems(json.loads(live_p.read_text()), base_for_verify)
        print("\n".join(found) if found else "verify OK: zero static deny, former-deny commands ask, defaults intact")
        return 1 if found else 0

    if live_p.is_symlink():
        print(f"REFUSING: {live_p} is a symlink; resolve it and pass the real path")
        return 4
    base_bytes = Path(a.baseline).read_bytes()
    raw_bytes = live_p.read_bytes()
    raw = raw_bytes.decode()
    live_sha, base_sha = hashlib.sha256(raw_bytes).hexdigest(), hashlib.sha256(base_bytes).hexdigest()
    base = json.loads(base_bytes)
    live = json.loads(raw)
    new = merge(live, base, owners, dev_dirs)
    bad = problems(new, base)
    print(f"live     sha256 {live_sha}\nbaseline sha256 {base_sha}")
    print("\n".join(summary(live, new)))
    gone = [e for e in live.get("autoMode", {}).get("environment", []) if e not in new["autoMode"]["environment"]]
    print(f"environment lines removed ({len(gone)}):")
    print("\n".join("  - " + e[:110].replace("\n", " ") for e in gone))
    if bad:
        print("REFUSING, merged result violates policy:\n  " + "\n  ".join(bad))
        return 2
    if a.mode == "plan":
        return 0
    if not a.unmerged_ok:
        why = baseline_not_merged(a.baseline)
        if why:
            print(f"REFUSING: {why}. Deploy only a merged baseline (or pass --unmerged-ok for a rehearsal on a copy).")
            return 7
    if a.expect_live_sha != live_sha or a.expect_baseline_sha != base_sha:
        print("REFUSING: --expect-live-sha / --expect-baseline-sha do not match the inputs; re-review required")
        return 3
    backups = Path.home() / ".claude/backups"
    backups.mkdir(parents=True, exist_ok=True)
    # Nanosecond name + exclusive create: a second apply can never overwrite the
    # backup that holds the original file.
    backup = backups / f"settings.json.pre-SPE-106.{time.time_ns()}"
    write_new(backup, raw_bytes, live_p.stat().st_mode & 0o777)
    indent = 4 if '\n    "' in raw else 2
    out = json.dumps(new, indent=indent, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
    if live_p.read_bytes() != raw_bytes:
        print("REFUSING: the live file changed while the merge was prepared; nothing was written")
        return 5
    atomic_write(live_p, out.encode())
    print(f"applied | backup {backup} | rollback: python3 {sys.argv[0]} rollback --live {live_p} --backup {backup}")
    return 0


def write_new(path, data, mode):
    """Exclusive create with the intended mode from the first byte (no umask-looser window)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        os.unlink(path)
        raise


def atomic_write(path, data):
    """Private exclusive-create sibling temp, fsync, mode copied before the rename."""
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".spe106.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        json.loads(Path(tmp).read_text())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def rollback(live_p, backup):
    if not backup:
        print("rollback needs --backup")
        return 2
    if live_p.is_symlink():
        print(f"REFUSING: {live_p} is a symlink")
        return 4
    data = Path(backup).read_bytes()
    json.loads(data)
    current = live_p.read_bytes() if live_p.exists() else None
    if current is not None:
        saved = live_p.with_name(f"{live_p.name}.pre-rollback.{time.time_ns()}")
        write_new(saved, current, live_p.stat().st_mode & 0o777)
        print(f"current file saved to {saved}")
    if live_p.exists():
        atomic_write(live_p, data)
    else:
        write_new(live_p, data, Path(backup).stat().st_mode & 0o777)
    print(f"restored {live_p} from {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
