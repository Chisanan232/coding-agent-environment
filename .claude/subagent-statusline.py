#!/usr/bin/env python3
"""Custom subagentStatusLine rows.

    🤖 AAASM-5713 benchmark | RUNNING | 51m | 38k tok | compatibility decision

Reads the official `tasks` array on stdin (one row per visible subagent) and
writes one `{"id", "content"}` JSON line per row to override. No separate
registry — this is exactly the data Claude Code already tracks.
"""
import json
import sys
import time


def fmt_elapsed(start_ms):
    if not start_ms:
        return "?"
    secs = max(0, int(time.time() - start_ms / 1000))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m"
    return f"{m}m" if m else f"{s}s"


def fmt_tokens(tok):
    if not isinstance(tok, (int, float)) or tok <= 0:
        return ""
    return f"{tok / 1000:.0f}k tok" if tok >= 1000 else f"{int(tok)} tok"


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return

    for t in d.get("tasks", []):
        tid = t.get("id")
        if not tid:
            continue
        name = t.get("label") or t.get("name") or "agent"
        status = (t.get("status") or "").upper()
        elapsed = fmt_elapsed(t.get("startTime"))
        tok_s = fmt_tokens(t.get("tokenCount"))
        desc = (t.get("description") or "")[:40]

        parts = [f"🤖 {name}"]
        if status:
            parts.append(status)
        parts.append(elapsed)
        if tok_s:
            parts.append(tok_s)
        if desc:
            parts.append(desc)

        print(json.dumps({"id": tid, "content": " | ".join(parts)}))


if __name__ == "__main__":
    main()
