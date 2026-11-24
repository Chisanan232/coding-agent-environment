#!/usr/bin/env python3
"""SPE-188: scripts/scheduler-observe.py prints one line and reports only material change.

Every test uses private temporary directories for the queue receipts, the monitor status, the supervisor state and the observer's own
fingerprint. The owner's real queue, monitor and supervisor state are never read or written, and no network or `gh` call is made
(the pull-request provider is injected)."""
import contextlib
import io
import json
import os
from pathlib import Path
from importlib.util import module_from_spec, spec_from_file_location
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = spec_from_file_location("scheduler_observe", ROOT / "scripts/scheduler-observe.py")
so = module_from_spec(_spec)
_spec.loader.exec_module(so)


class Observe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.queue, self.sup, self.state_dir = root / "queue", root / "sup", root / "obs"
        self.queue.mkdir()
        self.sup.mkdir()
        self.monitor = root / "monitor.json"
        self.failure_file = root / "failures.json"
        self.state_arg = str(self.state_dir / "last.json")
        self.extra_args = []
        self.environ = {}
        self.receipts = []
        self.prs = [229, 227]
        self.pr_error = False
        self.write_all(telegram="running", incidents=[], handoff=[], mode_actions=[])

    # -- fixtures ------------------------------------------------------------------------------------
    def event(self, event, ident, **extra):
        self.receipts.append(dict(event=event, id=ident, ts="2026-11-24T00:00:00Z", **extra))
        (self.queue / "receipts.jsonl").write_text("\n".join(json.dumps(r) for r in self.receipts) + "\n")

    def write_all(self, telegram="running", incidents=(), handoff=(), mode_actions=(), provider=False, poll=1):
        (self.queue / "receipts.jsonl").write_text("\n".join(json.dumps(r) for r in self.receipts) + ("\n" if self.receipts else ""))
        self.monitor.write_text(json.dumps({"actions": list(mode_actions), "handoff_incidents": list(handoff)}))
        (self.sup / "telegram-health.json").write_text(json.dumps({"status": telegram, "completed_poll_batches": poll}))
        (self.sup / "incident-status.json").write_text(json.dumps(
            {"open_incidents": [{"incident_ref": r, "message_id": "9"} for r in incidents]}))
        marker = self.sup / "provider-maintenance.json"
        if provider:
            marker.write_text("{}")
        elif marker.exists():
            marker.unlink()

    def run_once(self):
        argv = ["--state", self.state_arg, "--queue-dir", str(self.queue),
                "--monitor-status", str(self.monitor), "--supervisor-dir", str(self.sup), *self.extra_args]
        if self.failure_file is not None:
            argv += ["--failure-file", str(self.failure_file)]

        def provider(repo):
            if self.pr_error:
                raise RuntimeError("gh unavailable")
            return list(self.prs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = so.main(argv, environ=self.environ, pr_provider=provider)
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    # -- behaviour -----------------------------------------------------------------------------------
    def test_the_first_run_records_a_baseline_quietly_and_the_second_is_no_change(self):
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_volatile_counter_never_changes_the_fingerprint(self):
        self.run_once()
        self.write_all(poll=999999)
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_new_incident_is_reported_once_then_quiet(self):
        self.run_once()
        self.write_all(incidents=["inc:aaaa"])
        self.assertEqual(self.run_once(), "CHANGE: incident opened inc:aaaa")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_recovery_is_reported_once(self):
        self.write_all(incidents=["inc:aaaa"])
        self.run_once()
        self.write_all(incidents=[])
        self.assertEqual(self.run_once(), "CHANGE: incident resolved inc:aaaa")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_the_supervisor_leaving_running_and_coming_back_is_reported_both_times(self):
        self.run_once()
        self.write_all(telegram="degraded")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram running->degraded")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        self.write_all(telegram="running")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram degraded->running")

    def test_routine_queue_churn_is_quiet_and_then_settles(self):
        self.run_once()
        self.event("granted", "q1")
        self.event("claimed", "q1")
        self.assertEqual(self.run_once(), "QUIET")
        self.event("released", "q1")
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_live_backfill_claim_is_reported_but_an_unclaimed_or_revoked_one_is_quiet(self):
        self.run_once()
        self.event("backfill", "q2", reason="head-floor-only")
        self.event("backfill-revoked", "q2", kind="head-unprotected")
        self.assertEqual(self.run_once(), "QUIET")
        self.event("backfill", "q3", reason="qualified")
        self.event("claimed", "q3")
        line = self.run_once()
        self.assertTrue(line.startswith("CHANGE: live backfill claimed q3"), line)
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_the_first_cap_refusal_is_reported_and_later_ones_are_quiet(self):
        self.run_once()
        self.event("backfill-decision", "q4", reason="cumulative-loss-cap", outcome="refused")
        self.assertEqual(self.run_once(), "CHANGE: first cumulative-loss-cap refusal")
        self.event("backfill-decision", "q5", reason="cumulative-loss-cap", outcome="refused")
        self.assertIn(self.run_once(), ("NO_CHANGE", "QUIET"))

    def test_a_monitor_mode_change_and_a_new_open_pr_are_reported(self):
        self.run_once()
        self.write_all(mode_actions=[{"mode": "act", "executed": True}])
        self.assertEqual(self.run_once(), "CHANGE: monitor mode observe->act")
        self.prs = [229, 227, 321]
        self.assertEqual(self.run_once(), "CHANGE: new open PR #321")
        self.prs = [229, 227]
        self.assertEqual(self.run_once(), "QUIET")

    def test_monitor_handoff_incident_changes_alone_are_quiet(self):
        self.run_once()
        self.write_all(handoff=[{"id": "q9", "resolved_at": None}])
        self.assertEqual(self.run_once(), "QUIET")
        self.write_all(handoff=[{"id": "q9", "resolved_at": 5}])
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_an_unreadable_source_keeps_its_value_and_is_reported_only_when_it_repeats(self):
        self.run_once()
        (self.sup / "telegram-health.json").write_text("{not json")
        self.assertEqual(self.run_once(), "QUIET")                                   # first failure: unconfirmed
        self.assertEqual(self.run_once(), "CHANGE: source supervisor unreadable (JSONDecodeError) twice running")
        self.assertEqual(self.run_once(), "NO_CHANGE")                               # the same error again: already reported
        self.write_all(telegram="running")
        self.assertEqual(self.run_once(), "CHANGE: source supervisor readable again")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_failed_read_does_not_hide_a_change_that_happened_meanwhile(self):
        self.run_once()
        (self.sup / "telegram-health.json").write_text("{not json")
        self.assertEqual(self.run_once(), "QUIET")
        self.write_all(telegram="degraded")
        self.assertEqual(self.run_once(), "CHANGE: supervisor telegram running->degraded")

    def test_a_single_transient_failure_costs_nothing_afterwards(self):
        self.run_once()
        self.pr_error = True
        self.assertEqual(self.run_once(), "QUIET")
        self.pr_error = False
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_corrupt_state_file_is_rebuilt_without_a_report(self):
        self.run_once()
        (self.state_dir / "last.json").write_text("garbage")
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")
        data = json.loads((self.state_dir / "last.json").read_text())
        self.assertEqual(data["schema"], 1)

    def test_the_state_file_is_private_and_the_only_thing_written(self):
        before = {p: p.stat().st_mtime_ns for p in Path(self.tmp.name).rglob("*") if p.is_file()}
        self.run_once()
        state = self.state_dir / "last.json"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        changed = {p for p in Path(self.tmp.name).rglob("*") if p.is_file() and before.get(p) != p.stat().st_mtime_ns}
        self.assertEqual(changed, {state})
        self.assertEqual([p.name for p in self.state_dir.iterdir()], ["last.json"])

    def test_output_never_carries_a_path_or_secretlike_text(self):
        self.write_all(incidents=["inc:aaaa"])
        self.run_once()
        self.write_all(incidents=["inc:aaaa", "inc:bbbb"], telegram="running")
        line = self.run_once()
        self.assertNotIn("/", line)
        self.assertNotIn(self.tmp.name, line)
        self.assertNotIn("message_id", line)
        self.assertEqual(so.clean("a/b\\c \x1b[31m$(x)`y`"), "abc 31m(x)y")

    def test_a_missing_queue_directory_never_crashes_the_caller(self):
        self.run_once()
        for p in self.queue.iterdir():
            p.unlink()
        self.queue.rmdir()
        self.assertIn(self.run_once(), ("QUIET", "NO_CHANGE"))

    # -- wrong-typed stored state (HIGH finding) -------------------------------------------------------
    def test_wrong_typed_state_for_every_source_is_repaired_and_the_next_real_change_is_reported(self):
        bad_states = {
            "queue": {"queue": "x"}, "monitor": {"monitor": []}, "supervisor": {"supervisor": 5}, "prs": {"prs": 3},
            "queue-fields": {"queue": {"claimed": 1, "backfilled_claimed": [], "backfill_events": 0, "cap_refusals": 0, "floor_only_grants": 0}},
            "monitor-mode": {"monitor": {"mode": "weird", "open_handoff": [], "resolved_handoff": []}},
            "unknown-key": {"mystery": {}},
            "prs-bool": {"prs": [True]},
        }
        for name, fingerprint in bad_states.items():
            with self.subTest(name):
                self.write_all(incidents=[])
                self.run_once()
                (self.state_dir / "last.json").write_text(json.dumps({"schema": 1, "fingerprint": fingerprint}))
                self.assertEqual(self.run_once(), "QUIET")                       # rebuilt, not stuck
                data = json.loads((self.state_dir / "last.json").read_text())
                self.assertTrue(so.valid_fingerprint(data["fingerprint"]))
                self.write_all(incidents=["inc:real"])
                self.assertEqual(self.run_once(), "CHANGE: incident opened inc:real")
                self.write_all(incidents=[])
                self.run_once()

    def test_wrong_typed_error_bookkeeping_is_discarded(self):
        self.run_once()
        data = json.loads((self.state_dir / "last.json").read_text())
        data["errors"] = {"queue": {"class": 5, "count": "x"}}
        (self.state_dir / "last.json").write_text(json.dumps(data))
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_comparison_that_raises_still_persists_a_fresh_fingerprint_and_is_not_stuck(self):
        self.run_once()
        self.write_all(incidents=["inc:aaaa"])
        with patch.object(so, "delta", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_once(), "QUIET")                           # first internal failure: unconfirmed
        data = json.loads((self.state_dir / "last.json").read_text())
        self.assertEqual(data["fingerprint"]["supervisor"]["open_incidents"], ["inc:aaaa"])
        self.assertEqual(self.run_once(), "NO_CHANGE")                           # repaired: the bad run no longer repeats
        self.write_all(incidents=["inc:aaaa", "inc:bbbb"])
        self.assertEqual(self.run_once(), "CHANGE: incident opened inc:bbbb")

    # -- a broken observer must not look healthy (MEDIUM a) ---------------------------------------------
    def test_an_unwritable_state_directory_is_reported_once_and_recovery_once(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("a file where a directory is needed")
        self.state_arg = str(blocker / "last.json")
        self.assertEqual(self.run_once(), "QUIET")
        second = self.run_once()
        self.assertTrue(second.startswith("CHANGE: observer failing ("), second)
        self.assertEqual(self.run_once(), "NO_CHANGE")
        self.state_arg = str(self.state_dir / "last.json")
        self.assertEqual(self.run_once(), "CHANGE: observer recovered")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_a_single_internal_failure_is_not_reported_even_after_recovery(self):
        self.run_once()
        with patch.object(so, "save_state", side_effect=OSError("disk")):
            self.assertEqual(self.run_once(), "QUIET")
        self.assertIn(self.run_once(), ("NO_CHANGE", "QUIET"))
        self.assertFalse(self.failure_file.exists())

    def test_when_even_the_failure_counter_cannot_be_written_the_failure_is_reported_every_run(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("x")
        self.state_arg = str(blocker / "last.json")
        self.failure_file = blocker / "failures.json"
        for _ in range(3):
            line = self.run_once()
            self.assertTrue(line.startswith("CHANGE: observer failing ("), line)

    def test_recovery_after_a_reported_failure_still_reports_a_real_change_in_the_same_line(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("x")
        self.state_arg = str(blocker / "last.json")
        self.run_once()
        self.run_once()
        self.state_arg = str(self.state_dir / "last.json")
        self.assertEqual(self.run_once(), "CHANGE: observer recovered")
        self.assertFalse(self.failure_file.exists())
        self.assertEqual(self.run_once(), "NO_CHANGE")

    # -- PR provider switch (MEDIUM b) -------------------------------------------------------------------
    def test_the_pr_provider_can_be_switched_off_by_flag_or_environment(self):
        calls = []
        original = so.gh_prs
        self.addCleanup(setattr, so, "gh_prs", original)
        for label, extra, environ in (("flag", ["--no-prs"], {}), ("env", [], {"SCHEDULER_OBSERVE_NO_PRS": "1"})):
            with self.subTest(label):
                self.extra_args, self.environ = extra, environ
                self.state_arg = str(self.state_dir / f"{label}.json")
                self.prs = [1]

                def spy(repo):
                    calls.append(repo)
                    return [1]
                out = io.StringIO()
                argv = ["--state", self.state_arg, "--failure-file", str(self.failure_file), "--queue-dir", str(self.queue),
                        "--monitor-status", str(self.monitor), "--supervisor-dir", str(self.sup), *extra]
                with contextlib.redirect_stdout(out):
                    so.main(argv, environ=environ, pr_provider=spy)
                    so.main(argv, environ=environ, pr_provider=spy)
                self.assertEqual(calls, [])
                self.assertNotIn("prs", json.loads(Path(self.state_arg).read_text())["fingerprint"])

    # -- bounds and file mode (LOW) --------------------------------------------------------------------
    def test_an_oversized_json_file_is_a_source_error_not_a_crash(self):
        self.run_once()
        self.assertLess((self.state_dir / "last.json").stat().st_size, 1000)
        self.monitor.write_text("{}" + " " * 2000)                                   # valid JSON, but larger than the cap
        with patch.object(so, "MAX_JSON_BYTES", 1000):
            self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")

    def test_only_the_tail_of_a_large_receipts_file_is_read(self):
        self.event("claimed", "ancient")                                           # never released, but older than the window
        for n in range(200):
            self.event("claimed", f"old{n}")
            self.event("released", f"old{n}")
        self.event("claimed", "live1")
        self.assertGreater((self.queue / "receipts.jsonl").stat().st_size, 600)
        self.assertEqual(so.observe_queue(self.queue, 600)["claimed"], ["live1"])
        self.assertEqual(so.observe_queue(self.queue)["claimed"], ["ancient", "live1"])

    def test_the_state_and_failure_files_stay_private_whatever_the_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        self.run_once()
        so.write_failure(self.failure_file, {"class": "X", "count": 1})
        for path in (self.state_dir / "last.json", self.failure_file):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    # -- the failure counter lives only in a private per-user directory (security review) -------------------
    def default_counter(self):
        self.failure_file = None
        return self.state_dir / so.FAILURE_NAME

    def fail_once(self):
        with patch.object(so, "save_state", side_effect=OSError("disk")):
            return self.run_once()

    def test_the_default_counter_sits_next_to_the_state_in_a_0700_directory_with_a_0600_file(self):
        counter = self.default_counter()
        self.run_once()
        self.assertEqual(self.fail_once(), "QUIET")
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(counter.stat().st_mode), 0o600)
        self.assertTrue(self.fail_once().startswith("CHANGE: observer failing ("))

    def test_a_looser_existing_state_directory_is_tightened(self):
        self.state_dir.mkdir()
        os.chmod(self.state_dir, 0o755)
        self.default_counter()
        self.run_once()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    def test_a_planted_symlink_at_the_old_shared_temp_path_is_ignored_and_never_written(self):
        old = Path(tempfile.gettempdir()) / f"scheduler-observe-failures-{os.getuid()}.json"
        if old.exists() or old.is_symlink():
            self.skipTest("the legacy path exists on this machine; not touching it")
        decoy = Path(self.tmp.name) / "decoy.json"
        decoy.write_text(json.dumps({"class": "Evil", "count": 5}))
        old.symlink_to(decoy)
        self.addCleanup(old.unlink)
        self.default_counter()
        self.run_once()
        self.assertEqual(self.fail_once(), "QUIET")                                  # the planted count of 5 was never consulted
        self.assertEqual(json.loads(decoy.read_text()), {"class": "Evil", "count": 5})
        self.assertTrue(old.is_symlink())

    def test_a_counter_that_is_a_symlink_wrong_mode_or_not_a_regular_file_is_discarded(self):
        counter = self.default_counter()
        self.run_once()
        decoy = Path(self.tmp.name) / "decoy2.json"
        decoy.write_text(json.dumps({"class": "X", "count": 5}))
        os.chmod(decoy, 0o600)
        cases = {}
        cases["symlink"] = lambda: counter.symlink_to(decoy)
        cases["group-readable"] = lambda: (counter.write_text(json.dumps({"class": "X", "count": 5})), os.chmod(counter, 0o640))
        cases["fifo"] = lambda: os.mkfifo(counter)
        cases["oversized"] = lambda: (counter.write_text(" " * 9000 + json.dumps({"class": "X", "count": 5})), os.chmod(counter, 0o600))
        for name, plant in cases.items():
            with self.subTest(name):
                so._MEMORY_FAILURE.clear()
                if counter.exists() or counter.is_symlink():
                    counter.unlink()
                plant()
                self.assertIsNone(so.read_failure(counter))
                self.assertEqual(self.fail_once(), "QUIET")                          # a planted count of 5 would have printed CHANGE
                if counter.exists() and not counter.is_symlink():
                    counter.unlink()

    def test_a_counter_owned_by_someone_else_is_discarded(self):
        counter = self.default_counter()
        self.run_once()
        so.write_failure(counter, {"class": "X", "count": 5})
        so._MEMORY_FAILURE.clear()
        with patch.object(so.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(so.read_failure(counter))

    def test_a_foreign_or_symlinked_private_directory_is_refused_and_nothing_goes_to_a_shared_temp_path(self):
        before = set(Path(tempfile.gettempdir()).glob("scheduler-observe-failures-*"))
        target = Path(self.tmp.name) / "real"
        target.mkdir()
        link = Path(self.tmp.name) / "linked"
        link.symlink_to(target)
        self.state_arg = str(link / "last.json")
        self.failure_file = None
        for _ in range(3):
            line = self.run_once()
            self.assertTrue(line.startswith("CHANGE: observer failing ("), line)    # unusable directory: reported, not silent
        self.assertEqual(list(target.iterdir()), [])
        with patch.object(so.os, "getuid", return_value=os.getuid() + 1):
            self.assertRaises(PermissionError, so.ensure_private_dir, self.state_dir)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("scheduler-observe-failures-*")), before)

    def test_a_deeply_nested_state_file_cannot_crash_or_stall_the_observer(self):
        self.default_counter()
        self.run_once()
        (self.state_dir / "last.json").write_text("[" * 100000)
        os.chmod(self.state_dir / "last.json", 0o600)
        self.assertEqual(self.run_once(), "QUIET")
        self.assertEqual(self.run_once(), "NO_CHANGE")


if __name__ == "__main__":
    unittest.main()
