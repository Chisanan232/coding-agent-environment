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
group's leader pid is still the recorded wrapper or already gone. Queue grants are unchanged: FIFO, non-preemptive, one grant at
a time, and the lane counts are untouched. A grant-time resource check, where one exists, is defense in depth and is never used
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
(next to the existing `--bytes`). They are stored, validated, shown and measured. **No admission decision reads them**: the lane
cap (1), the byte floor, queue order and grants are exactly as before, and a record without them means what it always meant
(the whole budget, all cores). The shadow budget below reads them for reporting only.

Classes live in one table, `WORKLOAD_CLASSES` in `scripts/disk-admission.py`. The numbers are guesses, conservative on purpose,
to be lowered only by a reviewed constants change backed by `disk-admission.py measurements` evidence.

| Class | Memory | Jobs (default / min) | Default disk | Exclusion key |
|---|---|---|---|---|
| `light`, `remote` | none | none | none | none |
| `build-crate` | 2 GiB + 1 GiB per job | 4 / 2 | 10 GiB | Cargo target dir |
| `build-workspace` | 4 GiB + 1.5 GiB per job | 8 / 4 | 40 GiB | Cargo target dir |
| `build-cold-isolated` | 4 GiB + 1.5 GiB per job | 8 / 4 | 60 GiB | Cargo target dir (its own) |
| `container-suite` | 12 GiB | 4 / 2 | 20 GiB | `docker` |
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

## Resource budget in shadow (SPE-150)

**Shadow: not enforced.** At the three lane sites (`reserve`, `queue advance` "lane occupied", `queue claim` lane cap) the
resource budget is computed and recorded beside the decision, and the existing `HEAVY_LANES` check (1 everywhere) still decides
alone. No admit, refusal, grant order or reason string changes. The record is for measurement: it shows when resources would have
allowed more than one lane. Nothing is persisted except a `shadow-budget` line in the existing `queue/receipts.jsonl` (only when
the queue directory exists and a host sample was readable).

Where it appears. A `budget` object on the result of `reserve`/`run`, `queue advance` and `queue claim` (absent when no host
sample was requested, so library callers and tests see the old shape); a `budget` section in `snapshot --json` and `status --json`
(state of the occupied budget plus a probe with the reference class `build-crate`); and, on a terminal only, one line starting
`budget (shadow: not enforced): ...` after `reserve`/`run`. Every record carries `"shadow": "shadow: not enforced"`.

Sampling. The host is never sampled inside the lock. A windowed sample (two `memory-admission` samples `SHADOW_WINDOW_S` = 2 s
apart, plus two process-table readings for per-holder rss and cpu) is taken before it, as `queue claim` already does for the
memory gate. `queue advance` samples only when something is waiting. A sample that fails marks the budget `unavailable` and never
alters the outcome. An injected free-space reading (tests) takes no host sample.

Added latency and limits. Each sampled call costs about the 2 s window plus the ps, vm_stat and lsof rounds, and the whole sample
is capped by `SHADOW_DEADLINE_S` = 3 s of wall time: past it the budget is `unavailable` (`host-sample-timeout`) and the
abandoned worker thread, which only reads, is left to finish. Building the budget under the lock is guarded: any `Exception` from
a malformed record or sample (never `BaseException`) yields `{"status": "unavailable", "reason": "budget-computation-failed: ..."}`
and the lane decision proceeds as without it.

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
- The shadow verdict `would_admit` needs all of: projected <= RAM x (1 - headroom) (headroom 0.30 GREEN, 0.40 AMBER, whole
  percent so the boundary is exact); sum of `cpu_slots` + request <= cores x 1.0; no `excl_key` shared with a holder; the unchanged
  byte floor (free - reserved - requested >= 100 GiB); band not RED; and count + 1 <= `MAX_CONCURRENT[tier]` (1 wherever a lane
  exists, so `would_admit` is `false` for a second job today). `fits_if_cap_were_2` is the same verdict with the cap at 2, so a
  reviewer can see when resources alone would have allowed more. `resource_reasons` and `cap_reason` say which part failed.
- Bands come from `memory-admission` through two read-only accessors (`budget_view`, `budget_band`); thresholds, latch and
  decisions there are unchanged. RED: any current ENTER refusal or unreadable input. AMBER: not RED but some EXIT bound unmet.
  GREEN: all met. The heavy-lane-count refusal ("one heavy lane at a time") is left out of the band, because the lane count is what
  the budget replaces and every admitted job would otherwise turn the host RED. The load used for banding is corrected for admitted
  work: `max(0, load1 - sum min(cpu_slots_r, measured_cpu_r)) / cores`.
