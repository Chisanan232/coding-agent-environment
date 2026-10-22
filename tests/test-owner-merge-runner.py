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


def green():
    return [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": "SUCCESS"}]


def base_pr(**over):
    pr = {"state": "OPEN", "isDraft": False, "author": {"login": ME}, "baseRefName": "main",
          "headRefName": "feature/x", "headRefOid": HEAD, "isCrossRepository": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "",
          "reviews": [], "statusCheckRollup": green(), "files": [{"path": "a.txt"}]}
    pr.update(over)
    return pr


class FakeGh:
    def __init__(self, pr=None, merge_rc=0, merge_err="", merged=True, head_after=None, user=ME):
        self.pr = pr if pr is not None else base_pr()
        self.merge_rc, self.merge_err, self.merged = merge_rc, merge_err, merged
        self.head_after = head_after  # head reported by the second (pre-exec) view
        self.user = user
        self.calls = []
        self.views = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv == ["api", "user"]:
            return 0, json.dumps({"login": self.user}), ""
        if argv[:2] == ["pr", "view"]:
            fields = argv[6]
            if fields == omr.PR_FIELDS:
                return 0, json.dumps(self.pr), ""
            if fields == omr.HEAD_FIELDS:
                head = self.head_after or self.pr["headRefOid"]
                return 0, json.dumps({"state": self.pr["state"], "headRefOid": head}), ""
            if fields == omr.READBACK_FIELDS:
                if self.merged:
                    return 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA}}), ""
                return 0, json.dumps({"state": "OPEN", "mergeCommit": None}), ""
        if argv[:2] == ["pr", "merge"]:
            return self.merge_rc, "", self.merge_err
        raise AssertionError(f"unexpected argv {argv}")

    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = pathlib.Path(self.tmp.name)
        self.allow = d / "allow.json"
        self.allow.write_text(json.dumps({"repos": [REPO]}))
        self.evidence = d / "evidence.json"
        self.write_evidence()
        self.audit = d / "audit" / "log.jsonl"

    def write_evidence(self, **over):
        ev = {"head": HEAD, "verdict": "PASS", "reviewer": "independent-1"}
        ev.update(over)
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
        gh = FakeGh(head_after=OTHER)
        self.assertRefused(gh, "head moved")

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
        self.assertRefused(FakeGh(base_pr(statusCheckRollup=[])), "no status checks")

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
        self.allow.write_text(json.dumps({"repos": ["Other/repo"]}))
        self.assertRefused(FakeGh(), "allowlist")

    def test_allowlist_missing_or_empty_is_refused(self):
        self.assertRefused(FakeGh(), "allowlist", allow=pathlib.Path(self.tmp.name) / "nope.json")
        self.allow.write_text(json.dumps({"repos": []}))
        self.assertRefused(FakeGh(), "allowlist")

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
        self.assertRefused(FakeGh(), "evidence missing", evidence=pathlib.Path(self.tmp.name) / "none.json")
        self.evidence.write_text("not json")
        self.assertRefused(FakeGh(), "unparseable")

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
        link = pathlib.Path(self.tmp.name) / "link.json"
        link.symlink_to(self.allow)
        self.assertRefused(FakeGh(), "non-symlink", allow=link)

    def test_group_or_world_writable_file_or_dir_refused(self):
        for mode in (0o664, 0o666, 0o646):
            os.chmod(self.allow, mode)
            self.assertRefused(FakeGh(), "writable")
        os.chmod(self.allow, 0o644)
        os.chmod(self.tmp.name, 0o777)
        try:
            self.assertRefused(FakeGh(), "writable")
        finally:
            os.chmod(self.tmp.name, 0o700)

    def test_inside_git_worktree_refused(self):
        wt = pathlib.Path(self.tmp.name) / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nowhere")
        f = wt / "allow.json"
        f.write_text(json.dumps({"repos": [REPO]}))
        self.assertRefused(FakeGh(), "git worktree", allow=f)

    def test_foreign_owner_refused(self):
        real = os.geteuid
        omr.os.geteuid = lambda: real() + 1
        try:
            self.assertRefused(FakeGh(), "not owned")
        finally:
            omr.os.geteuid = real

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
        codes = {omr.EXIT_OK, omr.EXIT_REFUSED, omr.EXIT_DENIED, omr.EXIT_ERROR, omr.EXIT_UNVERIFIED, omr.EXIT_USAGE}
        self.assertEqual(len(codes), 6)

    def test_gh_unavailable_is_error(self):
        def broken(argv):
            return 1, "", "boom"
        code, rec = self.go(broken)
        self.assertEqual((code, rec["outcome"]), (omr.EXIT_ERROR, "ERROR"))

    def test_audit_unwritable_blocks_merge(self):
        gh = FakeGh()
        blocker = pathlib.Path(self.tmp.name) / "file"
        blocker.write_text("x")
        code, rec = omr.run(REPO, "7", HEAD, str(self.evidence), str(self.allow), str(blocker / "log"), False, gh)
        self.assertNotEqual(code, omr.EXIT_OK)
        self.assertEqual(gh.merges(), [])


class Audit(Base):
    def test_audit_written_0600_append_only_and_clean(self):
        os.makedirs(self.audit.parent)
        self.audit.write_text("")
        os.chmod(self.audit, 0o644)
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

    def test_scrub_redacts_common_token_shapes(self):
        for s in ("github_pat_" + "x" * 30, "ghs_" + "y" * 30, "password=hunter2", "Authorization: abc"):
            self.assertIn("[redacted]", omr.scrub(s))


class ArgvFixedness(Base):
    def test_check_argv_allows_only_fixed_shapes(self):
        ok = omr.merge_argv(REPO, "7", HEAD)
        self.assertEqual(omr.check_argv(ok), "merge")
        self.assertEqual(omr.check_argv(["api", "user"]), "user")
        bad = [ok + ["--admin"], ok + ["--squash"], ok[:-2], [x for x in ok if x != "--merge"],
               ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD],
               ["pr", "merge", "7", "--repo", REPO, "--merge", "--match-head-commit", "x" * 40],
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
        bin_path = pathlib.Path(self.tmp.name) / "fake-gh"
        log = pathlib.Path(self.tmp.name) / "argv.log"
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
