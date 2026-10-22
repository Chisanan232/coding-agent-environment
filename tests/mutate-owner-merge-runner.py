#!/usr/bin/env python3
"""Mutation check for scripts/owner-merge-runner.py (SPE-118). Not part of the all-tests loop (name does not
match test-*.py): each mutant removes or weakens one guard in a scratch copy and the unit-test suite must fail.

  python3 tests/mutate-owner-merge-runner.py        # exit 0 only if every mutant is killed

A hang counts as a kill. Deterministic: no network, no real gh, no settings."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = "scripts/owner-merge-runner.py"
ALLOW_FD = "        if {}:\n            bad.append(f\"{{label}} is"
MUTANTS = [
    ("head pin", '    if observed != head:\n        bad.append("observed head differs from expected head")', '    if False:\n        bad.append("observed head differs from expected head")'),
    ("pre-exec full re-evaluation removed", '            observed2, class2, _, rec["server_side"], _ = evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock, pin)', '            observed2, class2 = head, rec["class"]'),
    ("pre-exec class compare", 'or class2 != rec["class"]', 'or False'),
    ("pre-exec head compare", 'if observed2 != head', 'if False'),
    ("server protection check skipped", '        info = check_server_protection(runner, repo, base, cfg)', '        info = {"contexts": []}'),
    ("server rules type filter", '        if r.get("type") != "required_status_checks":', '        if False:'),
    ("server 404 handling", '    elif re.search(r"404|Not Found", err):', '    elif False:'),
    ("server set non-empty", '    if not names:\n        if prot_state', '    if False:\n        if prot_state'),
    ("server covers required checks", '    if missing:', '    if False:'),
    ("server empty rule list", ' or not lst or ', ' or '),
    ("server rule id validation", 'isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0', 'False'),
    ("server rule context validation", 'or not all(isinstance(c, dict) and isinstance(c.get("context"), str) and c["context"] for c in lst)', ''),
    ("protection empty set", '    if not names:\n        raise Refused(["branch protection required_status_checks is empty"])', '    pass'),
    ("protection checks[] context", ' | {c["context"] for c in checks}', ''),
    ("protection contexts[]", 'names = set(ctx) |', 'names = set() |'),
    ("protection malformed", '    if not isinstance(ctx, list) or not isinstance(checks, list) \\', '    if False \\'),
    ("bypass refusal", '    if risky and not cfg["accept_bypass_actors"]:', '    if False:'),
    ("bypass: only some actor types count", 'risky += [str(a.get("actor_type")) for a in actors]', 'risky += [str(a.get("actor_type")) for a in actors if a.get("actor_type") in ("Team", "OrganizationAdmin")]'),
    ("enforce_admins required", '        if not admins and not cfg["accept_bypass_actors"]:', '        if False:'),
    ("enforce_admins strict true", 'admins = isinstance(ea, dict) and ea.get("enabled") is True', 'admins = bool(ea)'),
    ("base pin on later pass", '    if pin is not None and base != pin["base"]:', '    if False:'),
    ("base re-read after protection", '    if after.get("baseRefName") != base or after.get("headRefOid") != head:', '    if False:'),
    ("evidence byte-equality across passes", '    if pin is not None and digest != pin["evidence"]:', '    if False:'),
    ("protection read for pinned base", 'info = check_server_protection(runner, repo, base, cfg)  # for the pinned base', 'info = check_server_protection(runner, repo, "main", cfg)'),
    ("read-back base confirmed", '                if merged and back.get("baseRefName") == pin["base"]:', '                if merged:'),
    ("bypass unreadable recorded", '"unreadable-accepted" if unreadable', '"none" if unreadable'),
    ("bypass unreadable refuses", '    if unreadable and not cfg["accept_bypass_actors"]:', '    if False:'),
    ("bypass metadata shape check", 'if rc != 0 or not isinstance(actors, list) or not all(isinstance(a, dict) for a in actors):', 'if rc != 0:'),
    ("protection unreadable refuses", '    if prot_state == "unreadable" and not cfg["accept_bypass_actors"]:', '    if False:'),
    ("final read full decision", '    final_class, _ = decide(after, repo, pr, head, me, cfg)', '    final_class = pr_class'),
    ("final read class compare", '    if final_class != pr_class:', '    if False:'),
    ("base-mismatch exit code", 'rec["outcome"], code = "UNVERIFIED", EXIT_BASE_MISMATCH', 'rec["outcome"], code = "UNVERIFIED", EXIT_UNVERIFIED'),
    ("base-mismatch audit marker", '                    rec["incident"] = INCIDENT\n', ''),
    ("base-mismatch stderr marker", '                    sys.stderr.write(f"INCIDENT: {INCIDENT}', '                    (lambda *a: None)(f"INCIDENT: {INCIDENT}'),
    ("base-mismatch incident branch", '                elif merged:\n', '                elif False:\n'),
    ("check status", '            if v != "SUCCESS":', '            if False:'),
    ("required check presence", '        if name not in seen:', '        if False:'),
    ("malformed entry name", 'if not isinstance(name, str) or not name or not isinstance(verdict, str) or not verdict:', 'if False:'),
    ("malformed entry status", '        if not isinstance(status, str) or not status:', '        if False:'),
    ("allowlist empty required_checks", 'ok = ok and isinstance(v, list) and bool(v) and', 'ok = ok and isinstance(v, list) and'),
    ("allowlist reviewers required", '    for key in ("required_checks", "reviewers"):', '    for key in ("required_checks",):'),
    ("allowlist age bound", 'and 1 <= age <= MAX_EVIDENCE_AGE_HOURS', 'and True'),
    ("allowlist signed needs key", '    ok = ok and not (signed and key_file is None)\n', ''),
    ("allowlist signed default", 'cfg.get("require_signed_evidence", key_file is not None)', 'cfg.get("require_signed_evidence", False)'),
    ("argv: merge_argv adds --admin", '"--merge", "--match-head-commit", sha]', '"--merge", "--admin", "--match-head-commit", sha]'),
    ("argv: check_argv disabled", '    raise RuntimeError("refused gh argv outside the fixed shapes")', '    return "merge"'),
    ("mergeStateStatus", '    if mss != "CLEAN":', '    if False:'),
    ("changes requested", '    if decision == "CHANGES_REQUESTED" or any(v[0] == "CHANGES_REQUESTED" for v in latest.values()):', '    if False:'),
    ("draft", '    if draft:', '    if False:'),
    ("syntax validation", '    if bad:\n        raise Refused(bad)\n\n\ndef in_git_worktree', '    if False:\n        raise Refused(bad)\n\n\ndef in_git_worktree'),
    ("read-back verified", 'merged = back.get("state") == "MERGED" and', 'merged = True or'),
    ("secret scrub", '    return SECRET_RE.sub("[redacted]", str(text))[:300]', '    return str(text)[:300]'),
    ("class C association", 'and assoc in TRUSTED_ASSOC', 'and True'),
    ("class C cross-repo", '        if cross and not mine:', '        if False:'),
    ("evidence repo bound", '    if ev["repo"].lower() != repo.lower():', '    if False:'),
    ("evidence pr bound", 'str(pr_v) != pr:', 'False:'),
    ("evidence base bound", '    if ev["base"] != base:', '    if False:'),
    ("evidence head bound", '    if ev["head"] != head:', '    if False:'),
    ("evidence verdict", '    if ev["verdict"] != "PASS":', '    if False:'),
    ("evidence reviewer allowlisted", '    if ev["reviewer"] not in cfg["reviewers"]:', '    if False:'),
    ("evidence reviewer_kind", '    if ev["reviewer_kind"] not in REVIEWER_KINDS:', '    if False:'),
    ("evidence reviewed_at format", '    if not TS_RE.fullmatch(ev["reviewed_at"]):', '    if False:'),
    ("evidence future", '            if ts > now + 60:', '            if False:'),
    ("evidence max age", '            elif now - ts > cfg["max_evidence_age_hours"] * 3600:', '            elif False:'),
    ("evidence required fields", '        if not isinstance(ev.get(k), str) or not ev.get(k):', '        if False:'),
    ("evidence signature check", '        if key is None or not isinstance(mac, str) or not hmac.compare_digest(mac, evidence_mac(key, ev)):', '        if False:'),
    ("evidence mac excludes hmac field", 'if k != "hmac"}', 'if True}'),
    ("key file mode", 'read_trusted_file(path, "evidence key file", 0o077)', 'read_trusted_file(path, "evidence key file", 0o022)'),
    ("key length", '    if len(key) < MIN_KEY_BYTES or len(key) > 4096:', '    if False:'),
    ("trusted file O_NOFOLLOW", 'fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)', 'fd = os.open(path, os.O_RDONLY)'),
    ("trusted file owner", '        if st.st_uid != os.geteuid():\n            bad.append(f"{label} is not owned', '        if False:\n            bad.append(f"{label} is not owned'),
    ("trusted file mode", '        if st.st_mode & loose_mask:', '        if False:'),
    ("trusted file regular", '        if not stat.S_ISREG(st.st_mode):\n            bad.append(f"{label} is not a regular', '        if False:\n            bad.append(f"{label} is not a regular'),
    ("trusted file worktree", '        if in_git_worktree(path):', '        if False:'),
    ("ancestor owner", '        if st.st_uid not in (0, os.geteuid()):', '        if False:'),
    ("ancestor writable", '        if st.st_mode & 0o022 and not', '        if False and not'),
    ("ancestor sticky exception widened", 'st.st_mode & stat.S_ISVTX and st.st_uid == 0', 'True'),
    ("ancestor symlink realpath", '    if os.path.realpath(cur) != cur:', '    if False:'),
    ("ancestor walk to /", '        if parent == cur:\n            return bad', '        if True:\n            return bad'),
    ("default allowlist path in repo", 'DEFAULT_ALLOWLIST = os.path.join(os.path.expanduser("~"), ".claude", "owner-merge-runner-allowlist.json")', 'DEFAULT_ALLOWLIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "x.json")'),
    ("audit O_NOFOLLOW", ' | os.O_NOFOLLOW, 0o600)', ', 0o600)'),
    ("audit nlink", ' or st.st_nlink != 1', ''),
    ("audit mode 0600", ' or stat.S_IMODE(st.st_mode) != 0o600', ''),
    ("audit owner", 'or st.st_uid != os.geteuid() or st.st_nlink', 'or st.st_nlink'),
    ("audit dir trust", '        if bad:\n            raise AuditError("audit directory untrusted', '        if False:\n            raise AuditError("audit directory untrusted'),
    ("audit fsync", '        os.fsync(fd)\n', ''),
    ("audit write loop", '    while total < len(data):\n        n = os.write(fd, view[total:])', '    while total < 1:\n        n = os.write(fd, view[total:])'),
    ("audit zero progress", '        if n <= 0:\n            raise AuditError("audit write made no progress")', '        if False:\n            raise AuditError("x")'),
    ("audit length check", '    if total != len(data):\n        raise AuditError("audit write length mismatch")\n', ''),
    ("PENDING failure refuses", 'raise Refused(["audit record could not be written, refusing before execution: " + scrub(e)])', 'pass'),
    ("final audit failure exit code", '        code = EXIT_AUDIT', '        pass'),
    ("final audit stderr", '        sys.stderr.write(f"error: final audit record', '        pass  # f"error: final audit record'),
]


def main():
    survivors = []
    for name, old, new in MUTANTS:
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            ignore = shutil.ignore_patterns("__pycache__")
            shutil.copytree(ROOT / "scripts", d / "scripts", ignore=ignore)
            shutil.copytree(ROOT / "tests", d / "tests", ignore=ignore)
            p = d / RUNNER
            src = p.read_text()
            if old not in src:
                print(f"STALE     {name}: pattern not found", flush=True)
                survivors.append(name)
                continue
            p.write_text(src.replace(old, new, 1))
            try:
                rc = subprocess.run([sys.executable, str(d / "tests/test-owner-merge-runner.py")],
                                    capture_output=True, text=True, timeout=40).returncode
            except subprocess.TimeoutExpired:
                rc = 1
            print(f"{'KILLED  ' if rc else 'SURVIVED'}  {name}", flush=True)
            if not rc:
                survivors.append(name)
    print(f"{len(MUTANTS) - len(survivors)} of {len(MUTANTS)} killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
