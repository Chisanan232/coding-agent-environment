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
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_lifecycle_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

PORT = 23456


class FakeProc:
    """exited = the process is gone but its exit status is not collected yet (a zombie); reaped = poll() or wait() collected it."""

    def __init__(self, pid):
        self.pid = pid
        self.exited = False
        self.reaped = False
        self.code = None
        self.waits = []

    @property
    def returncode(self):
        return self.code if self.reaped else None

    def poll(self):
        if self.exited:
            self.reaped = True
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.exited:
            raise subprocess.TimeoutExpired("x", timeout)
        self.reaped = True
        return self.code

    def exit(self, code=0):
        self.exited = True
        self.code = code


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
        self.table = []                   # [(pid, ppid, start)] of everything else
        self.kills = []
        self.sigkill_while_unreaped = []

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
        if proc.exited:
            return side.ECONNREFUSED
        return self.connect_after_start

    def kind_of(self, pid):
        return next((k for k, p in self.procs.items() if p.pid == pid), "?")

    def listeners(self, port):
        self.log.append(("listeners", port))
        result = self.listeners_result
        if callable(result):
            return result()
        if result is None:
            return None
        daemon = self.procs.get("daemon")
        return [(daemon.pid if daemon else 0, name) if isinstance(name, str) else name for name in result]

    def leader_exited(self, proc):
        return proc.exited

    def process_table(self):
        rows = [(self.procs[k].pid, 1, "t0") for k in self.procs if not self.procs[k].exited]
        return rows + list(self.table)

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        self.table = [r for r in self.table if r[0] != pid and r[0] not in getattr(self, "immortal", ())]
        if pid in getattr(self, "immortal", ()):
            self.table = self.table + [r for r in self.immortal_rows if r[0] == pid and r not in self.table]

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))
        self.log.append(("signal", self.kind_of(pid), sig))
        proc = next((p for p in self.procs.values() if p.pid == pid), None)
        if proc is not None and sig == signal.SIGKILL:
            self.sigkill_while_unreaped.append(not proc.reaped)
        if proc is not None and not proc.exited and not (sig == signal.SIGTERM and pid in self.stubborn):
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
                                decoy_socket=self.decoy_socket, leader_exited=self.leader_exited, process_table=self.process_table, kill=self.kill)


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

    def test_teardown_after_a_failed_create_leaves_a_directory_it_did_not_make(self):
        self.life.create()
        marker = self.endpoint.state / "theirs"
        marker.write_text("x")
        other = side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops())
        with self.assertRaises(side.EndpointFailed):
            other.create()
        steps = other.teardown()
        self.assertTrue(marker.exists())
        self.assertFalse(steps["clean"])
        self.assertTrue(steps["not_ours"])

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
        self.assertEqual(relay[2][:4], ("/usr/bin/sandbox-exec", "-f", str(self.record_dir / "relay.sb"), side.tool_python()))
        self.assertEqual(relay[2][4:6], ("-I", "-c"))
        self.assertEqual(relay[2][6:], (side.RELAY_SRC, str(self.endpoint.relay_sock), str(PORT)))
        self.assertEqual(relay[3], {"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"})
        kinds = [e[0] for e in self.fakes.log]
        self.assertLess(kinds.index("listeners"), len(kinds) - 1 - kinds[::-1].index("spawn"))      # the port is proved before the relay starts
        self.assertEqual(self.life.record["port_proof"]["listeners"], ["127.0.0.1:%d" % PORT])
        self.assertEqual(self.life.record["port_proof"]["daemon_pid"], self.fakes.procs["daemon"].pid)
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

    def test_another_process_listening_on_the_port_fails_the_proof_whoever_owns_it(self):
        addr = "127.0.0.1:%d" % PORT
        cases = (lambda d: [(4242, addr)],                                  # someone else holds it
                 lambda d: [(d, addr), (4242, addr)],                       # a second listener appeared
                 lambda d: [(d, addr), (d, "[::1]:%d" % PORT)],             # the daemon also listens on ::1
                 lambda d: [(d, "*:%d" % PORT)])                            # wildcard
        for make in cases:
            fakes = Fakes(self.endpoint)
            fakes.listeners_result = lambda fakes=fakes, make=make: make(fakes.procs["daemon"].pid)
            life = side.EndpointLifecycle(self.endpoint, self.record_dir, fakes.ops())
            if self.endpoint.daemon_sock.exists():
                self.endpoint.daemon_sock.unlink()
            with self.assertRaises(side.EndpointFailed):
                life.start()

    def test_the_port_proof_is_repeated_on_every_supervision_step(self):
        self.life.start()
        self.assertTrue(self.life.supervise())
        before = len([e for e in self.fakes.log if e[0] == "listeners"])
        self.assertTrue(self.life.supervise())
        self.assertEqual(len([e for e in self.fakes.log if e[0] == "listeners"]), before + 1)
        daemon_pid = self.fakes.procs["daemon"].pid
        self.fakes.listeners_result = lambda: [(daemon_pid, "127.0.0.1:%d" % PORT), (daemon_pid, "*:%d" % PORT)]     # the daemon rebinds wide
        self.assertFalse(self.life.supervise())
        self.assertIn("port_proof_lost", self.life.record)
        relay = self.fakes.procs["relay"]
        self.assertIn((relay.pid, signal.SIGTERM), self.fakes.signals)                      # the relay stops carrying traffic
        daemon = self.fakes.procs["daemon"]
        self.assertIn((daemon.pid, signal.SIGKILL), self.fakes.signals)                     # and a wildcard-bound daemon stops serving

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
        self.assertTrue(self.life.record["daemon_exited_early"])


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

    def test_the_group_is_killed_before_the_leader_is_reaped_and_a_reaped_process_is_never_signalled(self):
        self.life.teardown()
        self.assertTrue(self.fakes.sigkill_while_unreaped)
        self.assertTrue(all(self.fakes.sigkill_while_unreaped), self.fakes.sigkill_while_unreaped)    # every SIGKILL came while the pid was still held
        sent = len(self.fakes.signals)
        self.life.teardown()                                                              # both are reaped now
        self.assertEqual(len(self.fakes.signals), sent)

    def test_a_descendant_that_survives_the_group_kill_is_found_killed_and_reported(self):
        daemon = self.fakes.procs["daemon"]
        self.fakes.table = [(9001, daemon.pid, "t1"), (9002, 9001, "t2")]                 # grandchild escaped the group
        steps = self.life.teardown()
        self.assertEqual(sorted(p for p, _ in self.fakes.kills), [9001, 9002])
        self.assertEqual(steps["descendants_left"], [])
        self.assertTrue(steps["clean"], steps)

    def test_a_descendant_that_cannot_be_killed_makes_teardown_unclean(self):
        daemon = self.fakes.procs["daemon"]
        self.fakes.immortal = {9001}
        self.fakes.immortal_rows = [(9001, daemon.pid, "t1")]
        self.fakes.table = list(self.fakes.immortal_rows)
        steps = self.life.teardown()
        self.assertEqual(steps["descendants_left"], [9001])
        self.assertFalse(steps["clean"])

    def test_an_unreadable_process_table_is_not_clean(self):
        self.life.ops.process_table = lambda: None
        steps = self.life.teardown()
        self.assertEqual(steps["descendants_left"], ["unknown"])
        self.assertFalse(steps["clean"])

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


