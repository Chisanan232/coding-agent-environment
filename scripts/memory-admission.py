#!/usr/bin/env python3
"""Memory-aware admission precondition for heavy work (SPE-117). Advisory, additive to SPE-109.

  memory-admission.py check  --weight {heavy,light} [--json] [--window SECONDS] [--state FILE] [--memory-only]
  memory-admission.py status [--json] [--window SECONDS]

Exit 0 admits, 77 refuses (same convention as disk-admission.py). `check` runs the EXISTING disk gate
(`disk-admission.py check`, which also owns reservations and heavy-lane caps) AND this memory assessment; both must admit.
It does not change the disk gate, reserves nothing, kills nothing and deletes nothing.

Why not "swap used": macOS keeps swap files allocated long after the pages stopped being touched, and creates none right
after a reboot, so used or free swap says little about CURRENT pressure. This tool uses the kernel pressure level, the
sustained swap I/O rate over a measured window, the compressor's share of RAM, the load per core, other running heavy
processes, and (only to judge whether swap CAN grow) free disk. Thresholds below are PROPOSED values derived from
measurements on this workstation (calm: level 1, 0 MiB/s; stressed but not thrashing: level 2, ~3-6 MiB/s, compressor 56% of RAM,
load/core 8); they need owner approval before any gate adopts them, and they are constants so they stay reviewable.
Heavy admission has hysteresis: after a refusal it stays refused until EXIT_SAMPLES consecutive checks, at least
MIN_SPACING seconds apart, all satisfy the stricter EXIT thresholds. Unknown or corrupt data fails closed for heavy work.
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import time

GIB = 1024 ** 3
MIB = 1024 ** 2
HERE = pathlib.Path(__file__).resolve().parent

# ---- proposed thresholds (see module docstring) --------------------------------------------------------------------
PRESSURE_NORMAL, PRESSURE_WARN, PRESSURE_CRITICAL = 1, 2, 4          # kern.memorystatus_vm_pressure_level
ENTER = {"swap_io_mib_s_heavy": 16.0, "swap_io_mib_s_light": 128.0,   # swapins + swapouts over the window
         "compressor_ratio_heavy": 0.35, "compressor_ratio_light": 0.50,  # compressor bytes / physical RAM
         "load_per_core_heavy": 4.0}
EXIT = {"swap_io_mib_s": 4.0, "compressor_ratio": 0.25, "load_per_core": 2.0}
EXIT_SAMPLES = 3          # consecutive good checks needed to leave a heavy refusal
MIN_SPACING = 30          # seconds between checks that count toward EXIT_SAMPLES
SWAP_FULL_FRACTION = 0.90
SWAP_LOW_FREE = 4 * GIB
SWAP_GROWTH_DISK_FLOOR = 100 * GIB   # same figure as the SPE-109 heavy floor: below it swap cannot be trusted to grow
HEAVY_COMMS = frozenset({"cargo", "rustc", "rust-lld", "cc1", "cc1plus", "clang", "clang++", "ld", "xcodebuild",
                         "swift-build", "swift-frontend", "gradle", "buildkitd"})
# A heavy-named process is a running lane only with evidence of work or ownership. An orphan (parent is launchd) that is
# not runnable, has used almost no CPU recently and has been around a while is a leftover: reported, never counted.
IDLE_MIN_AGE = 900          # seconds
IDLE_PCPU = 1.0             # ps %cpu, a decaying recent average
MAX_IDLE_REPORT = 5
CWD_LOOKUP_TIMEOUT = 4      # seconds per lsof call
CWD_LOOKUP_BUDGET = 8       # seconds for all cwd lookups of one sample
IDLE_STATES = frozenset({"S", "I", "Z"})   # first letter of the ps state; R and U are work, T (stopped) is a deliberate pause that can resume
DEFAULT_STATE = pathlib.Path.home() / ".cache/coding-agent-environment/memory-admission/state.json"
REFUSE_RC = 77
VALID_LEVELS = frozenset({1, 2, 4})   # the only values the kernel reports
SYSCTL, PS, VM_STAT = "/usr/sbin/sysctl", "/bin/ps", "/usr/bin/vm_stat"   # absolute: PATH is not trusted
LSOF = "/usr/sbin/lsof"
PS_FIELDS = "pid=,ppid=,state=,etime=,time=,pcpu=,rss=,comm="   # comm last (it may contain spaces); never args/command: command lines can carry secrets
MAX_STATE_BYTES = 64 * 1024
LOCK_WAIT = 10.0


# ---- parsing ---------------------------------------------------------------------------------------------------------
def parse_swapusage(text):
    """'total = 69632.00M  used = 69001.25M  free = 630.75M  (encrypted)' -> bytes, or None when unparseable."""
    units = {"K": 1024, "M": MIB, "G": GIB}
    out = {}
    for key in ("total", "used", "free"):
        m = re.search(rf"{key}\s*=\s*([\d.]+)\s*([KMG])", text or "")
        if not m:
            return None
        try:
            out[key] = int(float(m.group(1)) * units[m.group(2)])
        except ValueError:
            return None
    return out


def parse_vm_stat(text):
    """Counters in pages plus the page size, or None when the header or a needed counter is missing."""
    m = re.search(r"page size of (\d+) bytes", text or "")
    if not m:
        return None
    out = {"page_size": int(m.group(1))}
    wanted = {"Pages free": "free", "Pages active": "active", "Pages inactive": "inactive", "Pages wired down": "wired",
              "Pages occupied by compressor": "compressor", "Swapins": "swapins", "Swapouts": "swapouts",
              "Decompressions": "decompressions", "Compressions": "compressions"}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip().strip('"') in wanted:
            digits = re.sub(r"[^\d]", "", rest)
            if digits:
                out[wanted[name.strip().strip('"')]] = int(digits)
    needed = {"compressor", "swapins", "swapouts"}
    return out if needed <= out.keys() else None


def _run(cmd, run=subprocess.run, timeout=20):
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def parse_clock(text):
    """ps elapsed/cpu time: [[dd-]hh:]mm:ss[.ss] -> seconds, or None when it is not that shape."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text or "")
    if not m:
        return None
    d, h, mi, sec = m.groups()
    return ((int(d or 0) * 24 + int(h or 0)) * 60 + int(mi)) * 60 + float(sec)


