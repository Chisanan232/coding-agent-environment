#!/usr/bin/env python3
"""Read-only inventory of MCP server configuration by scope (SPE-117).

  mcp-scope-inspect.py [--home DIR] [--project-root DIR]... [--managed-file FILE] [--json]

Reports, per scope, each configured MCP server's NAME, transport and whether it is a GitHub MCP server, plus the exact
owner-run command that would remove ONLY the GitHub entry. It never prints arguments, environment values, headers or URLs:
those routinely carry tokens. A server is recognised as GitHub by its name, the host of its url, its command name or
a container image named like github-mcp-server; that is reported only as a category (`matched_by`), never as the text.
It writes nothing and runs no server, so it cannot reconnect, rebuild or repair a failed container.

Scopes read (Claude Code documentation): user (~/.claude.json `mcpServers`), local (~/.claude.json `projects.<dir>.mcpServers`),
project (`<root>/.mcp.json`), managed (managed-mcp.json). Servers supplied by plugins are not listed here; they cannot be
removed one by one, so retiring one means disabling its plugin, which also drops unrelated servers of that plugin: owner decision.
"""
import argparse
import json
import os
import pathlib
import re
import sys

GITHUB_NAME = re.compile(r"(^|[^a-z0-9])github([^a-z0-9]|$)", re.I)
GITHUB_IMAGE = re.compile(r"github[-_/]*mcp[-_]*server", re.I)
GITHUB_HOSTS = ("api.githubcopilot.com", "github.com", "api.github.com")
MANAGED_DEFAULT = "/Library/Application Support/ClaudeCode/managed-mcp.json"


def host_of(url):
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", str(url or ""), re.I)
    return m.group(1).lower() if m else ""


def github_match(name, spec):
    """Return a category string (never the matched text) or None."""
    if GITHUB_NAME.search(str(name)):
        return "name"
    if isinstance(spec, dict):
        host = host_of(spec.get("url"))
        if host and any(host == h or host.endswith("." + h) for h in GITHUB_HOSTS):
            return "url_host"
        cmd = os.path.basename(str(spec.get("command") or ""))
        if GITHUB_IMAGE.search(cmd):
            return "command"
        for a in spec.get("args") or []:
            if isinstance(a, str) and GITHUB_IMAGE.search(a):
                return "image"
    return None


TRANSPORTS = ("http", "sse", "stdio", "ws")


def transport(spec):
    """Allowlisted transport name only: the raw `type` text is never echoed."""
    if not isinstance(spec, dict):
        return "unknown"
    t = str(spec.get("type") or "").lower()
    if t:
        return t if t in TRANSPORTS else "other"
    return "http" if spec.get("url") else "stdio" if spec.get("command") else "unknown"


def printable(text, limit=200):
    """True when the text is safe to print and to put inside a quoted shell command."""
    t = str(text)
    return 0 < len(t) <= limit and t.isprintable()


def show(text, limit=60):
    """Display form: control characters become '?' and the length is capped."""
    t = "".join(c if c.isprintable() else "?" for c in str(text))
    return t if len(t) <= limit else t[:limit - 1] + "…"


def load(path):
    try:
        data = json.loads(pathlib.Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def servers_in(block):
    return block if isinstance(block, dict) else {}


def inspect(home, project_roots, managed_file):
    found, notes = [], []
    cj = load(pathlib.Path(home) / ".claude.json")
    if cj is None:
        notes.append("~/.claude.json missing or unreadable: user and local scopes not inspected")
    else:
        for n, s in servers_in(cj.get("mcpServers")).items():
            found.append(("user", "~/.claude.json", None, n, s))
        for proj, body in servers_in(cj.get("projects")).items():
            body = body if isinstance(body, dict) else {}
            for n, s in servers_in(body.get("mcpServers")).items():
                found.append(("local", "~/.claude.json", proj, n, s))
    for root in project_roots:
        p = pathlib.Path(root) / ".mcp.json"
        d = load(p)
        if d is None:
            if p.exists():
                notes.append(f"{p} unreadable")
            continue
        for n, s in servers_in(d.get("mcpServers")).items():
            found.append(("project", str(p), str(root), n, s))
    mf = pathlib.Path(managed_file)
    if mf.exists():
        d = load(mf)
        if d is None:
            notes.append(f"{mf} unreadable")
        else:
            for n, s in servers_in(d.get("mcpServers")).items():
                found.append(("managed", str(mf), None, n, s))
    rows = []
    for scope, src, proj, name, spec in found:
        gh = github_match(name, spec)
        row = {"scope": scope, "source": src, "server": show(name), "transport": transport(spec), "github": bool(gh), "matched_by": gh}
        if proj:
            row["project"] = show(proj, 200)
        if gh:
            row["retire"] = retire_command(scope, name, proj, src)
        rows.append(row)
    return {"servers": rows, "notes": notes}


def retire_command(scope, name, project, source):
    if not printable(name) or (project is not None and not printable(project)):
        return "unusual server name or path (control characters or very long): remove it by hand; no command is generated"
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"
    if scope == "user":
        return f"claude mcp remove {q(name)} -s user"
    if scope == "local":
        return f"cd {q(project)} && claude mcp remove {q(name)} -s local"
    if scope == "project":
        return f"remove the {q(name)} key from {source} by hand (project scope is shared and version-controlled)"
    return "managed scope: remove through the organization's managed-settings process; do not edit it here"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=str(pathlib.Path.home()))
    ap.add_argument("--project-root", action="append", default=[])
    ap.add_argument("--managed-file", default=MANAGED_DEFAULT)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = inspect(a.home, a.project_root, a.managed_file)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    rows = out["servers"]
    print(f"{len(rows)} MCP server entries across scopes; GitHub entries: {sum(r['github'] for r in rows)}")
    for r in rows:
        mark = "GITHUB" if r["github"] else "      "
        print(f"  {mark} {r['scope']:<8} {r['server'][:36]:<36} {r['transport']}" + (f"  project={r['project']}" if r.get("project") else ""))
        if r.get("retire"):
            print(f"         retire: {r['retire']}")
    for n in out["notes"]:
        print("  note:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
