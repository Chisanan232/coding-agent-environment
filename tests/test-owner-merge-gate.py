#!/usr/bin/env python3
"""Unit tests for scripts/owner-merge-gate.py with a fake gh runner (SPE-115).

Live behavior (the host classifier, a real merge) is covered by the acceptance runs recorded in
the ticket, not here.
"""
import calendar
import hashlib
import importlib.util
import json
import base64
import pathlib
import time
import unittest

SPEC = importlib.util.spec_from_file_location("omg", pathlib.Path(__file__).resolve().parents[1] / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(omg)

HEAD = "a" * 40
SRC = [{"path": "src/a.py"}]
OWNERS = ["Chisanan232", "horonomy", "ai-agent-assembly"]
BASE_SHA = "b" * 40
BASE_TREE = "c" * 40
HEAD_TREE = "d" * 40
NOW = 1_800_000_000
REVIEW = f"OWNER-MERGE-REVIEW head={HEAD} verdict=PASS blocking=0 reviewer=independent-agent-1"


def green():
    return [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


class Fake:
    """Answers the gh calls the gate makes from a mutable state dict."""

    def __init__(self, **over):
        self.pr = {"number": 7, "state": "OPEN", "isDraft": False, "title": "feat: x", "baseRefName": "main", "headRefName": "feat/x", "headRefOid": HEAD,
                   "baseRefOid": BASE_SHA,
                   "author": {"login": "chisanan232"}, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                   "reviewDecision": "", "reviews": [], "isCrossRepository": False, "mergeCommit": None,
                   "comments": [{"author": {"login": "chisanan232"}, "body": REVIEW}],
                   "statusCheckRollup": green()}
        self.repo = {"nameWithOwner": "Chisanan232/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
                     "viewerPermission": "ADMIN", "owner": {"login": "Chisanan232"}, "isPrivate": True}
        self.behind, self.threads, self.me, self.more_threads = 0, [], "chisanan232", False
        self.parents, self.cmp, self.org_role = [HEAD, "b" * 40], "ahead", "admin active"
        self.merge_base, self.message, self.runs = BASE_SHA, "chore: change [skip actions]", 0
        self.pr_reads = 0
        self.calls = []
        self.tree_commits = {BASE_SHA: BASE_TREE, HEAD: HEAD_TREE}
        self.tree_entries = {
            BASE_TREE: [{"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "1" * 40}],
            HEAD_TREE: [{"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "1" * 40}],
        }
        self.truncated_trees = set()
        for k, v in over.items():
            target = self.repo if k in self.repo else self.pr if k in self.pr else None
            if target is None:
                setattr(self, k, v)
            else:
                target[k] = v

    def __call__(self, *a):
        self.calls.append(a)
        if a[:2] == ("repo", "view"):
            return json.dumps(self.repo)
        if a[:2] == ("api", "user"):
            return self.me + "\n"
        if a[:2] == ("pr", "view"):
            self.pr_reads += 1
            pr_view = dict(self.pr)
            if self.pr_reads > 1 and getattr(self, "pr_after_first_view", None):
                pr_view.update(self.pr_after_first_view)
            return json.dumps(pr_view)
        if a[0] == "api" and a[1].endswith("/files?per_page=100"):
            listed = getattr(self, "api_files", None)
            if listed is None:
                listed = [{"filename": f["path"], "status": "modified"} for f in self.pr.get("files") or []]
            return json.dumps(listed)
        if a[0] == "api" and "/actions/runs?head_sha=" in a[1]:
            self.runs_reads = getattr(self, "runs_reads", []) + [a[1]]
            return f"{self.runs}\n"
        if a[0] == "api" and "/git/commits/" in a[1]:
            sha = a[1].rsplit("/", 1)[1]
            if sha not in self.tree_commits:
                raise AssertionError(a)
            return self.tree_commits[sha] + "\n"
        if a[0] == "api" and "/git/trees/" in a[1]:
            tree = a[1].split("/git/trees/", 1)[1].split("?", 1)[0]
            if tree not in self.tree_entries:
                raise AssertionError(a)
            return json.dumps({"truncated": tree in self.truncated_trees, "tree": self.tree_entries[tree]})
        if a[0] == "api" and "/git/blobs/" in a[1]:
            sha = a[1].rsplit("/", 1)[1]
            data = self.git_blobs[sha]
            if a[-1] == ".content":
                encoded = getattr(self, "blob_content_override", {}).get(sha, base64.b64encode(data).decode("ascii"))
                return "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + "\n\n"
            raise AssertionError(a)
        if a[0] == "api" and "/compare/" in a[1] and "merge_base_commit" in a[3]:
            return self.merge_base + "\n"
        if a[0] == "api" and "/commits/" in a[1] and ".commit.message" in a[3]:
            return self.message + "\n"
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

    def test_review_completion_time_is_taken_from_the_review_comment(self):
        stamp = "2026-11-14T02:47:00Z"
        v, _, facts = run(Fake(comments=[{"author": {"login": "chisanan232"}, "body": REVIEW, "createdAt": stamp}]))
        self.assertEqual((v, facts["review_comment_at"]), ("PASS", stamp))
        self.assertNotIn("review_comment_at", run(Fake())[2])         # a comment without a time claims none

    def test_evidence_time_comes_from_the_newest_review_comment_for_the_head_only(self):
        mine = {"login": "chisanan232"}
        other = f"OWNER-MERGE-REVIEW head={'c' * 40} verdict=PASS blocking=0 reviewer=independent-agent-1"
        fake = Fake(comments=[{"author": mine, "body": REVIEW, "createdAt": "2026-11-14T01:00:00Z"},
                              {"author": mine, "body": other, "createdAt": "2026-11-14T02:00:00Z"},      # another head: ignored
                              {"author": {"login": "someone-else"}, "body": REVIEW, "createdAt": "2026-11-14T03:00:00Z"},   # not me: ignored
                              {"author": mine, "body": REVIEW, "createdAt": "2026-11-14T02:30:00Z"}])
        self.assertEqual(omg.review_completed_at(fake, "Chisanan232/r", 7, HEAD), "2026-11-14T02:30:00Z")

    def test_evidence_time_refuses_without_a_review_or_after_the_head_moved(self):
        for fake in (Fake(comments=[]), Fake(comments=[{"author": {"login": "chisanan232"}, "body": REVIEW}]),
                     Fake(headRefOid="c" * 40, comments=[{"author": {"login": "chisanan232"}, "body": REVIEW, "createdAt": "2026-11-14T02:30:00Z"}])):
            with self.subTest(), self.assertRaises(ValueError):
                omg.review_completed_at(fake, "Chisanan232/r", 7, HEAD)

    def test_evidence_json_takes_an_explicit_completion_time_and_never_a_future_one(self):
        args = ("o/r", 7, "main", HEAD, "agent-rv1")
        self.assertEqual(omg.evidence_json(*args, reviewed_at="2026-11-14T02:47:00Z", now=1794624480)["reviewed_at"], "2026-11-14T02:47:00Z")
        for bad in ("2026-11-14 02:47:00", "yesterday", "2026-11-14T02:47:00+00:00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                omg.evidence_json(*args, reviewed_at=bad, now=1794624480)
        with self.assertRaises(ValueError):
            omg.evidence_json(*args, reviewed_at="2026-11-14T03:47:00Z", now=1794624480)   # an hour ahead of "now"

    def test_evidence_line_can_emit_only_the_closed_controlled_review_extension(self):
        line = omg.evidence_line(HEAD, "independent-agent-1", base_sha=BASE_SHA,
                                 recipe_sha256="c" * 64, scope="project-scripts",
                                 authorization="standing-project-work")
        self.assertEqual(line, f"OWNER-MERGE-REVIEW head={HEAD} verdict=PASS blocking=0 reviewer=independent-agent-1 "
                         f"base_sha={BASE_SHA} recipe_sha256={'c' * 64} scope=project-scripts authorization=standing-project-work")
        for args in ({"base_sha": BASE_SHA}, {"base_sha": "x" * 40, "recipe_sha256": "c" * 64,
                     "scope": "project-scripts", "authorization": "standing-project-work"},
                     {"base_sha": BASE_SHA, "recipe_sha256": "c" * 64, "scope": "any", "authorization": "standing-project-work"},
                     {"base_sha": BASE_SHA, "recipe_sha256": "c" * 64, "scope": "project-scripts", "authorization": "per-pr"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                omg.evidence_line(HEAD, "independent-agent-1", **args)

    def test_controlled_inventory_uses_only_allowlisted_read_endpoints(self):
        for path in (f"repos/Chisanan232/r/git/commits/{HEAD}",
                     f"repos/Chisanan232/r/git/trees/{HEAD_TREE}?recursive=1",
                     f"repos/Chisanan232/r/git/blobs/{'e' * 40}"):
            omg.read_only(("api", path, "--jq", "."))
        for path in (f"repos/Chisanan232/r/git/trees/{HEAD_TREE}?recursive=0",
                     "repos/Chisanan232/r/git/trees/not-a-sha?recursive=1",
                     "repos/Chisanan232/r/git/blobs/not-a-sha"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                omg.read_only(("api", path, "--jq", "."))

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


LF_CONFIG = {"schema": 1, "repos": {"Chisanan232/r": {
    "checks": [{"name": "suite", "argv": ["python3", "-m", "pytest"]}, {"name": "lint", "argv": ["ruff", "check", "."]}],
    "max_age_hours": 24, "pinned_paths": ["scripts/ci/*"], "not_covered": ["windows and ubuntu matrix legs"]}}}
CONTROLLED_LF_CONFIG = {"schema": 1, "repos": {"Chisanan232/r": {
    "checks": LF_CONFIG["repos"]["Chisanan232/r"]["checks"], "max_age_hours": 24,
    "pinned_paths": ["scripts/*"], "controlled_script_paths": ["scripts/*"],
    "not_covered": ["windows and ubuntu matrix legs"]}}}


def controlled_recipe_sha(config=CONTROLLED_LF_CONFIG):
    entry = omg.load_local_first(config)["chisanan232/r"]
    recipe = {"checks": [{"name": name, "argv": list(argv)} for name, argv in entry["checks"]],
              "env": dict(entry.get("env") or {})}
    raw = json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def controlled_review(fake, *, base_sha=BASE_SHA, recipe_sha256=None, scope="project-scripts",
                      authorization="standing-project-work", verdict="PASS", blocking=0,
                      reviewer="independent-agent-1"):
    return (f"OWNER-MERGE-REVIEW head={fake.pr['headRefOid']} verdict={verdict} blocking={blocking} "
            f"reviewer={reviewer} base_sha={base_sha} recipe_sha256={recipe_sha256 or controlled_recipe_sha()} "
            f"scope={scope} authorization={authorization}")


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def lf_record(**over):
    rec = {"schema": 1, "repo": "Chisanan232/r", "pr": 7, "base": "main", "base_sha": BASE_SHA, "head": HEAD, "env": {},
           "context": {"PULL_BRANCH": "feat/x", "PR_TITLE": "feat: x", "GIT_BASE": BASE_SHA},
           "commands": [{"name": "suite", "argv": ["python3", "-m", "pytest"], "exit_code": 0, "started_at": iso(NOW - 900),
                         "finished_at": iso(NOW - 600), "log_sha256": "1" * 64},
                        {"name": "lint", "argv": ["ruff", "check", "."], "exit_code": 0, "started_at": iso(NOW - 590),
                         "finished_at": iso(NOW - 580), "log_sha256": "2" * 64}]}
    rec.update(over)
    return rec


def lf_comment(rec=None, body=None, **over):
    comment = {"author": {"login": "chisanan232"}, "body": body if body is not None else "OWNER-MERGE-LOCAL-CI " + json.dumps(rec or lf_record()),
               "createdAt": iso(NOW - 60), "includesCreatedEdit": False, "isMinimized": False}
    comment.update(over)
    return comment


class LocalFirst(unittest.TestCase):
    """Third CI basis: the head's own skip directive left no hosted CI, and owner-fixed local commands passed at this exact head."""

    def fake(self, *records, files=None, **over):
        comments = [{"author": {"login": "chisanan232"}, "body": REVIEW}] + (list(records) if records else [lf_comment()])
        fake = Fake(statusCheckRollup=over.pop("statusCheckRollup", []), comments=comments, **over)
        fake.pr["files"] = SRC if files is None else files
        return fake

    def evaluate(self, fake, config=LF_CONFIG, **kw):
        kw.setdefault("ci_exception", True)
        return omg.evaluate(fake, "Chisanan232/r", 7, OWNERS, local_first=omg.load_local_first(config) if config else None, now=NOW, **kw)

    def refused(self, fake, code, **kw):
        v, reasons, facts = self.evaluate(fake, **kw)
        self.assertEqual(v, "REFUSE", reasons)
        self.assertTrue(any(r.startswith(code) for r in reasons), reasons)
        self.assertNotIn("ci_exception", facts)

    def controlled_fake(self, review=None, files=None, api_files=None, **over):
        path = "scripts/update.py"
        changed_files = files if files is not None else [{"path": path}]
        fake = self.fake(lf_comment(), files=changed_files, **over)
        fake.api_files = api_files if api_files is not None else [
            {"filename": item["path"], "status": "modified"} for item in changed_files]
        fake.tree_commits = {BASE_SHA: BASE_TREE, HEAD: HEAD_TREE}
        fake.tree_entries = {
            BASE_TREE: [{"path": path, "mode": "100644", "type": "blob", "sha": "1" * 40, "size": 20}],
            HEAD_TREE: [{"path": path, "mode": "100644", "type": "blob", "sha": "2" * 40, "size": 20}],
        }
        fake.pr["comments"] = [lf_comment(), {
            "author": {"login": "chisanan232"}, "body": review or controlled_review(fake),
            "createdAt": iso(NOW - 30), "includesCreatedEdit": False, "isMinimized": False}]
        return fake

    def controlled_evaluate(self, fake, config=CONTROLLED_LF_CONFIG, **kw):
        kw.setdefault("ci_exception", True)
        return omg.evaluate(fake, "Chisanan232/r", 7, OWNERS,
                            local_first=omg.load_local_first(config), now=NOW, **kw)

    def controlled_refused(self, fake, code, **kw):
        verdict, reasons, facts = self.controlled_evaluate(fake, **kw)
        self.assertEqual(verdict, "REFUSE", reasons)
        self.assertTrue(any(reason.startswith(code) for reason in reasons), reasons)

    def package_fake(self, old, new, *, path="package.json", modes=("100644", "100644"),
                     status="modified", review=None):
        fake = self.controlled_fake(files=[{"path": path}], api_files=[{"filename": path, "status": status}])
        old_bytes = old.encode("utf-8") if isinstance(old, str) else old
        new_bytes = new.encode("utf-8") if isinstance(new, str) else new
        def oid(data):
            return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        old_oid, new_oid = oid(old_bytes), oid(new_bytes)
        fake.tree_entries[BASE_TREE] = [{"path": path, "mode": modes[0], "type": "blob", "sha": old_oid, "size": len(old_bytes)}]
        fake.tree_entries[HEAD_TREE] = [{"path": path, "mode": modes[1], "type": "blob", "sha": new_oid, "size": len(new_bytes)}]
        fake.git_blobs = {old_oid: old_bytes, new_oid: new_bytes}
        fake.pr["comments"][-1]["body"] = review or controlled_review(fake)
        return fake

    def package_config(self, path="package.json"):
        entry = dict(CONTROLLED_LF_CONFIG["repos"]["Chisanan232/r"], pinned_paths=[path],
                     controlled_package_scripts={path: ["test"]})
        return {"schema": 1, "repos": {"Chisanan232/r": entry}}

    def test_controlled_package_test_script_change_passes_after_bounded_immutable_blob_reads(self):
        old = '{"name":"fixture","scripts":{"test":"python3 -m unittest"},"version":"1.0"}\n'
        new = '{"name":"fixture","scripts":{"test":"python3 -m unittest -v"},"version":"1.0"}\n'
        fake = self.package_fake(old, new)
        verdict, reasons, facts = self.controlled_evaluate(fake, config=self.package_config())
        self.assertEqual((verdict, reasons), ("PASS", []))
        self.assertEqual(facts["controlled_script_review"]["scope"], "project-scripts")
        self.assertEqual(len([call for call in fake.calls if call[0] == "api" and "/git/blobs/" in call[1]]), 2)

    def test_actual_gh_wrapped_content_terminators_and_short_final_line_are_accepted(self):
        old = '{"scripts":{"test":"old"},"padding":"' + "x" * 40 + '"}'
        new = '{"scripts":{"test":"new"},"padding":"' + "x" * 40 + '"}'
        fake = self.package_fake(old, new)
        verdict, reasons, _ = self.controlled_evaluate(fake, config=self.package_config())
        self.assertEqual((verdict, reasons), ("PASS", []))
        content_calls = [call for call in fake.calls if call[0] == "api" and "/git/blobs/" in call[1] and call[-1] == ".content"]
        for call in content_calls:
            raw = fake.git_blobs[call[1].rsplit("/", 1)[1]]
            encoded = base64.b64encode(raw)
            self.assertEqual(len(encoded) % 60, 48)
            self.assertTrue(fake("api", call[1], "--jq", ".content").endswith("\n\n"))

    def test_controlled_package_manifest_rejects_changes_outside_test_value(self):
        base = {"name": "fixture", "scripts": {"test": "python3 -m unittest", "build": "make"}, "version": "1.0"}
        cases = [
            dict(base, version="2.0"),
            {"name": "fixture", "scripts": {"test": "python3 -m unittest -v"}},
            {"name": "fixture", "scripts": {"test": "python3 -m unittest -v", "pretest": "echo hook", "build": "make"}, "version": "1.0"},
            {"name": "fixture", "scripts": {"test": "python3 -m unittest -v", "build": "make"}, "dependencies": {"x": "1"}, "version": "1.0"},
            {"name": "fixture", "scripts": {"test": "python3 -m unittest -v", "build": "make"}, "engines": {"node": ">=22"}, "version": "1.0"},
        ]
        original = json.dumps(base, separators=(",", ":"))
        for changed in cases:
            with self.subTest(changed=changed):
                fake = self.package_fake(original, json.dumps(changed, separators=(",", ":")))
                self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config())

    def test_controlled_package_manifest_rejects_duplicate_nonstandard_numeric_and_invalid_utf8(self):
        bad_pairs = [
            ('{"scripts":{"test":"old","test":"new"}}', '{"scripts":{"test":"new"}}'),
            ('{"n":1,"scripts":{"test":"old"}}', '{"n":1.0,"scripts":{"test":"new"}}'),
            ('{"n":0,"scripts":{"test":"old"}}', '{"n":-0,"scripts":{"test":"new"}}'),
            ('{"n":0.0,"scripts":{"test":"old"}}', '{"n":-0.0,"scripts":{"test":"new"}}'),
            ('{"n":1.00000000000000001,"scripts":{"test":"old"}}',
             '{"n":1.00000000000000002,"scripts":{"test":"new"}}'),
            ('{"n":1,"scripts":{"test":"old"}}', '{"n":1e400,"scripts":{"test":"new"}}'),
            ('{"scripts":{"test":"old"}}', '{"scripts":{"test":"new"},"x":NaN}'),
            ('{"scripts":{"test":"old"}}', b'{"scripts":{"test":"new"},"x":"ok"}\xff'),
        ]
        for old, new in bad_pairs:
            with self.subTest(new=new):
                fake = self.package_fake(old, new)
                self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config())

    def test_controlled_package_manifest_rejects_path_mode_status_and_blob_identity_bypass(self):
        old = '{"scripts":{"test":"old"}}'
        new = '{"scripts":{"test":"new"}}'
        for path, modes, status in (("pkg/package.json", ("100644", "100755"), "modified"),
                                    ("package.json", ("100644", "100644"), "renamed")):
            fake = self.package_fake(old, new, path=path, modes=modes, status=status)
            self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config(path))
        fake = self.package_fake(old, new)
        fake.git_blobs[next(iter(fake.git_blobs))] = b"different bytes"
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config())

    def test_controlled_package_path_schema_is_exact_and_optional(self):
        self.assertNotIn("controlled_package_scripts", omg.load_local_first(LF_CONFIG)["chisanan232/r"])
        self.assertEqual(omg.load_local_first(self.package_config())["chisanan232/r"]["controlled_package_scripts"],
                         {"package.json": ("test",)})
        for path, scripts in (("**/package.json", ["test"]), ("../package.json", ["test"]),
                              ("pkg/../package.json", ["test"]), ("pkg/package-lock.json", ["test"]),
                              ("pkg/package.json", ["pretest"]), ("pkg/package.json", ["test", "build"])):
            entry = dict(CONTROLLED_LF_CONFIG["repos"]["Chisanan232/r"], controlled_package_scripts={path: scripts})
            with self.subTest(path=path, scripts=scripts), self.assertRaises(ValueError):
                omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": entry}})

    def test_controlled_package_manifest_requires_a_current_extended_review(self):
        fake = self.package_fake('{"scripts":{"test":"old"}}', '{"scripts":{"test":"new"}}', review=REVIEW)
        self.controlled_refused(fake, "CONTROLLED_REVIEW_REQUIRED", config=self.package_config())

    def test_invalid_tree_manifest_sizes_refuse_before_any_blob_request(self):
        for tree_id in (BASE_TREE, HEAD_TREE):
            for size in ("unknown", True, -1, omg.POLICY_MAX_BYTES + 1, None):
                with self.subTest(tree_id=tree_id, size=size):
                    fake = self.package_fake('{"scripts":{"test":"old"}}', '{"scripts":{"test":"new"}}')
                    entry = fake.tree_entries[tree_id][0]
                    if size is None:
                        del entry["size"]
                    else:
                        entry["size"] = size
                    self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config())
                    self.assertFalse(any(call[0] == "api" and "/git/blobs/" in call[1] for call in fake.calls))

    def test_malformed_wrapped_base64_refuses(self):
        fake = self.package_fake('{"scripts":{"test":"old"}}', '{"scripts":{"test":"new"}}')
        head_blob = fake.tree_entries[HEAD_TREE][0]["sha"]
        fake.blob_content_override = {head_blob: "not*base64"}
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=self.package_config())

    def test_controlled_script_change_passes_only_with_extended_current_review(self):
        fake = self.controlled_fake()
        verdict, reasons, facts = self.controlled_evaluate(fake)
        self.assertEqual((verdict, reasons), ("PASS", []))
        self.assertEqual(facts["ci_exception"], "local-first")
        self.assertEqual(facts["hosted_checks"], "NOT_RUN")
        self.assertTrue(facts["ci_verdict"].startswith("NOT_A_CI_PASS"))
        self.assertEqual(facts["controlled_script_review"], {
            "base_sha": BASE_SHA, "recipe_sha256": controlled_recipe_sha(),
            "scope": "project-scripts", "authorization": "standing-project-work"})
        self.assertIn(f"repos/Chisanan232/r/git/trees/{HEAD_TREE}?recursive=1", [a[1] for a in fake.calls if a[0] == "api"])

    def test_full_hosted_ci_does_not_need_controlled_review(self):
        fake = self.controlled_fake(review=REVIEW, statusCheckRollup=green())
        verdict, reasons, facts = self.controlled_evaluate(fake)
        self.assertEqual((verdict, reasons), ("PASS", []))
        self.assertNotIn("ci_exception", facts)
        self.assertFalse(any("/git/trees/" in a[1] for a in fake.calls if a[0] == "api"))

    def test_controlled_change_rejects_missing_legacy_stale_and_invalid_review(self):
        fake = self.controlled_fake(review=REVIEW)
        self.controlled_refused(fake, "CONTROLLED_REVIEW_REQUIRED")
        fake = self.controlled_fake(review=controlled_review(fake, base_sha=BASE_SHA))
        fake.pr["headRefOid"] = "e" * 40
        self.controlled_refused(fake, "CONTROLLED_REVIEW_STALE")
        fake = self.controlled_fake(review=controlled_review(fake, base_sha="c" * 40))
        self.controlled_refused(fake, "CONTROLLED_REVIEW_STALE")
        fake = self.controlled_fake(review=controlled_review(fake, recipe_sha256="c" * 64))
        self.controlled_refused(fake, "CONTROLLED_REVIEW_STALE")
        fake = self.controlled_fake(review=controlled_review(fake, authorization="per-pr"))
        self.controlled_refused(fake, "CONTROLLED_REVIEW_INVALID")
        fake = self.controlled_fake(review=controlled_review(fake, reviewer="author"))
        self.controlled_refused(fake, "REVIEW_NOT_INDEPENDENT")
        fake = self.controlled_fake(review=controlled_review(fake, verdict="FAIL", blocking=1))
        self.controlled_refused(fake, "REVIEW_NOT_CLEAN")

    def test_controlled_change_rejects_ambiguous_review(self):
        fake = self.controlled_fake()
        fake.pr["comments"].append(dict(fake.pr["comments"][-1]))
        self.controlled_refused(fake, "CONTROLLED_REVIEW_AMBIGUOUS")

    def test_controlled_review_refresh_preserves_clean_history(self):
        for old in (REVIEW, controlled_review(Fake(), base_sha="c" * 40),
                    controlled_review(Fake(), recipe_sha256="c" * 64)):
            with self.subTest(old=old):
                fake = self.controlled_fake()
                fake.pr["comments"].insert(0, {"author": {"login": "chisanan232"}, "body": old})
                verdict, reasons, _ = self.controlled_evaluate(fake)
                self.assertEqual((verdict, reasons), ("PASS", []))

    def test_controlled_review_refresh_keeps_applicable_failures_blocking(self):
        fake = self.controlled_fake()
        records = (REVIEW.replace("verdict=PASS blocking=0", "verdict=FAIL blocking=1"),
                   controlled_review(fake, verdict="FAIL", blocking=1),
                   controlled_review(fake, blocking=1))
        for old in records:
            with self.subTest(old=old):
                fake = self.controlled_fake()
                fake.pr["comments"].insert(0, {"author": {"login": "chisanan232"}, "body": old})
                self.controlled_refused(fake, "REVIEW_NOT_CLEAN")

    def test_controlled_inventory_malformed_paths_return_structured_refusal(self):
        for bad in ({"path": None}, {"path": 42}, {"path": ""}, {}):
            with self.subTest(view=bad):
                fake = self.controlled_fake(files=[bad, {"path": "scripts/update.py"}], api_files=[
                    {"filename": "scripts/unknown.py", "status": "modified"},
                    {"filename": "scripts/update.py", "status": "modified"}])
                self.controlled_refused(fake, "LOCAL_FIRST_SCOPE")
        for bad in (None, 42, {}, {"filename": None}, {"filename": 42}, {"filename": ""}):
            with self.subTest(rest=bad):
                self.controlled_refused(self.controlled_fake(api_files=[bad]), "LOCAL_FIRST_SCOPE")

    def test_controlled_snapshot_is_rechecked_after_late_api_work(self):
        for phase in ("/actions/runs?", "/commits/"):
            for change in ({"baseRefOid": "e" * 40}, {"headRefOid": "e" * 40},
                           {"baseRefName": "other"}):
                with self.subTest(phase=phase, change=change):
                    fake = self.controlled_fake()
                    def late_change(*args):
                        result = fake(*args)
                        if args[0] == "api" and phase in args[1]:
                            fake.pr_after_first_view = change
                        return result
                    verdict, reasons, facts = omg.evaluate(late_change, "Chisanan232/r", 7, OWNERS,
                        local_first=omg.load_local_first(CONTROLLED_LF_CONFIG), ci_exception=True, now=NOW)
                    self.assertEqual(verdict, "REFUSE", reasons)
                    self.assertTrue(any(r.startswith("CONTROLLED_SCOPE_CHANGED") for r in reasons), reasons)
                    self.assertNotIn("ci_exception", facts)

    def test_controlled_inventory_and_record_share_immutable_merge_base(self):
        fake = self.controlled_fake()
        verdict, reasons, _ = self.controlled_evaluate(fake)
        self.assertEqual((verdict, reasons), ("PASS", []))
        comparisons = [a for a in fake.calls if a[0] == "api" and "/compare/" in a[1]
                       and "merge_base_commit" in a[3]]
        self.assertEqual([a[1] for a in comparisons], [f"repos/Chisanan232/r/compare/{BASE_SHA}...{HEAD}"])

    def test_controlled_script_links_cannot_select_unreviewed_execution_bytes(self):
        for old_mode in (None, "100644", "120000"):
            with self.subTest(old_mode=old_mode):
                fake = self.controlled_fake()
                if old_mode is None:
                    fake.tree_entries[BASE_TREE] = []
                    fake.api_files[0]["status"] = "added"
                else:
                    fake.tree_entries[BASE_TREE][0]["mode"] = old_mode
                fake.tree_entries[HEAD_TREE][0]["mode"] = "120000"
                verdict, reasons, facts = self.controlled_evaluate(fake)
                self.assertEqual(verdict, "REFUSE", reasons)
                self.assertTrue(any(r.startswith("LOCAL_FIRST_SCOPE") for r in reasons), reasons)
                self.assertNotIn("ci_exception", facts)

    def test_controlled_script_regular_modes_and_link_removal_remain_supported(self):
        for mode in ("100644", "100755"):
            with self.subTest(mode=mode):
                fake = self.controlled_fake()
                fake.tree_entries[BASE_TREE][0]["mode"] = "120000"
                fake.tree_entries[HEAD_TREE][0]["mode"] = mode
                verdict, reasons, _ = self.controlled_evaluate(fake)
                self.assertEqual((verdict, reasons), ("PASS", []))
        fake = self.controlled_fake()
        fake.tree_entries[BASE_TREE][0]["mode"] = "120000"
        fake.tree_entries[HEAD_TREE] = []
        fake.api_files[0]["status"] = "removed"
        verdict, reasons, _ = self.controlled_evaluate(fake)
        self.assertEqual((verdict, reasons), ("PASS", []))

    def test_controlled_change_rejects_base_or_head_changed_during_evaluation(self):
        fake = self.controlled_fake()
        fake.pr_after_first_view = {"baseRefOid": "c" * 40}
        self.controlled_refused(fake, "CONTROLLED_SCOPE_CHANGED")
        fake = self.controlled_fake()
        fake.pr_after_first_view = {"headRefOid": "c" * 40}
        self.controlled_refused(fake, "CONTROLLED_SCOPE_CHANGED")

    def test_every_changed_pin_requires_controlled_opt_in_and_workflow_never_opts_in(self):
        config = {"schema": 1, "repos": {"Chisanan232/r": dict(CONTROLLED_LF_CONFIG["repos"]["Chisanan232/r"],
            controlled_script_paths=["scripts/allowed/*"])}}
        fake = self.controlled_fake()
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=config)
        fake = self.controlled_fake(files=[{"path": ".github/workflows/ci.yml"}])
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE")
        fake = self.controlled_fake(files=[{"path": "scripts/allowed/a.py"}, {"path": "scripts/update.py"}],
                                    api_files=[{"filename": "scripts/allowed/a.py", "status": "modified"},
                                               {"filename": "scripts/update.py", "status": "modified"}])
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE", config=config)
        self.controlled_refused(self.controlled_fake(isCrossRepository=True), "LOCAL_FIRST_SCOPE")

    def test_controlled_change_requires_complete_matching_tree_and_file_inventory(self):
        fake = self.controlled_fake()
        fake.truncated_trees.add(HEAD_TREE)
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE")
        fake = self.controlled_fake()
        fake.tree_entries[HEAD_TREE] = []
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE")
        fake = self.controlled_fake()
        fake.api_files = [{"filename": "scripts/other.py", "status": "modified"}]
        self.controlled_refused(fake, "LOCAL_FIRST_SCOPE")

    def test_controlled_review_cannot_override_a_failed_ci_check(self):
        self.controlled_refused(self.controlled_fake(statusCheckRollup=[{
            "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")

    def test_matching_record_passes_and_is_never_reported_as_a_ci_pass(self):
        fake = self.fake()
        v, reasons, facts = self.evaluate(fake)
        self.assertEqual((v, reasons), ("PASS", []))
        self.assertEqual((facts["ci_exception"], facts["hosted_checks"]), ("local-first", "NOT_RUN"))
        self.assertTrue(facts["ci_verdict"].startswith("NOT_A_CI_PASS"))
        self.assertEqual(facts["not_covered"], ["windows and ubuntu matrix legs"])
        self.assertRegex(facts["local_ci_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(fake.runs_reads), 1)

    def test_controlled_script_path_schema_is_optional_and_closed(self):
        legacy = omg.load_local_first(LF_CONFIG)["chisanan232/r"]
        self.assertNotIn("controlled_script_paths", legacy)
        controlled = omg.load_local_first(CONTROLLED_LF_CONFIG)["chisanan232/r"]
        self.assertEqual(controlled["controlled_script_paths"], ("scripts/*",))
        for bad in ("../scripts/*", ".github/workflows/*", "/scripts/*", "scripts/../*", "src/*"):
            entry = dict(CONTROLLED_LF_CONFIG["repos"]["Chisanan232/r"], controlled_script_paths=[bad])
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": entry}})

    def test_the_flag_the_trust_file_and_the_repo_entry_are_all_required(self):
        self.refused(self.fake(), "NO_CI_EVIDENCE", ci_exception=False)
        self.refused(self.fake(), "NO_CI_EVIDENCE", config=None)
        other = {"schema": 1, "repos": {"Chisanan232/other": LF_CONFIG["repos"]["Chisanan232/r"]}}
        self.refused(self.fake(), "NO_CI_EVIDENCE", config=other)

    def test_a_public_repository_is_denied(self):
        self.refused(self.fake(isPrivate=False), "NOT_PRIVATE")
        self.refused(self.fake(isPrivate=None), "NOT_PRIVATE")

    def test_each_identity_field_mismatch_alone_refuses(self):
        for over in ({"repo": "Chisanan232/x"}, {"pr": 8}, {"base": "dev"}, {"head": "c" * 40}, {"base_sha": "d" * 40}):
            with self.subTest(over=over):
                fake = self.fake(lf_comment(lf_record(**over)))
                self.refused(fake, "LOCAL_CI_MISSING" if "head" in over else "LOCAL_CI_MISMATCH")

    def test_the_base_sha_is_the_real_merge_base_not_the_claimed_one(self):
        self.refused(self.fake(merge_base="e" * 40), "LOCAL_CI_MISMATCH")

    def test_a_head_that_moved_after_the_record_has_no_record(self):
        self.refused(self.fake(headRefOid="c" * 40), "LOCAL_CI_MISSING")

    def test_a_failing_run_is_denied_even_when_a_later_run_passes(self):
        bad = lf_record()
        bad["commands"][0]["exit_code"] = 1
        fake = self.fake(lf_comment(bad), lf_comment())
        self.refused(fake, "LOCAL_CI_FAILED")
        v, reasons, _ = self.evaluate(fake)
        self.assertEqual(omg.decide(v, reasons, {"class": "A"})["outcome"], "DENY")

    def test_stale_future_and_backdated_records_refuse(self):
        late = lf_record()
        late["commands"][1]["finished_at"] = iso(NOW - 25 * 3600)
        late["commands"][0]["finished_at"] = iso(NOW - 25 * 3600)
        late["commands"][0]["started_at"] = iso(NOW - 26 * 3600)
        late["commands"][1]["started_at"] = iso(NOW - 25 * 3600)
        self.refused(self.fake(lf_comment(late)), "LOCAL_CI_STALE")
        future = lf_record()
        future["commands"][1]["finished_at"] = iso(NOW + 3600)
        self.refused(self.fake(lf_comment(future)), "LOCAL_CI_MISMATCH")
        # finished_at after the comment's server-side creation time means the record was written before the work ran
        self.refused(self.fake(lf_comment(createdAt=iso(NOW - 595))), "LOCAL_CI_MISMATCH")

    def test_commands_must_equal_the_owner_configuration_exactly(self):
        suite, lint = lf_record()["commands"]
        weaker = dict(suite, argv=["python3", "-m", "pytest", "-k", "nothing"])
        renamed = dict(suite, name="other")
        for label, commands in (("argv", [weaker, lint]), ("name", [renamed, lint]), ("missing", [suite]),
                                ("extra", [suite, lint, dict(lint, name="extra")]), ("order", [lint, suite])):
            with self.subTest(label):
                self.refused(self.fake(lf_comment(lf_record(commands=commands))), "LOCAL_CI_COMMANDS_MISMATCH")

    def test_the_recorded_pr_context_must_match_the_pr_now(self):
        self.refused(self.fake(title="feat: renamed"), "LOCAL_CI_CONTEXT_MISMATCH")
        self.refused(self.fake(headRefName="feat/renamed"), "LOCAL_CI_CONTEXT_MISMATCH")
        for key, value in (("PR_TITLE", "other"), ("PULL_BRANCH", "other"), ("GIT_BASE", "e" * 40)):
            with self.subTest(key):
                self.refused(self.fake(lf_comment(lf_record(context=dict(lf_record()["context"], **{key: value})))), "LOCAL_CI_CONTEXT_MISMATCH")

    def test_a_record_without_a_well_formed_context_is_malformed(self):
        missing = lf_record()
        del missing["context"]
        self.refused(self.fake(lf_comment(missing)), "LOCAL_CI_MALFORMED")
        good = lf_record()["context"]
        for label, bad in (("extra", dict(good, CARGO_TARGET_DIR="/x")), ("short", {"PULL_BRANCH": "feat/x"}), ("type", dict(good, PR_TITLE=1)),
                           ("list", [])):
            with self.subTest(label):
                self.refused(self.fake(lf_comment(lf_record(context=bad))), "LOCAL_CI_MALFORMED")

    def test_edited_minimized_or_foreign_records_do_not_count(self):
        self.refused(self.fake(lf_comment(includesCreatedEdit=True)), "LOCAL_CI_MISSING")
        self.refused(self.fake(lf_comment(isMinimized=True)), "LOCAL_CI_MISSING")
        self.refused(self.fake(lf_comment(author={"login": "someone-else"})), "LOCAL_CI_MISSING")
        bare = lf_comment()
        del bare["includesCreatedEdit"]
        self.refused(self.fake(bare), "LOCAL_CI_MISSING")

    def test_a_fenced_record_does_not_count(self):
        self.refused(self.fake(lf_comment(body="```\nOWNER-MERGE-LOCAL-CI " + json.dumps(lf_record()) + "\n```")), "LOCAL_CI_MISSING")

    def test_malformed_and_duplicate_key_records_refuse(self):
        dup = '{"schema": 1, "schema": 1, "head": "%s"}' % HEAD
        for body in ("OWNER-MERGE-LOCAL-CI " + json.dumps(dict(lf_record(), extra=1)), "OWNER-MERGE-LOCAL-CI " + json.dumps(lf_record(schema=2)),
                     "OWNER-MERGE-LOCAL-CI " + json.dumps(lf_record(pr="7"))):
            with self.subTest(body[:60]):
                self.refused(self.fake(lf_comment(body=body)), "LOCAL_CI_MALFORMED")
        for body in ("OWNER-MERGE-LOCAL-CI {not json %s" % HEAD, "OWNER-MERGE-LOCAL-CI " + dup,
                     "OWNER-MERGE-LOCAL-CI " + json.dumps(dict(lf_record(), pad="x" * 70000))):   # unreadable at this head: fail closed
            with self.subTest(body[:40]):
                self.refused(self.fake(lf_comment(body=body)), "LOCAL_CI_FAILED")
        digest = lf_record()
        digest["commands"][0]["log_sha256"] = "zz"
        self.refused(self.fake(lf_comment(digest)), "LOCAL_CI_MALFORMED")

    def test_no_record_at_all_is_missing(self):
        fake = self.fake()
        fake.pr["comments"] = [fake.pr["comments"][0]]
        self.refused(fake, "LOCAL_CI_MISSING")

    def test_a_check_in_the_rollup_means_the_basis_does_not_apply(self):
        for conclusion in ("SUCCESS", "SKIPPED"):
            v, _, facts = self.evaluate(self.fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": conclusion}]))
            self.assertNotIn("ci_exception", facts)
        self.refused(self.fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]), "CI_FAILED")
        v, _, _ = self.evaluate(self.fake(statusCheckRollup=[{"name": "t", "status": "IN_PROGRESS", "conclusion": ""}]))
        self.assertEqual(v, "WAIT")

    def test_an_empty_rollup_without_a_skip_directive_waits(self):
        v, reasons, _ = self.evaluate(self.fake(message="chore: change"))
        self.assertEqual(v, "WAIT")
        self.assertTrue(reasons[0].startswith("CI_PENDING"), reasons)

    def test_skip_directives_are_recognised_only_where_github_honours_them(self):
        for msg in ("x [skip ci]", "x [ci skip]", "x [no ci]", "x [skip actions]", "x [actions skip]", "x\n\nbody\n\nskip-checks: true\n"):
            with self.subTest(msg):
                self.assertEqual(self.evaluate(self.fake(message=msg))[0], "PASS")
        for msg in ("x skip ci", "x [skip ci please]", "x skip-checks: false", "x (skip actions)", "x [CI SKIP]", "x [Skip Actions]",
                    "skip-checks: true", "x\n\nskip-checks: TRUE", "x\n\nskip-checks: true\n\nmore text after", "x skip-checks: true"):
            with self.subTest(msg):
                self.assertEqual(self.evaluate(self.fake(message=msg))[0], "WAIT")

    def test_any_actions_run_for_the_head_waits(self):
        v, reasons, _ = self.evaluate(self.fake(runs=1))
        self.assertEqual(v, "WAIT")
        self.assertTrue(reasons[0].startswith("CI_PENDING"), reasons)

    def test_workflow_pinned_and_oversized_changes_refuse(self):
        for files in ([{"path": ".github/workflows/q.yml"}], [{"path": ".GitHub/Workflows/q.yml"}], [{"path": "scripts/ci/run.sh"}], [],
                      [{"path": f"f{i}"} for i in range(100)]):
            with self.subTest(files=files[:1]):
                self.refused(self.fake(files=files), "LOCAL_FIRST_SCOPE")

    def test_a_rename_out_of_a_workflow_or_pinned_path_refuses(self):
        old = {"filename": "docs/quality.yml", "status": "renamed", "previous_filename": ".github/workflows/quality.yml"}
        for listed in ([old], [dict(old, previous_filename=".GitHub/Workflows/q.yml")], [dict(old, previous_filename="scripts/ci/run.sh")],
                       [dict(old, previous_filename="docs/old.md")]):
            with self.subTest(prev=listed[0]["previous_filename"]):
                self.refused(self.fake(files=[{"path": "docs/quality.yml"}], api_files=listed), "LOCAL_FIRST_SCOPE")

    def test_the_two_file_listings_must_agree_and_not_be_truncated(self):
        self.refused(self.fake(api_files=[]), "LOCAL_FIRST_SCOPE")
        self.refused(self.fake(api_files=[{"filename": "src/a.py", "status": "modified"}, {"filename": "src/b.py", "status": "modified"}]), "LOCAL_FIRST_SCOPE")
        self.refused(self.fake(api_files=[{"filename": "src/other.py", "status": "modified"}]), "LOCAL_FIRST_SCOPE")
        self.refused(self.fake(files=[{"path": f"f{i}"} for i in range(99)] + [{"path": "g"}]), "LOCAL_FIRST_SCOPE")

    def test_the_pull_files_read_is_an_allowed_read_only_call(self):
        omg.read_only(("api", "repos/Chisanan232/r/pulls/7/files?per_page=100", "--jq", "."))
        for bad in ("repos/Chisanan232/r/pulls/7/files?per_page=100&page=2", "repos/Chisanan232/r/pulls/7/files?per_page=1",
                    "repos/Chisanan232/r/pulls/7/files?per_page=100&x=1", "repos/Chisanan232/r/pulls/x/files?per_page=100"):
            with self.subTest(bad), self.assertRaises(RuntimeError):
                omg.read_only(("api", bad, "--jq", "."))

    def test_a_failing_record_stands_wherever_it_was_posted_and_however_it_is_shaped(self):
        bad = lf_record()
        bad["commands"][0]["exit_code"] = 1
        for body in ("```\nOWNER-MERGE-LOCAL-CI " + json.dumps(bad) + "\n```", "~~~\nOWNER-MERGE-LOCAL-CI " + json.dumps(bad) + "\n~~~"):
            with self.subTest(body[:3]):
                self.refused(self.fake(lf_comment(body=body), lf_comment()), "LOCAL_CI_FAILED")
        self.refused(self.fake(lf_comment(bad, includesCreatedEdit=True), lf_comment()), "LOCAL_CI_FAILED")
        self.refused(self.fake(lf_comment(bad, isMinimized=True), lf_comment()), "LOCAL_CI_FAILED")
        for code in (1.0, "1", False, True, None, 0.0, "0"):
            odd = lf_record()
            odd["commands"][0]["exit_code"] = code
            with self.subTest(code=code):
                self.refused(self.fake(lf_comment(odd), lf_comment()), "LOCAL_CI_FAILED")
        missing = lf_record()
        del missing["commands"][1]["exit_code"]
        self.refused(self.fake(lf_comment(missing), lf_comment()), "LOCAL_CI_FAILED")

    def test_a_failure_in_a_quoted_or_indented_line_still_counts(self):
        bad = lf_record()
        bad["commands"][0]["exit_code"] = 2
        for prefix in ("> ", ">> ", "  ", "> > ", "\t> "):
            with self.subTest(prefix=prefix):
                self.refused(self.fake(lf_comment(body=prefix + "OWNER-MERGE-LOCAL-CI " + json.dumps(bad)), lf_comment()), "LOCAL_CI_FAILED")

    def test_skip_trailer_must_be_the_final_line_after_a_blank_line(self):
        for msg in ("x\n\nskip-checks: true", "x\n\nskip-checks:true\n", "x\n\nbody\n\nskip-checks: true\n\n"):
            with self.subTest(msg):
                self.assertEqual(self.evaluate(self.fake(message=msg))[0], "PASS")
        for msg in ("x\n\nskip-checks: true\nSigned-off-by: a", "x\nskip-checks: true", "x\n\nskip-checks: true\n\ntail", "skip-checks: true",
                    "x\n\nskip-checks:  true", "x\n\n skip-checks: true"):
            with self.subTest(msg):
                self.assertEqual(self.evaluate(self.fake(message=msg))[0], "WAIT")

    def test_env_is_bound_into_the_record(self):
        entry = LF_CONFIG["repos"]["Chisanan232/r"]
        cfg = {"schema": 1, "repos": {"Chisanan232/r": dict(entry, env={"CI": "1"})}}
        self.assertEqual(self.evaluate(self.fake(lf_comment(lf_record(env={"CI": "1"}))), config=cfg)[0], "PASS")
        for env in ({}, {"CI": "2"}, {"CI": "1", "X": "1"}):
            with self.subTest(env=env):
                self.refused(self.fake(lf_comment(lf_record(env=env))), "LOCAL_CI_COMMANDS_MISMATCH", config=cfg)
        self.refused(self.fake(lf_comment(lf_record(env={"CI": "1"}))), "LOCAL_CI_COMMANDS_MISMATCH")   # config has no env
        bad = lf_record(env={"CI": 1})
        self.refused(self.fake(lf_comment(bad)), "LOCAL_CI_MALFORMED")
        missing = lf_record()
        del missing["env"]
        self.refused(self.fake(lf_comment(missing)), "LOCAL_CI_MALFORMED")

    def test_more_redirecting_env_names_are_refused_in_the_trust_file(self):
        entry = LF_CONFIG["repos"]["Chisanan232/r"]
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TERM", "TZ", "UV_PYTHON",
                    "PYTEST_ADDOPTS", "JAVA_TOOL_OPTIONS", "CLASSPATH", "GEM_PATH", "RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER", "NPM_CONFIG_PREFIX",
                    "BUNDLE_GEMFILE"):
            with self.subTest(key), self.assertRaises(ValueError):
                omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": dict(entry, env={key: "x"})}})

    def test_check_names_must_have_distinct_log_names(self):
        entry = LF_CONFIG["repos"]["Chisanan232/r"]
        with self.assertRaises(ValueError):
            omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": dict(entry, checks=[{"name": "a b", "argv": ["x"]}, {"name": "a_b", "argv": ["y"]}])}})

    def test_a_tilde_fenced_passing_record_does_not_count(self):
        self.refused(self.fake(lf_comment(body="~~~\nOWNER-MERGE-LOCAL-CI " + json.dumps(lf_record()) + "\n~~~")), "LOCAL_CI_MISSING")

    def test_a_missing_review_still_refuses(self):
        fake = self.fake()
        fake.pr["comments"] = fake.pr["comments"][1:]
        v, reasons, _ = self.evaluate(fake)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any(r.startswith("NO_REVIEW_EVIDENCE") for r in reasons), reasons)

    def test_the_three_ci_exception_records_do_not_stand_in_for_each_other(self):
        billing = {"author": {"login": "chisanan232"}, "body": f"OWNER-MERGE-CI-UNAVAILABLE head={HEAD} cause=billing local_equivalent=PASS"}
        noci = {"author": {"login": "chisanan232"}, "body": Gate.NOCI}
        self.refused(self.fake(billing, noci, workflows=3), "LOCAL_CI_MISSING")
        self.refused(self.fake(billing, noci, workflows=3), "NO_CI_EVIDENCE", config=None)
        self.refused(Gate().noci_fake(comments=[{"author": {"login": "chisanan232"}, "body": REVIEW}, lf_comment()]), "NO_CI_EVIDENCE", config=None)

    def test_the_actions_runs_read_is_an_allowed_read_only_call(self):
        omg.read_only(("api", f"repos/Chisanan232/r/actions/runs?head_sha={HEAD}&per_page=1", "--jq", ".total_count"))
        for bad in (f"repos/Chisanan232/r/actions/runs?head_sha={HEAD}", f"repos/Chisanan232/r/actions/runs?head_sha={HEAD}&per_page=100",
                    "repos/Chisanan232/r/actions/runs?head_sha=main&per_page=1", f"repos/Chisanan232/r/actions/runs?head_sha={HEAD}&per_page=1&x=1",
                    f"repos/Chisanan232/r/actions/runs/1?head_sha={HEAD}&per_page=1"):
            with self.subTest(bad), self.assertRaises(RuntimeError):
                omg.read_only(("api", bad, "--jq", ".total_count"))

    def test_the_trust_file_is_loaded_with_the_owner_file_checks(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(os.path.realpath(d), "local")
            os.mkdir(root, 0o700)
            f = os.path.join(root, "local-first-ci.json")
            pathlib.Path(f).write_text(json.dumps(LF_CONFIG))
            os.chmod(f, 0o600)
            cfg = omg.read_owner_file(f, True, omg.load_local_first, "local-first CI config", root)
            self.assertEqual(cfg["chisanan232/r"]["max_age_hours"], 24)
            self.assertIsNone(omg.read_owner_file(os.path.join(root, "none.json"), False, omg.load_local_first, "x", root))
            os.chmod(f, 0o666)
            with self.assertRaises(ValueError):
                omg.read_owner_file(f, True, omg.load_local_first, "x", root)
            os.chmod(f, 0o600)
            link = os.path.join(root, "link.json")
            os.symlink(f, link)
            with self.assertRaises(ValueError):
                omg.read_owner_file(link, True, omg.load_local_first, "x", root)

    def test_the_trust_file_may_fix_environment_variables(self):
        entry = LF_CONFIG["repos"]["Chisanan232/r"]
        self.assertEqual(omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": dict(entry, env={"CI": "1"})}})["chisanan232/r"]["env"], {"CI": "1"})
        self.assertEqual(omg.load_local_first(LF_CONFIG)["chisanan232/r"]["env"], {})
        for bad in ({"ci": "1"}, {"CI": 1}, {"CI=": "1"}, {"PYTHONPATH": "/x"}, [], {"LD_PRELOAD": "/x"}):
            with self.assertRaises(ValueError, msg=bad):
                omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": dict(entry, env=bad)}})

    def test_a_malformed_trust_file_is_rejected(self):
        entry = LF_CONFIG["repos"]["Chisanan232/r"]
        for bad in ({}, {"schema": 2, "repos": {}}, {"schema": 1, "repos": {"Chisanan232/r": dict(entry, checks=[])}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, checks=[{"name": "a", "argv": []}])}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, checks=[{"name": "a", "argv": ["x"]}, {"name": "a", "argv": ["y"]}])}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, max_age_hours=0)}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, max_age_hours=169)}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, max_age_hours=True)}},
                    {"schema": 1, "repos": {"Chisanan232/r": dict(entry, surprise=1)}},
                    {"schema": 1, "repos": {"Chisanan232/r": entry, "chisanan232/R": entry}}):
            with self.assertRaises(ValueError, msg=bad):
                omg.load_local_first(bad)
        self.assertEqual(omg.load_local_first({"schema": 1, "repos": {"Chisanan232/r": {"checks": entry["checks"]}}})["chisanan232/r"]["max_age_hours"], 24)

    def test_check_accepts_only_a_trust_file_under_the_owner_config_root(self):
        import contextlib, io, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(os.path.realpath(d), "local-first-ci.json")  # outside the fixed owner-config directory
            pathlib.Path(f).write_text(json.dumps(LF_CONFIG))
            os.chmod(f, 0o600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = omg.main(["check", "--repo", "Chisanan232/r", "--pr", "7", "--owners", "Chisanan232", "--ci-exception", "--local-first-config", f], run=self.fake())
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out.getvalue())["decision"]["outcome"], "DENY")


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

    def test_unstable_merge_state_is_explained_only_by_a_fully_waived_billing_failure(self):
        v, reasons, facts = self.verdict([self.job("1", "t"), self.job("2", "lint", "SUCCESS")], {"1": [self.TEXT]}, mergeStateStatus="UNSTABLE")
        self.assertEqual((v, reasons), ("PASS", []))
        self.assertIn("UNSTABLE", facts["merge_state"])
        self.assertTrue(facts["ci_verdict"].startswith("NOT_A_CI_PASS"))

    def test_unstable_still_refuses_without_the_waiver_or_with_other_states(self):
        # no billing record: the failed check keeps CI_FAILED and UNSTABLE stays a reason
        v, reasons, _ = run(self.fake([self.job("1", "t")], {"1": [self.TEXT]}, record="no record", mergeStateStatus="UNSTABLE"), ci_unavailable=True)
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any(r.startswith("MERGE_STATE_NOT_CLEAN") for r in reasons), reasons)
        # a real step failure is not waived, so UNSTABLE is not explained
        v, reasons, _ = self.verdict([self.job("1", "t")], {"1": ["Process completed with exit code 1."]},
                                     steps={"1": [{"conclusion": "failure"}]}, mergeStateStatus="UNSTABLE")
        self.assertEqual(v, "REFUSE")
        self.assertTrue(any(r.startswith("CI_FAILED") for r in reasons) and any(r.startswith("MERGE_STATE_NOT_CLEAN") for r in reasons), reasons)
        # BLOCKED and BEHIND-type states are never explained by the waiver
        for state in ("BLOCKED", "DIRTY"):
            with self.subTest(state=state):
                v, reasons, _ = self.verdict([self.job("1", "t")], {"1": [self.TEXT]}, mergeStateStatus=state)
                self.assertEqual(v, "REFUSE", reasons)

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
