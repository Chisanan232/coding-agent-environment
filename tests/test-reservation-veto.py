#!/usr/bin/env python3
"""Reservation-aware heavy-lane veto in memory-admission (SPE-150 PR5): same outcomes at cap 1, the reasons name the budget."""
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = spec_from_file_location(name, ROOT / file)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ma = load("ma_veto", "scripts/memory-admission.py")
da = load("da_veto", "scripts/disk-admission.py")
GIB, PAGE, RAM = 1024 ** 3, 16384, 128 * 1024 ** 3
OLD_TEXT = "1 heavy build workload(s) running (cargo: 5 procs, 5.0 GiB): heavy lanes full (1 of 1)"


def tree(root=100, comm="cargo", procs=5, rss=5 * GIB, idle=False, members=None, ancestors=None):
    return {"root_pid": root, "root_comm": comm, "procs": procs, "rss_bytes": rss, "cpu_s": 1.0, "idle": idle,
            "member_cpu": {p: 1.0 for p in (members or [root])}, "ancestors": ancestors or []}


def sample(t, workloads=()):
    return {"time": t, "mono": t, "ram": RAM, "pressure_level": 1, "swap": {"total": 0, "used": 0, "free": 0},
            "vm": {"page_size": PAGE, "free": 1, "swapins": 0, "swapouts": 0, "compressor": 0},
            "load1": 1.0, "cpus": 16, "disk_free": 400 * GIB, "heavy_procs": {"cargo": len(workloads)} if workloads else {},
            "idle_procs": [], "cpu_by_pid": {}, "active_pids": [], "heavy_workloads": list(workloads)}


def metrics(workloads, owned=None):
    m = ma.derive(sample(0, workloads), sample(10, workloads))
    m["owned"] = owned
    return m


def owned(label="nda-x", pid=50, members=(), mem=6 * GIB, cls="build-crate"):
    return {"label": label, "pid": pid, "members": list(members), "mem_bytes": mem, "class": cls}


class Occupancy(unittest.TestCase):
    def test_wrapper_ancestor_member_and_recorded_member_all_own_a_tree(self):
        running = [tree(root=100, members=[100, 101], ancestors=[50, 7])]
        for rec in (owned(pid=50), owned(pid=101), owned(pid=999, members=[100]), owned(pid=7)):
            occ = ma.lane_occupancy(metrics(running), [rec])
            self.assertEqual((occ["in_use"], occ["workloads"][0]["label"]), (1, "nda-x"), rec)

    def test_an_unrelated_reservation_leaves_the_tree_unmanaged(self):
        occ = ma.lane_occupancy(metrics([tree(ancestors=[7])]), [owned(pid=51)])
        self.assertIsNone(occ["workloads"][0]["label"])

    def test_idle_trees_hold_no_lane(self):
        self.assertEqual(ma.lane_occupancy(metrics([tree(idle=True)]))["in_use"], 0)

    def test_the_fallback_cap_is_one_and_the_derived_cap_follows_the_disk_gate(self):
        self.assertEqual(ma.HEAVY_LANE_CAP, 1)
        self.assertEqual(ma.read_lane_cap(), max(da.heavy_lane_cap(tier) for tier in da.HEAVY_LANES))
        self.assertEqual(metrics([tree()], [owned()]).get("lane_cap"), None)   # the cap is attached by assess(), not by derive()

    def test_workloads_carry_their_ancestors_from_the_process_list(self):
        ps = ("   50      1 S    1-00:00:00    0:10.00   0.0   100 /bin/zsh\n"
              "  100     50 R    0-00:10:00    0:10.00  50.0  5120 /x/cargo\n"
              "  101    100 R    0-00:10:00    0:10.00  50.0  5120 /x/rustc\n")
        w = ma.classify_heavy(ps)[4][0]
        self.assertEqual((w["root_pid"], w["ancestors"]), (100, [50]))


