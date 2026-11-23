#!/usr/bin/env python3
"""Tests for the SPE-184a release commands in scripts/release-line.py: candidate-tag, promote, verify, final-tag, protection.

Reuses the SPE-182 harness (bare origin + clone, git wrapper that logs every call, fake `gh` with real merges into the bare repo) and
extends the fake gh with `pr view` (merge commit + status check rollup), `--match-head-commit` enforcement and `api` branch protection.
GO records and the local-first trust file are fixtures under a temp HOME. No network, no hosted workflow runs.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_train_tests", ROOT / "tests/test-release-line-train.py")
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)
base = train.base
REPO = base.REPO
REAL_GIT = train.REAL_GIT
omg = base.omg


def stamp(delta=0):
    """Real wall-clock stamp: the tool's freshness decisions ignore SOURCE_DATE_EPOCH, which the harness pins to 2026-01-01."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta))

FAKE_GH = train.FAKE_GH
for old, new in (
    ('    work = S + ".merge"\n', '''    mh = opt("--match-head-commit")
    if st.get("merge_fail"):
        sys.exit(1)
    if mh and subprocess.run(["{git}", "-C", st["bare"], "rev-parse", "refs/heads/" + pr["headRefName"]], capture_output=True, text=True).stdout.strip() != mh:
        sys.exit(1)
    work = S + ".merge"
'''),
    ('    subprocess.run(["rm", "-rf", work])\n    pr["state"] = "MERGED"', '''    pr["mergeOid"] = g("rev-parse", "HEAD").stdout.decode().strip()
    subprocess.run(["rm", "-rf", work])
    if st.get("drift_on_merge"):
        st["protection"]["required_status_checks"] = {{"strict": False, "contexts": []}}
    pr["state"] = "MERGED"'''),
    ('else:\n    sys.exit(1)', '''elif a[:2] == ["pr", "view"]:
    pr = next((p for p in st["prs"] if p["number"] == int(a[2])), None)
    if pr is None:
        sys.exit(1)
    if "headRefOid" in (opt("--json") or ""):
        oid = subprocess.run(["{git}", "-C", st["bare"], "rev-parse", "refs/heads/" + pr["headRefName"]], capture_output=True, text=True).stdout.strip()
        print(json.dumps({{"number": pr["number"], "state": pr["state"], "title": "Promote " + pr["headRefName"].split("/")[1] + " to " + pr["baseRefName"],
                          "baseRefName": pr["baseRefName"], "headRefName": pr["headRefName"], "headRefOid": oid, "baseRefOid": "0" * 40,
                          "isCrossRepository": False, "files": [{{"path": "feature-a.txt"}}], "comments": st.get("comments", []), "commits": []}}))
        sys.exit(0)
    print(json.dumps({{"number": pr["number"], "state": pr["state"], "baseRefName": pr["baseRefName"],
                      "mergeCommit": ({{"oid": pr["mergeOid"]}} if pr.get("mergeOid") else None), "statusCheckRollup": st.get("checks", [])}}))
elif a[:2] == ["repo", "view"]:
    print(json.dumps({{"nameWithOwner": "Example-Owner/example-product", "defaultBranchRef": {{"name": "main"}}, "isFork": False, "isPrivate": True,
                      "viewerPermission": "ADMIN", "owner": {{"login": "Example-Owner"}}}}))
elif a[0] == "api" and a[1] == "user":
    print("me")
elif a[0] == "api" and "/issues/" in a[1]:
    print("[]")
elif a[0] == "api" and "/pulls/" in a[1]:
    print(json.dumps([{{"filename": "feature-a.txt", "previous_filename": None, "status": "added"}}]))
elif a[0] == "api" and "/actions/runs" in a[1]:
    print("0")
elif a[0] == "api" and "/commits/" in a[1]:
    print("outage [skip ci]")
elif a[0] == "api" and "/compare/" in a[1]:
    b, _, h = a[1].split("/compare/")[1].partition("...")
    print(subprocess.run(["{git}", "-C", st["bare"], "merge-base", "refs/heads/" + b, h], capture_output=True, text=True).stdout.strip())
elif a[0] == "api":
    if "protection" not in st:
        sys.exit(1)
    print(json.dumps(st["protection"]))
    sys.exit(1 if st["protection"].get("message") else 0)
else:
    sys.exit(1)'''),
):
    assert FAKE_GH.count(old) == 1, old
    FAKE_GH = FAKE_GH.replace(old, new)


