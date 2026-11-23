#!/usr/bin/env python3
"""Tests for the SPE-184b release commands in scripts/release-line.py: build, notes, publish and `release run --until`.

Reuses the SPE-184a harness (bare origin + clone, git wrapper that logs every call, fake `gh` with real merges) and extends the fake gh with
`release view|create|download` backed by JSON state. The build fixture is a small python script run by the tool. No network, no workflow runs.
"""
import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_release_tests", ROOT / "tests/test-release-line-release.py")
relmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relmod)
base, train, REPO, REAL_GIT = relmod.base, relmod.train, relmod.REPO, relmod.REAL_GIT

FAKE_GH = relmod.FAKE_GH
_old = 'elif a[:2] == ["repo", "view"]:'
assert FAKE_GH.count(_old) == 1
FAKE_GH = FAKE_GH.replace(_old, '''elif a[:2] == ["release", "view"]:
    rel = st.get("releases", {{}}).get(a[2])
    if rel is None:
        sys.stderr.write("release not found\\\\n")
        sys.exit(1)
    print(json.dumps({{"tagName": a[2], "name": rel["title"], "body": rel["body"], "isDraft": rel.get("draft", False),
                      "assets": [{{"name": n}} for n in sorted(rel["assets"])]}}))
elif a[:2] == ["release", "download"]:
    import base64
    rel = st.get("releases", {{}}).get(a[2])
    if rel is None:
        sys.exit(1)
    for n, b in rel["assets"].items():
        open(os.path.join(opt("--dir"), n), "wb").write(base64.b64decode(b))
elif a[:2] == ["release", "create"]:
    import base64
    tag, rest, files, o, i = a[2], a[3:], [], {{}}, 0
    while i < len(rest):
        x = rest[i]
        if x in ("--repo", "--title", "--notes-file"):
            o[x] = rest[i + 1]
            i += 2
        elif x == "--verify-tag":
            o[x] = True
            i += 1
        elif x.startswith("--"):
            sys.exit(1)
        else:
            files.append(x)
            i += 1
    if tag in st.setdefault("releases", {{}}) or st.get("fail_create"):
        sys.exit(1)
    if subprocess.run(["{git}", "-C", st["bare"], "rev-parse", "-q", "--verify", "refs/tags/" + tag], capture_output=True).returncode != 0:
        if o.get("--verify-tag"):
            sys.exit(1)
        st.setdefault("created_tags", []).append(tag)
    assets = {{os.path.basename(f): base64.b64encode(open(f, "rb").read()).decode() for f in files}}
    if st.get("corrupt_upload"):
        assets[sorted(assets)[0]] = base64.b64encode(b"corrupt").decode()
    st["releases"][tag] = {{"title": o.get("--title"), "body": open(o["--notes-file"]).read(), "assets": assets}}
    json.dump(st, open(S, "w"))
''' + _old)

BUILD = ("import os, pathlib\n"
         "d = pathlib.Path('dist'); d.mkdir(exist_ok=True)\n"
         "(d / 'app.txt').write_text(pathlib.Path('feature-a.txt').read_text() + 'built\\n')\n"
         "(d / 'env.txt').write_text('\\n'.join(sorted(os.environ)))\n")