class VetoText(unittest.TestCase):
    def reasons(self, running, rec=None):
        return ma.refusal_reasons("heavy", metrics(running, rec))[0]

    def test_unmanaged_veto_names_the_lane_budget_and_keeps_the_old_sentence(self):
        (text,) = self.reasons([tree()])
        self.assertTrue(text.startswith(OLD_TEXT))
        self.assertIn("lane budget: 1 of 1 heavy lane(s) in use, 5.0 GiB resident", text)
        self.assertIn("unmanaged cargo (5.0 GiB resident, no reservation accounts for it)", text)

    def test_reserved_veto_names_the_reservation_and_its_estimate(self):
        (text,) = self.reasons([tree(ancestors=[50])], [owned()])
        self.assertTrue(text.startswith(OLD_TEXT))
        self.assertIn("reserved nda-x (cargo, 5.0 GiB resident, est 6.0 GiB)", text)
        (legacy,) = self.reasons([tree(ancestors=[50])], [owned(mem=None, cls=None)])
        self.assertIn("legacy: no estimate", legacy)

    def test_two_trees_report_two_lanes_in_use(self):
        (text,) = self.reasons([tree(100), tree(200, procs=2, rss=GIB)], [owned()])
        self.assertIn("2 heavy build workload(s) running", text)
        self.assertIn("2 of 1 heavy lane(s) in use, 6.0 GiB resident", text)


class CapOneUnchanged(unittest.TestCase):
    def test_owned_and_unmanaged_trees_refuse_and_latch_the_same_way(self):
        for rec in (None, [], [owned()], [owned(pid=999)]):
            m = metrics([tree(ancestors=[50])], rec)
            verdict, state = ma.decide("heavy", m, None, 1000.0)
            self.assertFalse(verdict["admit"])
            self.assertTrue(state["latched"])
            self.assertEqual(len(verdict["reasons"]), 1)
            self.assertTrue(verdict["reasons"][0].startswith(OLD_TEXT))

    def test_no_running_tree_admits_exactly_as_before(self):
        verdict, state = ma.decide("heavy", metrics([]), None, 1000.0)
        self.assertEqual((verdict["admit"], verdict["reasons"], state["latched"]), (True, [], False))

    def test_the_hysteresis_after_a_tree_ends_is_unchanged(self):
        state = None
        _, state = ma.decide("heavy", metrics([tree()]), state, 1000.0)
        verdict, state = ma.decide("heavy", metrics([]), state, 1001.0)
        self.assertFalse(verdict["admit"])
        self.assertIn("hysteresis: recovering (1/3", verdict["reasons"][0])

    def test_the_light_weight_is_untouched(self):
        verdict, _ = ma.decide("light", metrics([tree()]), None, 1000.0)
        self.assertTrue(verdict["admit"])


class OccupancyIsArithmeticNotAName(unittest.TestCase):
    """What a future cap above 1 changes: occupancy below the cap is no veto and no latch."""

    def setUp(self):
        self.cap = ma.HEAVY_LANE_CAP
        self.addCleanup(setattr, ma, "HEAVY_LANE_CAP", self.cap)
        ma.HEAVY_LANE_CAP = 2

    def test_one_tree_under_a_cap_of_two_neither_refuses_nor_latches(self):
        verdict, state = ma.decide("heavy", metrics([tree()], [owned()]), None, 1000.0)
        self.assertTrue(verdict["admit"])
        self.assertFalse(state["latched"])

    def test_two_trees_fill_a_cap_of_two(self):
        (text,) = ma.refusal_reasons("heavy", metrics([tree(100), tree(200)]))[0]
        self.assertIn("2 of 2 heavy lane(s) in use", text)