class Rel(train.TrainBase):
    AUTO_PIN = True

    def setUp(self):
        super().setUp()
        (self.bin / "gh").write_text(FAKE_GH.format(py=sys.executable, git=REAL_GIT, date=base.DATE))
        self.env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.x",
                         "GIT_AUTHOR_DATE": base.DATE, "GIT_COMMITTER_DATE": base.DATE})
        self.go_dir = self.local / "release-go"
        self.go_dir.mkdir(mode=0o700)
        os.chmod(self.go_dir, 0o700)
        self.evidence = self.home / ".claude/owner-merge-evidence"  # the fixed host evidence directory, under the fixture HOME
        self.lf = self.local / "local-first-ci.json"
        self.install(self.v0_only_policy())
        self.gh_update(checks=[{"status": "COMPLETED", "conclusion": "SUCCESS"}], protection={"enforce_admins": {"enabled": True}})
        if self.AUTO_PIN:
            self.pin_protection()

    def pin_verification(self, final, pr=1, digest=None):
        """The owner pins the sha256 of the private verification record in release-verification.json."""
        import hashlib
        tag = f"{REPO.replace('/', '__')}-{pr}"
        rec = json.loads((self.evidence / omg.local_ci_evidence_filename(tag, final)).read_text())
        digest = digest or hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        pins = self.local / "release-verification.json"
        pins.write_text(json.dumps({"schema": 1, "records": {REPO: {final: digest}}}))
        os.chmod(pins, 0o600)

    def pin_protection(self, branch="main"):
        rc, out, _ = self.cli("protection", "snapshot", "--branch", branch, "--execute")
        self.assertEqual(rc, 0, out)
        digest = next(s for s in out["steps"] if s["step"] == "snapshot")["evidence"]["sha256"]
        pins = self.local / "release-protection.json"
        cur = json.loads(pins.read_text()) if pins.exists() else {"schema": 1, "snapshots": {REPO: {}}}
        cur["snapshots"][REPO][branch] = digest
        pins.write_text(json.dumps(cur))
        os.chmod(pins, 0o600)

    CHECKS = [("t", [sys.executable, "-c", "pass"])]

    def local_first(self, checks=None):
        checks = self.CHECKS if checks is None else checks
        self.lf.write_text(json.dumps({"schema": 1, "repos": {REPO: {"checks": [{"name": n, "argv": a} for n, a in checks]}}}))
        os.chmod(self.lf, 0o600)

    # fixtures
    def gh_update(self, **kw):
        st = json.loads(self.gh_state.read_text())
        st.update(kw)
        self.gh_state.write_text(json.dumps(st))

    def sha(self, ref):
        return self.w("rev-parse", ref + "^{commit}", cwd=self.bare)

    def go(self, version="0.3.0", tag="v0.3.0-rc.1", sha=None, scopes=("candidate_tag", "promote", "final_tag"), **over):
        rec = {"repo": REPO, "version": version, "candidate_tag": tag, "candidate_sha": sha or self.sha(tag), "publication_base": "main",
               "scopes": list(scopes), "authority": "test fixture", "created_utc": "2026-01-01T00:00:00Z", **over}
        path = self.go_dir / f"{REPO.replace('/', '__')}__{version}.json"
        path.write_text(json.dumps(rec))
        os.chmod(path, 0o600)
        return rec

    def rel(self, cmd, *extra, execute=False, version="0.3.0", go=True):
        argv = ["release", cmd, "--version", version, "--clone", str(self.work), *(["--go-dir", str(self.go_dir)] if go else []), *extra]
        return self.cli(*argv, *(["--execute"] if execute else []))

    def refused(self, out):
        return next((s["state"] for s in out["steps"] if s["step"] == "refused"), None)

    def step(self, out, name):
        return next(s for s in out["steps"] if s["step"] == name)

    def merges(self):
        return [c for c in self.calls() if c.startswith("pr merge")]

    def pushes(self):
        return [x for x in self.git_log.read_text().splitlines() if " push " in f" {x} "]

    def assert_no_force(self):
        for x in self.pushes():
            self.assertRegex(x, r"push origin [0-9a-f]{40}:refs/(heads|tags)/\S+$")
            self.assertNotRegex(x, r"--force|--delete|--mirror| -f |\+")

    def write_evidence(self, cand, pr=1, exit_code=0, argv=None, finished=None, log_digest=None, mode=0o600, env=None,
                       base_sha=None, post=True, edited=False, author="me", posted_differs=False):
        created = stamp()
        finished = finished or created
        """The recorder's private record and log (fixed evidence directory) plus the line the owner posts as a PR comment."""
        import hashlib
        self.local_first()
        self.evidence.mkdir(parents=True, exist_ok=True)
        os.chmod(self.evidence, 0o700)
        tag = f"{REPO.replace('/', '__')}-{pr}"
        log = b"ok\n"
        lp = self.evidence / omg.local_ci_evidence_filename(tag, cand, "t")
        lp.write_bytes(log)
        os.chmod(lp, 0o600)
        base_sha = base_sha or self.w("merge-base", "main", cand, cwd=self.bare)
        rec = {"schema": 1, "repo": REPO, "pr": pr, "base": "main", "base_sha": base_sha, "head": cand, "env": env or {},
               "context": {"PULL_BRANCH": "promote/0.3.0", "PR_TITLE": "Promote 0.3.0 to main", "GIT_BASE": base_sha},
               "commands": [{"name": "t", "argv": argv or self.CHECKS[0][1], "exit_code": exit_code, "started_at": finished, "finished_at": finished,
                             "log_sha256": log_digest or hashlib.sha256(log).hexdigest()}]}
        rp = self.evidence / omg.local_ci_evidence_filename(tag, cand)
        rp.write_text(json.dumps(rec, separators=(",", ":")))
        os.chmod(rp, mode)
        posted = dict(rec, commands=[dict(rec["commands"][0], log_sha256="f" * 64)]) if posted_differs else rec
        comments = [{"author": {"login": author}, "body": "OWNER-MERGE-LOCAL-CI " + json.dumps(posted, separators=(",", ":")),
                     "createdAt": created, "includesCreatedEdit": edited, "isMinimized": False}] if post else []
        self.gh_update(comments=comments)
        return rec

    def base_only_change(self, fname="base-only.txt", text="x\n"):
        """A direct commit on main (not on any line) plus the reviewed not-applicable record on the line, so accounting is complete."""
        self.w("fetch", "-q", "--tags", "origin")
        self.w("checkout", "-q", "-B", "main", "origin/main")
        (self.work / fname).write_text(text)
        self.w("add", ".")
        self.w("commit", "-q", "-m", "base-only " + fname)
        self.w("push", "-q", "origin", "main")
        return self.w("rev-parse", "HEAD")

    def mark_not_applicable(self, src):
        self.w("fetch", "-q", "--tags", "origin")
        self.w("checkout", "-q", "-B", "integration/v0.x", "origin/integration/v0.x")
        self.w("commit", "-q", "--allow-empty", "-m", f"not applicable\n\nRelease-Line-Source: {src}\nRelease-Line-Kind: not-applicable\nRelease-Line-Reason: base only")
        self.w("push", "-q", "origin", "integration/v0.x")


