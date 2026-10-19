#!/usr/bin/env python3
"""Hash-pinned, owner-operated merge of a settings candidate into a settings file (SPE-116).

  candidate-rollout.py plan     --live F --candidate F [--baseline F] [--hook-command CMD [--fail-closed]]
  candidate-rollout.py apply    --live F --candidate F [--baseline F] [--hook-command CMD] --expect-live-sha S --expect-result-sha S [--backup-dir D]
  candidate-rollout.py verify   --live F --expect-result-sha S
  candidate-rollout.py rollback --live F --backup F --expect-backup-sha S

Only three things are merged: the `autoMode` block is replaced by the candidate's, `permissions.ask` becomes the
ordered union of live, baseline and candidate asks, and (optionally) one PreToolUse hook entry is added. Everything
else (`allow`, `deny`, `env`, `model`, `statusLine`, other hooks) is left untouched, and the baseline's own `autoMode`
text is never applied. `plan` and `verify` write nothing. `apply` and `rollback` write the live file, so the OWNER runs
them; they refuse unless the file's current hash equals the one pinned from `plan`. Values under `env` are never printed.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("wsp", HERE / "workstation-policy.py")
wsp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsp)
HOOK_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def dump(cfg):
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def load(path):
    raw = pathlib.Path(path).read_bytes()
    return raw, json.loads(raw)


def union(*lists):
    out, seen = [], set()
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def merge(live, candidate, baseline=None, hook_command=None, fail_closed=False):
    """Pure function: returns (merged, report). `live` is not mutated."""
    merged = json.loads(json.dumps(live))
    report = {"autoMode_before": {k: len(v) for k, v in (live.get("autoMode") or {}).items() if isinstance(v, list)},
              "autoMode_after": {k: len(v) for k, v in candidate["autoMode"].items() if isinstance(v, list)}}
    merged["autoMode"] = json.loads(json.dumps(candidate["autoMode"]))
    perms = merged.setdefault("permissions", {})
    before = list(perms.get("ask", []))
    base_asks = ((baseline or {}).get("permissions") or {}).get("ask", [])
    perms["ask"] = union(before, base_asks, candidate["permissions"]["ask"])
    report["asks_added"] = [a for a in perms["ask"] if a not in before]
    report["asks_kept"] = len(before)
    report["hook_added"] = False
    if hook_command:
        entry = {"type": "command", "command": hook_command}
        if fail_closed:
            entry["onFailure"] = "block"
        groups = merged.setdefault("hooks", {}).setdefault("PreToolUse", [])
        if not any(h.get("command") == hook_command for g in groups for h in g.get("hooks", [])):
            groups.append({"matcher": HOOK_MATCHER, "hooks": [entry]})
            report["hook_added"] = True
    return merged, report


def compat(live, merged, candidate):
    """Findings the owner should read before applying. Informational unless noted BLOCKING."""
    out = []
    for problem in wsp.lint_settings(candidate):
        out.append(f"BLOCKING candidate lint: {problem}")
    am = merged.get("autoMode") or {}
    for key in ("allow", "soft_deny", "hard_deny", "environment"):
        if key in am and (not am[key] or am[key][0] != "$defaults"):
            out.append(f"BLOCKING autoMode.{key} lost $defaults")
    live_allow = (live.get("permissions") or {}).get("allow", [])
    for problem in wsp.lint_settings({"permissions": {"allow": live_allow}}):
        out.append(f"live allow list (untouched, informational): {problem}")
    allow, ask = set(live_allow), set(merged["permissions"]["ask"])
    for rule in sorted(allow & ask):
        out.append(f"rule is in both allow and ask (ask wins per the permissions docs, informational): {rule[:80]}")
    for k in ("bypassPermissions",):
        if k in json.dumps(live):
            out.append(f"live settings mention {k} (untouched, informational)")
    return out


def plan(args):
    raw, live = load(args.live)
    _, cand = load(args.candidate)
    baseline = load(args.baseline)[1] if args.baseline else None
    merged, report = merge(live, cand, baseline, args.hook_command, args.fail_closed)
    text = dump(merged)
    findings = compat(live, merged, cand)
    return {"live_sha": sha(raw), "candidate_sha": sha(pathlib.Path(args.candidate).read_bytes()),
            "baseline_sha": sha(pathlib.Path(args.baseline).read_bytes()) if args.baseline else None,
            "result_sha": sha(text), "report": report, "compat": findings,
            "blocking": any(f.startswith("BLOCKING") for f in findings),
            "untouched_keys": sorted(k for k in live if k not in ("autoMode",)), "_text": text}


def atomic_write(path, text):
    path = pathlib.Path(path).resolve()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rollout-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def apply(args):
    p = plan(args)
    if p["live_sha"] != args.expect_live_sha:
        raise SystemExit(f"REFUSED: live file hash {p['live_sha']} != pinned {args.expect_live_sha}; re-run plan")
    if p["result_sha"] != args.expect_result_sha:
        raise SystemExit(f"REFUSED: predicted result {p['result_sha']} != pinned {args.expect_result_sha}; inputs changed, re-run plan")
    if p["blocking"]:
        raise SystemExit("REFUSED: blocking compatibility findings; see plan")
    bdir = pathlib.Path(os.path.expanduser(args.backup_dir))
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"settings.{p['live_sha'][:12]}.json"
    shutil.copyfile(args.live, backup)
    os.chmod(backup, 0o600)
    atomic_write(args.live, p["_text"])
    return {"applied": True, "backup": str(backup), "backup_sha": p["live_sha"], "result_sha": sha(pathlib.Path(args.live).read_bytes())}


def verify(args):
    cur = sha(pathlib.Path(args.live).read_bytes())
    return {"live_sha": cur, "expected": args.expect_result_sha, "verdict": "VERIFIED" if cur == args.expect_result_sha else "MISMATCH"}


def rollback(args):
    data = pathlib.Path(args.backup).read_bytes()
    if sha(data) != args.expect_backup_sha:
        raise SystemExit("REFUSED: backup hash does not match the pinned value")
    atomic_write(args.live, data.decode())
    return {"restored": True, "live_sha": sha(pathlib.Path(args.live).read_bytes())}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--live", required=True)
        sp.add_argument("--candidate", required=True)
        sp.add_argument("--baseline")
        sp.add_argument("--hook-command")
        sp.add_argument("--fail-closed", action="store_true")
    ap_apply = sub.choices["apply"]
    ap_apply.add_argument("--expect-live-sha", required=True)
    ap_apply.add_argument("--expect-result-sha", required=True)
    ap_apply.add_argument("--backup-dir", default="~/.claude/backups")
    v = sub.add_parser("verify")
    v.add_argument("--live", required=True)
    v.add_argument("--expect-result-sha", required=True)
    r = sub.add_parser("rollback")
    r.add_argument("--live", required=True)
    r.add_argument("--backup", required=True)
    r.add_argument("--expect-backup-sha", required=True)
    a = ap.parse_args(argv)
    out = {"plan": plan, "apply": apply, "verify": verify, "rollback": rollback}[a.cmd](a)
    out.pop("_text", None)
    print(json.dumps(out, indent=2))
    return 1 if out.get("blocking") or out.get("verdict") == "MISMATCH" else 0


if __name__ == "__main__":
    sys.exit(main())