def parse_pcpu(text):
    """ps %cpu as a finite, non-negative float, else None (nan, inf and negatives are not readings)."""
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def classify_heavy(ps_text):
    """(active, idle, cpu_by_pid, active_pids, workloads) from `ps -axo PS_FIELDS`; five Nones when the list is unreadable.
    Anything that cannot be fully parsed counts as active: an unknown process is never treated as an abandoned one.
    cpu_by_pid holds the CPU seconds of every heavy-named process, so a later sample can tell whether any of them worked in
    between. A workload is a heavy-named process with no heavy-named ancestor plus its whole process subtree (test binaries
    and shells included); it is idle only when its root is an idle orphan and every member is idle."""
    unknown = (None, None, None, None, None)
    if ps_text is None or not ps_text.strip():
        return unknown
    active, idle, cpus, active_pids = {}, [], {}, []
    rows, loose = {}, []   # pid -> row for every parsed line; heavy lines that could not be parsed
    for line in ps_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 7)
        parsed = None
        if len(parts) == 8 and parts[0].isdigit() and parts[1].isdigit():
            base = os.path.basename(parts[7].strip())
            rss = int(parts[6]) * 1024 if parts[6].isdigit() else None   # ps reports KiB
            parsed = (int(parts[0]), int(parts[1]), parts[2], parse_clock(parts[3]), parse_clock(parts[4]), parts[5], rss)
        else:
            base = os.path.basename(line.split()[-1])
            if base not in HEAVY_COMMS:
                return unknown   # a row that cannot be read might be a heavy process: the list is unknown
        if base not in HEAVY_COMMS:
            if parsed and (parsed[3] is None or parsed[4] is None or parse_pcpu(parsed[5]) is None or parsed[6] is None):
                return unknown   # fields that cannot be read: the row is not trusted, the list is unknown
            rows.setdefault(parsed[0], {"ppid": parsed[1], "heavy": False, "rss": parsed[6], "cpu": parsed[4],
                                        "quiet": _quiet(parsed), "comm": base})
            continue
        orphan = False
        if parsed:
            pid, ppid, state, age, cpu, pcpu, rss = parsed
            if cpu is not None:
                cpus[pid] = cpu
            quiet = _quiet(parsed)
            orphan = ppid == 1 and quiet
            rows.setdefault(pid, {"ppid": ppid, "heavy": True, "rss": rss, "cpu": cpu, "quiet": quiet, "comm": base})
        else:
            loose.append(base)
        if orphan:
            idle.append({"pid": pid, "comm": base, "ppid": ppid, "age_s": int(age), "cpu_s": cpu, "cwd": None})
        else:
            active[base] = active.get(base, 0) + 1
            if parsed:
                active_pids.append(parsed[0])
    return active, idle, cpus, active_pids, _workloads(rows, loose)