class DerivedCap(unittest.TestCase):
    """The memory gate takes its lane cap from disk-admission.py and falls back to one lane when it cannot."""

    def fake_disk(self, tmp, body):
        (pathlib.Path(tmp) / "disk-admission.py").write_text(body)
        original = ma.HERE
        ma.HERE = pathlib.Path(tmp)
        self.addCleanup(setattr, ma, "HERE", original)

    def test_a_cap_of_two_allows_one_tree_and_refuses_at_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fake_disk(tmp, "HEAVY_LANES = {'stable': 2, 'wide': 2}\ndef heavy_lane_cap(tier):\n    return HEAVY_LANES[tier]\n")
            self.assertEqual(ma.read_lane_cap(), 2)
            one = metrics([tree()], [owned()])
            one["lane_cap"] = ma.read_lane_cap()
            self.assertEqual(ma.refusal_reasons("heavy", one)[0], [])
            two = metrics([tree(100), tree(200)], [owned()])
            two["lane_cap"] = ma.read_lane_cap()
            (text,) = ma.refusal_reasons("heavy", two)[0]
            self.assertIn("heavy lanes full (2 of 2)", text)
            self.assertIn("2 of 2 heavy lane(s) in use", text)

    def test_any_failure_to_load_or_a_nonsense_cap_falls_back_to_one_lane(self):
        for body in ("raise RuntimeError('broken')", "HEAVY_LANES = {'wide': 0}\ndef heavy_lane_cap(tier):\n    return 0\n",
                     "HEAVY_LANES = {'wide': 1}\ndef heavy_lane_cap(tier):\n    return 'two'\n", "syntax error here"):
            with tempfile.TemporaryDirectory() as tmp:
                self.fake_disk(tmp, body)
                self.assertEqual(ma.read_lane_cap(), 1, body)
        ma.HERE = pathlib.Path("/nonexistent-dir-for-spe-160")
        self.assertEqual(ma.read_lane_cap(), 1)

    def test_assess_attaches_the_derived_cap_and_an_injected_one_wins(self):
        for injected, expected in ((None, ma.read_lane_cap()), (lambda: 2, 2)):
            seen = []
            real = ma.decide
            ma.decide = lambda weight, m, state, now, _real=real: (seen.append(m.get("lane_cap")), _real(weight, m, state, now))[1]
            self.addCleanup(setattr, ma, "decide", real)
            calls = iter([sample(0, [tree(ancestors=[50])]), sample(10, [tree(ancestors=[50])])])
            deps = {"sample": lambda: next(calls), "sleep": lambda s: None, "now": lambda: 1000.0, "owned": lambda path: [owned()]}
            if injected:
                deps["lane_cap"] = injected
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                ma.main(["check", "--weight", "heavy", "--json", "--memory-only", "--state", str(pathlib.Path(tmp) / "s.json")], deps)
            ma.decide = real
            self.assertEqual(seen, [expected])


class ReadOwned(unittest.TestCase):
    def test_reads_the_sibling_reservation_dir_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            admission = root / "admission/state.json"
            (root / "memory-admission").mkdir()
            admission.parent.mkdir()
            da.write_reservation(admission, "nda-x", 20 * GIB, 4242, "tok", datetime.now(timezone.utc) + timedelta(hours=1),
                                 declared={"class": "build-crate", "mem_bytes": 6 * GIB, "cpu_slots": 4})
            before = sorted(p.name for p in root.rglob("*"))
            got = ma.read_owned(root / "memory-admission/state.json")
            self.assertEqual([(o["label"], o["pid"], o["class"], o["mem_bytes"]) for o in got], [("nda-x", 4242, "build-crate", 6 * GIB)])
            self.assertEqual(sorted(p.name for p in root.rglob("*")), before)

    def test_absent_or_hostile_directory_means_none_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ma.read_owned(pathlib.Path(tmp) / "memory-admission/state.json"), [])
            reservations = pathlib.Path(tmp) / "admission/reservations"
            reservations.mkdir(parents=True)
            (reservations / "bad.json").write_text("{not json")
            self.assertEqual(ma.read_owned(pathlib.Path(tmp) / "memory-admission/state.json"), [])


class EndToEnd(unittest.TestCase):
    def test_check_json_names_the_reservation_and_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = iter([sample(0, [tree(ancestors=[50])]), sample(10, [tree(ancestors=[50])])])
            out = io.StringIO()
            deps = {"sample": lambda: next(calls), "sleep": lambda s: None, "now": lambda: 1000.0,
                    "owned": lambda path: [owned()], "disk": lambda w: (True, "ok")}
            with contextlib.redirect_stdout(out):
                rc = ma.main(["check", "--weight", "heavy", "--json", "--memory-only", "--state", str(pathlib.Path(tmp) / "s.json")], deps)
            verdict = json.loads(out.getvalue())
            self.assertEqual(rc, ma.REFUSE_RC)
            self.assertIn("reserved nda-x", " ".join(verdict["reasons"]))


if __name__ == "__main__":
    unittest.main()