class GoRecords(Rel):
    def test_missing_scope_expired_mismatch_and_untrusted(self):
        rc, out, _ = self.rel("candidate-tag", "--n", "1")
        self.assertEqual((rc, self.refused(out)), (1, "GO_MISSING"))
        self.go(scopes=("promote",))
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "GO_SCOPE")
        self.go(expires_utc="2025-12-31T00:00:00Z")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "GO_EXPIRED")
        self.go(expires_utc="2099-01-01T00:00:00Z")
        self.assertEqual(self.step(self.rel("candidate-tag")[1], "go")["state"], "ACCEPTED")  # a live record is accepted
        for over in ({"repo": "Other-Owner/other"}, {"version": "0.9.0"}, {"publication_base": "master"}, {"candidate_sha": "a" * 40},
                     {"candidate_tag": "v0.9.0-rc.1"}, {"candidate_tag": "v0.3.0"}):
            self.go(**over)
            rc, out, _ = self.rel("candidate-tag", execute=True)
            self.assertEqual((rc, self.refused(out)), (1, "GO_MISMATCH"), over)
        self.go()
        os.chmod(self.go_dir / f"{REPO.replace('/', '__')}__0.3.0.json", 0o666)
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "GO_INVALID")
        self.assertEqual(self.remote("refs/tags/v0.3.0-rc.1"), self.sha("v0.3.0-rc.1"))

    def test_go_record_for_another_repo_directory_is_refused(self):
        self.go()
        outside = pathlib.Path(self.tmp.name) / "elsewhere"
        outside.mkdir(mode=0o700)
        (outside / f"{REPO.replace('/', '__')}__0.3.0.json").write_text(json.dumps(self.go()))
        rc, out, _ = self.cli("release", "candidate-tag", "--version", "0.3.0", "--clone", str(self.work), "--go-dir", str(outside))
        self.assertEqual(self.refused(out), "GO_INVALID")  # must live under the owner config root


