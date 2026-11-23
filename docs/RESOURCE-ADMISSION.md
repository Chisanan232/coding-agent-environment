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
| Active build/test processes | `ps` pid, ppid, state, elapsed and CPU time, %cpu, rss and the executable name (`cargo`, `rustc`, `clang`, `ld`, `xcodebuild`, ...); never the command line | Up to two heavy lanes, a third as a pilot in the stable and wide tiers (each beyond the first only when the resource budget passes, the third also needing the disk margin; ADR 0017 revisions 2, 7, 11 and 12), counted as a process tree (workload), not per process; see "Idle orphans" below |
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

Up to two admitted heavy lanes, as the resource governor allows (the second only when the resource budget passes; a third runs as a pilot behind `THIRD_LANE_ENABLED`, still only when the budget passes and 160 GiB (floor plus a 60 GiB margin) stays free after all reservations and the request; [ADR 0017](adr/0017-resource-aware-workload-scheduling.md) revision 2); a fourth runs in the wide tier as a pilot behind `FOURTH_LANE_ENABLED` (ADR 0017 revision 14) and needs 200 GiB to stay free after all reservations and the request. Do not start competing Cargo workspace builds, Docker
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
reparenting to launchd. The wrapper's death is therefore never, by itself, a release, and neither is an empty-looking process
table: **absence of evidence is not evidence of exit**.

