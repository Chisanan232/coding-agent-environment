#!/usr/bin/env python3
"""Install the SPE-119 routine read-only hook (.claude/hooks/routine-readonly-hook.py) into a live Claude Code settings file.

Owner-run, like candidate-rollout.py: `Edit(~/.claude/settings.json)` and `Edit(~/.claude/hooks/**)` are ask rules, so an agent session
does not run `apply`. Modes (nothing is written except by `apply` and `rollback`):
  candidate write a matrix settings file (live permissions + rtk hook + this hook) for permission-host-matrix.py --candidate
  plan      print the hashes and the exact groups that would be added
  apply     back up the settings, install the hook file, add TWO PreToolUse groups (matcher `Bash|Monitor` and matcher `Agent`)
  verify    check the live install: both groups present exactly once, hook file hash
  rollback  remove exactly the two groups and restore or remove the hook file (later unrelated settings edits are kept)

The change is pinned by --expect-live-sha (the settings you reviewed) and --expect-hook-sha (the hook file you reviewed), and the hook
file must be merged to origin/main (--unmerged-ok is for rehearsals on a copy). Nothing else changes: no permission rule is added,
removed or retired, no existing hook is touched. The hook can only allow; every ask and deny rule still wins. A running Claude Code
session reloads settings and hooks from the file watcher (documented), so no restart is required.
"""
import argparse
import json
import os
import sys
import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import pwd

ROOT = Path(__file__).resolve().parent.parent
_spec = spec_from_file_location("hook_rollout", ROOT / "scripts/hook-rollout.py")
hr = module_from_spec(_spec)
_spec.loader.exec_module(hr)
rollout = hr.rollout

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
HOOK_NAME = "routine-readonly-hook.py"
HOOK_SRC = ROOT / ".claude/hooks" / HOOK_NAME
HOOK_DEST = HOME / ".claude/hooks" / HOOK_NAME
LIVE = HOME / ".claude/settings.json"
BACKUPS = HOME / ".claude/backups"
MATCHERS = ("Bash|Monitor", "Agent")  # Bash alone is not enough: a Bash-matched hook's allow has no effect on Monitor (documented, 2.1.314+)
TIMEOUT = 5


def command_for(dest):
    dest = Path(dest)
    try:
        return "python3 ~/%s" % dest.relative_to(HOME)
    except ValueError:
        return "python3 %s" % dest


def groups_for(command):
    return [{"matcher": m, "hooks": [{"type": "command", "command": command, "timeout": TIMEOUT}]} for m in MATCHERS]


def ours(live):
    return [g for g in (live.get("hooks") or {}).get("PreToolUse", []) if any(HOOK_NAME in str(h.get("command", "")) for h in g.get("hooks", []))]


def state(live, command):
    found = ours(live)
    if not found:
        return "absent"
    return "installed" if found == groups_for(command) else "conflict"


def with_groups(live, command):
    new = json.loads(json.dumps(live))
    new.setdefault("hooks", {}).setdefault("PreToolUse", []).extend(groups_for(command))
    return new


def without_groups(live, command):
    new = json.loads(json.dumps(live))
    canon = groups_for(command)
    new["hooks"]["PreToolUse"] = [g for g in new["hooks"]["PreToolUse"] if g not in canon]
    return new


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("plan", "apply", "verify", "rollback", "candidate"))
    ap.add_argument("--live", default=str(LIVE))
    ap.add_argument("--hook-src", default=str(HOOK_SRC))
    ap.add_argument("--hook-dest", default=str(HOOK_DEST))
    ap.add_argument("--backup-dir", default=str(BACKUPS))
    ap.add_argument("--out", help="candidate mode: where to write the matrix settings file")
    ap.add_argument("--expect-live-sha")
    ap.add_argument("--expect-hook-sha")
    ap.add_argument("--unmerged-ok", action="store_true")
    ap.add_argument("--allow-reformat", action="store_true")
    return ap


def plan(args):
    raw_bytes, raw, live = hr.load_settings(args.live)
    command = command_for(args.hook_dest)
    hook_bytes = Path(args.hook_src).read_bytes()
    print(json.dumps({"live_sha": hr.sha_bytes(raw_bytes), "hook_sha": hr.sha_bytes(hook_bytes), "state": state(live, command),
                      "hook_dest": args.hook_dest, "groups_to_add": groups_for(command),
                      "result_sha": hr.sha_bytes(hr.serialise(with_groups(live, command), raw).encode()) if state(live, command) == "absent" else None,
                      "merged_to_origin_main": rollout.baseline_not_merged(Path(args.hook_src)) is None}, indent=2))
    return 0


def candidate(args):
    """A settings file for permission-host-matrix.py --candidate: the live permissions and autoMode, the rtk rewrite hook (so the hook sees
    what a real session sees) and this hook from --hook-src. No other live hook (supervisor, governors) is carried into a fixture run."""
    if not args.out:
        print("REFUSING: candidate mode needs --out")
        return 2
    _, _, live = hr.load_settings(args.live)
    cfg = {k: live[k] for k in ("permissions", "autoMode") if k in live}
    rtk = [g for g in (live.get("hooks") or {}).get("PreToolUse", []) if any(str(h.get("command", "")).startswith("rtk hook") for h in g.get("hooks", []))]
    cfg["hooks"] = {"PreToolUse": rtk + groups_for("python3 %s" % args.hook_src)}
    Path(args.out).write_text(json.dumps(cfg, indent=2))
    print("wrote %s (rtk groups: %d, hook groups: 2)" % (args.out, len(rtk)))
    return 0