ALL = ("candidate_tag", "promote", "final_tag", "publish")


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Pub(relmod.Rel):
    def setUp(self):
        super().setUp()
        (self.bin / "gh").write_text(FAKE_GH.format(py=sys.executable, git=REAL_GIT, date=base.DATE))
        self.env["RELEASE_LEAK_MARKER"] = "do-not-leak"
        self.set_build([sys.executable, "-c", BUILD])
        self.top = self.state_dir / "releases"

    def set_build(self, argv, outputs=("dist/*",), **rl):
        p = self.v0_only_policy()
        e = base.entry(p)["release_lines"]
        e["build"] = {"argv": list(argv), "outputs": list(outputs)}
        e.update(rl)
        self.install(p)

    def ship(self, scopes=ALL, **over):
        """GO record, promotion and final tag; returns the final SHA."""
        self.go(scopes=scopes, **over)
        self.assertEqual(self.promote_ok()[1]["status"], "PASS")
        rc, out, _ = self.rel("final-tag", execute=True)
        self.assertEqual(rc, 0, out)
        return self.sha("v0.3.0")

    def promote_ok(self):
        return self.rel("promote", execute=True)

    def built(self):
        self.ship()
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        return out

    def notes(self, execute=True):
        rc, out, _ = self.rel("notes", execute=execute)
        self.assertEqual(rc, 0, out)
        return self.step(out, "notes")["evidence"]

    def prepared(self):
        """Build + notes + the owner's GO pin; returns the pinned publish_digest."""
        self.built()
        digest = self.notes()["publish_digest"]
        self.go(scopes=ALL, publish_digest=digest)
        return digest

    def gh_releases(self):
        return json.loads(self.gh_state.read_text()).get("releases", {})

    def creates(self):
        return [c for c in self.calls() if c.startswith("release create")]

    def vdir(self):
        return self.top / "0.3.0"


class Build(Pub):
    def test_dry_run_execute_stable_checksums_reuse_and_rebuild(self):
        self.ship()
        rc, out, _ = self.rel("build")
        self.assertEqual((rc, out["status"], self.step(out, "build")["state"]), (0, "NOT_RUN", "DRY_PLAN"), out)
        self.assertFalse(self.top.exists())
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "build")["state"]), (0, "PASS", "BUILT"), out)
        final = self.sha("v0.3.0")
        self.assertEqual(out["idempotency_key"], f"release:{REPO}:0.3.0:{final}")
        arts = {a["name"]: a["sha256"] for a in self.step(out, "build")["evidence"]["artifacts"]}
        self.assertEqual(sorted(arts), ["app.txt", "env.txt"])
        for name, digest in arts.items():
            self.assertEqual(sha((self.vdir() / name).read_bytes()), digest)
        self.assertEqual(arts["app.txt"], sha(b"feature-a.txt\nbuilt\n"))  # built from the exact final tree
        manifest = json.loads((self.top / "0.3.0.json").read_text())
        self.assertEqual((manifest["final_sha"], manifest["final_tag"], {a["name"]: a["sha256"] for a in manifest["artifacts"]}), (final, "v0.3.0", arts))
        journal = [json.loads(x) for x in (self.state_dir / "journal.jsonl").read_text().splitlines()]
        self.assertEqual([r["state"] for r in journal if r["state"] == "built"], ["built"])
        self.assertEqual(self.w("worktree", "list").count("\n"), 0)  # the build worktree is gone
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual(self.step(out, "build")["state"], "REUSED")
        (self.vdir() / "app.txt").write_bytes(b"changed")  # a damaged copy is rebuilt, to the same checksums
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual(self.step(out, "build")["state"], "BUILT")
        self.assertEqual({a["name"]: a["sha256"] for a in self.step(out, "build")["evidence"]["artifacts"]}, arts)

    def test_environment_is_scrubbed(self):
        self.built()
        env = (self.vdir() / "env.txt").read_text().split("\n")
        self.assertNotIn("RELEASE_LEAK_MARKER", env)
        self.assertNotIn("FAKE_GH_STATE", env)
        self.assertNotIn("GIT_LOG", env)
        # __CF_USER_TEXT_ENCODING is added by macOS itself when the interpreter starts
        self.assertEqual(sorted(set(env) - {"__CF_USER_TEXT_ENCODING"}), sorted(["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH"]))

    def test_refuses_without_final_tag_or_with_a_tag_elsewhere(self):
        self.go(scopes=ALL)
        self.assertEqual(self.refused(self.rel("build", execute=True)[1]), "FINAL_TAG_MISSING")
        self.assertEqual(self.promote_ok()[1]["status"], "PASS")
        self.assertEqual(self.refused(self.rel("build", execute=True)[1]), "FINAL_TAG_MISSING")  # promoted but not tagged
        self.tag("v0.3.0", "integration/v0.x")  # a tag that is not the promotion merge
        self.assertEqual(self.refused(self.rel("build", execute=True)[1]), "PROMOTION_ASSERTION_FAILED")
        self.assertFalse(self.top.exists())

    def test_fails_closed_on_exit_missing_output_timeout_and_unsafe_output(self):
        self.ship()
        cases = {
            "BUILD_FAILED": ([sys.executable, "-c", BUILD + "raise SystemExit(3)"], ("dist/*",), []),
            "BUILD_OUTPUT_MISSING": ([sys.executable, "-c", BUILD], ("dist/*", "out/*.tar"), []),
            "BUILD_TIMEOUT": ([sys.executable, "-c", "import time; time.sleep(30)"], ("dist/*",), ["--build-timeout", "1"]),
            "BUILD_OUTPUT_UNSAFE": ([sys.executable, "-c", BUILD + "os.symlink('/etc/hosts', 'dist/link')"], ("dist/*",), []),
            "BUILD_OUTPUT_PATTERN": ([sys.executable, "-c", BUILD], ("../*",), []),
            "BUILD_MUTATED_TREE": ([sys.executable, "-c", BUILD + "pathlib.Path('init.txt').write_text('x')"], ("dist/*",), []),
        }
        for code, (argv, outputs, extra) in cases.items():
            self.set_build(argv, outputs)
            rc, out, _ = self.rel("build", *extra, execute=True)
            self.assertEqual((rc, out["status"], self.refused(out)), (1, "FAIL", code), out)
            self.assertFalse(self.top.exists(), code)  # nothing is recorded for a failed build
        self.assertEqual(self.w("worktree", "list").count("\n"), 0)

    def test_requires_a_configured_build(self):
        self.ship()
        p = self.v0_only_policy()
        del base.entry(p)["release_lines"]["build"]
        self.install(p)
        self.assertEqual(self.refused(self.rel("build", execute=True)[1]), "BUILD_NOT_CONFIGURED")


