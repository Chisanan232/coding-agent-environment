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
import importlib.util
import json
import os
import pathlib
import re
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
DEFAULT_STATE = pathlib.Path.home() / ".cache/coding-agent-environment/memory-admission/state.json"
REFUSE_RC = 77


# ---- parsing ---------------------------------------------------------------------------------------------------------
def parse_swapusage(text):
    """'total = 69632.00M  used = 69001.25M  free = 630.75M  (encrypted)' -> bytes, or None when unparseable."""
    units = {"K": 1024, "M": MIB, "G": GIB}
    out = {}
    for key in ("total", "used", "free"):
        m = re.search(rf"{key}\s*=\s*([\d.]+)\s*([KMG])", text or "")
        if not m:
            return None
        out[key] = int(float(m.group(1)) * units[m.group(2)])
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


def _run(cmd, run=subprocess.run):
    try:
        r = run(cmd, capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def take_sample(run=subprocess.run, now_fn=time.time, load_fn=os.getloadavg, cpu_fn=os.cpu_count, disk_free_fn=None):
    """One reading of the machine. Every field may be None when it could not be read (fail closed upstream)."""
    def sysctl_int(name):
        t = _run(["sysctl", "-n", name], run)
        try:
            return int(t.strip())
        except (AttributeError, ValueError):
            return None
    comms = _run(["ps", "-axo", "comm="], run)
    heavy = {}
    for line in (comms or "").splitlines():
        base = os.path.basename(line.strip())
        if base in HEAVY_COMMS:
            heavy[base] = heavy.get(base, 0) + 1
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
    return {"time": now_fn(), "ram": sysctl_int("hw.memsize"), "pressure_level": sysctl_int("kern.memorystatus_vm_pressure_level"),
            "swap": parse_swapusage(_run(["sysctl", "-n", "vm.swapusage"], run)), "vm": parse_vm_stat(_run(["vm_stat"], run)),
            "load1": load1, "cpus": cpu_fn() or None, "disk_free": disk_free, "heavy_procs": heavy}


def derive(s1, s2):
    """Rates and ratios from two samples taken `window` seconds apart. None means unknown."""
    d = {"pressure_level": s2.get("pressure_level"), "ram": s2.get("ram"), "disk_free": s2.get("disk_free"),
         "heavy_procs": s2.get("heavy_procs") or {}, "swap": s2.get("swap")}
    dt = (s2.get("time") or 0) - (s1.get("time") or 0)
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


# ---- decision (pure) -------------------------------------------------------------------------------------------------
def swap_cannot_grow(m):
    sw = m.get("swap")
    if not sw or not sw.get("total") or m.get("disk_free") is None:
        return False
    return sw["used"] / sw["total"] >= SWAP_FULL_FRACTION and sw["free"] < SWAP_LOW_FREE and m["disk_free"] < SWAP_GROWTH_DISK_FLOOR


def refusal_reasons(weight, m):
    """Reasons memory alone would refuse `weight` work right now (ENTER thresholds)."""
    r, unknown = [], []
    lvl = m.get("pressure_level")
    if lvl is None:
        unknown.append("pressure level unreadable")
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
        procs = m.get("heavy_procs", {})
        if procs:
            names = ", ".join(f"{k} x{v}" for k, v in sorted(procs.items()))
            r.append(f"another heavy build/test process is running ({names}): one heavy lane at a time")
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
    reasons, unknown = refusal_reasons(weight, m)
    warnings = []
    lvl = m.get("pressure_level")
    levels = (st.get("levels") or [])[-4:] + ([lvl] if lvl is not None else [])
    st["levels"] = levels
    if len(levels) >= 2 and levels[-1] > levels[-2]:
        warnings.append(f"pressure trend rising ({levels[-2]} -> {levels[-1]})")
    if lvl == PRESSURE_WARN and weight == "light":
        warnings.append("memory pressure is at warn level: keep light work light")
    sw = m.get("swap")
    if sw and sw["total"] and sw["used"] / sw["total"] >= SWAP_FULL_FRACTION and not swap_cannot_grow(m) and not reasons:
        warnings.append("swap is mostly used but current swap I/O and pressure are low: historical, not current exhaustion")
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
    """(state, problem). A missing file is a clean start; unreadable or malformed state is reported and latches heavy work."""
    try:
        raw = pathlib.Path(path).read_text()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {"latched": True}, f"state unreadable: {exc}"
    try:
        d = json.loads(raw)
        ok = (isinstance(d, dict) and isinstance(d.get("latched"), bool) and isinstance(d.get("ok_streak"), int) and 0 <= d["ok_streak"] <= 100
              and isinstance(d.get("last_ok"), (int, float)) and isinstance(d.get("levels", []), list))
    except ValueError:
        ok = False
    return (d, None) if ok else ({"latched": True}, "state corrupt: treated as latched")


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


# ---- CLI -------------------------------------------------------------------------------------------------------------
def assess(weight, window, state_path, memory_only, deps):
    s1 = deps["sample"]()
    deps["sleep"](window)
    s2 = deps["sample"]()
    m = derive(s1, s2)
    state, problem = ({}, None)
    if weight == "heavy":
        state, problem = load_state(state_path)
    verdict, new_state = decide(weight, m, state, deps["now"]())
    if problem:
        verdict["admit"] = False
        verdict["reasons"] = verdict["reasons"] + [problem]
    if weight == "heavy" or problem is None:
        with contextlib.suppress(OSError):
            if weight == "heavy":
                save_state(state_path, new_state)
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
    d = {"sample": take_sample, "sleep": time.sleep, "now": time.time, "disk": disk_gate}
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
    window = min(max(a.window, 1.0), 120.0)
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
