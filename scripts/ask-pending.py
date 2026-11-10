#!/usr/bin/env python3
"""Pending-ASK ledger (SPE-170): record an action the host will ASK about, keep working, resume it safely later.

This tool never authorizes anything. The only authorization is the host's own permission decision on the exact
command, which is issued again at resume time. The ledger records the request, orders the follow-up and makes the
resume safe: exact head, independent review, required checks and the merge gate are revalidated before the action is
handed back, the hand-back happens once, and every other transition is explicit.

A *pending item* is a record. A *live prompt* is a host prompt that is open right now (`prompt: live`). They are
different facts: an item can be pending with no prompt open (the session ended, the prompt expired, nothing issued it
yet). Elapsed time only decides when to move on to other work (`past_grace`); it never approves, expires-to-allow or
resumes anything.

Stdlib only. State: one JSON file per item plus an append-only receipts log under `$ASK_PENDING_DIR`
(default `~/.cache/coding-agent-environment/asks`), mode 0700/0600, one flock around every read-modify-write.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone

GRACE_MAX_S = 900  # 15 minutes: the longest a runnable task waits on a pending ASK before the agent moves on
MAX_PENDING = 20
LEASE_S = 1800  # a RESUMING item with no outcome for this long returns to PENDING (restart/crash), never to approved

PENDING, RESUMING, CONSUMED, DENIED, STALE, EXPIRED, CANCELLED = (
    "PENDING", "RESUMING", "CONSUMED", "DENIED", "STALE_HEAD", "EXPIRED", "CANCELLED")
TERMINAL = {CONSUMED, DENIED, STALE, EXPIRED, CANCELLED}
OUTCOMES = {"executed": CONSUMED, "denied": DENIED, "expired": EXPIRED, "failed": PENDING}
HEAD_RE = re.compile(r"[0-9a-f]{40}")
TARGET_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+")
ID_RE = re.compile(r"[0-9a-f]{16}")
PROMPTS = {"live", "none", "unknown"}
TRIGGERS = {"owner_approval", "owner_present", "dependency_merged", "ci_green", "review_complete", "manual_restart"}


class AskError(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def state_dir():
    path = os.environ.get("ASK_PENDING_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "coding-agent-environment", "asks")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextmanager
def locked(directory):
    with open(os.path.join(directory, ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def item_key(action, target, head):
    return hashlib.sha256("\0".join((action, target, head)).encode()).hexdigest()[:16]


def _path(directory, item_id):
    if not ID_RE.fullmatch(item_id):
        raise AskError("bad id")
    return os.path.join(directory, item_id + ".json")


def load(directory, item_id):
    try:
        with open(_path(directory, item_id)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise AskError(f"no such item {item_id}")


def save(directory, item):
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        json.dump(item, fh, sort_keys=True, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(directory, item["id"]))


def receipt(directory, event, item, **detail):
    line = {"at": iso(now_utc()), "event": event, "id": item["id"], "state": item["state"], **detail}
    path = os.path.join(directory, "receipts.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def all_items(directory, quiet=False):
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json") and not name.startswith("."):
            try:
                with open(os.path.join(directory, name)) as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                # One unreadable file must not take the whole ledger down; it is left in place for the owner to inspect.
                if not quiet:  # the read path (list) writes nothing
                    receipt(directory, "unreadable_item", {"id": name[:-5], "state": "UNREADABLE"})
    return out


def view(item, at):
    """The item plus derived, read-only facts. Nothing here changes state."""
    out = dict(item)
    age = (at - parse(item["created"])).total_seconds()
    out["age_s"] = int(age)
    out["past_grace"] = item["state"] == PENDING and age >= item["grace_s"]
    out["move_on"] = item["state"] == PENDING and age >= item["grace_s"]
    out["live_prompt"] = item["prompt"] == "live"
    return out


# --- transitions -------------------------------------------------------------------------------------------------------

def record(directory, action, target, head, deps, trigger, session_id, request_id, grace_s, prompt, at, owners="", gate_args=(), settings=()):
    if not action.strip() or not TARGET_RE.fullmatch(target) or not HEAD_RE.fullmatch(head):
        raise AskError("action, a target of the form owner/repo#N and a full 40-hex head sha are required")
    if target.split("#")[0] not in action or head not in action:
        raise AskError("the action must name the target repo and the exact head it is pinned to")
    if trigger not in TRIGGERS or prompt not in PROMPTS:
        raise AskError("unknown trigger or prompt value")
    grace_s = max(0, min(int(grace_s), GRACE_MAX_S))
    key = item_key(action, target, head)
    with locked(directory):
        try:
            existing = load(directory, key)
        except AskError:
            existing = None
        if existing is not None and existing["state"] == STALE:
            # The head moved away and came back (or the head was mistyped): the same pinned action is requested again.
            # Gate options and layers are part of what was reviewed; a reopen with different ones is a new decision, not a reopen.
            if (existing.get("owners"), existing.get("gate_args"), existing.get("settings")) != (owners, list(gate_args), list(settings)):
                raise AskError("the stale item was recorded with different gate options or settings layers; cancel it and record a new request")
            existing.update(state=PENDING, created=iso(at), prompt=prompt, session_id=session_id)  # resumes stays: a hand-back did happen
            existing.pop("closed", None), existing.pop("reason", None)
            save(directory, existing)
            receipt(directory, "reopened_after_stale", existing)
            return existing, True
        if existing is not None:
            # Same action, target and head is the same request: a duplicate is a no-op, a consumed one stays consumed.
            receipt(directory, "duplicate_record", existing)
            return existing, False
        live_open = [i for i in all_items(directory) if i["state"] in (PENDING, RESUMING)]
        if len(live_open) >= MAX_PENDING:
            raise AskError(f"{MAX_PENDING} items already pending")
        # An older pending item for the same target at a different head can no longer be the action asked about.
        for old in live_open:
            if old["target"] == target and old["head"] != head and old["state"] == PENDING:
                old["state"], old["closed"], old["reason"] = STALE, iso(at), f"superseded by head {head}"
                save(directory, old)
                receipt(directory, "superseded", old, new_head=head)
        item = {"id": key, "action": action, "target": target, "head": head, "dependencies": sorted(deps), "resume_trigger": trigger,
                "session_id": session_id, "request_id": request_id or None, "prompt": prompt, "grace_s": grace_s,
                "created": iso(at), "state": PENDING, "resumes": 0, "owners": owners, "gate_args": list(gate_args), "settings": list(settings)}
        save(directory, item)
        receipt(directory, "recorded", item, prompt=prompt, request_id=request_id or None)
        return item, True


def _lease_expired(item, at):
    return item["state"] == RESUMING and (at - parse(item["lease_at"])).total_seconds() >= LEASE_S


def recovered_view(item, at):
    """Read-only: what the item will look like once its lapsed lease is recovered. `list` uses this and writes nothing."""
    if not _lease_expired(item, at):
        return item
    out = {k: v for k, v in item.items() if k != "lease_at"}
    out["state"], out["prompt"] = PENDING, "unknown"
    return out


def _recover(directory, item, at):
    """A RESUMING item whose lease ran out (the session died mid-resume) goes back to PENDING. Never to done. Call under the lock."""
    if _lease_expired(item, at):
        item["state"], item["prompt"] = PENDING, "unknown"
        item.pop("lease_at", None)
        save(directory, item)
        # The earlier hand-back's host prompt may still be open and answerable; it is not cancelled by this, and the next
        # resume checks the merged state first so a late answer to it cannot be followed by a second execution.
        receipt(directory, "lease_expired", item, earlier_handback_may_still_be_open=True)
    return item


def restart(directory, session_id, at):
    """After a restart no earlier prompt can be assumed open: every live flag becomes unknown. State is untouched otherwise."""
    changed = []
    with locked(directory):
        for item in all_items(directory):
            if item["state"] in (PENDING, RESUMING) and item["prompt"] == "live" and item.get("session_id") != session_id:
                item["prompt"] = "unknown"
                save(directory, item)
                receipt(directory, "prompt_unknown_after_restart", item)
                changed.append(item["id"])
    return changed


def begin_resume(directory, item_id, current_head, validate, at, verify=None):
    """PENDING -> RESUMING exactly once, after revalidation. Returns the exact recorded action to run (the host then asks again)."""
    if not HEAD_RE.fullmatch(current_head):
        raise AskError("--current-head must be the full 40-hex head read from GitHub; nothing was changed")
    with locked(directory):
        item = _recover(directory, load(directory, item_id), at)
        if item["state"] in TERMINAL:
            receipt(directory, "refused_terminal", item)
            raise AskError(f"item is {item['state']}: an approval is consumed once and a closed item is never resumed")
        if item["state"] == RESUMING:
            receipt(directory, "refused_duplicate_resume", item)
            raise AskError("a resume is already in flight for this item")
        if current_head != item["head"]:
            item["state"], item["closed"], item["reason"] = STALE, iso(at), f"head moved to {current_head}"
            save(directory, item)
            receipt(directory, "stale_head", item, current_head=current_head)
            raise AskError(f"head changed ({item['head'][:12]} -> {current_head[:12]}): record a new request after review of the new head")
        if verify is not None and not verify(item):
            # An earlier hand-back may have been answered after its lease lapsed: if the merge is already verified, close it instead.
            item["state"], item["closed"], item["outcome_ref"] = CONSUMED, iso(at), "merge verified at re-resume"
            save(directory, item)
            receipt(directory, "consumed_by_verification", item)
            raise AskError("the merge is already verified: item closed as CONSUMED, nothing to run")
        problems = validate(item)
        if problems:
            receipt(directory, "revalidation_failed", item, problems=problems)
            raise AskError("revalidation failed: " + "; ".join(problems))
        item["state"], item["lease_at"], item["resumes"] = RESUMING, iso(at), item["resumes"] + 1
        item["prompt"] = "none"
        save(directory, item)
        receipt(directory, "resuming", item)
        return item


def finish(directory, item_id, outcome, ref, at, verify=None):
    if outcome not in OUTCOMES:
        raise AskError("outcome must be one of " + ", ".join(sorted(OUTCOMES)))
    with locked(directory):
        item = load(directory, item_id)
        if item["state"] in TERMINAL:
            receipt(directory, "duplicate_finish", item, outcome=outcome)
            raise AskError(f"item already {item['state']}: duplicate event ignored")
        if item["state"] != RESUMING:
            raise AskError("only a RESUMING item can finish; run resume first")
        if outcome == "executed":
            if not ref:
                raise AskError("executed needs --ref: a durable reference for the evidence")
            problems = (verify or (lambda _item: ["no verifier"]))(item)
            if problems:
                receipt(directory, "executed_unverified", item, problems=problems)
                raise AskError("executed not accepted: " + "; ".join(problems))
        item["state"] = OUTCOMES[outcome]
        item["closed"], item["outcome_ref"] = iso(at), ref or None
        if outcome == "failed":
            item.pop("lease_at", None)
            item.pop("closed")
            item["prompt"] = "unknown"
        save(directory, item)
        receipt(directory, "finished", item, outcome=outcome, ref=ref or None)
        return item


def cancel(directory, item_id, reason, at):
    with locked(directory):
        item = load(directory, item_id)
        if item["state"] in TERMINAL:
            raise AskError(f"item already {item['state']}")
        item["state"], item["closed"], item["reason"] = CANCELLED, iso(at), reason
        save(directory, item)
        receipt(directory, "cancelled", item, reason=reason)
        return item


# --- default revalidation: reuse the gate, do not reimplement it -----------------------------------------------------------

def _run_gate(argv):
    """Run the gate; any launch problem is a refusal, never a traceback and never a pass."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        out = json.loads(proc.stdout)
        if not isinstance(out, dict):
            raise ValueError("gate output is not an object")
        return proc.returncode, out
    except (OSError, ValueError, subprocess.TimeoutExpired) as err:
        return None, {"error": type(err).__name__}


