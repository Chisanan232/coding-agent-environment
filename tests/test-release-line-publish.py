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
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_release_tests", ROOT / "tests/test-release-line-release.py")
relmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relmod)
base, train, REPO, REAL_GIT = relmod.base, relmod.train, relmod.REPO, relmod.REAL_GIT

FAKE_GH = relmod.FAKE_GH
_old = 'elif a[:2] == ["repo", "view"]:'
assert FAKE_GH.count(_old) == 1
FAKE_GH = FAKE_GH.replace(_old, '''elif a[:2] == ["release", "view"]:
    open(S + ".host", "a").write(os.environ.get("GH_HOST", "") + "\\n")
    open(S + ".envs", "a").write(" ".join(os.environ.get(k, "") for k in ("HOME", "GH_CONFIG_DIR", "XDG_CONFIG_HOME")) + "\\n")
    rel = st.get("releases", {{}}).get(a[2])
    if rel is None:
        sys.stderr.write("release not found\\\\n")
        sys.exit(1)
    open(S + ".cwd", "a").write(os.getcwd() + "\\n")
    if st.get("fail_view"):
        sys.exit(1)
    if st.get("bad_assets"):
        print(json.dumps({{"tagName": a[2], "name": a[2], "body": "", "isDraft": False, "assets": [1]}}))
        sys.exit(0)
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
    open(S + ".tok", "a").write("1" if os.environ.get("GH_TOKEN") else "0")
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
        for cmd in (("push", "-q", "--delete", "origin", "v0.3.0-rc.1"), ("tag", "-d", "v0.3.0-rc.1"), ("tag", "-a", "-m", "rc", "v0.3.0-rc.1", "integration/v0.x"),
                    ("push", "-q", "origin", "v0.3.0-rc.1")):
            self.w(*cmd)  # release tags are annotated; the harness fixture tag is lightweight
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

    def notes_text(self):
        self.built()
        self.notes()
        return (self.top / "0.3.0.notes.md").read_text()

    def test_notes_without_a_prior_release_record_verifications_and_no_deferrals(self):
        text = self.notes_text()
        for want in ("- Prior release: none", "- Comparison: none", "## Verifications", "- Final tree verification: CARRIED_OVER", "- Hosted CI: CI_PASS (journal-recorded, unverified)",
                     "- Local-equivalent evidence: none", "- Branch protection after promotion: UNCHANGED (journal-recorded, unverified)", "## Deferrals\n\n- none\n"):
            self.assertIn(want, text)
        self.assertLess(text.index("## Deferrals"), text.index("## Artifact checksums (sha256)"))

    def test_notes_name_the_prior_release_and_the_comparison_range_from_git(self):
        self.w("fetch", "-q", "--tags", "origin")
        self.w("tag", "-a", "-m", "v0.2.0", "v0.2.0", "origin/main")
        self.w("push", "-q", "origin", "v0.2.0")
        self.w("tag", "-a", "-m", "v0.9.0", "v0.9.0", "origin/main")  # a later version is never the prior release of 0.3.0
        self.w("push", "-q", "origin", "v0.9.0")
        text = self.notes_text()
        self.assertRegex(text, r"- Prior release: `v0\.2\.0` \(local tags\)\n- Comparison: `v0\.2\.0\.\.\.v0\.3\.0` \([0-9]+ commit\(s\)\)\n")
        self.assertNotIn("v0.9.0", text)

    def test_notes_list_not_run_checks_as_deferrals_and_ignore_unrecorded_journal_values(self):
        self.built()
        journal = self.state_dir / "journal.jsonl"
        rows = [json.loads(x) for x in journal.read_text().splitlines()]
        kept = []
        for r in rows:
            if r.get("state") == "protection-after":
                continue  # a promotion whose after-merge protection check left no record
            if r.get("state") == "promoted":
                r["hosted_ci"], r["local_equivalent_sha256"] = "NOT_RUN", "z" * 64  # a value outside the closed set is dropped
            kept.append(r)
        journal.write_text("".join(json.dumps(r) + "\n" for r in kept))
        self.notes()
        text = (self.top / "0.3.0.notes.md").read_text()
        for want in ("- Hosted CI: NOT_RUN", "- Local-equivalent evidence: none", "- Branch protection after promotion: not recorded",
                     "- Hosted CI: NOT_RUN\n- Protection check after promotion: NOT_VERIFIED (no record)\n"):
            self.assertIn(want, text)

    def rewrite_journal(self, edit):
        journal = self.state_dir / "journal.jsonl"
        rows = [json.loads(x) for x in journal.read_text().splitlines()]
        journal.write_text("".join(json.dumps(r) + "\n" for r in edit(rows)))

    def test_protection_drift_is_not_overwritten_by_a_later_ok_row_and_is_deferred(self):
        self.built()

        def edit(rows):
            row = next(r for r in rows if r.get("state") == "protection-after")
            row["result"] = "DRIFT"
            return rows + [dict(row, result="UNCHANGED")]
        self.rewrite_journal(edit)
        self.notes()
        text = (self.top / "0.3.0.notes.md").read_text()
        self.assertIn("- Branch protection after promotion: DRIFT (journal-recorded, unverified)", text)
        self.assertIn("- Protection check after promotion: DRIFT\n", text)

    def test_a_protection_row_for_another_final_sha_is_ignored(self):
        self.built()
        self.rewrite_journal(lambda rows: [dict(r, final_sha="0" * 40) if r.get("state") == "protection-after" else r for r in rows])
        self.notes()
        text = (self.top / "0.3.0.notes.md").read_text()
        self.assertIn("- Branch protection after promotion: not recorded", text)
        self.assertIn("- Protection check after promotion: NOT_VERIFIED (no record)", text)

    def test_an_unbound_drift_row_stays_visible_and_an_unbound_ok_row_is_not_a_pass(self):
        self.built()

        def edit(rows):
            row = next(r for r in rows if r.get("state") == "protection-after")
            return [r for r in rows if r is not row] + [dict(row, final_sha=None, merged=False, result="DRIFT"), dict(row, final_sha=None, merged=False, result="UNCHANGED")]
        self.rewrite_journal(edit)
        self.notes()
        text = (self.top / "0.3.0.notes.md").read_text()
        self.assertIn("- Branch protection after promotion: DRIFT (journal-recorded, unverified)", text)
        self.assertIn("- Protection check after promotion: DRIFT\n", text)
        self.rewrite_journal(lambda rows: [dict(r, result="UNCHANGED") if r.get("state") == "protection-after" and r.get("result") == "DRIFT" else r for r in rows])
        self.notes()
        self.assertIn("- Branch protection after promotion: not recorded", (self.top / "0.3.0.notes.md").read_text())

    def test_a_valid_local_equivalent_digest_is_rendered_with_the_unverified_label(self):
        self.built()
        self.rewrite_journal(lambda rows: [dict(r, local_equivalent_sha256="ab" * 32) if r.get("state") == "promoted" else r for r in rows])
        self.notes()
        self.assertIn(f"- Local-equivalent evidence: `{'ab' * 32}` (journal-recorded, unverified)", (self.top / "0.3.0.notes.md").read_text())

    def test_prerelease_other_major_and_non_ancestor_tags_are_not_the_prior_release(self):
        self.w("fetch", "-q", "--tags", "origin")
        self.w("tag", "-a", "-m", "x", "v0.2.0-rc.1", "origin/main")  # a prerelease is not a final tag of this format
        self.w("tag", "-a", "-m", "x", "v1.0.0", "origin/main")  # another major
        self.w("checkout", "-q", "-b", "side", "origin/main")
        (self.work / "side.txt").write_text("s\n")
        self.w("add", ".")
        self.w("commit", "-q", "-m", "side")
        self.w("tag", "-a", "-m", "x", "v0.1.5", "side")  # not an ancestor of the base
        for t in ("v0.2.0-rc.1", "v1.0.0", "v0.1.5"):
            self.w("push", "-q", "origin", t)
        text = self.notes_text()
        self.assertIn("- Prior release: none\n- Comparison: none", text)

    def test_deferrals_are_capped_at_twenty_with_a_count(self):
        spec = importlib.util.spec_from_file_location("release_line_notes_unit", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bad = [{"source": f"{i:012x}", "line": "v0", "code": "TO_PORT"} for i in range(23)]
        c = types.SimpleNamespace(rl={"candidate_format": "v{version}-rc.{n}"}, version="0.3.0", base="main", ln={"integration_branch": "integration/v0.x", "id": "v0"})
        f = types.SimpleNamespace(cand="c" * 40, final="f" * 40, tag="v0.3.0", first="b" * 40, pr=1)
        facts = {"hosted_ci": "CI_PASS", "local_equivalent": None, "protection_after": "UNCHANGED"}
        with mock.patch.multiple(mod, tag_names=lambda *a: [], commit_of=lambda *a: None, accounting_complete=lambda *a: (23, bad), prior_release=lambda *a: None,
                                 journal_facts=lambda *a: facts, pinned_digest=lambda *a: None, git=lambda *a, **k: (0, "t\n")):
            text = mod.render_notes(types.SimpleNamespace(clone="x", repo="o/r"), c, f, {"artifacts": []}).decode()
        self.assertEqual(text.count("- Forward-port not merged: `"), 20)
        self.assertIn("- Forward-port not merged: 3 more\n", text)

    def test_notes_bytes_are_identical_for_identical_inputs(self):
        self.built()
        first = self.notes()
        again = self.notes()
        self.assertEqual((first["sha256"], first["publish_digest"]), (again["sha256"], again["publish_digest"]))

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
        self.assertEqual([r["state"] for r in journal if r["state"].startswith("publish")], ["publishing", "published"])
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


GIT_TOKEN_WRAPPER = """#!/bin/sh
[ -n "${{GH_TOKEN+x}}${{GITHUB_TOKEN+x}}" ] && echo leak >> "$GIT_LOG.leak"
printf '%s\\n' "$*" >> "$GIT_LOG"
exec {git} "$@"
"""
HOSTILE_BUILD = (BUILD + "import re, subprocess\n"
                 "gd = re.search(r'gitdir: (.*)', pathlib.Path('.git').read_text()).group(1).strip()\n"
                 "common = os.path.dirname(os.path.dirname(gd))\n"
                 "marker = os.environ['TMPDIR'] + '/../filter-ran'\n"
                 "open(common + '/config', 'a').write('[filter \"x\"]\\n\\tclean = touch ' + marker + '\\n')\n"
                 "os.makedirs(common + '/info', exist_ok=True)\n"
                 "open(common + '/info/attributes', 'a').write('* filter=x\\n')\n")


class Hardened(Pub):
    def probe(self):
        spec = importlib.util.spec_from_file_location("rl_publish_probe", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def journal(self):
        return [json.loads(x) for x in (self.state_dir / "journal.jsonl").read_text().splitlines()]

    def pub_states(self):
        keep = {"publishing", "created", "published", "publish-unverified", "publish-failed", "reused"}
        return [r["state"] for r in self.journal() if r["state"] in keep]

    def test_git_children_never_receive_a_github_token_but_gh_does(self):
        mod = self.probe()
        seen = []
        real = mod.subprocess.run

        def spy(argv, **kw):
            seen.append((os.path.basename(argv[0]), dict(kw["env"])))
            return real(["true"], **{k: v for k, v in kw.items() if k != "env"}, env={"PATH": "/usr/bin:/bin"})
        os.environ.update({"GH_TOKEN": "t-not-real", "GITHUB_TOKEN": "t-not-real"})
        mod.subprocess.run = spy
        mod._tool_cache.update({"git": "/usr/bin/git", "gh": "/usr/bin/gh"})
        try:
            mod.run3(["git", "--version"])
            mod.run3(["gh", "--version"])
        finally:
            mod.subprocess.run = real
            os.environ.pop("GH_TOKEN"), os.environ.pop("GITHUB_TOKEN")
        (git_env,), (gh_env,) = [e for n, e in seen if n == "git"], [e for n, e in seen if n == "gh"]
        for k in ("GH_TOKEN", "GITHUB_TOKEN"):
            self.assertFalse(k in git_env, k)
        self.assertTrue("GH_TOKEN" in gh_env)

    def test_tokens_reach_gh_only_and_never_the_envelope_or_journal(self):
        (self.bin / "git").write_text(GIT_TOKEN_WRAPPER.format(git=REAL_GIT))
        self.env["GH_TOKEN"] = "fake-not-a-real-token-123"
        self.env["GITHUB_TOKEN"] = "fake-not-a-real-token-456"
        self.prepared()
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        self.assertFalse(pathlib.Path(str(self.git_log) + ".leak").exists())  # no git child ever saw a token
        self.assertEqual(pathlib.Path(str(self.gh_state) + ".tok").read_text(), "1")  # gh did
        blob = json.dumps(out) + (self.state_dir / "journal.jsonl").read_text()
        env_txt = (self.vdir() / "env.txt").read_text().split("\n")
        for secret in ("fake-not-a-real-token-123", "fake-not-a-real-token-456"):
            self.assertNotIn(secret, blob)
        for k in ("GH_TOKEN", "GITHUB_TOKEN"):
            self.assertFalse(k in env_txt, k)  # and the build never saw them

    def test_a_build_that_plants_a_filter_in_the_clone_is_refused_and_never_runs_it(self):
        self.ship()
        (self.bin / "git").write_text(GIT_TOKEN_WRAPPER.format(git=REAL_GIT))
        self.env["GH_TOKEN"] = "fake-not-a-real-token-123"
        self.set_build([sys.executable, "-c", HOSTILE_BUILD])
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual((rc, self.refused(out)), (1, "CLONE_CONFIG_HOSTILE"), out)
        self.assertFalse(self.top.exists())  # nothing recorded
        self.assertFalse((pathlib.Path(self.tmp.name) / "filter-ran").exists())
        self.assertFalse(any(pathlib.Path(self.tmp.name).glob("**/filter-ran")))
        self.assertFalse(pathlib.Path(str(self.git_log) + ".leak").exists())

    def test_info_attributes_with_a_driver_is_refused_by_the_clone_check(self):
        mod = self.probe()
        mod.require_clone(self.work, None)
        info = pathlib.Path(self.work) / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* filter=x\n")
        with self.assertRaises(mod.Refusal) as cm:
            mod.require_clone(self.work, None)
        self.assertEqual(cm.exception.code, "CLONE_CONFIG_HOSTILE")
        (info / "attributes").write_text("*.png binary\n")
        mod.require_clone(self.work, None)

    def test_build_process_group_is_killed_on_normal_exit(self):
        self.ship()
        pidfile = pathlib.Path(self.tmp.name) / "child.pid"
        spawn = ("import subprocess, sys\n"
                 f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                 f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n")
        self.set_build([sys.executable, "-c", BUILD + spawn])
        rc, out, _ = self.rel("build", execute=True)
        self.assertEqual((rc, out["status"]), (0, "PASS"), out)
        pid = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("the build's background process survived the build")

    def test_verification_outage_after_create_is_a_journaled_fail_not_blocked(self):
        self.prepared()
        self.gh_update(fail_view=True)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "release")["state"], self.step(out, "verify")["state"]), (1, "FAIL", "CREATED", "FAIL"), out)
        self.assertEqual(len(self.creates()), 1)
        states = self.pub_states()
        self.assertEqual(states, ["publishing", "created", "publish-unverified"])

    def test_created_is_journaled_before_verification_and_reuse_and_failure_are_journaled(self):
        self.prepared()
        self.gh_update(fail_create=True)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual(self.refused(out), "PUBLISH_FAILED")
        self.assertEqual(self.pub_states(), ["publishing", "publish-failed"])
        self.gh_update(fail_create=False)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.pub_states(), ["publishing", "publish-failed", "publishing", "created", "published"])
        rc, out, _ = self.rel("publish")  # dry run of a reuse writes nothing
        self.assertEqual(self.step(out, "release")["state"], "REUSED")
        self.assertEqual(len(self.pub_states()), 5)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual(self.step(out, "release")["state"], "REUSED")
        self.assertEqual(self.pub_states()[-1], "reused")

    def test_publishing_intent_is_journaled_before_create_and_dry_run_writes_nothing(self):
        self.prepared()
        self.rel("publish")
        self.assertEqual(self.pub_states(), [])
        self.gh_update(fail_create=True)
        self.rel("publish", execute=True)
        self.assertEqual(self.pub_states(), ["publishing", "publish-failed"])

    def test_malformed_release_json_after_create_is_a_journaled_fail(self):
        self.prepared()
        self.gh_update(bad_assets=True)
        rc, out, _ = self.rel("publish", execute=True)
        self.assertEqual((rc, out["status"], self.step(out, "verify")["state"]), (1, "FAIL", "FAIL"), out)
        self.assertEqual(self.pub_states(), ["publishing", "created", "publish-unverified"])

    def test_gh_runs_in_a_fresh_empty_directory_not_the_callers(self):
        self.prepared()
        self.rel("publish", execute=True)
        cwds = set(pathlib.Path(str(self.gh_state) + ".cwd").read_text().split())
        self.assertTrue(cwds)
        for d in cwds:
            self.assertTrue(os.path.basename(d).startswith("release-gh-"), d)
            self.assertNotEqual(os.path.realpath(d), os.path.realpath(os.getcwd()))
            self.assertFalse(os.path.exists(d))  # removed after each call

    def test_a_github_outage_fails_closed_in_resolve_final(self):
        self.prepared()
        self.gh_update(list_fail=True)
        for cmd in ("notes", "build", "publish"):
            rc, out, _ = self.rel(cmd, execute=True)
            self.assertEqual((rc, out["status"]), (1, "BLOCKED"), (cmd, out))
            self.assertEqual(self.refused(out), "GH_UNAVAILABLE", cmd)
        self.assertEqual(self.creates(), [])


class Hardening(Pub):
    def test_child_env_pins_gh_host_path_and_drops_enterprise_settings(self):
        spec = importlib.util.spec_from_file_location("rl_mod", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hostile = {"GH_HOST": "evil.example", "GH_CONFIG_DIR": "/tmp/evil", "GH_ENTERPRISE_TOKEN": "x", "GITHUB_ENTERPRISE_TOKEN": "x",
                   "PATH": "/tmp/decoy:/usr/bin", "GIT_SSH_COMMAND": "evil"}
        old = {k: os.environ.get(k) for k in hostile}
        os.environ.update(hostile)
        try:
            env = mod.child_env()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual((env["GH_HOST"], env["PATH"]), ("github.com", "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"))
        for k in ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GIT_SSH_COMMAND"):
            self.assertFalse(k in env, k)  # never assertNotIn(k, env): a failure would print the whole environment, tokens included
        self.assertNotEqual(env["GH_CONFIG_DIR"], "/tmp/evil")

    def test_tools_resolve_from_the_fixed_path_not_the_callers_path(self):
        decoy = pathlib.Path(self.tmp.name) / "decoy"
        decoy.mkdir()
        for n in ("gh", "git"):
            (decoy / n).write_text(f"#!/bin/sh\ntouch {decoy}/used-{n}\nexit 1\n")
            (decoy / n).chmod(0o755)
        (self.local / "release-line-test-env.json").unlink()  # no hook: the tool must resolve git itself
        self.env["PATH"] = f"{decoy}:{self.env['PATH']}"
        self.env["GH_HOST"] = "evil.example"
        rc, out, _ = self.cli("route", "--kind", "fix", "--version", "0.3.0", "--clone", str(self.work))
        self.assertFalse(list(decoy.glob("used-*")), "a git or gh earlier in the caller's PATH was run")

    def test_gh_is_pinned_to_github_com_and_the_hooked_binary_is_used(self):
        self.prepared()
        decoy = pathlib.Path(self.tmp.name) / "decoy"
        decoy.mkdir()
        (decoy / "gh").write_text(f"#!/bin/sh\ntouch {decoy}/used-gh\nexit 1\n")
        (decoy / "gh").chmod(0o755)
        self.env.update({"PATH": f"{decoy}:{self.env['PATH']}", "GH_HOST": "evil.example", "GH_CONFIG_DIR": str(decoy)})
        rc, out, _ = self.rel("publish")
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"), out)
        self.assertFalse(list(decoy.glob("used-*")))
        hosts = set(pathlib.Path(str(self.gh_state) + ".host").read_text().split())
        self.assertEqual(hosts, {"github.com"})


class Trust(Pub):
    def load_module(self):
        spec = importlib.util.spec_from_file_location("rl_trust_mod", ROOT / "scripts/release-line.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_home_and_config_dirs_come_from_the_account_database_not_the_environment(self):
        self.prepared()
        evil = pathlib.Path(self.tmp.name) / "evil-home"
        (evil / ".config/gh").mkdir(parents=True)
        (evil / ".config/coding-agent-environment/local").mkdir(parents=True)
        (evil / ".config/coding-agent-environment/local/version-branches.json").write_text("{}")
        self.env.update({"HOME": str(evil), "GH_CONFIG_DIR": str(evil / ".config/gh"), "XDG_CONFIG_HOME": str(evil / ".config")})
        rc, out, _ = self.rel("publish")  # owner files (policy, GO record, pins) are still found under the real home
        self.assertEqual((rc, out["status"]), (0, "NOT_RUN"), out)
        seen = {tuple(x.split()) for x in pathlib.Path(str(self.gh_state) + ".envs").read_text().splitlines()}
        home = str(self.home)
        self.assertEqual(seen, {(home, f"{home}/.config/gh", f"{home}/.config")})
        rc, out, _ = self.cli("registry", "validate", policy=False)  # default policy path: the real home's, not the hostile one
        self.assertEqual(rc, 0, out)
        mod, old = self.load_module(), os.environ.get("HOME")
        try:
            os.environ.update({"HOME": str(evil), "GH_CONFIG_DIR": "/x", "XDG_CONFIG_HOME": "/y"})
            env = mod.child_env()
        finally:
            os.environ["HOME"] = old
            os.environ.pop("GH_CONFIG_DIR"), os.environ.pop("XDG_CONFIG_HOME")
        self.assertNotEqual(env["HOME"], str(evil))
        self.assertEqual((env["GH_CONFIG_DIR"], env["XDG_CONFIG_HOME"]), (env["HOME"] + "/.config/gh", env["HOME"] + "/.config"))

    def test_world_writable_tool_on_the_search_path_is_refused(self):
        evil = pathlib.Path(self.tmp.name) / "evil-bin"
        evil.mkdir()
        for n in ("git", "gh"):
            (evil / n).write_text(f"#!/bin/sh\ntouch {evil}/used-{n}\nexit 1\n")
            (evil / n).chmod(0o777)
        hook = self.local / "release-line-test-env.json"
        hook.write_text(json.dumps({"schema": 1, "pass": ["FAKE_GH_STATE"], "trusted_path": [str(evil)]}))
        os.chmod(hook, 0o600)
        rc, out, _ = self.cli("route", "--kind", "fix", "--version", "0.3.0", "--clone", str(self.work))
        self.assertEqual((rc, out["status"], self.refused(out)), (1, "BLOCKED", "TOOL_PATH_UNTRUSTED"), out)
        self.assertIn("git", [s for s in out["steps"] if s["step"] == "refused"][0]["evidence"]["detail"])
        self.assertFalse(list(evil.glob("used-*")))

    def test_each_component_of_the_chain_is_checked(self):
        mod = self.load_module()
        root = pathlib.Path(self.tmp.name) / "chain"
        (root / "inner").mkdir(parents=True)
        tool = root / "inner/gh"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        self.assertIsNone(mod.untrusted_component(str(tool)))
        for target, mode in ((root, 0o775), (root, 0o757), (root / "inner", 0o777)):
            target.chmod(mode)
            got = mod.untrusted_component(str(tool))
            self.assertIn(os.path.realpath(got), (os.path.realpath(root), os.path.realpath(root / "inner")), (target, mode))
            target.chmod(0o755)
        tool.chmod(0o775)  # group-writable binary
        self.assertEqual(os.path.realpath(mod.untrusted_component(str(tool))), os.path.realpath(tool))
        tool.chmod(0o755)
        link = root / "link-gh"
        link.symlink_to(tool)
        self.assertIsNone(mod.untrusted_component(str(link)))
        root.chmod(0o777)  # the directory holding the symlink can swap it
        self.assertEqual(os.path.realpath(mod.untrusted_component(str(link))), os.path.realpath(root))
        root.chmod(0o755)


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
