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
| Active build/test processes | `ps` pid, ppid, state, elapsed and CPU time, %cpu, rss and the executable name (`cargo`, `rustc`, `clang`, `ld`, `xcodebuild`, ...); never the command line | One heavy lane at a time, counted as a process tree (workload), not per process; see "Idle orphans" below |
| Load per core | `getloadavg` / CPU count | Scheduler saturation that RAM numbers do not show |
| Reservations and ownership | the existing `disk-admission.py` gate (reserve, lane cap, process identity) | Task ownership already lives there; it is not duplicated |

The script runs from the coding-agent-environment checkout; it is not installed into `~/.claude` (installing it next to
`disk-admission.py` is a separate owner decision). Commands are called by absolute path (`/usr/sbin/sysctl`, `/bin/ps`, `/usr/bin/vm_stat`).

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
`~/.cache/coding-agent-environment/memory-admission/state.json` (0600, atomic replace). For heavy work these fail closed and are reported: an unreadable pressure level (or one outside 1, 2, 4), swap I/O, compressor size,
load, process list, swap usage or free disk, a corrupt, oversized, non-regular or symlinked state file, an unwritable state file (a refusal
that cannot be remembered is not allowed to become an admission), and a lock held by another check for more than 10 s. Light work is refused
only on measured critical conditions and is otherwise admitted with a warning. Rates use the monotonic clock, so a wall-clock step during the
window cannot dilute them; a state whose recovery time is in the future restarts its count. `status` is read-only: it never refuses, never writes state and never asks the disk gate.

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

## Idle orphans (SPE-134)

A heavy-named process is a running lane, not a name match. A process counts as an **idle orphan** only when all of these
hold: its parent is launchd (ppid 1), its state is not running, uninterruptible or stopped (a stopped process is a deliberate pause that can resume), `%cpu` is at most 1.0, it has been alive
for at least 15 minutes, and it spent no CPU between the two samples of the check (any increase, even 10 ms, or no first-sample reading at all, or having been counted as active in the first sample, makes it active). An idle orphan does not refuse
heavy work. It is reported as a note with its pid, age and working directory (read with `lsof`, 4 seconds per call and never more than 8 seconds in total per sample, never the command line) so
its owner can reclaim it; nothing is ever signalled. Everything else counts as an active lane, including a quiet process
with a live parent (a `rustc` under `cargo` or a shell), and any line `ps` printed that cannot be parsed. An unreadable
process list still fails closed. Load, memory pressure, swap and disk reasons are unchanged. The live parent chain is the
ownership signal: an SPE-109 reservation is held by the job that reserved it, whose build processes have that job as a
live ancestor, so a reservation-holding build is never an orphan; reservation records are not read here.

Workloads (SPE-150): the lane count is the number of process trees, not of heavy-named processes. A workload is a heavy-named
process with no heavy-named ancestor plus its whole subtree (shells and test binaries included); `cargo` with three `rustc`
children and a test binary is one workload, reported with its process count and summed RSS. A workload is idle only when its root is an idle orphan
and every member is quiet by the same tests, so an orphan `cargo` with an idle `rustc` child no longer refuses; any member that spends CPU, is new, or was active in the first sample makes the whole tree active.

## Measured workload peaks (SPE-150)

Observation only; no admission decision reads it. `disk-admission.py` appends one line per live reservation holder to
`measurements.jsonl` in the admission state directory (mode 0600, rotated once at 1 MiB to `.1`, like `receipts.jsonl`)
on `snapshot` (the 60 s refresher), `reserve` (and so `run`), `queue advance` and `queue claim`, and an `end` line on
`release` with the run's duration and its free-bytes change. A line carries the label, queue id, pid and start token,
the holder's whole process subtree (summed RSS, CPU seconds, process count), the kernel pressure level, how many
reservations were live, and free bytes. It never carries argv, a command line or the environment; `ps` is asked for
`pid,ppid,rss,time` only. Sampling runs after the admission lock is released, and any failure is swallowed (counted
in-process), so a log problem cannot change a reserve, release or advance result. A job that exits without `release`
gets no `end` line; its samples still count. The `end` free-bytes change uses a plain volume reading, so it can differ
slightly from the Glomeris-minimum figure on sample lines.

