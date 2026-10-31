#!/usr/bin/env python3
"""Offline tests of the endpoint lifecycle (SPE-145 PR-b3). Every system touch is a double; what is pinned is ORDER (no candidate code before
both generated profiles passed every control), the port proof, supervision and teardown. Real sandbox behaviour is tests/test-qa-endpoint-live.py
and the owner demo."""
import importlib.util
import os
import signal
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_lifecycle_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

PORT = 23456


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.returncode

    def exit(self, code=0):
        self.returncode = code


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Fakes:
    """Scenario knobs plus a log of everything that happened, in order."""

    def __init__(self, endpoint):
        self.e = endpoint
        self.log = []
        self.clock = Clock()
        self.port_busy = False
        self.leak = set()                 # control names that come back wrong
        self.unrunnable = set()
        self.listeners_result = ["127.0.0.1:%d" % PORT]
        self.connect_after_start = 0      # errno when the daemon is up
        self.stubborn = set()             # pids that ignore SIGTERM
        self.daemon_dies_at_start = False
        self.started = False
        self.procs = {}
        self.next_pid = 5000
        self.expect = {}
        self.signals = []
        self.leave_sockets = True

    # -- ops --
    def run(self, argv, env, timeout):
        code = argv[-1]
        name = [l for l in code.splitlines() if l.startswith("#")]
        self.log.append(("probe", argv[2].rsplit("/", 1)[-1]))
        if self.unrunnable:
            raise OSError("cannot run")
        return 0, self.answer.pop(0) + "\n"

    def popen(self, argv, **kw):
        self.next_pid += 1
        proc = FakeProc(self.next_pid)
        kind = "daemon" if argv[3] == self.e.daemon_binary else "relay"
        self.log.append(("spawn", kind, tuple(argv), kw.get("env"), kw.get("cwd")))
        self.procs[kind] = proc
        if kind == "daemon":
            if self.daemon_dies_at_start:
                proc.exit(1)
            else:
                self.e.daemon_sock.parent.mkdir(parents=True, exist_ok=True)
                self.e.daemon_sock.write_text("")
        else:
            self.e.relay_sock.write_text("")
        return proc

    def connect(self, port):
        self.log.append(("connect",))
        if "daemon" not in self.procs:
            return 0 if self.port_busy else side.ECONNREFUSED
        proc = self.procs["daemon"]
        if self.torn_down(proc):
            return side.ECONNREFUSED
        return self.connect_after_start

    def torn_down(self, proc):
        return proc.returncode is not None and ("daemon", proc.pid) in self.signals_seen()

    def signals_seen(self):
        return {(k, p) for k, p, s in [(self.kind_of(pid), pid, sig) for pid, sig in self.signals]}

    def kind_of(self, pid):
        return next((k for k, p in self.procs.items() if p.pid == pid), "?")

    def listeners(self, pid):
        self.log.append(("listeners", pid))
        return self.listeners_result

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))
        self.log.append(("signal", self.kind_of(pid), sig))
        proc = next((p for p in self.procs.values() if p.pid == pid), None)
        if proc is not None and proc.returncode is None and not (sig == signal.SIGTERM and pid in self.stubborn):
            proc.exit(-int(sig))
            if self.kind_of(pid) == "daemon" and not self.leave_sockets and self.e.daemon_sock.exists():
                self.e.daemon_sock.unlink()

    def rmtree(self, path):
        self.log.append(("rmtree", str(path)))
        import shutil
        shutil.rmtree(path)

    def canary_listener(self):
        self.log.append(("canary_listener",))
        return 24999, lambda: self.log.append(("close_canary",))

    def decoy_socket(self, path):
        self.log.append(("decoy", os.path.basename(path)))
        return lambda: self.log.append(("close_decoy",))

    def ops(self):
        return side.EndpointOps(popen=self.popen, run=self.run, sleep=self.clock.sleep, monotonic=self.clock.now, connect=self.connect,
                                listeners=self.listeners, killpg=self.killpg, rmtree=self.rmtree, canary_listener=self.canary_listener,
                                decoy_socket=self.decoy_socket)


class LifecycleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.home, self.ex = root / "home", root / "ex"
        self.pay = self.ex / "outbox" / "att-1" / "payload"
        (self.pay / "bin").mkdir(parents=True)
        self.home.mkdir(mode=0o700)
        (self.pay / "bin" / "daemon").write_text("x")
        self.endpoint = side.Endpoint(str(self.home), str(self.ex), "att-1", PORT, "fornax-daemon/1", str(self.pay), str(self.pay / "bin" / "daemon"))
        self.record_dir = self.ex / "outbox" / "att-1" / "endpoint"
        self.fakes = Fakes(self.endpoint)
        self.table = side.endpoint_probe_table(self.endpoint, "/c", "/a", 24999, "/d/qa-decoy.sock", None)
        self.fakes.answer = None
        self.life = side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops())

    def answers(self, leak=()):
        out = []
        for name, profile, code, expect in self.table:
            if name in leak:
                out.append("ALLOWED" if expect not in ("ALLOWED", "REFUSED") else "DENIED:1")
            else:
                out.append({"ALLOWED": "ALLOWED", "REFUSED": "REFUSED", "DENIED": "DENIED:13", "DENIED_EPERM": "DENIED:1"}[expect])
        return out

    def prove(self, leak=()):
        self.fakes.answer = self.answers(leak)
        self.life.prove_profiles("/c", "/a", "/d")

    def spawned(self):
        return [e for e in self.fakes.log if e[0] == "spawn"]


class CreateTests(LifecycleBase):
    def test_creates_private_directories_and_writes_the_profiles_read_only(self):
        self.life.create()
        e = self.endpoint
        for d in (e.dir, e.state, e.tmp, e.rl, e.dir.parent):
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700, d)
        for name in ("daemon.sb", "relay.sb"):
            self.assertEqual(stat.S_IMODE(os.lstat(self.record_dir / name).st_mode), 0o400)
        self.assertEqual((self.record_dir / "daemon.sb").read_text(), side.daemon_sb(e))
        self.assertEqual((self.record_dir / "relay.sb").read_text(), side.relay_sb(e))
        self.assertEqual(self.life.record["daemon_profile_sha256"], side.endpoint_digests(e)["daemon_profile_sha256"])

    def test_an_existing_endpoint_directory_is_a_failure(self):
        self.life.create()
        with self.assertRaises(side.EndpointFailed):
            side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops()).create()

    def test_a_loose_or_linked_parent_is_refused(self):
        parent = self.endpoint.dir.parent
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o770)
        with self.assertRaises(side.EndpointFailed):
            self.life.create()
        os.chmod(parent, 0o700)
        parent.rmdir()
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.symlink(elsewhere, parent)
        with self.assertRaises(side.EndpointFailed):
            side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops()).create()


