#!/usr/bin/env python3
"""PreToolUse adapter for scripts/workstation-policy.py (SPE-116). NOT deployed by this repository.

Reads one PreToolUse JSON event on stdin and prints either nothing (no opinion; the normal permission flow
applies) or a `permissionDecision` of `ask` or `deny`. It never prints `allow`: per the hooks reference an allow
cannot override permission asks or denies, and this hook must not be able to widen anything.

Covers: Edit/Write/MultiEdit/NotebookEdit on security-surface paths, and Bash commands that upload data with curl,
wget, scp, rsync, sftp, nc or ncat. It does NOT see: git/gh/package-manager traffic, interpreter-made requests
(python -c, node -e), MCP tools, WebFetch, redirects, or what a file actually contains. A crashed or timed-out hook
fails OPEN unless the hook entry sets `onFailure: "block"`; internal errors on file edits here escalate to `ask`.
Profile: $WORKSTATION_POLICY_PROFILE, default ~/.config/coding-agent-environment/workstation-profile.json (private).
"""
import importlib.util
import json
import os
import pathlib
import re
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("wsp", HERE / "workstation-policy.py")
wsp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsp)

FILE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
NET_TOOLS = {"curl", "wget", "scp", "rsync", "sftp", "nc", "ncat"}
CURL_PAYLOAD = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F", "--form", "-T", "--upload-file"}
SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}
URL_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^/@\s]*@)?([^/:\s?#]+)", re.I)
SCP_HOST = re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9][A-Za-z0-9.-]*):(?!//)")


def decision(kind, reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind, "permissionDecisionReason": reason[:900]}}


def default_profile_path():
    return os.environ.get("WORKSTATION_POLICY_PROFILE") or os.path.expanduser("~/.config/coding-agent-environment/workstation-profile.json")


def load_profile(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def simple_commands(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    cur, out = [], []
    for tok in lexer:
        if tok in SEPARATORS or set(tok) <= set("&|;"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def strip_wrappers(argv):
    i = 0
    while i < len(argv) and (argv[i] in ("rtk", "command", "nohup", "time", "env", "sudo", "timeout", "nice") or "=" in argv[i].split("/")[0]):
        i += 1
        if argv[i - 1] == "timeout" and i < len(argv):
            i += 1
    return argv[i:]


def uploads(argv):
    """Return (host, source_hint) when this simple command sends data to a remote host, else None."""
    argv = strip_wrappers(argv)
    if not argv:
        return None
    tool = os.path.basename(argv[0])
    args = argv[1:]
    if tool == "curl":
        payload = any(a in CURL_PAYLOAD or any(a.startswith(p + "=") for p in CURL_PAYLOAD if p.startswith("--")) for a in args) \
            or any(a.startswith("-d") and len(a) > 2 for a in args)
        method = next((args[i + 1].upper() for i, a in enumerate(args[:-1]) if a in ("-X", "--request")), "")
        if not (payload or method in ("POST", "PUT", "PATCH")):
            return None
    elif tool == "wget":
        if not any(a.startswith(("--post-data", "--post-file", "--body-data", "--body-file")) or a in ("--method=POST", "--method=PUT") for a in args):
            return None
    elif tool in ("scp", "rsync", "sftp", "nc", "ncat"):
        pass
    else:
        return None
    host = None
    for a in args:
        m = URL_HOST.match(a)
        if m:
            host = m.group(1)
            break
    if host is None and tool in ("scp", "rsync", "sftp"):
        positional = [x for x in args if not x.startswith("-")]
        dest = positional[-1] if positional else ""
        remote = [x for x in positional if SCP_HOST.match(x)]
        if tool == "sftp":
            host = SCP_HOST.match(remote[-1]).group(1) if remote else (positional[-1] if positional else None)
        elif SCP_HOST.match(dest):
            host = SCP_HOST.match(dest).group(1)  # data goes to a remote destination
        elif remote:
            return None  # remote is only a source: a download
    if host is None and tool in ("nc", "ncat"):
        pos = [a for a in args if not a.startswith("-")]
        host = pos[0] if pos else None
    if host is None:
        return ("unknown-host", " ".join(argv)[:120])
    return (host.lower(), " ".join(argv)[:120])


def judge_bash(event, profile, fp_fn):
    command = (event.get("tool_input") or {}).get("command") or ""
    if not any(t in command for t in NET_TOOLS):
        return None
    purpose = (event.get("tool_input") or {}).get("description") or None
    for argv in simple_commands(command):
        hit = uploads(argv)
        if not hit:
            continue
        host, hint = hit
        action = {"kind": "outbound", "destination": host, "data_classes": ["unclassified"], "data_source": f"command arguments: {hint}",
                  "purpose": purpose, "visibility": "unknown"}
        res = wsp.decide(profile, action, fp_fn())
        if res["verdict"] == "ALLOW":
            continue
        pk = res.get("ask_packet") or {}
        detail = "; ".join(res["reasons"])
        if res["verdict"] == "ASK" and pk:
            detail += f" | data={pk['data']} source={pk['source']} destination={pk['destination']} purpose={pk['purpose']} alternatives={pk['safer_alternatives']} risks={pk['risks']}"
        return decision("ask" if res["verdict"] == "ASK" else "deny", f"workstation policy ({res['profile_state']}): {detail}")
    return None


def judge_file(event):
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    content = "\n".join(str(v) for k, v in ti.items() if isinstance(v, str) and k not in ("file_path", "notebook_path"))
    res = wsp.decide({}, {"kind": "modify_policy", "path": path, "content": content}, None)
    if res["verdict"] == "ASK":
        return decision("ask", f"workstation policy: {'; '.join(res['reasons'])} ({path})")
    return None


def evaluate_event(event, profile, fp_fn):
    tool = str(event.get("tool_name", "")).lower()
    if tool in FILE_TOOLS:
        try:
            return judge_file(event)
        except Exception as ex:
            return decision("ask", f"workstation policy hook error on a file edit, escalating: {type(ex).__name__}")
    if tool in ("bash", "powershell"):
        try:
            return judge_bash(event, profile, fp_fn)
        except Exception as ex:
            print(f"workstation policy hook: could not judge command ({type(ex).__name__}); no opinion", file=sys.stderr)
            return None
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    out = evaluate_event(event, load_profile(default_profile_path()), wsp.fingerprint)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