def apply(args):
    live_p, src, dest = Path(args.live), Path(args.hook_src), Path(args.hook_dest)
    message = hr.redirected(live_p, dest)
    if message:
        print(message)
        return 4
    raw_bytes, raw, live = hr.load_settings(live_p)
    command = command_for(dest)
    st = state(live, command)
    if st == "conflict":
        print("REFUSING: the settings already hold a different %s entry" % HOOK_NAME)
        return 2
    hook_bytes = src.read_bytes()
    hook_sha, live_sha = hr.sha_bytes(hook_bytes), hr.sha_bytes(raw_bytes)
    dest_sha = rollout.sha(dest) if dest.exists() else None
    if st == "installed" and dest_sha == hook_sha:
        print("already installed: both groups present once and hook file identical; nothing changed")
        return 0
    if not args.unmerged_ok:
        why = rollout.baseline_not_merged(src)
        if why:
            print("REFUSING: %s. Deploy only a merged hook file (or pass --unmerged-ok for a rehearsal on a copy)." % why)
            return 7
    if args.expect_hook_sha != hook_sha or (st == "absent" and args.expect_live_sha != live_sha):
        print("REFUSING: --expect-hook-sha / --expect-live-sha do not match the inputs; re-review required")
        return 3
    new_text = None
    if st == "absent":
        if hr.serialise(live, raw) != raw and not args.allow_reformat:
            print("REFUSING: re-serialising the settings would change unrelated formatting; pass --allow-reformat")
            return 8
        new_text = hr.serialise(with_groups(live, command), raw)
    backups = Path(args.backup_dir)
    backups.mkdir(parents=True, exist_ok=True)
    lock = hr.take_lock(backups)
    if lock is None:
        print("REFUSING: another apply holds the lock in %s" % backups)
        return 11
    try:
        stamp = time.time_ns()
        backup = backups / ("settings.readonly-hook.%d.json" % stamp)
        rollout.write_new(backup, raw_bytes, 0o600)
        hook_backup = None
        if dest.exists():
            hook_backup = backups / ("%s.%d" % (HOOK_NAME, stamp))
            rollout.write_new(hook_backup, dest.read_bytes(), 0o600)
        hr.install_bytes(dest, hook_bytes, 0o755)  # hook first: a settings group must never point at a missing file
        if new_text is not None:
            try:
                hr.replace_if_unchanged(live_p, new_text.encode(), raw_bytes)
            except hr.SettingsChanged:
                print("REFUSING: the live file changed immediately before the replace; hook file installed, settings untouched; re-run plan")
                return 5
        receipt = backups / ("receipt.readonly-hook.%d.json" % stamp)
        rollout.write_new(receipt, json.dumps({"settings_backup": str(backup), "hook_backup": str(hook_backup) if hook_backup else None,
                                               "hook_created": hook_backup is None, "hook_sha": hook_sha, "live_sha_before": live_sha,
                                               "result_sha": hr.sha_bytes(live_p.read_bytes())}, indent=2).encode(), 0o600)
        print("APPLIED readonly-hook; receipt %s; result_sha %s" % (receipt, hr.sha_bytes(live_p.read_bytes())))
        return 0
    finally:
        os.close(lock)


def verify(args):
    raw_bytes, raw, live = hr.load_settings(args.live)
    command = command_for(args.hook_dest)
    dest = Path(args.hook_dest)
    problems = []
    if state(live, command) != "installed":
        problems.append("settings groups: %s" % state(live, command))
    if not dest.exists():
        problems.append("hook file missing")
    elif rollout.sha(dest) != hr.sha_bytes(Path(args.hook_src).read_bytes()):
        problems.append("hook file differs from %s" % args.hook_src)
    print("VERIFIED readonly-hook installed (2 groups, hook hash matches)" if not problems else "PROBLEMS: " + "; ".join(problems))
    return 1 if problems else 0


def rollback(args):
    backups = Path(args.backup_dir)
    receipts = sorted(backups.glob("receipt.readonly-hook.*.json"), key=lambda r: r.stat().st_mtime)
    if not receipts:
        print("REFUSING: no receipt in %s" % backups)
        return 2
    receipt = json.loads(receipts[-1].read_text())
    live_p, dest = Path(args.live), Path(args.hook_dest)
    raw_bytes, raw, live = hr.load_settings(live_p)
    command = command_for(dest)
    if state(live, command) == "conflict":
        print("REFUSING: the settings hold a %s entry that is not exactly what apply wrote; fix it by hand, nothing was changed" % HOOK_NAME)
        return 2
    if state(live, command) == "installed":
        hr.replace_if_unchanged(live_p, hr.serialise(without_groups(live, command), raw).encode(), raw_bytes)
        print("removed the two hook groups; every other setting kept as it is now")
    if receipt.get("hook_backup"):
        hr.install_bytes(dest, Path(receipt["hook_backup"]).read_bytes(), 0o755)
    elif receipt.get("hook_created") and dest.exists() and rollout.sha(dest) == receipt["hook_sha"]:
        dest.unlink()
        print("removed %s (created by apply)" % dest)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {"plan": plan, "candidate": candidate, "apply": apply, "verify": verify, "rollback": rollback}[args.mode](args)
    except hr.SettingsError as exc:
        print("REFUSING: %s" % exc)
        return 10


if __name__ == "__main__":
    sys.exit(main())
