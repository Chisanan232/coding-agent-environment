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

FAKE_GH = train.FAKE_GH.replace('if a[:2] == ["pr", "list"]:', 'if a[:2] == ["pr", "list"] and st.get("list_fail"):\n    sys.exit(1)\nelif a[:2] == ["pr", "list"]:', 1)
for old, new in (
    ('    work = S + ".merge"\n', '''    mh = opt("--match-head-commit")
    if st.get("merge_fail"):
        sys.exit(1)
    if mh and subprocess.run(["{git}", "-C", st["bare"], "rev-parse", "refs/heads/" + pr["headRefName"]], capture_output=True, text=True).stdout.strip() != mh:
        sys.exit(1)
    if st.get("pre_merge"):
        subprocess.run([st["pre_merge"]], check=True)
    work = S + ".merge"
'''),
    ('    subprocess.run(["rm", "-rf", work])\n    pr["state"] = "MERGED"', '''    pr["mergeOid"] = g("rev-parse", "HEAD").stdout.decode().strip()
    subprocess.run(["rm", "-rf", work])
    if st.get("break_api_on_merge"):
        st["api_broken"] = True
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
    if "protection" not in st or st.get("api_broken"):
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
        cur = json.loads(pins.read_text()) if pins.exists() else {"schema": 1, "records": {REPO: {}}}
        cur["records"][REPO][final] = digest
        pins.write_text(json.dumps(cur))
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

    def test_local_lightweight_tag_is_replaced_by_an_annotated_one(self):
        tip = self.commit("integration/v0.x", "late.txt")
        self.go(tag="v0.3.0-rc.2", sha=tip)
        self.w("tag", "v0.3.0-rc.2", tip)  # lightweight, local only
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, self.step(out, "tag")["state"]), (0, "CREATED"), out)
        self.assertEqual(self.w("cat-file", "-t", "refs/tags/v0.3.0-rc.2", cwd=self.bare), "tag")

    def test_lightweight_tag_already_on_origin_is_not_reused(self):
        self.go()  # rc.1 exists on origin as a lightweight tag at the approved commit
        for execute in (False, True):
            rc, out, _ = self.rel("candidate-tag", execute=execute)
            self.assertEqual((rc, self.refused(out)), (1, "CONFLICT_EXISTING"))
            self.assertIn("lightweight", self.step(out, "refused")["evidence"]["detail"])

    def test_n_and_sha_arguments_are_validated(self):
        self.go()
        for bad in (("--n", "0"), ("--n", "-1"), ("--n", "x"), ("--sha", "--upload-pack=evil"), ("--sha", ".hidden")):
            self.assertEqual(self.rel("candidate-tag", *bad)[0], 2, bad)

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

    def test_final_tag_refuses_when_github_cannot_answer(self):
        self.go()
        self.assertEqual(self.promote_ok()[1]["status"], "PASS")
        self.gh_update(list_fail=True)
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "GH_UNAVAILABLE"))
        self.assertIsNone(self.remote("refs/tags/v0.3.0"))
        self.gh_update(list_fail=False)
        self.assertEqual(self.step(self.rel("final-tag", execute=True)[1], "tag")["state"], "CREATED")

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
            self.pin_verification(cand)  # pinned, so the refusal below is for the stated defect
            self.assertEqual(self.refused(self.promote_ok()[1]), "LOCAL_EVIDENCE_REQUIRED", bad)
        self.assertEqual(self.merges(), [])
        self.write_evidence(cand)
        (self.local / "release-verification.json").unlink()  # a perfect record the agent could have written is still not enough
        out = self.promote_ok()[1]
        self.assertEqual(self.refused(out), "LOCAL_EVIDENCE_REQUIRED")
        self.assertIn("OWNER_PIN_MISSING", self.step(out, "refused")["evidence"]["detail"])
        self.pin_verification(cand, digest="0" * 64)
        self.assertEqual(self.refused(self.promote_ok()[1]), "LOCAL_EVIDENCE_REQUIRED")  # a pin for another record does not match
        self.assertEqual(self.merges(), [])
        self.pin_verification(cand)
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
        self.pin_verification(cand)
        self.env["SOURCE_DATE_EPOCH"] = "1767225600"
        self.assertEqual(self.refused(self.promote_ok()[1]), "LOCAL_EVIDENCE_REQUIRED")
        # a record that is fresh on the real clock stays fresh when SOURCE_DATE_EPOCH claims the year 2020
        self.write_evidence(cand)
        self.pin_verification(cand)
        self.env["SOURCE_DATE_EPOCH"] = "1577836800"
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.step(out, "local_equivalent")["state"]), (0, "PASS", "PASS"), out)

    def test_required_checks_from_the_pinned_snapshot_must_succeed(self):
        self.go()
        prot = {"required_status_checks": {"strict": True, "contexts": ["ci"]}, "enforce_admins": {"enabled": True}}
        self.gh_update(protection=prot)
        self.pin_protection()
        for rollup in ([{"name": "other", "conclusion": "SUCCESS"}], [{"name": "ci", "conclusion": "SKIPPED"}], [{"name": "ci", "conclusion": "NEUTRAL"}], []):
            self.gh_update(checks=rollup)
            out = self.promote_ok()[1]
            self.assertEqual((self.step(out, "hosted_ci")["state"], self.refused(out)), ("NOT_RUN", "LOCAL_EVIDENCE_REQUIRED"), rollup)
        self.assertEqual(self.merges(), [])
        self.gh_update(checks=[{"name": "ci", "conclusion": "SUCCESS"}])
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, self.step(out, "hosted_ci")["state"]), (0, "CI_PASS"), out)
        self.assertEqual(self.step(out, "hosted_ci")["evidence"]["required"], ["ci"])

    def test_all_skipped_rollup_is_not_a_pass_even_with_no_required_checks(self):
        self.go()
        self.gh_update(checks=[{"name": "a", "conclusion": "SKIPPED"}, {"name": "b", "conclusion": "NEUTRAL"}])
        out = self.promote_ok()[1]
        self.assertEqual((self.step(out, "hosted_ci")["state"], self.refused(out)), ("NOT_RUN", "LOCAL_EVIDENCE_REQUIRED"))
        self.assertEqual(self.merges(), [])

    def test_unverifiable_protection_after_the_merge_is_not_a_pass(self):
        self.go()
        self.gh_update(break_api_on_merge=True)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"]), (1, "FAIL"), out)
        self.assertEqual(self.step(out, "protection_after")["state"], "NOT_RUN")
        self.assertIn("manually", out["next_action"])
        self.assertEqual(len(self.merges()), 1)

    def test_base_that_moves_before_the_merge_is_accounted_again(self):
        self.go()
        hook = pathlib.Path(self.tmp.name) / "move-base.sh"
        hook.write_text(f"#!/bin/sh\nset -e\nd=$(mktemp -d)\n{REAL_GIT} clone -q {self.bare} $d/w\ncd $d/w\necho moved > moved.txt\n{REAL_GIT} add .\n"
                        f"{REAL_GIT} commit -q -m moved\n{REAL_GIT} push -q origin HEAD:main\n")
        hook.chmod(0o755)
        self.gh_update(pre_merge=str(hook))
        rc, out, env = self.promote_ok()
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "FAIL", "BASE_MOVED_UNACCOUNTED"), out)
        self.assertEqual(self.step(out, "base_moved")["state"], "INCOMPLETE")
        self.assertEqual(len(self.merges()), 1)  # merged, and reported as such: nothing is silently accepted
        # sticky: a rerun recomputes from git and does not report a clean ALREADY_PROMOTED, and final-tag refuses too
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "FAIL", "BASE_MOVED_UNACCOUNTED"), out)
        self.assertEqual(self.step(out, "accounting_recheck")["state"], "INCOMPLETE")
        self.assertEqual(self.refused(self.rel("final-tag", execute=True)[1]), "BASE_MOVED_UNACCOUNTED")
        self.assertEqual(len(self.merges()), 1)
        # once the moved change is accounted (reviewed not-applicable record on the line) the recheck passes
        moved = self.w("rev-parse", "main^1", cwd=self.bare)
        self.mark_not_applicable(moved)
        rc, out, _ = self.promote_ok()
        self.assertEqual((rc, out["status"], self.step(out, "accounting_recheck")["state"]), (0, "PASS", "ACCOUNTED"), out)
        self.assertNotEqual(self.refused(self.rel("final-tag", execute=True)[1]), "BASE_MOVED_UNACCOUNTED")  # passes accounting; stops at verification

    def test_local_only_tags_do_not_count_as_shipped_or_latest(self):
        self.branch("maintenance/v0", "main")
        self.install(self.v0_only_policy(publication_base={"branch": "maintenance/v0", "kind": "maintenance"}))
        self.pin_protection("maintenance/v0")
        self.w("tag", "v0.9.0", "main")  # never pushed: not on origin
        self.go(publication_base="maintenance/v0")
        out = self.rel("promote")[1]
        self.assertEqual(self.step(out, "maintenance_base")["evidence"]["latest_final"], None)
        self.assertEqual(self.step(out, "maintenance_base")["state"], "OK")

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


class HardenedGit(Rel):
    def probe(self):
        spec = importlib.util.spec_from_file_location("rl_hardened_probe", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_replace_refs_cannot_forge_ancestry_or_a_promotion(self):
        mod = self.probe()
        root = self.w("commit-tree", "-m", "a", self.w("rev-parse", "main^{tree}"))
        other = self.w("commit-tree", "-m", "b", self.w("rev-parse", "main^{tree}"))
        self.assertFalse(mod.is_ancestor(self.work, root, other))
        # forge: replace `other` by a commit whose parent is `root`
        forged = self.w("commit-tree", "-m", "b", "-p", root, self.w("rev-parse", "main^{tree}"))
        self.w("replace", other, forged)
        plain = subprocess.run([REAL_GIT, "-C", str(self.work), "merge-base", "--is-ancestor", root, other], env=self.genv).returncode
        self.assertEqual(plain, 0)  # plain git is fooled ...
        self.assertFalse(mod.is_ancestor(self.work, root, other))  # ... the tool is not
        # forge a promotion merge: `q` is replaced by a merge whose second parent is the approved candidate
        cand = self.sha("v0.3.0-rc.1")
        self.w("fetch", "-q", "origin")
        q = self.w("commit-tree", "-m", "q", "-p", self.w("rev-parse", "origin/main"), self.w("rev-parse", "origin/main^{tree}"))
        self.w("branch", "-f", "fq", q)
        fake = self.w("commit-tree", "-m", "fake", "-p", self.w("rev-parse", "origin/main"), "-p", cand, self.w("rev-parse", "origin/main^{tree}"))
        self.w("replace", q, fake)
        plain = self.w("log", "--first-parent", "--merges", "--format=%H %P", "fq")
        self.assertIn(cand, plain)
        self.assertIsNone(mod.scan_promotion(self.work, "fq", cand))

    def test_clone_with_replace_refs_or_grafts_is_refused(self):
        self.go()
        a = self.w("rev-parse", "main")
        b = self.w("rev-parse", "integration/v0.x")
        self.w("replace", b, a)
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "CLONE_CONFIG_HOSTILE"))
        self.w("replace", "-d", b)
        self.assertNotEqual(self.refused(self.rel("candidate-tag")[1]), "CLONE_CONFIG_HOSTILE")
        (pathlib.Path(self.w("rev-parse", "--git-dir", cwd=self.work)) if False else pathlib.Path(self.work) / ".git" / "info").mkdir(exist_ok=True)
        (pathlib.Path(self.work) / ".git" / "info" / "grafts").write_text(f"{b} {a}\n")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "CLONE_CONFIG_HOSTILE")

    def test_inherited_git_environment_is_scrubbed_and_never_executes(self):
        marker = pathlib.Path(self.tmp.name) / "env-ran"
        script = pathlib.Path(self.tmp.name) / "envhook.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        script.chmod(0o755)
        hostile = {"GIT_SSH_COMMAND": str(script), "GIT_SSH": str(script), "GIT_PROXY_COMMAND": str(script), "GIT_ASKPASS": str(script), "SSH_ASKPASS": str(script),
                   "GIT_EXTERNAL_DIFF": str(script), "GIT_EXEC_PATH": "/nonexistent", "GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent",
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/nonexistent", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": str(script),
                   "GIT_CONFIG_PARAMETERS": f"'core.pager={script}'", "GIT_INDEX_FILE": "/nonexistent", "GIT_PAGER": str(script), "GIT_EDITOR": str(script)}
        self.env.update(hostile)
        tip = self.commit("integration/v0.x", "late.txt")
        self.go(tag="v0.3.0-rc.2", sha=tip)
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, self.step(out, "tag")["state"]), (0, "CREATED"), out)  # GIT_DIR=/nonexistent would have broken every call if inherited
        self.assertFalse(marker.exists())
        mod = self.probe()
        os.environ.update(hostile)
        try:
            env = mod.child_env({"X": "1"})
        finally:
            for k in hostile:
                os.environ.pop(k, None)
        self.assertEqual([k for k in env if k.startswith("GIT_") and k not in ("GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_CONFIG_NOSYSTEM", "GIT_NO_REPLACE_OBJECTS")], [])
        self.assertEqual((env["GIT_NO_REPLACE_OBJECTS"], env["GIT_CONFIG_NOSYSTEM"], env["GIT_TERMINAL_PROMPT"], env["X"]), ("1", "1", "0", "1"))
        self.assertNotIn("SSH_ASKPASS", env)


class VerifyBinding(Rel):
    def test_verify_is_bound_to_the_go_candidate_and_never_overwrites_a_pinned_record(self):
        x = self.base_only_change()
        self.mark_not_applicable(x)
        self.w("fetch", "-q", "origin")
        self.tag("v0.3.0-rc.2", "integration/v0.x")
        self.go(tag="v0.3.0-rc.2")
        self.assertEqual(self.rel("promote", execute=True)[1]["status"], "PASS")
        final = self.remote("refs/heads/main")
        self.go(tag="v0.3.0-rc.1")  # approves a different candidate: that promotion does not exist
        self.assertEqual(self.refused(self.rel("verify")[1]), "PROMOTION_NOT_FOUND")
        os.unlink(self.go_dir / f"{REPO.replace('/', '__')}__0.3.0.json")
        self.assertEqual(self.refused(self.rel("verify")[1]), "GO_MISSING")
        self.go(tag="v0.3.0-rc.2")
        self.local_first()
        extra = ("--local-first", str(self.lf))
        self.env.pop("SOURCE_DATE_EPOCH")
        out = self.rel("verify", *extra, execute=True)[1]
        self.assertEqual(out["status"], "PASS", out)
        self.pin_verification(final, digest=self.step(out, "local_checks")["evidence"]["record_sha256"])
        out = self.rel("verify", *extra, execute=True)[1]
        self.assertEqual(self.refused(out), "VERIFY_RECORD_PINNED")


class OriginBinding(Rel):
    def probe(self):
        spec = importlib.util.spec_from_file_location("rl_origin_probe", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_origin_urls_must_belong_to_the_repo(self):
        mod, r = self.probe(), REPO
        for good in (f"https://github.com/{r}.git", f"https://github.com/{r}", f"git@github.com:{r}.git", f"ssh://git@github.com/{r}.git",
                     f"https://github.com/{r.lower()}.git", f"https://user@github.com/{r}.git", f"https://user:tok%20en@github.com/{r}/"):
            self.assertTrue(mod.origin_matches(good, r), good)
        for bad in ("https://github.com/Other-Owner/example-product.git", f"https://github.com/{r}-evil.git", f"https://evil.example/{r}.git",
                    f"https://github.com/{r}/../x.git", f"git@evil.example:{r}.git", "/tmp/not-listed", "relative/path",
                    # authority tricks: git and curl end the authority at # ? / and a backslash
                    f"https://evil.host#@github.com/{r}.git", f"https://evil.host?@github.com/{r}.git", f"https://evil.host\\@github.com/{r}.git",
                    f"https://github.com@evil.host/{r}.git", f"https://github.com.evil.host/{r}.git", f"https://GitHub.com/{r}.git",
                    f"https://GITHUB.COM/{r}.git", f"https://github.com:8443/{r}.git", f"https://github.com/{r}.git?x=1", f"https://github.com/{r}.git#frag",
                    f"https://us er@github.com/{r}.git", f"https://us\ter@github.com/{r}.git", f"https://us\x01er@github.com/{r}.git",
                    f"https://us\x7fer@github.com/{r}.git", f"https://u@s@github.com/{r}.git", f"http://github.com/{r}.git", f"ftp://github.com/{r}.git",
                    f"ext::sh -c touch% x", f"git@github.com/{r}.git", f"git@github.com/{r}", f"github.com:{r}.git", f"other@github.com:{r}.git",
                    f"git@github.com:{r}.git\n", f"git@github.com:{r}.git evil"):
            self.assertFalse(mod.origin_matches(bad, r), repr(bad))

    def test_commands_refuse_another_repositorys_clone(self):
        self.go()
        self.w("remote", "set-url", "origin", "https://github.com/Other-Owner/other.git")
        rc, out, _ = self.rel("candidate-tag", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "ORIGIN_MISMATCH"))
        self.w("remote", "set-url", "origin", str(self.bare))
        self.w("remote", "set-url", "--push", "origin", "https://github.com/Other-Owner/other.git")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "ORIGIN_MISMATCH")  # push URL is checked too
        self.w("remote", "set-url", "--push", "origin", str(self.bare))
        self.assertNotEqual(self.refused(self.rel("candidate-tag")[1]), "ORIGIN_MISMATCH")

    def test_every_clone_using_handler_checks_the_origin(self):
        self.w("remote", "set-url", "origin", f"https://evil.host#@github.com/{REPO}.git")
        sha = self.sha("main")
        c = str(self.work)
        cmds = (("successor", "ensure", "--predecessor", "0.3.0", "--clone", c, "--execute"),
                ("guard", "--base", "integration/v0.x", "--head", sha, "--clone", c),
                ("train", "integrate", "--from", "v0", "--into", "v0.4.0", "--clone", c, "--execute"),
                ("train", "converge", "--line", "v0", "--clone", c, "--execute"),
                ("route", "--kind", "fix", "--version", "0.3.0", "--clone", c),
                ("audit", "observe", "--clone", c),
                ("forward-port", "plan", "--merge", sha, "--clone", c),
                ("forward-port", "run", "--merge", sha, "--clone", c, "--execute"),
                ("release", "candidate-tag", "--version", "0.3.0", "--clone", c, "--execute"),
                ("release", "promote", "--version", "0.3.0", "--clone", c, "--execute"),
                ("release", "verify", "--version", "0.3.0", "--clone", c, "--execute"),
                ("release", "final-tag", "--version", "0.3.0", "--clone", c, "--execute"))
        for argv in cmds:
            rc, out, _ = self.cli(*argv)
            self.assertEqual((rc, self.refused(out)), (1, "ORIGIN_MISMATCH"), argv)
        self.assertEqual((self.pushes(), self.merges()), ([], []))

    def test_ext_transport_origin_is_refused_without_running_anything(self):
        marker = pathlib.Path(self.tmp.name) / "ext-ran"
        self.w("remote", "set-url", "origin", f"ext::sh -c touch% {marker}")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "ORIGIN_MISMATCH")
        self.assertFalse(marker.exists())

    HOSTILE = (("gpg.program", "{m}"), ("tag.gpgsign", "true"), ("commit.gpgsign", "true"), ("log.showsignature", "true"), ("user.signingkey", "ABCD"),
               ("merge.x.driver", "{m}"), ("diff.x.command", "{m}"), ("diff.x.textconv", "{m}"), ("http.sslcapath", "/tmp/ca"), ("http.curloptresolve", "a:1:b"),
               ("http.sslverify", "false"), ("http.proxy", "http://127.0.0.1:9"), ("http.https://github.com/.proxy", "http://127.0.0.1:9"),
               ("http.https://github.com/.extraheader", "X-A: b"), ("include.path", "/tmp/other"), ("includeif.gitdir:/.path", "/tmp/other"),
               ("core.pager", "{m}"), ("core.editor", "{m}"), ("core.hookspath", "/tmp/hooks"), ("core.fsmonitor", "{m}"), ("core.sshcommand", "{m}"),
               ("core.askpass", "{m}"), ("core.gitproxy", "{m}"), ("credential.helper", "!{m}"), ("alias.x", "!{m}"), ("url.https://evil/.insteadof", "https://github.com/"),
               ("remote.origin.proxy", "http://127.0.0.1:9"), ("remote.origin.uploadpack", "{m}"), ("remote.origin.receivepack", "{m}"),
               ("filter.evil.clean", "{m}"), ("diff.external", "{m}"), ("extensions.worktreeconfig", "true"), ("protocol.ext.allow", "always"),
               ("gc.auto", "0"), ("pull.rebase", "true"))

    def test_clone_config_outside_the_allowlist_is_refused_and_never_runs(self):
        marker = pathlib.Path(self.tmp.name) / "hostile-ran"
        script = pathlib.Path(self.tmp.name) / "hostile.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        script.chmod(0o755)
        self.go()
        self.assertNotEqual(self.refused(self.rel("candidate-tag")[1]), "CLONE_CONFIG_HOSTILE")  # the clean clone passes
        for key, val in self.HOSTILE:
            self.w("config", "--add", key, val.format(m=script))
            for argv in (("release", "candidate-tag", "--version", "0.3.0", "--clone", str(self.work), "--go-dir", str(self.go_dir), "--execute"),
                         ("guard", "--base", "integration/v0.x", "--head", self.sha("main"), "--clone", str(self.work))):
                rc, out, _ = self.cli(*argv)
                self.assertEqual((rc, self.refused(out)), (1, "CLONE_CONFIG_HOSTILE"), key)
            self.w("config", "--unset-all", key)
        self.assertFalse(marker.exists())
        self.assertEqual((self.pushes(), self.merges()), ([], []))
        self.assertNotEqual(self.refused(self.rel("candidate-tag")[1]), "CLONE_CONFIG_HOSTILE")  # clean again

    def test_other_remotes_must_name_the_repo_too(self):
        self.go()
        self.w("remote", "add", "evil", "https://github.com/Other-Owner/other.git")
        self.assertEqual(self.refused(self.rel("candidate-tag", execute=True)[1]), "ORIGIN_MISMATCH")
        self.w("remote", "set-url", "evil", f"https://github.com/{REPO}.git")
        self.assertNotEqual(self.refused(self.rel("candidate-tag")[1]), "ORIGIN_MISMATCH")

    def test_git_invocation_overrides_signing_and_transport_levers(self):
        mod = self.probe()
        # the clone's own values cannot be set (refused above); the -c overrides still define what every git call sees
        for key, want in (("http.sslVerify", "true"), ("remote.origin.uploadpack", "git-upload-pack"), ("remote.origin.receivepack", "git-receive-pack"),
                          ("remote.origin.proxy", ""), ("http.extraHeader", ""), ("http.sslCAInfo", ""), ("http.sslCAPath", ""), ("http.curloptResolve", ""),
                          ("tag.gpgSign", "false"), ("commit.gpgSign", "false"), ("log.showSignature", "false"), ("gpg.program", "/usr/bin/false"),
                          ("merge.renormalize", "false"), ("core.sshCommand", "ssh"), ("core.askPass", "/usr/bin/false"), ("protocol.ext.allow", "never"),
                          ("http.proxy", ""), ("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false")):
            self.assertEqual(mod.git(self.work, "config", "--get", key)[1].strip(), want, key)
        self.assertEqual(mod.git(self.work, "config", "--get-all", "credential.helper")[1].split("\n")[-2], "")  # the last value is empty: helper list reset


class SuccessorPromotion(Rel):
    def setUp(self):
        super().setUp()
        train.TrainTests.two_successors_registry(self, [])

    def test_predecessor_must_be_shipped_and_integrated(self):
        self.commit("integration/v0.4.0", "succ-2.txt")
        self.tag("v0.4.0-rc.1", "integration/v0.4.0")
        self.go(version="0.4.0", tag="v0.4.0-rc.1", scopes=("promote",))
        self.assertEqual(self.refused(self.rel("promote", version="0.4.0")[1]), "PREDECESSOR_NOT_SHIPPED")
        self.w("tag", "v0.3.0", "main")  # a final tag only in the agent's clone is not a shipped version
        self.assertEqual(self.refused(self.rel("promote", version="0.4.0")[1]), "PREDECESSOR_NOT_SHIPPED")
        self.w("tag", "-d", "v0.3.0")
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
        self.gh_update(drift_on_merge=True, checks=[{"name": "ci", "conclusion": "SUCCESS"}])  # the pinned snapshot requires `ci`
        rc, out, _ = self.rel("promote", execute=True)
        self.assertEqual((rc, out["status"]), (1, "FAIL"), out)
        self.assertEqual(self.step(out, "protection_after")["state"], "DRIFT")
        self.assertEqual(self.step(out, "protection_before")["state"], "UNCHANGED")
        self.assertIn("branch-protection-rollout.py", out["next_action"])
        self.assertEqual(len(self.merges()), 1)  # the merge itself happened; the drift is reported, never repaired by this tool


if __name__ == "__main__":
    unittest.main()
