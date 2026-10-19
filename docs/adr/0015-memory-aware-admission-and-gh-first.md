# ADR 0015: Memory-aware admission, `gh`-first GitHub operations, GitHub MCP retirement (SPE-117)

Status: accepted for the repository (policy and tooling merged); **not deployed to any machine**.

## Context

A workstation shared by several long-running Claude Code and Codex sessions stalled repeatedly and was rebooted after severe memory
pressure. SPE-109 admission looked only at disk. The pre-reboot "swap used 69 of 69.6 GiB" number was history, not current
thrashing (swap I/O was a few MiB/s), yet it was easy to misread, and a later guard written as "free swap >= 4 GiB" would have
refused a healthy machine right after the reboot, when no swap files exist. Separately, the GitHub MCP container was failing while
`gh` already worked and is the interface the merge gate and PR classes (ADR 0014) are written against.

## Decisions

1. **Memory is assessed with current signals, not swap-used.** `scripts/memory-admission.py` uses the kernel pressure level and trend,
   swap I/O over a window, compressor share of RAM, load per core, other heavy processes, and free disk only to judge whether swap can
   grow. It composes with the SPE-109 disk gate (reservations and lane caps stay there) and admits only when both admit.
2. **Conservative, reviewable thresholds with hysteresis**, as constants with tests; they are *proposed* until the owner approves them,
   and the existing disk gate thresholds are not changed.
3. **One heavy lane; no destructive shortcuts.** Never kill another session or delete its worktree, runtime, cache, container,
   reservation or scratch. Cleanup is by proven idleness and ownership, never age alone; ask-only resources stay ask-only.
4. **`gh` is the GitHub interface; the GitHub MCP is retired** (repository snapshot entry removed, inspector and owner-run removal
   runbook provided). Host authorization, PR classes A/B/C, checks, reviews and the merge gate are unchanged, and a host denial is
   never rerouted.
5. **Post-reboot recovery** treats earlier process, lock, reservation and job observations as stale and resumes only incomplete work.

## Consequences

- Heavy work can be refused for memory even when disk is plentiful, and the reason names the signal.
- A fresh machine (pressure 1, no swap) is admitted; a machine with historical swap but calm current behaviour is admitted.
- The pre-start check cannot see later load and is not enforcement; wiring it into `disk-admission.py run` or a hook is a separate decision.
- Live deployment (user-level `CLAUDE.md`, MCP removal) is owner-run: `docs/ROLLOUT-USER-CLAUDE-MD.md`, `docs/GITHUB-MCP-RETIREMENT.md`.

## Version history

| Rev | Change |
|---|---|
| 1 | Initial decision (SPE-117) |
