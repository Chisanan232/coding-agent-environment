#!/usr/bin/env python3
"""PreToolUse adapter for scripts/workstation-policy.py (SPE-116). NOT deployed by this repository.

Reads one PreToolUse JSON event on stdin and prints either nothing (no opinion; the normal permission flow
applies) or a `permissionDecision` of `ask` or `deny`. It never prints `allow`: per the hooks reference an allow
cannot override permission asks or denies, and this hook must not be able to widen anything.

Covers: Edit/Write/MultiEdit/NotebookEdit on security-surface paths, and Bash commands that upload data with curl,
wget, scp, rsync, sftp, nc or ncat, including inside $(), backticks, sh -c and common wrappers; a network tool named inside
xargs/eval/base64/pipe-to-shell is judged as an unknown destination. It does NOT see: git/gh/package-manager traffic,
interpreter-made requests (python -c, node -e), MCP tools, WebFetch, redirects, or what a file actually contains. Shell
parsing is best-effort and never complete. A crashed or timed-out hook
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
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("wsp", HERE / "workstation-policy.py")
wsp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsp)

FILE_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
NET_TOOLS = {"curl", "wget", "scp", "rsync", "sftp", "nc", "ncat"}
NET_WORD = re.compile(r"\b(curl|wget|scp|rsync|sftp|nc|ncat)\b")
OPAQUE = re.compile(r"\b(xargs|eval|base64|exec)\b|-exec\b|\|\s*(sh|bash|zsh)\b")
WRAPPERS = {"rtk", "command", "nohup", "time", "env", "sudo", "timeout", "nice", "stdbuf", "ionice", "caffeinate", "doas", "exec",
            "builtin", "if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SUBSTITUTION = re.compile(r"\$\(|<\(|>\(| ; ")
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
SCP_HOST = re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9][A-Za-z0-9.-]*):(?!//)")
HOSTISH = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$|^\d{1,3}(?:\.\d{1,3}){3}$|^localhost$|^\[[0-9A-Fa-f:]+\]$")
CURL_LONG_PAYLOAD = ("--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json", "--form", "--form-string",
                     "--upload-file", "--config")
CURL_VALUE_OPTS = {"-H", "-X", "-o", "-u", "-A", "-e", "-b", "-c", "-m", "-w", "-E", "-z", "-r", "-C", "-D", "-d", "-F", "-T", "-K", "-x",
                   "--header", "--request", "--output", "--user", "--user-agent", "--referer", "--cookie", "--cookie-jar", "--max-time",
                   "--write-out", "--cert", "--time-cond", "--range", "--continue-at", "--dump-header", "--proxy", "--connect-timeout",
                   "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json", "--form", "--form-string",
                   "--upload-file", "--config", "--connect-to", "--resolve", "--interface", "--cacert", "--capath", "--key", "--pass",
                   "--retry", "--retry-delay", "--limit-rate", "--noproxy", "--proxy-user", "--oauth2-bearer", "--aws-sigv4",
                   "--unix-socket", "--doh-url", "--cert-type", "--key-type", "--ciphers", "--tls-max", "--tlsv1.3"}
CURL_VALUE_SHORT = set("HoubAecmwEzrCDdFTKXx")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


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
    command = command.replace("\n", " ; ").replace("`", " ; ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        cur, out = [], []
        for tok in lexer:
            if tok in SEPARATORS or (set(tok) <= set("&|;()<>") and tok):  # redirections and process substitutions split too
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append(tok)
        if cur:
            out.append(cur)
        return out
    except ValueError:
        # Unbalanced quotes (an apostrophe in a heredoc commit message, or a deliberate trick): fall back to a quote-blind split
        # so a command that starts with a network tool is still seen, while prose that merely mentions one is not.
        return [seg.split() for seg in re.split(r"[;&|()<>\n]+", command) if seg.split()]


def host_of(token):
    """Real URL parsing: the hostname, 'unknown-host' for an unparseable URL, or None when the token is not a destination."""
    if "://" in token:
        try:
            host = urlsplit(token).hostname
        except ValueError:
            return "unknown-host"
        return host.lower() if host else "unknown-host"
    m = SCP_HOST.match(token)
    if m:
        return m.group(1).lower()
    bare = token.split("/")[0].split("?")[0].split("#")[0]
    bare = bare.rsplit("@", 1)[-1]
    bare = re.sub(r":\d+$", "", bare)
    return bare.lower() if HOSTISH.match(bare) else None


def find_tool(argv):
    """If the command starts (after assignments) with a network tool, wrapper or shell keyword, return (tool, args) for the
    first network tool within it."""
    i = 0
    while i < len(argv) and ASSIGNMENT.match(argv[i]):
        i += 1
    argv = argv[i:]
    if not argv or os.path.basename(argv[0]) not in WRAPPERS | NET_TOOLS:
        return None
    for i, tok in enumerate(argv[:14]):
        if os.path.basename(tok) in NET_TOOLS:
            return os.path.basename(tok), argv[i + 1:]
    return None


def curl_is_upload(args):
    method = None
    for i, a in enumerate(args):
        if a.startswith("--"):
            if a.split("=")[0] in CURL_LONG_PAYLOAD:
                return True
            if a.split("=")[0] == "--request":
                method = (a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")).upper()
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for j, ch in enumerate(cluster):  # the rest of a cluster after a value-taking flag is that flag's value
                if ch in "dFTK":
                    return True
                if ch == "X":
                    method = (cluster[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")).upper()
                    break
                if ch in "HoubAecmwEzrCD":
                    break
    return method is not None and method not in SAFE_METHODS


def curl_destinations(args):
    """Every destination of a curl invocation; 'unknown-host' when a positional argument cannot be resolved."""
    skip, out = False, []
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--url="):
            out.append(host_of(a[6:]) or "unknown-host")
            continue
        if a == "--url":
            skip = False
            out.append("__url_next__")
            continue
        if out and out[-1] == "__url_next__":
            out[-1] = host_of(a) or "unknown-host"
            continue
        if a in CURL_VALUE_OPTS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:]):
                if ch in CURL_VALUE_SHORT:
                    skip = j == len(a) - 2  # value attached to the cluster, or in the next token
                    break
            continue
        if a.startswith("@") or "=@" in a:
            continue
        out.append(host_of(a) or "unknown-host")
    return [h for h in out if h != "__url_next__"] or ["unknown-host"]


def commands_in(command, depth=0):
    """Simple commands of `command`, including the contents of $(...), <(...), backtick and sh -c strings (best effort)."""
    out = []
    for argv in simple_commands(command):
        out.append(argv)
        if depth < 3:
            for tok in argv:
                if SUBSTITUTION.search(tok):
                    inner = re.sub(r"\$\(|<\(|>\(|\)", " ; ", tok)
                    out.extend(commands_in(inner, depth + 1))
            base = os.path.basename(argv[0]) if argv else ""
            if base in SHELLS:
                for i, tok in enumerate(argv[1:-1], 1):
                    if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", tok):
                        out.extend(commands_in(argv[i + 1], depth + 1))
    return out


def uploads(argv):
    """Return a list of (host, hint) pairs for every remote destination this simple command sends data to."""
    found = find_tool(argv)
    if not found:
        return []
    tool, args = found
    hint = " ".join(argv)[:120]
    hosts = []
    if tool == "curl":
        if not curl_is_upload(args):
            return []
        hosts = curl_destinations(args)
    elif tool == "wget":
        if not any(a.startswith(("--post-data", "--post-file", "--body-data", "--body-file")) or
                   (a.startswith("--method=") and a.split("=", 1)[1].upper() not in SAFE_METHODS) for a in args):
            return []
        hosts = [h for h in (host_of(a) for a in args if not a.startswith("-")) if h]
    elif tool in ("scp", "rsync", "sftp"):
        positional = [a for a in args if not a.startswith("-")]
        if tool == "sftp":
            hosts = [h for h in (host_of(a) for a in positional) if h]
        elif positional and host_of(positional[-1]) and (SCP_HOST.match(positional[-1]) or "://" in positional[-1]):
            hosts = [host_of(positional[-1])]
        elif any(SCP_HOST.match(a) or "://" in a for a in positional):
            return []  # remote is only a source: a download
    else:  # nc / ncat
        pos = [a for a in args if not a.startswith("-")]
        hosts = [pos[0].lower()] if pos else []
    return [(h, hint) for h in (hosts or ["unknown-host"])]


def judge_bash(event, profile, fp_fn):
    command = (event.get("tool_input") or {}).get("command") or ""
    if not NET_WORD.search(command):
        return None
    purpose = (event.get("tool_input") or {}).get("description") or None
    try:
        hits = [h for argv in commands_in(command) for h in uploads(argv)]
    except Exception:  # e.g. an unterminated quote: a network tool is named but the command cannot be read
        hits = [("unknown-host", command[:120])]
    if OPAQUE.search(command):
        hits.append(("unknown-host", command[:120]))  # a network tool is named inside a construct this hook cannot read
    worst, fp = None, None
    for host, hint in hits:
        action = {"kind": "outbound", "destination": host, "data_classes": ["unclassified"], "data_source": f"command arguments: {hint}",
                  "purpose": purpose, "visibility": "unknown"}
        if fp is None:
            try:
                fp = fp_fn()
            except Exception:
                fp = ""  # unreadable machine id: the profile cannot be verified, so it is ambiguous and fails closed
        res = wsp.decide(profile, action, fp)
        rank = {"ALLOW": 0, "ASK": 1, "DENY": 2}[res["verdict"]]
        if rank and (worst is None or rank > worst[0]):
            worst = (rank, res)
    if worst is None:
        return None
    res = worst[1]
    pk = res.get("ask_packet") or {}
    detail = "; ".join(res["reasons"])
    if res["verdict"] == "ASK" and pk:
        detail += f" | data={pk['data']} source={pk['source']} destination={pk['destination']} purpose={pk['purpose']} alternatives={pk['safer_alternatives']} risks={pk['risks']}"
    return decision("ask" if res["verdict"] == "ASK" else "deny", f"workstation policy ({res['profile_state']}): {detail}")


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
            command = (event.get("tool_input") or {}).get("command") or ""
            if NET_WORD.search(command):  # never stay silent about a command that names a network tool
                return decision("ask", f"workstation policy hook error while judging a command that names a network tool: {type(ex).__name__}")
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
