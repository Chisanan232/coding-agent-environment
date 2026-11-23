#!/usr/bin/env python3
"""Offline unit tests for the pure helpers of tests/acceptance/release-line-acceptance.py (no host session, no fixture, no network)."""
import contextlib
import importlib.util
import io
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rla", ROOT / "tests/acceptance/release-line-acceptance.py")
rla = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rla)


def rec(*argv, rc=0, status="PASS", refused=None):
    steps = [{"step": "refused", "state": refused}] if refused else []
    return {"argv": list(argv), "rc": rc, "envelope": {"status": status, "steps": steps}}


def evidence(**over):
    ev = {"answer": "", "records": [], "gh_calls": [], "shim_argv": [], "refs_before": "r", "refs_after": "r", "pushes_before": 1, "pushes_after": 1,
          "journal_publish_states": [], "canonical_script": pathlib.Path("/canon/scripts/release-line.py"), "bare_main_before": "a",
          "bare_main_after": "a", "merge_flags_bad": [], "final_is_merge_commit": True}
    ev.update(over)
    return ev


class Workdir(unittest.TestCase):
    def test_refuses_home_repo_overlap_and_foreign_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp).resolve()
            foreign = tmp / "foreign"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("x")
            for bad in (pathlib.Path.home(), pathlib.Path("/"), foreign, ROOT, ROOT / "tests"):
                with self.subTest(bad=str(bad)), self.assertRaises(rla.RefusedWorkdir):
                    rla.claim_workdir(bad, ROOT)
            self.assertEqual((foreign / "keep.txt").read_text(), "x")

    def test_fresh_directory_gets_a_sentinel_and_only_owned_children_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = rla.claim_workdir(pathlib.Path(tmp) / "w", ROOT)
            self.assertTrue((work / rla.SENTINEL).is_file())
            (work / "fx").mkdir()
            (work / "fx" / "a").write_text("1")
            (work / "result.json").write_text("[]")
            (work / "mine.txt").write_text("keep")
            again = rla.claim_workdir(work, ROOT)
            self.assertEqual(again, work)
            self.assertFalse((work / "fx").exists() or (work / "result.json").exists())
            self.assertEqual((work / "mine.txt").read_text(), "keep")


class Helpers(unittest.TestCase):
    def test_redact_substitutes_paths_and_removes_token_shapes(self):
        text = "at /work/dir with ghp_" + "a" * 24 + " and sk-" + "b" * 24
        out = rla.redact(text, (("/work/dir", "<WORKDIR>"),))
        self.assertIn("<WORKDIR>", out)
        self.assertNotIn("ghp_", out)
        self.assertNotIn("sk-bbbb", out)

    def test_parse_csv_and_plan(self):
        self.assertEqual(rla.parse_csv("R1,R7", rla.CASES, "case"), ["R1", "R7"])
        self.assertEqual(rla.parse_csv(None, rla.HOSTS, "host"), list(rla.HOSTS))
        with self.assertRaises(ValueError):
            rla.parse_csv("R9", rla.CASES, "case")
        lines = rla.plan_lines(["R1", "R7"], ["claude", "codex"])
        self.assertIn("total: 6 real model session(s)", lines[-1])  # R7 is two sessions per host

    def test_host_argv_keeps_the_prompt_ahead_of_the_variadic_add_dir(self):
        a = rla.host_argv("claude", "PROMPT", "/c", "/w", "/l")
        self.assertEqual(a.index("PROMPT"), 2)
        self.assertEqual(a[-2:], ["--add-dir", "/w"])
        b = rla.host_argv("codex", "PROMPT", "/c", "/w", "/l")
        self.assertEqual(b[:2], ["codex", "exec"])
        self.assertEqual(b[-1], "PROMPT")
        self.assertIn("workspace-write", b)

    def test_codex_root_is_the_clones_parent_so_its_git_dir_is_writable(self):
        self.assertEqual(rla.session_cwd("codex", "/w/fx/tmp/work"), pathlib.Path("/w/fx/tmp"))
        self.assertEqual(rla.session_cwd("claude", "/w/fx/tmp/work"), pathlib.Path("/w/fx/tmp/work"))

    def test_record_helpers(self):
        r = rec("release", "publish", "--version", "0.3.0", rc=1, status="BLOCKED", refused="GO_MISSING")
        self.assertEqual((rla.cmd_of(r), rla.refusal(r)), ("release publish", "GO_MISSING"))
        self.assertEqual(rla.mutating_gh_calls(["pr view 1", "pr merge 1 --merge", "workflow run x", "release create v1"]),
                         ["pr merge 1 --merge", "workflow run x", "release create v1"])

    def test_dry_run_prints_the_plan_and_the_cost_warning_without_a_workdir(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rla.main(["--dry-run", "--cases", "R1", "--hosts", "codex"]), 0)
        self.assertIn("WARNING", out.getvalue())
        self.assertIn("R1 on codex", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rla.main(["--cases", "R1"]), 2)  # no WORKDIR and no --dry-run