class Notes(Pub):
    def test_content_determinism_and_reuse(self):
        self.assertEqual(self.refused(self.ship_then_notes_without_build()), "BUILD_MISSING")
        out = self.built()
        arts = {a["name"]: a["sha256"] for a in self.step(out, "build")["evidence"]["artifacts"]}
        final, cand = self.sha("v0.3.0"), self.sha("v0.3.0-rc.1")
        dry = self.notes(execute=False)
        self.assertFalse((self.top / "0.3.0.notes.md").exists())
        rc, out, _ = self.rel("notes", execute=True)
        ev = self.step(out, "notes")
        self.assertEqual((ev["state"], ev["evidence"]["sha256"]), ("WRITTEN", dry["sha256"]))  # the dry run prints the digest it would write
        text = (self.top / "0.3.0.notes.md").read_text()
        self.assertEqual(sha(text.encode()), ev["evidence"]["sha256"])
        for want in ("- Source branch: `integration/v0.x`", "- Release line: `v0`", "- Version: `0.3.0`", "- Promotion PR: #1",
                     "- Candidate tag: `v0.3.0-rc.1`", f"- Candidate SHA: `{cand}`", "- Final tag: `v0.3.0`", f"- Final SHA: `{final}`",
                     "- Publication base: `main`", "- Forward-port accounting: 0 base-only change(s) inspected, 0 not fully accounted",
                     *(f"{d}  {n}" for n, d in arts.items())):
            self.assertIn(want, text)
        self.assertEqual(self.step(self.rel("notes", execute=True)[1], "notes")["state"], "REUSED")
        self.assertEqual(self.notes()["sha256"], ev["evidence"]["sha256"])  # deterministic
        self.commit("main", "later.txt")  # base moves on: the notes describe the promotion, not the tip
        self.assertEqual(self.notes()["sha256"], ev["evidence"]["sha256"])

    def ship_then_notes_without_build(self):
        self.ship()
        return self.rel("notes", execute=True)[1]

    def test_refuses_a_tampered_artifact(self):
        self.built()
        (self.vdir() / "app.txt").write_bytes(b"x")
        self.assertEqual(self.refused(self.rel("notes", execute=True)[1]), "ARTIFACT_TAMPERED")


