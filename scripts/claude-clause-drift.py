#!/usr/bin/env python3
"""Report live Claude policy clauses that exist in neither the public base nor the private overlay.

Only headings and numbered rules are compared. Output carries line numbers and short
hashes, never clause text, so a private overlay cannot leak through the report.
"""
import argparse
import hashlib
import importlib.util
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUSE = re.compile(r"^(?:#{1,6}\s+(?P<heading>.+?)|\d+\.\s+(?P<rule>.{1,60}).*)\s*$")
KEYWORDS = re.compile(r"<!--\s*clause-keywords:\s*(.*?)\s*-->")


def digest(kind: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()[:10]


def clauses(text: str):
    found = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else CLAUSE.match(line)
        if match:
            kind = "heading" if match.group("heading") else "rule"
            found.append((number, kind, digest(kind, match.group("heading") or match.group("rule"))))
    return found


def overlay_path(home: Path) -> Path:
    local = Path(os.environ.get("CODING_AGENT_LOCAL_DIR") or home / ".config/coding-agent-environment/local")
    base = Path(os.environ.get("CODING_AGENT_LOCAL_BASELINE_DIR") or local / "baseline")
    return base / "claude/CLAUDE.private.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=os.environ.get("CODING_AGENT_SYNC_HOME") or str(Path.home()))
    parser.add_argument("--ledger", help="local policy ledger (default: the baseline policy-ledger.tsv when present)")
    args = parser.parse_args()
    home = Path(args.home)
    live_path = home / ".claude/CLAUDE.md"
    if not live_path.is_file():
        print(f"Live policy absent: {live_path}")
        return 1
    public = (ROOT / ".claude/CLAUDE.md").read_text()
    overlay_file = overlay_path(home)
    if overlay_file.is_symlink():
        print("Refusing symlinked private overlay.")
        return 2
    if overlay_file.exists():
        info = overlay_file.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            print("Private overlay must be a regular file owned by you with mode 0600.")
            return 2
    overlay = overlay_file.read_text() if overlay_file.is_file() else ""
    covered = {h for _, _, h in clauses(public)} | {h for _, _, h in clauses(overlay)}
    live = live_path.read_text()
    missing = [(n, k, h) for n, k, h in clauses(live) if h not in covered]
    ledgered, conflicts = set(), []
    ledger_file = Path(args.ledger) if args.ledger else None
    if ledger_file is None:
        spec = importlib.util.spec_from_file_location("ledger", ROOT / "scripts/spe108-live/ledger.py")
        ledger_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger_mod)
        candidate = ledger_mod.default_path(home)
        ledger_file = candidate if candidate.is_file() else None
    if ledger_file is not None:
        spec = importlib.util.spec_from_file_location("ledger", ROOT / "scripts/spe108-live/ledger.py")
        ledger_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger_mod)
        try:
            rows = ledger_mod.load(ledger_file)
        except ledger_mod.LedgerError as error:
            print(f"Policy ledger refused: {error}")
            return 2
        live_lines = live.splitlines()
        # A clause is dispositioned when its own line is ledgered and not an open conflict.
        ledgered = {n for n, _, _ in missing
                    if rows.get(ledger_mod.line_key(live_lines[n - 1]), (None,))[0] not in (None, ledger_mod.CONFLICT)}
        conflicts = [n for n, line in ledger_mod.content_lines(live)
                     if rows.get(ledger_mod.line_key(line), (None,))[0] == ledger_mod.CONFLICT]
        missing = [m for m in missing if m[0] not in ledgered]
        for number in conflicts:
            print(f"OPEN CONFLICT live line {number} (owner decision required; ledger disposition REAL_CONFLICT)")
    for number, kind, short in missing:
        print(f"UNTRACKED live {kind} at line {number} sha256:{short} (in neither public base nor private overlay)")
    live_folded = live.lower()
    keyword_drift = 0
    for group in KEYWORDS.findall(overlay):
        for keyword in (k.strip() for k in group.split(",") if k.strip()):
            if keyword.lower() not in live_folded:
                keyword_drift += 1
                print(f"MISSING overlay keyword sha256:{digest('keyword', keyword)} absent from the live policy")
    print(f"Claude clause drift: {len(missing) + keyword_drift + len(conflicts)} "
          f"(overlay {'present' if overlay_file.is_file() else 'absent'}; {len(ledgered)} dispositioned by ledger; "
          f"clause text not shown)")
    return int(bool(missing or keyword_drift or conflicts))


if __name__ == "__main__":
    sys.exit(main())
