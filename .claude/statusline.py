#!/usr/bin/env python3
"""Claude Code status line.

Two rows, plus an optional third row (BG N | sub/shell/monitor/other counts)
when bg-track.py has recorded anything for this session — see bg_segment():

    [CAVEMAN] [Opus 5·xhigh] 📁 aa-5674 🌿 AAASM-5674/feat/apply_outcome_wire* ⇡2 · PR #1972 ✓
    ███████░░░░░░ 42% ctx · 5h 23% · 7d 41% · $1.24 · 💾 35% (1.2T) · 🧠 54%

Row 1 answers "where am I" — the thing that goes wrong when 35 near-identically
named worktrees exist. Row 2 answers "what am I running out of", ordered by how
abruptly each one stops you: rate limit kills a task mid-flight, context forces a
compact, disk kills the machine.

Everything optional is omitted entirely when absent rather than rendered empty,
so the line stays short outside a repo or before the first API call.

Wired up via ~/.claude/settings.json -> statusLine.
"""

import json
import os
import subprocess
import sys
import time

BG_STATE_DIR = os.path.expanduser("~/.claude/bg-state")
BG_TTL_SECONDS = 60 * 60

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[38;5;71m"
YELLOW = "\033[38;5;179m"
RED = "\033[38;5;167m"
CYAN = "\033[38;5;80m"
BLUE = "\033[38;5;110m"
GREY = "\033[38;5;245m"

SEP = f"{DIM} · {RESET}"


def c(text, color):
    return f"{color}{text}{RESET}"


def pressure(pct, warn=75, crit=90):
    """Green below warn, yellow at warn, red at crit. One rule everywhere."""
    return RED if pct >= crit else YELLOW if pct >= warn else GREEN


def run(args, cwd=None, timeout=0.4):
    """Git in a 300 GB repo can stall. A status line must never be why."""
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def human_kb(kb):
    if kb >= 1024 ** 3:
        return f"{kb / 1024 ** 3:.1f}T"
    if kb >= 1024 ** 2:
        return f"{kb / 1024 ** 2:.0f}G"
    return f"{kb / 1024:.0f}M"


def bar(pct, width=12):
    """Solid/shaded blocks, colored by the same pressure rule as everything else."""
    pct = max(0, min(100, pct))
    filled = round(width * pct / 100)
    return c("█" * filled, pressure(pct)) + c("░" * (width - filled), DIM)


def caveman_badge():
    p = os.path.expanduser(
        "~/.claude/plugins/cache/caveman/caveman/25d22f864ad6/src/hooks/caveman-statusline.sh"
    )
    if not os.access(p, os.X_OK):
        return None
    return run(["bash", p], timeout=0.3) or None


def git_segments(cwd):
    """Branch, dirty marker, and unpushed count — the trio that goes stale silently.

    One `git status --porcelain --branch` yields all three. Three separate git
    invocations tripled the wall time for no extra information.

    --untracked-files=no keeps this off the "walk every file" path; in a repo
    with a multi-GB node_modules the untracked scan alone blows the timeout.
    """
    out = []
    raw = run(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=no"],
        cwd=cwd,
        timeout=0.6,
    )
    if raw is None:
        return out

    lines = raw.splitlines()
    if not lines or not lines[0].startswith("##"):
        return out

    header, changes = lines[0][3:], lines[1:]

    ahead = 0
    if "[" in header:
        header, _, track = header.partition(" [")
        for part in track.rstrip("]").split(", "):
            if part.startswith("ahead "):
                ahead = int(part.split()[1])

    branch = header.split("...")[0].strip()
    if branch.startswith("HEAD (no branch)"):
        branch = "detached"

    # Long ticket branches (v0.0.1/AAASM-5674/feat/apply_outcome_wire) eat the
    # whole row. The release prefix is the least informative part — drop it.
    label = "/".join(branch.split("/")[1:]) if branch.count("/") >= 3 else branch

    mark = c("*", YELLOW) if changes else ""
    out.append(f"🌿 {c(label, GREEN)}{mark}")
    if ahead:
        out.append(c(f"⇡{ahead}", YELLOW))
    return out


def disk_segment():
    # /System/Volumes/Data, not / — the sealed system volume reads ~5% forever
    # and would have shown nothing while the data volume filled to 83%.
    out = run(["df", "-k", "/System/Volumes/Data"], timeout=0.4)
    if not out:
        return None
    try:
        f = out.splitlines()[1].split()
        avail, used = int(f[3]), int(f[4].rstrip("%"))
    except (IndexError, ValueError):
        return None
    return c(f"💾 {used}% ({human_kb(avail)})", pressure(used))


def mem_segments():
    """Used = active + wired + compressed. Inactive pages are reclaimable on
    demand, so counting them would read as permanent pressure."""
    out = []
    total = run(["sysctl", "-n", "hw.memsize"], timeout=0.3)
    vm = run(["vm_stat"], timeout=0.3)
    if total and vm and total.isdigit():
        pages, size = {}, 4096
        for line in vm.splitlines():
            if "page size of" in line:
                size = int(line.split()[-2])
            elif ":" in line:
                k, _, v = line.partition(":")
                v = v.strip().rstrip(".")
                if v.isdigit():
                    pages[k.strip()] = int(v)
        used = sum(
            pages.get(k, 0)
            for k in ("Pages active", "Pages wired down", "Pages occupied by compressor")
        )
        pct = int(used * size * 100 / int(total))
        out.append(c(f"🧠 {pct}%", pressure(pct)))

    # Swap only when non-zero: a healthy machine shouldn't be swapping at all,
    # so a permanent "swap 0M" would train the eye to ignore the segment.
    sw = run(["sysctl", "-n", "vm.swapusage"], timeout=0.3)
    if sw and "used = " in sw:
        try:
            raw = sw.split("used = ")[1].split()[0]
            mb = float(raw.rstrip("MGK")) * (1024 if raw.endswith("G") else 1)
            if mb >= 1:
                val = f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"
                out.append(c(f"swap {val}", YELLOW))
        except (IndexError, ValueError):
            pass
    return out


def bg_segment(session_id):
    """Row 3: what's actually tracked as running, straight from bg-track.py's
    state file — never inferred from prose. `sub` is exact (SubagentStart/Stop
    is a real add/remove pair). `shell`/`monitor`/`other` are add-only (no
    verified completion hook exists), so a nonzero count gets a trailing "?"
    rather than asserted as precise — see hooks/bg-track.py's docstring.
    """
    if not session_id:
        return None
    path = os.path.join(BG_STATE_DIR, f"{session_id}.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}

    now = time.time()
    sub = len(data.get("subagents", {}))
    shell = sum(1 for v in data.get("shells", {}).values() if now - v.get("t", 0) < BG_TTL_SECONDS)
    mon = sum(1 for v in data.get("monitors", {}).values() if now - v.get("t", 0) < BG_TTL_SECONDS)
    other = sum(1 for v in data.get("workflows", {}).values() if now - v.get("t", 0) < BG_TTL_SECONDS)
    total = sub + shell + mon + other

    def n(count, uncertain):
        return f"{count}?" if uncertain and count else str(count)

    col = YELLOW if total else GREY
    return (
        f"{c(f'BG {total}', col)}{SEP}"
        f"🤖 sub {sub}{SEP}"
        f"🐚 shell {n(shell, True)}{SEP}"
        f"👁️ monitor {n(mon, True)}{SEP}"
        f"⚙️ other {n(other, True)}"
    )


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}

    cwd = d.get("workspace", {}).get("current_dir") or d.get("cwd") or os.getcwd()

    # ---- row 1: where am I --------------------------------------------------
    row1 = []

    badge = caveman_badge()
    if badge:
        row1.append(badge)

    model = d.get("model", {}).get("display_name")
    if model:
        model = model.replace(" (1M context)", "")
        effort = (d.get("effort") or {}).get("level")
        label = f"{model}·{effort}" if effort else model
        if d.get("fast_mode"):
            label += "·fast"
        row1.append(c(f"[{label}]", CYAN))

    # The worktree name is the ticket. With 35 sibling worktrees whose folder
    # names differ by a suffix, this is the single most misread thing on screen.
    wt = d.get("workspace", {}).get("git_worktree") or (d.get("worktree") or {}).get("name")
    row1.append(f"📁 {c(wt, BOLD + BLUE)}" if wt else f"📁 {c(os.path.basename(cwd), BLUE)}")

    row1 += git_segments(cwd)

    pr = d.get("pr") or {}
    if pr.get("number"):
        state = pr.get("review_state")
        icon, col = {
            "approved": ("✓", GREEN),
            "changes_requested": ("✗", RED),
            "pending": ("·", YELLOW),
            "draft": ("◌", GREY),
        }.get(state, ("", GREY))
        row1.append(c(f"PR #{pr['number']} {icon}".strip(), col))

    agent = (d.get("agent") or {}).get("name")
    if agent:
        row1.append(c(f"@{agent}", GREY))

    # ---- row 2: what am I running out of ------------------------------------
    row2 = []

    ctx = d.get("context_window") or {}
    pct = ctx.get("used_percentage")
    if pct is not None:
        pct = int(pct)
        row2.append(f"{bar(pct)} {c(f'{pct}%', pressure(pct))}{DIM} ctx{RESET}")

    # Ordered before cost deliberately: hitting the 5h window stops the session
    # outright, which is a harder stop than any dollar figure.
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        rl = (d.get("rate_limits") or {}).get(key) or {}
        u = rl.get("used_percentage")
        if u is not None:
            u = int(u)
            row2.append(f"{DIM}{label}{RESET} {c(f'{u}%', pressure(u, 70, 88))}")

    cost = (d.get("cost") or {}).get("total_cost_usd")
    if cost:
        row2.append(c(f"${cost:.2f}", GREY))

    ms = (d.get("cost") or {}).get("total_duration_ms")
    if ms and ms > 60000:
        m, s = divmod(int(ms // 1000), 60)
        h, m = divmod(m, 60)
        row2.append(c(f"⏱️ {h}h{m}m" if h else f"⏱️ {m}m{s}s", GREY))

    ds = disk_segment()
    if ds:
        row2.append(ds)
    row2 += mem_segments()

    print(SEP.join(row1))
    if row2:
        print(SEP.join(row2))

    row3 = bg_segment(d.get("session_id"))
    if row3:
        print(row3)


if __name__ == "__main__":
    main()