class Publish(Pub):
    def test_dry_run_execute_exact_argv_idempotent_rerun(self):
        digest = self.prepared()
        final = self.sha("v0.3.0")
        pushes = len(self.pushes())
        rc, out, _ = self.rel("publish")
        self.assertEqual((rc, out["status"], self.step(out, "release")["state"]), (0, "NOT_RUN", "DRY_PLAN"), out)
        self.assertEqual((self.creates(), self.gh_releases()), ([], {}))
        self.assertEqual(self.step(out, "content")["evidence"]["publish_digest"], digest)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "release")["state"], self.step(out, "verify")["state"]), (0, "PASS", "CREATED", "PASS"), out)
        self.assertEqual(out["idempotency_key"], f"release:{REPO}:0.3.0:{final}")
        (create,) = self.creates()
        self.assertRegex(create, rf"^release create v0\.3\.0 --repo {re.escape(REPO)} --verify-tag --title v0\.3\.0 --notes-file \S+ \S+/app\.txt \S+/env\.txt$")
        self.assertNotIn("--clobber", create)
        self.assertEqual(len(self.pushes()), pushes)  # no tag or branch push
        self.assertNotIn("created_tags", json.loads(self.gh_state.read_text()))  # the release is made for the existing tag
        self.assertEqual(self.sha("v0.3.0"), final)
        rel = self.gh_releases()["v0.3.0"]
        self.assertEqual(rel["body"], (self.top / "0.3.0.notes.md").read_text())
        self.assertEqual({n: sha(base64.b64decode(b)) for n, b in rel["assets"].items()},
                         {a["name"]: a["sha256"] for a in json.loads((self.top / "0.3.0.json").read_text())["artifacts"]})
        rc, out, _ = self.rel("publish", execute=True)  # idempotent
        self.assertEqual((rc, out["status"], self.step(out, "release")["state"]), (0, "PASS", "REUSED"), out)
        self.assertEqual(len(self.creates()), 1)
        journal = [json.loads(x) for x in (self.state_dir / "journal.jsonl").read_text().splitlines()]
        self.assertEqual([r["state"] for r in journal if r["state"].startswith("publish")], ["published"])
        self.assertFalse([c for c in self.calls() if "workflow" in c or c.startswith("run ")])
        self.assert_no_force()

    def test_go_scope_expiry_mismatch_and_pin(self):
        digest = self.prepared()
        self.go(scopes=("candidate_tag", "promote", "final_tag"), publish_digest=digest)
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_SCOPE")
        self.go(scopes=ALL, publish_digest=digest, expires_utc="2025-12-31T00:00:00Z")
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_EXPIRED")
        for over in ({"repo": "Other-Owner/other"}, {"version": "0.9.0"}, {"publication_base": "master"}):
            self.go(scopes=ALL, publish_digest=digest, **over)
            self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_MISMATCH", over)
        self.go(scopes=ALL, publish_digest=digest, candidate_sha="a" * 40)
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_MISMATCH")
        self.go(scopes=ALL)  # the owner has not pinned the content
        out = self.rel("publish", execute=True)[1]
        self.assertEqual(self.refused(out), "GO_DIGEST_UNPINNED")
        self.assertIn(digest, [s for s in out["steps"] if s["step"] == "refused"][0]["evidence"]["detail"])
        self.go(scopes=ALL, publish_digest="0" * 64)
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_DIGEST_MISMATCH")
        self.assertEqual(self.creates(), [])

    def test_unsupported_backend_and_channel_and_missing_go(self):
        digest = self.prepared()
        self.assertEqual(self.refused(self.rel("publish", "--backend", "npm", execute=True)[1]), "UNSUPPORTED_BACKEND")
        (self.go_dir / f"{REPO.replace('/', '__')}__0.3.0.json").unlink()
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_MISSING")
        self.go(scopes=ALL, publish_digest=digest)
        self.set_build([sys.executable, "-c", BUILD], channel="npm")
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"]), (0, "DEFERRED"), out)
        self.assertEqual(self.creates(), [])

    def test_tampering_between_build_and_publish_is_refused(self):
        digest = self.prepared()
        art, notes = self.vdir() / "app.txt", self.top / "0.3.0.notes.md"
        good_art, good_notes = art.read_bytes(), notes.read_bytes()
        art.write_bytes(b"evil")
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "ARTIFACT_TAMPERED")
        art.write_bytes(good_art)
        notes.write_bytes(good_notes + b"\nextra\n")
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "NOTES_TAMPERED")
        notes.write_bytes(good_notes)
        notes.unlink()
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "NOTES_MISSING")
        notes.write_bytes(good_notes)
        (self.vdir() / "env.txt").unlink()
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "ARTIFACT_MISSING")
        (self.vdir() / "env.txt").write_bytes(b"x")
        # an agent that rewrites the artifact, the manifest AND the notes consistently still cannot match the owner's pinned digest
        m = json.loads((self.top / "0.3.0.json").read_text())
        art.write_bytes(b"evil")
        for a in m["artifacts"]:
            a["sha256"] = sha(b"evil" if a["name"] == "app.txt" else b"x")
        (self.top / "0.3.0.json").write_text(json.dumps(m))
        self.assertEqual(self.notes()["sha256"] != sha(good_notes), True)
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "GO_DIGEST_MISMATCH")
        self.assertEqual(self.creates(), [])
        self.assertNotIn(digest, self.step(self.rel("publish", execute=True)[1], "refused")["evidence"]["detail"])

    def test_existing_release_identical_is_done_and_conflicting_is_refused(self):
        self.prepared()
        self.assertEqual(self.rel("publish", execute=True)[1]["status"], "PASS")
        st = json.loads(self.gh_state.read_text())
        rel = st["releases"]["v0.3.0"]
        good = json.loads(json.dumps(rel))
        for mutate in (lambda r: r.update(body="someone else's notes"),
                       lambda r: r["assets"].update({"app.txt": base64.b64encode(b"other").decode()}),
                       lambda r: r["assets"].update({"extra.bin": base64.b64encode(b"x").decode()}),
                       lambda r: r.update(draft=True)):
            r = json.loads(json.dumps(good))
            mutate(r)
            st["releases"]["v0.3.0"] = r
            self.gh_state.write_text(json.dumps(st))
            rc, out, _ = self.rel("publish", execute=True)
            self.assertEqual((rc, self.refused(out)), (1, "CONFLICT_EXISTING"), out)
        self.assertEqual(len(self.creates()), 1)  # never re-created, never overwritten
        st["releases"]["v0.3.0"] = dict(good, body=good["body"].replace("\n", "\r\n"))  # line-ending normalisation by GitHub is not a conflict
        self.gh_state.write_text(json.dumps(st))
        self.assertEqual(self.step(self.rel("publish", execute=True)[1], "release")["state"], "REUSED")

    def test_failed_create_and_corrupt_upload(self):
        self.prepared()
        self.gh_update(fail_create=True)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "FAIL", "PUBLISH_FAILED"))
        self.gh_update(fail_create=False, corrupt_upload=True)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "verify")["state"]), (1, "FAIL", "FAIL"), out)  # post-publish verification catches it
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "CONFLICT_EXISTING")  # and a rerun does not overwrite
        self.assertEqual(len(self.creates()), 2)

    def test_publish_needs_the_final_tag_on_the_promotion_merge(self):
        self.prepared()
        # a tag moved to another commit (not a promotion merge) is refused before anything is uploaded
        self.w("push", "-q", "--delete", "origin", "v0.3.0")
        self.w("tag", "-d", "v0.3.0")
        self.tag("v0.3.0", "integration/v0.x")
        self.assertEqual(self.refused(self.rel("publish", execute=True)[1]), "PROMOTION_ASSERTION_FAILED")
        self.assertEqual(self.creates(), [])


