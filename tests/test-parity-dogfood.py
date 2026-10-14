"""Isolated tests for the opt-in native parity evidence capture runner."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/parity-dogfood.py"
SPEC = importlib.util.spec_from_file_location("parity_dogfood", SCRIPT)
dogfood = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dogfood)


class ParityDogfoodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / ".local/lib/coding-agent-environment/bin"
        self.bin.mkdir(parents=True)
        self.calls = self.home / "calls.jsonl"
        self.env = dict(os.environ, HOME=str(self.home), PARITY_FAKE_CALLS=str(self.calls))

    def make_wrapper(self, host, body=None):
        path = self.bin / f"ca-{host}"
        path.write_text(body or f'''#!/usr/bin/env python3
import json, os, re, sys, subprocess
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("{host} fake CLI 1.2.3")
    raise SystemExit(0)
with open(os.environ["PARITY_FAKE_CALLS"], "a") as stream:
    git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                              capture_output=True, text=True).stdout.strip()
    config_write = None
    if os.environ.get("PARITY_TEST_GIT_CONFIG_WRITE") == "1":
        config_write = subprocess.run(["git", "config", "--add", "parity.fixture-check", "isolated"],
                                      capture_output=True, text=True).returncode
    stream.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:], "git_root": git_root,
                             "git_env_keys": sorted(key for key in os.environ if key.startswith("GIT_")),
                             "config_write_returncode": config_write}}) + "\\n")
mode = os.environ.get("PARITY_FAKE_MODE", "ok")
host = "{host}"
if mode == "quota":
    print("weekly quota exceeded", file=sys.stderr)
    raise SystemExit(9)
if mode == "missing":
    raise SystemExit(0)
if mode == "corrupt":
    print("not-json")
    raise SystemExit(0)
if host == "claude":
    session = sys.argv[sys.argv.index("--session-id") + 1]
    requested_session = session
    if mode == "quota_structured":
        print(json.dumps({{"type":"result","subtype":"error_max_turns","is_error":True,"session_id":session,"result":"You've hit your weekly limit · resets 2am (Asia/Taipei)"}}))
        raise SystemExit(0)
    project_key = re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd().resolve()))
    history_dir = Path(os.environ["HOME"]) / ".claude" / "projects" / project_key
    history_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    history_path = history_dir / (requested_session + ".jsonl")
    history_path.write_text(json.dumps({{"type":"assistant","message":{{"content":[
        {{"type":"tool_use","name":"Read","input":{{"file_path":"/private/fixture-skill.md","text":"do not copy fixture contents"}}}},
        {{"type":"tool_use","name":"Skill","input":{{"skill":"parity-fixture-skill","opaque_argument":"do not record"}}}}
    ]}}}}) + "\\n")
    os.chmod(history_dir, 0o700)
    os.chmod(history_path, 0o600)
    if mode == "wrong_session":
        session = "unexpected-session-id"
    session_payload = {{}} if mode == "missing_session" else {{"session_id": session}}
    print(json.dumps({{"type":"result","subtype":"success","is_error":False,**session_payload,"modelUsage":{{"claude-sonnet-fixture":{{}}}},"result":"Synthetic handoff from process " + str(os.getpid())}}))
else:
    thread_id = "reused-thread-fixture" if mode == "same_thread" else "thread-" + str(os.getpid())
    print(json.dumps({{"type":"thread.started","thread_id":thread_id,"model":"gpt-fixture"}}))
    print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"Synthetic handoff from process " + str(os.getpid())}}}}))
    print(json.dumps({{"type":"turn.completed","turn_id":"turn-" + str(os.getpid())}}))
''')
        path.chmod(0o700)
        return path

    def run_cli(self, *args, mode="ok"):
        env = dict(self.env, PARITY_FAKE_MODE=mode)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              text=True, capture_output=True, check=False)

    def test_baseline_is_eighteen_canonical_stimuli_without_expected_answers(self):
        baseline = dogfood.load_baseline()
        self.assertEqual([row["id"] for row in baseline["scenarios"]], list(range(1, 19)))
        self.assertEqual(sum(row["mode"] == "local_fixture" for row in baseline["scenarios"]), 3)

        def keys(value):
            if isinstance(value, dict):
                yield from value.keys()
                for child in value.values():
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertFalse({"expected", "expected_answer", "rubric", "pass_criteria"} & set(keys(baseline)))

    def test_default_is_read_only_dry_plan(self):
        self.make_wrapper("claude")
        output = self.home / "private-output"
        result = self.run_cli("--host", "claude", "--case", "18", "--output-dir", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY PLAN", result.stdout)
        self.assertFalse(output.exists())
        self.assertFalse(self.calls.exists())

    def test_unmanaged_local_bin_alias_is_not_a_wrapper_fallback(self):
        legacy = self.home / ".local/bin/ca-claude"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("#!/bin/sh\nexit 0\n")
        legacy.chmod(0o700)
        output = self.home / ".local/state/coding-agent-environment/parity-dogfood"
        result = self.run_cli("--host", "claude", "--case", "1", "--run")
        self.assertEqual(result.returncode, 2)
        self.assertIn(".local/lib/coding-agent-environment/bin/ca-claude", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(self.calls.exists())

    def test_git_routing_environment_cannot_redirect_fixture_or_child_to_external_repo(self):
        sentinel = self.home / "external-sentinel"
        sentinel.mkdir()
        subprocess.run(["git", "init", "--quiet", str(sentinel)], check=True)
        subprocess.run(["git", "-C", str(sentinel), "config", "user.name", "Sentinel"], check=True)
        subprocess.run(["git", "-C", str(sentinel), "config", "user.email", "sentinel@example.invalid"], check=True)
        (sentinel / "keep.txt").write_text("external sentinel stays untouched\\n")
        subprocess.run(["git", "-C", str(sentinel), "add", "keep.txt"], check=True)
        subprocess.run(["git", "-C", str(sentinel), "commit", "--quiet", "-m", "sentinel"], check=True)
        index_path = sentinel / ".git/index"
        index_before = hashlib.sha256(index_path.read_bytes()).hexdigest()
        status_before = subprocess.run(["git", "-C", str(sentinel), "status", "--porcelain"],
                                       check=True, capture_output=True, text=True).stdout
        external_config = self.home / "external.gitconfig"
        external_config_bytes = b"[parity]\\n  fixture-check = untouched\\n"
        external_config.write_bytes(external_config_bytes)

        env = dict(self.env)
        env.update({
            "GIT_DIR": str(sentinel / ".git"),
            "GIT_WORK_TREE": str(sentinel),
            "GIT_INDEX_FILE": str(index_path),
            "GIT_COMMON_DIR": str(sentinel / ".git"),
            "GIT_OBJECT_DIRECTORY": str(sentinel / ".git/objects"),
            "GIT_CONFIG": str(external_config),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "parity.injected",
            "GIT_CONFIG_VALUE_0": "should-be-discarded",
            "GIT_CUSTOM_UNTRUSTED": "should-also-be-discarded",
            "PARITY_TEST_GIT_CONFIG_WRITE": "1",
        })
        self.make_wrapper("claude")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--host", "claude", "--case", "3", "--run",
             "--output-dir", str(self.home / "safe-run")],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(self.calls.read_text().splitlines()[0])
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        fixture_repo = Path(json.loads((run_dir / "cases/03/result.json").read_text())["fixture_repository"])
        self.assertEqual(Path(call["git_root"]), fixture_repo)
        self.assertEqual(call["git_env_keys"], sorted(dogfood.GIT_ENV))
        self.assertEqual(call["config_write_returncode"], 0)
        self.assertEqual(external_config.read_bytes(), external_config_bytes)
        self.assertEqual(index_before, hashlib.sha256(index_path.read_bytes()).hexdigest())
        self.assertEqual(status_before, subprocess.run(
            ["git", "-C", str(sentinel), "status", "--porcelain"], check=True,
            capture_output=True, text=True).stdout)

    def test_fixture_environment_drops_all_git_vars_and_preserves_other_env_opaquely(self):
        injected = {
            "GIT_CONFIG": "/external/config",
            "GIT_CONFIG_GLOBAL": "/external/global-config",
            "GIT_CONFIG_SYSTEM": "/external/system-config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.hostile",
            "GIT_CONFIG_VALUE_0": "!external-command",
            "GIT_CUSTOM_ROUTING": "/external/repository",
            "ANTHROPIC_API_KEY": "opaque-test-capability",
            "OPENAI_API_KEY": "opaque-test-capability",
            "CODEX_HOME": "/preserved/codex-home",
        }
        with mock.patch.dict(os.environ, injected):
            env = dogfood.fixture_process_env()
        self.assertEqual({name for name in env if name.startswith("GIT_")},
                         set(dogfood.GIT_ENV))
        self.assertEqual({name: env[name] for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME")},
                         {name: injected[name] for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME")})
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_native_capture_uses_installed_wrapper_and_never_reports_pass(self):
        self.make_wrapper("claude")
        result = self.run_cli("--host", "claude", "--case", "18", "--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        record = json.loads((run_dir / "cases/18/result.json").read_text())
        summary = json.loads((run_dir / "run.json").read_text())
        call = json.loads(self.calls.read_text().splitlines()[0])
        self.assertEqual(record["status"], "CAPTURED")
        self.assertEqual(summary["acceptance_verdict"], "NOT_EVALUATED")
        request = (run_dir / "cases/18/request.txt").read_text()
        self.assertIn("read applicable installed skill", request)
        self.assertIn("Context7", request)
        self.assertIn("resolve-library-id", request)
        self.assertIn("query-docs", request)
        self.assertEqual(summary["baseline_sha256"], hashlib.sha256(dogfood.BASELINE.read_bytes()).hexdigest())
        self.assertFalse(any(item["status"] == "PASS" for item in summary["scenarios"]))
        self.assertIn("-p", call["argv"])
        self.assertIn("--output-format", call["argv"])
        self.assertIn("--session-id", call["argv"])
        self.assertIn("--permission-mode", call["argv"])
        self.assertIn("--tools=Read,Glob,Grep,Skill", call["argv"])
        self.assertFalse(any(arg in {"--model", "--effort", "--dangerously-skip-permissions"} for arg in call["argv"]))
        requested = call["argv"][call["argv"].index("--session-id") + 1]
        self.assertEqual(record["requested_session_id"], requested)
        self.assertEqual(record["native_session_id"], requested)
        self.assertTrue(record["session_id_matches_request"])
        self.assertEqual(record["resolved_models_observed"], ["claude-sonnet-fixture"])
        tool_evidence = record["native_tool_evidence"]
        self.assertEqual(tool_evidence["status"], "available")
        self.assertRegex(tool_evidence["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(tool_evidence["tool_names"], ["Read", "Skill"])
        self.assertEqual(tool_evidence["skill_identifiers"], ["parity-fixture-skill"])
        self.assertNotIn("file_path", json.dumps(tool_evidence))
        self.assertNotIn("opaque_argument", json.dumps(tool_evidence))
        self.assertNotIn("do not copy fixture contents", json.dumps(tool_evidence))
        self.assertRegex(record["stdout_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(summary["host_cli_version_observed"], "claude fake CLI 1.2.3")
        self.assertEqual(stat.S_IMODE((run_dir / "cases/18/stdout.bin").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_codex_discovery_allows_readonly_exec_without_inventing_lookup_interface(self):
        case = dogfood.load_baseline()["scenarios"][17]
        prompt = dogfood.build_prompt(case, case["prompt"], host="codex")
        self.assertIn("Read-only exec commands are allowed", prompt)
        self.assertIn("already selected read-only sandbox", prompt)
        self.assertIn("Do not write files", prompt)
        self.assertIn("actual interface is discoverable", prompt)
        self.assertNotIn("resolve-library-id", prompt)
        self.assertNotIn("query-docs", prompt)

    def test_codex_jsonl_capture_uses_default_model_and_permission_routing(self):
        self.make_wrapper("codex")
        result = self.run_cli("--host", "codex", "--case", "1", "--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        record = json.loads((run_dir / "cases/01/result.json").read_text())
        argv = json.loads(self.calls.read_text().splitlines()[0])["argv"]
        self.assertEqual(record["status"], "CAPTURED")
        self.assertEqual(argv[:4], ["exec", "--json", "--sandbox", "read-only"])
        self.assertTrue(record["native_thread_id"].startswith("thread-"))
        self.assertEqual(record["resolved_models_observed"], ["gpt-fixture"])
        self.assertRegex(record["stdout_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["host_cli_version_observed"], "codex fake CLI 1.2.3")
        self.assertEqual(argv[:4], ["exec", "--json", "--sandbox", "read-only"])
        self.assertFalse(any(arg in {"-m", "--model", "--ask-for-approval", "--dangerously-bypass-approvals-and-sandbox"} for arg in argv))

    def test_zero_exit_with_corrupt_or_missing_output_is_not_capture_or_pass(self):
        self.make_wrapper("claude")
        for mode in ("corrupt", "missing"):
            with self.subTest(mode=mode):
                result = self.run_cli("--host", "claude", "--case", "1", "--run", "--output-dir",
                                      str(self.home / f"out-{mode}"), mode=mode)
                self.assertEqual(result.returncode, 1)
                run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
                record = json.loads((run_dir / "cases/01/result.json").read_text())
                self.assertNotEqual(record["status"], "CAPTURED")
                self.assertNotEqual(record["status"], "PASS")

    def test_claude_requested_and_observed_session_ids_are_distinct_evidence(self):
        self.make_wrapper("claude")
        missing = self.run_cli("--host", "claude", "--case", "18", "--run", "--output-dir",
                               str(self.home / "missing-session"), mode="missing_session")
        self.assertEqual(missing.returncode, 0, missing.stderr)
        missing_dir = Path(missing.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        missing_record = json.loads((missing_dir / "cases/18/result.json").read_text())
        self.assertEqual(missing_record["status"], "CAPTURED")
        self.assertIn("requested_session_id", missing_record)
        self.assertNotIn("native_session_id", missing_record)

        mismatch = self.run_cli("--host", "claude", "--case", "18", "--run", "--output-dir",
                                str(self.home / "mismatched-session"), mode="wrong_session")
        self.assertEqual(mismatch.returncode, 1)
        mismatch_dir = Path(mismatch.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        mismatch_record = json.loads((mismatch_dir / "cases/18/result.json").read_text())
        self.assertEqual(mismatch_record["status"], "FAILED_OUTPUT")
        self.assertNotEqual(mismatch_record["requested_session_id"], mismatch_record["native_session_id"])
        self.assertFalse(mismatch_record["session_id_matches_request"])

    def test_claude_history_reads_only_verified_session_uuid_and_records_safe_summary(self):
        cwd = self.home / "fixture-workspace"
        cwd.mkdir()
        project_key = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
        history_dir = self.home / ".claude/projects" / project_key
        history_dir.mkdir(parents=True)
        own_id = "11111111-1111-4111-8111-111111111111"
        peer_id = "22222222-2222-4222-8222-222222222222"
        own_history = json.dumps({"type":"assistant","message":{"content":[
            {"type":"tool_use","name":"Read","input":{"file_path":"/secret/path","text":"private content"}},
            {"type":"tool_use","name":"Skill","input":{"skill":"verified-skill","private_arg":"secret"}},
        ]}}).encode() + b"\n"
        (history_dir / f"{own_id}.jsonl").write_bytes(own_history)
        (history_dir / f"{peer_id}.jsonl").write_text(json.dumps({"type":"assistant","message":{"content":[
            {"type":"tool_use","name":"Bash","input":{"command":"private command"}},
        ]}}) + "\n")

        evidence = dogfood.collect_claude_tool_evidence(self.home, cwd, own_id, own_id)
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["sha256"], hashlib.sha256(own_history).hexdigest())
        self.assertEqual(evidence["tool_names"], ["Read", "Skill"])
        self.assertEqual(evidence["skill_identifiers"], ["verified-skill"])
        serialized = json.dumps(evidence)
        for private_value in ("/secret/path", "private content", "private_arg", "secret",
                              "Bash", "private command", str(history_dir)):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(dogfood.collect_claude_tool_evidence(
            self.home, cwd, own_id, peer_id), {"status": "unverified_session_id"})
        self.assertEqual(dogfood.collect_claude_tool_evidence(
            self.home, cwd, own_id, None), {"status": "unverified_session_id"})

    def test_claude_project_key_replaces_dots_and_underscores(self):
        cwd = Path("/parity-test/.private/run_name")
        session_id = "33333333-3333-4333-8333-333333333333"
        history_dir = self.home / ".claude/projects/-parity-test--private-run-name"
        history_dir.mkdir(parents=True)
        history = json.dumps({"type":"assistant","message":{"content":[
            {"type":"tool_use","name":"Skill","input":{"skill":"encoding-fixture"}},
        ]}}).encode() + b"\n"
        (history_dir / f"{session_id}.jsonl").write_bytes(history)

        evidence = dogfood.collect_claude_tool_evidence(self.home, cwd, session_id, session_id)
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["sha256"], hashlib.sha256(history).hexdigest())
        self.assertEqual(evidence["tool_names"], ["Skill"])
        self.assertEqual(evidence["skill_identifiers"], ["encoding-fixture"])

    def test_case17_requires_observed_native_sessions_for_both_fresh_processes(self):
        self.make_wrapper("claude")
        result = self.run_cli("--host", "claude", "--case", "17", "--run", mode="missing_session")
        self.assertEqual(result.returncode, 1)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        record = json.loads((run_dir / "cases/17/result.json").read_text())
        self.assertEqual(record["status"], "FAILED_NATIVE_SESSION_ID_MISSING")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_quota_failure_is_recorded_without_retry_and_stops_all(self):
        self.make_wrapper("claude")
        result = self.run_cli("--host", "claude", "--all", "--run", mode="quota")
        self.assertEqual(result.returncode, 1)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        first = json.loads((run_dir / "cases/01/result.json").read_text())
        second = json.loads((run_dir / "cases/02/result.json").read_text())
        self.assertEqual(first["status"], "FAILED_QUOTA")
        self.assertEqual(second["status"], "NOT_RUN_AFTER_HOST_FAILURE")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_structured_success_envelope_with_weekly_limit_text_is_quota_failure(self):
        self.make_wrapper("claude")
        result = self.run_cli("--host", "claude", "--all", "--run", mode="quota_structured")
        self.assertEqual(result.returncode, 1)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        first = json.loads((run_dir / "cases/01/result.json").read_text())
        second = json.loads((run_dir / "cases/02/result.json").read_text())
        self.assertEqual(first["status"], "FAILED_QUOTA")
        self.assertEqual(first["native_session_id"], first["requested_session_id"])
        self.assertTrue(first["session_id_matches_request"])
        self.assertEqual(second["status"], "NOT_RUN_AFTER_HOST_FAILURE")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_finite_all_case_cap_is_reported_without_spawning_omitted_cases(self):
        self.make_wrapper("codex")
        result = self.run_cli("--host", "codex", "--all", "--max-cases", "2", "--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        summary = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(summary["selected_scenarios"], [1, 2])
        self.assertEqual(summary["not_run_by_budget"], list(range(3, 19)))
        self.assertTrue(all(item["status"] == "CAPTURED" for item in summary["scenarios"][:2]))
        self.assertTrue(all(item["status"] == "NOT_RUN_BY_BUDGET" for item in summary["scenarios"][2:]))
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_case14_withholds_cleanup_authority_and_requires_protection(self):
        case = dogfood.load_baseline()["scenarios"][13]
        self.assertEqual(case["mode"], "plan_only")
        self.assertIn("no authoritative native ownership receipt", case["prompt"])
        self.assertIn("no fresh claim/liveness result", case["prompt"])
        self.assertIn("Do not remove or alter", case["prompt"])

    def test_every_local_fixture_is_seeded_inside_the_private_run_tree(self):
        baseline = dogfood.load_baseline()
        for case in (row for row in baseline["scenarios"] if row["mode"] == "local_fixture"):
            with self.subTest(case=case["id"]):
                run_case = self.home / "fixture-run" / f"case-{case['id']:02d}"
                run_case.mkdir(parents=True)
                repo = dogfood.materialize_fixture(run_case, case, baseline)
                self.assertTrue(repo.is_relative_to(run_case))
                remotes = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True,
                                         text=True, check=True).stdout
                self.assertEqual(remotes, "")
                if case["id"] == 4:
                    self.assertIn("operator-notes.txt", subprocess.run(
                        ["git", "-C", str(repo), "status", "--short"], capture_output=True,
                        text=True, check=True).stdout)
                hooks_path = subprocess.run(
                    ["git", "-C", str(repo), "config", "--local", "core.hooksPath"],
                    capture_output=True, text=True, check=True).stdout.strip()
                identity = subprocess.run(
                    ["git", "-C", str(repo), "config", "--local", "user.email"],
                    capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(hooks_path, "/dev/null")
                self.assertEqual(identity, "fixture@example.invalid")

    def test_scenario17_persists_and_consumes_handoff_across_fresh_processes(self):
        self.make_wrapper("claude")
        result = self.run_cli("--host", "claude", "--case", "17", "--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        case_dir = run_dir / "cases/17"
        record = json.loads((case_dir / "result.json").read_text())
        handoff = (case_dir / "handoff.md").read_text()
        first = json.loads((case_dir / "steps/01/result.json").read_text())
        second = json.loads((case_dir / "steps/02/result.json").read_text())
        request2 = (case_dir / "steps/02/request.txt").read_text()
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(record["status"], "CAPTURED")
        self.assertNotEqual(first["invocation_id"], second["invocation_id"])
        self.assertNotEqual(first["native_session_id"], second["native_session_id"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["pid"], calls[1]["pid"])
        session_ids = [call["argv"][call["argv"].index("--session-id") + 1] for call in calls]
        self.assertEqual(session_ids, [first["native_session_id"], second["native_session_id"]])
        self.assertEqual(session_ids, [call["argv"][call["argv"].index("--session-id") + 1]
                                       for call in calls])
        self.assertIn(handoff.strip(), request2)
        self.assertIn("Synthetic handoff from process", handoff)
        self.assertEqual(stat.S_IMODE((case_dir / "handoff.md").stat().st_mode), 0o600)

    def test_codex_scenario17_rejects_reused_native_thread_id(self):
        self.make_wrapper("codex")
        result = self.run_cli("--host", "codex", "--case", "17", "--run", mode="same_thread")
        self.assertEqual(result.returncode, 1)
        run_dir = Path(result.stdout.split("Private evidence directory: ", 1)[1].splitlines()[0])
        case_dir = run_dir / "cases/17"
        record = json.loads((case_dir / "result.json").read_text())
        first = json.loads((case_dir / "steps/01/result.json").read_text())
        second = json.loads((case_dir / "steps/02/result.json").read_text())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(record["status"], "FAILED_NATIVE_ID_REUSED")
        self.assertEqual(first["status"], "CAPTURED")
        self.assertEqual(second["status"], "CAPTURED")
        self.assertEqual(first["native_thread_id"], second["native_thread_id"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["pid"], calls[1]["pid"])

    def test_interrupt_preserves_real_child_in_its_own_process_group(self):
        wrapper = self.bin / "ca-claude"
        wrapper.write_text('''#!/usr/bin/env python3
import json, sys, time
if sys.argv[1:] == ["--version"]:
    print("claude fake CLI 1.2.3")
    raise SystemExit(0)
time.sleep(0.5)
session = sys.argv[sys.argv.index("--session-id") + 1]
print(json.dumps({"subtype":"success","is_error":False,"session_id":session,"result":"finished"}))
''')
        wrapper.chmod(0o700)
        case = {"id": 3, "name": "interrupt fixture", "mode": "local_fixture"}
        children = []
        real_popen = subprocess.Popen
        real_wait = real_popen.wait

        def capture_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        def interrupt_wait(child, *args, **kwargs):
            if child in children:
                raise KeyboardInterrupt
            return real_wait(child, *args, **kwargs)

        with mock.patch.object(dogfood.subprocess, "Popen", side_effect=capture_child):
            with mock.patch.object(real_popen, "wait", interrupt_wait):
                record, stop_run, _ = dogfood.execute_process(
                    "claude", wrapper, self.home, self.home / "interrupt-evidence", case,
                    "synthetic interrupt test", "session-interrupt-test")

        child = children[0]
        self.assertTrue(stop_run)
        self.assertEqual(record["status"], "RUNNING_HANDLE_PRESERVED")
        self.assertEqual(record["child_state_after_interrupt"], "alive")
        self.assertEqual(record["process_group_id"], child.pid)
        self.assertEqual(os.getpgid(child.pid), child.pid)
        self.assertEqual(real_wait(child, timeout=3), 0)

    def test_interrupt_records_exited_child_and_unknown_poll_without_signalling(self):
        wrapper = self.bin / "ca-claude"
        wrapper.write_text('''#!/usr/bin/env python3
import json, sys, time
if sys.argv[1:] == ["--version"]:
    print("claude fake CLI 1.2.3")
    raise SystemExit(0)
time.sleep(0.2)
session = sys.argv[sys.argv.index("--session-id") + 1]
print(json.dumps({"subtype":"success","is_error":False,"session_id":session,"result":"finished"}))
''')
        wrapper.chmod(0o700)
        case = {"id": 3, "name": "interrupt fixture", "mode": "local_fixture"}
        real_popen = subprocess.Popen
        real_wait = real_popen.wait

        def run_interrupted(label, wait_behavior, poll_behavior=None):
            children = []

            def capture_child(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                if poll_behavior:
                    child.poll = lambda: poll_behavior()
                return child

            def interrupt_wait(child, *args, **kwargs):
                if child in children:
                    if wait_behavior == "exit_first":
                        real_wait(child, timeout=3)
                    raise KeyboardInterrupt
                return real_wait(child, *args, **kwargs)

            with mock.patch.object(dogfood.subprocess, "Popen", side_effect=capture_child):
                with mock.patch.object(real_popen, "wait", interrupt_wait):
                    record, _, _ = dogfood.execute_process(
                        "claude", wrapper, self.home, self.home / f"interrupt-{label}", case,
                        "synthetic interrupt test", f"session-interrupt-{label}")
            real_wait(children[0], timeout=3)
            return record

        exited = run_interrupted("exited", "exit_first")
        self.assertEqual(exited["child_state_after_interrupt"], "exited")
        self.assertEqual(exited["status"], "INTERRUPTED_CHILD_EXITED")
        self.assertEqual(exited["exit_code"], 0)

        def unavailable_poll():
            raise OSError("poll fixture unavailable")

        unknown = run_interrupted("unknown", "interrupt", unavailable_poll)
        self.assertEqual(unknown["child_state_after_interrupt"], "unknown")
        self.assertEqual(unknown["status"], "CHILD_STATE_UNKNOWN")
        self.assertEqual(unknown["interrupt_observation_error"], "OSError")

    def test_codex_completed_turn_without_agent_message_is_refused(self):
        valid, detail, metadata = dogfood.parse_host_output("codex", "\n".join((
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"turn.completed","turn_id":"turn-1"}',
        )))
        self.assertFalse(valid)
        self.assertIn("agent_message", detail)
        self.assertEqual(metadata, {})

    def test_language_repair_seed_has_a_real_typescript_behavior_failure_and_no_lint_claim(self):
        seed = dogfood.load_baseline()["local_fixtures"]["05-language-repair"]
        self.assertIn("sum + value, 1", seed["files"]["typescript/total.ts"])
        self.assertIn("total([1, 2, 3]) !== 6", seed["files"]["typescript/total.test.ts"])
        task = seed["files"]["TASK.md"]
        self.assertIn("no linter", task)
        self.assertIn("go vet ./...", task)

    def test_failure_classifier_names_host_gate_categories(self):
        cases = (
            ("weekly quota exceeded", "", 1, "FAILED_QUOTA"),
            ("You've hit your weekly limit · resets 2am", "", 0, "FAILED_QUOTA"),
            ("", "authentication required", 1, "FAILED_AUTH"),
            ("", "permission denied", 1, "FAILED_PERMISSION"),
            ('{"type":"turn.failed","error":"runner stopped"}', "", 1, "FAILED_JOB"),
        )
        for stdout, stderr, exit_code, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(dogfood.classify_failure(stdout, stderr, exit_code), expected)

    def test_output_path_traversal_is_refused(self):
        with self.assertRaises(dogfood.DogfoodError):
            dogfood.safe_output_root(self.home, str(self.home / ".." / "outside"))

    def test_requires_explicit_single_scenario_selection(self):
        result = self.run_cli("--host", "claude")
        self.assertEqual(result.returncode, 2)
        self.assertIn("required", result.stderr)


if __name__ == "__main__":
    unittest.main()
