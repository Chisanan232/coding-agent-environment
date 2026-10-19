# Memory-aware resource admission (SPE-117)

Shared policy for every personal Claude Code and Codex session on one workstation. It adds a **memory**
precondition next to the existing SPE-109 **disk** gate. It does not change that gate, its thresholds, its
reservations or its lane caps, and it does not replace Glomeris, which stays the only cleanup executor.

## 1. What is assessed before heavy work

Heavy work means workspace or full builds, Cargo workspace builds, Docker suites, native QA, full E2E and large parallel
subagent fan-out. Run `scripts/memory-admission.py check --weight heavy` first; it reads:

| Signal | Source | Why it matters |
|---|---|---|
| Pressure level and trend | `sysctl kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, 4 critical), kept across checks | The kernel's own verdict on current pressure |
| Physical RAM and compressed memory | `hw.memsize`, `vm_stat` "occupied by compressor" | A large compressor share means the machine is already holding memory it cannot drop cheaply |
| Swap used **and** sustained swap I/O | `vm.swapusage`, swapins+swapouts over a measured window | Used swap alone is history; the I/O rate is current behaviour |
| Disk and swap-growth headroom | free bytes of the Data volume | Swap files grow on that volume; a full disk means swap cannot grow |
| Active build/test processes | executable names only (`cargo`, `rustc`, `clang`, `ld`, `xcodebuild`, ...) | One heavy lane at a time |
| Load per core | `getloadavg` / CPU count | Scheduler saturation that RAM numbers do not show |
| Reservations and ownership | the existing `disk-admission.py` gate (reserve, lane cap, process identity) | Task ownership already lives there; it is not duplicated |

`check` asks the disk gate too and admits only when **both** admit. Neither gate relies on the other.

## 2. Why "swap used" is not a gate

Evidence from one workstation (128 GiB RAM, Data volume 1.8 TiB), 2026-10-19:

| Moment | Pressure | Swap used / total | Swap I/O | Compressor | Load per core | Free disk | Reading |
|---|---|---|---|---|---|---|---|
| Before a forced reboot | 2 then 4 | 69.0 / 69.6 GiB | 3-6 MiB/s | 71 GiB (56% of RAM) | ~8 | 74-79 GiB | Stressed, not thrashing; swap largely historical |
| One hour after reboot | 1 | 0 / 0 GiB (no swap files yet) | 0 | 0 | 0.3 | 494 GiB | Healthy |

A rule such as "refuse if free swap < 4 GiB" refused the healthy second row (no swap file exists yet, so free swap is 0)
and says nothing about whether the first row was thrashing. Pressure level, swap I/O rate and compressor share separate the two.
Swap-free is kept only as a warning and, combined with a nearly full disk, as the "swap cannot grow" refusal below.

## 3. Proposed thresholds and hysteresis (owner approval required before any gate adopts them)

All values are constants at the top of `scripts/memory-admission.py`. They are conservative measurements, not a tuned model.

| Signal | Refuse heavy at | Refuse light at | Leave a heavy refusal only when |
|---|---|---|---|
| Pressure level | >= 2 (heavy needs level 1) | 4 (critical) | level 1 |
| Swap I/O over the window | >= 16 MiB/s | >= 128 MiB/s | <= 4 MiB/s |
| Compressor share of RAM | >= 35% | >= 50% | <= 25% |
| Load per core | >= 4.0 | never | <= 2.0 |
| Other heavy process running | any | never | none |
| Swap >= 90% used, < 4 GiB free **and** disk < 100 GiB | refuse | never | any condition clears |

Hysteresis: a heavy refusal latches. It clears only after **3 consecutive checks, at least 30 s apart**, that all satisfy the
stricter exit column. A bad check resets the count; checks closer than 30 s do not count. Heavy state lives in
`~/.cache/coding-agent-environment/memory-admission/state.json` (0600, atomic replace). Missing data, an unreadable counter, or a
corrupt state file fails closed for heavy work and is reported; light work is refused only on measured critical conditions and
is otherwise admitted with a warning. `status` is read-only: it never refuses, never writes state and never asks the disk gate.

Exit code 0 admits and 77 refuses, like `disk-admission.py`. Typical use:

```bash
scripts/memory-admission.py check --weight heavy && scripts/disk-admission.py run --label <job> --bytes <n> -- <command>
```

Limits, stated plainly: this is a pre-start check, not enforcement; it cannot see work that starts after the check; heavy-process
detection matches executable names only; it targets macOS. Wiring it into `disk-admission.py run` or a hook is a separate owner decision.

## 4. Concurrency

One admitted heavy lane at a time, as the resource governor already allows. Do not start competing Cargo workspace builds, Docker
suites, native QA or full E2E; lower subagent fan-out while pressure is not normal (the seven-subagent cap is a ceiling, not a
target). Prefer reusable build artifacts and isolated targets inside the budget. Never kill another active session, and never delete
its worktree, runtime, cache, container or reservation.

## 5. Cleanup

Aim for at least 100 GiB free, using only proven-safe actions. Investigate by owner, process lifetime, open handles, age and
reproducibility. Age alone never justifies deletion, and unknown or live work is never deleted to reach an admission threshold.

| Class | Meaning | Action |
|---|---|---|
| SAFE_TO_CLEAN | Regenerable cache with a documented clean command and no active user | Clean (prefer Glomeris) |
| INACTIVE_AND_PROVEN_OWNED | Idle scratch whose owner is this task, with no open handles | Remove only what this task created |
| NEEDS_OWNER_CONFIRMATION | Idle but not provably this task's, or ask-only by policy (redirected Cargo targets, other sessions' scratch, container volumes) | Present sizes and evidence; the owner runs the command |
| PROTECTED_ACTIVE | Open handles, a live process tree, a live reservation, or a recent session | Do not touch |

Ownership evidence is a live-process check (`lsof` handles, process start times), not a name match. A host denial of a delete is
binding; give the operator the exact command instead.

## 6. Post-reboot recovery

1. Treat every earlier process, lock, reservation, background-task and job observation as stale.
2. Re-measure memory, swap I/O, disk and the session count; do not reuse an earlier NO-GO or GO.
3. Reconcile GitHub (`gh pr list`, `gh run list`) and Jira with the persisted checkpoints and handoff files.
4. Revalidate process and reservation identities by pid **and** start time; drop reservations whose process is gone.
5. For each earlier test job read its durable result file or log; do not assume it survived or completed, and do not rerun
   finished work. Keep completed QA evidence.
6. Resume only genuinely incomplete tasks and record each blocker with its exact resume trigger.

## 7. Owner decisions

1. Approve, change or reject the proposed thresholds above.
2. Decide whether `disk-admission.py run` or a hook should call `memory-admission.py` automatically.
3. Decide whether the SPE-109 gate itself should take a memory input (this change does not touch it).