def _gate_argv(gate_cmd):
    return list(gate_cmd or [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner-merge-gate.py")])


def _settings_flags(settings):
    paths = [settings] if isinstance(settings, str) else list(settings)
    return [flag for path in paths for flag in ("--settings", path)]


def gate_validator(settings, gate_cmd=None):
    """Revalidation for a merge item: the owner-merge gate must PASS for the same repo/PR/head and print the same merge action.

    The gate itself checks the exact head, the independent review evidence for that head, required checks, owner class and
    host permission over every settings layer passed. Any non-zero exit, a missing or different head, or a different printed
    command refuses the resume. The item's recorded gate options (CI exception, version policy, local-first config) are replayed.
    """
    def validate(item):
        repo, _, pr = item["target"].partition("#")
        argv = _gate_argv(gate_cmd) + ["check", "--repo", repo, "--pr", pr, "--owners", item.get("owners") or repo.split("/")[0]]
        argv += _settings_flags(settings) + list(item.get("gate_args") or [])
        rc, out = _run_gate(argv)
        if rc is None:
            return [f"gate could not run ({out['error']})"]
        problems = []
        if rc != 0 or out.get("verdict") != "PASS":
            problems.append(f"gate verdict {out.get('verdict')!r} (exit {rc})")
        if out.get("head") != item["head"]:
            problems.append("gate head differs from the recorded head")
        if out.get("merge_command") != item["action"]:
            problems.append("gate merge_command differs from the recorded action")
        return problems
    return validate


def merge_verifier(gate_cmd=None):
    """Proof for `executed`: the gate's own verify must report VERIFIED for this repo, PR and head."""
    def verify(item):
        repo, _, pr = item["target"].partition("#")
        rc, out = _run_gate(_gate_argv(gate_cmd) + ["verify", "--repo", repo, "--pr", pr, "--head", item["head"]])
        if rc is None:
            return [f"verify could not run ({out['error']})"]
        return [] if rc == 0 and out.get("verdict") == "VERIFIED" else [f"verify verdict {out.get('verdict')!r}"]
    return verify


def default_settings():
    """Every settings layer that exists, so the gate's host-permission view matches what the host will enforce."""
    layers = [os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
              os.path.join(os.getcwd(), ".claude", "settings.json"), os.path.join(os.getcwd(), ".claude", "settings.local.json")]
    return [p for p in layers if os.path.isfile(p)] or layers[:1]


# --- CLI -----------------------------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a pending ASK")
    r.add_argument("--action", required=True, help="the exact command the host will ask about")
    r.add_argument("--target", required=True, help="e.g. owner/repo#N")
    r.add_argument("--head", required=True)
    r.add_argument("--dep", action="append", default=[])
    r.add_argument("--trigger", default="owner_approval", choices=sorted(TRIGGERS))
    r.add_argument("--session-id", default="")
    r.add_argument("--request-id", default="", help="host-supplied identifier only (e.g. tool_use_id); never invented")
    r.add_argument("--prompt", default="none", choices=sorted(PROMPTS))
    r.add_argument("--owners", default="", help="comma-separated owner logins passed to the gate (default: the repo owner)")
    r.add_argument("--gate-ci-unavailable", action="store_true", help="replay `--ci-unavailable` when revalidating")
    r.add_argument("--gate-ci-exception", action="store_true", help="replay `--ci-exception` when revalidating")
    r.add_argument("--gate-version-policy", default="")
    r.add_argument("--gate-local-first-config", default="")
    r.add_argument("--grace-s", type=int, default=GRACE_MAX_S)
    l = sub.add_parser("list")
    l.add_argument("--json", action="store_true")
    rs = sub.add_parser("restart", help="mark live prompts unknown after a session restart")
    rs.add_argument("--session-id", required=True)
    b = sub.add_parser("resume", help="revalidate and hand back the exact action once")
    b.add_argument("--id", required=True)
    b.add_argument("--current-head", required=True, help="the head you just read from GitHub")
    b.add_argument("--settings", action="append", default=[], help="must equal the layers recorded with the item (user, plus project and local in the recording cwd, when present)")
    f = sub.add_parser("finish", help="close a RESUMING item with an observed outcome")
    f.add_argument("--id", required=True)
    f.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    f.add_argument("--ref", default="")
    c = sub.add_parser("cancel")
    c.add_argument("--id", required=True)
    c.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    directory, at = state_dir(), now_utc()
    try:
        if args.cmd == "record":
            gate_args = (["--ci-unavailable"] * args.gate_ci_unavailable + ["--ci-exception"] * args.gate_ci_exception
                         + (["--version-policy", args.gate_version_policy] if args.gate_version_policy else [])
                         + (["--local-first-config", args.gate_local_first_config] if args.gate_local_first_config else []))
            item, created = record(directory, args.action, args.target, args.head, args.dep, args.trigger, args.session_id,
                                   args.request_id, args.grace_s, args.prompt, at, args.owners, gate_args, default_settings())
            print(json.dumps({"created": created, **view(item, at)}, sort_keys=True))
        elif args.cmd == "list":
            items = [view(recovered_view(i, at), at) for i in all_items(directory, quiet=True)]
            open_items = [i for i in items if i["state"] in (PENDING, RESUMING)]
            if args.json:
                print(json.dumps({"pending": open_items, "closed": [i for i in items if i["state"] in TERMINAL]}, sort_keys=True))
            else:
                for i in open_items:
                    print(f"{i['id']} {i['state']} prompt={i['prompt']} age={i['age_s']}s move_on={i['move_on']} {i['target']}@{i['head'][:12]} {i['action']}")
        elif args.cmd == "restart":
            print(json.dumps({"prompt_unknown": restart(directory, args.session_id, at)}))
        elif args.cmd == "resume":
            item = load(directory, args.id)
            layers = item.get("settings") or default_settings()
            if args.settings and sorted(args.settings) != sorted(layers):
                raise AskError("--settings differs from the layers recorded with the item; record a new request instead")
            item = begin_resume(directory, args.id, args.current_head, gate_validator(layers), at, merge_verifier())
            print(json.dumps({"run_exactly": item["action"], "id": item["id"], "note": "the host will ask again; its answer is the only approval"}))
        elif args.cmd == "finish":
            print(json.dumps(view(finish(directory, args.id, args.outcome, args.ref, at, merge_verifier()), at), sort_keys=True))
        elif args.cmd == "cancel":
            print(json.dumps(view(cancel(directory, args.id, args.reason, at), at), sort_keys=True))
    except AskError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