- In AMBER the report shows `granted_jobs` = max(class `min_jobs`, requested / 2) and halved `granted_test_threads`. These are
  reported only; no job is told to run smaller.

Known gaps. All constants are guesses until `measurements` backs them. A heavy-named process the process list could not parse
into a tree is not counted as unmanaged. `status` reads records without verification (it takes no lock), so it can briefly show a
holder that has just exited. A 2 s window is shorter than the 10 s `memory-admission check` uses, so its swap-I/O reading is
noisier; the band is advisory here for that reason. Enforcement, a cap above 1 and degrading jobs are separate, owner-approved
changes.

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

The veto fires when lanes in use reach `HEAVY_LANE_CAP` (1, mirroring `HEAVY_LANES` for the stable and wide tiers; a test pins the
equality). At cap 1 nothing changes: any running tree still refuses, still latches the hysteresis, and nothing is newly admitted
or refused. The first sentence of the reason is unchanged and a budget part is appended, for example
`... one heavy lane at a time [lane budget: 1 of 1 heavy lane(s) in use, 5.0 GiB resident: reserved nda-x (cargo, 5.0 GiB resident,
est 6.0 GiB)]` or `... unmanaged cargo (5.0 GiB resident, no reservation accounts for it)`. With a cap above 1 (not configured; there is
no `CAE_HEAVY_LANES` knob yet) one running tree would be neither a veto nor a latch, and a second workload would be decided by
the budget arithmetic above. The verdict's other fields are unchanged.

### Distinct `CARGO_TARGET_DIR` per workload (policy for gates)

Cargo serialises on the target directory's build lock, and concurrent builds with different flags or features can rebuild each
other's artifacts, so a shared target is a hidden second resource that the lane cap and the memory budget do not see. Every gate or
build that really runs beside another workload must set its own `CARGO_TARGET_DIR` (for example a per-label directory under the job's
scratch space, class `build-cold-isolated`), and must not rely on a shared `build.target-dir` in `~/.cargo/config.toml` for that
case. The home `~/.cargo/config.toml` deliberately shares one target to bound disk use (per-worktree and per-ticket targets caused
the measured disk incidents), so a distinct target is a byte reservation too: size it in the admission record and delete it with the
workload. Serial builds keep the shared target. The admission record's
`cargo-target:<path>` exclusion key is how a future enforcing change refuses two live workloads that share a directory; today it is
recorded and reported by the shadow budget only. The key resolution limits are listed under "Declared workload classes".

## Banded per-job caps (SPE-150)

The budget's band now reaches the job, and only through its environment. The grant is `granted_jobs` and `granted_test_threads` in
the `budget` object of the `run`, `reserve` and `queue claim` results: GREEN gives the full class values (the class defaults, or the
raised estimate), AMBER the reduced values (the larger of the class `min_jobs` and half the request; for every class default that is
exactly `min_jobs`, 2 for `build-crate` and 4 for the workspace classes), RED none.

`disk-admission.py run` (and `run --queue-id`) export them to the child just before the exec, and only when all of these hold: a
class with an entry in `GRANT_ENV` was declared; the budget status is `ok`; the grant is positive. Variables: `CARGO_BUILD_JOBS` and
`RUST_TEST_THREADS` for `build-crate`, plus `NEXTEST_TEST_THREADS` for `build-workspace` and `build-cold-isolated`. `light`, `remote`,
`container-suite` and legacy runs (no class) export nothing, and so does an unavailable or RED budget. A value the job already set is
never raised, by this rule exactly: an unset or empty variable takes the grant; a positive integer is lowered to the grant if it is
above it; any other value (0, negative, `default`, `num-cpus-2`, garbage) has a meaning only the tool knows and is left untouched.
`queue claim` takes the class and the numbers from the queue entry's recorded fields, never from the caller (a claim that declares
something else is refused before any export).

AMBER is the larger of the class `min_jobs` and half the request (RED and unavailable export nothing). In the shadow era RED and
an unavailable budget therefore leave the job's environment alone, and the job runs as it would have. **Note for the later
enforcing change:** when the budget is enforced, RED must still export `min_jobs` (and its test threads) rather than nothing, or a
RED host would be given a job with no cap at all.

Nothing about admission changes: the lane cap stays 1 and admit or refuse is decided by the lane check exactly as before; a refused
run never reaches the exec. Limits: the caps bind only jobs that read these variables (a `cargo` invocation passing `-j` still
wins); the band comes from the 2 s shadow sample, so it is as noisy as that sample; and the pre-existing `queue claim` subcommand,
which does not exec, applies the grant only to its own process.