def _quiet(parsed):
    """True when a row shows no sign of work (idle state, old enough, almost no recent CPU, every field readable)."""
    _, _, state, age, cpu, pcpu, rss = parsed
    pcpu_v = parse_pcpu(pcpu)
    return (state[:1] in IDLE_STATES and age is not None and age >= IDLE_MIN_AGE and cpu is not None
            and pcpu_v is not None and pcpu_v <= IDLE_PCPU and rss is not None)


def _workloads(rows, loose):
    children = {}
    for pid, r in rows.items():
        children.setdefault(r["ppid"], []).append(pid)
    out = []
    for pid in sorted(rows):
        if not rows[pid]["heavy"]:
            continue
        seen, up, nested = {pid}, rows[pid]["ppid"], False
        while up in rows and up not in seen:   # topmost heavy-named process owns the tree
            if rows[up]["heavy"]:
                nested = True
                break
            seen.add(up)
            up = rows[up]["ppid"]
        if nested:
            continue
        members, stack = [], [pid]
        while stack:
            cur = stack.pop()
            if cur in members:
                continue
            members.append(cur)
            stack.extend(children.get(cur, []))
        root = rows[pid]
        out.append({"root_pid": pid, "root_comm": root["comm"], "procs": len(members), "ancestors": sorted(seen - {pid}),
                    "rss_bytes": sum(rows[m]["rss"] or 0 for m in members),
                    "cpu_s": sum(rows[m]["cpu"] or 0.0 for m in members),
                    "idle": root["ppid"] == 1 and all(rows[m]["quiet"] for m in members),
                    "member_cpu": {m: rows[m]["cpu"] for m in members}})
    for base in loose:   # an unparsed heavy line has no tree: one active workload each
        out.append({"root_pid": None, "root_comm": base, "procs": 1, "ancestors": [], "rss_bytes": 0, "cpu_s": 0.0, "idle": False, "member_cpu": {}})
    return out


def lookup_cwd(pid, run, timeout=CWD_LOOKUP_TIMEOUT):
    out = _run([LSOF, "-a", "-d", "cwd", "-p", str(pid), "-Fn"], run, timeout=timeout)
    for line in (out or "").splitlines():
        if line.startswith("n") and len(line) > 1:
            return "".join(c for c in line[1:] if c.isprintable())[:200]
    return None


def fill_cwds(recs, run, budget=CWD_LOOKUP_BUDGET, now=time.monotonic):
    """Best-effort cwd for the first few idle orphans; a stuck lsof can never stall an admission check beyond `budget`."""
    start = now()
    for rec in recs[:MAX_IDLE_REPORT]:
        left = budget - (now() - start)
        if left <= 0:
            break
        rec["cwd"] = lookup_cwd(rec["pid"], run, timeout=min(CWD_LOOKUP_TIMEOUT, left))


def take_sample(run=subprocess.run, now_fn=time.time, load_fn=os.getloadavg, cpu_fn=os.cpu_count, disk_free_fn=None, mono_fn=time.monotonic):
    """One reading of the machine. Every field may be None when it could not be read (fail closed upstream)."""
    def sysctl_int(name):
        t = _run([SYSCTL, "-n", name], run)
        try:
            return int(t.strip())
        except (AttributeError, ValueError):
            return None
    # None = the process list could not be read (unknown), never "no heavy process"; empty output from a successful ps is
    # not "nothing running" either, because a live system always has processes.
    heavy, idle, cpus, active_pids, workloads = classify_heavy(_run([PS, "-axo", PS_FIELDS], run))
    fill_cwds(idle or [], run)
    try:
        load1 = load_fn()[0]
    except OSError:
        load1 = None
    if disk_free_fn is None:
        def disk_free_fn():
            st = os.statvfs("/System/Volumes/Data")
            return st.f_bavail * st.f_frsize
    try:
        disk_free = disk_free_fn()
    except OSError:
        disk_free = None
    return {"time": now_fn(), "mono": mono_fn(), "ram": sysctl_int("hw.memsize"), "pressure_level": sysctl_int("kern.memorystatus_vm_pressure_level"),
            "swap": parse_swapusage(_run([SYSCTL, "-n", "vm.swapusage"], run)), "vm": parse_vm_stat(_run([VM_STAT], run)),
            "load1": load1, "cpus": cpu_fn() or None, "disk_free": disk_free, "heavy_procs": heavy, "idle_procs": idle or [], "cpu_by_pid": cpus or {}, "active_pids": active_pids or [], "heavy_workloads": workloads}


