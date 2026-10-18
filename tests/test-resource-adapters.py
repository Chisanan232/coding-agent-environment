"""Black-box tests for opt-in Glomeris holds around native host launches."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "bin/coding-agent-resource"


class ResourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.log = self.root / "protocol.jsonl"
        self.marker = self.root / "child-finished"
        self.core = self.root / "glomeris"
        self.core.write_text('''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if args[:3] != ["resource", "hold", "resource-1"] or args[3] != "generation-2": sys.exit(90)
with open(os.environ["CORE_ARGS_FILE"], "w") as f: json.dump(args, f)
mode = os.environ.get("CORE_MODE", "ok")
initial_mode = os.environ.get("CORE_INITIAL_MODE", "ok")
attempt_file = os.environ.get("CORE_HOLD_ATTEMPTS_FILE")
attempt = 0
if attempt_file:
 try: attempt = int(open(attempt_file).read())
 except (OSError, ValueError): pass
 with open(attempt_file, "w") as f: f.write(str(attempt + 1))
if initial_mode == "permanent_busy" or (initial_mode == "busy_then_success" and attempt < int(os.environ.get("CORE_BUSY_HOLD_COUNT", "1"))):
 sys.exit(75)
if initial_mode == "lost_ack": sys.exit(0)
if initial_mode == "malformed_ack":
 print("not-json", flush=True)
 sys.exit(0)
child_pgid = None
busy_counts = {}
closed_consumers = True
print(json.dumps({"status":os.environ.get("CORE_INITIAL_STATUS", "refused" if mode == "refuse" else "held"),
                  "id":os.environ.get("CORE_INITIAL_ID", "resource-1"),
                  "generation":os.environ.get("CORE_INITIAL_GENERATION", "generation-2"),
                  "path":os.environ.get("CORE_INITIAL_PATH", "/managed/resource-1/generation-2")}), flush=True)
# Exposure to the native agent permanently invalidates producer closure proof.
closed_consumers = False
for line in sys.stdin:
 request = json.loads(line)
 with open(os.environ["PROTOCOL_LOG"], "a") as f: f.write(json.dumps(request)+"\\n")
 op = request["op"]
 if op == "register_child" and mode == "crash_after_register": os._exit(0)
 elif op == "renew" and mode == "crash_after_renew": os._exit(0)
 elif op == "register_child":
  child_pgid = request.get("pgid")
  time.sleep(float(os.environ.get("CORE_DELAY_REGISTER", "0")))
  reply = {"status":os.environ.get("CORE_STATUS_"+op.upper(), "ok")}
 elif op == "release" and child_pgid and mode == "check_group":
  try: os.killpg(child_pgid, 0); group_active = True
  except ProcessLookupError: group_active = False
  reply = {"status":"refused" if group_active else "ok"}
 elif op == "release" and os.environ.get("PRODUCER_OUTPUT"):
  if closed_consumers:
   os.unlink(os.environ["PRODUCER_OUTPUT"])
   open(os.environ["SEALED_MARKER"], "w").write("sealed")
  reply = {"status":"ok"}
 elif os.environ.get("CORE_REPLY_STATUS_TYPE") == op.upper():
  reply = {"status":["unexpected"]}
 elif op == "renew" and busy_counts.get(op, 0) < int(os.environ.get("CORE_BUSY_RENEW_COUNT", "0")):
  busy_counts[op] = busy_counts.get(op, 0) + 1
  reply = {"status":"busy"}
 else: reply = {"status":os.environ.get("CORE_STATUS_"+op.upper(), "ok")}
 print(json.dumps(reply), flush=True)
''')
        self.core.chmod(0o755)
        self.bootstrap_home()
        self.core_args_log = self.root / "core-args.json"
        self.env = dict(os.environ, HOME=str(self.home), PROTOCOL_LOG=str(self.log),
                        CORE_ARGS_FILE=str(self.core_args_log),
                        CODING_AGENT_MANAGED_RESOURCE_ID="resource-1",
                        CODING_AGENT_MANAGED_RESOURCE_GENERATION="generation-2",
                        CODING_AGENT_RESOURCE_SESSION_ID="session-existing",
                        ENGINEERING_STORAGE_BIN="/must-not-be-used")

    def bootstrap_home(self, host="claude"):
        scripts = self.home / f".{host}/skills/engineering-runtime/scripts"
        scripts.mkdir(parents=True)
        (scripts / "bootstrap.py").write_text(
            "print('export ENGINEERING_STORAGE_BIN=' + repr(" + repr(str(self.core)) + "))\n"
            "print('export ENGINEERING_STORAGE_STATUS=verified_installation')\n")

    def run_adapter(self, *command, env=None, timeout=8):
        return subprocess.run([sys.executable, str(ADAPTER), "--host", "claude", "--", *command],
                              env=env or self.env, text=True, capture_output=True, timeout=timeout)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def core_args(self):
        return json.loads(self.core_args_log.read_text())

    def test_hold_precedes_child_and_release_follows_exit_with_exact_native_argv(self):
        child = self.root / "child.py"
        child.write_text('''import json, os, sys, time
time.sleep(.2)
open(os.environ["MARKER"], "w").write(json.dumps([sys.argv[1:], os.environ.get("GLOMERIS_RESOURCE_DIR")]))
''')
        env = dict(self.env, MARKER=str(self.marker))
        result = self.run_adapter(sys.executable, str(child), "--profile", "chosen", "a b", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(self.marker.read_text())
        self.assertEqual(observed, [["--profile", "chosen", "a b"], "/managed/resource-1/generation-2"])
        ops = [request["op"] for request in self.requests()]
        self.assertEqual(ops[0:2], ["child_pending", "register_child"])
        self.assertNotIn("hold", ops)  # the initial acknowledgment already confirms acquisition
        self.assertEqual(ops[-1], "release")
        self.assertEqual(set(self.requests()[0]), {"op"})
        args = self.core_args()
        self.assertEqual(args[args.index("--session") + 1], "session-existing")

    def test_refusal_or_missing_identity_blocks_child(self):
        child = self.root / "should-not-run"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        refused_env = dict(self.env, CORE_MODE="refuse", MARKER=str(self.marker))
        refused = self.run_adapter(str(child), env=refused_env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_initial_busy_before_acquisition_retries_until_held_then_launches(self):
        child = self.root / "busy-then-held-child"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        attempts = self.root / "hold-attempts"
        env = dict(self.env, CORE_INITIAL_MODE="busy_then_success", CORE_BUSY_HOLD_COUNT="2",
                   CORE_HOLD_ATTEMPTS_FILE=str(attempts), MARKER=str(self.marker))
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['HOLD_RETRY_WINDOW']=1; raise SystemExit(ns['main'](sys.argv[2:]))")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 str(child)], env=env, text=True, capture_output=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertEqual(attempts.read_text(), "3")
        self.assertEqual([r["op"] for r in self.requests()],
                         ["child_pending", "register_child", "release"])

    def prelaunch_core(self, env):
        """Launch the fake core once so a timed retry window does not absorb its first-launch cost.

        The core is a script written moments ago; its first launch pays OS validation (up to ~230-530 ms measured on
        a loaded Mac). With no arguments it exits 90 before any side effect, so the attempts file, protocol log and
        args file are untouched; the exit code is asserted so a later reorder cannot turn this into a counted attempt.
        """
        self.assertEqual(subprocess.run([str(self.core)], env=env, capture_output=True).returncode, 90)

    def test_permanent_initial_busy_is_bounded_and_never_launches(self):
        child = self.root / "permanent-busy-child"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        attempts = self.root / "hold-attempts"
        env = dict(self.env, CORE_INITIAL_MODE="permanent_busy", CORE_HOLD_ATTEMPTS_FILE=str(attempts),
                   MARKER=str(self.marker))
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['HOLD_RETRY_WINDOW']=.38; raise SystemExit(ns['main'](sys.argv[2:]))")
        self.prelaunch_core(env)
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 str(child)], env=env, text=True, capture_output=True, timeout=3)
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requested persisted resource hold", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertGreaterEqual(int(attempts.read_text()), 2)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.requests(), [])

    def test_lost_or_malformed_initial_hold_ack_is_never_retried(self):
        child = self.root / "uncertain-hold-child"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        for mode in ("lost_ack", "malformed_ack"):
            with self.subTest(mode=mode):
                attempts = self.root / "hold-attempts"
                attempts.unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                env = dict(self.env, CORE_INITIAL_MODE=mode, CORE_HOLD_ATTEMPTS_FILE=str(attempts),
                           MARKER=str(self.marker))
                result = self.run_adapter(str(child), env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.marker.exists())
                self.assertEqual(attempts.read_text(), "1")
        missing = dict(self.env)
        missing.pop("CODING_AGENT_RESOURCE_SESSION_ID")
        missing.pop("CODEX_THREAD_ID", None)
        missing.pop("CODEX_SESSION_ID", None)
        result = self.run_adapter(str(child), env=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_unrecognized_scoped_status_blocks_launch_and_is_reported(self):
        child = self.root / "unexpected-status-child"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        env = dict(self.env, CORE_STATUS_CHILD_PENDING="future_status", MARKER=str(self.marker))
        result = self.run_adapter(str(child), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not acknowledge pending child scope", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual([r["op"] for r in self.requests()], ["child_pending"])

    def test_initial_held_ack_must_match_requested_id_generation_and_absolute_path(self):
        child = self.root / "must-not-run-for-mismatched-ack"
        child.write_text('#!/bin/sh\ntouch "$MARKER"\n')
        child.chmod(0o755)
        for key, value in (("CORE_INITIAL_ID", "other-resource"),
                           ("CORE_INITIAL_GENERATION", "other-generation"),
                           ("CORE_INITIAL_PATH", "relative/path")):
            with self.subTest(field=key):
                self.log.unlink(missing_ok=True)
                env = dict(self.env, **{key: value}, MARKER=str(self.marker))
                result = self.run_adapter(str(child), env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requested persisted resource hold", result.stderr)
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.requests(), [])

    def test_malformed_status_schema_and_spawn_failure_keep_pending_claim(self):
        malformed = dict(self.env, CORE_REPLY_STATUS_TYPE="CHILD_PENDING")
        result = self.run_adapter(sys.executable, "-c", "pass", env=malformed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not acknowledge pending child scope", result.stderr)
        self.assertNotIn("release", [r["op"] for r in self.requests()])

        self.log.unlink()
        missing_native = str(self.root / "not-installed-native")
        result = self.run_adapter(missing_native)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("native process could not be started", result.stderr)
        self.assertEqual([r["op"] for r in self.requests()], ["child_pending"])

    def test_unrecognized_registration_status_preserves_claim_without_release(self):
        child = self.root / "registration-child.py"
        child.write_text("import time; time.sleep(.1)\n")
        env = dict(self.env, CORE_STATUS_REGISTER_CHILD="future_status")
        result = self.run_adapter(sys.executable, str(child), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("registration unconfirmed", result.stderr)
        self.assertNotIn("release", [r["op"] for r in self.requests()])

    def test_unrecognized_release_status_preserves_claim_and_reports(self):
        env = dict(self.env, CORE_STATUS_RELEASE="future_status")
        result = self.run_adapter(sys.executable, "-c", "pass", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release unconfirmed", result.stderr)

    def test_registration_carries_process_group_and_release_defers_to_core(self):
        leader = self.root / "leader.py"
        leader.write_text('''import subprocess,sys
subprocess.Popen([sys.executable,"-c","import time; time.sleep(.4)"])
''')
        env = dict(self.env, CORE_MODE="check_group")
        result = self.run_adapter(sys.executable, str(leader), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = self.requests()
        registration = next(r for r in requests if r["op"] == "register_child")
        release = next(r for r in requests if r["op"] == "release")
        self.assertEqual(registration["pgid"], registration["pid"])
        self.assertEqual(release["op"], "release")
        self.assertIn("release unconfirmed", result.stderr)
        time.sleep(.45)

    def test_native_agent_exposure_preserves_outputs_without_auto_sealing(self):
        output = self.root / "generated.o"
        output.write_bytes(b"fixture object")
        sealed = self.root / "sealed"
        env = dict(self.env, PRODUCER_OUTPUT=str(output), SEALED_MARKER=str(sealed))
        result = self.run_adapter(sys.executable, "-c", "pass", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests()[-1]["op"], "release")
        self.assertTrue(output.exists())
        self.assertFalse(sealed.exists())

    def test_short_child_exit_before_registration_ack_preserves_claim(self):
        child = self.root / "short-child.py"
        child.write_text("pass\n")
        env = dict(self.env, CORE_DELAY_REGISTER=".2")
        result = self.run_adapter(sys.executable, str(child), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exited before registration was confirmed", result.stderr)
        self.assertNotIn("release", [r["op"] for r in self.requests()])

    def test_adapter_does_not_release_while_child_is_running(self):
        child = self.root / "long-child.py"
        child.write_text("import time; time.sleep(.5)\n")
        process = subprocess.Popen([sys.executable, str(ADAPTER), "--host", "claude", "--",
                                    sys.executable, str(child)], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(r["op"] == "register_child" for r in self.requests()):
                break
            time.sleep(.02)
        self.assertNotIn("release", [r["op"] for r in self.requests()])
        self.assertEqual(process.wait(timeout=4), 0)
        process.communicate(timeout=1)
        self.assertEqual(self.requests()[-1]["op"], "release")

    def test_renewal_contains_no_synthetic_progress(self):
        child = self.root / "renewing.py"
        child.write_text("import time; time.sleep(.35)\n")
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['RENEW_INTERVAL']=.05; ns['BUSY_RETRY_WINDOW']=.15; "
               "ns['BUSY_RETRY_INITIAL_DELAY']=.01; raise SystemExit(ns['main'](sys.argv[2:]))")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 sys.executable, str(child)], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        renewals = [r for r in self.requests() if r["op"] == "renew"]
        self.assertGreaterEqual(len(renewals), 1)
        self.assertTrue(all(set(request) == {"op"} for request in renewals))

    def test_busy_renewal_retries_same_safe_request_then_succeeds(self):
        child = self.root / "busy-renew.py"
        child.write_text("import time; time.sleep(.4)\n")
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['RENEW_INTERVAL']=.15; ns['BUSY_RETRY_WINDOW']=.3; "
               "ns['BUSY_RETRY_INITIAL_DELAY']=.01; raise SystemExit(ns['main'](sys.argv[2:]))")
        env = dict(self.env, CORE_BUSY_RENEW_COUNT="2")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 sys.executable, str(child)], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        renewals = [r for r in self.requests() if r["op"] == "renew"]
        self.assertGreaterEqual(len(renewals), 3)
        self.assertTrue(all(request == {"op": "renew"} for request in renewals[:3]))
        self.assertEqual(self.requests()[-1]["op"], "release")

    def test_unrecognized_renew_status_preserves_claim_without_release(self):
        child = self.root / "renew-failure.py"
        child.write_text("import time; time.sleep(.12)\n")
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['RENEW_INTERVAL']=.1; raise SystemExit(ns['main'](sys.argv[2:]))")
        env = dict(self.env, CORE_STATUS_RENEW="future_status")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 sys.executable, str(child)], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("renewal unconfirmed", result.stderr)
        self.assertNotIn("release", [r["op"] for r in self.requests()])
        self.assertEqual(len([r for r in self.requests() if r["op"] == "renew"]), 1)

    def test_lost_renew_ack_is_not_retried(self):
        child = self.root / "lost-renew.py"
        child.write_text("import time; time.sleep(.15)\n")
        run = ("import sys; ns={'__name__':'adapter'}; "
               "exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),ns); "
               "ns['RENEW_INTERVAL']=.1; raise SystemExit(ns['main'](sys.argv[2:]))")
        env = dict(self.env, CORE_MODE="crash_after_renew")
        result = subprocess.run([sys.executable, "-c", run, str(ADAPTER), "--host", "claude", "--",
                                 sys.executable, str(child)], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("renewal unconfirmed", result.stderr)
        self.assertEqual(len([r for r in self.requests() if r["op"] == "renew"]), 1)
        self.assertNotIn("release", [r["op"] for r in self.requests()])

    def test_crashed_core_does_not_kill_registered_child_or_invent_progress(self):
        child = self.root / "survives.py"
        child.write_text('''import os, time
time.sleep(.2)
open(os.environ["MARKER"], "w").write("finished")
''')
        env = dict(self.env, CORE_MODE="crash_after_register", MARKER=str(self.marker))
        result = self.run_adapter(sys.executable, str(child), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertNotIn("progress", " ".join(r["op"] for r in self.requests()))

    def test_host_session_fallback_and_native_exit_code(self):
        env = dict(self.env)
        env.pop("CODING_AGENT_RESOURCE_SESSION_ID")
        env["CODEX_THREAD_ID"] = "native-thread"
        result = self.run_adapter("/bin/sh", "-c", "exit 7", env=env)
        self.assertEqual(result.returncode, 7)
        args = self.core_args()
        self.assertEqual(args[args.index("--session") + 1], "native-thread")

    def test_ca_codex_preserves_native_arguments_in_legacy_and_managed_modes(self):
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        native = fakebin / "codex"
        native.write_text('''#!/usr/bin/env python3
import json, os, sys
open(os.environ["NATIVE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
if sys.argv[1:3] == ["debug", "models"]:
 print(json.dumps({"models":[{"slug":"gpt-6-astra","supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]}, {"slug":"gpt-6-luna","supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]}]}))
elif sys.argv[1:2] == ["app-server"]:
 sys.exit(0)
elif "--json" in sys.argv:
 print('{"native":true,"items":[]}')
''')
        native.chmod(0o755)
        self.bootstrap_home("codex")
        env = dict(self.env, PATH=f"{fakebin}:{os.environ['PATH']}",
                   CODEX_HOME=str(self.home / ".codex"), NATIVE_ARGS=str(self.root / "native-args"))
        args = ["--profile", "custom", "-c", 'model="keep"', "argument with spaces"]
        legacy = dict(env)
        legacy.pop("CODING_AGENT_MANAGED_RESOURCE_ID")
        legacy.pop("CODING_AGENT_MANAGED_RESOURCE_GENERATION")
        result = subprocess.run([str(ROOT / "bin/ca-codex"), *args], env=legacy,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "native-args").read_text()), args)
        result = subprocess.run([str(ROOT / "bin/ca-codex"), *args], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "native-args").read_text()), args)

        profile_root = self.root / "profile-project"
        profile_root.mkdir()
        (profile_root / ".coding-agent-profile").write_text("team-profile\n")
        overlay = self.root / "profiles/team-profile/codex"
        overlay.mkdir(parents=True)
        (overlay / "team-profile.config.toml").write_text('model = "profile-model"\n')
        managed = dict(env, CODING_AGENT_LOCAL_PROFILE_DIR=str(self.root / "profiles"))
        result = subprocess.run([str(ROOT / "bin/ca-codex"), *args], env=managed,
                                cwd=profile_root, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "native-args").read_text()),
                         ["--profile", "team-profile", "-c",
                          'project_root_markers=[".coding-agent-profile"]', *args])

    def test_ca_codex_routing_launch_check_keeps_native_json_stdout_clean(self):
        self.bootstrap_home("codex")
        fakebin = self.root / "routing-fakebin"
        fakebin.mkdir()
        native = fakebin / "codex"
        native.write_text('''#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["debug", "models"]:
 print(json.dumps({"models":[{"slug":"gpt-6-astra","supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]}, {"slug":"gpt-6-luna","supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]}]}))
elif sys.argv[1:2] == ["app-server"]:
 sys.exit(0)
else:
 print('{"native":true,"items":[]}')
''')
        native.chmod(0o755)
        codex_home = self.root.resolve() / "routing-home" / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        project = self.root.resolve() / "routing-project"
        project.mkdir()
        (codex_home / "config.toml").write_text('model_provider = "openai"\n')
        env = dict(self.env, PATH=f"{fakebin}:{os.environ['PATH']}", CODEX_HOME=str(codex_home),
                   CODING_AGENT_CODEX_BIN=str(native))
        installed = subprocess.run([sys.executable, str(ROOT / "scripts/codex-routing.py"), "--apply"],
                                   env=env, cwd=project, text=True, capture_output=True, timeout=10)
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        result = subprocess.run([str(ROOT / "bin/ca-codex"), "--json"], env=env,
                                cwd=project, text=True, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"native":true,"items":[]}\n')
        self.assertIn("Routing drift = 0", result.stderr)


if __name__ == "__main__":
    unittest.main()