class StateSnapshotTests(LifecycleBase):
    """The copy of the daemon's state runs as the QA user without the daemon's sandbox, so it follows nothing."""

    def setUp(self):
        super().setUp()
        self.life.create()
        self.dst = self.record_dir / "state-copy"

    def snapshot(self):
        return self.life._snapshot_state(self.dst)

    def test_regular_files_and_directories_are_copied_and_the_socket_is_skipped(self):
        import socket as sk
        state = self.endpoint.state
        (state / "a.db").write_bytes(b"rows")
        (state / "sub").mkdir()
        (state / "sub" / "b.log").write_bytes(b"log")
        server = sk.socket(sk.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(state)
        try:
            server.bind("fornax.sock")
        finally:
            os.chdir(cwd)
            self.addCleanup(server.close)
        got = self.snapshot()
        self.assertTrue(got["copied"], got)
        self.assertEqual(got["bytes"], 7)
        self.assertEqual((self.dst / "a.db").read_bytes(), b"rows")
        self.assertEqual((self.dst / "sub" / "b.log").read_bytes(), b"log")
        self.assertFalse((self.dst / "fornax.sock").exists())

    def test_a_hard_link_or_a_symlink_is_never_followed_and_fails_the_snapshot(self):
        outside = self.home / "secret.txt"
        outside.write_text("do not copy")
        os.link(outside, self.endpoint.state / "linked")
        os.symlink(outside, self.endpoint.state / "sym")
        (self.endpoint.state / "ok").write_text("fine")
        got = self.snapshot()
        self.assertFalse(got["copied"], got)
        self.assertEqual(sorted(got["refused"]), ["linked", "sym"])
        self.assertFalse((self.dst / "linked").exists())
        self.assertFalse((self.dst / "sym").exists())
        self.assertEqual((self.dst / "ok").read_text(), "fine")

    def test_a_fifo_is_refused_and_the_size_limit_is_enforced_while_copying(self):
        os.mkfifo(self.endpoint.state / "fifo")
        self.assertIn("fifo", self.snapshot()["refused"])
        (self.endpoint.state / "fifo").unlink()
        __import__("shutil").rmtree(self.dst)
        (self.endpoint.state / "big").write_bytes(b"x" * 100)
        with mock.patch.object(side, "SNAPSHOT_MAX_BYTES", 50):
            got = self.snapshot()
        self.assertFalse(got["copied"])
        self.assertIn("limit", got["error"])

    def test_a_flood_of_tiny_files_is_stopped_by_the_entry_cap(self):
        for n in range(30):
            (self.endpoint.state / ("f%d" % n)).write_text("")
        with mock.patch.object(side, "SNAPSHOT_MAX_ENTRIES", 10):
            got = self.snapshot()
        self.assertFalse(got["copied"])
        self.assertIn("limit", got["error"])

    def test_a_socket_is_named_as_skipped_not_silently_dropped(self):
        import socket as sk
        server = sk.socket(sk.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(self.endpoint.state)
        try:
            server.bind("other.sock")
        finally:
            os.chdir(cwd)
            self.addCleanup(server.close)
        got = self.snapshot()
        self.assertEqual(got["skipped_sockets"], ["other.sock"])

    def test_no_snapshot_is_taken_while_a_process_of_the_endpoint_is_still_running(self):
        (self.endpoint.state / "kept").write_text("x")
        with mock.patch.object(side.EndpointLifecycle, "_reap_stragglers", lambda self, before: [4242]):
            steps = self.life.teardown(snapshot=self.dst)
        self.assertFalse(steps["state_snapshot"]["copied"])
        self.assertFalse(self.dst.exists())

    def test_teardown_with_a_snapshot_reports_it_and_never_for_a_directory_it_does_not_own(self):
        (self.endpoint.state / "kept").write_text("x")
        steps = self.life.teardown(snapshot=self.dst)
        self.assertTrue(steps["state_snapshot"]["copied"], steps)
        self.assertEqual((self.dst / "kept").read_text(), "x")
        other = side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops())
        self.assertNotIn("state_snapshot", other.teardown(snapshot=self.home / "x-copy"))


class FailedStartCleanupTests(LifecycleBase):
    def test_nothing_is_left_when_the_proof_fails(self):
        self.life.create()
        self.fakes.answer = self.answers(leak=("neg_r_read_canary",))
        with self.assertRaises(side.EndpointFailed):
            self.life.prove_profiles("/c", "/a", "/d")
        self.life.teardown()
        self.assertFalse(self.endpoint.dir.exists())
        self.assertEqual(self.spawned(), [])


GOOD_REPLY = b'{"ok": true, "mode": "shadow"}\n'


class UnixFakes(Fakes):
    """The unix-contract daemon (SPE-155): the spawned daemon binds a REAL socket node at its path (0600, under a 0700 directory) so the
    lstat checks see the real thing; who answers, what it answers and which inet sockets the group holds are knobs."""

    def __init__(self, endpoint):
        super().__init__(endpoint)
        self.reply = GOOD_REPLY
        self.peer = "child"                 # 'child' (a process in the daemon's group), 'leader', 'stranger'
        self.peer_uid = os.getuid()
        self.inet = []
        self.ping_fails = False
        self.on_ping = None                 # called during a ping (to swap the node between the two lstats)
        self.pings = []
        self.uds_errno = side.ECONNREFUSED
        self.groups = {}

    def bind_socket(self, path=None):
        import socket as sk
        path = path or self.e.daemon_sock
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        server = sk.socket(sk.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(path.parent)
        try:
            server.bind(path.name)
        finally:
            os.chdir(cwd)
        server.close()                      # the node stays; nothing here needs it to accept
        os.chmod(path, 0o600)

    def popen(self, argv, **kw):
        self.next_pid += 1
        proc = FakeProc(self.next_pid)
        self.log.append(("spawn", "daemon" if argv[3] == self.e.daemon_binary else "other", tuple(argv), kw.get("env"), kw.get("cwd")))
        self.procs["daemon"] = proc
        self.groups = {proc.pid: proc.pid, 7001: proc.pid, 4242: 4242}
        if self.daemon_dies_at_start:
            proc.exit(1)
        else:
            self.bind_socket()
        return proc

    def uds_ping(self, path):
        self.log.append(("ping", path))
        self.pings.append(path)
        if self.ping_fails:
            raise OSError("refused")
        if self.on_ping:
            self.on_ping()
        pid = {"child": 7001, "leader": self.procs["daemon"].pid, "stranger": 4242}[self.peer]
        return pid, self.peer_uid, self.reply

    def getpgid(self, pid):
        if pid not in self.groups:
            raise ProcessLookupError(pid)
        return self.groups[pid]

    def inet_sockets(self, pgid):
        self.log.append(("inet", pgid))
        return self.inet() if callable(self.inet) else self.inet

    def uds_connect(self, path):
        self.log.append(("uds_connect", path))
        return self.uds_errno if os.path.lexists(path) else side.ENOENT

    def ops(self):
        return side.EndpointOps(popen=self.popen, run=self.run, sleep=self.clock.sleep, monotonic=self.clock.now, connect=self.connect,
                                listeners=self.listeners, killpg=self.killpg, rmtree=self.rmtree, canary_listener=self.canary_listener,
                                decoy_socket=self.decoy_socket, leader_exited=self.leader_exited, process_table=self.process_table, kill=self.kill,
                                uds_ping=self.uds_ping, uds_connect=self.uds_connect, inet_sockets=self.inet_sockets, getpgid=self.getpgid)


class UnixBase(LifecycleBase):
    def setUp(self):
        # Short and real: the socket node is bound for real, and a long TMPDIR would push its path past the 104-byte limit.
        self.tmp = tempfile.TemporaryDirectory(prefix=".cae-lc-", dir=str(Path.home()))
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.home, self.ex = root / "h", root / "x"
        self.pay = self.ex / "outbox" / "a" / "payload"
        (self.pay / "bin").mkdir(parents=True)
        self.home.mkdir(mode=0o700)
        (self.pay / "bin" / "python3.13").write_text("x")
        self.endpoint = side.Endpoint(str(self.home), str(self.ex), "a", 0, "circinus-daemon/1", str(self.pay), str(self.pay / "bin" / "python3.13"))
        self.record_dir = self.ex / "outbox" / "a" / "endpoint"
        self.fakes = UnixFakes(self.endpoint)
        self.table = side.endpoint_probe_table(self.endpoint, "/c", "/a", 24999, "/d/qa-decoy.sock", None)
        self.manifest = self.seed_manifest({})
        self.life = self.lifecycle()

    def lifecycle(self, fakes=None):
        return side.EndpointLifecycle(self.endpoint, self.record_dir, (fakes or self.fakes).ops(), manifest=self.manifest)

    def seed_manifest(self, files, digests=None):
        """A manifest whose payload holds bin/python3.13 and the given state-seed files (written to the payload directory)."""
        import hashlib
        entries = [{"path": "bin/python3.13", "sha256": hashlib.sha256(b"x").hexdigest(), "size": 1, "kind": "exec"}]
        for rel, data in files.items():
            target = self.pay / "state-seed" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append({"path": "state-seed/" + rel, "sha256": (digests or {}).get(rel, hashlib.sha256(data).hexdigest()), "size": len(data),
                            "kind": "data"})
        return {"files": sorted(entries, key=lambda f: f["path"]), "endpoint": {"daemon": "bin/python3.13", "contract": "circinus-daemon/1"}}

    def up(self):
        self.life.create()
        self.prove()
        self.life.start()


class UnixCreateAndProveTests(UnixBase):
    def test_create_makes_no_relay_directory_and_writes_only_the_daemon_profile(self):
        self.life.create()
        self.assertFalse((self.endpoint.dir / "rl").exists())
        self.assertEqual(sorted(p.name for p in self.record_dir.iterdir()), ["daemon.sb"])
        self.assertEqual((self.record_dir / "daemon.sb").read_text(), side.daemon_sb(self.endpoint))
        self.assertEqual(self.life.record["daemon_launch_sha256"], side.endpoint_digests(self.endpoint)["daemon_launch_sha256"])
        self.assertNotIn("relay_started", self.life.record)
        self.assertNotIn("relay_profile_sha256", self.life.record)
        self.assertEqual(self.life.record["transport"], "unix")
        self.assertEqual(self.life.record["seeded"], [])

    def test_prove_profiles_has_no_port_check_and_runs_only_the_daemon_profile(self):
        self.life.create()
        self.prove()
        self.assertNotIn(("connect",), self.fakes.log)
        self.assertEqual({p[1] for p in self.fakes.log if p[0] == "probe"}, {"daemon.sb"})
        self.assertEqual(len([p for p in self.fakes.log if p[0] == "probe"]), len(self.table))
        self.assertEqual(self.spawned(), [])

    def test_a_leaking_unix_control_fails_before_the_daemon_starts(self):
        self.life.create()
        for leaked in ("neg_d_bind_loopback", "neg_d_bind_tmp_socket", "pos_d_bind_state_socket"):
            self.fakes.answer = self.answers(leak=(leaked,))
            with self.assertRaises(side.EndpointFailed) as caught:
                self.lifecycle().prove_profiles("/c", "/a", "/d")
            self.assertIn(leaked, str(caught.exception))
        self.assertEqual(self.spawned(), [])


class UnixSeedTests(UnixBase):
    def test_seed_files_are_copied_private_before_anything_runs_and_recorded(self):
        import hashlib
        self.manifest = self.seed_manifest({"install.json": b'{"a": 1}', "circinus/mode.json": b'{"mode": "shadow"}'})
        life = self.lifecycle()
        life.create()
        state = self.endpoint.state
        self.assertEqual((state / "install.json").read_bytes(), b'{"a": 1}')
        self.assertEqual((state / "circinus" / "mode.json").read_bytes(), b'{"mode": "shadow"}')
        self.assertEqual(stat.S_IMODE(os.lstat(state / "install.json").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(state / "circinus").st_mode), 0o700)
        self.assertEqual(life.record["seeded"], [{"path": "circinus/mode.json", "sha256": hashlib.sha256(b'{"mode": "shadow"}').hexdigest()},
                                                 {"path": "install.json", "sha256": hashlib.sha256(b'{"a": 1}').hexdigest()}])
        self.assertEqual(life.record["seeded"], side.seed_record(self.manifest))
        self.assertEqual(self.fakes.log, [])                       # nothing ran: no probe, no spawn

    def test_a_file_that_does_not_match_its_digest_is_refused(self):
        self.manifest = self.seed_manifest({"install.json": b"x"}, digests={"install.json": "0" * 64})
        with self.assertRaises(side.EndpointFailed) as caught:
            self.lifecycle().create()
        self.assertIn("SHA-256", str(caught.exception))

    def test_a_linked_seed_source_is_refused(self):
        self.manifest = self.seed_manifest({"install.json": b"x"})
        target = self.pay / "state-seed" / "install.json"
        target.unlink()
        (self.home / "secret").write_text("x")
        os.symlink(self.home / "secret", target)
        with self.assertRaises(side.EndpointFailed):
            self.lifecycle().create()

    def test_an_existing_file_is_never_overwritten_and_a_linked_directory_is_never_followed(self):
        self.manifest = self.seed_manifest({"install.json": b"x", "circinus/mode.json": b"m"})
        life = self.lifecycle()
        life.create()
        with self.assertRaises(side.EndpointFailed) as caught:
            life.seed_state()
        self.assertIn("overwrite", str(caught.exception))
        (self.endpoint.state / "install.json").unlink()
        (self.endpoint.state / "circinus" / "mode.json").unlink()
        (self.endpoint.state / "circinus").rmdir()
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.symlink(elsewhere, self.endpoint.state / "circinus")
        with self.assertRaises(side.EndpointFailed):
            life.seed_state()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_manifest_that_breaks_a_seed_rule_is_refused_by_the_lifecycle_too(self):
        self.manifest = self.seed_manifest({"qa-probe": b"x"})
        with self.assertRaises(side.EndpointFailed):
            self.lifecycle().create()
        self.manifest = None
        with self.assertRaises(side.EndpointFailed):
            side.EndpointLifecycle(self.endpoint, self.record_dir, self.fakes.ops()).create()      # a seed contract with no manifest

    def test_a_fornax_endpoint_neither_seeds_nor_records_a_seed(self):
        life = side.EndpointLifecycle(side.Endpoint(str(self.home), str(self.ex), "b", PORT, "fornax-daemon/1", str(self.pay),
                                                    str(self.pay / "bin" / "python3.13")), self.record_dir, Fakes(self.endpoint).ops())
        life.create()
        self.assertNotIn("seeded", life.record)


class UnixStartTests(UnixBase):
    def test_start_spawns_the_contract_argv_and_proves_the_socket_without_a_relay(self):
        self.up()
        (daemon,) = self.spawned()
        self.assertEqual(daemon[2], ("/usr/bin/sandbox-exec", "-f", str(self.record_dir / "daemon.sb")) + tuple(self.endpoint.daemon_argv()))
        self.assertEqual(daemon[2][4:], ("-I", "-B", "-c", side.CIRCINUS_BOOTSTRAP, "daemon-run"))
        self.assertEqual(daemon[3], self.endpoint.daemon_env())
        proof = self.life.record["socket_proof"]
        pid = self.fakes.procs["daemon"].pid
        self.assertEqual(proof, {"socket": str(self.endpoint.daemon_sock), "peer_pid": 7001, "peer_pgid": pid, "peer_uid": os.getuid(),
                                 "daemon_pid": pid, "mode": 0o600, "ping_mode": "shadow", "inet": [], "inode_stable": True})
        self.assertGreaterEqual(len(self.fakes.pings), 2)          # readiness, then the proof's own connection
        self.assertIn(("inet", pid), self.fakes.log)

    def test_a_leader_peer_and_the_enforce_mode_pass(self):
        self.fakes.peer = "leader"
        self.fakes.reply = b'{"ok": true, "mode": "enforce", "extra": 1}\n'
        self.up()
        self.assertEqual(self.life.record["socket_proof"]["ping_mode"], "enforce")

    def fail_start(self, expect=None):
        self.life.create()
        self.prove()
        with self.assertRaises(side.EndpointFailed) as caught:
            self.life.start()
        if expect:
            self.assertIn(expect, str(caught.exception))
        self.assertIsNone(self.life.record["socket_proof"])

    def test_a_peer_outside_the_daemon_group_fails(self):
        self.fakes.peer = "stranger"
        self.fail_start("process group")

    def test_a_peer_that_vanished_fails(self):
        self.fakes.getpgid = lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid))
        self.life = self.lifecycle()
        self.fail_start("process group")

    def test_another_users_peer_fails(self):
        self.fakes.peer_uid = os.getuid() + 1
        self.fail_start("another user")

    def test_a_bad_ping_reply_never_becomes_ready(self):
        for reply in (b'{"ok": false, "mode": "shadow"}\n', b'{"ok": true, "mode": "open"}\n', b"[]\n", b"not json", b"",
                      b'{"ok": "true", "mode": "shadow"}', b'{"ok": true, "mode": "shadow"}' + b" " * side.PING_REPLY_MAX):
            self.setUp()
            self.fakes.reply = reply
            self.fail_start("did not come up")

    def test_a_socket_that_never_answers_fails_at_the_start_wait(self):
        self.fakes.ping_fails = True
        self.fail_start("did not come up")

    def test_a_socket_replaced_between_the_two_lstats_fails(self):
        self.life.create()
        self.prove()
        swaps = []

        def swap():
            if len(self.fakes.pings) >= 2 and not swaps:              # after readiness: during the proof's own connection
                swaps.append(1)
                self.endpoint.daemon_sock.unlink()
                self.fakes.bind_socket()
        self.fakes.on_ping = swap
        with self.assertRaises(side.EndpointFailed) as caught:
            self.life.start()
        self.assertIn("replaced", str(caught.exception))

    def test_an_inet_socket_in_the_daemon_group_or_an_unlistable_group_fails(self):
        for inet, expect in ((["*:*"], "IPv4 or IPv6"), (None, "could not be listed")):
            self.setUp()
            self.fakes.inet = inet
            self.fail_start(expect)

    def test_a_loose_socket_or_directory_or_a_linked_socket_never_becomes_ready(self):
        def loose_socket():
            os.chmod(self.endpoint.daemon_sock, 0o644)

        def loose_dir():
            os.chmod(self.endpoint.daemon_sock.parent, 0o755)

        def linked():
            real = self.endpoint.state / "real.sock"
            self.fakes.bind_socket(real)
            self.endpoint.daemon_sock.unlink()
            os.symlink(real, self.endpoint.daemon_sock)
        def hard_linked():
            os.link(self.endpoint.daemon_sock, self.endpoint.state / "second-name")

        for mutate in (loose_socket, loose_dir, linked, hard_linked):
            self.setUp()
            original = self.fakes.popen

            def popen(argv, original=original, mutate=mutate, **kw):
                proc = original(argv, **kw)
                mutate()
                return proc
            self.fakes.popen = popen
            self.life = self.lifecycle()
            self.fail_start("did not come up")
            self.assertEqual(self.fakes.pings, [], mutate.__name__)      # nothing is dialled through a node that failed its checks