class CandidateTag(Rel):
    def test_create_idempotent_and_conflict(self):
        tip = self.commit("integration/v0.x", "late.txt")
        self.go(tag="v0.3.0-rc.2", sha=tip)
        rc, out, _ = self.rel("candidate-tag")
        self.assertEqual((rc, out["status"], self.step(out, "tag")["state"]), (0, "NOT_RUN", "DRY_PLAN"))
        self.assertIsNone(self.remote("refs/tags/v0.3.0-rc.2"))
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "tag")["state"]), (0, "PASS", "CREATED"), out)
        self.assertEqual(out["idempotency_key"], f"tag:{REPO}:v0.3.0-rc.2")
        self.assertEqual(self.w("cat-file", "-t", "refs/tags/v0.3.0-rc.2", cwd=self.bare), "tag")  # annotated
        self.assertEqual(self.sha("v0.3.0-rc.2"), tip)
        pushes = len(self.pushes())
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "tag")["state"]), (0, "PASS", "REUSED"))
        self.assertEqual(len(self.pushes()), pushes)
        # an existing tag at another commit is never moved
        older = self.sha("v0.3.0-rc.1")
        self.go(tag="v0.3.0-rc.2", sha=older)
        rc, out, _ = self.rel("candidate-tag", "--sha", older, execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "CONFLICT_EXISTING"))
        self.assertEqual(self.sha("v0.3.0-rc.2"), tip)
        self.assert_no_force()

    def test_sha_must_be_on_line_and_go_must_match_it(self):
        off = self.base_only_change()  # on main only
        self.go(tag="v0.3.0-rc.2", sha=off)
        self.assertEqual(self.refused(self.rel("candidate-tag", "--sha", off, execute=True)[1]), "SHA_NOT_ON_LINE")
        self.go(tag="v0.3.0-rc.2", sha=self.sha("v0.3.0-rc.1"))  # approves rc.1's commit
        tip = self.commit("integration/v0.x", "later.txt")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "GO_MISMATCH")  # default sha is the new tip
        self.assertIsNone(self.remote("refs/tags/v0.3.0-rc.2"))
        self.assertNotEqual(tip, self.sha("v0.3.0-rc.1"))

    def test_pre_authorized_repo_needs_no_go_and_numbers_the_next_candidate(self):
        p = self.v0_only_policy()
        base.entry(p)["release_lines"]["milestone_tags"] = "pre-authorized"
        self.install(p)
        self.commit("integration/v0.x", "late.txt")
        rc, out, _ = self.rel("candidate-tag", execute=True, go=False)
        self.assertEqual((rc, out["status"], self.step(out, "go")["state"]), (0, "PASS", "PRE_AUTHORIZED"), out)
        self.assertEqual(self.step(out, "tag")["evidence"]["tag"], "v0.3.0-rc.2")


