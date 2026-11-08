# ADR 0017: Resource-aware workload scheduling (SPE-150)

Status: accepted for the repository. Revision 2 (SPE-160) adds the machinery for a **second heavy lane** and the evidence tooling for
a conditional third. **The shipped value of the cap is a single constant, `HEAVY_LANES`, changed only by the final "flip" commit
described below; until that commit is merged the cap is 1 and revision 2 is dormant.** A third lane stays off behind
`THIRD_LANE_ENABLED = False`. Supersedes the clause "One heavy lane" in decision 3 of [ADR 0015](0015-memory-aware-admission-and-gh-first.md);
the rest of ADR 0015 stands.

## Context

ADR 0015 fixed "one heavy lane" because it was the only safe rule available: admission knew bytes of disk and process names, not
what a job needs. The rule is safe and wasteful at once. A small crate build and a workspace build both take the whole lane, while
the host may have most of its memory and cores idle. SPE-150 replaced the lane count with a model of what each workload costs, and
shipped it behind the old cap so it could be measured before it decides anything.

## Decision

The intended long-term rule is admission by a measured resource budget; "one heavy lane at a time" is a cap, not a permanent rule. Revision 1 shipped the budget beside the cap; revision 2 (below) makes it decide for lanes beyond the first.

1. **Workloads, not processes.** A workload is a process tree (the wrapper that exec'd the job and every descendant), owned by one
   reservation or queue entry. Admission reasons about owned workloads; an unowned running heavy tree is *unmanaged*.
2. **Declared cost.** A job declares a class (`build-crate`, `build-workspace`, `build-cold-isolated`, `container-suite`, `light`,
   `remote`) and may raise, never lower, the class estimate of memory, cpu slots and disk. A record with no class keeps the legacy
   meaning: the whole budget and all cores.
3. **Budget arithmetic, under the existing lock.** projected memory = host used + the unmeasured remainder of every owned workload
   (estimate minus measured resident memory, measured only if the sample is newer than the record) + 8 GiB per unmanaged tree + the
   request. A request fits only if projected <= RAM x (1 - headroom), cpu slots <= cores, no exclusion key is shared, the 100 GiB disk
   floor holds, and the band is not RED. The host is sampled before the lock, never inside it.
4. **Bands.** GREEN (all memory-admission EXIT bounds met), AMBER (not RED, some EXIT bound unmet) and RED (any ENTER refusal or
   unreadable input). Headroom 0.30 in GREEN and 0.40 otherwise. The load used for banding is corrected for admitted work.
5. **Exclusive key = the resolved target directory.** Two live workloads must not share a Cargo target directory (or the Docker
   daemon). Every gate that may run beside another workload uses its own `CARGO_TARGET_DIR`; a shared `build.target-dir` is a hidden
   second resource the budget cannot see.
6. **Banded per-job caps.** The grant carries jobs and test threads by band: GREEN the class values, AMBER the larger of `min_jobs`
   and half the request, RED none. They are exported to the job's environment only for a declared Cargo class, only from a budget with
   status ok, and never above the job's own setting.
7. **Shadow first.** Each step ships recorded and unenforced, with the old rule still deciding, and moves only on measured evidence
   and an authorized change. The budget is computed beside the lane cap and reported as `fits_if_cap_were_2` (and, since revision 2,
   `fits_if_cap_were_3`).

**Invariants that no step may weaken:** atomic admission (check and record under one lock), headroom, ownership (a lane is freed only
on proof the whole workload ended), FIFO queue order, backpressure (a refusal keeps the grant and the reason names the resource and
numbers) and no preemption (nothing is signalled, released or reaped on behalf of another job).

## Current state (honest)

| Piece | State |
|---|---|
| Lane cap (`HEAVY_LANES`, read through `heavy_lane_cap()`) | **1** until the flip commit of revision 2 merges, then 2 in stable and wide; never above `LANE_CEILING` = 3 |
| Declared classes and estimates | shipped (PR #171); a declared estimate is what lane 2 is charged |
| Resource budget and `fits_if_cap_were_2` / `fits_if_cap_were_3` | reported everywhere (#174); **enforced for lanes 2 and up** (revision 2; dormant while the cap is 1) |
| Reservation-aware memory veto | shipped (#175); its cap now follows `heavy_lane_cap()`; a second tree also needs the EXIT bounds |
| Banded per-job caps in the child environment | shipped (#176); revision 2: RED exports `min_jobs`, an unavailable budget exports the declared figures, oversized integers are safe, the exported jobs are recorded as `granted_jobs` |
| Third lane | **not enabled** (`THIRD_LANE_ENABLED = False`); needs its own reviewed change |

The constants (class estimates, 8 GiB allowance, headroom, `host_used` = active + wired + compressed pages) are guesses until measured.

## Staged concurrency: what had to be measured first (revision 1 list, status in revision 2)

Revision 1 recorded what a raised cap needs. Their status after revision 2:

1. Shadow `fits_if_cap_were_2` against real contention: now collected as trial telemetry (see "Trial plan"); the cap-2 trial is the
   first real data. Measurement lines carry `swap_used_bytes` and the budget verdict is logged per site.
2. The cost of the host sample on `reserve`, `claim` and the 60 s snapshot, and the 3 s deadline margin: unchanged and still open. A
   sample that misses its deadline now means **no second lane** (never a guess), so the cost shows up as lost concurrency, not risk.
3. Calibration of `host_used` and the class estimates against measured peaks: still open. **Estimate recalibration needs measured
   evidence from the trial and is deliberately not part of revision 2**; the constants are unchanged.
4. RED must still export `min_jobs`: done (revision 2).
5. A guard for oversized integers: done (revision 2).

## Revision 2: two heavy lanes (SPE-160)

### Instruction

Recorded in Jira SPE-160 (founder, 2026-11-08), summarised here, not a verbatim quote: raise the normal heavy-lane concurrency from
1 to 2 and keep 3 as a conditional ceiling; keep the `HEAVY_FLOOR` disk reserve, strict first-in-first-out queue fairness and active
job claims; use per-job CPU, memory and disk estimates and Cargo job limits so concurrent builds do not each claim the whole machine;
never interrupt or requeue a running job; collect trial telemetry; under pressure stop admitting and let active jobs finish; have the
admission logic tested and the rollback documented before rollout.

### Design

- **One function decides the lane count.** `heavy_lane_cap(tier, latch)` is 0 while latched or below stable, `HEAVY_LANES[tier]`
  otherwise, plus one in the wide tier only when `THIRD_LANE_ENABLED`, and never more than `LANE_CEILING` (3). `reserve`, `queue
  advance`, `queue claim`, `snapshot` and the budget all read it. `memory-admission.py` derives its cap from the same function (the
  widest cap of any tier, since it cannot see the tier; the disk gate applies the tier's own) and falls back to 1 if it cannot load
  it. There is no runtime flag file: a file an agent can write is a forgeable cap.
- **Lane 1 is exactly today's rule.** The disk tier, latch, byte floor, ownership and queue order admit it, and it never reads the
  budget, so a failed host sample cannot stop a lone job.
- **Lanes 2 and up also need the budget** (`_budget_refusal`): a host sample, a budget with status ok, `would_admit` (which already
  honours the tier's cap, the CPU sum, memory headroom, exclusion keys, the byte floor and refuses a request with no estimate) and
  band GREEN. A missing, failed or timed-out sample means an effective cap of one. A refusal reason starts `budget: ` and lists the
  resources that failed. A same-process refresh of an already running reservation is exempt from the budget (it holds its lane), so
  a running job is never refused its renewal because the host turned AMBER.
- **Strict first-in-first-out grants.** `queue advance` grants the head, recomputes the budget beside that grant from the same host
  sample, and tries the next entry; it stops at the first entry that does not fit, so a smaller later entry never overtakes a larger
  head. Bytes of grants made earlier in the same call count toward the 100 GiB floor. A claim re-checks the budget under the lock; a
  refusal keeps the grant (backpressure), and the grant window still lapses after an hour.
- **Per-job limits.** A job started by `run` or `run --queue-id` gets `CARGO_BUILD_JOBS` (and test threads) from the band: GREEN the
  class values, AMBER about half, RED the class minimum, an unavailable budget the declared figures, and never above what the job set
  itself. The value actually exported is recorded as `granted_jobs` on the reservation and on the CLAIMED holder (additive; not a
  declared field, not in the fingerprint). In the budget a running Cargo-class holder **without** a recorded `granted_jobs` is
  charged every core (and a lower recorded figure is charged as itself).
- **Production consequence.** Only Cargo jobs started through `run` / `run --queue-id`, and non-Cargo classes, can ever share the host.
  A holder created by the `reserve` CLI or by the non-exec `queue claim` CLI, a legacy holder with no estimate, and any job claimed
  before this change are charged all cores or the whole budget and keep the second lane closed until they finish.
- **Pressure.** Lanes 2 and up need band GREEN: memory pressure level 1, swap I/O at most 4 MiB/s, compressor at most 25% of RAM and
  load per core (corrected for admitted work) at most 2.0, i.e. the memory gate's EXIT bounds. The memory gate also asks for these
  before it admits a tree beside a running one. AMBER or RED means no new concurrent job; lane 1 keeps today's ENTER thresholds.
  Nothing is ever signalled: active jobs finish.

### Shared-target limitation (read before expecting any speed-up)

Every live Cargo job in this environment shares one target directory (`cargo-target:/Users/bryant/.cargo/shared-target`, the
exclusive key). The budget refuses two jobs that share a key, so **cap 2 yields no extra concurrency for ordinary builds until the
trial jobs run as `build-cold-isolated` with their own `CARGO_TARGET_DIR`**. Isolated targets are a byte reservation too (size them
with `--bytes`; delete them with the workload). That is the intended first use of the second lane and the substance of the trial.

### Trial plan

1. Run isolated, classified jobs (`--class build-cold-isolated`, own `CARGO_TARGET_DIR`) through `queue enqueue` and `run --queue-id`
   so that two overlap, with `disk-admission.py snapshot --json` refreshing every 60 s (the printed `refresher-plist`).
2. Compare overlapped runs to solo runs of the same class with `disk-admission.py queue trial-report --since ISO [--until ISO]
   [--baseline-since ISO --baseline-until ISO] [--json]`. It is read-only (writes nothing), takes no lock and reports per job the
   wait, run (an upper bound from the release time and a lower bound from the last sample) and queue-to-completion, peak concurrency
   and pressure, swap growth, minimum free disk, per class solo baseline versus overlapped slowdown and jobs per busy hour, and the
   share of budget verdicts with `fits_if_cap_were_3` true.
3. Third-lane criteria, all required (missing data is **insufficient**, never PASS): at least 10 overlapped finished jobs across at
   least 2 classes; median queue-to-completion at least 20% better than the solo baseline; worst class slowdown at most 1.6; no
   pressure sample above 1 during an overlap; swap growth at most 2 GiB; minimum free disk at least the floor plus 60 GiB;
   `fits_if_cap_were_3` true in at least half of the verdicts.

### Enabling a third lane

Only a separate, reviewed pull request flips `THIRD_LANE_ENABLED` to `True`. It must carry the `trial-report` PASS output and the
owner's sign-off, and is not auto-merged. `LANE_CEILING` is not raised by that change; raising it is a further decision.

### Rollback procedure

Revert the single flip commit (the one that sets `HEAVY_LANES` stable and wide to 2 and updates the pins); nothing else needs to
change. Effects, in order:

1. The memory gate follows automatically (its cap is derived from `heavy_lane_cap()`); no second constant exists to forget.
2. Jobs admitted under cap 2 keep running and stay CLAIMED / reserved. Nothing is signalled, released, requeued or reaped. A lane is
   freed only on proof the whole workload exited, as always.
3. New work is refused with `lane-cap: N of 1 ...` until the count of live holders drops below 1; `queue advance` reports `lane
   occupied`. Snapshot shows `heavy_lanes_allowed` 1 with `active_heavy_jobs` 2 until one ends.
4. A GRANTED but unclaimed entry keeps its grant, but the grant window (1 h) may lapse before a lane frees; a lapsed grant becomes
   `EXPIRED` (terminal), so the owner re-enqueues it with `--again`, which puts it at the back (a fairness loss, not data loss).
5. While two holders are live after a rollback, a same-process `reserve` refresh by either is refused with `lane-cap` (as before
   this change); the running job is not killed, and its pid-bound reservation is simply not renewed.
6. Only `run` / `run --queue-id` record `granted_jobs`. A standalone `reserve` or non-exec `queue claim` holder records no job limit,
   so it is charged all cores and no second lane opens beside it.
7. Never run `queue release` or `queue cancel` on a running entry to "make room": release is for a holder proved gone, and neither
   touches a process.
8. Mixed checkouts are safe: a checkout still at cap 1 only refuses more, and it reads the new fields (`granted_jobs`,
   `swap_used_bytes`) as unknown keys. Verified by loading the pre-change script against a reservation, a CLAIMED entry and a
   measurement line that carry them.

## Consequences

- Text that says "one heavy lane at a time" as a permanent rule is replaced with "one heavy lane today (a second lane is admitted only
  by the resource budget, once the cap is raised; ADR 0017 revision 2)" in this repository. The memory gate's reason now reads
  `heavy lanes full (N of cap)`.
- User-level copies (`~/.claude/CLAUDE.md`, installed skills) still carry the old wording until an owner-run deploy.
- Cap 2 is one constant (`HEAVY_LANES`) behind the enforcing code path of revision 2, with the invariants above unchanged; the flip is
  the last, separately reviewable commit and the rollback is its revert.

## Version history

| Rev | Change |
|---|---|
| 1 | Initial decision (SPE-150 PR7) |
| 2 | Two heavy lanes behind the budget, `heavy_lane_cap()`, `LANE_CEILING` 3 with `THIRD_LANE_ENABLED` off, `granted_jobs`, `queue trial-report`, rollback procedure (SPE-160) |