class UnixSupervisionAndTeardownTests(UnixBase):
    def setUp(self):
        super().setUp()
        self.up()

    def test_a_poll_checks_the_inode_and_inet_without_pinging(self):
        pings = len(self.fakes.pings)
        self.assertTrue(self.life.supervise())
        self.assertEqual(len(self.fakes.pings), pings)
        self.assertNotIn("socket_recheck", self.life.record)

    def test_the_final_step_repeats_the_full_proof(self):
        pings = len(self.fakes.pings)
        self.assertTrue(self.life.supervise(final=True))
        self.assertEqual(len(self.fakes.pings), pings + 1)
        self.assertEqual(self.life.record["socket_recheck"], self.life.record["socket_proof"])

    def test_a_replaced_socket_or_a_new_inet_socket_stops_the_daemon(self):
        for mutate, final in ((lambda: (self.endpoint.daemon_sock.unlink(), self.fakes.bind_socket()), False),
                              (lambda: setattr(self.fakes, "inet", ["127.0.0.1:80"]), False),
                              (lambda: setattr(self.fakes, "peer", "stranger"), True)):
            self.setUp()
            mutate()
            self.assertFalse(self.life.supervise(final=final))
            self.assertIn("socket_proof_lost", self.life.record)
            self.assertIn((self.fakes.procs["daemon"].pid, signal.SIGKILL), self.fakes.signals)

    def test_teardown_has_the_unix_steps_and_is_clean(self):
        steps = self.life.teardown()
        self.assertEqual(set(side.ENDPOINT_TEARDOWN_STEPS["unix"]) - set(steps), set())
        for gone in ("relay_stopped", "port_closed"):
            self.assertNotIn(gone, steps)
        self.assertTrue(steps["clean"], steps)
        self.assertEqual(steps["socket_removed_by_helper"], ["circinusd.sock"])      # the stand-in left its node; unlinked and reported
        self.assertFalse(self.endpoint.dir.exists())
        self.assertNotIn(("connect",), self.fakes.log)

    def test_a_socket_that_still_accepts_after_the_stop_is_not_clean(self):
        self.fakes.uds_errno = 0
        steps = self.life.teardown()
        self.assertFalse(steps["socket_refuses"])
        self.assertFalse(steps["clean"])

    def test_the_exec_record_is_established_on_the_socket_proof(self):
        import types
        outbox = self.ex / "outbox" / "a"
        for record, want in (({"socket_proof": {"x": 1}, "problems": []}, True), ({"port_proof": {"x": 1}, "problems": []}, False)):
            life = types.SimpleNamespace(record=dict(record), proof_key="socket_proof", teardown=lambda snapshot: {"clean": True})
            side.finish_exec_endpoint(life, outbox, self.home / "no-canary")
            self.assertIs(life.record["established"], want, record)


if __name__ == "__main__":
    unittest.main()
