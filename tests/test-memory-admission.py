#!/usr/bin/env python3
"""scripts/memory-admission.py with synthetic samples only (SPE-117). Never reads or changes the real machine's state."""
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ma", ROOT / "scripts/memory-admission.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

GIB, PAGE = 1024 ** 3, 16384
RAM = 128 * GIB

# Real formats captured from macOS 26 (values synthetic).
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6406.
Pages active:                                1567550.
Pages inactive:                              1563258.
Pages wired down:                             524300.
Pages occupied by compressor:                4681379.
Decompressions:                            415856000.
Compressions:                              456704000.
Swapins:                                    17446826.
Swapouts:                                   21935261.
"""
SWAP = "total = 69632.00M  used = 69001.25M  free = 630.75M  (encrypted)"


def vm(swapins=0, swapouts=0, compressor_pages=0):
    return {"page_size": PAGE, "free": 1, "swapins": swapins, "swapouts": swapouts, "compressor": compressor_pages}


ZERO_SWAP = {"total": 0, "used": 0, "free": 0}   # a fresh post-reboot machine: no swap files yet


def sample(t, level=1, swapins=0, swapouts=0, compressor=0.0, load=2.0, cpus=16, disk=494 * GIB, swap=ZERO_SWAP, heavy=None, idle=None, cpu_by_pid=None, active_pids=None):
    return {"time": t, "mono": t, "ram": RAM, "pressure_level": level, "swap": swap, "vm": vm(swapins, swapouts, int(compressor * RAM / PAGE)),
            "load1": load, "cpus": cpus, "disk_free": disk, "heavy_procs": {} if heavy is None else heavy,
            "idle_procs": [] if idle is None else idle,
            "cpu_by_pid": {} if cpu_by_pid is None else cpu_by_pid,
            "active_pids": [] if active_pids is None else active_pids}


def metrics(**kw):
    io_mib = kw.pop("io", 0.0)
    pages = int(io_mib * 1024 ** 2 * 10 / PAGE)  # over a 10 s window
    return ma.derive(sample(0, **{k: v for k, v in kw.items() if k not in ("swapins", "swapouts")}),
                     sample(10, swapins=pages, **kw))


def ps_line(pid, ppid, state, etime, cputime, pcpu, comm):
    return f"{pid:>6} {ppid:>6} {state:<2} {etime:>12} {cputime:>10} {pcpu:>5} {comm}"


def ps_out(*lines):
    return "\n".join(lines) + "\n"


class Parsing(unittest.TestCase):
    def test_swapusage_and_vm_stat_parse_real_formats(self):
        s = ma.parse_swapusage(SWAP)
        self.assertEqual((s["total"], s["used"]), (69632 * 1024 ** 2, int(69001.25 * 1024 ** 2)))
        v = ma.parse_vm_stat(VM_STAT)
        self.assertEqual((v["page_size"], v["compressor"], v["swapins"], v["swapouts"]), (16384, 4681379, 17446826, 21935261))

    def test_garbage_and_missing_counters_are_none_not_zero(self):
        self.assertIsNone(ma.parse_swapusage("nonsense"))
        self.assertIsNone(ma.parse_swapusage(""))
        self.assertIsNone(ma.parse_swapusage(None))
        self.assertIsNone(ma.parse_vm_stat("Pages free: 1."))
        self.assertIsNone(ma.parse_vm_stat(VM_STAT.replace("Swapins:", "Whatever:")))

    def test_post_reboot_swap_of_zero_parses_and_is_not_an_error(self):
        self.assertEqual(ma.parse_swapusage("total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)")["total"], 0)

    def test_take_sample_counts_heavy_process_names_only(self):
        def run(cmd, **kw):
            self.assertTrue(os.path.isabs(cmd[0]), cmd)   # PATH is never consulted
            out = {"ps": ps_out(ps_line(10, 500, "S", "05:00", "0:01.00", "0.0", "/usr/bin/cargo"), ps_line(11, 10, "S", "05:00", "0:01.00", "0.0", "/bin/zsh"),
                           ps_line(12, 10, "R", "05:00", "0:09.00", "90.0", "/x/y/rustc"), ps_line(13, 10, "S", "05:00", "0:01.00", "0.0", "/x/rustc"),
                           ps_line(14, 10, "S", "05:00", "0:01.00", "0.0", "claude")), "vm_stat": VM_STAT,
                   "sysctl": {"hw.memsize": str(RAM), "kern.memorystatus_vm_pressure_level": "2", "vm.swapusage": SWAP}.get(cmd[-1], "")}[os.path.basename(cmd[0])]
            return type("R", (), {"returncode": 0, "stdout": out})()
        s = ma.take_sample(run=run, now_fn=lambda: 5.0, mono_fn=lambda: 5.0, load_fn=lambda: (1.0, 1.0, 1.0), cpu_fn=lambda: 16, disk_free_fn=lambda: GIB)
        self.assertEqual(s["heavy_procs"], {"cargo": 1, "rustc": 2})
        self.assertEqual((s["pressure_level"], s["ram"]), (2, RAM))

    def test_take_sample_survives_failing_commands(self):
        def run(cmd, **kw):
            raise OSError("nope")
        s = ma.take_sample(run=run, now_fn=lambda: 1.0, load_fn=lambda: (_ for _ in ()).throw(OSError()), cpu_fn=lambda: None, disk_free_fn=lambda: (_ for _ in ()).throw(OSError()))
        self.assertTrue(all(s[k] is None for k in ("ram", "pressure_level", "swap", "vm", "load1", "cpus", "disk_free", "heavy_procs")))


class Derivation(unittest.TestCase):
    def test_swap_io_rate_and_compressor_ratio(self):
        m = metrics(io=20.0, compressor=0.5)
        self.assertAlmostEqual(m["swap_io_mib_s"], 20.0, delta=0.1)
        self.assertAlmostEqual(m["compressor_ratio"], 0.5, delta=0.01)

    def test_counter_going_backwards_or_zero_window_is_unknown_not_zero(self):
        a, b = sample(0, swapins=100), sample(10, swapins=50)
        self.assertIsNone(ma.derive(a, b)["swap_io_mib_s"])
        self.assertIsNone(ma.derive(sample(5), sample(5))["swap_io_mib_s"])


class Decisions(unittest.TestCase):
    def heavy(self, m, state=None, now=1000.0):
        return ma.decide("heavy", m, state, now)

    def test_healthy_post_reboot_machine_admits_both_weights(self):
        m = metrics(swap={"total": 0, "used": 0, "free": 0})
        self.assertTrue(self.heavy(m)[0]["admit"])
        self.assertTrue(ma.decide("light", m, None, 1.0)[0]["admit"])

    def test_historical_swap_used_alone_is_not_exhaustion(self):
        # The pre-reboot shape: swap 99% used, but calm now. The old "swap free < 4 GiB" rule refused this; this tool must not.
        m = metrics(level=1, io=0.5, compressor=0.10, load=3.0, swap={"total": 69632 * 1024 ** 2, "used": int(69000 * 1024 ** 2), "free": 630 * 1024 ** 2})
        v, _ = self.heavy(m)
        self.assertTrue(v["admit"], v)
        self.assertTrue(any("historical" in w for w in v["warnings"]))

    def test_swap_nearly_full_and_cannot_grow_refuses_heavy_but_not_light(self):
        sw = {"total": 69632 * 1024 ** 2, "used": int(69000 * 1024 ** 2), "free": 630 * 1024 ** 2}
        m = metrics(swap=sw, disk=60 * GIB)
        v, _ = self.heavy(m)
        self.assertFalse(v["admit"])
        self.assertTrue(any("cannot grow" in r for r in v["reasons"]))
        self.assertTrue(ma.decide("light", m, None, 1.0)[0]["admit"])
        m_ok = metrics(swap=sw, disk=494 * GIB)  # same swap, plenty of disk: swap can grow
        self.assertTrue(self.heavy(m_ok)[0]["admit"])

    def test_pressure_levels(self):
        self.assertFalse(self.heavy(metrics(level=2))[0]["admit"])      # warn: heavy refused
        self.assertTrue(ma.decide("light", metrics(level=2), None, 1.0)[0]["admit"])
        self.assertTrue(any("warn level" in w for w in ma.decide("light", metrics(level=2), None, 1.0)[0]["warnings"]))
        self.assertFalse(ma.decide("light", metrics(level=4), None, 1.0)[0]["admit"])   # critical: nothing is admitted
        self.assertFalse(self.heavy(metrics(level=4))[0]["admit"])

    def test_sustained_swap_io(self):
        self.assertFalse(self.heavy(metrics(io=20.0))[0]["admit"])
        self.assertTrue(ma.decide("light", metrics(io=20.0), None, 1.0)[0]["admit"])
        self.assertFalse(ma.decide("light", metrics(io=200.0), None, 1.0)[0]["admit"])
        self.assertTrue(self.heavy(metrics(io=2.0))[0]["admit"])

    def test_compressor_share_and_load(self):
        self.assertFalse(self.heavy(metrics(compressor=0.56))[0]["admit"])     # the measured pre-reboot value
        self.assertTrue(ma.decide("light", metrics(compressor=0.40), None, 1.0)[0]["admit"])
        self.assertFalse(ma.decide("light", metrics(compressor=0.56), None, 1.0)[0]["admit"])
        self.assertFalse(self.heavy(metrics(load=129.0))[0]["admit"])          # load average 129 on 16 cores
        self.assertTrue(ma.decide("light", metrics(load=129.0), None, 1.0)[0]["admit"])

    def test_one_heavy_lane_other_build_process_blocks_heavy_only(self):
        m = metrics(heavy={"cargo": 1})
        v, _ = self.heavy(m)
        self.assertFalse(v["admit"])
        self.assertIn("cargo x1", " ".join(v["reasons"]))
        self.assertTrue(ma.decide("light", m, None, 1.0)[0]["admit"])

    def test_unknown_data_fails_closed_for_heavy_and_warns_for_light(self):
        a = sample(0)
        b = sample(10)
        b["pressure_level"] = None
        b["vm"] = None
        m = ma.derive(a, b)
        v, st = self.heavy(m)
        self.assertFalse(v["admit"])
        self.assertTrue(st["latched"])
        lv, _ = ma.decide("light", m, None, 1.0)
        self.assertTrue(lv["admit"])
        self.assertTrue(any(w.startswith("unknown") for w in lv["warnings"]))


    def test_each_unknown_input_alone_fails_closed_for_heavy(self):
        for field in ("pressure_level", "vm", "load1", "cpus"):
            b = sample(10)
            b[field] = None
            v, _ = self.heavy(ma.derive(sample(0), b))
            self.assertFalse(v["admit"], field)
            self.assertTrue(any("fails closed" in r for r in v["reasons"]), field)


class Hysteresis(unittest.TestCase):
    def good(self):
        return metrics(level=1, io=0.0, compressor=0.05, load=2.0)

    def test_recovery_needs_consecutive_spaced_good_checks(self):
        _, st = ma.decide("heavy", metrics(level=4), None, 0.0)
        self.assertTrue(st["latched"])
        v, st = ma.decide("heavy", self.good(), st, 100.0)
        self.assertFalse(v["admit"])
        self.assertIn("recovering (1/3", v["reasons"][0])
        v, st = ma.decide("heavy", self.good(), st, 105.0)       # too close: does not count
        self.assertEqual(st["ok_streak"], 1)
        v, st = ma.decide("heavy", self.good(), st, 140.0)
        v, st = ma.decide("heavy", self.good(), st, 180.0)
        self.assertTrue(v["admit"], v)
        self.assertFalse(st["latched"])

    def test_a_bad_sample_resets_the_streak(self):
        _, st = ma.decide("heavy", metrics(level=4), None, 0.0)
        _, st = ma.decide("heavy", self.good(), st, 100.0)
        _, st = ma.decide("heavy", self.good(), st, 140.0)
        self.assertEqual(st["ok_streak"], 2)
        _, st = ma.decide("heavy", metrics(level=2), st, 180.0)
        self.assertEqual(st["ok_streak"], 0)
        self.assertTrue(st["latched"])

    def test_exit_thresholds_are_stricter_than_enter(self):
        # 8 MiB/s is below the 16 MiB/s entry threshold (admitted when never latched) but above the 4 MiB/s exit threshold.
        m = metrics(io=8.0)
        self.assertTrue(ma.decide("heavy", m, None, 1.0)[0]["admit"])
        _, st = ma.decide("heavy", metrics(level=4), None, 0.0)
        v, st = ma.decide("heavy", m, st, 100.0)
        self.assertFalse(v["admit"])
        self.assertEqual(st["ok_streak"], 0)

    def test_light_work_has_no_latch(self):
        _, st = ma.decide("light", metrics(level=4), None, 0.0)
        self.assertFalse(st["latched"])
        self.assertTrue(ma.decide("light", self.good(), st, 1.0)[0]["admit"])

    def test_pressure_trend_warning(self):
        _, st = ma.decide("light", metrics(level=1), None, 0.0)
        v, _ = ma.decide("light", metrics(level=2), st, 10.0)
        self.assertTrue(any("rising" in w for w in v["warnings"]))


class StateFile(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ma-test-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.path = self.d / "sub" / "state.json"

    def test_round_trip_is_atomic_and_private(self):
        ma.save_state(self.path, {"latched": True, "ok_streak": 2, "last_ok": 5.0, "levels": [1, 2]})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        st, problem = ma.load_state(self.path)
        self.assertEqual((st["latched"], st["ok_streak"], problem), (True, 2, None))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_missing_state_is_a_clean_start(self):
        self.assertEqual(ma.load_state(self.path), ({}, None))

    def test_corrupt_state_latches_heavy_with_a_reason(self):
        self.path.parent.mkdir()
        for bad in ("not json", "[]", json.dumps({"latched": "yes", "ok_streak": 1, "last_ok": 0}), json.dumps({"latched": False, "ok_streak": 99999, "last_ok": 0})):
            self.path.write_text(bad)
            st, problem = ma.load_state(self.path)
            self.assertTrue(st["latched"], bad)
            self.assertIn("corrupt", problem)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ma-cli-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.state = str(self.d / "state.json")

    def run_cli(self, argv, level=1, mibs=0.0, disk=(True, "heavy admitted in tier wide")):
        calls = iter([sample(0.0, level=level), sample(10.0, level=level, swapins=int(mibs * 1024 ** 2 * 10 / PAGE))])
        deps = {"sample": lambda: next(calls), "sleep": lambda s: None, "now": lambda: 1000.0, "disk": lambda w: disk}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ma.main(argv, deps)
        return rc, buf.getvalue()

    def test_admit_and_refuse_exit_codes(self):
        rc, out = self.run_cli(["check", "--weight", "heavy", "--state", self.state, "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["admit"])
        rc, out = self.run_cli(["check", "--weight", "heavy", "--state", self.state, "--json"], level=4)
        self.assertEqual(rc, 77)
        self.assertFalse(json.loads(out)["admit"])

    def test_disk_gate_refusal_wins_even_when_memory_is_healthy(self):
        rc, out = self.run_cli(["check", "--weight", "heavy", "--state", self.state, "--json"], disk=(False, "heavy refused in tier low"))
        d = json.loads(out)
        self.assertEqual(rc, 77)
        self.assertTrue(any(r.startswith("disk gate") for r in d["reasons"]))

    def test_memory_refusal_wins_even_when_disk_is_healthy(self):
        rc, out = self.run_cli(["check", "--weight", "heavy", "--state", self.state, "--json"], level=2)
        self.assertEqual(rc, 77)
        self.assertTrue(json.loads(out)["disk_gate"]["admit"])

    def test_corrupt_state_file_refuses_heavy_through_the_cli(self):
        pathlib.Path(self.state).write_text("garbage")
        rc, out = self.run_cli(["check", "--weight", "heavy", "--state", self.state, "--json"])
        self.assertEqual(rc, 77)
        self.assertTrue(any("corrupt" in r for r in json.loads(out)["reasons"]))

    def test_status_never_refuses_writes_no_state_and_skips_the_disk_gate(self):
        rc, out = self.run_cli(["status", "--state", self.state, "--json"], level=4, disk=(False, "must not be asked"))
        d = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertNotIn("disk_gate", d)
        self.assertFalse(pathlib.Path(self.state).exists())

    def test_light_check_does_not_touch_the_heavy_state(self):
        rc, _ = self.run_cli(["check", "--weight", "light", "--state", self.state, "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(pathlib.Path(self.state).exists())

    def test_disk_gate_helper_fails_closed(self):
        ok, msg = ma.disk_gate("heavy", run=lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        self.assertFalse(ok)
        self.assertIn("unavailable", msg)
        ok, _ = ma.disk_gate("heavy", run=lambda *a, **k: type("R", (), {"returncode": 77, "stdout": "heavy refused", "stderr": ""})())
        self.assertFalse(ok)

    def test_window_is_clamped(self):
        seen = []
        calls = iter([sample(0.0), sample(10.0)])
        with redirect_stdout(io.StringIO()):
            ma.main(["status", "--window", "100000", "--json"], {"sample": lambda: next(calls), "sleep": seen.append, "now": lambda: 1.0, "disk": lambda w: (True, "")})
        self.assertEqual(seen, [120.0])


class ReviewHardening(unittest.TestCase):
    """Each case is a concrete failure from the independent review of PR #99."""

    def heavy(self, m, state=None, now=1000.0):
        return ma.decide("heavy", m, state, now)

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ma-hard-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_unreadable_process_list_is_unknown_not_no_heavy_process(self):
        def run(cmd, **kw):
            if os.path.basename(cmd[0]) == "ps":
                raise __import__("subprocess").TimeoutExpired(cmd, 20)
            return type("R", (), {"returncode": 0, "stdout": VM_STAT if "vm_stat" in cmd[0] else "1\n"})()
        s = ma.take_sample(run=run, now_fn=lambda: 1.0, mono_fn=lambda: 1.0, load_fn=lambda: (1.0, 1.0, 1.0), cpu_fn=lambda: 16, disk_free_fn=lambda: 494 * GIB)
        self.assertIsNone(s["heavy_procs"])
        m = ma.derive(sample(0), dict(sample(10), heavy_procs=None))
        v, st = self.heavy(m)
        self.assertFalse(v["admit"])
        self.assertTrue(any("process list unreadable" in r for r in v["reasons"]))
        self.assertTrue(ma.decide("light", m, None, 1.0)[0]["admit"])

    def test_empty_ps_output_is_unknown_not_nothing_running(self):
        def run(cmd, **kw):
            out = {"ps": "", "vm_stat": VM_STAT}.get(os.path.basename(cmd[0]), "1\n")
            return type("R", (), {"returncode": 0, "stdout": out})()
        s = ma.take_sample(run=run, now_fn=lambda: 1.0, mono_fn=lambda: 1.0, load_fn=lambda: (1.0, 1.0, 1.0), cpu_fn=lambda: 16, disk_free_fn=lambda: GIB)
        self.assertIsNone(s["heavy_procs"])

    def test_an_out_of_range_pressure_level_is_never_saved_into_state(self):
        _, st = ma.decide("heavy", metrics(level=0), None, 0.0)
        self.assertEqual(st["levels"], [])
        self.assertTrue(st["latched"])

    def test_each_remaining_unknown_input_alone_fails_closed_for_heavy(self):
        for field in ("swap", "disk_free"):
            b = sample(10)
            b[field] = None
            v, _ = self.heavy(ma.derive(sample(0), b))
            self.assertFalse(v["admit"], field)
            self.assertTrue(any("fails closed" in r for r in v["reasons"]), field)

    def test_pressure_level_outside_what_the_kernel_reports_is_unknown(self):
        for bad in (0, -1, 3, 5, 99):
            v, _ = self.heavy(metrics(level=bad))
            self.assertFalse(v["admit"], bad)
        v, _ = ma.decide("light", metrics(level=0), None, 1.0)
        self.assertTrue(v["admit"])
        self.assertTrue(any(w.startswith("unknown") for w in v["warnings"]))

    def test_tampered_streak_cannot_skip_hysteresis(self):
        v, st = self.heavy(metrics(level=1), {"latched": True, "ok_streak": 100, "last_ok": 0.0, "levels": []}, now=1000.0)
        self.assertFalse(v["admit"])
        self.assertTrue(st["latched"])

    def test_future_or_nonfinite_last_ok_restarts_the_count_and_still_recovers(self):
        for bad in (1e18, float("nan"), float("inf"), True, "later"):
            _, st = ma.decide("heavy", metrics(level=4), None, 0.0)
            st.update(ok_streak=2, last_ok=bad)
            v, st = self.heavy(metrics(level=1), st, now=1000.0)
            self.assertFalse(v["admit"], bad)
            self.assertIn("recovering (1/3", v["reasons"][0], bad)
            v, st = self.heavy(metrics(level=1), st, now=1040.0)
            v, st = self.heavy(metrics(level=1), st, now=1080.0)
            self.assertTrue(v["admit"], bad)

    def test_garbage_levels_cannot_crash_decide(self):
        v, st = self.heavy(metrics(level=1), {"latched": False, "ok_streak": 0, "last_ok": 0.0, "levels": [{}, [], "x", 7, True]})
        self.assertTrue(v["admit"])
        self.assertEqual(st["levels"], [1])

    def test_swap_io_rate_uses_the_monotonic_clock_not_the_wall_clock(self):
        pages = int(10 * 1024 ** 3 / PAGE)          # a 10 GiB swap burst
        a = sample(0, swapins=0)
        b = sample(10, swapins=pages)
        b["time"] = 1010.0                          # wall clock stepped forward 1000 s during the window
        rate = ma.derive(a, b)["swap_io_mib_s"]
        self.assertGreater(rate, 900)               # 10240 MiB / 10 s, not diluted to ~10 MiB/s
        self.assertFalse(self.heavy(ma.derive(a, b))[0]["admit"])

    def test_load_state_rejects_every_malformed_shape(self):
        path = self.d / "state.json"
        good = {"latched": False, "ok_streak": 0, "last_ok": 0.0, "levels": [1]}
        bad_variants = [dict(good, levels=[{}, []]), dict(good, levels=[1] * 50), dict(good, levels=[3]), dict(good, ok_streak=100),
                        dict(good, ok_streak=-1), dict(good, ok_streak=True), dict(good, last_ok="x")]
        for v in bad_variants:
            path.write_text(json.dumps(v))
            st, problem = ma.load_state(path)
            self.assertTrue(st["latched"], v)
            self.assertIn("corrupt", problem)
        path.write_text('{"latched": false, "ok_streak": 0, "last_ok": NaN, "levels": []}')
        self.assertTrue(ma.load_state(path)[0]["latched"])
        path.write_text(json.dumps(good))
        self.assertEqual(ma.load_state(path), (good, None))
        path.write_text("x" * (ma.MAX_STATE_BYTES + 5))
        self.assertIn("too large", ma.load_state(path)[1])

    def test_fifo_and_symlink_state_paths_are_refused_without_hanging(self):
        fifo = self.d / "fifo.json"
        os.mkfifo(fifo)
        st, problem = ma.load_state(fifo)
        self.assertTrue(st["latched"])
        self.assertIn("not a regular file", problem)
        target = self.d / "real.json"
        target.write_text(json.dumps({"latched": False, "ok_streak": 0, "last_ok": 0.0, "levels": []}))
        link = self.d / "link.json"
        os.symlink(target, link)
        st, problem = ma.load_state(link)
        self.assertTrue(st["latched"])
        self.assertIn("unreadable", problem)

    def test_a_held_lock_refuses_heavy_instead_of_racing(self):
        path = self.d / "s" / "state.json"
        with ma.state_lock(path):
            with self.assertRaises(OSError) as cm:
                with ma.state_lock(path, wait=0.3, sleep=lambda s: None):
                    pass
            self.assertIn("busy", str(cm.exception))
        calls = iter([sample(0.0), sample(10.0)])
        def busy(p):
            raise OSError("state busy: another check holds the lock")
        deps = {"sample": lambda: next(calls), "sleep": lambda s: None, "now": lambda: 1000.0, "disk": lambda w: (True, "ok"), "lock": busy}
        v = ma.assess("heavy", 1.0, str(path), False, deps)
        self.assertFalse(v["admit"])
        self.assertTrue(any("busy" in r for r in v["reasons"]))

    def test_unwritable_state_refuses_heavy_instead_of_forgetting_the_latch(self):
        from unittest import mock
        calls = iter([sample(0.0), sample(10.0)])
        deps = {"sample": lambda: next(calls), "sleep": lambda s: None, "now": lambda: 1000.0, "disk": lambda w: (True, "ok"), "lock": ma.state_lock}
        with mock.patch.object(ma, "save_state", side_effect=OSError(30, "Read-only file system")):
            v = ma.assess("heavy", 1.0, str(self.d / "state.json"), False, deps)
        self.assertFalse(v["admit"])
        self.assertTrue(any("state unwritable" in r for r in v["reasons"]))

    def test_malformed_numbers_and_nan_window_do_not_crash(self):
        self.assertIsNone(ma.parse_swapusage("total = 1.2.3M  used = 1M  free = 1M"))
        seen = []
        calls = iter([sample(0.0), sample(10.0)])
        with redirect_stdout(io.StringIO()):
            ma.main(["status", "--window", "nan", "--json"], {"sample": lambda: next(calls), "sleep": seen.append, "now": lambda: 1.0, "disk": lambda w: (True, "")})
        self.assertEqual(seen, [10.0])


