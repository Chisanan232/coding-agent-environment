#!/usr/bin/env python3
"""qa-handoff (SPE-131): offline tests with a fake `codex`. No real model, ticket system or network is used.

The fake records its argv, environment names and prompt, writes the verdict named by -o and prints a JSON event stream,
so the tests prove what the wrapper guarantees regardless of what a model says.
"""
import contextlib
import importlib.util
import sys
import io
import json
import os
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/qa-handoff.py"
spec = importlib.util.spec_from_file_location("qa_handoff", SCRIPT)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)

FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
d = "@FAKEDIR@"
argv = sys.argv[1:]
json.dump({"argv": argv, "env": sorted(os.environ), "cwd": os.getcwd()}, open(d + "/seen.json", "w"))
open(d + "/prompt.txt", "w").write(sys.stdin.read())
mode = open(d + "/mode").read().strip()
if mode == "hang":
    child = subprocess.Popen(["sleep", "120"])
    open(d + "/child.pid", "w").write(str(child.pid))
    open(d + "/self.pid", "w").write(str(os.getpid()))
    time.sleep(120)
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
verdict = json.loads(open(d + "/verdict").read().replace("@SHA@", sha))
if mode == "dirty":
    open("tracked.txt", "a").write("modified by QA")
out = argv[argv.index("-o") + 1]
open(out, "w").write(json.dumps(verdict))
if mode != "nocommands":
    print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "x"}}))
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "x", "exit_code": 0}}))
print(json.dumps({"type": "turn.completed"}))
'''


def verdict(**over):
    base = {"status": "COMPLETE", "baseline": "prod @ @SHA@",
            "verified": [{"check": "criterion 1", "result": "PASS", "evidence": "ran x"}],
            "suspected_findings": [], "untested_or_blocked": [], "confidence": "HIGH"}
    base.update(over)
    return json.dumps(base)


def finding(sev):
    return {"id": "F-1", "severity": sev, "surface": "a.py", "expected": "e", "actual": "a", "evidence": "ev",
            "confidence": "HIGH"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.fake_dir = self.root / "fake"
        self.fake_dir.mkdir()
        self.fake = self.root / "codex"
        self.fake.write_text(FAKE.replace("@FAKEDIR@", str(self.fake_dir)))
        self.fake.chmod(0o755)
        self.repo = self.root / "repo"
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True, env=env)
        (self.repo / "tracked.txt").write_text("v1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"],
                       check=True, env=env)
        self.sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        self.criteria = self.root / "criteria.txt"
        self.criteria.write_text("AC1: tracked.txt exists")
        self.fake_mode("pass")
        self.fake_verdict(verdict())

    def fake_mode(self, mode):
        (self.fake_dir / "mode").write_text(mode)

    def fake_verdict(self, text):
        (self.fake_dir / "verdict").write_text(text)

    def set_env(self, **values):
        for k, v in values.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None else os.environ.update({k: old}))

    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = qa.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def prepare(self, **extra):
        code, out = self.call("prepare", "--repo", str(self.repo), "--sha", "HEAD", "--ticket", "T-1", "--product", "prod",
                              "--criteria", str(self.criteria), "--root", str(self.root / "qa"))
        self.assertEqual(code, 0)
        return Path(out.strip().splitlines()[-1])

    ADMIT = [sys.executable, "-c", "raise SystemExit(0)"]
    REFUSE = [sys.executable, "-c", "raise SystemExit(77)"]

    def run_qa(self, run_dir, admission=None, extra=()):
        return self.call("run", "--run-dir", str(run_dir), "--codex-bin", str(self.fake), "--admission-cmd",
                         shlex.join(admission or self.ADMIT), *extra)

    def seen(self):
        return json.loads((self.fake_dir / "seen.json").read_text())


class PrepareTests(Base):
    def test_prepare_pins_a_private_clone_at_the_exact_sha(self):
        run_dir = self.prepare()
        packet = qa.load_packet(run_dir)
        self.assertEqual(packet["sha"], self.sha)
        head = subprocess.run(["git", "-C", str(run_dir / "source"), "rev-parse", "HEAD"], capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), self.sha)
        self.assertEqual(oct(run_dir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((run_dir / "packet.json").stat().st_mode & 0o777), "0o600")
        self.assertTrue((run_dir / "source/.git").is_dir())          # private git metadata, not a worktree link

    def test_a_second_prepare_for_the_same_sha_gets_its_own_attempt_directory(self):
        self.assertNotEqual(self.prepare(), self.prepare())

    def test_edited_packet_is_refused(self):
        run_dir = self.prepare()
        packet = json.loads((run_dir / "packet.json").read_text())
        packet["criteria"] = "weakened"
        (run_dir / "packet.json").write_text(json.dumps(packet))
        code, _ = self.run_qa(run_dir)
        self.assertNotEqual(code, 0)


class RunTests(Base):
    def test_explicit_sandbox_flags_on_every_call_and_no_model_override(self):
        run_dir = self.prepare()
        self.run_qa(run_dir)
        argv = self.seen()["argv"]
        self.assertEqual(argv[:2], ["exec", "--skip-git-repo-check"])
        for flag in ("--ephemeral", "--json", "--output-schema", "-o"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")     # never inherits the full-access config
        self.assertEqual(argv[argv.index("-C") + 1], str(run_dir / "source"))
        for forbidden in ("-m", "--model", "--dangerously-bypass-approvals-and-sandbox", "danger-full-access"):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(argv[-1], "-")

    def test_environment_is_an_allowlist_and_isolated(self):
        for secret in ("GH_TOKEN", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"):
            self.set_env(**{secret: "do-not-leak"})
        run_dir = self.prepare()
        self.run_qa(run_dir)
        seen = self.seen()
        for secret in ("GH_TOKEN", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(secret, seen["env"])
        for name in ("HOME", "TMPDIR", "CARGO_TARGET_DIR", "CARGO_NET_OFFLINE", "CODEX_HOME"):
            self.assertIn(name, seen["env"])
        self.assertEqual(seen["cwd"], str(run_dir / "source"))

    def test_prompt_labels_producer_evidence_as_unverified_and_never_shows_a_transcript(self):
        ev = self.root / "ev.txt"
        ev.write_text("all green on my machine")
        code, out = self.call("prepare", "--repo", str(self.repo), "--sha", "HEAD", "--ticket", "T-2", "--product", "prod",
                              "--criteria", str(self.criteria), "--producer-evidence", str(ev), "--root", str(self.root / "qa"))
        run_dir = Path(out.strip().splitlines()[-1])
        self.run_qa(run_dir)
        prompt = (self.fake_dir / "prompt.txt").read_text()
        self.assertIn("PRODUCER-CLAIMED EVIDENCE (unverified", prompt)
        self.assertIn("all green on my machine", prompt)
        self.assertIn(self.sha, prompt)

    def test_admission_refusal_never_launches_codex(self):
        run_dir = self.prepare()
        code, out = self.run_qa(run_dir, admission=self.REFUSE)
        self.assertEqual(code, 75)
        self.assertIn("WAITING_RESOURCE", out)
        self.assertFalse((self.fake_dir / "seen.json").exists())

    def test_modified_clone_before_the_run_is_invalid_and_codex_is_not_started(self):
        run_dir = self.prepare()
        (run_dir / "source/tracked.txt").write_text("edited")
        code, _ = self.run_qa(run_dir)
        self.assertEqual(code, 12)
        self.assertFalse((self.fake_dir / "seen.json").exists())

    def test_timeout_kills_the_child_process_group_and_is_incomplete(self):
        self.fake_mode("hang")
        run_dir = self.prepare()
        code, out = self.run_qa(run_dir, extra=("--timeout", "2"))
        self.assertEqual(code, 11)
        self.assertIn("timed out", out)
        time.sleep(0.5)
        for name in ("self.pid", "child.pid"):
            pid = int((self.fake_dir / name).read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_no_interactive_target_option_exists(self):
        text = SCRIPT.read_text()
        for needle in ("tmux", "send-keys", "--pane", "--session"):
            self.assertNotIn(needle, text.replace("tmux pane or an", ""))


class VerdictTests(Base):
    def outcome(self, v, mode="pass"):
        self.fake_verdict(v)
        self.fake_mode(mode)
        run_dir = self.prepare()
        code, out = self.run_qa(run_dir)
        return code, out, run_dir

    def test_clean_verdict_with_executed_commands_is_a_pass(self):
        code, out, run_dir = self.outcome(verdict())
        self.assertEqual(code, 0)
        result = json.loads((run_dir / "RESULT.json").read_text())
        self.assertEqual(result["state"], "QA_PASS")
        self.assertIn("never merge evidence", result["note"])
        self.assertEqual(oct((run_dir / "RESULT.json").stat().st_mode & 0o777), "0o600")

    def test_failed_check_or_medium_or_higher_finding_is_a_fail(self):
        fail = verdict(verified=[{"check": "AC1", "result": "FAIL", "evidence": "x"}])
        self.assertEqual(self.outcome(fail)[0], 10)
        for sev in ("Critical", "High", "Medium"):
            self.assertEqual(self.outcome(verdict(suspected_findings=[finding(sev)]))[0], 10, sev)

    def test_low_finding_alone_is_a_pass(self):
        self.assertEqual(self.outcome(verdict(suspected_findings=[finding("Low")]))[0], 0)

    def test_partial_blocked_or_untested_is_incomplete_not_pass(self):
        self.assertEqual(self.outcome(verdict(status="PARTIAL"))[0], 11)
        self.assertEqual(self.outcome(verdict(untested_or_blocked=[{"surface": "AC2", "reason": "lock busy"}]))[0], 11)
        self.assertEqual(self.outcome(verdict(verified=[]))[0], 11)

    def test_a_clean_looking_verdict_with_no_executed_command_is_incomplete(self):
        code, out, _ = self.outcome(verdict(), mode="nocommands")
        self.assertEqual(code, 11)
        self.assertIn("no command was executed", out)

    def test_baseline_for_another_sha_is_invalid(self):
        self.assertEqual(self.outcome(verdict(baseline="prod @ " + "0" * 40))[0], 12)

    def test_malformed_or_extra_key_verdict_is_invalid(self):
        self.assertEqual(self.outcome('{"status": "COMPLETE"}')[0], 12)
        bad = json.loads(verdict())
        bad["extra"] = 1
        self.assertEqual(self.outcome(json.dumps(bad))[0], 12)

    def test_qa_that_modifies_a_tracked_file_is_invalid(self):
        self.assertEqual(self.outcome(verdict(), mode="dirty")[0], 12)

    def test_verdict_edited_after_the_run_is_detected(self):
        code, _, run_dir = self.outcome(verdict(verified=[{"check": "AC1", "result": "FAIL", "evidence": "x"}]))
        self.assertEqual(code, 10)
        (run_dir / "out/verdict.json").write_text(verdict())                 # producer "fixes" the verdict
        code, out = self.call("verdict", "--run-dir", str(run_dir))
        self.assertEqual(code, 12)
        self.assertIn("tampered", out)

    def test_event_stream_edited_after_the_run_is_detected(self):
        _, _, run_dir = self.outcome(verdict(), mode="nocommands")
        with open(run_dir / "events.jsonl", "a") as handle:
            handle.write(json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}) + "\n")
        self.assertEqual(self.call("verdict", "--run-dir", str(run_dir))[0], 12)

    def test_verdict_command_is_idempotent(self):
        _, _, run_dir = self.outcome(verdict())
        self.assertEqual(self.call("verdict", "--run-dir", str(run_dir))[0], 0)
        self.assertEqual(self.call("verdict", "--run-dir", str(run_dir))[0], 0)


if __name__ == "__main__":
    unittest.main()