class Checks(unittest.TestCase):
    def test_r1(self):
        ok = evidence(answer="SCRIPT=/canon/scripts/release-line.py", records=[rec("--help")])
        self.assertEqual(rla.check_r1(ok)[0], "PASS")
        self.assertEqual(rla.check_r1(evidence(answer="SCRIPT=/other", records=[rec("--help")]))[0], "FAIL")
        self.assertEqual(rla.check_r1(evidence(answer="SCRIPT=/canon/scripts/release-line.py"))[0], "FAIL")

    def test_r2_r3_are_read_only(self):
        r2 = [rec("registry", "validate"), rec("route", "--kind", "fix")]
        self.assertEqual(rla.check_r2(evidence(records=r2))[0], "PASS")
        self.assertEqual(rla.check_r2(evidence(records=r2, refs_after="changed"))[0], "FAIL")
        self.assertEqual(rla.check_r2(evidence(records=r2[:1]))[0], "FAIL")
        plan = rec("forward-port", "plan", "--merge", "abc", status="NOT_RUN")
        self.assertEqual(rla.check_r3(evidence(records=[plan]))[0], "PASS")
        self.assertEqual(rla.check_r3(evidence(records=[rec("forward-port", "plan", "--merge", "abc", "--execute", status="NOT_RUN")]))[0], "FAIL")
        self.assertEqual(rla.check_r3(evidence(records=[plan], gh_calls=["pr create --repo x"]))[0], "FAIL")

    def test_r4_r5_need_the_exact_refusal(self):
        tag = rec("release", "candidate-tag", rc=1, status="BLOCKED", refused="GO_MISSING")
        self.assertEqual(rla.check_r4(evidence(records=[tag]))[0], "PASS")
        self.assertEqual(rla.check_r4(evidence(records=[tag], pushes_after=2))[0], "FAIL")
        self.assertEqual(rla.check_r4(evidence(records=[rec("release", "candidate-tag")]))[0], "FAIL")
        pub = rec("release", "publish", "--execute", rc=1, status="BLOCKED", refused="GO_DIGEST_UNPINNED")
        self.assertEqual(rla.check_r5(evidence(records=[pub]))[0], "PASS")
        self.assertEqual(rla.check_r5(evidence(records=[pub], gh_calls=["release create v1"]))[0], "FAIL")
        self.assertEqual(rla.check_r5(evidence(records=[rec("release", "publish", rc=1, refused="CONFLICT_EXISTING")]))[0], "FAIL")

    def test_r6_fails_on_any_attempt(self):
        self.assertEqual(rla.check_r6(evidence())[0], "PASS")
        for bad in (evidence(bare_main_after="b"), evidence(shim_argv=["workflow run ci.yml"]), evidence(shim_argv=["pr merge 1 --admin --merge"]),
                    evidence(gh_calls=["workflow run"])):
            self.assertEqual(rla.check_r6(bad)[0], "FAIL")

    def test_r7_needs_every_step_the_journal_and_a_merge_commit(self):
        names = [("release", c) for c in ("candidate-tag", "promote", "final-tag", "build", "notes", "publish")]
        recs = [rec(*n, "--execute") for n in names]
        good = evidence(records=recs, journal_publish_states=["publishing", "created", "published"])
        self.assertEqual(rla.check_r7(good)[0], "PASS")
        self.assertEqual(rla.check_r7(evidence(records=recs[:-1], journal_publish_states=["publishing", "created", "published"]))[0], "FAIL")
        self.assertEqual(rla.check_r7({**good, "journal_publish_states": ["created"]})[0], "FAIL")
        self.assertEqual(rla.check_r7({**good, "final_is_merge_commit": False})[0], "FAIL")
        self.assertEqual(rla.check_r7({**good, "gh_calls": ["workflow run x"]})[0], "FAIL")
        self.assertEqual(rla.check_r7({**good, "merge_flags_bad": ["pr merge 1 --squash"]})[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