def _derive_workloads(s1, s2, before, d):
    """Workloads of the second sample with `idle` re-proven across the window. Every member follows the re-add rules of a lone
    orphan: CPU spent in the window, absent from the first sample, or part of a tree that was active in the first sample makes
    the whole tree active. A root that this turns active is no longer reported as an idle orphan."""
    works = s2.get("heavy_workloads")
    if works is None or d["heavy_procs"] is None:
        return None   # no workload view (unreadable list or a hand-built sample): the name counts in heavy_procs decide
    first = s1.get("heavy_workloads") or []
    first_cpu, first_active = {}, set()
    for w in first:
        first_cpu.update(w.get("member_cpu") or {})
        if not w["idle"]:
            first_active.update((w.get("member_cpu") or {}))
    out = []
    for w in works:
        w = dict(w)
        if w["idle"]:
            for pid, cpu in (w.get("member_cpu") or {}).items():
                prior = first_cpu.get(pid, before.get(pid))
                if prior is None or cpu is None or cpu > prior or pid in first_active:
                    w["idle"] = False
                    break
        if not w["idle"]:
            d["idle_procs"] = [r for r in d["idle_procs"] if r["pid"] != w["root_pid"]]
        out.append(w)
    return out


def derive(s1, s2):
    """Rates and ratios from two samples taken `window` seconds apart. None means unknown."""
    d = {"pressure_level": s2.get("pressure_level"), "ram": s2.get("ram"), "disk_free": s2.get("disk_free"),
         "heavy_procs": s2.get("heavy_procs"), "swap": s2.get("swap")}
    if s1.get("heavy_procs") is None:
        d["heavy_procs"] = None   # a window with an unreadable first process list cannot show an orphan idle, or any lane absent
    # An orphan is idle only if it is proven so across the window: seen in the first sample with no more CPU time than now.
    # Any CPU spent, or no first reading at all (it may have been busy and unparsed), makes it a running lane.
    before = {r["pid"]: r["cpu_s"] for r in s1.get("idle_procs") or []}
    before.update(s1.get("cpu_by_pid") or {})
    was_active = set(s1.get("active_pids") or [])   # counted as a lane a moment ago: a decaying %cpu must not release it at once
    d["idle_procs"] = []
    for rec in s2.get("idle_procs") or []:
        if rec["pid"] not in before or rec["cpu_s"] > before[rec["pid"]] or rec["pid"] in was_active:
            if d["heavy_procs"] is not None:   # None stays None: an unreadable list already fails closed
                d["heavy_procs"] = dict(d["heavy_procs"], **{rec["comm"]: d["heavy_procs"].get(rec["comm"], 0) + 1})
        else:
            d["idle_procs"].append(rec)
    d["heavy_workloads"] = _derive_workloads(s1, s2, before, d)
    key = "mono" if s1.get("mono") is not None and s2.get("mono") is not None else "time"   # monotonic: a wall-clock step must not dilute the rate
    dt = (s2.get(key) or 0) - (s1.get(key) or 0)
    v1, v2 = s1.get("vm"), s2.get("vm")
    d["swap_io_mib_s"] = None
    d["compressor_ratio"] = None
    if v1 and v2 and dt > 0:
        pages = (v2["swapins"] - v1["swapins"]) + (v2["swapouts"] - v1["swapouts"])
        if pages >= 0:
            d["swap_io_mib_s"] = pages * v2["page_size"] / MIB / dt
        if s2.get("ram"):
            d["compressor_ratio"] = v2["compressor"] * v2["page_size"] / s2["ram"]
    d["load_per_core"] = (s2["load1"] / s2["cpus"]) if s2.get("load1") is not None and s2.get("cpus") else None
    return d


# ---- read-only accessors for disk-admission's SHADOW resource budget (SPE-150 PR4) -----------------------------------------
# They only re-read what derive() and refusal_reasons() already compute; no threshold, latch or decision here changes.
def budget_view(s1, s2):
    """What the shadow budget needs from a pair of samples: RAM, host used bytes (active + wired + compressed pages; an
    approximation to be calibrated), cores, load, the derived metrics `m`, and the running heavy-build trees (idle orphans
    excluded) with their pids. A value that could not be read is None."""
    m = derive(s1, s2)
    vm_ = s2.get("vm") or {}
    used = None
    if vm_.get("page_size") and all(type(vm_.get(k)) is int for k in ("active", "wired", "compressor")):
        used = (vm_["active"] + vm_["wired"] + vm_["compressor"]) * vm_["page_size"]
    trees = [{"root_pid": w["root_pid"], "root_comm": w["root_comm"], "procs": w["procs"], "rss_bytes": w["rss_bytes"],
              "members": sorted(w.get("member_cpu") or {})} for w in m.get("heavy_workloads") or [] if not w["idle"]]
    return {"m": m, "ram": s2.get("ram"), "used_bytes": used, "cores": s2.get("cpus"), "load1": s2.get("load1"), "workloads": trees}