`disk-admission.py measurements [--json]` is read-only. Per class (the reservation `class` when present, otherwise its
label) it reports runs, max RSS, p95 of per-run peaks, solo versus concurrent runs, and samples taken under pressure above 1.
**All figures are lower bounds**: processes are sampled about every 60 s, so a short peak can be missed. A class gets an
estimate only from at least 3 solo runs with pressure 1 throughout (the estimate is the largest such peak); otherwise it
reports "insufficient evidence". Because labels are usually per job, most classes will stay at "insufficient evidence"
until jobs share a stable label or class.

## Lane holders, workloads and the UNRESOLVED state (SPE-111)

A lane belongs to a **workload**, not to the wrapper process that reserved it. A wrapper that exited (even one nobody reaped,
a zombie) proves nothing about the work it started: that work can outlive it through `setsid`, a double fork or plain
reparenting to launchd. The wrapper's death is therefore never, by itself, a release.

What is recorded. At reserve and claim time, and again every time a reservation is inspected while its wrapper is still
alive (every reserve, claim, queue advance and the 60-second snapshot refresher), the reservation stores a bounded workload
identity: the wrapper's process group when it leads one (`pgid == pid`) and up to 256 descendant `pid` + start-token pairs
(the wrapper's process subtree, their subtrees, and live members of the wrapper's own group). Only `pid`, `ppid`, `pgid`,
`stat` and `lstart` are read from `ps`, never a command line. The record is kept on the queue entry's holder as well, so it
survives an explicit `release` of a job that is still running.

Decision per holder (`holder_status`):

| Situation | State | Lane and bytes |
|---|---|---|
| Wrapper is the same running process (start token matches, not a zombie) | alive | kept |
| Wrapper gone, a zombie, or its pid reused, and a recorded member still runs (start token matches, not a zombie) | **unresolved** | kept, reason reported |
| Holder or process table cannot be inspected (`ps` error, unparseable table) | **unresolved** | kept, reported as a problem (refuses admission) |
| Wrapper is an unreaped zombie and its workload was never recorded, or more than 256 descendants were seen | **unresolved** | kept |
| Wrapper gone or zombie and every recorded identity is gone | exited | released |
| Record written before this change (no workload): wrapper token mismatch or absent | exited | released |

`unresolved` is a reported state, never a silent one: `live_reservations` returns it as `unresolved` on the record, lane-cap
refusals print `UNRESOLVED: <reason>`, and the snapshot lists `reservation-unresolved: <label> ...` in `blockers`. Release
happens only on positive evidence (all recorded identities gone, or an explicit `release` by the owner). The time limit is not
evidence: a past-TTL holder keeps its lane exactly as before, marked `past_ttl`. A reused pid never counts as the wrapper or as
a member, because every comparison includes the start token; a group is attributed to the wrapper only while the group's leader
pid is still the recorded wrapper or already gone. Queue grants are unchanged: FIFO, non-preemptive, one grant at a time, and
the lane counts are untouched. A grant-time resource check, where one exists, is defense in depth and is never used to certify
that a previous workload ended.

Known limits (not guaranteed):

- Detached work is found only if it was seen while linked to the workload. Work that left the wrapper's process tree and group
  (`setsid`, or a double fork) **before any inspection ran** after it started is invisible, and so is work launched in that
  window after the last refresh. There is no polling; inspections happen when a queue-aware command or the snapshot refresher
  runs. Where the wrapper does not lead its own group, group co-members are not attributed to it.
- The session id (`sess`) is deliberately not used: on macOS it is a kernel pointer, and a shared terminal session would pin
  lanes to unrelated shells. Working-directory or target-dir ownership is not used either (it needs `lsof`).
- More than 256 descendants make the record non-exhaustive and the holder unresolved once the recorded ones end; the owner
  releases it explicitly.
- An unresolved reservation is cleared only by the workload ending or by the owner's explicit `release`. Nothing here signals,
  kills or deletes another session's process or state.