class ProofBeforeCodeTests(LifecycleBase):
    def setUp(self):
        super().setUp()
        self.life.create()

    def test_every_control_runs_under_the_right_profile_before_anything_starts(self):
        self.prove()
        probes = [e for e in self.fakes.log if e[0] == "probe"]
        self.assertEqual(len(probes), len(self.table))
        self.assertEqual({p[1] for p in probes}, {"daemon.sb", "relay.sb"})
        self.assertEqual(self.spawned(), [])
        self.assertTrue(all(c["ok"] for c in self.life.record["controls"]))
        self.assertGreater(len(self.life.record["controls"]), 35)

    def test_every_snippet_is_plain_python_with_no_object_reprs(self):
        for name, profile, code, expect in self.table:
            self.assertNotIn("PosixPath", code, name)
            compile(code, name, "exec")
            self.assertIn(profile, ("daemon.sb", "relay.sb"))
            self.assertIn(expect, ("ALLOWED", "REFUSED", "DENIED", "DENIED_EPERM"))

    def test_each_control_class_is_present_for_both_processes(self):
        names = {n for n, *_ in self.table}
        for who in ("d", "r"):
            for control in ("testnet4", "testnet6", "udp", "resolver", "canary_listener", "decoy_socket", "bind_other_port", "read_canary",
                            "read_login", "list_home", "list_exchange", "write_home"):
                self.assertIn("neg_%s_%s" % (who, control), names)
        for control in ("pos_d_write_state", "pos_d_write_tmp", "pos_d_read_payload", "pos_d_sqlite_state", "pos_d_bind_port", "neg_d_write_rl",
                        "neg_r_write_state", "neg_r_read_payload", "pos_r_write_rl", "pos_r_connect_daemon_port"):
            self.assertIn(control, names)

    def test_any_wrong_answer_fails_the_endpoint_and_nothing_is_spawned(self):
        for leaked in ("neg_d_read_login", "neg_r_testnet4", "neg_d_canary_listener", "pos_d_bind_port"):
            self.fakes.log.clear()
            life = side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops())
            self.fakes.answer = self.answers(leak=(leaked,))
            with self.assertRaises(side.EndpointFailed) as caught:
                life.prove_profiles("/c", "/a", "/d")
            self.assertIn(leaked, str(caught.exception))
            self.assertEqual(self.spawned(), [], leaked)

    def test_socket_controls_accept_only_eperm(self):
        index = next(i for i, row in enumerate(self.table) if row[0] == "neg_d_testnet4")
        for token in ("DENIED:13", "DENIED:61", "DENIED:65", "REFUSED", "NO_OUTPUT", "UNRUNNABLE"):
            answers = self.answers()
            answers[index] = token
            self.fakes.answer = answers
            with self.assertRaises(side.EndpointFailed, msg=token):
                side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops()).prove_profiles("/c", "/a", "/d")

    def test_the_relay_connect_control_wants_refused_not_denied(self):
        index = next(i for i, row in enumerate(self.table) if row[0] == "pos_r_connect_daemon_port")
        answers = self.answers()
        answers[index] = "DENIED:1"                     # the profile blocked the one thing the relay needs
        self.fakes.answer = answers
        with self.assertRaises(side.EndpointFailed):
            self.life.prove_profiles("/c", "/a", "/d")

    def test_a_port_already_in_use_stops_before_any_probe(self):
        self.fakes.port_busy = True
        with self.assertRaises(side.EndpointFailed) as caught:
            self.life.prove_profiles("/c", "/a", "/d")
        self.assertIn("in use", str(caught.exception))
        self.assertEqual([e for e in self.fakes.log if e[0] == "probe"], [])

    def test_a_probe_that_cannot_run_fails_closed(self):
        self.fakes.unrunnable = {"all"}
        with self.assertRaises(side.EndpointFailed):
            self.life.prove_profiles("/c", "/a", "/d")
        self.assertEqual(self.spawned(), [])


class StartTests(LifecycleBase):
    def setUp(self):
        super().setUp()
        self.life.create()
        self.prove()

    def test_start_order_environment_and_cwd(self):
        self.life.start()
        spawned = self.spawned()
        self.assertEqual([s[1] for s in spawned], ["daemon", "relay"])
        daemon, relay = spawned
        self.assertEqual(daemon[2], ("/usr/bin/sandbox-exec", "-f", str(self.record_dir / "daemon.sb"), self.endpoint.daemon_binary))
        self.assertEqual(daemon[3], self.endpoint.daemon_env())                   # nothing inherited
        self.assertEqual((daemon[4], relay[4]), ("/", "/"))
        self.assertEqual(relay[2][:4], ("/usr/bin/sandbox-exec", "-f", str(self.record_dir / "relay.sb"), "/usr/bin/python3"))
        self.assertEqual(relay[2][4:6], ("-I", "-c"))
        self.assertEqual(relay[2][6:], (side.RELAY_SRC, str(self.endpoint.relay_sock), str(PORT)))
        self.assertEqual(relay[3], {"PATH": "/usr/bin:/bin"})
        kinds = [e[0] for e in self.fakes.log]
        self.assertLess(kinds.index("listeners"), len(kinds) - 1 - kinds[::-1].index("spawn"))      # the port is proved before the relay starts
        self.assertEqual(self.life.record["port_proof"]["listeners"], ["127.0.0.1:%d" % PORT])
        self.assertTrue(self.life.record["daemon_started"] and self.life.record["relay_started"])

    def test_wildcard_ipv6_extra_or_missing_listeners_fail_the_port_proof(self):
        for listeners in (["*:%d" % PORT], ["127.0.0.1:%d" % PORT, "[::1]:%d" % PORT], ["127.0.0.1:%d" % PORT, "127.0.0.1:%d" % (PORT + 1)],
                          ["127.0.0.1:%d" % (PORT + 1)], [], None, ["127.0.0.1:%d" % PORT] * 2):
            fakes = Fakes(self.endpoint)
            fakes.listeners_result = listeners
            life = side.EndpointLifecycle(self.endpoint, self.record_dir, fakes.ops())
            if self.endpoint.daemon_sock.exists():
                self.endpoint.daemon_sock.unlink()
            with self.assertRaises(side.EndpointFailed, msg=str(listeners)):
                life.start()
            self.assertEqual([s[1] for s in fakes.log if s[0] == "spawn"], ["daemon"], listeners)     # the relay never starts

    def test_a_port_that_does_not_accept_connections_fails(self):
        self.fakes.connect_after_start = 61
        with self.assertRaises(side.EndpointFailed):
            self.life.start()

    def test_a_daemon_that_dies_at_start_fails_and_the_relay_never_starts(self):
        self.fakes.daemon_dies_at_start = True
        with self.assertRaises(side.EndpointFailed) as caught:
            self.life.start()
        self.assertIn("daemon", str(caught.exception))
        self.assertEqual([s[1] for s in self.spawned()], ["daemon"])

    def test_supervision_kills_the_relay_the_moment_the_daemon_exits(self):
        self.life.start()
        self.assertTrue(self.life.supervise())
        self.fakes.procs["daemon"].exit(7)
        self.assertFalse(self.life.supervise())
        relay = self.fakes.procs["relay"]
        self.assertIn((relay.pid, signal.SIGTERM), self.fakes.signals)
        self.assertEqual(self.life.record["daemon_exited_early"], 7)