def budget_band(m, load_per_core):
    """(band, reasons) for the shadow budget. RED: any current ENTER refusal or unreadable input (heavy-lane count reasons are
    left out: the lane count is what the budget replaces, and every admitted job would otherwise make the host RED). AMBER:
    not RED but an EXIT bound is unmet. GREEN: all met. `load_per_core` replaces the measured one (load corrected for admitted
    work); None keeps the sampled value."""
    view = dict(m, heavy_workloads=[], heavy_procs=None if m.get("heavy_procs") is None else {})
    if load_per_core is not None:
        view["load_per_core"] = load_per_core
    refuse, unknown = refusal_reasons("heavy", view)
    if refuse or unknown:
        return "RED", refuse + [f"{u}: fails closed" for u in unknown]
    return ("GREEN" if exit_ok(view) else "AMBER"), []


# ---- decision (pure) -------------------------------------------------------------------------------------------------
def format_age(seconds):
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d{h}h" if d else (f"{h}h{rem // 60}m" if h else f"{rem // 60}m")


def swap_cannot_grow(m):
    sw = m.get("swap")
    if not sw or not sw.get("total") or m.get("disk_free") is None:
        return False
    return sw["used"] / sw["total"] >= SWAP_FULL_FRACTION and sw["free"] < SWAP_LOW_FREE and m["disk_free"] < SWAP_GROWTH_DISK_FLOOR


# Mirrors HEAVY_LANES for the stable and wide disk tiers in disk-admission.py (a test pins the equality). Lane occupancy below is
# judged against this cap from the workloads actually running, owned or not, instead of from "any heavy process exists".
HEAVY_LANE_CAP = 1


def lane_occupancy(m, owned=None):
    """Reservation-aware lane accounting. Every running (non-idle) heavy workload tree occupies one lane. A tree is OWNED when an
    admitted reservation's pid, or a pid recorded in its workload, is the tree's root, a member or an ancestor of its root (the
    wrapper that exec'd the job); otherwise it is UNMANAGED. Pure and read-only: `owned` is a list of
    {"label","pid","members","mem_bytes","class"} the caller read from the reservations."""
    trees = []
    for w in m.get("heavy_workloads") or []:
        if w["idle"]:
            continue
        pids = ({w["root_pid"]} | set(w.get("member_cpu") or {}) | set(w.get("ancestors") or [])) - {None}
        hit = next((o for o in owned or [] if o["pid"] in pids or pids & set(o.get("members") or [])), None)
        trees.append({"comm": w["root_comm"], "procs": w["procs"], "rss_bytes": w["rss_bytes"],
                      "label": hit["label"] if hit else None, "class": hit.get("class") if hit else None,
                      "est_mem_bytes": hit.get("mem_bytes") if hit else None})
    return {"cap": HEAVY_LANE_CAP, "in_use": len(trees), "workloads": trees}


def lane_budget_text(occ):
    """The numbers behind a lane veto: lanes used of the cap, resident memory, and which tree is reserved or unmanaged."""
    parts = []
    for t in occ["workloads"]:
        if t["label"]:
            est = f", est {t['est_mem_bytes'] / GIB:.1f} GiB" if t["est_mem_bytes"] else ", legacy: no estimate"
            parts.append(f"reserved {t['label']} ({t['comm']}, {t['rss_bytes'] / GIB:.1f} GiB resident{est})")
        else:
            parts.append(f"unmanaged {t['comm']} ({t['rss_bytes'] / GIB:.1f} GiB resident, no reservation accounts for it)")
    resident = sum(t["rss_bytes"] for t in occ["workloads"]) / GIB
    return f"lane budget: {occ['in_use']} of {occ['cap']} heavy lane(s) in use, {resident:.1f} GiB resident: " + "; ".join(parts)


def refusal_reasons(weight, m):
    """Reasons memory alone would refuse `weight` work right now (ENTER thresholds)."""
    r, unknown = [], []
    lvl = m.get("pressure_level")
    if lvl is None or lvl not in VALID_LEVELS:
        unknown.append("pressure level unreadable or out of range")
    elif lvl >= PRESSURE_CRITICAL:
        r.append(f"memory pressure CRITICAL (level {lvl})")
    elif weight == "heavy" and lvl >= PRESSURE_WARN:
        r.append(f"memory pressure not normal (level {lvl}); heavy work needs level {PRESSURE_NORMAL}")
    io = m.get("swap_io_mib_s")
    lim = ENTER["swap_io_mib_s_" + weight]
    if io is None:
        unknown.append("swap I/O rate unreadable")
    elif io >= lim:
        r.append(f"sustained swap I/O {io:.1f} MiB/s >= {lim:g}")
    cr = m.get("compressor_ratio")
    lim = ENTER["compressor_ratio_" + weight]
    if cr is None:
        unknown.append("compressor size unreadable")
    elif cr >= lim:
        r.append(f"compressor holds {cr:.0%} of RAM >= {lim:.0%}")
    if weight == "heavy":
        lpc = m.get("load_per_core")
        if lpc is None:
            unknown.append("load unreadable")
        elif lpc >= ENTER["load_per_core_heavy"]:
            r.append(f"load per core {lpc:.1f} >= {ENTER['load_per_core_heavy']:g}")
        procs = m.get("heavy_procs")
        if procs is None:
            unknown.append("process list unreadable")
        elif m.get("heavy_workloads"):   # an empty list with heavy-named processes present falls to the name count below: fail closed
            running = [w for w in m["heavy_workloads"] if not w["idle"]]
            occ = lane_occupancy(m, m.get("owned"))
            if running and occ["in_use"] >= occ["cap"]:
                detail = "; ".join(f"{w['root_comm']}: {w['procs']} procs, {w['rss_bytes'] / GIB:.1f} GiB" for w in running)
                r.append(f"{len(running)} heavy build workload(s) running ({detail}): one heavy lane at a time "
                         f"[{lane_budget_text(occ)}]")
        elif procs:
            names = ", ".join(f"{k} x{v}" for k, v in sorted(procs.items()))
            r.append(f"another heavy build/test process is running ({names}): one heavy lane at a time")
        if m.get("swap") is None:
            unknown.append("swap usage unreadable")
        if m.get("disk_free") is None:
            unknown.append("free disk unreadable")
        if swap_cannot_grow(m):
            r.append("swap is nearly full and cannot grow (free disk below the heavy floor)")
    return r, unknown


def exit_ok(m):
    """True when this reading satisfies the stricter EXIT thresholds (counts toward leaving a heavy refusal)."""
    refuse, unknown = refusal_reasons("heavy", m)
    if refuse or unknown:
        return False
    io, cr, lpc = m.get("swap_io_mib_s"), m.get("compressor_ratio"), m.get("load_per_core")
    return m.get("pressure_level") == PRESSURE_NORMAL and io <= EXIT["swap_io_mib_s"] and cr <= EXIT["compressor_ratio"] and lpc <= EXIT["load_per_core"]


def decide(weight, m, state, now):
    """Pure. Returns (verdict, new_state). state = {'latched': bool, 'ok_streak': int, 'last_ok': float, 'levels': [..]}."""
    st = {"latched": False, "ok_streak": 0, "last_ok": 0.0, "levels": []}
    st.update(state or {})
    st["levels"] = [x for x in (st.get("levels") or []) if type(x) is int and x in VALID_LEVELS]
    if type(st["ok_streak"]) is not int or not 0 <= st["ok_streak"] < EXIT_SAMPLES:
        st["ok_streak"] = 0   # a hand-edited streak can never skip the hysteresis
    lo = st["last_ok"]
    if not isinstance(lo, (int, float)) or isinstance(lo, bool) or not math.isfinite(lo) or lo > now:
        st["last_ok"], st["ok_streak"] = 0.0, 0   # clock stepped back or tampered: restart the recovery count
    reasons, unknown = refusal_reasons(weight, m)
    warnings = []
    lvl = m.get("pressure_level")
    levels = (st.get("levels") or [])[-4:] + ([lvl] if lvl in VALID_LEVELS else [])
    st["levels"] = levels
    if len(levels) >= 2 and levels[-1] > levels[-2]:
        warnings.append(f"pressure trend rising ({levels[-2]} -> {levels[-1]})")
    if lvl == PRESSURE_WARN and weight == "light":
        warnings.append("memory pressure is at warn level: keep light work light")
    sw = m.get("swap")
    if sw and sw["total"] and sw["used"] / sw["total"] >= SWAP_FULL_FRACTION and not swap_cannot_grow(m) and not reasons:
        warnings.append("swap is mostly used but current swap I/O and pressure are low: historical, not current exhaustion")
    if weight == "heavy":
        for rec in m.get("idle_procs") or []:
            warnings.append(f"idle orphan {rec['comm']} pid {rec['pid']} (age {format_age(rec['age_s'])}, cwd {rec.get('cwd') or 'unknown'}) "
                            "is not counted as a heavy lane; its owner can reclaim it, nothing is signalled")
    if weight == "light":
        verdict = {"admit": not reasons, "reasons": reasons, "warnings": warnings + [f"unknown: {u}" for u in unknown]}
        return dict(verdict, weight=weight), st
    if unknown:
        reasons = reasons + [f"{u}: heavy work fails closed" for u in unknown]
    if reasons:
        st.update(latched=True, ok_streak=0)
    elif st["latched"]:
        if exit_ok(m):
            if now - st["last_ok"] >= MIN_SPACING or st["ok_streak"] == 0:
                st["ok_streak"] += 1
                st["last_ok"] = now
            if st["ok_streak"] >= EXIT_SAMPLES:
                st.update(latched=False, ok_streak=0)
        else:
            st["ok_streak"] = 0
        if st["latched"]:
            reasons = [f"hysteresis: recovering ({st['ok_streak']}/{EXIT_SAMPLES} good checks >= {MIN_SPACING}s apart)"]
    return {"admit": not reasons, "reasons": reasons, "warnings": warnings, "weight": weight}, st


# ---- state -----------------------------------------------------------------------------------------------------------
def load_state(path):
    """(state, problem). A missing file is a clean start; unreadable, non-regular or malformed state is reported and latches heavy work."""
    latched = {"latched": True}
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return latched, f"state unreadable: {exc.strerror or type(exc).__name__}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return latched, "state is not a regular file: treated as latched"
        raw = os.read(fd, MAX_STATE_BYTES + 1)
    except OSError as exc:
        return latched, f"state unreadable: {exc.strerror or type(exc).__name__}"
    finally:
        os.close(fd)
    if len(raw) > MAX_STATE_BYTES:
        return latched, "state corrupt: too large, treated as latched"
    try:
        d = json.loads(raw.decode("utf-8"))
        ok = (isinstance(d, dict) and isinstance(d.get("latched"), bool) and type(d.get("ok_streak")) is int and 0 <= d["ok_streak"] < EXIT_SAMPLES
              and isinstance(d.get("last_ok"), (int, float)) and not isinstance(d.get("last_ok"), bool) and math.isfinite(d["last_ok"])
              and isinstance(d.get("levels", []), list) and len(d.get("levels", [])) <= 8
              and all(type(x) is int and x in VALID_LEVELS for x in d.get("levels", [])))
    except (ValueError, UnicodeDecodeError):
        ok = False
    return (d, None) if ok else (latched, "state corrupt: treated as latched")


@contextlib.contextmanager
def state_lock(path, wait=LOCK_WAIT, sleep=time.sleep):
    """Exclusive advisory lock so two concurrent checks cannot overwrite a fresh latch with a stale unlatched state."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(p.parent / (p.name + ".lock")), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        waited = 0.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waited >= wait:
                    raise OSError("state busy: another check holds the lock")
                sleep(0.1)
                waited += 0.1
        yield
    finally:
        os.close(fd)


def save_state(path, state):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({k: state[k] for k in ("latched", "ok_streak", "last_ok", "levels")}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---- disk gate composition -------------------------------------------------------------------------------------------
def disk_gate(weight, run=subprocess.run):
    """Ask the EXISTING SPE-109 gate. Anything other than rc 0 refuses. Returns (admit, message)."""
    try:
        r = run([sys.executable, str(HERE / "disk-admission.py"), "check", "--weight", weight], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"disk gate unavailable: {type(exc).__name__}"
    msg = (r.stdout or r.stderr or "").strip().splitlines()
    return r.returncode == 0, (msg[0] if msg else f"disk gate rc={r.returncode}")[:200]


def read_owned(state_path):
    """Admitted reservations as {"label","pid","members","mem_bytes","class"}, read-only and never raising. They live next to this
    tool's state dir (`<cache>/admission/reservations`); an unreadable or absent directory means none are known, which only makes
    a running tree look unmanaged (the veto at cap 1 is the same either way)."""
    try:
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("disk_admission_readonly", HERE / "disk-admission.py")
        disk = module_from_spec(spec)
        spec.loader.exec_module(disk)
        directory = pathlib.Path(state_path).parent.parent / "admission" / "reservations"
        owned = []
        for path in sorted(directory.glob("*.json")):
            try:
                rec = disk._read_reservation(path)
            except (OSError, ValueError):
                continue
            owned.append({"label": rec["label"], "pid": rec["pid"], "class": rec.get("class"), "mem_bytes": rec.get("mem_bytes"),
                          "members": [x["pid"] for x in (rec.get("workload") or {}).get("members", [])]})
        return owned
    except Exception:
        return []


# ---- CLI -------------------------------------------------------------------------------------------------------------
def assess(weight, window, state_path, memory_only, deps):
    s1 = deps["sample"]()
    deps["sleep"](window)
    s2 = deps["sample"]()
    m = derive(s1, s2)
    if weight == "heavy":
        m["owned"] = deps["owned"](state_path) if deps.get("owned") else read_owned(state_path)
        try:
            with deps["lock"](state_path):
                state, problem = load_state(state_path)
                verdict, new_state = decide(weight, m, state, deps["now"]())
                if problem:
                    verdict["admit"] = False
                    verdict["reasons"] = verdict["reasons"] + [problem]
                try:
                    save_state(state_path, new_state)
                except OSError as exc:   # a refusal that cannot be remembered must not become an admission
                    verdict["admit"] = False
                    verdict["reasons"] = verdict["reasons"] + [f"state unwritable ({exc.strerror or type(exc).__name__}): heavy work fails closed"]
        except OSError as exc:
            verdict, _ = decide(weight, m, {"latched": True}, deps["now"]())
            verdict["admit"] = False
            verdict["reasons"] = verdict["reasons"] + [f"{exc}: heavy work fails closed"]
    else:
        verdict, _ = decide(weight, m, None, deps["now"]())
    verdict["idle_orphans"] = [{k: r.get(k) for k in ("pid", "comm", "age_s", "cwd")} for r in m.get("idle_procs") or []]
    verdict["memory"] = {k: m[k] for k in ("pressure_level", "swap_io_mib_s", "compressor_ratio", "load_per_core", "disk_free")}
    verdict["memory"]["swap_used_fraction"] = (m["swap"]["used"] / m["swap"]["total"]) if m.get("swap") and m["swap"]["total"] else None
    if not memory_only and weight:
        ok, msg = deps["disk"](weight)
        verdict["disk_gate"] = {"admit": ok, "message": msg}
        if not ok:
            verdict["admit"] = False
            verdict["reasons"] = verdict["reasons"] + [f"disk gate: {msg}"]
    return verdict


def main(argv=None, deps=None):
    d = {"sample": take_sample, "sleep": time.sleep, "now": time.time, "disk": disk_gate, "lock": state_lock}
    d.update(deps or {})
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--weight", required=True, choices=("heavy", "light"))
    c.add_argument("--memory-only", action="store_true")
    s = sub.add_parser("status")
    for p in (c, s):
        p.add_argument("--json", action="store_true")
        p.add_argument("--window", type=float, default=10.0)
        p.add_argument("--state", default=str(DEFAULT_STATE))
    a = ap.parse_args(argv)
    window = min(max(a.window, 1.0), 120.0) if math.isfinite(a.window) else 10.0
    if a.cmd == "status":
        v = assess("light", window, a.state, True, d)  # status never writes heavy state or asks the disk gate
        v.pop("admit")
        v["weight"] = None
    else:
        v = assess(a.weight, window, a.state, a.memory_only, d)
    if a.json:
        print(json.dumps(v, indent=1))
    else:
        m = v["memory"]
        head = ("ADMIT" if v.get("admit") else "REFUSE") if a.cmd == "check" else "STATUS"
        print(f"{head} {v.get('weight') or ''} | pressure={m['pressure_level']} swapIO={m['swap_io_mib_s'] if m['swap_io_mib_s'] is None else round(m['swap_io_mib_s'], 1)}MiB/s "
              f"compressor={'?' if m['compressor_ratio'] is None else format(m['compressor_ratio'], '.0%')} load/core={'?' if m['load_per_core'] is None else round(m['load_per_core'], 1)}")
        for r in v.get("reasons", []):
            print("  reason:", r)
        for w in v.get("warnings", []):
            print("  note:", w)
    return 0 if a.cmd == "status" or v["admit"] else REFUSE_RC


if __name__ == "__main__":
    sys.exit(main())