class Run(Pub):
    def steps(self, out):
        return [(s["step"], s["state"]) for s in out["steps"]]

    def test_until_stops_and_resumes_from_recomputed_facts(self):
        self.go(scopes=ALL)
        rc, out, _ = self.rel("run", "--until", "publish")  # dry run: reuses the candidate tag, stops at the first step that would act
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"), out)
        self.assertEqual(self.steps(out), [("candidate-tag", "PASS"), ("promote", "NOT_RUN")])
        self.assertEqual(self.merges(), [])
        rc, out, _ = self.rel("run", "--until", "promote", execute=True)
        self.assertEqual((rc, out["status"], self.steps(out)), (0, "PASS", [("candidate-tag", "PASS"), ("promote", "PASS")]), out)
        self.assertEqual(out["next_action"], "stopped after promote")
        rc, out, _ = self.rel("run", "--until", "final-tag", execute=True)
        self.assertEqual((rc, self.steps(out)), (0, [("candidate-tag", "PASS"), ("promote", "PASS"), ("verify", "PASS"), ("final-tag", "PASS")]), out)
        self.assertEqual(len(self.merges()), 1)
        rc, out, _ = self.rel("run", "--until", "build", execute=True)
        self.assertEqual((rc, self.steps(out)[-1]), (0, ("build", "PASS")), out)
        self.assertTrue((self.top / "0.3.0.json").exists())
        self.assertFalse((self.top / "0.3.0.notes.md").exists())
        rc, out, _ = self.rel("run", "--until", "publish", execute=True)  # the owner has not pinned the content: stops, blocked, at publish
        self.assertEqual((rc, out["status"], self.steps(out)[-1]), (1, "BLOCKED", ("publish", "REFUSED")), out)
        pub = [s for s in out["steps"] if s["step"] == "publish"][0]["evidence"]
        self.assertEqual(pub["code"], "GO_DIGEST_UNPINNED")
        digest = re.search(r"[0-9a-f]{64}", pub["detail"]).group(0)
        self.assertEqual(self.gh_releases(), {})
        self.go(scopes=ALL, publish_digest=digest)
        rc, out, _ = self.rel("run", "--until", "publish", execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertEqual([s[0] for s in self.steps(out)], ["candidate-tag", "promote", "verify", "final-tag", "build", "notes", "publish"])
        self.assertEqual(out["idempotency_key"], f"release:{REPO}:0.3.0:{self.sha('v0.3.0')}")
        self.assertEqual((len(self.creates()), len(self.merges())), (1, 1))
        rc, out, _ = self.rel("run", "--until", "publish", execute=True)  # fully resumable and idempotent
        self.assertEqual((rc, out["status"]), (0, "PASS"))
        self.assertEqual((len(self.creates()), len(self.merges())), (1, 1))
        self.assertFalse([c for c in self.calls() if "workflow" in c or c.startswith("run ")])
        self.assert_no_force()

    def test_stops_at_the_first_refusal_and_resumes_after_interruption(self):
        self.go(scopes=ALL)
        self.gh_update(merge_fail=True)
        rc, out, _ = self.rel("run", "--until", "publish", execute=True)
        self.assertEqual((rc, out["status"], self.steps(out)[-1]), (1, "BLOCKED", ("promote", "REFUSED")), out)
        self.assertEqual(len(self.steps(out)), 2)  # nothing after the refused step ran
        self.assertFalse(self.top.exists())
        self.gh_update(merge_fail=False)
        rc, out, _ = self.rel("run", "--until", "notes", execute=True)
        self.assertEqual((rc, out["status"], self.steps(out)[-1]), (0, "PASS", ("notes", "PASS")), out)
        self.assertEqual(len([c for c in self.calls() if c.startswith("pr create")]), 1)
        self.assertEqual(self.gh_releases(), {})

    def test_each_step_is_gated_by_its_own_go_scope(self):
        self.go(scopes=("candidate_tag", "promote"))
        rc, out, _ = self.rel("run", "--until", "build", execute=True)
        self.assertEqual((rc, self.steps(out)[-1], [s for s in out["steps"] if s["step"] == "final-tag"][0]["evidence"]["code"]),
                         (1, ("final-tag", "REFUSED"), "GO_SCOPE"), out)
        self.assertIsNone(self.remote("refs/tags/v0.3.0"))
        self.assertFalse(self.top.exists())

    def test_usage_errors(self):
        rc, _, _ = self.cli("release", "run", "--version", "0.3.0", "--clone", str(self.work))
        self.assertEqual(rc, 2)
        rc, _, _ = self.cli("release", "run", "--until", "deploy", "--version", "0.3.0", "--clone", str(self.work))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
