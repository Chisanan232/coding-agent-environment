#!/usr/bin/env python3
"""Workstation-profile policy decisions and settings-candidate linting (SPE-116). Read-only.

  workstation-policy.py fingerprint
  workstation-policy.py evaluate --profile FILE --action JSON [--fingerprint SHA256]
  workstation-policy.py render-settings --kind personal|corporate [--asks-only | --proxy-scoped] [--out FILE]
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
                  "internal_config", "personal_data", "unclassified"}
SENSITIVE_PERSONAL = PROTECTED_DATA - {"source_code"}  # source code to an ordinary private dev host is routine
KNOWN_CLASSES = PROTECTED_DATA | {"public", "credentials", "auth_handshake"}
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
PLACEHOLDER = re.compile(r"<[^<>]+>")
PROTECTED_PATHS = (
    "~/.claude", "~/.claude.json", "/Library/Application Support/ClaudeCode", "/etc/claude-code", "/private/etc/claude-code", "/Library/Managed Preferences",
)
PROTECTED_BASENAMES = {"managed-settings.json", "managed-mcp.json", ".mcp.json"}
PROTECTED_RELATIVE = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", ".claude/claude.md", ".claude/agents",
                      ".claude/skills", ".claude/commands", ".claude/rules", ".claude/plugins")
# Settings keys that may change without owner approval; every other key can run commands or move a trust boundary.
BENIGN_KEYS = {"model", "effortlevel", "advisormodel", "theme", "verbose"}
NON_GOVERNED_KINDS = {"local"}
# The only wildcard Bash allows a candidate may carry; everything else with a `*` is reported. Case-sensitive on purpose.
# Read-only git verbs seen denied in real sessions (git ls-tree) with no allow: bare and rtk spellings. cat-file and blame are left out (cat-file --textconv/--filters and blame, which enables textconv by default, run configured helpers).
ROUTINE_BARE_ALLOW = [f"Bash({p}git {v}:*)" for v in ("ls-tree", "merge-base", "describe") for p in ("", "rtk ")]
# `rg` is deliberately absent: `rg --pre CMD` runs CMD, and no bare rg allow exists to be at parity with.
# SPE-119 adds the scoped `rtk proxy` spellings of read-only git, search and local cargo checks: `rtk proxy` is never rewritten by the
# hook, so a rule naming the plain command does not see it, and the broad `rtk proxy *` ask it replaces cannot be overridden by an allow.
PROXY_ROUTINE_ALLOW = (
    [f"Bash(rtk proxy git {v}:*)" for v in ("diff", "show", "log", "status", "rev-parse", "rev-list", "ls-files", "merge-base")]
    + [f"Bash(rtk proxy git {v}:*)" for v in ("ls-tree", "describe")]
    + ["Bash(rtk proxy grep:*)"]
    + [f"Bash(rtk proxy cargo {v}:*)" for v in ("test", "build", "check", "clippy", "fmt", "nextest run")]
)
# Wrapped twins of the bare asks that write files, run helpers or reach outside the repository (SPE-114 parity gaps).
# git accepts any unique prefix of a long option, so the prefixes below are the shortest that stay unambiguous (--ou*, --ext*, --tex*,
# --upl*); --no-index has no shorter unique spelling. A wrapped merge is never allowed by a wrapper rule: the pinned form is the plain command.
PROXY_PARITY_ASKS = ["Bash(rtk proxy git*--ou*)", "Bash(rtk proxy git*--ext*)", "Bash(rtk proxy git*--no-index*)", "Bash(rtk proxy git fetch*--upl*)",
                     "Bash(rtk proxy git*--tex*)", "Bash(rtk proxy gh pr merge*)"]
# Every bare protected ask also asks when wrapped, so wrapping a release, publish, delete or force-push never lowers its protection.
PROXY_TWIN_ASKS = None  # filled after ASK_BASH_RULES is defined
# The only ask candidate-rollout may retire; any other ask stays until the owner removes it by hand.
RETIRABLE_ASKS = ("Bash(rtk proxy *)",)
READ_ONLY_ALLOW = re.compile(
    r"Bash\((git (status|diff|log|show)|gh (pr|issue|run) (view|list|checks|diff|status)|ls|cat|pytest|python3 -m pytest)(:\*| \*)\)"
    r"|Bash\((rtk )?git (ls-tree|merge-base|describe):\*\)"
    r"|Bash\(rtk proxy (git (diff|show|log|status|rev-parse|rev-list|ls-files|merge-base|ls-tree|describe)|grep|cargo (test|build|check|clippy|fmt|nextest run)):\*\)")

ALTERNATIVES = {
    "documents": "share a redacted excerpt or a synthetic stand-in", "logs": "share a redacted excerpt with identifiers removed",
    "screenshots": "crop to the relevant region or use a synthetic reproduction", "recordings": "use a synthetic recording or a text summary",
    "structured_data": "use a synthetic fixture with the same shape", "internal_config": "describe the shape without values",
    "personal_data": "use synthetic data", "source_code": "share only the minimal reproducing snippet",
    "unclassified": "name the data class and share a redacted or synthetic stand-in",
    "auth_handshake": "authenticate only to hosts on the approved list, from the client's own credential store",
}
RISKS = {"documents": "content may be retained or indexed by the destination", "logs": "logs often embed identifiers and tokens",
         "screenshots": "images can expose unrelated windows and secrets", "recordings": "recordings capture more than intended",
         "structured_data": "records may contain personal or customer fields", "internal_config": "reveals topology and trust boundaries",
         "personal_data": "disclosure of personal data is hard to retract", "source_code": "code may be retained by the destination",
         "unclassified": "the payload cannot be judged, so it may be sensitive",
         "auth_handshake": "credentials sent to an unapproved host can leak"}


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
        if state == "corporate":
            return done("DENY", "AUTH_HOST_NOT_APPROVED")
        return done("ASK", "AUTH_HOST_NOT_APPROVED", ask_packet=ask_packet(action, dest, classes))
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
    sensitive = sorted(classes & (PROTECTED_DATA | {"auth_handshake"}))
    packet = {"data": sorted(classes), "source": action.get("data_source"), "destination": dest, "purpose": action.get("purpose"),
              "safer_alternatives": [ALTERNATIVES[c] for c in sensitive], "risks": [RISKS[c] for c in sensitive]}
    packet["missing"] = [k for k in ("source", "purpose") if not packet[k]] + ([] if dest else ["destination"])
    return packet


# The host's rtk PreToolUse hook rewrites `git ...` to `rtk git ...` before rules match, so every Bash ask needs both forms.
# `git push * :**` (not `:*`): a pattern ending in `:*` is read as the legacy prefix suffix and never matched a colon refspec (measured on the host).
ASK_BASH_RULES = [
    "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --force-with-lease:*)", "Bash(git push * --force*)",
    "Bash(git push * -f)", "Bash(git push * -f *)", "Bash(git push * +*)", "Bash(git push origin --delete:*)",
    "Bash(git push * --delete*)", "Bash(git push --delete*)", "Bash(git push -d *)", "Bash(git push * -d *)", "Bash(git push * -d)",
    "Bash(git push :**)", "Bash(git push * :**)", "Bash(gh api *pulls/*/merge*)", "Bash(gh release create:*)", "Bash(gh release upload:*)",
    "Bash(gh pr merge * --admin*)", "Bash(gh pr merge --admin*)",
    "Bash(gh repo delete:*)", "Bash(gh api * -X DELETE*)", "Bash(gh api * --method DELETE*)", "Bash(gh api -X DELETE*)",
    "Bash(gh api --method DELETE*)",
    "Bash(gh api *DELETE*)", "Bash(gh api *delete*)", "Bash(gh api *Delete*)",  # also -XDELETE, --method=DELETE, -X=DELETE
    "Bash(npm publish:*)", "Bash(pnpm publish:*)", "Bash(yarn npm publish:*)", "Bash(cargo publish:*)", "Bash(twine upload:*)",
    "Bash(yarn publish:*)", "Bash(python3 -m twine upload:*)", "Bash(python -m twine upload:*)", "Bash(uv publish:*)", "Bash(docker push:*)",
]
ASK_PATH_RULES = [f"{tool}({path})" for tool in ("Edit", "Write") for path in (
    "~/.claude/settings.json", "~/.claude/settings.local.json", "~/.claude/hooks/**", "~/.claude/bin/**", "~/.claude.json",
    "**/.claude/settings.json", "**/.claude/settings.local.json", "**/.claude/hooks/**", "**/.claude/plugins/**", "**/.mcp.json")]


PROXY_TWIN_ASKS = [r.replace("Bash(", "Bash(rtk proxy ", 1) for r in ASK_BASH_RULES]


def candidate(kind, asks_only=False):
    env = [
        "Source of truth: GitHub repositories owned by the owner; Jira for tickets; the workstation profile is " + kind + ".",
        "Task-owned resources (own worktree, scratch dir, regenerable build output) may be created and removed; other sessions' work is not task-owned.",
        "Merges follow each repository's documented PR class policy and are never pre-authorized by this text; a host denial stands and is reported, never rerouted.",
    ]
    if kind == "corporate":
        env.append("Corporate machine: data leaves only to destinations inside the formally approved corporate trust boundary.")
    else:
        env.append("Personal machine: no standing approval exists for sending sensitive data to external services; each such send needs explicit user approval at that time.")
    cand = {
        "_scope_note": "Candidate only. Review and deploy by hand; never applied automatically. Keeps the built-in rule lists via $defaults. The ask patterns are prefix/glob matches, not a complete gate: wrapped or reordered commands can evade them, so test them against the live host before relying on them.",
        "autoMode": {"environment": ["$defaults"] + env, "allow": ["$defaults"], "soft_deny": ["$defaults"], "hard_deny": ["$defaults"]},
        "permissions": {
            "ask": ASK_BASH_RULES + [r.replace("Bash(", "Bash(rtk ", 1) for r in ASK_BASH_RULES] + ASK_PATH_RULES,
            "deny": [],
        },
    }
    if asks_only:  # canary shape: asks only; the owner's existing autoMode (or its absence) is left exactly as it is
        cand.pop("autoMode")
    return cand


MERGE_SCOPE_BASH_ASKS = ["Bash(gh pr merge:*)", "Bash(gh pr merge *)", "Bash(gh api *pulls/*/merge*)"]
MERGE_SCOPE_OTHER_ASKS = ["mcp__github__merge_pull_request"]


def merge_scope_candidate():
    """SPE-118: make every direct merge route ask. Asks win over allows, so the broad `gh pr merge` / `gh pr *` / GitHub MCP merge
    allows stop working without being removed. Adds no allow, no autoMode change and no runner rule (that broadens host
    authorization and stays an owner decision). Until the owner also allows the validating runner, every merge prompts."""
    asks = MERGE_SCOPE_BASH_ASKS + [r.replace("Bash(", "Bash(rtk ", 1) for r in MERGE_SCOPE_BASH_ASKS] + MERGE_SCOPE_OTHER_ASKS
    return {
        "_scope_note": "Candidate only. Review and deploy by hand with candidate-rollout.py; never applied automatically. Adds asks for the direct merge routes "
                       "(gh pr merge, rtk gh pr merge, gh api .../merge, the GitHub MCP merge tool) so the broad merge allows no longer run silently. "
                       "Adds no allow and no autoMode change; the validating runner's allow rule stays an owner step. Every merge prompts until that rule exists.",
        "permissions": {"ask": asks, "deny": []},
    }


def proxy_scoped_candidate():
    """SPE-119: retire the broad `rtk proxy *` ask, keep every protected wrapped operation asking, allow routine wrapped work.

    The scoped asks are read from the repository baseline (the SPE-114 rules and their tests), not retyped here.
    """
    base = json.loads((pathlib.Path(__file__).resolve().parent.parent / ".claude/settings.json").read_text())["permissions"]["ask"]
    gh_api = {f"Bash({p}gh api {f}*)" for p in ("", "rtk ") for f in ("-X DELETE", "--method DELETE")}
    asks = [r for r in base if "rtk proxy" in r or r in gh_api]
    asks += [r for r in PROXY_PARITY_ASKS + PROXY_TWIN_ASKS if r not in asks]
    return {
        "_scope_note": "Candidate only. Review and deploy by hand with candidate-rollout.py; never applied automatically. Retires the broad Bash(rtk proxy *) ask "
                       "(an ask cannot be overridden by a narrower allow) and replaces it with scoped asks plus scoped allows for routine wrapped work. No autoMode change.",
        "permissions": {"ask": asks, "allow": PROXY_ROUTINE_ALLOW + ROUTINE_BARE_ALLOW, "retire_ask": list(RETIRABLE_ASKS), "deny": []},
    }


# A custom autoMode.allow entry is classifier guidance, never proof of review or authorization. It is accepted only when the
# owner supplied that exact string in a registry file (the agent never authors this text) and it stays scoped to the
# validating runner with an exact-head pin; the runner and gate remain the real enforcement.
OWNER_ALLOW_REQUIRED = ("owner-merge-runner", "--expect-head")
OWNER_ALLOW_FORBIDDEN = re.compile(
    r"--admin|gh pr merge|bypass|dangerously|Bash\(|\bstanding\b|\bwithout\b|\bskip|\balways\b|\bwhatever\b|\bautomatic|\bapprov"
    r"|\boverride|\bregardless|\bno review|\bnot needed|\boptional|\bomit|\bunreview|\bbot\b|\bpre-?authori|\bauto-?merge|\beligible"
    r"|\bfuture\b|\bwaive|\bdisable|\bignore|\bauthori[sz]ed?\b.{0,40}\bmerge|\bmerge\w*\b.{0,40}\bauthori[sz]ed?\b"
    r"|\b(any|every|all|each)\b.{0,30}\b(pull requests?|PRs?|merges?|repositor(y|ies))\b", re.I)
STANDING_MERGE = re.compile(r"authorized pull request merge|standing\b.{0,40}\bmerge|merge\b.{0,40}\b(authori[sz]ation|without review)", re.I)
OWNER_ALLOW_MAX = 3


def load_owner_allow(path):
    """Owner-supplied registry: a JSON array of strings. Anything else is refused rather than guessed at."""
    return parse_owner_allow(pathlib.Path(path).read_bytes())


def parse_owner_allow(raw):
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(e, str) and e for e in data):
        raise ValueError("owner allow registry must be a JSON array of non-empty strings")
    return data


def owner_allow_problems(entry):
    bad = [f"owner autoMode.allow entry must mention {t!r}: {entry[:60]}" for t in OWNER_ALLOW_REQUIRED if t not in entry]
    if len(entry) > 400:
        bad.append(f"owner autoMode.allow entry over 400 chars: {entry[:60]}")
    if not entry.isascii() or not entry.isprintable():  # homoglyphs and zero-width/format characters can hide a forbidden phrase
        bad.append(f"owner autoMode.allow entry must be printable ASCII on one line: {entry[:60]!r}")
    flat = " ".join(entry.split())  # newlines, tabs and runs of spaces must not split a forbidden phrase
    if OWNER_ALLOW_FORBIDDEN.search(flat) or STANDING_MERGE.search(flat):
        bad.append(f"owner autoMode.allow entry is broader than the pinned runner: {entry[:60]}")
    return bad


def lint_settings(cfg, owner_allow=()):
    """Invariants every candidate must hold. Returns a list of violations (empty means clean)."""
    bad = []
    owner_allow = set(owner_allow)
    accepted = [r for r in (cfg.get("autoMode") or {}).get("allow", [])[1:] if isinstance(r, str) and r in owner_allow]
    for rule in accepted:
        bad += owner_allow_problems(rule)
    if len(accepted) > OWNER_ALLOW_MAX:
        bad.append(f"more than {OWNER_ALLOW_MAX} owner autoMode.allow entries")
    am = cfg.get("autoMode") or {}
    for key in ("allow", "soft_deny", "hard_deny", "environment"):
        if key in am and (not am[key] or am[key][0] != "$defaults"):
            bad.append(f"autoMode.{key} must start with $defaults")
    env = am.get("environment") or []
    if len(env) > 8 or any(len(str(e)) > 300 for e in env):
        bad.append("autoMode.environment must stay short and factual (<= 8 entries of <= 300 chars)")
    for rule in am.get("allow", [])[1:]:
        if isinstance(rule, str) and rule in accepted:
            continue  # owner-registered: judged by owner_allow_problems and, like all text, by the blob checks below
        bad.append(f"custom autoMode.allow entry needs an independently documented need: {str(rule)[:60]}")
    blob = json.dumps(cfg)
    if re.search(r'"Bash\(\*\)"|Bash\(\*:\*\)', blob):
        bad.append("blanket Bash(*) permission")
    if re.search(r"bypassPermissions|dangerously-skip-permissions", blob):
        bad.append("bypassPermissions must not appear")
    if (cfg.get("permissions") or {}).get("defaultMode") in ("dontAsk", "bypassPermissions") or cfg.get("defaultMode") in ("dontAsk", "bypassPermissions"):
        bad.append("defaultMode removes prompts")
    if STANDING_MERGE.search(blob):
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
    r.add_argument("--asks-only", action="store_true", help="omit the autoMode block (minimal canary candidate)")
    r.add_argument("--proxy-scoped", action="store_true", help="SPE-119 candidate: retire the broad rtk proxy ask, add scoped asks and allows")
    r.add_argument("--merge-scope", action="store_true", help="SPE-118 candidate: ask for every direct merge route (no allow, no autoMode change)")
    r.add_argument("--owner-allow-file", help="JSON array of owner-authored autoMode.allow entries to append after $defaults (each is linted; the agent never writes this text)")
    lint = sub.add_parser("lint-settings")
    lint.add_argument("file")
    lint.add_argument("--owner-allow-file", help="owner registry that makes matching custom autoMode.allow entries acceptable")
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
            cand = merge_scope_candidate() if a.merge_scope else proxy_scoped_candidate() if a.proxy_scoped else candidate(a.kind, asks_only=a.asks_only)
            if a.owner_allow_file:
                owner_allow = load_owner_allow(a.owner_allow_file)
                if "autoMode" not in cand:
                    raise ValueError("--owner-allow-file needs a candidate with an autoMode block (not --asks-only or --proxy-scoped)")
                cand["autoMode"]["allow"] = ["$defaults"] + owner_allow
                problems = lint_settings(cand, owner_allow)
                if problems:
                    raise ValueError("owner allow registry rejected: " + "; ".join(problems))
            text = json.dumps(cand, indent=2) + "\n"
            if a.out:
                with open(safe_out(a.out), "x") as fh:  # exclusive create: never overwrites an existing file
                    fh.write(text)
            else:
                sys.stdout.write(text)
            return 0
        problems = lint_settings(json.load(open(a.file)), load_owner_allow(a.owner_allow_file) if a.owner_allow_file else ())
        print(json.dumps({"verdict": "CLEAN" if not problems else "VIOLATIONS", "problems": problems}, indent=1))
        return 0 if not problems else 1
    except Exception as ex:
        print(json.dumps({"verdict": "ERROR", "reasons": [f"{type(ex).__name__}: {str(ex)[:200]}"]}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
