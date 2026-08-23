#!/usr/bin/env python3
"""Session-scoped background-task tracker, fed by hooks.

Writes ~/.claude/bg-state/<session_id>.json, keyed by session_id so
concurrent sessions never collide. Consumed by statusline.py's BG row and by
subagent-statusline.py.

Verified-sources-only design (see AAASM-5702 observability review,
2026-08-15) — `background_tasks`/`session_crons` do not appear anywhere in
the official hooks reference for this Claude Code version, so nothing here
depends on them:

- SubagentStart/SubagentStop are a real, documented add/remove pair — exact.
- PostToolUse for Bash (tool_input.run_in_background == true), Monitor, and
  Workflow is add-only. There is no verified hook that fires when a
  background shell/monitor/workflow actually finishes, so these entries are
  never claimed as exact live counts — the reader (statusline.py) renders
  them with a trailing "?" and garbage-collects by TTL, never by a
  completion signal that doesn't exist.

Cron: intentionally untracked. CronList is a model-only tool this external
process can't call, and no CronCreated/session_crons hook exists.
"""
import json
import os
import sys
import tempfile
import time

STATE_DIR = os.path.expanduser("~/.claude/bg-state")
TTL_SECONDS = 60 * 60  # add-only entries older than this are dropped on read


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"subagents": {}, "shells": {}, "monitors": {}, "workflows": {}}


def save_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def gc(data):
    now = time.time()
    for bucket in ("shells", "monitors", "workflows"):
        data[bucket] = {
            k: v
            for k, v in data.get(bucket, {}).items()
            if now - v.get("t", 0) < TTL_SECONDS
        }
    for bucket in ("subagents", "shells", "monitors", "workflows"):
        data.setdefault(bucket, {})
    return data


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return

    sid = d.get("session_id")
    if not sid:
        return
    path = os.path.join(STATE_DIR, f"{sid}.json")

    event = d.get("hook_event_name")
    data = gc(load(path))

    if event == "SubagentStart":
        aid = d.get("agent_id")
        if aid:
            data["subagents"][aid] = {"type": d.get("agent_type", ""), "t": time.time()}
    elif event == "SubagentStop":
        data["subagents"].pop(d.get("agent_id"), None)
    elif event == "PostToolUse":
        tool = d.get("tool_name")
        ti = d.get("tool_input") or {}
        tuid = d.get("tool_use_id")
        if tuid:
            if tool == "Bash" and ti.get("run_in_background"):
                data["shells"][tuid] = {"t": time.time(), "desc": ti.get("description", "")}
            elif tool == "Monitor":
                data["monitors"][tuid] = {"t": time.time(), "desc": ti.get("description", "")}
            elif tool == "Workflow":
                data["workflows"][tuid] = {"t": time.time(), "desc": ""}

    save_atomic(path, data)


if __name__ == "__main__":
    main()