class TeardownTests(LifecycleBase):
    def setUp(self):
        super().setUp()
        self.life.create()
        self.prove()
        self.life.start()

    def test_relay_stops_before_daemon_then_everything_is_verified_and_removed(self):
        self.fakes.leave_sockets = False
        steps = self.life.teardown()
        order = [(e[1], e[2]) for e in self.fakes.log if e[0] == "signal"]
        self.assertLess(order.index(("relay", signal.SIGTERM)), order.index(("daemon", signal.SIGTERM)))
        self.assertTrue(steps["clean"], steps)
        self.assertFalse(self.endpoint.dir.exists())
        self.assertIn("rmtree", [e[0] for e in self.fakes.log])

    def test_a_socket_the_daemon_left_behind_is_unlinked_and_reported(self):
        self.fakes.leave_sockets = True
        steps = self.life.teardown()
        self.assertEqual(sorted(steps["socket_removed_by_helper"]), ["fornax.sock", "http.sock"])
        self.assertTrue(steps["sockets_gone"])

    def test_a_process_that_ignores_sigterm_is_killed(self):
        self.fakes.stubborn = {self.fakes.procs["daemon"].pid, self.fakes.procs["relay"].pid}
        steps = self.life.teardown()
        self.assertTrue(steps["daemon_stopped"] and steps["relay_stopped"], steps)
        self.assertIn((self.fakes.procs["daemon"].pid, signal.SIGKILL), self.fakes.signals)

    def test_an_open_port_after_teardown_is_not_clean(self):
        self.life.ops.connect = lambda port: 0
        steps = self.life.teardown()
        self.assertFalse(steps["port_closed"])
        self.assertFalse(steps["clean"])

    def test_teardown_is_safe_twice_and_after_a_failed_start(self):
        self.life.teardown()
        self.assertTrue(self.life.teardown()["dir_removed"])
        # a lifecycle whose start never ran must still clean its directory
        self.setUp()
        self.life.teardown()
        self.assertFalse(self.endpoint.dir.exists())

    def test_teardown_closes_the_canary_listener_and_decoy(self):
        self.life.teardown()
        logged = [e[0] for e in self.fakes.log]
        self.assertIn("close_canary", logged)
        self.assertIn("close_decoy", logged)


class FailedStartCleanupTests(LifecycleBase):
    def test_nothing_is_left_when_the_proof_fails(self):
        self.life.create()
        self.fakes.answer = self.answers(leak=("neg_r_read_canary",))
        with self.assertRaises(side.EndpointFailed):
            self.life.prove_profiles("/c", "/a", "/d")
        self.life.teardown()
        self.assertFalse(self.endpoint.dir.exists())
        self.assertEqual(self.spawned(), [])


if __name__ == "__main__":
    unittest.main()
