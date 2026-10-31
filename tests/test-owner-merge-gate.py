#!/usr/bin/env python3
"""Unit tests for scripts/owner-merge-gate.py with a fake gh runner (SPE-115).

Live behavior (the host classifier, a real merge) is covered by the acceptance runs recorded in
the ticket, not here.
"""
import importlib.util
import json
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location("omg", pathlib.Path(__file__).resolve().parents[1] / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

HEAD = "a" * 40
SRC = [{"path": "src/a.py"}]
OWNERS = ["Chisanan232", "horonomy", "ai-agent-assembly"]
REVIEW = f"OWNER-MERGE-REVIEW head={HEAD} verdict=PASS blocking=0 reviewer=independent-agent-1"


def green():
    return [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


class Fake:
    """Answers the gh calls the gate makes from a mutable state dict."""

    def __init__(self, **over):
        self.pr = {"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "feat/x", "headRefOid": HEAD,
                   "author": {"login": "chisanan232"}, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                   "reviewDecision": "", "reviews": [], "isCrossRepository": False, "mergeCommit": None,
                   "comments": [{"author": {"login": "chisanan232"}, "body": REVIEW}],
                   "statusCheckRollup": green()}
        self.repo = {"nameWithOwner": "Chisanan232/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
                     "viewerPermission": "ADMIN", "owner": {"login": "Chisanan232"}}
        self.behind, self.threads, self.me, self.more_threads = 0, [], "chisanan232", False
        self.parents, self.cmp, self.org_role = [HEAD, "b" * 40], "ahead", "admin active"
        for k, v in over.items():
            target = self.repo if k in self.repo else self.pr if k in self.pr else None
            if target is None:
                setattr(self, k, v)
            else:
                target[k] = v

    def __call__(self, *a):
        if a[:2] == ("repo", "view"):
            return json.dumps(self.repo)
        if a[:2] == ("api", "user"):
            return self.me + "\n"
        if a[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        if a[0] == "api" and "/compare/" in a[1]:
            return (str(self.behind) if "behind_by" in a[3] else self.cmp) + "\n"
        if a[:2] == ("api", "graphql"):
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": self.more_threads}, "nodes": self.threads}}}}})
        if a[0] == "api" and a[1].startswith("orgs/"):
            return self.org_role + "\n"
        if a[0] == "api" and "/contents/.github/workflows?ref=" in a[1]:
            ref = a[1].rpartition("?ref=")[2]
            self.workflow_reads = getattr(self, "workflow_reads", []) + [ref]
            answer = self.workflows.get(ref, self.workflows.get("*")) if isinstance(getattr(self, "workflows", None), dict) else getattr(self, "workflows", None)
            if answer is None:
                raise RuntimeError(f"gh api repos/{a[1].split('/')[1]}/{a[1].split('/')[2]}/contents/.github/workflows failed: gh: Not Found (HTTP 404)")
            if answer == "error":
                raise RuntimeError("gh api repos/x/y/contents/.github/workflows failed: HTTP 502")
            if answer == "ratelimit":
                raise RuntimeError("gh api repos/a/app404/contents/.github/workflows failed: gh: API rate limit exceeded (HTTP 403)")
            return "" if answer == "empty" else str(answer) + "\n"
        if a[0] == "api" and "/commits/" in a[1]:
            return json.dumps(self.parents)
        raise AssertionError(a)


def run(fake, **kw):
    return omg.evaluate(fake, "Chisanan232/r", 7, OWNERS, **kw)


POLICY = {"schema": 1, "repos": {"Chisanan232/r": {
    "branches": [{"branch": "integration/mvp-5.0", "version": "5.0"},
                 {"branch": "mvp-4.0/HORO-2046/integration", "version": "4.0"}],
    "future_head_prefixes": ["mvp-6.0/"]}}}


class Gate(unittest.TestCase):
    def refused(self, fake, code, **kw):
        v, reasons, _ = run(fake, **kw)
        self.assertEqual(v, "REFUSE", reasons)
        self.assertTrue(any(r.startswith(code) for r in reasons), reasons)

    def test_reviewed_green_pr_passes_with_pinned_merge_commit_command(self):
        v, reasons, facts = run(Fake())
        self.assertEqual((v, reasons), ("PASS", []))
        cmd = omg.merge_command(facts)
        self.assertIn("--merge", cmd)
        self.assertIn(f"--match-head-commit {HEAD}", cmd)
        self.assertNotIn("--admin", cmd)
        self.assertNotIn("--squash", cmd)

    def test_failed_required_ci_refuses(self):
        self.refused(Fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")

    def test_pending_ci_waits_not_passes(self):
        v, _, _ = run(Fake(statusCheckRollup=[{"name": "t", "status": "IN_PROGRESS", "conclusion": ""}]))
        self.assertEqual(v, "WAIT")

    def test_unresolved_review_thread_refuses(self):
        self.refused(Fake(threads=[{"isResolved": False, "isOutdated": False}]), "UNRESOLVED_REVIEW_THREADS")

    def test_outstanding_request_changes_refuses(self):
        self.refused(Fake(reviews=[{"state": "CHANGES_REQUESTED", "author": {"login": "x"}}]), "CHANGES_REQUESTED")

    def test_head_changed_after_review_refuses(self):
        self.refused(Fake(headRefOid="c" * 40), "REVIEW_STALE")

    def test_missing_review_record_refuses(self):
        self.refused(Fake(comments=[]), "NO_REVIEW_EVIDENCE")

    def test_review_record_from_another_user_does_not_count(self):
        self.refused(Fake(comments=[{"author": {"login": "someone-else"}, "body": REVIEW}]), "NO_REVIEW_EVIDENCE")

    def test_review_with_blocking_findings_refuses(self):
        body = REVIEW.replace("blocking=0", "blocking=2")
        self.refused(Fake(comments=[{"author": {"login": "chisanan232"}, "body": body}]), "REVIEW_NOT_CLEAN")

    def test_author_review_is_not_independent(self):
        body = REVIEW.replace("independent-agent-1", "coordinator")
        self.refused(Fake(comments=[{"author": {"login": "chisanan232"}, "body": body}]), "REVIEW_NOT_INDEPENDENT")

    def test_stale_branch_refuses_so_overlapping_lockfile_prs_serialize(self):
        self.refused(Fake(behind=1), "STALE_BRANCH")

    def test_conflicting_pr_refuses(self):
        self.refused(Fake(mergeable="CONFLICTING", mergeStateStatus="DIRTY"), "CONFLICTS")

    def test_required_formal_review_is_never_substituted(self):
        self.refused(Fake(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED"), "FORMAL_APPROVAL_REQUIRED")

    def test_repository_rule_block_refuses(self):
        self.refused(Fake(mergeStateStatus="BLOCKED"), "BLOCKED_BY_REPOSITORY_RULES")

    def test_untrusted_owner_refuses_before_any_gh_call(self):
        v, reasons, _ = omg.evaluate(lambda *a: self.fail("gh called"), "evil/r", 7, OWNERS)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(reasons[0].startswith("UNAUTHORIZED_REPO"))

    def test_lookalike_owner_prefix_refuses(self):
        v, reasons, _ = omg.evaluate(lambda *a: self.fail("gh called"), "Chisanan2320/r", 7, OWNERS)
        self.assertEqual(v, "REFUSE")

    def test_fork_and_cross_repository_head_refuse(self):
        self.refused(Fake(isFork=True), "FORK_REPO")
        self.refused(Fake(isCrossRepository=True), "CROSS_REPOSITORY_HEAD")

    def test_non_default_and_deployment_bases_refuse(self):
        self.refused(Fake(baseRefName="release"), "BASE_NOT_DEFAULT_OR_DEPLOY")
        self.refused(Fake(baseRefName="feature-x"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_draft_and_closed_refuse(self):
        self.refused(Fake(isDraft=True), "DRAFT")
        self.refused(Fake(state="MERGED"), "NOT_OPEN")

    def test_no_checks_without_exception_refuses(self):
        self.refused(Fake(statusCheckRollup=[]), "NO_CI_EVIDENCE")

    def test_ci_unavailable_needs_flag_and_head_pinned_record(self):
        record = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": record}]
        self.refused(Fake(statusCheckRollup=[], comments=comments), "NO_CI_EVIDENCE")  # flag missing
        v, _, facts = run(Fake(statusCheckRollup=[], comments=comments), ci_unavailable=True)
        self.assertEqual((v, facts["ci_exception"]), ("PASS", "billing"))
        stale = [comments[0], {"author": {"login": "chisanan232"}, "body": record.replace(HEAD, "d" * 40)}]
        self.refused(Fake(statusCheckRollup=[], comments=stale), "NO_CI_EVIDENCE", ci_unavailable=True)

    NOCI = f"OWNER-MERGE-CI-EXCEPTION head={HEAD} basis=no-hosted-ci local_equivalent=PASS"

    def noci_fake(self, files=None, comments=None, **over):
        fake = Fake(statusCheckRollup=over.pop("statusCheckRollup", []), comments=comments if comments is not None else self.noci_comments(), **over)
        fake.pr["files"] = SRC if files is None else files
        return fake

    def noci_comments(self, record=None):
        return [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": record or self.NOCI}]

    def test_no_hosted_ci_exception_needs_flag_record_zero_checks_and_no_workflows(self):
        self.refused(self.noci_fake(comments=self.noci_comments()), "NO_CI_EVIDENCE")                  # flag missing
        v, _, facts = run(self.noci_fake(comments=self.noci_comments()), ci_exception=True)
        self.assertEqual((v, facts["ci_exception"]), ("PASS", "no-hosted-ci"))
        self.refused(self.noci_fake(comments=[self.noci_comments()[0]]), "NO_CI_EVIDENCE", ci_exception=True)   # no record
        stale = self.noci_comments(self.NOCI.replace(HEAD, "d" * 40))
        self.refused(self.noci_fake(comments=stale), "NO_CI_EVIDENCE", ci_exception=True)            # another head
        self.refused(self.noci_fake(comments=self.noci_comments(self.NOCI.replace("PASS", "FAIL"))), "NO_CI_EVIDENCE", ci_exception=True)
        other = [self.noci_comments()[0], {"author": {"login": "someone-else"}, "body": self.NOCI}]
        self.refused(self.noci_fake(comments=other), "NO_CI_EVIDENCE", ci_exception=True)            # not the authenticated user

    def test_no_hosted_ci_exception_is_refused_where_workflows_exist_or_cannot_be_read(self):
        for workflows in (3, 1, 0, "empty", "error", "ratelimit", {"main": None, "*": 2}, {HEAD: 2, "*": None}):
            with self.subTest(workflows=workflows):
                self.refused(self.noci_fake(comments=self.noci_comments(), workflows=workflows), "NO_CI_EVIDENCE", ci_exception=True)

    def test_a_repository_name_containing_404_cannot_imitate_a_missing_directory(self):
        fake = self.noci_fake(comments=self.noci_comments(), workflows="ratelimit")
        self.assertTrue(omg.has_workflows(fake, "a/app404", "main"))                      # "404" only in the repo name and the path
        self.refused(fake, "NO_CI_EVIDENCE", ci_exception=True)

    def test_the_workflow_directory_is_read_at_the_base_and_at_the_head(self):
        fake = self.noci_fake(comments=self.noci_comments())
        v, _, _ = run(fake, ci_exception=True)
        self.assertEqual(v, "PASS")
        self.assertEqual(sorted(fake.workflow_reads), sorted(["main", HEAD]))
        # workflows only at the head (a PR that brings them) or only at the base both refuse
        self.refused(self.noci_fake(comments=self.noci_comments(), workflows={HEAD: 1, "*": None}), "NO_CI_EVIDENCE", ci_exception=True)
        self.refused(self.noci_fake(comments=self.noci_comments(), workflows={"main": 1, "*": None}), "NO_CI_EVIDENCE", ci_exception=True)

    def test_a_pr_that_adds_or_touches_a_workflow_or_has_an_unknown_file_list_cannot_use_the_exception(self):
        for files in ([{"path": ".github/workflows/ci.yml"}], [{"path": "src/a.py"}, {"path": ".github/workflows/x.yml"}],
                      [{"path": ".GitHub/Workflows/x.yml"}], [{"path": "pkg/.github/workflows/a.yml"}], [], [{"path": f"f{i}"} for i in range(100)]):
            with self.subTest(files=files[:2]):
                self.refused(self.noci_fake(files=files), "NO_CI_EVIDENCE", ci_exception=True)
        v, _, _ = run(self.noci_fake(), ci_exception=True)
        self.assertEqual(v, "PASS")

    def test_a_pull_request_from_a_fork_cannot_use_the_exception(self):
        self.refused(self.noci_fake(comments=self.noci_comments(), isCrossRepository=True), "NO_CI_EVIDENCE", ci_exception=True)

    def test_a_check_that_ran_without_failing_still_blocks_the_exception(self):
        for conclusion in ("SKIPPED", "NEUTRAL", "SUCCESS"):
            with self.subTest(conclusion=conclusion):
                v, _, facts = run(self.noci_fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": conclusion}]), ci_exception=True)
                self.assertNotIn("ci_exception", facts)

    def test_no_hosted_ci_exception_never_excuses_checks_that_ran_or_a_missing_review(self):
        bad = [{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.refused(self.noci_fake(statusCheckRollup=bad), "CI_FAILED", ci_exception=True)
        pending = [{"name": "t", "status": "IN_PROGRESS", "conclusion": ""}]
        v, _, _ = run(self.noci_fake(statusCheckRollup=pending), ci_exception=True)
        self.assertEqual(v, "WAIT")
        self.refused(self.noci_fake(comments=[self.noci_comments()[1]]), "NO_REVIEW_EVIDENCE", ci_exception=True)

    def test_the_two_ci_exceptions_do_not_substitute_for_each_other(self):
        unavailable = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
        self.refused(self.noci_fake(comments=self.noci_comments(unavailable)), "NO_CI_EVIDENCE", ci_exception=True)
        self.refused(self.noci_fake(comments=self.noci_comments()), "NO_CI_EVIDENCE", ci_unavailable=True)

    def test_the_workflow_read_is_an_allowed_read_only_call(self):
        omg.read_only(("api", "repos/Chisanan232/r/contents/.github/workflows?ref=main", "--jq", "length"))
        omg.read_only(("api", f"repos/Chisanan232/r/contents/.github/workflows?ref={'a' * 40}", "--jq", "length"))
        for bad in ("repos/Chisanan232/r/contents/.github/workflows", "repos/Chisanan232/r/contents/.github/workflows/ci.yml?ref=main",
                    "repos/Chisanan232/r/contents/.github/workflows?ref=../x", "repos/Chisanan232/r/contents/.github/workflows?ref=main&x=1"):
            with self.subTest(bad), self.assertRaises(RuntimeError):
                omg.read_only(("api", bad, "--jq", "length"))

    def test_ci_unavailable_never_excuses_a_genuine_failure(self):
        record = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": record}]
        bad = [{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.refused(Fake(statusCheckRollup=bad, comments=comments), "CI_FAILED", ci_unavailable=True)


class VersionBranches(unittest.TestCase):
    """Development-Ahead / Sequential Release: approved version integration branches are merge targets, main is not."""

    def evaluate(self, fake, policy=POLICY):
        return run(fake, version_policy=omg.load_version_policy(policy))

    def refused(self, fake, code, policy=POLICY):
        v, reasons, _ = self.evaluate(fake, policy)
        self.assertEqual(v, "REFUSE", reasons)
        self.assertTrue(any(r.startswith(code) for r in reasons), reasons)

    def test_eligible_pr_into_approved_version_branch_passes(self):
        for base in ("integration/mvp-5.0", "mvp-4.0/HORO-2046/integration"):
            v, reasons, facts = self.evaluate(Fake(baseRefName=base))
            self.assertEqual((v, reasons), ("PASS", []), base)
            self.assertEqual(facts["base_policy"], "approved version integration branch")
            self.assertIn("--merge", omg.merge_command(facts))

    def test_unlisted_branch_is_still_refused(self):
        self.refused(Fake(baseRefName="integration/mvp-9.0"), "BASE_NOT_DEFAULT_OR_DEPLOY")
        self.refused(Fake(baseRefName="feature-x"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_branch_approved_for_another_repo_does_not_authorize(self):
        other = {"schema": 1, "repos": {"Chisanan232/other": POLICY["repos"]["Chisanan232/r"]}}
        self.refused(Fake(baseRefName="integration/mvp-5.0"), "BASE_NOT_DEFAULT_OR_DEPLOY", other)

    def test_no_policy_keeps_the_old_behaviour(self):
        v, reasons, _ = run(Fake(baseRefName="integration/mvp-5.0"))
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any(r.startswith("BASE_NOT_DEFAULT_OR_DEPLOY") for r in reasons))

    def test_policy_can_never_approve_a_deployment_base(self):
        bad = {"schema": 1, "repos": {"Chisanan232/r": {"branches": [{"branch": "release/1.0", "version": "1.0"}]}}}
        with self.assertRaises(ValueError):
            omg.load_version_policy(bad)

    def test_approved_base_still_needs_review_ci_and_threads(self):
        base = "integration/mvp-5.0"
        self.refused(Fake(baseRefName=base, comments=[]), "NO_REVIEW_EVIDENCE")
        self.refused(Fake(baseRefName=base, statusCheckRollup=[]), "NO_CI_EVIDENCE")
        self.refused(Fake(baseRefName=base, statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")
        self.refused(Fake(baseRefName=base, headRefOid="c" * 40), "REVIEW_STALE")
        self.refused(Fake(baseRefName=base, threads=[{"isResolved": False, "isOutdated": False}]), "UNRESOLVED_REVIEW_THREADS")
        self.refused(Fake(baseRefName=base, mergeable="CONFLICTING", mergeStateStatus="DIRTY"), "CONFLICTS")
        self.refused(Fake(baseRefName=base, behind=2), "STALE_BRANCH")

    def test_future_version_code_cannot_enter_the_default_branch(self):
        self.refused(Fake(headRefName="integration/mvp-5.0"), "FUTURE_VERSION_INTO_MAIN")
        self.refused(Fake(headRefName="mvp-4.0/HORO-2046/integration"), "FUTURE_VERSION_INTO_MAIN")
        self.refused(Fake(headRefName="mvp-6.0/HORO-2087/path_boundary_experiment"), "FUTURE_VERSION_INTO_MAIN")

    def test_ordinary_default_branch_merge_is_unchanged(self):
        v, reasons, facts = self.evaluate(Fake())
        self.assertEqual((v, reasons), ("PASS", []))
        self.assertNotIn("base_policy", facts)

    def test_deployment_base_stays_refused_with_a_policy_loaded(self):
        self.refused(Fake(baseRefName="release"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_policy_file_must_be_private_and_missing_default_means_no_policy(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(os.path.realpath(d), "local")
            os.mkdir(root, 0o700)
            missing = os.path.join(root, "none.json")
            self.assertIsNone(omg.read_version_policy(missing, False, root))
            with self.assertRaises(FileNotFoundError):
                omg.read_version_policy(missing, True, root)
            f = os.path.join(root, "p.json")
            pathlib.Path(f).write_text(json.dumps(POLICY))
            os.chmod(f, 0o600)
            self.assertIn("chisanan232/r", omg.read_version_policy(f, True, root))
            os.chmod(f, 0o666)
            with self.assertRaises(ValueError):
                omg.read_version_policy(f, True, root)

    def test_policy_outside_the_owner_config_root_or_via_symlink_is_refused(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            real = os.path.realpath(d)
            root = os.path.join(real, "local")
            os.mkdir(root, 0o700)
            elsewhere = os.path.join(real, "repo-worktree.json")  # e.g. a file a PR could edit
            pathlib.Path(elsewhere).write_text(json.dumps(POLICY))
            os.chmod(elsewhere, 0o600)
            with self.assertRaises(ValueError):
                omg.read_version_policy(elsewhere, True, root)
            link = os.path.join(root, "link.json")
            os.symlink(elsewhere, link)
            with self.assertRaises(ValueError):  # the real path escapes the root, so the link buys nothing
                omg.read_version_policy(link, True, root)
            os.chmod(root, 0o777)
            ok = os.path.join(root, "p.json")
            pathlib.Path(ok).write_text(json.dumps(POLICY))
            os.chmod(ok, 0o600)
            with self.assertRaises(ValueError):  # group/world-writable directory
                omg.read_version_policy(ok, True, root)

    def test_environment_and_release_names_can_never_be_listed(self):
        for name in ("staged", "beta", "alpha", "canary", "hotfix/x", "x/staging/y", "staging-eu", "beta-2", "hotfix-1", "integration/beta-fix", "v1-prod", "v1.0-release", "deploy"):
            bad = {"schema": 1, "repos": {"Chisanan232/r": {"branches": [{"branch": name, "version": "1"}]}}}
            with self.assertRaises(ValueError, msg=name):
                omg.load_version_policy(bad)

    def test_base_approval_is_case_exact(self):
        self.refused(Fake(baseRefName="Integration/MVP-5.0"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_future_head_match_ignores_case(self):
        self.refused(Fake(headRefName="MVP-6.0/x"), "FUTURE_VERSION_INTO_MAIN")
        self.refused(Fake(headRefName="Integration/MVP-5.0"), "FUTURE_VERSION_INTO_MAIN")

    def test_case_variant_duplicate_repo_is_rejected(self):
        entry = {"branches": [{"branch": "integration/mvp-5.0", "version": "5.0"}]}
        with self.assertRaises(ValueError):
            omg.load_version_policy({"schema": 1, "repos": {"Chisanan232/r": entry, "chisanan232/R": entry}})

    def test_default_branch_other_than_main(self):
        v, reasons, _ = self.evaluate(Fake(defaultBranchRef={"name": "master"}, baseRefName="integration/mvp-5.0"))
        self.assertEqual((v, reasons), ("PASS", []))
        self.refused(Fake(defaultBranchRef={"name": "master"}, baseRefName="master", headRefName="integration/mvp-5.0"),
                     "FUTURE_VERSION_INTO_MAIN")
        self.refused(Fake(defaultBranchRef={"name": "master"}, baseRefName="main"), "BASE_NOT_DEFAULT_OR_DEPLOY")

    def test_loader_hardening_spe_133(self):
        import os, tempfile
        entry = POLICY["repos"]["Chisanan232/r"]
        bad = [{"schema": True, "repos": {}}, {"schema": 1.0, "repos": {}},
               {"schema": 1, "repos": {"Chisanan232/r": dict(entry, future_head_prefixes="mvp-6.0/")}},
               {"schema": 1, "repos": {"Chisanan232/r": dict(entry, future_head_prefixes=[""])}}]
        for name in ("a//b", "stage", "stg", "uat", "qa", "live", "preprod", "integration/uat-1", "env/live"):
            bad.append({"schema": 1, "repos": {"Chisanan232/r": {"branches": [{"branch": name, "version": "1"}]}}})
        for b in bad:
            with self.assertRaises(ValueError, msg=b):
                omg.load_version_policy(b)
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(os.path.realpath(d), "local")
            os.mkdir(root, 0o700)
            dup = os.path.join(root, "dup.json")
            pathlib.Path(dup).write_text('{"schema": 1, "repos": {}, "repos": {}}')
            big = os.path.join(root, "big.json")
            pathlib.Path(big).write_text('{"schema": 1, "repos": {}, "pad": "' + "x" * 70000 + '"}')
            fifo = os.path.join(root, "fifo.json")
            os.mkfifo(fifo, 0o600)
            for f in (dup, big, fifo):
                os.chmod(f, 0o600)
                with self.assertRaises(ValueError, msg=f):
                    omg.read_version_policy(f, True, root)

    def test_codex_review_findings_spe_132(self):
        import os, tempfile
        def listed(name):
            return {"schema": 1, "repos": {"Chisanan232/r": {"branches": [{"branch": name, "version": "1"}]}}}
        for name in ("integration/.hidden/bar", "integration/x.lock/y", "integration/trailing.", "integration/betafix",
                     "stagingeu", "integration/stg2", "uatx", "preprod1", "StG", "STAGE", "integration/stg/x", "qa2", "livefix", "integration/qa2", "integration/livefix",
                     "mvp-staging", "mvp-5-staging", "x-beta", "mvp-beta", "mvp-canary", "mvp-preprod", "mvp-qa", "mvp-QA",
                     "mvp-prd", "prd", "sandbox", "mvp-sandbox", "integration/release-notes", "mvp.live.v2"):
            with self.assertRaises(ValueError, msg=name):
                omg.load_version_policy(listed(name))
        for name in ("integration/mvp-5.0", "mvp-4.0/HORO-2046/integration", "mvp-5.0/integration"):
            omg.load_version_policy(listed(name))
        pre = {"schema": 1, "repos": {"Chisanan232/r": {"branches": [], "future_head_prefixes": ["mvp-6.0/", ".x/"]}}}
        with self.assertRaises(ValueError):
            omg.load_version_policy(pre)
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(os.path.realpath(d), "local")
            os.mkdir(root, 0o700)
            real = os.path.join(root, "real.json")
            pathlib.Path(real).write_text(json.dumps(POLICY))
            os.chmod(real, 0o600)
            link = os.path.join(root, "link.json")
            os.symlink(real, link)
            with self.assertRaises(ValueError):
                omg.read_version_policy(link, True, root)

    def test_malformed_policy_fails_closed(self):
        for bad in ({}, {"schema": 2, "repos": {}}, {"schema": 1, "repos": {"x": {"branches": [{"branch": "../x"}]}}},
                    {"schema": 1, "repos": {"Chisanan232/r": {"branches": "integration/mvp-5.0"}}}):
            with self.assertRaises(ValueError):
                omg.load_version_policy(bad)


class Verify(unittest.TestCase):
    def fake(self, state="MERGED", parents=None, cmp="ahead"):
        f = Fake(parents=parents or [HEAD, "b" * 40], cmp=cmp)
        f.pr = {"state": state, "mergeCommit": {"oid": "m" * 40} if state == "MERGED" else None, "baseRefName": "main"}
        return f

    def test_merge_commit_with_reviewed_head_parent_verifies(self):
        self.assertEqual(omg.verify(self.fake(), "Chisanan232/r", 7, HEAD)["verdict"], "VERIFIED")

    def test_squash_shaped_single_parent_is_a_mismatch(self):
        self.assertEqual(omg.verify(self.fake(parents=["b" * 40]), "Chisanan232/r", 7, HEAD)["verdict"], "MISMATCH")

    def test_head_not_a_parent_is_a_mismatch(self):
        self.assertEqual(omg.verify(self.fake(parents=["c" * 40, "b" * 40]), "Chisanan232/r", 7, HEAD)["verdict"], "MISMATCH")

    def test_open_pr_is_not_merged(self):
        self.assertEqual(omg.verify(self.fake(state="OPEN"), "Chisanan232/r", 7, HEAD)["verdict"], "NOT_MERGED")


class ReviewHardening(unittest.TestCase):
    def refused(self, fake, code, **kw):
        v, reasons, _ = run(fake, **kw)
        self.assertEqual(v, "REFUSE", reasons)
        self.assertTrue(any(r.startswith(code) for r in reasons), reasons)

    def test_read_only_allowlist_rejects_attached_and_header_forms(self):
        for call in (("api", "repos/o/r/issues/1/comments", "-XPOST", "-fbody=x"), ("api", "repos/o/r", "-H", "X-HTTP-Method-Override: POST"),
                     ("api", "repos/o/r", "--hostname", "evil.example"), ("api", "repos/o/r", "--input", "x.json"), ("api", "repos/o/r", "--paginate"),
                     ("api", "repos/o/r", "--method=POST"), ("api", "graphql", "-f", "query=mutation{x}"), ("api", "graphql", "-f", "evil=1"),
                     ("api", "graphql", "-H", "x: y"), ("pr", "merge", "1"), ("repo", "delete", "o/r"), ("api", "user", "-XDELETE")):
            with self.assertRaises(RuntimeError, msg=call):
                omg.read_only(call)

    def test_read_only_allowlist_accepts_the_calls_the_gate_makes(self):
        for call in (("repo", "view", "o/r", "--json", "x"), ("pr", "view", "7", "--repo", "o/r", "--json", "x"), ("api", "user", "--jq", ".login"),
                     ("api", "repos/o/r/compare/main...abc", "--jq", ".behind_by"), ("api", "orgs/o/memberships/me", "--jq", ".role"),
                     ("api", "graphql", "-f", "query=query($o:String!){x}", "-F", "o=o", "-F", "n=7")):
            omg.read_only(call)

    def test_read_only_allowlist_accepts_exactly_the_billing_waiver_reads(self):
        # The waiver's three reads must pass the real filter (the fake-gh tests of BillingCI bypass it).
        for call in (("api", "repos/o/r/check-runs/124163437007"), ("api", "repos/o/r/actions/jobs/124163437345"),
                     ("api", "repos/o/r/check-runs/124163437007/annotations?per_page=100")):
            omg.read_only(call)
        for bad in ("repos/o/r/check-runs/abc", "repos/o/r/check-runs/1/annotations", "repos/o/r/check-runs/1/annotations?per_page=100&x=1",
                    "repos/o/r/actions/jobs/1/logs", "repos/o/r/actions/runs/1/rerun", "repos/o/r/check-runs/1234567890123456",
                    "repos/o/r/check-runs/../x"):
            with self.assertRaises(RuntimeError, msg=bad):
                omg.read_only(("api", bad))
        with self.assertRaises(RuntimeError):
            omg.read_only(("api", "repos/o/r/check-runs/1", "-XPOST"))

    def test_billing_waiver_reads_go_through_the_real_read_only_filter(self):
        calls = []

        def run(*args):
            omg.read_only(args)   # what the real gate runner does before starting gh
            calls.append(args)
            raise RuntimeError("stop after the filter")
        try:
            omg._billing_job_facts(run, "Chisanan232/r", "a" * 40, "test (ubuntu-latest)", "9", "124163437007")
        except Exception:
            pass
        self.assertTrue(calls and calls[0][1].startswith("repos/Chisanan232/r/check-runs/124163437007"), calls)

    def test_repo_name_with_newline_or_dot_segments_is_refused(self):
        for bad in ("Chisanan232/r\n", "Chisanan232/..", "Chisanan232/."):
            v, reasons, _ = omg.evaluate(lambda *a: self.fail("gh called"), bad, 7, OWNERS)
            self.assertEqual(v, "REFUSE", bad)

    def test_error_exit_code_is_distinct_from_wait_and_refuse(self):
        import contextlib
        import io
        def boom(*a):
            raise FileNotFoundError("gh")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = omg.main(["check", "--repo", "Chisanan232/r", "--pr", "1", "--owners", "Chisanan232"], run=boom)
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "ERROR")

    def test_queue_reports_error_per_pr_and_nonzero_exit(self):
        import contextlib
        import io
        def boom(*a):
            raise RuntimeError("no auth")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = omg.main(["queue", "--repo", "Chisanan232/r", "--prs", "1,2", "--owners", "Chisanan232"], run=boom)
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue().count("ERROR"), 2)

    def test_stale_approval_is_not_reported_as_formal_approval(self):
        old = [{"state": "APPROVED", "author": {"login": "rev"}, "commit": {"oid": "c" * 40}}]
        self.assertFalse(run(Fake(reviews=old))[2]["formal_github_approval"])
        cur = [{"state": "APPROVED", "author": {"login": "rev"}, "commit": {"oid": HEAD}}]
        self.assertTrue(run(Fake(reviews=cur))[2]["formal_github_approval"])

    def test_record_inside_a_code_fence_is_ignored(self):
        body = "example:\n```\n" + REVIEW + "\n```\n"
        self.refused(Fake(comments=[{"author": {"login": "chisanan232"}, "body": body}]), "NO_REVIEW_EVIDENCE")

    def test_mixed_pass_and_fail_records_at_one_head_refuse(self):
        fail = REVIEW.replace("verdict=PASS blocking=0", "verdict=FAIL blocking=1")
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}, {"author": {"login": "chisanan232"}, "body": fail}]
        self.refused(Fake(comments=comments), "REVIEW_NOT_CLEAN")

    def test_all_skipped_or_neutral_checks_are_not_a_pass(self):
        self.refused(Fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "SKIPPED"}]), "NO_CI_EVIDENCE")

    def test_status_context_shapes(self):
        ok = [{"context": "legacy", "state": "SUCCESS"}]
        self.assertEqual(run(Fake(statusCheckRollup=ok))[0], "PASS")
        self.refused(Fake(statusCheckRollup=[{"context": "legacy", "state": "FAILURE"}]), "CI_FAILED")
        self.assertEqual(run(Fake(statusCheckRollup=[{"context": "legacy", "state": "PENDING"}]))[0], "WAIT")

    def test_merge_state_values(self):
        self.refused(Fake(mergeStateStatus="UNSTABLE"), "MERGE_STATE_NOT_CLEAN")
        self.assertEqual(run(Fake(mergeStateStatus="UNKNOWN"))[0], "WAIT")

    def test_possibly_truncated_comment_history_refuses(self):
        many = [{"author": {"login": "x"}, "body": "c"}] * 99 + [{"author": {"login": "chisanan232"}, "body": REVIEW}]
        self.refused(Fake(comments=many), "EVIDENCE_TRUNCATED")

    def test_org_is_trusted_only_if_viewer_administers_it(self):
        org = {"nameWithOwner": "horonomy/r", "owner": {"login": "horonomy"}}
        v, reasons, _ = omg.evaluate(Fake(**org), "horonomy/r", 7, OWNERS)
        self.assertEqual((v, reasons), ("PASS", []))
        v, reasons, _ = omg.evaluate(Fake(org_role="member active", **org), "horonomy/r", 7, OWNERS)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any(r.startswith("OWNER_NOT_ADMINISTERED_BY_VIEWER") for r in reasons))

    def test_non_admin_viewer_refuses(self):
        self.refused(Fake(viewerPermission="WRITE"), "NOT_REPO_ADMIN")

    def test_verify_rejects_bad_repo_or_head(self):
        self.assertEqual(omg.verify(lambda *a: self.fail("gh"), "Chisanan232/r\n", 7, HEAD)["verdict"], "ERROR")
        self.assertEqual(omg.verify(lambda *a: self.fail("gh"), "Chisanan232/r", 7, "abc")["verdict"], "ERROR")


class BillingCI(unittest.TestCase):
    """--ci-unavailable with cause=billing waives only Actions jobs that never ran a step (SPE-149)."""

    RECORD = f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"
    TEXT = "The job was not started because recent account payments have failed or your spending limit needs to be increased."

    def job(self, jid, name, conclusion="FAILURE", **over):
        c = {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion,
             "detailsUrl": f"https://github.com/Chisanan232/r/actions/runs/900/job/{jid}"}
        c.update(over)
        return c

    def fake(self, checks, notes, steps=None, record=None, errors=(), **over):
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW},
                    {"author": {"login": "chisanan232"}, "body": record or self.RECORD}]
        fake = Fake(statusCheckRollup=checks, comments=comments, **over)
        base = fake.__call__
        by_id = {str(c["detailsUrl"]).rpartition("/")[2]: c for c in checks if c.get("detailsUrl")}

        def call(*a):
            if a[0] == "api" and a[1].startswith("repos/Chisanan232/r/") and ("/check-runs/" in a[1] or "/actions/jobs/" in a[1]):
                jid = a[1].split("/")[4 if "/check-runs/" in a[1] else 5].split("?")[0]
                if jid in errors:
                    raise RuntimeError("gh api failed: HTTP 502")
                c = by_id[jid]
                if "/annotations" in a[1]:
                    return json.dumps([{"message": m} for m in notes.get(jid, [])])
                if "/check-runs/" in a[1]:
                    return json.dumps({"id": int(jid), "head_sha": HEAD, "name": c["name"], "app": {"slug": "github-actions"}})
                return json.dumps({"id": int(jid), "run_id": 900, "head_sha": HEAD, "name": c["name"],
                                   "steps": (steps or {}).get(jid, [])})
            return base(*a)
        return call

    def verdict(self, *a, **k):
        return run(self.fake(*a, **k), ci_unavailable=True)

    def test_billing_annotated_job_with_no_steps_is_waived_and_never_called_green(self):
        v, reasons, facts = self.verdict([self.job("1", "t"), self.job("2", "lint", "SUCCESS")], {"1": [self.TEXT]})
        self.assertEqual((v, reasons, facts["ci_exception"]), ("PASS", [], "billing"))
        self.assertEqual([c["name"] for c in facts["ci_unavailable_checks"]], ["t"])
        self.assertEqual(facts["ci_unavailable_checks"][0]["conclusion"], "FAILURE")
        self.assertTrue(facts["ci_verdict"].startswith("NOT_A_CI_PASS"))

    def test_skipped_only_steps_count_as_not_executed(self):
        v, _, _ = self.verdict([self.job("1", "t")], {"1": [self.TEXT]}, steps={"1": [{"conclusion": "skipped"}]})
        self.assertEqual(v, "PASS")

    def test_matrix_sibling_cancelled_by_a_billing_blocked_job_is_waived(self):
        cancel = 'The strategy configuration was canceled because "t._py311" failed'
        v, _, facts = self.verdict([self.job("1", "t (py311)"), self.job("2", "t (py312)", "CANCELLED")],
                                   {"1": [self.TEXT], "2": [cancel]})
        self.assertEqual(v, "PASS")
        self.assertEqual(len(facts["ci_unavailable_checks"]), 2)

    def test_matrix_sibling_of_a_real_failure_refuses(self):
        cancel = 'The strategy configuration was canceled because "t._py311" failed'
        for notes in ({"1": ["Process completed with exit code 1."], "2": [cancel]}, {"2": [cancel]}):
            with self.subTest(notes=notes):
                checks = [self.job("1", "t (py311)"), self.job("2", "t (py312)", "CANCELLED")]
                v, reasons, _ = self.verdict(checks, notes, steps={"1": [{"conclusion": "failure"}]})
                self.assertEqual(v, "REFUSE")
                self.assertTrue(any(r.startswith("CI_FAILED") for r in reasons))

    def test_a_job_that_executed_a_step_refuses_even_with_the_billing_text(self):
        v, reasons, _ = self.verdict([self.job("1", "t")], {"1": [self.TEXT]}, steps={"1": [{"conclusion": "success"}]})
        self.assertEqual(v, "REFUSE")
        self.assertTrue(reasons[0].startswith("CI_FAILED"))

    def test_non_actions_check_failing_refuses(self):
        sonar = {"__typename": "StatusContext", "context": "SonarCloud", "state": "FAILURE"}
        v, reasons, _ = self.verdict([self.job("1", "t"), sonar], {"1": [self.TEXT]})
        self.assertEqual(v, "REFUSE")
        sonar_run = self.job("3", "Sonar", detailsUrl="https://sonarcloud.io/dashboard?id=x")
        self.assertEqual(self.verdict([self.job("1", "t"), sonar_run], {"1": [self.TEXT]})[0], "REFUSE")

    def test_a_non_failed_but_unsuccessful_check_blocks_the_waiver(self):
        sonar = {"__typename": "StatusContext", "context": "SonarCloud", "state": "ERRORISH"}
        v, reasons, _ = self.verdict([self.job("1", "t"), sonar], {"1": [self.TEXT]})
        self.assertEqual(v, "REFUSE")

    def test_pending_check_still_waits(self):
        pend = {"__typename": "CheckRun", "name": "slow", "status": "IN_PROGRESS", "conclusion": ""}
        self.assertEqual(self.verdict([self.job("1", "t"), pend], {"1": [self.TEXT]})[0], "WAIT")

    def test_flag_record_head_and_cause_are_all_required(self):
        checks, notes = [self.job("1", "t")], {"1": [self.TEXT]}
        v, reasons, _ = run(self.fake(checks, notes))  # no flag
        self.assertEqual(v, "REFUSE")
        for record in (self.RECORD.replace(HEAD, "d" * 40), self.RECORD.replace("billing", "quota"),
                       self.RECORD.replace("billing", "outage"), self.RECORD.replace("PASS", "FAIL")):
            with self.subTest(record=record):
                v, reasons, _ = self.verdict(checks, notes, record=record)
                self.assertEqual(v, "REFUSE")
                self.assertTrue(reasons[0].startswith("CI_FAILED"))
        v, _, _ = omg.evaluate(self.fake(checks, notes, record="unrelated"), "Chisanan232/r", 7, OWNERS, ci_unavailable=True)
        self.assertEqual(v, "REFUSE")

    def test_a_later_non_billing_record_overrides_a_billing_one(self):
        fake = self.fake([self.job("1", "t")], {"1": [self.TEXT]})
        # rebuild with both records, billing first then quota
        comments = [{"author": {"login": "chisanan232"}, "body": b} for b in (REVIEW, self.RECORD, self.RECORD.replace("billing", "quota"))]
        f2 = Fake(statusCheckRollup=[self.job("1", "t")], comments=comments)
        self.assertEqual(run(lambda *a: fake(*a) if a[:2] != ("pr", "view") else f2(*a), ci_unavailable=True)[0], "REFUSE")

    def test_unknown_missing_or_truncated_annotation_refuses(self):
        for notes in ({"1": ["Process completed with exit code 1."]}, {}, {"1": [self.TEXT] * 100}):
            with self.subTest(n=len(notes.get("1", []))):
                self.assertEqual(self.verdict([self.job("1", "t")], notes)[0], "REFUSE")

    def test_api_error_refuses(self):
        v, reasons, facts = self.verdict([self.job("1", "t")], {"1": [self.TEXT]}, errors=("1",))
        self.assertEqual(v, "REFUSE")
        self.assertIn("ci_unavailable_refused", facts)

    def test_only_cancelled_and_failed_actions_jobs_for_this_repo_qualify(self):
        for over in ({"conclusion": "TIMED_OUT"}, {"__typename": "StatusContext"},
                     {"detailsUrl": "https://github.com/other/r/actions/runs/900/job/1"}, {"detailsUrl": ""}):
            with self.subTest(over=over):
                self.assertEqual(self.verdict([self.job("1", "t", **over)], {"1": [self.TEXT]})[0], "REFUSE")


if __name__ == "__main__":
    unittest.main()
