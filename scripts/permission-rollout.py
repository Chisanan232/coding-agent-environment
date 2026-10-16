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
import shutil
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
    # Environment lines that are stale snapshots, copies of the built-in
    # defaults (restored by "$defaults"), or contradict the ownership policy.
    "retire_env_prefixes": [
        "### ", "routine under ", "**Trusted repo**", "**Worktree cleanup**",
        "**git -C convention**", "**Secrets**: never materialize",
        "**Repository visibility**", "**Internal sharing", "**Sensitive data locations",
        "**Sensitive remote targets**", "**Protected deployment namespaces", "**Protected IaC scopes**",
        "**Primary use of Claude Code**",
        # Older live copies of lines the baseline now carries (kept once, in the baseline).
        "**Force-push**: every force-push (--force,", "**Command rewriting**: a PreToolUse hook (`rtk hook claude`)",
        "**File/directory deletion (2026-10-10)**", "**gh api method classification (2026-10-10)**",
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


def is_stale_env(line):
    return line.startswith(tuple(MIGRATION["retire_env_prefixes"])) or any(m in line for m in MIGRATION["stale_env_markers"])


# Entries written by this tool are replaced (not duplicated) on every apply.
OWNER_PREFIXES = ("**Trusted GitHub owners", "**Personal source-control scope")
DEV_ENV_PREFIXES = ("**Personal development machine",)
DEV_ALLOW_PREFIXES = ("**Cross-repository personal development",)
FORBIDDEN_DEV_DIRS = {"/", "/Users", "/Library", "/System", "/private", "/etc", "/usr", "/var", "/bin", "/sbin", "/opt", "/Volumes", "~", "~/"}
SYSTEM_PREFIXES = ("/System/", "/Library/", "/etc/", "/usr/", "/bin/", "/sbin/", "/private/etc", "/private/var", "/var/", "/dev/", "/Applications/")
SENSITIVE_PARTS = (".ssh", ".aws", ".gnupg", ".config/gcloud", "Keychains", ".claude")


def check_dev_dir(d):
    """Reject roots that would turn 'development trust' into broad filesystem access."""
    norm = d.rstrip("/") or "/"
    if norm in FORBIDDEN_DEV_DIRS or norm == str(Path.home()):
        return f"dev dir {d!r} is too broad (filesystem root, system path or the home directory)"
    if norm.startswith(SYSTEM_PREFIXES):
        return f"dev dir {d!r} is inside a system path"
    if any(part in norm for part in SENSITIVE_PARTS):
        return f"dev dir {d!r} overlaps a credential or protected path"
    if not (norm.startswith("~/") or norm.startswith("/")):
        return f"dev dir {d!r} must be absolute or start with ~/"
    return None


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

    am = new.setdefault("autoMode", {})
    kept = [e for e in am.get("environment", [])
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
        have = perm.setdefault("additionalDirectories", [])
        have += [d for d in dev_dirs if d not in have]
    return new


def problems(live, base=None):
    out = []
    p = live.get("permissions", {})
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
    ap.add_argument("--expect-live-sha")
    ap.add_argument("--expect-baseline-sha")
    ap.add_argument("--backup", help="backup file to restore (rollback)")
    a = ap.parse_args(argv)
    owners = [o.strip() for o in a.owners.split(",") if o.strip()]
    dev_dirs = [d.strip() for d in a.dev_dirs.split(",") if d.strip()]
    for d in dev_dirs:
        why = check_dev_dir(d)
        if why:
            print("REFUSING: " + why)
            return 6
    live_p = Path(a.live)

    if a.mode == "rollback":
        return rollback(live_p, a.backup)
    if a.mode == "verify":
        found = problems(json.loads(live_p.read_text()))
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
    if bad:
        print("REFUSING, merged result violates policy:\n  " + "\n  ".join(bad))
        return 2
    if a.mode == "plan":
        return 0
    if a.expect_live_sha != live_sha or a.expect_baseline_sha != base_sha:
        print("REFUSING: --expect-live-sha / --expect-baseline-sha do not match the inputs; re-review required")
        return 3
    backups = Path.home() / ".claude/backups"
    backups.mkdir(parents=True, exist_ok=True)
    # Nanosecond name + exclusive create: a second apply can never overwrite the
    # backup that holds the original file.
    backup = backups / f"settings.json.pre-SPE-106.{time.time_ns()}"
    with open(backup, "xb") as fh:
        fh.write(raw_bytes)
    shutil.copystat(live_p, backup)
    indent = 4 if '\n    "' in raw else 2
    out = json.dumps(new, indent=indent, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
    if live_p.read_bytes() != raw_bytes:
        print("REFUSING: the live file changed while the merge was prepared; nothing was written")
        return 5
    atomic_write(live_p, out.encode())
    print(f"applied | backup {backup} | rollback: {sys.argv[0]} rollback --live {live_p} --backup {backup}")
    return 0


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
        with open(saved, "xb") as fh:
            fh.write(current)
        print(f"current file saved to {saved}")
    atomic_write(live_p, data) if live_p.exists() else live_p.write_bytes(data)
    print(f"restored {live_p} from {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