class IdleOrphan(unittest.TestCase):
    """SPE-134: a heavy lane is live work, not a process name. Idle orphans are reported, never counted."""

    ORPHAN = ps_line(25830, 1, "S", "3-22:10:05", "0:10.00", "0.0", "/Users/x/.rustup/bin/rustc")

    def take(self, ps, lsof="p25830\nn/work/tree\n", calls=None):
        def run(cmd, **kw):
            if calls is not None:
                calls.append(list(cmd))
            name = os.path.basename(cmd[0])
            if name == "lsof" and lsof is None:
                raise OSError("no lsof")
            out = {"ps": ps, "lsof": lsof, "vm_stat": VM_STAT,
                   "sysctl": {"hw.memsize": str(RAM), "kern.memorystatus_vm_pressure_level": "1", "vm.swapusage": SWAP}.get(cmd[-1], "")}[name]
            return type("R", (), {"returncode": 0, "stdout": out})()
        return ma.take_sample(run=run, now_fn=lambda: 5.0, mono_fn=lambda: 5.0, load_fn=lambda: (1.0, 1.0, 1.0), cpu_fn=lambda: 16, disk_free_fn=lambda: GIB)

    def test_idle_orphan_is_reported_with_pid_age_and_cwd_and_not_counted(self):
        s = self.take(ps_out(self.ORPHAN, ps_line(1, 0, "Ss", "9-00:00:00", "1:00.00", "0.0", "/sbin/launchd")))
        self.assertEqual(s["heavy_procs"], {})
        (o,) = s["idle_procs"]
        self.assertEqual((o["pid"], o["comm"], o["ppid"]), (25830, "rustc", 1))
        self.assertEqual(o["age_s"], 3 * 86400 + 22 * 3600 + 10 * 60 + 5)
        self.assertEqual(o["cwd"], "/work/tree")

    def test_a_process_with_a_live_parent_counts_even_when_quiet(self):
        s = self.take(ps_out(ps_line(40, 39, "S", "02:00:00", "0:01.00", "0.0", "/x/rustc")))
        self.assertEqual((s["heavy_procs"], s["idle_procs"]), ({"rustc": 1}, []))

    def test_orphan_that_is_running_busy_young_or_uninterruptible_still_counts(self):
        for line in (ps_line(41, 1, "R", "3-00:00:00", "0:10.00", "0.0", "/x/rustc"),
                     ps_line(42, 1, "S", "3-00:00:00", "0:10.00", "40.0", "/x/rustc"),
                     ps_line(43, 1, "S", "00:30", "0:00.10", "0.0", "/x/rustc"),
                     ps_line(44, 1, "U", "3-00:00:00", "0:10.00", "0.0", "/x/rustc")):
            s = self.take(ps_out(line))
            self.assertEqual(s["heavy_procs"], {"rustc": 1}, line)
            self.assertEqual(s["idle_procs"], [], line)

    def test_unparseable_heavy_line_fails_closed_as_active(self):
        s = self.take(ps_out("garbage that still ends in /x/y/rustc", ps_line(45, 1, "S", "bad", "0:10.00", "0.0", "/x/rustc")))
        self.assertEqual(s["heavy_procs"], {"rustc": 2})

    def test_unreadable_process_list_stays_unknown(self):
        self.assertIsNone(self.take("")["heavy_procs"])

    def test_no_command_line_is_ever_requested(self):
        calls = []
        self.take(ps_out(self.ORPHAN), calls=calls)
        for cmd in calls:
            joined = " ".join(cmd)
            for forbidden in ("args", "command", "-ww", "ucomm", "-Eo"):
                self.assertNotIn(forbidden, joined.replace("comm=", ""), joined)

    def test_cwd_lookup_failure_only_drops_the_cwd(self):
        (o,) = self.take(ps_out(self.ORPHAN), lsof=None)["idle_procs"]
        self.assertEqual((o["pid"], o["cwd"]), (25830, None))

    def test_idle_orphan_alone_does_not_refuse_but_is_reported_with_its_owner_hint(self):
        idle = [{"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 340000, "cpu_s": 10.0, "cwd": "/work/tree"}]
        m = ma.derive(sample(0, idle=idle), sample(10, idle=idle))
        verdict, _ = ma.decide("heavy", m, None, 1000.0)
        self.assertTrue(verdict["admit"], verdict)
        note = " ".join(verdict["warnings"])
        self.assertIn("25830", note)
        self.assertIn("/work/tree", note)
        self.assertIn("3d", note)

    def test_active_lane_and_real_load_still_refuse_next_to_an_idle_orphan(self):
        idle = [{"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 340000, "cpu_s": 10.0, "cwd": None}]
        m = ma.derive(sample(0, idle=idle, heavy={"cargo": 1}), sample(10, idle=idle, heavy={"cargo": 1}))
        verdict, _ = ma.decide("heavy", m, None, 1000.0)
        self.assertFalse(verdict["admit"])
        self.assertIn("one heavy lane at a time", " ".join(verdict["reasons"]))
        loaded = ma.derive(sample(0, idle=idle, load=100.0), sample(10, idle=idle, load=100.0))
        self.assertFalse(ma.decide("heavy", loaded, None, 1000.0)[0]["admit"])

    def test_an_orphan_that_used_cpu_during_the_window_is_reclassified_active(self):
        first = [{"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 340000, "cpu_s": 10.0, "cwd": None}]
        second = [dict(first[0], cpu_s=12.5)]
        m = ma.derive(sample(0, idle=first), sample(10, idle=second))
        self.assertEqual(m["heavy_procs"], {"rustc": 1})
        self.assertEqual(m["idle_procs"], [])

    def test_unreadable_idle_data_does_not_hide_an_unknown_process_list(self):
        m = ma.derive(sample(0), dict(sample(10), heavy_procs=None))
        self.assertFalse(ma.decide("heavy", m, None, 1000.0)[0]["admit"])

    def test_any_cpu_spent_in_the_window_makes_an_orphan_active(self):
        first = [{"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 3600, "cpu_s": 10.0, "cwd": None}]
        m = ma.derive(sample(0, idle=first), sample(10, idle=[dict(first[0], cpu_s=10.01)]))
        self.assertEqual((m["heavy_procs"], m["idle_procs"]), ({"rustc": 1}, []))

    def test_cpu_seen_only_in_the_first_sample_still_counts(self):
        rec = {"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 3600, "cpu_s": 12.5, "cwd": None}
        busy_first = sample(0, heavy={"rustc": 1}, cpu_by_pid={25830: 10.0})   # active then (pcpu 2.0), quiet now
        m = ma.derive(busy_first, sample(10, idle=[rec], cpu_by_pid={25830: 12.5}))
        self.assertEqual((m["heavy_procs"], m["idle_procs"]), ({"rustc": 1}, []))
        verdict, _ = ma.decide("heavy", m, None, 1000.0)
        self.assertFalse(verdict["admit"])

    def test_an_orphan_missing_from_the_first_sample_cannot_be_proven_idle(self):
        rec = {"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 3600, "cpu_s": 10.0, "cwd": None}
        m = ma.derive(sample(0), sample(10, idle=[rec], cpu_by_pid={25830: 10.0}))
        self.assertEqual(m["heavy_procs"], {"rustc": 1})

    def test_unchanged_cpu_in_both_samples_stays_idle(self):
        rec = {"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 3600, "cpu_s": 10.0, "cwd": None}
        m = ma.derive(sample(0, idle=[rec], cpu_by_pid={25830: 10.0}), sample(10, idle=[rec], cpu_by_pid={25830: 10.0}))
        self.assertEqual((m["heavy_procs"], len(m["idle_procs"])), ({}, 1))

    def test_busy_in_the_first_sample_and_quiet_in_the_second_with_the_same_cpu_stays_active(self):
        rec = {"pid": 25830, "comm": "rustc", "ppid": 1, "age_s": 3600, "cpu_s": 10.0, "cwd": None}
        first = sample(0, heavy={"rustc": 1}, cpu_by_pid={25830: 10.0}, active_pids=[25830])   # %cpu 2.0 then
        m = ma.derive(first, sample(10, idle=[rec], cpu_by_pid={25830: 10.0}))                  # %cpu 0.0 and unchanged CPU time now
        self.assertEqual((m["heavy_procs"], m["idle_procs"]), ({"rustc": 1}, []))

    def test_take_sample_records_which_pids_it_counted_as_active(self):
        s = self.take(ps_out(ps_line(41, 1, "R", "3-00:00:00", "0:10.00", "0.0", "/x/rustc"), self.ORPHAN))
        self.assertEqual(s["active_pids"], [41])

    def test_an_unreadable_first_process_list_is_not_forgotten_by_a_readable_second(self):
        first = dict(sample(0), heavy_procs=None, idle_procs=[], cpu_by_pid={})
        m = ma.derive(first, sample(10))
        self.assertIsNone(m["heavy_procs"])
        verdict, _ = ma.decide("heavy", m, None, 1000.0)
        self.assertFalse(verdict["admit"])
        self.assertIn("process list unreadable", " ".join(verdict["reasons"]))

    def test_a_truncated_or_malformed_process_row_fails_closed(self):
        for bad in ("25830 1 S", "  7    "):
            s = self.take(ps_out(ps_line(10, 500, "S", "05:00", "0:01.00", "0.0", "/bin/zsh"), bad))
            self.assertIsNone(s["heavy_procs"], bad)                  # cannot tell what it was: the list is unknown
        s = self.take(ps_out("abc 1 S 05:00 0:01.00 0.0 /x/rustc"))   # unreadable pid but a heavy name: counted as a lane
        self.assertEqual(s["heavy_procs"], {"rustc": 1})
        ok = self.take(ps_out(ps_line(10, 500, "S", "05:00", "0:01.00", "0.0", "/bin/zsh")))
        self.assertEqual(ok["heavy_procs"], {})                      # positive control: well-formed rows stay readable

    def test_a_row_with_unreadable_fields_is_unknown_even_when_its_name_is_not_heavy(self):
        good = ps_line(10, 500, "S", "05:00", "0:01.00", "0.0", "/bin/zsh")
        for bad in (ps_line(11, 500, "S", "garbage", "0:01.00", "0.0", "/bin/zsh"),
                    ps_line(11, 500, "S", "05:00", "nope", "0.0", "/bin/zsh"),
                    ps_line(11, 500, "S", "05:00", "0:01.00", "x.y", "/bin/zsh"),
                    "abc def S 05:00 0:01.00 0.0 /bin/zsh"):
            self.assertIsNone(self.take(ps_out(good, bad))["heavy_procs"], bad)
        self.assertEqual(self.take(ps_out(good))["heavy_procs"], {})


if __name__ == "__main__":
    unittest.main()
