#!/usr/bin/env python3
"""Workstation-profile policy decisions and settings-candidate linting (SPE-116). Read-only.

  workstation-policy.py fingerprint
  workstation-policy.py evaluate --profile FILE --action JSON [--fingerprint SHA256]
  workstation-policy.py render-settings --kind personal|corporate [--out FILE]
  workstation-policy.py lint-settings FILE

`evaluate` answers ALLOW, ASK or DENY for one described action. It is a decision function and a
validation design, not host enforcement: the host enforces through permissions.ask/deny, hooks and
managed settings that the owner deploys. This script never reads or writes ~/.claude, managed
settings or credentials, and `render-settings` refuses to write to a protected settings location.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

PROTECTED_DATA = {"source_code", "documents", "structured_data", "logs", "screenshots", "recordings",
                  "internal_config", "personal_data"}
SENSITIVE_PERSONAL = PROTECTED_DATA - {"source_code"}  # source code to an ordinary private dev host is routine
KNOWN_CLASSES = PROTECTED_DATA | {"public", "credentials", "auth_handshake"}
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
PLACEHOLDER = re.compile(r"<[^<>]+>")
PROTECTED_PATHS = (
    "~/.claude", "~/.claude.json", "/Library/Application Support/ClaudeCode", "/etc/claude-code", "/private/etc/claude-code", "/Library/Managed Preferences",
)
PROTECTED_BASENAMES = {"managed-settings.json", "managed-mcp.json", ".mcp.json"}
PROTECTED_RELATIVE = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", ".claude/claude.md", ".claude/agents",
                      ".claude/skills", ".claude/commands", ".claude/rules")
# Settings keys that may change without owner approval; every other key can run commands or move a trust boundary.
BENIGN_KEYS = {"model", "effortlevel", "advisormodel", "theme", "verbose"}
NON_GOVERNED_KINDS = {"local"}
# The only wildcard Bash allows a candidate may carry; everything else with a `*` is reported. Case-sensitive on purpose.
READ_ONLY_ALLOW = re.compile(
    r"Bash\((git (status|diff|log|show)|gh (pr|issue|run) (view|list|checks|diff|status)|ls|cat|pytest|python3 -m pytest)(:\*| \*)\)")

ALTERNATIVES = {
    "documents": "share a redacted excerpt or a synthetic stand-in", "logs": "share a redacted excerpt with identifiers removed",
    "screenshots": "crop to the relevant region or use a synthetic reproduction", "recordings": "use a synthetic recording or a text summary",
    "structured_data": "use a synthetic fixture with the same shape", "internal_config": "describe the shape without values",
    "personal_data": "use synthetic data", "source_code": "share only the minimal reproducing snippet",
}
RISKS = {"documents": "content may be retained or indexed by the destination", "logs": "logs often embed identifiers and tokens",
         "screenshots": "images can expose unrelated windows and secrets", "recordings": "recordings capture more than intended",
         "structured_data": "records may contain personal or customer fields", "internal_config": "reveals topology and trust boundaries",
         "personal_data": "disclosure of personal data is hard to retract", "source_code": "code may be retained by the destination"}


def fingerprint():
    """Hash of the machine UUID so profiles can pin a machine without storing the raw identifier."""
    out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(r'"IOPlatformUUID" = "([0-9A-Fa-f-]{36})"', out)
    if not m:
        raise RuntimeError("machine id unavailable")
    return hashlib.sha256(m.group(1).lower().encode()).hexdigest()


def profile_state(profile, fp):
    """Return (kind, problems). Anything unverifiable is AMBIGUOUS, never inferred from a username."""
    problems = []
    if not isinstance(profile, dict) or profile.get("schema") != 1:
        return "AMBIGUOUS", ["profile schema missing or unsupported"]
    kind = profile.get("kind")
    if kind not in ("personal", "corporate"):
        problems.append("profile kind is neither personal nor corporate")
    want = str(profile.get("host_fingerprint_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        problems.append("profile has no concrete machine fingerprint (template or placeholder)")
    elif fp is None or want != fp:
        problems.append("machine fingerprint does not match the profile")
    return ("AMBIGUOUS" if problems else kind), problems


def host_ok(host, allowed):
    host = (host or "").strip().lower()
    if not HOST_RE.fullmatch(host):
        return False
    for a in allowed or []:
        a = str(a).lower()
        if PLACEHOLDER.search(a) or not HOST_RE.fullmatch(a.lstrip("*.")):
            continue
        if a.startswith("*."):
            if host.endswith(a[1:]) and host != a[2:]:
                return True
        elif host == a:
            return True
    return False


def is_protected_path(path):
    """Case-insensitive, symlink-resolved, relative-aware. Unparseable input is treated as protected."""
    if not isinstance(path, str) or not path or "\0" in path:
        return True
    raw = os.path.normpath(os.path.expanduser(path))
    forms = {raw, os.path.realpath(os.path.expanduser(path))}
    bases = set()
    for prot in PROTECTED_PATHS:
        base = os.path.normpath(os.path.expanduser(prot))
        bases |= {base, os.path.realpath(base)}
    bases = {b.casefold() for b in bases}
    for form in forms:
        cf = form.casefold()
        if os.path.basename(cf) in PROTECTED_BASENAMES:
            return True
        if any(cf == b or cf.startswith(b + os.sep) for b in bases):
            return True
        hay = "/" + cf.replace(os.sep, "/").lstrip("/") + "/"
        if any(("/" + r + "/") in hay for r in PROTECTED_RELATIVE):
            return True
    return False


def decide(profile, action, fp):
    state, problems = profile_state(profile, fp)
    kind = action.get("kind") if isinstance(action, dict) else None
    if isinstance(kind, str):
        kind = kind.strip().lower()
        action = {**action, "kind": kind}
    out = {"profile_state": state, "action_kind": kind, "reasons": list(problems)}

    def done(verdict, *why, **extra):
        out.update(verdict=verdict, reasons=out["reasons"] + list(why), **extra)
        return out

    if kind == "modify_policy":
        path, key = action.get("path"), action.get("key")
        if (path is None and key is None) or (path is not None and is_protected_path(path)) \
                or (key is not None and str(key).split(".")[0].casefold() not in BENIGN_KEYS):
            return done("ASK", "POLICY_CHANGE_NEEDS_OWNER_APPROVAL: effective Auto Mode, permissions, hooks, sandbox and trust boundaries are owner-approved",
                        owner_approval_required=True)
        return done("ALLOW", "not a protected policy location")
    if kind != "outbound":
        if isinstance(kind, str) and kind in NON_GOVERNED_KINDS:
            return done("ALLOW", "explicitly non-governed local action")
        return done("DENY", "UNKNOWN_ACTION_KIND: only outbound, modify_policy and local actions are described; fail closed")
    classes = action.get("data_classes")
    if not isinstance(classes, list) or not classes or not set(map(str, classes)) <= KNOWN_CLASSES:
        return done("DENY", "UNKNOWN_DATA_CLASS: outbound data must be classified; fail closed")
    classes = set(classes)
    dest = str(action.get("destination", "")).lower()
    if "credentials" in classes:
        return done("DENY", "RAW_CREDENTIAL_OUTBOUND: raw credentials never leave; approved authentication uses the client's own credential store")
    if state == "AMBIGUOUS":
        if classes <= {"public"}:
            return done("ASK", "AMBIGUOUS_PROFILE: only public data may even be asked about", ask_packet=ask_packet(action, dest, classes))
        return done("DENY", "AMBIGUOUS_PROFILE: fail closed until the machine profile is verified")
    if classes <= {"auth_handshake"}:
        if host_ok(dest, profile.get("auth_hosts")):
            return done("ALLOW", "approved authentication host")
        return done("DENY" if state == "corporate" else "ASK", "AUTH_HOST_NOT_APPROVED")
    approved = host_ok(dest, profile.get("approved_destinations"))
    if state == "corporate":
        if classes <= {"public"}:
            return done("ALLOW", "public data")
        if approved:
            return done("ALLOW", "destination is inside the approved corporate trust boundary")
        return done("DENY", "CORPORATE_OUTBOUND_PROHIBITED: destination is outside the formally approved trust boundary")
    # personal workstation
    if classes <= {"public"}:
        return done("ALLOW", "public data")
    visibility = action.get("visibility", "unknown")
    routine = approved and visibility == "private" and not (classes & SENSITIVE_PERSONAL)
    if routine:
        return done("ALLOW", "routine development traffic to an approved private destination")
    packet = ask_packet(action, dest, classes)
    if packet["missing"]:
        return done("DENY", f"ASK_PACKET_INCOMPLETE: cannot ask without {packet['missing']}")
    return done("ASK", "SENSITIVE_OUTBOUND_NEEDS_PER_OPERATION_APPROVAL", ask_packet=packet)


def ask_packet(action, dest, classes):
    sensitive = sorted(classes & PROTECTED_DATA)
    packet = {"data": sorted(classes), "source": action.get("data_source"), "destination": dest, "purpose": action.get("purpose"),
              "safer_alternatives": [ALTERNATIVES[c] for c in sensitive], "risks": [RISKS[c] for c in sensitive]}
    packet["missing"] = [k for k in ("source", "purpose") if not packet[k]] + ([] if dest else ["destination"])
    return packet


# The host's rtk PreToolUse hook rewrites `git ...` to `rtk git ...` before rules match, so every Bash ask needs both forms.
ASK_BASH_RULES = [
    "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --force-with-lease:*)", "Bash(git push * --force*)",
    "Bash(git push * -f)", "Bash(git push * -f *)", "Bash(git push * +*)", "Bash(git push origin --delete:*)",
    "Bash(git push * --delete*)", "Bash(gh api * merge*)", "Bash(gh release create:*)", "Bash(gh release upload:*)",
    "Bash(gh pr merge * --admin*)", "Bash(gh pr merge --admin*)",
]
ASK_PATH_RULES = [f"{tool}({path})" for tool in ("Edit", "Write") for path in (
    "~/.claude/settings.json", "~/.claude/settings.local.json", "~/.claude/hooks/**", "~/.claude/bin/**", "~/.claude.json",
    "**/.claude/settings.json", "**/.claude/settings.local.json", "**/.claude/hooks/**", "**/.mcp.json")]


def candidate(kind):
    env = [
        "Source of truth: GitHub repositories owned by the owner; Jira for tickets; the workstation profile is " + kind + ".",
        "Task-owned resources (own worktree, scratch dir, regenerable build output) may be created and removed; other sessions' work is not task-owned.",
        "Routine merges follow the repository's documented PR class policy; a host denial stands and is reported, never rerouted.",
    ]
    if kind == "corporate":
        env.append("Corporate machine: data leaves only to destinations inside the formally approved corporate trust boundary.")
    else:
        env.append("Personal machine: sensitive outbound disclosure is approved per operation.")
    return {
        "_scope_note": "Candidate only. Review and deploy by hand; never applied automatically. Keeps the built-in rule lists via $defaults. The ask patterns are prefix/glob matches, not a complete gate: wrapped or reordered commands can evade them, so test them against the live host before relying on them.",
        "autoMode": {"environment": ["$defaults"] + env, "allow": ["$defaults"], "soft_deny": ["$defaults"], "hard_deny": ["$defaults"]},
        "permissions": {
            "ask": ASK_BASH_RULES + [r.replace("Bash(", "Bash(rtk ", 1) for r in ASK_BASH_RULES] + ASK_PATH_RULES,
            "deny": [],
        },
    }


def lint_settings(cfg):
    """Invariants every candidate must hold. Returns a list of violations (empty means clean)."""
    bad = []
    am = cfg.get("autoMode") or {}
    for key in ("allow", "soft_deny", "hard_deny", "environment"):
        if key in am and (not am[key] or am[key][0] != "$defaults"):
            bad.append(f"autoMode.{key} must start with $defaults")
    env = am.get("environment") or []
    if len(env) > 8 or any(len(str(e)) > 300 for e in env):
        bad.append("autoMode.environment must stay short and factual (<= 8 entries of <= 300 chars)")
    for rule in am.get("allow", [])[1:]:
        bad.append(f"custom autoMode.allow entry needs an independently documented need: {str(rule)[:60]}")
    blob = json.dumps(cfg)
    if re.search(r'"Bash\(\*\)"|Bash\(\*:\*\)', blob):
        bad.append("blanket Bash(*) permission")
    if re.search(r"bypassPermissions|dangerously-skip-permissions", blob):
        bad.append("bypassPermissions must not appear")
    if (cfg.get("permissions") or {}).get("defaultMode") in ("dontAsk", "bypassPermissions") or cfg.get("defaultMode") in ("dontAsk", "bypassPermissions"):
        bad.append("defaultMode removes prompts")
    if re.search(r"authorized pull request merge|standing\b.{0,40}\bmerge|merge\b.{0,40}\b(authori[sz]ation|without review)", blob, re.I):
        bad.append("standing merge authorization text must not return")
    asks = [str(r) for r in (cfg.get("permissions") or {}).get("ask", [])]
    for r in asks:
        if r.startswith("Bash(") and not r.startswith("Bash(rtk ") and r.replace("Bash(", "Bash(rtk ", 1) not in asks:
            bad.append(f"ask rule has no rtk-rewritten twin: {r}")
    for r in (cfg.get("permissions") or {}).get("allow", []):
        r = str(r)
        if re.fullmatch(r"(Bash|Edit|Write|PowerShell|WebFetch)(\(\*+\))?", r):
            bad.append(f"blanket allow {r}")
        elif re.search(r"gh pr merge|gh api.*merge|--admin", r):
            bad.append(f"standing merge permission {r}")
        elif "*" in r and not READ_ONLY_ALLOW.fullmatch(r):
            bad.append(f"wildcard allow outside the read-only allowlist {r}")
    return bad


def safe_out(path):
    p = pathlib.Path(os.path.expanduser(path)).resolve()
    if is_protected_path(str(p)) or p.name.casefold() in ("settings.json", "settings.local.json") or ".claude" in map(str.casefold, p.parts):
        raise PermissionError("refusing to write to a protected settings location; review the candidate and deploy it by hand")
    if p.suffix != ".json":
        raise PermissionError("candidate output must be a .json file")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fingerprint")
    e = sub.add_parser("evaluate")
    e.add_argument("--profile", required=True)
    e.add_argument("--action", required=True)
    e.add_argument("--fingerprint")
    r = sub.add_parser("render-settings")
    r.add_argument("--kind", required=True, choices=("personal", "corporate"))
    r.add_argument("--out")
    lint = sub.add_parser("lint-settings")
    lint.add_argument("file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fingerprint":
            print(fingerprint())
            return 0
        if a.cmd == "evaluate":
            profile = json.load(open(a.profile))
            if a.fingerprint is not None and os.environ.get("WORKSTATION_POLICY_TEST_FINGERPRINT") != "1":
                raise PermissionError("--fingerprint is a test hook; set WORKSTATION_POLICY_TEST_FINGERPRINT=1 to use it")
            fp = a.fingerprint if a.fingerprint is not None else fingerprint()
            out = decide(profile, json.loads(a.action), fp)
            print(json.dumps(out, indent=1))
            return {"ALLOW": 0, "ASK": 1, "DENY": 2}[out["verdict"]]
        if a.cmd == "render-settings":
            text = json.dumps(candidate(a.kind), indent=2) + "\n"
            if a.out:
                with open(safe_out(a.out), "x") as fh:  # exclusive create: never overwrites an existing file
                    fh.write(text)
            else:
                sys.stdout.write(text)
            return 0
        problems = lint_settings(json.load(open(a.file)))
        print(json.dumps({"verdict": "CLEAN" if not problems else "VIOLATIONS", "problems": problems}, indent=1))
        return 0 if not problems else 1
    except Exception as ex:
        print(json.dumps({"verdict": "ERROR", "reasons": [f"{type(ex).__name__}: {str(ex)[:200]}"]}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
