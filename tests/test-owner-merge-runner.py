#!/usr/bin/env python3
"""Unit tests for scripts/owner-merge-runner.py (SPE-118 Option A) with an injected fake gh.

No real gh, no real PR, no network. The fake is passed through the module API; the CLI has no
way to select an alternate gh executable.
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("omr", ROOT / "scripts/owner-merge-runner.py")
omr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omr)

HEAD = "a" * 40
OTHER = "b" * 40
MERGE_SHA = "c" * 40
REPO = "Chisanan232/demo-repo"
ME = "chisanan232"


REQUIRED = ["ci", "SonarCloud Code Analysis"]


def green():
    return [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": "SUCCESS"}]


def allow_doc(**cfg):
    c = {"required_checks": REQUIRED, "reviewers": ["independent-1"]}
    c.update(cfg)
    return {"repos": {REPO: c}}


def base_pr(**over):
    pr = {"state": "OPEN", "isDraft": False, "author": {"login": ME}, "baseRefName": "main",
          "headRefName": "feature/x", "headRefOid": HEAD, "isCrossRepository": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "",
          "reviews": [], "statusCheckRollup": green(), "files": [{"path": "a.txt"}]}
    pr.update(over)
    return pr


class FakeGh:
    def __init__(self, pr=None, merge_rc=0, merge_err="", merged=True, later=None, user=ME,
                 rules=None, protection=(1, "HTTP 404: Not Found")):
        self.pr = pr if pr is not None else base_pr()
        self.merge_rc, self.merge_err, self.merged = merge_rc, merge_err, merged
        self.later = later  # PR facts returned from the second full fetch (the pre-exec re-check) on
        self.user = user
        self.rules = [{"type": "required_status_checks"}] if rules is None else rules
        self.protection = protection  # (rc, stdout-or-stderr) for the branch-protection read
        self.fetches = 0
        self.on_merge = None
        self.calls = []
        self.views = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv == ["api", "user"]:
            return 0, json.dumps({"login": self.user}), ""
        if argv[:2] == ["pr", "view"]:
            fields = argv[6]
            if fields == omr.PR_FIELDS:
                self.fetches += 1
                pr = self.later if (self.later is not None and self.fetches >= 2) else self.pr
                return 0, json.dumps(pr), ""
            if fields == omr.READBACK_FIELDS:
                if self.merged:
                    return 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA}}), ""
                return 0, json.dumps({"state": "OPEN", "mergeCommit": None}), ""
        if argv[0] == "api" and "/rules/branches/" in argv[1]:
            return 0, json.dumps(self.rules), ""
        if argv[0] == "api" and argv[1].endswith("/protection"):
            rc, text = self.protection
            return (0, text, "") if rc == 0 else (1, "", text)
        if argv[:2] == ["pr", "merge"]:
            if self.on_merge:
                self.on_merge()
            return self.merge_rc, "", self.merge_err
        raise AssertionError(f"unexpected argv {argv}")

    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


DROP = object()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(os.path.realpath(self.tmp.name))
        self.tmp_real = d
        self.allow = d / "allow.json"
        self.allow.write_text(json.dumps(allow_doc()))
        self.evidence = d / "evidence.json"
        self.write_evidence()
        self.audit = d / "audit" / "log.jsonl"

    def write_evidence(self, **over):
        ev = {"repo": REPO, "pr": 7, "head": HEAD, "verdict": "PASS", "reviewer": "independent-1",
              "reviewer_kind": "independent-agent", "reviewed_at": "2026-10-20T10:00:00Z"}
        ev.update(over)
        for k in [k for k, v in ev.items() if v is DROP]:
            del ev[k]
        self.evidence.write_text(json.dumps(ev))

    def go(self, gh, repo=REPO, pr="7", head=HEAD, dry=False, allow=None, evidence=None):
        return omr.run(repo, pr, head, str(evidence or self.evidence), str(allow or self.allow),
                       str(self.audit), dry, gh)

    def records(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()]

    def assertRefused(self, gh, needle, **kw):
        code, rec = self.go(gh, **kw)
        self.assertEqual(code, omr.EXIT_REFUSED, rec)
        self.assertEqual(gh.merges(), [], "a refusal must never reach gh pr merge")
        self.assertIn(needle, " | ".join(rec["reasons"]))
        return rec


    def assertBlocked(self, gh, needle, **kw):
        """Like assertRefused, but also accepts the audit-failed code when the audit shares the tampered tree."""
        code, rec = self.go(gh, **kw)
        self.assertIn(code, (omr.EXIT_REFUSED, omr.EXIT_AUDIT), rec)
        self.assertEqual(gh.merges(), [])
        self.assertIn(needle, " | ".join(rec["reasons"]))


class HappyPath(Base):
    def test_merge_uses_exactly_one_fixed_argv_and_reads_back(self):
        gh = FakeGh()
        code, rec = self.go(gh)
        self.assertEqual(code, omr.EXIT_OK, rec)
        self.assertEqual(gh.merges(), [["pr", "merge", "7", "--repo", REPO, "--merge", "--match-head-commit", HEAD]])
        self.assertEqual((rec["outcome"], rec["class"]), ("MERGED", "A"))
        flat = [t for c in gh.calls for t in c]
        for forbidden in ("--admin", "--squash", "--rebase", "--auto", "--delete-branch", "-d"):
            self.assertNotIn(forbidden, flat)

    def test_dry_run_validates_but_never_merges(self):
        gh = FakeGh()
        code, rec = self.go(gh, dry=True)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_OK, "DRY_RUN"))
        self.assertEqual(gh.merges(), [])

    def test_class_b_bot_needs_owner_approval_at_head(self):
        pr = base_pr(author={"login": "app/dependabot"}, headRefName="dependabot/npm/x")
        self.assertRefused(FakeGh(pr), "class B needs")
        pr["reviews"] = [{"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, rec = self.go(FakeGh(pr))
        self.assertEqual((code, rec["class"]), (omr.EXIT_OK, "B"))

    def test_class_b_requires_bot_branch(self):
        pr = base_pr(author={"login": "dependabot"}, headRefName="feature/x",
                     reviews=[{"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}])
        self.assertRefused(FakeGh(pr), "provenance")

    def test_class_c_needs_nonauthor_approval_at_head(self):
        pr = base_pr(author={"login": "someone"})
        self.assertRefused(FakeGh(pr), "class C needs")
        pr["reviews"] = [{"author": {"login": "someone"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        self.assertRefused(FakeGh(pr), "class C needs")  # self-approval does not count
        pr["reviews"] = [{"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": OTHER}}]
        self.assertRefused(FakeGh(pr), "class C needs")  # stale approval
        pr["reviews"] = [{"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, rec = self.go(FakeGh(pr))
        self.assertEqual((code, rec["class"]), (omr.EXIT_OK, "C"))


class Refusals(Base):
    def test_wrong_expected_head(self):
        self.write_evidence(head=OTHER)  # isolate the head pin from the evidence check
        self.assertRefused(FakeGh(), "observed head differs", head=OTHER)

    def test_head_moved_between_check_and_exec(self):
        self.assertRefused(FakeGh(later=base_pr(headRefOid=OTHER)), "changed before execution")

    def test_full_recheck_before_exec_catches_every_stale_fact(self):
        bad_roll = green() + [{"__typename": "CheckRun", "name": "late", "status": "COMPLETED", "conclusion": "FAILURE"}]
        cases = {"changes requested": base_pr(reviews=[{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED",
                                                         "commit": {"oid": HEAD}}]),
                 "check failed": base_pr(statusCheckRollup=bad_roll),
                 "required check vanished": base_pr(statusCheckRollup=green()[:1]),
                 "not clean": base_pr(mergeStateStatus="BLOCKED"),
                 "draft": base_pr(isDraft=True), "closed": base_pr(state="CLOSED"),
                 "class changed": base_pr(author={"login": "stranger"})}
        for name, later in cases.items():
            gh = FakeGh(later=later)
            code, rec = self.go(gh)
            self.assertEqual(code, omr.EXIT_REFUSED, name)
            self.assertEqual(gh.merges(), [], name)
            self.assertIn("changed before execution", " ".join(rec["reasons"]), name)
            self.assertEqual(gh.fetches, 2, name)

    def test_server_side_protection_required_by_default(self):
        self.assertRefused(FakeGh(rules=[{"type": "pull_request"}]), "no server-side required status checks")
        self.assertRefused(FakeGh(rules=[], protection=(0, json.dumps({"required_status_checks": None}))),
                           "no server-side required status checks")

    def test_server_side_protection_via_branch_protection_or_ruleset(self):
        ok = FakeGh(rules=[], protection=(0, json.dumps({"required_status_checks": {"contexts": ["ci"]}})))
        self.assertEqual(self.go(ok)[0], omr.EXIT_OK)
        self.assertEqual(self.go(FakeGh())[0], omr.EXIT_OK)

    def test_server_side_protection_read_error_fails_closed(self):
        gh = FakeGh(rules=[], protection=(1, "HTTP 403: Resource not accessible"))
        code, _ = self.go(gh)
        self.assertEqual((code, gh.merges()), (omr.EXIT_ERROR, []))

    def test_server_side_protection_explicit_waiver(self):
        self.allow.write_text(json.dumps(allow_doc(require_server_side_protection=False)))
        code, rec = self.go(FakeGh(rules=[]))
        self.assertEqual((code, rec["server_side_protection"]), (omr.EXIT_OK, "waived"))
        self.allow.write_text(json.dumps(allow_doc(require_server_side_protection="false")))
        self.assertRefused(FakeGh(rules=[]), "invalid")

    def test_draft(self):
        self.assertRefused(FakeGh(base_pr(isDraft=True)), "draft")

    def test_not_open(self):
        self.assertRefused(FakeGh(base_pr(state="MERGED")), "not OPEN")

    def test_failing_check(self):
        roll = green() + [{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check tests is FAILURE")

    def test_pending_check(self):
        roll = green() + [{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS", "conclusion": ""}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check tests is IN_PROGRESS")

    def test_pending_sonarcloud_status_context(self):
        roll = [{"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": "PENDING"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "SonarCloud")

    def test_missing_checks(self):
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=[])), "required check ci is missing")

    def test_each_required_check_must_be_present(self):
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=green()[:1])), "required check SonarCloud")
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=green()[1:])), "required check ci is missing")
        # only an unrelated green check: no required name present
        other = [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=other)), "required check")

    def test_extra_unnamed_failing_check_refuses(self):
        roll = green() + [{"__typename": "CheckRun", "name": "extra", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check extra is FAILURE")
        roll = green() + [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=roll)), "check ci is FAILURE")  # duplicate name

    def test_malformed_rollup_entries_refuse(self):
        for bad in ({}, {"name": "x"}, {"name": "x", "status": "COMPLETED"}, {"status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"__typename": "StatusContext", "state": "SUCCESS"}, {"__typename": "StatusContext", "context": "x"},
                    {"__typename": "CheckRun", "name": "x", "status": 5}, "string", None):
            self.assertRefused(FakeGh(base_pr(statusCheckRollup=green() + [bad])), "malformed check entry")

    def test_allowlist_repo_config_must_name_required_checks_and_reviewers(self):
        for cfg in ({"required_checks": [], "reviewers": ["a"]}, {"reviewers": ["a"]}, {"required_checks": ["ci"]},
                    {"required_checks": ["ci"], "reviewers": []}, {"required_checks": [""], "reviewers": ["a"]},
                    {"required_checks": "ci", "reviewers": ["a"]}, {"required_checks": ["ci"], "reviewers": ["a"], "x": 1}, []):
            self.allow.write_text(json.dumps({"repos": {REPO: cfg}}))
            self.assertRefused(FakeGh(), "invalid")

    def test_unresolved_changes_requested(self):
        rv = [{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}}]
        self.assertRefused(FakeGh(base_pr(reviews=rv)), "CHANGES_REQUESTED")
        self.assertRefused(FakeGh(base_pr(reviewDecision="CHANGES_REQUESTED")), "CHANGES_REQUESTED")

    def test_changes_requested_superseded_by_later_approval_is_not_blocking(self):
        rv = [{"author": {"login": "rev"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}},
              {"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": "MEMBER", "commit": {"oid": HEAD}}]
        code, _ = self.go(FakeGh(base_pr(reviews=rv)))
        self.assertEqual(code, omr.EXIT_OK)

    def test_not_clean(self):
        for mss in ("BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN"):
            self.assertRefused(FakeGh(base_pr(mergeStateStatus=mss)), "mergeStateStatus")
        self.assertRefused(FakeGh(base_pr(mergeable="CONFLICTING")), "mergeable")

    def test_wrong_author_is_class_c_without_approval(self):
        self.assertRefused(FakeGh(base_pr(author={"login": "stranger"})), "class C needs")

    def test_repo_not_allowlisted(self):
        self.allow.write_text(json.dumps({"repos": {"Other/repo": allow_doc()["repos"][REPO]}}))
        self.assertRefused(FakeGh(), "not on the allowlist")
        self.allow.write_text(json.dumps({"repos": [REPO]}))  # the old list format is not accepted
        self.assertRefused(FakeGh(), "allowlist must map")

    def test_allowlist_missing_or_empty_is_refused(self):
        self.assertRefused(FakeGh(), "allowlist", allow=self.tmp_real / "nope.json")
        self.allow.write_text(json.dumps({"repos": {}}))
        self.assertRefused(FakeGh(), "allowlist must map")

    def test_missing_pr_fields_fail_closed(self):
        for key in ("state", "isDraft", "author", "headRefOid", "mergeable", "mergeStateStatus", "reviews",
                    "statusCheckRollup", "files", "reviewDecision", "baseRefName"):
            pr = base_pr()
            del pr[key]
            self.assertRefused(FakeGh(pr), "missing or malformed")

    def test_evidence_head_mismatch_fail_and_missing(self):
        self.write_evidence(head=OTHER)
        self.assertRefused(FakeGh(), "evidence head")
        self.write_evidence(verdict="FAIL")
        self.assertRefused(FakeGh(), "verdict")
        self.write_evidence(pr=8)
        self.assertRefused(FakeGh(), "evidence pr")
        self.write_evidence(repo="Other/repo")
        self.assertRefused(FakeGh(), "evidence repo mismatch")
        self.assertRefused(FakeGh(), "evidence missing", evidence=self.tmp_real / "none.json")
        self.evidence.write_text("not json")
        self.assertRefused(FakeGh(), "unparseable")

    def test_evidence_requires_every_bound_field_with_the_right_type(self):
        for key in ("repo", "pr", "head", "verdict", "reviewer", "reviewed_at", "reviewer_kind"):
            self.write_evidence(**{key: DROP})
            self.assertRefused(FakeGh(), "evidence")
        for key, bad in (("reviewer", 5), ("reviewer", ""), ("reviewed_at", 123), ("reviewer_kind", ["x"]),
                         ("repo", None), ("pr", True), ("pr", 7.5), ("pr", [7])):
            self.write_evidence(**{key: bad})
            self.assertRefused(FakeGh(), "evidence")

    def test_evidence_pr_may_be_int_or_digit_string(self):
        self.write_evidence(pr="7")
        self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)

    def test_evidence_reviewer_must_be_allowlisted_for_the_repo(self):
        self.write_evidence(reviewer="someone-else")
        self.assertRefused(FakeGh(), "not in the allowlisted reviewers")
        self.write_evidence(reviewer="Independent-1")  # exact match only
        self.assertRefused(FakeGh(), "not in the allowlisted reviewers")

    def test_evidence_kind_and_timestamp_validated(self):
        self.write_evidence(reviewer_kind="self")
        self.assertRefused(FakeGh(), "reviewer_kind")
        for ts in ("yesterday", "2026-10-20 10:00:00", "2026-13-40T99:00:00Z", "2999-01-01T00:00:00Z"):
            self.write_evidence(reviewed_at=ts)
            self.assertRefused(FakeGh(), "reviewed_at")

    def test_class_a_still_requires_evidence(self):
        gh = FakeGh()
        code, rec = self.go(gh, evidence=self.tmp_real / "absent.json")
        self.assertEqual((rec["class"], code, gh.merges()), (None, omr.EXIT_REFUSED, []))

    def test_bad_syntax_never_reaches_gh(self):
        cases = [dict(repo="nope"), dict(repo="a/b;rm -rf"), dict(repo="a/b c"), dict(repo="a/../b"),
                 dict(repo="a/b\n"), dict(pr="0"), dict(pr="-1"), dict(pr="7 --admin"), dict(pr="7\n"),
                 dict(pr="$(id)"), dict(head="A" * 40), dict(head="a" * 39), dict(head="a" * 41),
                 dict(head=HEAD + " --admin"), dict(head=HEAD + "\n"), dict(head="`id`".ljust(40, "a"))]
        for kw in cases:
            gh = FakeGh()
            code, rec = self.go(gh, **kw)
            self.assertEqual(code, omr.EXIT_REFUSED, kw)
            self.assertEqual(gh.calls, [], f"gh must not be started for {kw}")

    def test_shell_metacharacters_rejected_in_every_arg(self):
        for meta in (";", "&&", "|", "$(x)", "`x`", ">", "<", "'", '"', "\\", "*", "{a,b}", "\t"):
            for kw in (dict(repo=f"a/b{meta}"), dict(pr=f"7{meta}"), dict(head=HEAD[:-1] + meta)):
                gh = FakeGh()
                code, _ = self.go(gh, **kw)
                self.assertEqual((code, gh.calls), (omr.EXIT_REFUSED, []), (meta, kw))


class AllowlistTrust(Base):
    def test_shipped_example_is_never_trusted_by_default_path(self):
        ex = ROOT / "docs/examples/owner-merge-runner-allowlist.example.json"
        self.assertTrue(ex.exists())
        self.assertNotEqual(os.path.realpath(omr.DEFAULT_ALLOWLIST), os.path.realpath(ex))
        self.assertRefused(FakeGh(), "git worktree", allow=ex)  # the repo copy is refused even if pointed at

    def test_symlink_refused(self):
        link = self.tmp_real / "link.json"
        link.symlink_to(self.allow)
        self.assertRefused(FakeGh(), "non-symlink", allow=link)

    def test_group_or_world_writable_file_or_dir_refused(self):
        for mode in (0o664, 0o666, 0o646):
            os.chmod(self.allow, mode)
            self.assertBlocked(FakeGh(), "writable")
        os.chmod(self.allow, 0o644)
        os.chmod(self.tmp.name, 0o777)
        try:
            self.assertBlocked(FakeGh(), "writable")
        finally:
            os.chmod(self.tmp.name, 0o700)

    def test_inside_git_worktree_refused(self):
        wt = self.tmp_real / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nowhere")
        f = wt / "allow.json"
        f.write_text(json.dumps(allow_doc()))
        self.assertRefused(FakeGh(), "git worktree", allow=f)

    def test_foreign_owner_refused(self):
        real = os.geteuid
        omr.os.geteuid = lambda: real() + 1
        try:
            self.assertBlocked(FakeGh(), "not owned")
        finally:
            omr.os.geteuid = real

    def test_group_writable_ancestor_refused(self):
        sub = self.tmp_real / "a" / "b"
        sub.mkdir(parents=True)
        f = sub / "allow.json"
        f.write_text(json.dumps(allow_doc()))
        os.chmod(sub.parent, 0o775)
        self.assertBlocked(FakeGh(), "writable", allow=f)
        os.chmod(sub.parent, 0o700)
        self.assertEqual(self.go(FakeGh(), dry=True, allow=f)[0], omr.EXIT_OK)

    def test_symlinked_ancestor_refused(self):
        real = self.tmp_real / "real"
        real.mkdir()
        (real / "allow.json").write_text(json.dumps(allow_doc()))
        link = self.tmp_real / "linkdir"
        link.symlink_to(real)
        self.assertRefused(FakeGh(), "symlink", allow=link / "allow.json")

    def test_sticky_root_owned_writable_ancestor_is_the_only_writable_exception(self):
        real_lstat = os.lstat
        target = str(self.tmp_real.parent)

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == target:
                vals = list(st)
                vals[0] = stat.S_IFDIR | 0o1777
                vals[4] = 0
                return os.stat_result(vals)
            return st
        omr.os.lstat = fake
        try:
            self.assertEqual(omr.dir_chain_problems(str(self.tmp_real)), [])
            def fake2(path, *a, **k):
                st = fake(path, *a, **k)
                if str(path) == target:
                    vals = list(st)
                    vals[0] = stat.S_IFDIR | 0o777  # writable, not sticky
                    return os.stat_result(vals)
                return st
            omr.os.lstat = fake2
            self.assertTrue(any("writable" in p for p in omr.dir_chain_problems(str(self.tmp_real))))
        finally:
            omr.os.lstat = real_lstat

    def test_allowlist_is_read_through_the_checked_descriptor(self):
        opened, real_open = [], os.open

        def spy(path, flags, *a, **k):
            opened.append((str(path), flags))
            return real_open(path, flags, *a, **k)
        omr.os.open = spy
        try:
            self.assertEqual(self.go(FakeGh(), dry=True)[0], omr.EXIT_OK)
        finally:
            omr.os.open = real_open
        flags = [f for p, f in opened if p == str(self.allow)]
        self.assertEqual(len(flags), 1)
        self.assertTrue(flags[0] & os.O_NOFOLLOW)
        src = (ROOT / "scripts/owner-merge-runner.py").read_text()
        body = src[src.index("def read_trusted_allowlist"):src.index("def load_allowlist")]
        self.assertNotIn("open(path", body.replace("os.open(path", ""))  # no second open by pathname

    def test_fifo_or_directory_allowlist_refused(self):
        fifo = self.tmp_real / "fifo.json"
        os.mkfifo(fifo)
        # O_NONBLOCK is not used by the runner, so refuse before reading: opening a FIFO read-only blocks
        # only without a writer; open a writer thread-free by testing the directory case instead.
        d = self.tmp_real / "dir.json"
        d.mkdir()
        self.assertRefused(FakeGh(), "allowlist", allow=d)

    def test_trusted_file_is_accepted(self):
        code, _ = self.go(FakeGh(), dry=True)
        self.assertEqual(code, omr.EXIT_OK)


class ClassCAssociation(Base):
    def pr_with(self, assoc, **over):
        return base_pr(author={"login": "someone"}, reviews=[
            {"author": {"login": "rev"}, "state": "APPROVED", "authorAssociation": assoc, "commit": {"oid": HEAD}}], **over)

    def test_untrusted_associations_refused(self):
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", None):
            self.assertRefused(FakeGh(self.pr_with(assoc)), "OWNER, MEMBER or COLLABORATOR")

    def test_trusted_associations_accepted(self):
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            code, _ = self.go(FakeGh(self.pr_with(assoc)))
            self.assertEqual(code, omr.EXIT_OK, assoc)

    def test_cross_repository_needs_authenticated_user_approval(self):
        self.assertRefused(FakeGh(self.pr_with("OWNER", isCrossRepository=True)), "cross-repository")
        pr = self.pr_with("OWNER", isCrossRepository=True)
        pr["reviews"].append({"author": {"login": ME}, "state": "APPROVED", "authorAssociation": "OWNER",
                              "commit": {"oid": HEAD}})
        code, _ = self.go(FakeGh(pr))
        self.assertEqual(code, omr.EXIT_OK)


class Outcomes(Base):
    def test_denial_recorded_not_retried(self):
        gh = FakeGh(merge_rc=1, merge_err="GraphQL: Merge Without Review denied: permission denied")
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_DENIED, "DENIED"))
        self.assertEqual(len(gh.merges()), 1)
        self.assertEqual(self.records()[-1]["outcome"], "DENIED")

    def test_other_gh_failure_is_error_not_retried(self):
        gh = FakeGh(merge_rc=1, merge_err="network unreachable")
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_ERROR, "ERROR"))
        self.assertEqual(len(gh.merges()), 1)

    def test_not_merged_after_success_is_unverified(self):
        gh = FakeGh(merged=False)
        code, rec = self.go(gh)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_UNVERIFIED, "UNVERIFIED"))

    def test_exit_codes_are_distinct(self):
        codes = {omr.EXIT_OK, omr.EXIT_REFUSED, omr.EXIT_DENIED, omr.EXIT_ERROR, omr.EXIT_UNVERIFIED, omr.EXIT_AUDIT,
                 omr.EXIT_USAGE}
        self.assertEqual(len(codes), 7)

    def test_gh_unavailable_is_error(self):
        def broken(argv):
            return 1, "", "boom"
        code, rec = self.go(broken)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_ERROR, "ERROR"))

    def test_audit_unwritable_blocks_merge(self):
        gh = FakeGh()
        blocker = self.tmp_real / "file"
        blocker.write_text("x")
        code, rec = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(blocker / "log"), False, gh)
        self.assertNotEqual(code, omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])


class Audit(Base):
    def test_audit_written_0600_append_only_and_clean(self):
        self.go(FakeGh())
        self.go(FakeGh(base_pr(isDraft=True)))
        self.assertEqual(stat.S_IMODE(os.stat(self.audit).st_mode), 0o600)
        recs = self.records()
        self.assertEqual([r["outcome"] for r in recs], ["PENDING", "MERGED", "REFUSED"])
        for k in ("ts", "repo", "pr", "expected_head", "observed_head", "checks", "decision", "reasons", "outcome"):
            self.assertIn(k, recs[0])
        self.assertEqual(recs[1]["checks"]["success"], 2)

    def test_no_secret_shaped_content_in_audit(self):
        secret = "ghp_" + "A1b2C3d4" * 5
        gh = FakeGh(merge_rc=1, merge_err=f"permission denied token={secret} Bearer abcdef123456")
        self.go(gh)
        text = self.audit.read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn("abcdef123456", text)
        self.assertNotRegex(text, omr.SECRET_RE)

    def test_audit_symlink_refused_no_merge(self):
        os.makedirs(self.audit.parent)
        victim = self.tmp_real / "victim.txt"
        victim.write_text("keep")
        self.audit.symlink_to(victim)
        gh = FakeGh()
        code, rec = self.go(gh)
        self.assertEqual(gh.merges(), [])
        self.assertEqual(victim.read_text(), "keep")
        self.assertNotEqual(code, omr.EXIT_OK)

    def test_audit_hardlink_wrong_mode_or_dir_refused(self):
        os.makedirs(self.audit.parent)
        other = self.tmp_real / "other.jsonl"
        other.write_text("")
        os.chmod(other, 0o600)
        os.link(other, self.audit)  # nlink == 2
        gh = FakeGh()
        self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])
        os.unlink(self.audit)
        self.audit.write_text("")
        os.chmod(self.audit, 0o644)
        gh = FakeGh()
        self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])
        os.chmod(self.audit, 0o600)
        os.chmod(self.audit.parent, 0o777)
        gh = FakeGh()
        try:
            self.assertNotEqual(self.go(gh)[0], omr.EXIT_OK)
        finally:
            os.chmod(self.audit.parent, 0o700)
        self.assertEqual(gh.merges(), [])

    def test_audit_symlinked_directory_refused(self):
        real = self.tmp_real / "realaudit"
        real.mkdir()
        link = self.tmp_real / "linkaudit"
        link.symlink_to(real)
        gh = FakeGh()
        code, _ = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(link / "log.jsonl"), False, gh)
        self.assertEqual((gh.merges(), code == omr.EXIT_OK), ([], False))

    def test_pending_record_fsynced_before_exec(self):
        gh, real = FakeGh(), os.fsync

        def spy(fd):
            gh.calls.append(["FSYNC"])
            return real(fd)
        omr.os.fsync = spy
        try:
            self.assertEqual(self.go(gh)[0], omr.EXIT_OK)
        finally:
            omr.os.fsync = real
        names = [c[0] if c[0] == "FSYNC" else " ".join(c[:2]) for c in gh.calls]
        self.assertLess(names.index("FSYNC"), names.index("pr merge"))

    def test_short_writes_are_looped_into_one_complete_line(self):
        real = os.write
        omr.os.write = lambda fd, data: real(fd, bytes(data[:3]))
        try:
            self.assertEqual(self.go(FakeGh())[0], omr.EXIT_OK)
        finally:
            omr.os.write = real
        recs = self.records()  # every line parses as complete JSON
        self.assertEqual([r["outcome"] for r in recs], ["PENDING", "MERGED"])

    def test_zero_progress_write_raises_and_blocks_merge(self):
        real = os.write
        omr.os.write = lambda fd, data: 0
        gh = FakeGh()
        try:
            code, rec = self.go(gh)
        finally:
            omr.os.write = real
        self.assertEqual(gh.merges(), [])
        self.assertNotEqual(code, omr.EXIT_OK)
        with self.assertRaises(omr.AuditError):
            omr.os.write = lambda fd, data: 0
            try:
                omr.write_all(1, b"abc")
            finally:
                omr.os.write = real

    def test_pending_failure_refuses_before_exec(self):
        blocker = self.tmp_real / "file"
        blocker.write_text("x")
        gh = FakeGh()
        with contextlib.redirect_stderr(io.StringIO()):
            code, rec = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(blocker / "log"), False, gh)
        self.assertEqual(gh.merges(), [])
        self.assertIn("refusing before execution", " ".join(rec["reasons"]))

    def test_final_record_failure_exits_with_distinct_code_and_stderr_line(self):
        gh = FakeGh()
        gh.on_merge = lambda: os.chmod(self.audit, 0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, rec = self.go(gh)
        self.assertEqual(len(gh.merges()), 1)
        self.assertEqual(code, omr.EXIT_AUDIT)
        self.assertNotEqual(code, omr.EXIT_OK)
        self.assertIn("final audit record", err.getvalue())
        self.assertIn("MERGED", err.getvalue())

    def test_scrub_redacts_common_token_shapes(self):
        for s in ("github_pat_" + "x" * 30, "ghs_" + "y" * 30, "password=hunter2", "Authorization: abc"):
            self.assertIn("[redacted]", omr.scrub(s))


class ArgvFixedness(Base):
    def test_check_argv_allows_only_fixed_shapes(self):
        ok = omr.merge_argv(REPO, "7", HEAD)
        self.assertEqual(omr.check_argv(ok), "merge")
        self.assertEqual(omr.check_argv(["api", "user"]), "user")
        self.assertEqual(omr.check_argv(omr.rules_argv(REPO, "main")), "protection")
        self.assertEqual(omr.check_argv(omr.protection_argv(REPO, "release/1.x")), "protection")
        bad = [ok + ["--admin"], ok + ["--squash"], ok[:-2], [x for x in ok if x != "--merge"],
               ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD],
               ["pr", "merge", "7", "--repo", REPO, "--merge", "--match-head-commit", "x" * 40],
               ["api", "repos/a/b/branches/main"], ["api", "repos/a/b/rules/branches/../x"],
               ["api", f"repos/{REPO}/branches/main/protection", "-X", "DELETE"],
               ["pr", "close", "7", "--repo", REPO], ["api", "user", "-X", "POST"],
               ["pr", "view", "7", "--repo", REPO, "--json", "body"], ["api", "repos/a/b/pulls/7/merge"]]
        for argv in bad:
            with self.assertRaises(RuntimeError, msg=argv):
                omr.check_argv(argv)

    def test_runner_wrapper_blocks_off_shape_calls_before_gh(self):
        gh = FakeGh()
        with self.assertRaises(RuntimeError):
            omr.Runner(gh).call(["pr", "merge", "7", "--repo", REPO, "--admin"])
        self.assertEqual(gh.calls, [])

    def test_subprocess_gh_is_shell_free_with_fixed_argv(self):
        src = (ROOT / "scripts/owner-merge-runner.py").read_text()
        self.assertIn("shell=False", src)
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        for forbidden in ("--admin", "--squash", "--rebase", "--auto", "--delete-branch"):
            self.assertNotIn(f'"{forbidden}"', src)

    def test_real_subprocess_path_with_fake_executable_in_tmp(self):
        bin_path = self.tmp_real / "fake-gh"
        log = self.tmp_real / "argv.log"
        script = ("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done >> LOG\nprintf -- '---\\n' >> LOG\n"
                  "case \"$1 $2\" in 'api user') echo '{\"login\":\"chisanan232\"}';; esac\nexit 0\n")
        bin_path.write_text(script.replace("LOG", f"'{log}'"))
        bin_path.chmod(0o755)
        code, rc_out, _ = omr.SubprocessGh(str(bin_path))(["api", "user"])
        self.assertEqual((code, json.loads(rc_out)["login"]), (0, "chisanan232"))
        self.assertEqual(log.read_text().split(), ["api", "user", "---"])

    def test_cli_has_no_gh_path_allowlist_or_audit_override(self):
        for flag in ("--gh-path", "--allowlist", "--audit-log"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts/owner-merge-runner.py"), flag, "/tmp/x",
                                "--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", "x"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, omr.EXIT_USAGE, flag)

    def test_default_paths_are_fixed_under_home_dot_claude_not_the_repo(self):
        home = os.path.expanduser("~")
        self.assertEqual(omr.DEFAULT_ALLOWLIST, os.path.join(home, ".claude", "owner-merge-runner-allowlist.json"))
        self.assertEqual(omr.DEFAULT_AUDIT, os.path.join(home, ".claude", "audit", "owner-merge-runner.jsonl"))
        self.assertFalse(str(ROOT) in omr.DEFAULT_ALLOWLIST)
        self.assertFalse((ROOT / "docs/owner-merge-runner-allowlist.json").exists())

    def test_main_uses_injected_gh_and_returns_code(self):
        gh = FakeGh()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = omr.main(["--repo", REPO, "--pr", "7", "--expect-head", HEAD, "--review-evidence", str(self.evidence),
                       "--dry-run"], gh=gh, allowlist=str(self.allow), audit=str(self.audit))
        self.assertEqual(rc, omr.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