What is recorded. At reserve and claim time (a zombie pid is refused there as `process-not-running`), and again every time a
reservation is inspected while its wrapper is still running (every reserve, claim, queue advance and the 60-second snapshot
refresher), the reservation stores a bounded workload identity: the wrapper's process group when it leads one (`pgid == pid`),
up to 256 descendant `pid` + start-token pairs (the wrapper's process subtree, their subtrees, and live members of the
wrapper's own group), and a **witness count**, the number of inspections that found the wrapper running after the capture.
Only `pid`, `ppid`, `pgid`, `stat` and `lstart` are read from `ps`, never a command line. A same-label re-reserve merges the
stored members into the new capture, so a member reparented since is not forgotten. The record is mirrored on the queue
entry's holder and refreshed there even after an explicit `release` removed the reservation file of a still-running job.
Records with an implausible shape (a leader whose group is not its own pid, pids at or below 1, booleans as numbers) are
rejected as `reservation-unreadable` problems: left on disk, never trusted, never deleted.

Decision per holder (`holder_status`):

| Situation | State | Lane and bytes |
|---|---|---|
| Wrapper is the same running process (start token matches, not a zombie); witness count goes up | alive | kept |
| Wrapper gone, a zombie, or its pid reused, and a recorded member still runs (start token matches, not a zombie) | **unresolved** | kept, reason reported |
| Holder or process table cannot be inspected (`ps` error, unparseable table) | **unresolved** | kept, reported as a problem (refuses admission) |
| More than 256 descendants were seen | **unresolved** | kept |
| Wrapper gone or zombie, nothing recorded runs, but **no inspection ever saw the wrapper running** (witness count 0) | **unresolved** (`workload-not-witnessed`) | kept |
| Wrapper gone or zombie, every recorded identity gone, and the wrapper was witnessed running at least once | exited | released |
| Record written before this change (no workload): wrapper token mismatch or absent | exited | released |

The exit rule, precisely. A lane is released automatically only when (1) the wrapper is gone, a zombie or a reused pid,
(2) every recorded identity (members and the wrapper's own group) is gone, (3) the record is not truncated, and (4) at least
one inspection saw the wrapper running after the capture. Condition (4) is the cheapest honest positive evidence available: it
means the workload was looked at while it existed, so its descendants had a chance to be recorded. It also means the
following. A job that runs across at least one snapshot cycle (the refresher runs every 60 s) and exits normally is released
on the next inspection. A job that ends **before its first inspection** (shorter than the gap to the next refresher run,
reserve-and-exit, or a wrapper that spawns detached work and ends before anyone looks) stays `unresolved` and keeps its lane
until the owner releases it. That is a deliberate starvation trade-off: a short job whose owner never runs `release` blocks the
lane (and its queue) rather than risk freeing a lane under hidden detached work. Wrappers should run `release --label` (or
`queue release --id`) when their job ends.

`unresolved` is a reported state, never a silent one: `live_reservations` returns it as `unresolved` on the record, lane-cap
refusals print `UNRESOLVED: <reason>`, and the snapshot lists `reservation-unresolved: <label> ...` in `blockers`. The time
limit is not evidence: a past-TTL holder keeps its lane exactly as before, marked `past_ttl`. A reused pid never counts as the
wrapper or as a member, because every comparison includes the start token; a group is attributed to the wrapper only while the
group's leader pid is still the recorded wrapper or already gone. Queue grants are unchanged in kind: FIFO, non-preemptive, one grant per
free lane (see "Concurrent heavy lanes" below). A grant-time resource check, where one exists, is defense in depth and is never used
to certify that a previous workload ended.

Owner escape hatches (neither signals anything). `disk-admission.py release --label L` deletes the reservation file of a direct
reservation. For a claimed queue entry, plain `release` deliberately does **not** free the lane: any same-user process could
otherwise free a running job. `disk-admission.py queue release --id qNNNNNN` is gated the same way, so it needs one of
`--session-id S` (the entry's own recorded session) or `--confirm-ended` (the owner states they verified the job and everything
it started has ended; the result prints the label, holder pid and prior state that were released). It is also refused while
the holder's wrapper is still running, for another user's entry, for a non-matching session id, and when the holder or the
process table cannot be inspected (fail closed: what cannot be checked is not released). Otherwise it removes the entry's
reservation, marks the entry `RELEASED` with evidence `owner-release` (recording what the state was) and leaves the next
`queue advance` to grant the first waiter in order.

Operational note. The 60-second refresher (`refresher-plist`) is only printed, never installed by this tool. Without it,
inspections happen only when a reserve, claim or queue command runs, so a short job that ends before any inspection stays
unresolved until an owner runs `release --label` / `queue release`. Callers of `run` and `claim` should release when their job
ends.

Known limits (not guaranteed):

- Detached work is found only if it was seen while linked to the workload. Work that left the wrapper's process tree and group
  (`setsid`, or a double fork) and was started **after the last inspection that saw the wrapper running** is invisible once the
  wrapper ends: the witness requirement closes this only for a wrapper that was never inspected, not for the interval between
  the last inspection and its exit (up to one refresher period). There is no polling beyond the refresher.
- Where the wrapper does not lead its own group, group co-members are not attributed to it.
- The session id (`sess`) is deliberately not used: on macOS it is a kernel pointer, and a shared terminal session would pin
  lanes to unrelated shells. Working-directory or target-dir ownership is not used either (it needs `lsof`).
- More than 256 descendants make the record non-exhaustive and the holder unresolved once the recorded ones end; the owner
  releases it explicitly.
- An unresolved reservation is cleared only by the workload ending (with the witness) or by the owner's explicit release.

## Declared workload classes and estimates (SPE-150)

Record-only. `run`, `reserve`, `queue enqueue` and `queue claim` accept optional `--class NAME`, `--mem-gib N` and `--cpus N`
(next to the existing `--bytes`). They are stored, validated, shown and measured. **Lane 1 never reads them**: the byte floor,
queue order and the first lane are exactly as before, and a record without them means what it always meant (the whole budget, all
cores). The resource budget below reads them to admit a lane beside a running job.

Classes live in one table, `WORKLOAD_CLASSES` in `scripts/disk-admission.py`. The numbers are conservative estimates (recalibrated by ADR 0017 revision 4 against measured solo peaks),
to be lowered only by a reviewed constants change backed by `disk-admission.py measurements` evidence.

| Class | Memory | Jobs (default / min) | Default disk | Exclusion key |
|---|---|---|---|---|
| `light`, `remote` | none | none | none | none |
| `build-crate` | 2 GiB + 1 GiB per job | 4 / 2 | 10 GiB | Cargo target dir |
| `build-workspace` | 2 GiB + 0.75 GiB per job | 8 / 4 | 40 GiB | Cargo target dir |
| `build-cold-isolated` | 4 GiB + 1.5 GiB per job | 8 / 4 | 60 GiB | Cargo target dir (its own) |
| `container-suite` | 4 GiB | 4 / 2 | 20 GiB | `docker` |
| none (legacy) | whole budget | all cores | as given | none |

Rules. An unknown class is refused (fail closed). `--mem-gib`, `--cpus` and `--bytes` may raise an estimate and are refused when
below the class default; with no class they are recorded as given. `min jobs` is read only by the shadow budget's AMBER report. The exclusion key is resolved without executing anything: `CARGO_TARGET_DIR`, else `build.target-dir` in
`~/.cargo/config.toml` (one small file read), else `./target`, recorded as `cargo-target:<normalised path>`.

Exclusion-key limits (record-only for now, so none changes a decision, but a later change that enforces exclusivity must
close them): only `~/.cargo/config.toml` is read, so `CARGO_HOME`, an extension-less `config`, a workspace or parent
`.cargo/config.toml` and `CARGO_BUILD_TARGET_DIR` are ignored; the path is normalised, not symlink-resolved, so two spellings
of one directory give two keys; and if `tomllib` is missing or the file does not parse the key silently falls back to
`./target`, which fails open for exclusivity. A same-label refresh without a declaration keeps the previously recorded fields.

Record fields (all optional, version stays 1), on a reservation and on a queue entry: `class`, `mem_bytes`, `cpu_slots`,
`test_threads` (equal to `cpu_slots` for now), `excl_keys`, `created` and `owner` (`uid`, `session_id`, `ticket`). A queue entry
already had `created`; a claim copies the fields onto the reservation. Readers ignore unknown keys, but a present field with a bad
type or range (booleans as numbers, zero, negative or huge numbers, malformed class, session or ticket text, more than 8 or
overlong keys, a timestamp without a timezone) makes the file a `reservation-unreadable` or `queue-entry-unreadable` problem:
never trusted and never deleted.

Claim. `class`, `mem_bytes`, `cpu_slots` and `test_threads` are checked at claim like `bytes`: a claim that declares a different
class or estimate from the enqueued entry (including declaring one for a legacy entry, or none for a classed entry) is refused and
keeps the grant. They also join the job fingerprint, but only when something was declared, so a legacy entry's fingerprint and
claimability are unchanged.

Display. `status --json` and `snapshot` add a `workloads` list (label, pid and the fields) only when a live reservation declared
something, so legacy output is byte-identical. `queue status --json` already lists whole entries. Measurement lines carry the
reservation `class`, which `measurements` already groups by.

## Resource budget (SPE-150; enforced for lanes 2 and up by SPE-160)

**Lane 1 is not gated by the budget; every later lane is.** At the three lane sites (`reserve`, `queue advance`, `queue claim`) the
resource budget is computed and recorded beside the decision. The first heavy lane is decided by the disk rules and the lane cap
alone, as before. A lane beside a running job also needs the budget to pass (see "Concurrent heavy lanes"). With
the cap at 1 (the rollback state) no second lane exists, so nothing changes. Nothing is persisted except a `shadow-budget` line in the existing
`queue/receipts.jsonl` (only when the queue directory exists and a host sample was readable); its name is historical.

Where it appears. A `budget` object on the result of `reserve`/`run`, `queue advance` and `queue claim` (absent when no host
sample was requested, so library callers and tests see the old shape); a `budget` section in `snapshot --json` and `status --json`
(state of the occupied budget plus a probe with the reference class `build-crate`); and, on a terminal only, one line starting
`budget (enforced for lanes 2+ only): ...` after `reserve`/`run`. Every record carries `"shadow": "enforced for lanes 2+ only"`
(the key name is historical; the wording is `SHADOW_NOTE`).

Sampling. The host is never sampled inside the lock. A windowed sample (two `memory-admission` samples `SHADOW_WINDOW_S` = 2 s
apart, plus two process-table readings for per-holder rss and cpu) is taken before it, as `queue claim` already does for the
memory gate. `queue advance` samples only when something is waiting. A sample that fails marks the budget `unavailable`: lane 1
is unaffected, and a lane beside a running job is refused (an effective cap of one). An injected free-space reading (tests) takes no
host sample.

Added latency and limits. Each sampled call costs about the 2 s window plus the ps, vm_stat and lsof rounds, and the whole sample
is capped by `SHADOW_DEADLINE_S` = 3 s of wall time: past it the budget is `unavailable` (`host-sample-timeout`) and the
abandoned worker thread, which only reads, is left to finish. Building the budget under the lock is guarded: any `Exception` from
a malformed record or sample (never `BaseException`) yields `{"status": "unavailable", "reason": "budget-computation-failed: ..."}`
and lane 1 proceeds as without it.

Arithmetic (`budget_decision(host, reservations, request, caps)`, pure):

    projected = host_used
              + sum over holders of max(0, est_mem - measured_rss)
              + UNMANAGED_ALLOWANCE (8 GiB) x unmanaged heavy trees
              + est_mem of the request

- `host_used` is (active + wired + compressed pages) from `vm_stat`: an approximation to be calibrated, not a promise.
- `measured_rss` is the holder's process subtree in the sample, counted only when the sample is newer than the record's
  `created`; an older or undated record is charged in full.
- An unmanaged tree is a running heavy-build tree no holder accounts for. Its own usage is already in `host_used`; it adds the
  allowance and is never a refusal by itself.
- Who counts: live reservations (past their time limit or UNRESOLVED too), claimed orphans, and unclaimed unlapsed GRANTED queue
  entries (not the grant being claimed), each at its recorded class cost. A record with no class and no declared estimate is
  legacy: it costs the whole memory budget and all cores. A legacy *request* needs an otherwise free host.
- The verdict `would_admit` needs all of: projected <= RAM x (1 - headroom) (headroom 0.30 GREEN, 0.40 AMBER, whole
  percent so the boundary is exact); sum of `cpu_slots` + request <= cores x 1.0; no `excl_key` shared with a holder; the unchanged
  byte floor (free - reserved - requested >= 100 GiB); band not RED; and count + 1 <= `heavy_lane_cap(tier)`. `fits_if_cap_were_2`
  and `fits_if_cap_were_3` are the same verdict with the cap at 2 and 3, so a reviewer can see when resources alone would have
  allowed more. `resource_reasons` and `cap_reason` say which part failed.
- A running Cargo-class holder (`GRANT_ENV` class) with no recorded `granted_jobs` is charged every core; with one it is charged
  `min(cpu_slots, granted_jobs)`. An unclaimed GRANTED entry is charged its declared `cpu_slots`.
- Bands come from `memory-admission` through two read-only accessors (`budget_view`, `budget_band`); thresholds, latch and
  decisions there are unchanged. RED: any current ENTER refusal or unreadable input. AMBER: not RED but some EXIT bound unmet.
  GREEN: all met. The heavy-lane-count refusal ("one heavy lane at a time") is left out of the band, because the lane count is what
  the budget replaces and every admitted job would otherwise turn the host RED. The load used for banding is corrected for admitted
  work: `max(0, load1 - sum min(cpu_slots_r, measured_cpu_r)) / cores`.
- In AMBER the report shows `granted_jobs` = max(class `min_jobs`, requested / 2) and halved `granted_test_threads`; they reach the
  job as described under "Banded per-job caps".

Known gaps. All constants are guesses until `measurements` backs them. A heavy-named process the process list could not parse
into a tree is not counted as unmanaged. `status` reads records without verification (it takes no lock), so it can briefly show a
holder that has just exited. A 2 s window is shorter than the 10 s `memory-admission check` uses, so its swap-I/O reading is
noisier, so a noisy sample can only close a second lane, never open one. The third lane is approved and shipped (margin PR #293, stable tier PR #294); `THIRD_LANE_ENABLED = False` is the kill switch. Lane 4 is still off and needs a head-protection follow-up before it is enabled.

Known and accepted. The `shadow-budget` lines share `queue/receipts.jsonl` (and its single 1 MiB rotation) with the queue's own
receipts, so a busy shadow stream rotates queue history sooner. The `budget` result key is additive: a caller that compares
whole result dictionaries while requesting a host sample sees the extra key.

## Reservation-aware lane veto (SPE-150)

The memory precondition's heavy-lane veto no longer reads "a heavy-named process exists". `lane_occupancy` in
`scripts/memory-admission.py` counts each running (non-idle) heavy build tree as one lane and matches it to an admitted
reservation: a tree is **reserved** when a reservation's pid, or a pid recorded in its workload, is the tree's root, a member or
an ancestor of its root (the wrapper that exec'd the job); otherwise it is **unmanaged**. The reservations are read, read-only,
from the `admission/reservations` directory next to this tool's state directory (`read_owned`); an unreadable directory only makes
trees look unmanaged.

The veto fires when lanes in use reach the cap, which `memory-admission.py` reads from `heavy_lane_cap()` in `disk-admission.py`
(`read_lane_cap`, attached to the reading as `lane_cap`); it uses the widest cap of any tier because it cannot see the tier, and
falls back to `HEAVY_LANE_CAP` (1) if the disk script cannot be loaded, so a failure there only makes it stricter. At cap 1 nothing
changes: any running tree still refuses and still latches the hysteresis. The reason reads
`... heavy lanes full (1 of 1) [lane budget: 1 of 1 heavy lane(s) in use, 5.0 GiB resident: reserved nda-x (cargo, 5.0 GiB resident,
est 6.0 GiB)]` or `... unmanaged cargo (5.0 GiB resident, no reservation accounts for it)`; the process-name fallback reads
`heavy lanes full (unattributed; cap N)` and still refuses. With a cap of 2, one running tree is no veto, but it adds the EXIT
bounds (swap I/O at most 4 MiB/s, compressor at most 25% of RAM, load per core at most 2.0): a second tree is admitted only into a
quiet host (`a second heavy lane needs ...`). The disk gate's budget stays authoritative for the tier's own cap.

### Distinct `CARGO_TARGET_DIR` per workload (policy for gates)

Cargo serialises on the target directory's build lock, and concurrent builds with different flags or features can rebuild each
other's artifacts, so a shared target is a hidden second resource that the lane cap and the memory budget do not see. Every gate or
build that really runs beside another workload must set its own `CARGO_TARGET_DIR` (for example a per-label directory under the job's
scratch space, class `build-cold-isolated`), and must not rely on a shared `build.target-dir` in `~/.cargo/config.toml` for that
case. The home `~/.cargo/config.toml` deliberately shares one target to bound disk use (per-worktree and per-ticket targets caused
the measured disk incidents), so a distinct target is a byte reservation too: size it in the admission record and delete it with the
workload. Serial builds keep the shared target. The admission record's
`cargo-target:<path>` exclusion key is how the budget refuses two live workloads that share a directory (a lane beside a running
job). Every live job here shares one `cargo-target:/Users/bryant/.cargo/shared-target`, so a second lane admits nothing for ordinary
builds until the trial jobs run as `build-cold-isolated` with their own `CARGO_TARGET_DIR`. The key resolution limits are listed under "Declared workload classes".

A `build-cold-isolated` run also gets `CARGO_INCREMENTAL=0` from `disk-admission.py run` when the job left it unset (`CLASS_ENV`): its target is
one-shot, so incremental sessions are pure growth. Lifecycle, ownership and reclaim scope: [RESOURCE-LIFECYCLE.md](RESOURCE-LIFECYCLE.md), "Cargo build-artifact lifecycle".

## Banded per-job caps (SPE-150)

The budget's band now reaches the job, and only through its environment. The grant is `granted_jobs` and `granted_test_threads` in
the `budget` object of the `run`, `reserve` and `queue claim` results: GREEN gives the full class values (the class defaults, or the
raised estimate), AMBER the reduced values (the larger of the class `min_jobs` and half the request; for every class default that is
exactly `min_jobs`, 2 for `build-crate` and 4 for the workspace classes), RED the class `min_jobs` (SPE-160; it used to be none).

`disk-admission.py run` (and `run --queue-id`) export them to the child just before the exec, and only when a class with an entry in
`GRANT_ENV` was declared and a budget was requested: a budget that is `ok` grants its band's figures, an unavailable budget the job's
declared `cpu_slots` and `test_threads`, and no budget at all (no host sampling requested) changes nothing. Variables: `CARGO_BUILD_JOBS` and
`RUST_TEST_THREADS` for `build-crate`, plus `NEXTEST_TEST_THREADS` for `build-workspace` and `build-cold-isolated`. `light`, `remote`,
`container-suite` and legacy runs (no class) export nothing. A value the job already set is never raised, by this rule exactly: an
unset or empty variable takes the grant; a positive integer is lowered to the grant if it is above it (a value longer than six
digits is lowered to the grant without calling `int()` on it, which would raise past 4300 digits); any other value (0, negative,
`default`, `num-cpus-2`, garbage) has a meaning only the tool knows and is left untouched. The `CARGO_BUILD_JOBS` value actually
exported is recorded as `granted_jobs` on the reservation and on the CLAIMED holder; a Cargo-class holder without one is charged
every core in the budget.
`queue claim` takes the class and the numbers from the queue entry's recorded fields, never from the caller (a claim that declares
something else is refused before any export).

AMBER is the larger of the class `min_jobs` and half the request; RED is `min_jobs`; an unavailable budget is the declared figures.

Admission itself is described in "Concurrent heavy lanes"; a refused run never reaches the exec. Limits: the caps bind only jobs that read these variables (a `cargo` invocation passing `-j` still
wins); the band comes from the 2 s host sample, so it is as noisy as that sample; and the pre-existing `queue claim` subcommand,
which does not exec, applies the grant only to its own process.

## Concurrent heavy lanes (SPE-160)

`heavy_lane_cap(tier, latch)` in `scripts/disk-admission.py` is the one place a lane count is decided: 0 while latched or below
stable, else `HEAVY_LANES[tier]`, plus one in each tier of `THIRD_LANE_TIERS` (stable and wide) only when `THIRD_LANE_ENABLED` (on, as a pilot; ADR 0017 revisions 7 and 12), never above `LANE_CEILING` (3). The table stays the two-lane base, so `THIRD_LANE_ENABLED = False` is the one-line kill switch that restores two lanes in every tier.
The shipped `HEAVY_LANES` value is 2 in stable and wide since the final, separately reviewed flip commit of ADR 0017 revision 2;
reverting that commit restores 1 (rollback procedure in the ADR) and the rules below go dormant.

- Lane 1: the disk tier and latch, the 100 GiB floor, process ownership and queue order. It never reads the budget.
- A lane beside a running job (the count of live reservations, claimed orphans and, in `advance`, unclaimed grants): also a host
  sample, status ok, `would_admit` under the tier's cap, and band GREEN. Otherwise the refusal reason starts `budget: `
  (`budget: no host sample ...`, `budget: unavailable (...)`, `budget: band AMBER: ...` or the failing resources such as
  `cpu: 16 slots in use + 4 requested > 16 cores`, `excl_key overlap with <label>`). A full cap still reads `lane-cap: N of M ...`.
  Order of refusal: cap, then the byte floor (`over-capacity`), then the budget.
- `reserve`: a same-process refresh of its own reservation is exempt from the budget. The queue-ahead rule is unchanged.
- `queue advance`: grants the head, then tries the next entry with the budget recomputed from the single host sample taken before the
  lock, and stops at the first entry that does not fit (the pivot). Grants made in the same call count toward the floor. `ok` also
  covers a `budget:` reason. Bounded backfill (ADR 0017 revisions 9 and 10, enabled: `BACKFILL_ENABLED = True`, `BACKFILL_MAX = 1`, `BACKFILL_GRANT_S = 600`, `BACKFILL_REQUEUE_MAX = 3`; the kill switch is reverting the one-line enabling PR, there is no flag or environment switch; only entries whose waiter declared `requeue_capable: true` are backfilled, so wrappers enqueued before #289 never are) is
  the only overtaking: when the pivot is the oldest waiting entry and its sole refusal is `excl_key overlap with <label>` held by the
  live reservation of a CLAIMED queue entry (not past its time limit, not unresolved; an orphan, unclaimed grant or non-queue
  reservation disqualifies), advance scans later entries in order for the first one that shares no exclusion key with the pivot or any
  skipped entry, has a waiter proven alive (a waiter-less hand-off is never backfilled), passes every lane check (cap, floor, budget
  including exclusion keys) and leaves the pivot admissible once its blocker releases (disk: free minus every current reservation, the
  blocker included, minus candidate and pivot at or above 100 GiB; the pivot's budget with the blockers removed and the candidate
  charged its full declared size: no refusal, band GREEN, within the cap). At most `BACKFILL_MAX` GRANTED or CLAIMED entries may sit
  ahead of the oldest waiting entry (counted from entry states under the lock). The tier, latch, floor and memory gate refuse first,
  exactly as before. A backfilled grant writes a `backfill` receipt (blocked id, blocker id and label, exclusion key, reason
  `head-excl-only`, the disk/lane/memory/CPU figures, in-flight count and cap) before its usual `granted` receipt. While OFF, advance
  grants nothing extra and writes one `backfill-shadow` receipt per (pivot, candidate) per 10 minutes. `out` gains `head_reason`,
  `backfilled` and `backfill_shadow`.
  - Hardening (ADR 0017 revision 9a). The head check also uses the lane cap of the tier that free space falls to after every
    reservation and the candidate (latch included), so a candidate that would shrink the tier below the head's lanes is refused.
  - A backfilled grant expires after `BACKFILL_GRANT_S` (600 s) instead of the usual window.
  - Receipts list every blocker in `blockers`; `blocker_id`, `blocker_label` and `excl_key` keep the first blocker.
  - In OFF mode with a cap above 1, each shadow candidate is judged beside the earlier ones. The shadow never runs the memory-admission
    check (`memory_gate: not-asked`) because that check writes the shared latch.
  - Waiter `ps` answers are cached for one pass.
  - Decision log (ADR 0017 revision 10a). While ON, each candidate evaluated behind an exclusion-key-blocked head writes one
    `backfill-decision` receipt: `id`/`label` (candidate), `blocked_id` (head), `blocker_id`/`blocker_ids`, `outcome` (`granted` or
    `refused`), `reason`, `in_flight`, `cap`. `reason` is one of `qualified`, `key-overlap-pivot`, `key-overlap-skipped`,
    `no-requeue-capability`, `waiter-not-alive`, `disk-floor`, `lane-refusal`, `head-unprotected-disk`, `head-unprotected-lanes`,
    `head-unprotected-lane3-margin`, `head-unprotected-lane4-margin`, `head-unprotected-budget`, `head-unprotected-hysteresis`, `memory-gate`, `in-flight-cap`, `other`. `head-unprotected-hysteresis` is a grant refused only because head protection had less than `BACKFILL_HYSTERESIS` (4 GiB, ADR 0017 revision 13) of disk above the floor: a new backfill grant needs floor + 4 GiB, while the claim-time recheck keeps the plain floor so free-space jitter between grant and claim does not revoke it (a larger drop still does). It only tightens; backfill still needs about three jobs' worth of headroom. `head-unprotected-lane3-margin` is a head whose own lane 3 would stay under the 160 GiB margin while the disk floor, the lane count and the tier all pass (see "Third lane disk margin"). An unchanged (head, candidate, outcome, reason, blocker set) is
    not written again for 10 minutes. Logging only: it changes no decision, and a failure of the logger itself is swallowed. `other` is
    never emitted by the shipped code paths; it is how an unknown reason would be recorded, so the vocabulary stays closed.
- `queue claim` of a backfilled grant (a GRANTED entry with an earlier unlapsed waiting entry) re-checks head protection against the
  oldest waiting entry.
  - If protection no longer holds, the grant is withdrawn: the entry becomes `EXPIRED`, a `backfill-revoked` receipt records the kind
    (`head-not-excl-only` or `head-unprotected`) and the blocked id, and the claim is refused with `backfill-revoked: ...`. Re-enqueue
    the job (the wrappers do this themselves, below).
  - With no host sample the claim is refused with `backfill-unverifiable: ...` and the grant is kept.
  - Ordinary grants are not re-checked.
  - The revoke runs before the reservation is written and before the job is exec'd, and advance never expires a `CLAIMED` entry, so a
    running job is never stranded by a revoke or by the 600 s window.
- `queue enqueue --again --requeue-of <id>` (ADR 0017 revision 9a, SPE-176) replaces an `EXPIRED` entry that lost a backfilled grant
  at that entry's original FIFO position.
  - **Accepted only when, under the lock:** the job, session and waiter (pid and start token) are the same; the entry has not already
    been replaced; the chain is below `BACKFILL_REQUEUE_MAX` = 3; and the receipts (live log and its rotated backup) show a `backfill`
    followed by `backfill-revoked`, or by `expired` with `was: GRANTED`, with no `claimed`.
  - **Otherwise refused** with `not-requeueable: ...`: no entry, counter or receipt is written. A repeated request returns only the
    old entry's own live replacement (`existing`, with `requeue_of`). Any other live entry of the same job is refused rather than
    returned, so a wrapper never adopts an entry another wrapper waits on.
  - **The new entry** gets a fresh id and seq (ids are never reused) plus `order` (the inherited position), `requeue_of` and
    `requeues`. Its `enqueued` receipt carries the same fields and the `evidence`.
  - **Order key.** Every ordering decision (listing, advance, in-flight count, the backfilled-grant test at claim, the recovery
    verifier) uses `(order or seq, seq)`.
  - **Grants made before the replacement existed.** An in-order grant made while the replacement did not exist yet is not counted as
    having overtaken it.
- `queue enqueue --requeue-capable` (ADR 0017 revision 9a, SPE-176) declares that the waiter re-queues a lost backfilled grant itself.
  - **Stored field.** The entry gets the additive field `requeue_capable: true`. The entry reader accepts only a literal `true`. The
    field is absent on older entries, is not in the fingerprint, and is not in the grant object. A `--requeue-of` replacement inherits
    it.
  - **Backfill gate.** Backfill (ON, or OFF in shadow) never grants a candidate without the field. Such a candidate is skipped like a
    refused one: its keys still hold later same-key entries behind it.
  - **Skip receipt.** The skip is recorded once per (pivot, candidate) per 10 minutes as a `backfill-shadow` receipt with
    `skipped: no-requeue-capability`. It is written in ON and OFF mode alike. When you review shadow evidence before the ON flip:
    - filter out receipts with `skipped` set: only the rest show what backfill would have granted;
    - look at decisions over more than one queue shape.
  - **Who passes the flag.** `heavy-run` and `la-cargo` pass it only when their runner file contains the `'--requeue-capable'` option
    string. They read the file as text and never run it, so an older runner is never handed an unknown flag.
  - **First enqueue only.** The wrappers pass it on the first enqueue. A `--requeue-of` call omits it, because the replacement
    inherits the field. A re-queue therefore cannot be refused just because the checkout moved back to an older runner during a long
    wait.
- `heavy-run` and `la-cargo` use `--requeue-of` when their polled entry turns `EXPIRED`.
  - They accept the reply only when it names the old id in `requeue_of`. On success they print
    `backfill grant revoked; re-queued as qNNNNNN` (or `backfill grant lapsed unclaimed; ...` for a lapse), move their registration
    and final cancel to the new id, and keep waiting within the original deadline.
  - After `REQUEUE_MAX` = 3 re-queues they exit 77, naming the limit.
  - When the runner refuses (an ordinary expiry, or an older runner without the flag), they print the unchanged
    `queue entry <id> is EXPIRED; refusing.` and exit 77. Any reply without the matching `requeue_of` is handled the same way.
  - Timing: one poll iteration is about 390 s worst case (15 s sleep, advance and status capped at 120 s each, and a refused claim
    bounded by the 120 s memory gate and two 5 s lock waits), within the 600 s backfill window. A grant that still lapses unclaimed,
    for example because its claims keep being refused, is re-queued at its position anyway.
- Exclusion keys (`excl_keys`). A new `cargo-target:` key follows cargo's own target-dir order and is realpath-canonical:
  1. `--target-dir` in the job argv, before any `--`;
  2. `CARGO_TARGET_DIR`;
  3. `CARGO_BUILD_TARGET_DIR`;
  4. `build.target-dir` from the nearest `.cargo/config[.toml]` walking up from the working directory, then `$CARGO_HOME`'s
     (default `~/.cargo`);
  5. otherwise `./target`.

  Every comparison, stored keys included, goes through identity tokens. The tokens are the raw string, the folded realpath and, when
  the directory exists, `(st_dev, st_ino)`. Two keys clash when they share any token, so no string-equal clash is lost and a directory
  created or removed mid-decision still clashes. So `/tmp` and `/private/tmp`, symlinks and trailing slashes all clash as one key. Stored keys and
  fingerprints are unchanged. The corrected order can also remove a clash: a job with its own `--target-dir` or repository config no
  longer shares the home config's key. Entries queued earlier keep their old keys until they drain. `heavy-run` and `la-cargo` exit
  77 when a revoked grant turns `EXPIRED`; they do not re-enqueue.
- `queue advance` and memory (SPE-175): the CLI grants the head only when the SPE-117 memory gate admits, because `queue claim` runs that
  gate and a grant it would refuse only holds a lane for its whole window and then lapses unclaimed. The gate takes 10 s or more, so it
  runs outside the lock and only when the head otherwise fits (a lane is free, the floor holds, the budget allows); otherwise it is not
  asked. A refusal grants nothing, leaves every entry `QUEUED` in FIFO order, still expires lapsed grants and waits, and reports reason
  `memory-gate: <first line of the gate verdict> ; <its first reason line>` (capped at 400 characters) (counted as `ok`). The library function stays memory-free unless given `memory_fn`.
- A grant is not a start. An entry is running only in state `CLAIMED` with `holder.workload.observed` true and a live workload; report
  `GRANTED` as "granted, not started" together with the claim refusal text.
- `queue claim`: re-checks the budget under the lock; a refusal keeps the grant.
- A job that was claimed before this change has no `granted_jobs`; as a Cargo-class holder it is charged every core, so it holds the
  second lane closed until it finishes.
- Pressure: AMBER or RED admits no new concurrent job and nothing running is signalled.
- Trial evidence: `disk-admission.py queue trial-report --since ISO [--until ISO] [--baseline-since ISO --baseline-until ISO]
  [--json]` (read-only, writes nothing, takes no lock). Measurement lines also carry `swap_used_bytes`. A third lane needs its PASS
  output, with the criteria of ADR 0017 revision 3 (exact-N stage gates, overlap-window disk margin, running-only deduplicated
  `fits_if_cap_were_3`; the window-wide minimum free disk is reported, not gated) and the rollback procedure of revision 2.

## Third lane disk margin

A third heavy lane (stable or wide tier, ADR 0017 revisions 11 and 12) needs, besides the budget, `free - reserved - request >= 160 GiB` (`HEAVY_FLOOR` plus
`THIRD_LANE_MARGIN`, 60 GiB), with every live, granted and orphaned reservation counted once. It applies at `reserve` (a same-process refresh
is exempt), `queue advance`, `queue claim`, backfill candidates and head protection (the head's own lane 3 once its blockers release). The
refusal reads `budget: lane 3 needs N GiB free after all reservations and this request, 160 GiB required`. Lanes 1 and 2 and lane 4 are
unaffected. In the stable tier (100 to under 200 GiB free) the margin leaves room for a third lane only while all reservations plus the request stay within 40 GiB; the recently measured 160 to 184 GiB free with 32 to 55 GiB held and 25 to 40 GiB requested admits none. `scripts/admission-monitor.py` does not
model the margin; its `callers` listing reports the effective stable cap (the table plus the third lane where `THIRD_LANE_TIERS` lists stable).

## Four heavy lanes (enabled as a monitored pilot)

The fourth lane (SPE-176, ADR 0017 revision 8) is code behind `FOURTH_LANE_ENABLED` in `scripts/disk-admission.py`. It shipped off and was
enabled as a monitored pilot by ADR 0017 revision 14 (`True`), after its head-protection prerequisite (revisions 11 and 12) was closed by
revision 13a: `_protects_head` refuses a backfill candidate that would leave the blocked head short of `FOURTH_LANE_MARGIN` once it is the
fourth job (logged as `head-unprotected-lane4-margin`) or that would push an unstamped head into lane 3 or 4. There is no runtime switch:
rollback is a reviewed pull request that reverts the constant, and with it off `heavy_lane_cap` and every refusal text return to the
three-lane behaviour (three lanes in the stable and wide tiers; the fourth lane is wide-only). Running jobs are never signalled either way.

With it on (and `THIRD_LANE_ENABLED`), the wide tier allows four lanes (`FOUR_LANE_CEILING`). Lane 4 additionally needs, at `reserve`,
`queue advance` (before the memory probe, so it cannot arm the latch) and `queue claim`: free space minus all reservations minus the
request at or above 200 GiB, no unmanaged process tree, and no holder past its time limit or unresolved. The refusal reads
`budget: lane 4 needs <each unmet condition>`. Lanes 3 and 4 go only to queue entries carrying `lane_protocol: 4` (written by `queue
enqueue`); an entry without it, or with a malformed value, waits with `budget: lane N needs a stamped runner (head qX)` and, being the head,
blocks the queue. `fits_if_cap_were_4` appears beside `fits_if_cap_were_3` in the shadow budget.

### Live verification procedure (never enqueues, never loops the real memory gate)

All read-only; none of it creates a queue entry, takes a reservation or runs the memory gate's latch-writing check.

1. Read `snapshot.json` next to the admission state file (published by the existing snapshot refresher; `python3 scripts/disk-admission.py
   snapshot --check-fresh` is its read-only freshness check): `heavy_lanes_allowed` is 3 in the stable tier (`THIRD_LANE_ENABLED`) and 3 in the wide tier while the fourth lane is off, 4 in the wide tier when it is on, and `budget.reference.fits_if_cap_were_4` is the shadow figure. Do not run `snapshot --json` or `status --json` for this: the
   first writes the file, the second does not report lanes.
2. In a Python shell: `import importlib.util as u; s=u.spec_from_file_location('m','scripts/memory-admission.py'); m=u.module_from_spec(s);
   s.loader.exec_module(m); print(m.read_lane_cap())` (it only loads `disk-admission.py` and reads the constants; it takes no sample).
3. Confirm from real work: after real jobs have run, look in the queue receipts for `granted` and `shadow-budget` lines (their `budget`
   carries `fits_if_cap_were_4`), and for a `budget: lane 4 needs ...` reason in `queue status` of a waiting entry. Do not enqueue a
   job, and do not run `memory-admission.py check`, to produce evidence: wait for real work.

## Any other heavy command: `scripts/heavy-run`

`scripts/heavy-run --label L --class C --bytes N [--max-wait-s W] -- COMMAND` is the supported entry point for a campaign's heavy
command (a Cargo workspace gate, a container suite). It runs `scripts/disk-admission.py` from its own checkout, declares the class,
runs the command at once when a lane is free, and otherwise enqueues the same class and argv, polls, claims with `run --queue-id` and
releases the entry when the command exits. It never starts the command outside a lane; exit 75 means not admitted in time (command
not started), 77 refused. A wrapper that calls an old runner directly is the defect it replaces: a runner from before ADR 0017
revision 2 writes a reservation with no class, which the budget must charge as an unknown workload (the whole resource budget and
every core), and it cannot see queued entries, so it starts ahead of jobs that are waiting. Unknown workloads stay charged
conservatively; declare `build-workspace` for a Cargo workspace test or clippy run. The runner version a session uses is the checkout
its wrapper points at: check `grep '^HEAVY_LANES' <checkout>/scripts/disk-admission.py` (the cap is 1 before revision 2).

## Local Agent builds through the queue: `la-cargo` (SPE-174)

`scripts/la-cargo` is the tracked source of the wrapper installed at `~/.local/bin/la-cargo` (before SPE-174 it existed only as the
installed file). It stays the entry point for every Local Agent cargo build and keeps its own limits unchanged: the dedicated target
`~/.cargo/targets/local-agent`, the 60 GiB artifact budget with the incremental-cache prune, the exclusive lock
`~/.cargo/targets/local-agent.lock`, the free-space gates (warn < 100, refuse < 75 with exit 75, stop < 50 GiB) and `--light` /
`--status`. What it adds is one lane reservation per heavy build:

| Situation | Behaviour |
|---|---|
| Heavy build, no marker | Inside the heavy lock: `disk-admission.py run --class build-workspace --bytes 40 GiB` with cargo as the job. A free lane runs it at once; otherwise `queue enqueue`, `queue advance` and `queue status` are polled (15 s) until the entry is granted, then `run --queue-id`. The lane is released (`release --label` or `queue release --id --session-id`) when cargo exits. |
| Already under a lane | `CAE_ADMISSION_HOLDER=<label>:<pid>` is exported by `run`, `run --queue-id` and printed as `holder_marker` by `queue claim`. la-cargo honours it only when the pid is itself or an ancestor, and then charges nothing (lock and gates still apply). A supervisor using `queue claim --pid` must export the printed value for the job it runs. |
| `--light`, `--status` | Never contact the runner. |
| Runner file missing (checked before spawning) | Fails closed: exit 77 and a `REFUSED runner-missing` log line, so a moved checkout cannot silently run unaccounted builds under disk pressure. `LA_CARGO_ALLOW_UNADMITTED=1` is the explicit opt-out (warning plus `UNADMITTED` log line, then the pre-SPE-174 behaviour: lock, gates and artifact budget still hold). |
| Runner present but refuses for the whole wait (`LA_CARGO_QUEUE_WAIT_S`, default 3600), cannot queue, or fails before cargo | Exit 77 (a runner failure keeps its own code) and cargo never starts: fail closed. The wait is cancelled with `queue cancel`. |

Effects an owner should know. (1) The runner's disk floor (`free - reserved - bytes >= 100 GiB`, bytes 40 GiB for `build-workspace`)
is stricter than la-cargo's own: heavy builds are refused below about 140 GiB free instead of 75. (2) The runner only lowers
`CARGO_BUILD_JOBS` (8 from la-cargo): AMBER gives 4, RED the class minimum; a non-integer `LA_CARGO_JOBS` is left alone and the holder is
charged every core. (3) The exclusion key is `cargo-target:` plus the resolved `CARGO_TARGET_DIR`, which la-cargo sets. (4) Plain `run`
is refused while anything is queued, so a build that finds a lane busy queues in order. (5) `LA_CARGO_RUNNER` overrides the runner
path (tests); the default is the merged checkout's `scripts/disk-admission.py`. (6) A runner without the SPE-174 marker (an older
checkout) still works but cannot tell a nested la-cargo that it is already charged, so a build wrapped in an outer `run` is charged twice
until the checkout contains this change.

Owner install (the agent never writes `~/.local/bin/la-cargo`). Baseline sha256 of the installed file at SPE-174:
`663b67bfffd612ee9d176c02d0b485dd1549378395dc114ab38bbe4c96f767bc`; the new sha256 is in the PR description. From the merged checkout:

```bash
cp -p ~/.local/bin/la-cargo ~/.local/bin/la-cargo.pre-SPE-174
install -m 0755 scripts/la-cargo ~/.local/bin/la-cargo
shasum -a 256 scripts/la-cargo ~/.local/bin/la-cargo      # the two lines must match
la-cargo --status
```

Rollback: `install -m 0755 ~/.local/bin/la-cargo.pre-SPE-174 ~/.local/bin/la-cargo` (its sha256 is the baseline above). Run
`disk-admission.py status --json` and `queue status --json` afterwards: a killed build that never released leaves an UNRESOLVED
holder that `release --label la-cargo-<pid>` (or `queue release --id qNNNNNN --confirm-ended`) clears once cargo has ended.

## Dead waiters and refused claims (SPE-176)

**Waiter.** `queue enqueue --waiter-pid PID` is optional and names the supervising wrapper that will claim the entry (`heavy-run` and
`la-cargo` pass their own pid). The pid must be a live process of this user that is the enqueue process, its parent or an ancestor, so
a session cannot declare an unrelated process. The entry stores `waiter: {pid, start_token}` (the same pid-reuse-safe identity as a
holder). Without the flag there is no `waiter` field, and such a handoff entry, which someone claims later by hand, is never
auto-cancelled.

**Cancellation.** `queue advance`, under the queue lock and before it grants, cancels a QUEUED, ELIGIBLE or GRANTED entry whose waiter is
provably gone: no such pid, or the pid now has a different start token. The receipt is `cancelled` with `evidence: waiter-exited`, the
granted marker is dropped, and a granted lane and its bytes are free for the next entry in the same advance. A live waiter changes
nothing, an uninspectable one is treated as alive, and a CLAIMED entry is never touched by this rule (the holder rules apply).

**Claim refusals.** A `queue claim` refused with exit 77 appends a `claim-refused` receipt with `kind` one of `memory-gate`, `floor`,
`lane`, `budget` or `other` (derived from the reason prefix; no free text, no argv). At most one receipt is written per entry and kind
per 10 minutes, judged from the newest matching line in `receipts.jsonl` (which still rotates at 1 MiB), and the entry itself is not
changed, so the grant is kept as before.

Notes: the stored `waiter.start_token` is a UTC `ps lstart` string (not `process_token`'s local-time format), so a timezone or DST
change cannot make a live waiter look replaced; waiter pids at or below 1 are refused. Cancellation is skipped while the disk tier
cannot be measured (advance returns early before it). Enqueue that dedupes to an existing live entry returns that entry and does not
refresh its waiter.

## Bounded hand-off grants (SPE-176, ADR 0019)

A queue entry enqueued without `--waiter-pid` is a manual hand-off. `queue advance` still grants it strictly in order and in its place, but
the grant window is `HANDOFF_GRANT_S` (1200 s, 20 min) instead of the 3600 s default, because observed grant-to-claim p99 is 29 min and an
absent owner must not hold the only usable lane for a full hour. The granted receipt, the `granted/<id>` marker and `grant.expires` all carry
the same shorter expiry; the grant object stays exactly `{at, expires}`. When the window lapses the entry expires through the existing
EXPIRED path and the owner re-enqueues (joining the end of the line). Entries with a declared waiter keep `min(grant_s, 4 h)`, and an
explicit smaller `--grant-s` still wins for either kind. Lane and byte accounting for an unclaimed grant is unchanged.

## Bounded hand-off grants (SPE-176, ADR 0019)

A queue entry enqueued without `--waiter-pid` is a manual hand-off. `queue advance` still grants it strictly in order and in its place, but
the grant window is `HANDOFF_GRANT_S` (1200 s, 20 min) instead of the 3600 s default, because observed grant-to-claim p99 is 29 min and an
absent owner must not hold the only usable lane for a full hour. The granted receipt, the `granted/<id>` marker and `grant.expires` all carry
the same shorter expiry; the grant object stays exactly `{at, expires}`. When the window lapses the entry expires through the existing
EXPIRED path and the owner re-enqueues (joining the end of the line). Entries with a declared waiter keep `min(grant_s, 4 h)`, and an
explicit smaller `--grant-s` still wins for either kind. Lane and byte accounting for an unclaimed grant is unchanged.

## Stall detection (SPE-176, ADR 0018)

`scripts/admission-monitor.py tick` reads the queue, the latch file and the process table and reports whether queued work is globally
stalled and why. It is observe-only unless started with `--act`, which allows one bounded recovery verb (`queue advance`) and the two guarded
recoveries below. It never resets the latch, changes a threshold or deletes, and it signals nothing but one SIGTERM to an obsolete wrapper. `status` shows the last result, `callers` lists admission runners found on disk by
protocol, and `plist` prints the launchd agent without installing it; installing it (`launchctl bootstrap gui/$(id -u) <plist>`) is an
owner-run step. A grant is not a start: only a claim, a release or CPU growth of a claimed workload counts as progress.

## Guarded recovery (wired under `tick --act`)

`scripts/admission-recovery.py` (SPE-176, ADR 0019 sections 3, 5 and 6) is the library the monitor's `tick --act` calls; nothing else does, and
`--observe-only` and a plain `tick` never reach it. It acts only through audited, conclusive-evidence guards.

- `abandoned_unstarted` / `do_cancel`: a GRANTED entry with no holder, release or workload, whose waiter is provably gone (the scheduler's own
  `_waiter_gone`; uninspectable or malformed is not gone) or absent, whose owner mechanism is `gone` or `refused-permanently`, older than the 5 min
  notice threshold, and that a fresh re-read still agrees with, is cancelled through `queue cancel --id` only (no session id, no end
  confirmation, no state-file edit). `refused-permanently` also needs the newest scheduler receipt for the entry to be a `claim-refused` of a permanent
  kind made after the grant; a later receipt of any kind, or a transient kind (`memory-gate`, `floor`, `lane`, `budget`, `other`), voids it. An
  `unknown` owner never cancels. `verify_recovery` then reports `pending`, `verified` or `failed` from scheduler status; it never assumes success.
- `obsolete_wrapper` / `terminate_wrapper`: a wrapper or waiter (real int pid above 1, not this process or its parent) whose entry is terminal
  and names this very pid and start token as its `waiter` (a terminal entry with no waiter, or another one, is refused: a forged record cannot aim a signal) or is gone, with matching pid, start token, uid, process group and parent, a matching executable identity, no workload by evidence and no child or
  detached workload, gets exactly one SIGTERM, a bounded wait, and a `failed-to-terminate` report on survival. Never SIGKILL, never a workload.
  A terminal entry only proves the queue no longer needs the pid, so identity is checked too: a wrapper that exec'd the workload keeps its pid and token.
- Wrapper registry: when `heavy-run` or `la-cargo` enqueue they record `wrappers/<qid>.json` (0600, in a 0700 directory beside the admission state,
  or `$ADMISSION_WRAPPERS_DIR`) holding exactly `{qid, pid, start_token (the queue waiter token), pgid, ppid, exe_id, kind, created}`, where `exe_id` is
  the interpreter and script basenames only (never argv), and remove it when they exit. Writing is best effort: any failure is ignored and never
  fails the job. `read_registry` refuses a symlink, a foreign owner, loose modes, an off-schema or oversized record and a pid whose live start token
  differs. A stale file (`kill -9`) is never trusted, and every monitor tick, observe-only or act, unlinks (never signals) records whose pid is gone, whose start token changed or that are older than 7 days: regular files of ours only, at most 50 per tick, one count-only `prune` receipt per batch. Registration uses `ps` lookups bounded to 1 s each; a slow `ps` means no record, which is never terminated, and a signal during registration still cancels or releases the queue entry (the registry step sits inside the wrapper's cleanup `try`, and its own removal is the last cleanup step).
- Monitor bindings (the library's own defaults still refuse): `registered_fn` is that registry; `exe_fn` reduces `ps` args to the same two basenames in
  memory; `holds_workload_fn` is true when anything else lives in the wrapper's group or below it, when an open `.cargo-lock` is seen, or when the
  process table or `lsof` cannot be read (uncertain is holding); `owner_exec_fn` is `gone` only when the entry's declared waiter is provably gone,
  or, for a hand-off entry, when a session record says the owning session ended (none is read yet, so such an entry stays `unknown` and is never
  cancelled), `live` when the waiter or session is alive, else `unknown`.
- Bounds and switches: at most one recovery action per tick and three per rolling 30 minutes (the action is recorded and persisted before it is
  taken, so a crash still counts); a wrapper is only a candidate after its entry has been terminal with that same live wrapper for two ticks
  (120 s), so a job that is just exiting is never signalled; one SIGTERM and a 10 s wait, then `failed-to-terminate` is reported and nothing more is
  sent; the file `recovery.disabled` in the monitor directory (any file) turns the whole act-mode recovery, verification included, into a no-op; a
  monitor directory that is a symlink, not ours or not 0700 does the same (`recovery.state` in `status.json`: `disabled`, `unsafe-monitor-dir`,
  `recovery-bound`, else `ok`).
- Effect and follow-up: each action writes receipts, one enum-only owner-outbox line and one strict session-notify notice (`obsolete-wrapper-terminated`,
  `abandoned-unstarted-cancelled` or `failed-to-terminate`), deduplicated by entry id, pid and start token. A later tick runs `verify_recovery` exactly
  once to a final state (`verified` or `failed`; `pending` while the next eligible entry has not started) and keeps the incident in `state.json`
  and `status.json` (`recovery_incidents`) across restarts. A wrapper with a live child or detached workload is listed in `recovery.reported`, never signalled.
- One exclusive flock (`recovery.lock` in the monitor dir, which must be a private 0700 real directory owned by us) serialises recoveries and the
  verify receipt write; the second recovery gets `busy`. Every decision and action is a closed-schema 0600 line in `recovery-receipts.jsonl`,
  rotated at 1 MiB.
- `load_terminate_allowlist` / `terminate_allowed_for_workload` prepare a broader, closed-schema termination policy. The file is absent by
  default, the policy is disabled, and neither function is used by the code above. Enabling it needs separate owner approval.

`default_env` alone still leaves the owner-execution, registry, executable and workload readers uninspectable, so without the monitor's bindings nothing is abandoned or obsolete.
