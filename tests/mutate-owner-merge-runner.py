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
    ("pre-exec full re-evaluation removed", '            observed2, class2, checks2, rec["server_side"], _ = evaluate(runner, repo, pr, head, me, cfg, evidence, key, clock, pin)', '            observed2, class2, checks2 = head, rec["class"], rec["checks"]'),
    ("pre-exec class compare", 'or class2 != rec["class"]', 'or False'),
    ("pre-exec head compare", 'if observed2 != head', 'if False'),
    ("server protection check skipped", '        info = check_server_protection(runner, repo, base, cfg)', '        info = {"contexts": []}'),
    ("server rules type filter", '        if r.get("type") != "required_status_checks":', '        if False:'),
    ("server 404 handling", '    elif http_status(err) == "404":', '    elif False:'),
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
    ("final read full decision", '    final_class, _ = decide(after, repo, pr, head, me, cfg, exempt_after)', '    final_class = pr_class'),
    ("final read class compare", '    if final_class != pr_class:', '    if False:'),
    ("base-mismatch exit code", 'rec["outcome"], code = "UNVERIFIED", EXIT_BASE_MISMATCH', 'rec["outcome"], code = "UNVERIFIED", EXIT_UNVERIFIED'),
    ("base-mismatch audit marker", '                    rec["incident"] = INCIDENT\n', ''),
    ("base-mismatch stderr marker", '                    sys.stderr.write(f"INCIDENT: {INCIDENT}', '                    (lambda *a: None)(f"INCIDENT: {INCIDENT}'),
    ("base-mismatch incident branch", '                elif merged:\n', '                elif False:\n'),
    ("check status", '            if v != "SUCCESS" and name not in exempt:', '            if False:'),
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
    ("evidence reviewed_at format", '    if not isinstance(value, str) or not TS_RE.fullmatch(value):', '    if False:'),
    ("evidence future", '    if ts > now + 60:\n        return [f"{label} is in the future"]', '    if False:\n        return []'),
    ("evidence max age", '    if now - ts > cfg["max_evidence_age_hours"] * 3600:', '    if False:'),
    ("evidence required fields", '        if not isinstance(ev.get(k), str) or not ev.get(k):', '        if False:'),
    ("protection 404 = any mention of 404", '    elif http_status(err) == "404":', '    elif "404" in err:'),
    ("protection 404 = last status token", 'HTTP_TOKEN_RE = re.compile(r"HTTP ([^\\s:)]*)")', 'HTTP_TOKEN_RE = re.compile(r"HTTP ([^\\s:)]*)(?!.*HTTP )")'),
    ("status token completeness", 'return m.group(1) if m and STATUS_RE.fullmatch(m.group(1)) else None', 'return m.group(1)[:3] if m and m.group(1)[:3].isdigit() else None'),
    ("status token charset", 'HTTP_TOKEN_RE = re.compile(r"HTTP ([^\\s:)]*)")', 'HTTP_TOKEN_RE = re.compile(r"HTTP (\\d{3})")'),
    ("waiver label: bypass none when waivers", '    if waivers:\n        bypass =', '    if False:\n        bypass ='),
    ("waiver label: enforce_admins false-accepted", '            admins = "false-accepted"\n            waivers.append("enforce_admins_false")', '            pass'),
    ("waiver label: protection unreadable", '        admins = "unverified-accepted"\n        waivers.append("protection_unreadable")', '        pass'),
    ("waiver label: actors present", '        waivers.append("bypass_actors_present:" + ",".join(sorted(set(risky))))', '        pass'),
    ("waiver label: bypass unreadable", '        waivers.append("bypass_unreadable")', '        pass'),
    ("signature shape before compare", ' or not MAC_RE.fullmatch(mac) \\\n', ' \\\n'),
    ("review element validation", '        if not isinstance(r, dict) or not isinstance(r.get("author"), dict) or not isinstance(r.get("state"), str) \\', '        if False \\'),
    ("review commit type", ' or not (r.get("commit") is None or isinstance(r.get("commit"), dict)):', ':'),
    ("generic exception handler", '    except Exception as e:  # fail closed', '    except ZeroDivisionError as e:  # fail closed'),
    ("generic handler exit code", 'rec["outcome"], code = "ERROR", EXIT_INTERNAL', 'rec["outcome"], code = "ERROR", EXIT_ERROR'),
    ("merge-attempted note", '        if rec.get("merge_attempted"):', '        if False:'),
    ("merge-attempted flag", '            rec["merge_attempted"] = True\n', ''),
    ("redact: authorization value to end of line", '|(?i:\\bauthorization)\\s*[=:][^\\r\\n]*"', '|(?i:\\bauthorization)\\s*[=:]\\s*\\S+"'),
    ("redact: basic/bearer scheme", '|(?i:\\b(?:bearer|basic)\\s+)[^\\s\\"\']+"', '|(?i:\\bbearer\\s+)\\S"'),
    ("redact: quoted values", '(?:\\"[^\\"\\r\\n]*\\"|\'[^\'\\r\\n]*\'|[^\\r\\n]*)")', '\\S+")'),
    ("redact: key names", 'token|password|passwd|secret|api[_-]?key|apikey|access[_-]?token', 'token|password'),
    ("FIFO: allowlist O_NONBLOCK", 'os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO must not hang the open', 'os.O_RDONLY | os.O_NOFOLLOW)  # a FIFO must not hang the open'),
    ("FIFO: evidence O_NONBLOCK", 'fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # a FIFO or device', 'fd = os.open(path, os.O_RDONLY)  # a FIFO or device'),
    ("FIFO: evidence regular-file check", '            if not stat.S_ISREG(os.fstat(fd).st_mode):\n                raise Refused(["review evidence is not a regular file"])', '            pass'),
    ("FIFO: audit O_NONBLOCK", ' | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)', ' | os.O_NOFOLLOW, 0o600)'),
    ("gh_error returns raw stderr", '    return f"gh failure stage={stage} status={status or \'unknown\'} category={category}"', '    return str(err)'),
    ("gh_error excerpt reintroduced", '    return f"gh failure stage={stage} status={status or \'unknown\'} category={category}"', "    return f\"gh failure stage={stage} status={status or 'unknown'} category={category} excerpt={scrub(err)[:60]}\""),
    ("gh_error digest reintroduced", '    return f"gh failure stage={stage} status={status or \'unknown\'} category={category}"', "    return f\"gh failure stage={stage} status={status or 'unknown'} category={category} sha256={hashlib.sha256(str(err).encode()).hexdigest()[:16]}\""),
    ("gh_error category mapping", '    category = ("denied" if status in ("401", "403")', '    category = ("denied" if False'),
    ("merge stderr stored (scrubbed only)", 'rec["reasons"].append(gh_error(merr, "merge"))', 'rec["reasons"].append(scrub(merr))'),
    ("pr-view stderr stored (scrubbed only)", 'raise RuntimeError(gh_error(err, "pr-view"))', 'raise RuntimeError(scrub(err))'),
    ("rules-read stderr stored (scrubbed only)", 'raise RuntimeError(gh_error(err, "rules-read"))', 'raise RuntimeError(scrub(err))'),
    ("protection-read stderr stored (scrubbed only)", 'raise RuntimeError(gh_error(err, "protection-read"))', 'raise RuntimeError(scrub(err))'),
    ("evidence signature compare", 'or not hmac.compare_digest(mac, evidence_mac(key, ev)):', 'or False:'),
    ("evidence signature key required", '        if key is None or not isinstance(mac, str)', '        if not isinstance(mac, str)'),
    ("evidence mac excludes hmac field", 'if k != "hmac"}', 'if True}'),
    ("key file mode", 'read_trusted_file(path, "evidence key file", 0o077)', 'read_trusted_file(path, "evidence key file", 0o022)'),
    ("key length", '    if len(key) < MIN_KEY_BYTES or len(key) > 4096:', '    if False:'),
    ("trusted file O_NOFOLLOW", 'os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO must not hang the open', 'os.O_RDONLY | os.O_NONBLOCK)  # a FIFO must not hang the open'),
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
    ("audit O_NOFOLLOW", ' | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)', ' | os.O_NONBLOCK, 0o600)'),
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
    ("lci: exception gated on allowlist flag", 'if local is None or not cfg["local_equivalent_ci"]:', 'if local is None:'),
    ("lci: local evidence must list the check", '        if name not in local:\n            continue', '        if False:\n            continue'),
    ("lci: only not-started verdicts", 'ok = ok and verdict in NOT_EXECUTED_VERDICTS and m is not None', 'ok = ok and m is not None'),
    ("lci: details url must be a job", ' and m is not None and m.group(1).lower()', ' and m.group(1).lower()'),
    ("lci: details url repo must match", 'm.group(1).lower() == repo.lower() \\', 'True \\'),
    ("lci: job must have no steps", 'and job.get("steps") == [] and', 'and'),
    ("lci: job must have no runner", 'and job.get("runner_id") in (None, 0) and not job.get("runner_name"))', ')'),
    ("lci: job head pinned", 'job.get("head_sha") == head and', ''),
    ("lci: job name pinned", 'job.get("name") == name and', ''),
    ("lci: job completed", ' and job.get("status") == "completed"', ''),
    ("lci: StatusContext never exempt", 'if c.get("__typename") == "CheckRun" else None', 'if True else None'),
    ("lci: availability recheck on final read", '    if exempt_after != exempt:', '    if False:'),
    ("lci: exempted check labelled CI_PASS", '"CI_UNAVAILABLE_EXTERNAL" if exempt else "CI_PASS"', '"CI_PASS"'),
    ("lci: local head pinned", '    if lc["head"] != head:', '    if False:'),
    ("lci: local exit code", '        if e["exit_code"] != 0 or e["tests"] < 0:', '        if False:'),
    ("lci: local classification", '    if lc["classification"] != "LOCAL_EQUIVALENT_PASS":', '    if False:'),
    ("lci: local freshness", '    bad += fresh_ts(lc["run_at"], "local_ci run_at", cfg, now)', '    pass'),
    ("lci: local key set exact", 'set(lc) != LOCAL_CI_KEYS', 'False'),
    ("lci: entry key set exact", 'set(e) == LOCAL_CI_CHECK_KEYS and', ''),
    ("lci: entry ints not bool", 'all(isinstance(e[k], int) and not isinstance(e[k], bool) for k in ("exit_code", "tests"))', 'True'),
    ("lci: duplicate names", '        if e["name"] in out:', '        if False:'),
    ("lci: prepass validates other facts first", '    decide(facts, repo, pr, head, me, cfg, frozenset(cfg["required_checks"]) if cfg["local_equivalent_ci"] else frozenset())\n', ''),
    ("lci: config flag is boolean", 'for x in (prot, bypass, signed, lci))', 'for x in (prot, bypass, signed))'),
    ("lci: job argv shape", 'JOB_PATH_RE.fullmatch(a[1])', 'None'),
    ("lci: all runs of the name are Actions app", 'r["app"].get("slug") == "github-actions" for r in runs)', 'True for r in runs)'),
    ("lci: check-runs listing complete", 'data.get("total_count") != len(runs) or ', ''),
    ("lci: check-runs head pinned", 'r.get("name") == name and r.get("head_sha") == head', 'r.get("name") == name'),
    ("lci: actions-only gate used", '        ok = only_actions_runs(runner, repo, head, name)', '        ok = True'),
    ("lci: second evaluation checks compared", '        if checks2 != rec["checks"]:', '        if False:'),
    ("lci: check-runs argv shape", 'CHECK_RUNS_RE.fullmatch(a[1])', 'None'),
    ("ind: requirement enforced", '    if ind and not any(who in ind', '    if False and not any(who in ind'),
    ("ind: not author or merging user", 'who in ind and who not in (a, me_l) and', 'who in ind and'),
    ("ind: exact head", 'st == "APPROVED" and oid == head and assoc in TRUSTED_ASSOC\n                       for who', 'st == "APPROVED" and assoc in TRUSTED_ASSOC\n                       for who'),
    ("ind: trusted association", 'st == "APPROVED" and oid == head and assoc in TRUSTED_ASSOC\n                       for who', 'st == "APPROVED" and oid == head\n                       for who'),
    ("ind: login validated in config", 'all(isinstance(x, str) and LOGIN_RE.fullmatch(x) for x in ind)', 'True'),
    ("ind: logins lowercased", '"independent_github_reviewers": [x.lower() for x in ind], "protection_capability": cap}', '"independent_github_reviewers": list(ind), "protection_capability": cap}'),
    ("pf: plan-limit text required", 'return http_status(err) == "403" and PLAN_LIMIT_RE.search(err) is not None', 'return http_status(err) == "403"'),
    ("pf: status 403 required", 'return http_status(err) == "403" and PLAN_LIMIT_RE', 'return PLAN_LIMIT_RE.search(err) is not None and PLAN_LIMIT_RE'),
    ("pf: both reads must be limited", '    if rc1 != 0 and rc2 != 0 and plan_limited(err1) and plan_limited(err2):', '    if rc1 != 0 and plan_limited(err1):'),
    ("pf: fall back to strict check", 'check_private_free(runner, repo, base) or check_server_protection(runner, repo, base, cfg)', 'check_private_free(runner, repo, base) or {"capability": "private-free-app-level"}'),
    ("pf: opt-in required", '    if cfg["protection_capability"] == "private-free-app-level":\n        info =', '    if True:\n        info ='),
    ("pf: capability validated", 'cap in CAPABILITIES and not', 'not'),
    ("pf: conflicting waiver refused", 'and not (cap == "private-free-app-level" and not prot)', ''),
    ("pf: guarantees recorded", '        rec["guarantees"] = {', '        rec["guarantees_off"] = {'),
    ("pf: review label honest", '"local-evidence-attestation (not a GitHub approval)"', '"github-independent-approval"'),
    ("pf: server backing compared across passes", '        if server_kind(rec["server_side"]) != kind:', '        if False:'),
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