class Promote(Rel):
    def promote_ok(self, **kw):
        return self.rel("promote", execute=True, **kw)

    def test_dry_run_then_execute_then_final_tag_on_captured_sha(self):
        self.go()
        cand = self.sha("v0.3.0-rc.1")
        rc, out, _ = self.rel("promote")
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"), out)
        self.assertEqual(out["idempotency_key"], f"promote:{REPO}:0.3.0:{cand}")
        pred = self.step(out, "prediction")
        self.assertEqual((pred["state"], pred["evidence"]["verification"]), ("CLEAN", "CARRIED_OVER"))
        self.assertEqual((self.heads("promote/"), self.merges(), [c for c in self.calls() if c.startswith("pr create")]), ([], [], []))

        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual(self.heads("promote/"), ["refs/heads/promote/0.3.0"])
        self.assertEqual(self.remote("refs/heads/promote/0.3.0"), cand)
        self.assertEqual(self.merges(), [f"pr merge 1 --merge --match-head-commit {cand} --repo {REPO}"])
        for flag in ("--admin", "--squash", "--rebase", "--auto"):
            self.assertNotIn(flag, self.merges()[0])
        final = self.remote("refs/heads/main")
        self.assertEqual(self.step(out, "merge")["evidence"]["final_sha"], final)
        self.assertEqual(self.w("rev-parse", "main^2", cwd=self.bare), cand)
        self.assertEqual(self.step(out, "hosted_ci")["state"], "CI_PASS")
        self.assertEqual(self.step(out, "assertions")["state"], "PASS")

        # idempotent: a second run reports the promotion and merges nothing
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.step(out, "merge")["state"]), (0, "PASS", "ALREADY_PROMOTED"))
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(len([c for c in self.calls() if c.startswith("pr create")]), 1)

        # the base advances: the final tag still lands on the captured promotion merge, never on the tip
        self.commit("main", "later-on-main.txt")
        tip = self.remote("refs/heads/main")
        self.assertNotEqual(tip, final)
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "tag")["state"]), (0, "PASS", "CREATED"), out)
        self.assertEqual(self.sha("v0.3.0"), final)
        self.assertEqual(self.w("cat-file", "-t", "refs/tags/v0.3.0", cwd=self.bare), "tag")
        journal = [json.loads(x) for x in (self.state_dir / "journal.jsonl").read_text().splitlines()]
        rec = next(r for r in journal if r.get("state") == "final-tag")
        self.assertEqual((rec["source_branch"], rec["release_line"], rec["version"], rec["promotion_pr"], rec["candidate_sha"], rec["final_sha"]),
                         ("integration/v0.x", "v0", "0.3.0", 1, cand, final))
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual((rc, self.step(out, "tag")["state"]), (0, "REUSED"))
        self.assert_no_force()
        self.assertFalse([c for c in self.calls() if "workflow" in c or "run" in c.split()[:2]])

    def test_final_tag_refuses_before_promotion_and_without_scope(self):
        self.go()
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "NOT_PROMOTED")
        self.go(scopes=("promote",))
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "GO_SCOPE")
        self.go(scopes=("candidate_tag",))
        self.assertEqual(self.refused(self.rel("promote")[1]), "GO_SCOPE")
        self.assertIsNone(self.remote("refs/tags/v0.3.0"))

    def test_final_tag_conflict_when_tag_exists_elsewhere(self):
        self.go()
        self.assertEqual(self.promote_ok()[1]["status"], "PASS")
        self.tag("v0.3.0", "integration/v0.x")
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "CONFLICT_EXISTING"))

    def test_hosted_ci_outage_requires_local_evidence_and_failure_is_never_bypassed(self):
        self.go()
        cand = self.sha("v0.3.0-rc.1")
        self.gh_update(checks=[])  # no hosted check ran (billing or quota outage)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, self.refused(out)), (1, "LOCAL_EVIDENCE_REQUIRED"))
        self.assertEqual(self.step(out, "hosted_ci")["state"], "NOT_RUN")
        self.assertEqual(self.merges(), [])
        for bad in ({"exit_code": 1}, {"argv": ["/bin/true"]}, {"finished": "2025-12-01T00:00:00Z"}, {"log_digest": "0" * 64}, {"mode": 0o666},
                    {"finished": stamp(86400)}, {"env": {"A": "b"}}, {"base_sha": "1" * 40}, {"post": False}, {"edited": True},
                    {"author": "someone-else"}, {"posted_differs": True}):
            # failing, other command than the owner's, stale, forged log digest, writable by others, finishing in the future, other env or
            # base commit, never posted, posted then edited, posted by another user, or posted record differing from the private evidence
            self.write_evidence(cand, **bad)
            self.assertEqual(self.refused(self.promote_ok()[1]), "LOCAL_EVIDENCE_REQUIRED", bad)
        self.assertEqual(self.merges(), [])
        self.write_evidence(cand)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual(self.step(out, "hosted_ci")["state"], "NOT_RUN")
        self.assertEqual(self.step(out, "local_equivalent")["state"], "PASS")
        self.assertFalse([c for c in self.calls() if "workflow" in c])

    def test_freshness_ignores_source_date_epoch(self):
        self.go()
        cand = self.sha("v0.3.0-rc.1")
        self.gh_update(checks=[])
        # a record that is stale on the real clock stays stale even when SOURCE_DATE_EPOCH makes it look fresh
        self.write_evidence(cand, finished="2026-01-01T00:00:00Z")
        self.env["SOURCE_DATE_EPOCH"] = "1767225600"
        self.assertEqual(self.refused(self.promote_ok()[1]), "LOCAL_EVIDENCE_REQUIRED")
        # a record that is fresh on the real clock stays fresh when SOURCE_DATE_EPOCH claims the year 2020
        self.write_evidence(cand)
        self.env["SOURCE_DATE_EPOCH"] = "1577836800"
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.step(out, "local_equivalent")["state"]), (0, "PASS", "PASS"), out)

    def test_failing_hosted_check_refuses_and_pending_defers(self):
        self.go()
        self.gh_update(checks=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "FAIL", "CI_FAILED"))
        self.gh_update(checks=[{"status": "IN_PROGRESS", "conclusion": ""}])
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"))
        self.assertEqual(self.merges(), [])

    def test_interrupted_merge_resumes_without_duplicates(self):
        self.go()
        self.gh_update(merge_fail=True)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, self.refused(out)), (1, "MERGE_FAILED"))
        self.assertEqual(self.heads("promote/"), ["refs/heads/promote/0.3.0"])
        self.gh_update(merge_fail=False)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual(len([c for c in self.calls() if c.startswith("pr create")]), 1)
        self.assertEqual(self.heads("promote/"), ["refs/heads/promote/0.3.0"])

    def test_head_that_differs_from_candidate_is_a_conflict(self):
        self.go()
        self.branch("promote/0.3.0", "main")
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, self.refused(out)), (1, "CONFLICT_EXISTING"))
        self.assertEqual(self.merges(), [])

    def test_merge_tree_conflict_is_blocked_and_pushes_nothing(self):
        # add/add with the candidate's feature-a.txt (different content)
        self.mark_not_applicable(self.base_only_change("feature-a.txt", "conflicting\n"))
        self.w("fetch", "-q", "origin")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.go(tag="v0.3.0-rc.2")
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "BLOCKED", "MERGE_CONFLICT"))
        self.assertEqual((self.heads("promote/"), self.merges()), ([], []))

    def test_base_only_change_needs_forward_port_accounting(self):
        x = self.base_only_change()
        self.go()
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "FORWARD_PORT_INCOMPLETE"))
        self.assertEqual(self.step(out, "forward_port_accounting")["evidence"]["incomplete"][0]["source"], x[:12])
        self.assertEqual((self.heads("promote/"), self.merges()), ([], []))

    def test_differing_final_tree_requires_verification_and_verify_runs_local_first(self):
        x = self.base_only_change()
        self.mark_not_applicable(x)
        self.w("fetch", "-q", "origin")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.go(tag="v0.3.0-rc.2")
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual(self.step(out, "verification")["state"], "REQUIRED")
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")  # the final tree was never verified
        lf = self.local / "local-first-ci.json"

        def checks(*argvs):
            lf.write_text(json.dumps({"schema": 1, "repos": {REPO: {"checks": [{"name": f"c{i}", "argv": a} for i, a in enumerate(argvs)]}}}))
            os.chmod(lf, 0o600)
        extra = ("--local-first", str(lf),)
        rc, out, _ = self.rel("verify")
        self.assertEqual((rc, out["status"], self.step(out, "local_checks")["state"]), (0, "NOT_RUN", "NOT_RUN"))
        rc, out, _ = self.rel("verify", *extra, execute=True)  # no trust file yet: honest NOT_RUN
        self.assertEqual((out["status"], self.step(out, "local_checks")["state"]), ("NOT_RUN", "NOT_RUN"))
        checks([sys.executable, "-c", "pass"])
        rc, out, _ = self.rel("verify", *extra, execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "local_checks")["state"]), (0, "PASS", "PASS"), out)
        final = self.remote("refs/heads/main")
        self.assertEqual(self.step(out, "local_checks")["evidence"]["final_sha"], final)
        self.assertEqual(self.step(out, "promotion")["evidence"]["candidate_sha"], self.sha("v0.3.0-rc.2"))
        checks([sys.executable, "-c", "raise SystemExit(3)"])
        rc, out, _ = self.rel("verify", *extra, execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "local_checks")["state"]), (1, "FAIL", "FAIL"))
        self.assertEqual(len(self.w("worktree", "list").splitlines()), 1)  # the detached worktree is gone
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")  # a FAIL record does not unlock it
        checks([sys.executable, "-c", "pass"])
        out = self.rel("verify", *extra, execute=True)[1]
        self.assertEqual(out["status"], "PASS")
        digest = self.step(out, "local_checks")["evidence"]["record_sha256"]
        self.assertIn("release-verification.json", out["next_action"])
        # the bare private record is not enough: the owner pins its digest
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")
        self.pin_verification(final, digest="0" * 64)
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")
        self.pin_verification(final, digest=digest)
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual((rc, self.step(out, "tag")["state"]), (0, "CREATED"), out)
        self.assertEqual(self.sha("v0.3.0"), final)

    def differing_tree_promotion(self):
        x = self.base_only_change()
        self.mark_not_applicable(x)
        self.w("fetch", "-q", "origin")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.go(tag="v0.3.0-rc.2")
        self.assertEqual(self.rel("promote", execute=True)[1]["status"], "PASS")
        return self.remote("refs/heads/main")

    def test_final_tag_ignores_forged_journal_and_forged_evidence(self):
        final = self.differing_tree_promotion()
        # the agent-writable journal is an audit trail only
        with open(self.state_dir / "journal.jsonl", "a") as f:
            f.write(json.dumps({"state": "verified", "final_sha": final}) + "\n")
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")
        # a planted record with a forged log digest, then one the owner's checks do not match, then a stale one
        self.local_first()
        first = self.w("rev-parse", final + "^1", cwd=self.bare)
        self.evidence.mkdir(parents=True, exist_ok=True)
        os.chmod(self.evidence, 0o700)
        tag = f"{REPO.replace('/', '__')}-1"
        (self.evidence / omg.local_ci_evidence_filename(tag, final, "t")).write_bytes(b"ok\n")
        os.chmod(self.evidence / omg.local_ci_evidence_filename(tag, final, "t"), 0o600)
        import hashlib
        good = hashlib.sha256(b"ok\n").hexdigest()

        def plant(**over):
            cmd = {"name": "t", "argv": self.CHECKS[0][1], "exit_code": 0, "started_at": stamp(), "finished_at": stamp(),
                   "log_sha256": good, **over.pop("cmd", {})}
            rec = {"schema": 1, "repo": REPO, "pr": 1, "base": "main", "base_sha": first, "head": final, "env": {},
                   "context": {"PULL_BRANCH": "promote/0.3.0", "PR_TITLE": "Promote 0.3.0 to main", "GIT_BASE": first}, "commands": [cmd], **over}
            rp = self.evidence / omg.local_ci_evidence_filename(tag, final)
            rp.write_text(json.dumps(rec, separators=(",", ":")))
            os.chmod(rp, 0o600)
        for over in ({"cmd": {"log_sha256": "0" * 64}}, {"cmd": {"argv": ["/bin/true"]}}, {"env": {"A": "b"}}, {"base_sha": "1" * 40},
                     {"cmd": {"started_at": "2020-01-01T00:00:00Z", "finished_at": "2020-01-01T00:00:00Z"}},
                     {"cmd": {"started_at": "2099-01-01T00:00:00Z", "finished_at": "2099-01-01T00:00:00Z"}}):
            plant(**over)
            self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED", over)
        self.assertIsNone(self.remote("refs/tags/v0.3.0"))
        plant()  # a well-formed, fresh record is still not enough on its own: the owner must pin its digest
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")
        self.pin_verification(final, pr=1)
        plant(env={})  # rewritten after pinning (same content here, so rewrite with a different log digest to change it)
        plant(cmd={"finished_at": stamp(-5)})
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "FINAL_NOT_VERIFIED")  # digest no longer matches the pin
        plant()
        self.pin_verification(final, pr=1)
        self.assertEqual(self.step(self.rel("final-tag", execute=True)[1], "tag")["state"], "CREATED")

    def test_old_git_is_refused_before_anything_is_pushed(self):
        self.go()
        wrapper = self.bin / "git"
        wrapper.write_text(wrapper.read_text().replace("exec ", 'case " $* " in *" --version "*) echo "git version 2.30.0"; exit 0;; esac\nexec ', 1))
        for execute in (False, True):
            rc, out, _ = self.rel("promote", execute=execute)
            self.assertEqual((rc, self.refused(out)), (1, "GIT_TOO_OLD"))
        self.assertEqual((self.heads("promote/"), self.merges()), ([], []))

    def test_carried_over_verification_runs_nothing(self):
        self.go()
        self.assertEqual(self.promote_ok()[1]["status"], "PASS")
        rc, out, _ = self.rel("verify")
        self.assertEqual((rc, out["status"], self.step(out, "verification")["state"]), (0, "PASS", "CARRIED_OVER"))

    def test_maintenance_base_needs_latest_final_tag_and_greater_version(self):
        self.branch("maintenance/v0", "main")
        p = self.v0_only_policy(publication_base={"branch": "maintenance/v0", "kind": "maintenance"})
        self.install(p)
        self.pin_protection("maintenance/v0")
        self.base_only_change()
        self.tag("v0.3.1", "main")  # a same-major final on main that the maintenance base lacks
        self.go(publication_base="maintenance/v0")
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "BASE_MISSING_FINAL"))
        self.w("checkout", "-q", "-B", "maintenance/v0", "main")
        self.w("push", "-q", "origin", "maintenance/v0")
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "VERSION_NOT_GREATER"))  # 0.3.0 <= shipped 0.3.1
        self.assertEqual(self.merges(), [])


