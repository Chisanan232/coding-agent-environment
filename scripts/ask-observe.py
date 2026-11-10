#!/usr/bin/env python3
"""Observe host permission prompts and their outcome (SPE-170). Observation only: this hook can never allow, deny or ask.

Wired as a Claude Code hook for `PermissionRequest`, `PostToolUse`, `PostToolUseFailure` and `Notification` (matcher `permission_prompt`), it appends one
sanitized line per event to `$ASK_OBSERVE_DIR/events.jsonl` (default `~/.cache/coding-agent-environment/asks/observe`, mode 0700/0600) and prints nothing,
always exiting 0. A `PermissionRequest` hook that prints no decision leaves the host prompt exactly as it was, so this is the proof that a prompt was raised
without being a second approver.

`report` joins the events by session, tool and a hash of the tool input:

| Observed | Meaning |
|---|---|
| `PROMPTED`, nothing later | A prompt was raised and no result has been seen: still pending, denied or expired. The documented events do not say which |
| `PROMPTED` then `PostToolUse` | The same call ran while or after a prompt was open. That is consistent with an approval, but a denied prompt followed by the same call under an allow rule looks identical, so it is never reported as consent |
| `PROMPTED` then `PostToolUseFailure` | The same call failed after a prompt was open |
| `PostToolUse` with no earlier `PROMPTED` | The call ran without a prompt (an allow rule, a mode, or no ask rule matched) |

Nothing here is evidence of consent by itself: a record says what the host did, never what the owner meant. No command arguments, file paths or URLs are stored: only the
program name (for a command), the tool name and a hash of the input.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone

PROMPT_EVENTS = {"PermissionRequest"}
RESULT_EVENTS = {"PostToolUse", "PostToolUseFailure"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe_dir():
    path = os.environ.get("ASK_OBSERVE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "coding-agent-environment", "asks", "observe")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


PROGRAMS = {"git", "gh", "rtk", "cargo", "python3", "python", "bash", "sh", "zsh", "npm", "pnpm", "node", "uv", "make", "docker", "kubectl",
            "gcloud", "terraform", "curl", "rm", "mv", "cp", "ls", "cat", "echo", "claude", "timeout", "env", "sudo", "ssh", "scp", "rsync"}
MAX_BYTES = 1_000_000


def _key_secret(directory):
    """Per-install random key so an input hash cannot be brute-forced from the file alone (the key sits beside the log, so this
    only protects a reader of the log file by itself). Created by linking a complete temp file into place: a concurrent hook
    can never read a half-written key."""
    path = os.path.join(directory, ".hmac-key")
    for _ in range(2):
        try:
            with open(path, "rb") as fh:
                key = fh.read()
            if len(key) == 32:
                return key
        except OSError:
            pass
        tmp = os.path.join(directory, f".hmac-key.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(32))
        try:
            os.link(tmp, path)  # fails if another hook won the race; then the loop re-reads theirs
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    with open(path, "rb") as fh:
        return fh.read()


def input_key(tool_input, directory=None):
    blob = json.dumps(tool_input, sort_keys=True, default=str).encode()
    key = _key_secret(directory or observe_dir())
    return hmac.new(key, blob, hashlib.sha256).hexdigest()[:16]


def preview(tool_input):
    """Only an allowlisted program name for a command, never its arguments, and nothing else from the command line."""
    if isinstance(tool_input, dict) and tool_input.get("command"):
        words = str(tool_input["command"]).split()
        name = os.path.basename(words[0]) if words else ""
        return name if name in PROGRAMS else "other"
    return ""


def sanitize(payload, at):
    event = str(payload.get("hook_event_name", ""))[:40]
    tool_input = payload.get("tool_input")
    return {"at": at, "event": event, "session": str(payload.get("session_id", ""))[:80], "tool": str(payload.get("tool_name", ""))[:120],
            "key": input_key(tool_input) if tool_input is not None else None, "preview": preview(tool_input) if tool_input is not None else "",
            "tool_use_id": str(payload.get("tool_use_id") or "")[:80] or None,  # PermissionRequest carries none; never invented
            "notification_type": str(payload.get("notification_type") or "")[:40] or None}


def append(directory, record):
    path = os.path.join(directory, "events.jsonl")
    try:
        if os.path.getsize(path) > MAX_BYTES:  # keep one previous generation; the report only needs recent prompts
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(os.path.join(directory, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read(directory):
    try:
        with open(os.path.join(directory, "events.jsonl")) as fh:
            rows = []
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # a torn or foreign line is skipped, never fatal
            return rows
    except OSError:
        return []


def report(rows, at):
    """Pure: one entry per prompt (and per un-prompted result) in time order."""
    out, open_prompts = [], {}
    for row in rows:
        k = (row.get("session"), row.get("tool"), row.get("key"))
        if row["event"] in PROMPT_EVENTS:
            entry = {"prompted_at": row["at"], "session": row["session"], "tool": row["tool"], "key": row["key"], "preview": row["preview"],
                     "outcome": "PROMPTED_NO_RESULT", "result_at": None}
            open_prompts.setdefault(k, []).append(entry)
            out.append(entry)
        elif row["event"] in RESULT_EVENTS:
            pending = open_prompts.get(k) or []
            waiting = next((e for e in pending if e["result_at"] is None), None)
            if waiting is not None:
                waiting["result_at"] = row["at"]
                waiting["outcome"] = "RAN_AFTER_OPEN_PROMPT" if row["event"] == "PostToolUse" else "FAILED_AFTER_OPEN_PROMPT"
            else:
                out.append({"prompted_at": None, "session": row["session"], "tool": row["tool"], "key": row["key"], "preview": row["preview"],
                            "outcome": "RAN_WITHOUT_PROMPT", "result_at": row["at"]})
    for e in out:
        if e["outcome"] == "PROMPTED_NO_RESULT":
            e["waiting_s"] = int((datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(e["prompted_at"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds())
    return out


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("hook", help="record one hook event from stdin (default)")
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    r.add_argument("--only", choices=("PROMPTED_NO_RESULT", "RAN_AFTER_OPEN_PROMPT", "FAILED_AFTER_OPEN_PROMPT", "RAN_WITHOUT_PROMPT"))
    args = ap.parse_args(argv)
    if args.cmd == "report":
        entries = report(read(observe_dir()), now())
        if args.only:
            entries = [e for e in entries if e["outcome"] == args.only]
        if args.json:
            print(json.dumps(entries, sort_keys=True))
        else:
            for e in entries:
                print(f"{e['prompted_at'] or '-'} {e['outcome']} {e['tool']} {e['preview'][:80]}" + (f" waiting={e['waiting_s']}s" if "waiting_s" in e else ""))
        return 0
    try:  # the hook path must never raise, print a decision, or exit non-zero
        payload = json.load(stdin or sys.stdin)
        if isinstance(payload, dict) and (payload.get("hook_event_name") in PROMPT_EVENTS | RESULT_EVENTS
                                          or (payload.get("hook_event_name") == "Notification" and payload.get("notification_type") == "permission_prompt")):
            append(observe_dir(), sanitize(payload, now()))
    except Exception:  # noqa: BLE001 - observation must not influence the host in any way
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