class SuccessorPromotion(Rel):
    def setUp(self):
        super().setUp()
        train.TrainTests.two_successors_registry(self, [])

    def test_predecessor_must_be_shipped_and_integrated(self):
        self.commit("integration/v0.4.0", "succ-2.txt")
        self.tag("v0.4.0-rc.1", "integration/v0.4.0")
        self.go(version="0.4.0", tag="v0.4.0-rc.1", scopes=("promote",))
        self.assertEqual(self.refused(self.rel("promote", version="0.4.0")[1]), "PREDECESSOR_NOT_SHIPPED")
        # v0.3.0 ships a late rc.2 that v0.4.0 does not contain yet
        self.commit("integration/v0.x", "late-fix.txt")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.promote("0.3.0", "v0.3.0-rc.2")
        self.assertEqual(self.refused(self.rel("promote", version="0.4.0")[1]), "PREDECESSOR_NOT_INTEGRATED")
        self.assertEqual(self.merges(), [])

    def test_stale_candidate_cut_before_integration_is_refused(self):
        self.commit("integration/v0.x", "late-fix.txt")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        stale = self.commit("integration/v0.4.0", "succ-2.txt")
        self.tag("v0.4.0-rc.1", "integration/v0.4.0")
        self.promote("0.3.0", "v0.3.0-rc.2")
        self.w("checkout", "-q", "-B", "integration/v0.4.0", "origin/integration/v0.4.0")
        self.w("merge", "-q", "--no-ff", "-m", "integrate 0.3.0", "v0.3.0-rc.2")  # integrated after rc.1 was cut
        self.w("push", "-q", "origin", "integration/v0.4.0")
        self.go(version="0.4.0", tag="v0.4.0-rc.1", sha=stale, scopes=("promote",))
        self.assertEqual(self.refused(self.rel("promote", version="0.4.0")[1]), "STALE_CANDIDATE")
        self.commit("integration/v0.4.0", "succ-3.txt")
        self.tag("v0.4.0-rc.2", "integration/v0.4.0")
        self.go(version="0.4.0", tag="v0.4.0-rc.2", scopes=("promote",))
        rc, out, _ = self.rel("promote", version="0.4.0")
        self.assertEqual((rc, out["status"], self.step(out, "predecessors")["state"]), (0, "NOT_RUN", "SHIPPED_AND_INTEGRATED"), out)


class Protection(Rel):
    AUTO_PIN = False

    PROT = {"url": "https://example.invalid/p", "required_status_checks": {"url": "u", "strict": True, "contexts": ["ci"]}, "enforce_admins": {"url": "u2", "enabled": True}}

    def prot(self, *extra, execute=False, branch="main"):
        return self.cli("protection", *extra, "--branch", branch, *(["--execute"] if execute else []))

    def test_snapshot_verify_and_drift(self):
        self.gh_update(protection=self.PROT)
        rc, out, _ = self.prot("snapshot")
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"))
        self.assertEqual(self.refused(self.prot("verify")[1]), "SNAPSHOT_UNPINNED")
        rc, out, _ = self.prot("snapshot", execute=True)
        self.assertEqual((rc, self.step(out, "snapshot")["state"]), (0, "RECORDED"))
        self.assertEqual(self.step(out, "snapshot")["evidence"]["protected"], True)
        self.assertEqual(self.refused(self.prot("verify")[1]), "SNAPSHOT_UNPINNED")  # recorded but not yet pinned by the owner
        self.pin_protection()
        rc, out, _ = self.prot("verify")
        self.assertEqual((rc, out["status"], self.step(out, "verify")["state"]), (0, "PASS", "UNCHANGED"))
        prot = json.loads(json.dumps(self.PROT))
        prot["url"] = "https://example.invalid/other"  # url-only change is not drift
        self.gh_update(protection=prot)
        self.assertEqual(self.prot("verify")[1]["status"], "PASS")
        prot["enforce_admins"]["enabled"] = False
        self.gh_update(protection=prot)
        rc, out, _ = self.prot("verify")
        self.assertEqual((rc, out["status"], self.step(out, "verify")["state"]), (1, "FAIL", "PROTECTION_DRIFT"))
        self.assertEqual(self.step(out, "verify")["evidence"]["changed"], ["enforce_admins"])
        self.assertIn("branch-protection-rollout.py", out["next_action"])
        self.assertEqual([c for c in self.calls() if not c.startswith("api")], [])  # read-only: only gh api calls

    def test_unprotected_branch_is_recorded_and_drift_detected_when_protection_appears(self):
        self.gh_update(protection={"message": "Branch not protected", "status": "404"})
        self.assertEqual(self.step(self.prot("snapshot", execute=True)[1], "snapshot")["evidence"]["protected"], False)
        self.pin_protection()
        self.gh_update(protection=self.PROT)
        rc, out, _ = self.prot("verify")
        self.assertEqual((rc, self.step(out, "verify")["state"]), (1, "PROTECTION_DRIFT"))
        self.assertIn("protected", self.step(out, "verify")["evidence"]["changed"])

    def test_promote_execute_fails_closed_without_a_pinned_untampered_snapshot(self):
        self.go()
        self.gh_update(protection=self.PROT)
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "SNAPSHOT_UNPINNED"))
        self.prot("snapshot", execute=True)
        self.assertEqual(self.refused(self.rel("promote", execute=True)[1]), "SNAPSHOT_UNPINNED")  # recorded, never pinned
        self.pin_protection()
        # the agent rewrites the snapshot through its own state dir: the pin no longer matches
        snap = next((self.state_dir / "protection").glob("*.json"))
        snap.write_text(json.dumps({"protected": False}))
        self.assertEqual(self.refused(self.rel("promote", execute=True)[1]), "SNAPSHOT_TAMPERED")
        snap.unlink()  # or removes it
        self.assertEqual(self.refused(self.rel("promote", execute=True)[1]), "NO_SNAPSHOT")
        self.assertEqual((self.merges(), self.heads("promote/")), ([], []))
        rc, out, _ = self.rel("promote")  # a dry run still reports
        self.assertEqual(out["status"], "NOT_RUN")

    def test_promote_reports_protection_drift_caused_during_the_admin_step(self):
        self.gh_update(protection=self.PROT)
        self.assertEqual(self.prot("snapshot", execute=True)[1]["status"], "PASS")
        self.pin_protection()
        self.go()
        self.gh_update(drift_on_merge=True)
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, out["status"]), (1, "FAIL"), out)
        self.assertEqual(self.step(out, "protection_after")["state"], "DRIFT")
        self.assertEqual(self.step(out, "protection_before")["state"], "UNCHANGED")
        self.assertIn("branch-protection-rollout.py", out["next_action"])
        self.assertEqual(len(self.merges()), 1)  # the merge itself happened; the drift is reported, never repaired by this tool


if __name__ == "__main__":
    unittest.main()
