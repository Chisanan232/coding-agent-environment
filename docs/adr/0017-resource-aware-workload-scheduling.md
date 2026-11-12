# ADR 0017: Resource-aware workload scheduling (SPE-150)

Status: accepted for the repository. Revision 2 (SPE-160) adds the machinery for a **second heavy lane** and the evidence tooling for
a conditional third. **The cap is 2 in the stable and wide tiers, set by the single constant `HEAVY_LANES` in the final "flip"
commit described below; reverting that commit restores 1 and is the rollback.** A third lane stays off behind
`THIRD_LANE_ENABLED = False`. Supersedes the clause "One heavy lane" in decision 3 of [ADR 0015](0015-memory-aware-admission-and-gh-first.md);
the rest of ADR 0015 stands.

**Revision 3 (SPE-173) is a DRAFT and is not accepted.** It proposes raising `LANE_CEILING` from 3 to 4. Nothing in it changes code or
the cap: the ceiling stays 3 until the stage-3 trial below passes and the owner decides. See "Revision 3 (draft)".

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
| Lane cap (`HEAVY_LANES`, read through `heavy_lane_cap()`) | **2** in stable and wide (the flip commit of revision 2; 1 before it and again after its revert); never above `LANE_CEILING` = 3 |
| Declared classes and estimates | shipped (PR #171); a declared estimate is what lane 2 is charged |
| Resource budget and `fits_if_cap_were_2` / `fits_if_cap_were_3` | reported everywhere (#174); **enforced for lanes 2 and up** (revision 2) |
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
- **Production consequence.** Only Cargo jobs started through `run` / `run --queue-id`, and non-Cargo classes, export a job limit
  themselves. A holder created by the `reserve` CLI or by the non-exec `queue claim` CLI for a declared Cargo class records the
  `granted_jobs` it would have exported (the band grant, or the declared slots when there is no usable budget) and is charged those
  slots, not every core (SPE-171); the claim output carries `cargo_build_jobs` and a warning that the supervising process must run
  the job with `CARGO_BUILD_JOBS` at or below it. A legacy holder with no estimate, a classed record that carries no limit, and
  any job claimed before SPE-171 are still charged all cores or the whole budget and keep the second lane closed until they finish.
  The recorded non-exec limit is NOT enforced: nothing can make a supervisor honour it. The charge is a declaration; the safety net
  is the measured-load gate (`load_credit` is `min(slots, measured cpu rate)`, so a holder that really burns more than it declared
  still raises the load the next lane sees), not the charge. A non-exec refresh of an existing holder never changes its record. Both
  `queue claim` and `reserve` print `cargo_build_jobs` and the warning.
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

Revert the flip pull request's merge commit (it sets `HEAVY_LANES` stable and wide to 2, updates the pins and the status wording); nothing else needs to
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

## Revision 3 (draft): a fourth heavy lane (SPE-173)

Status: **DRAFT, not accepted.** Documentation only. The measurements below are from "analysis 2026-11-11" (read-only).

### Decision statement

Target: four concurrent heavy jobs in normal operation. **The ceiling stays 3, and `FOURTH_LANE_ENABLED` stays `False`, until the stage 3 trial below (three jobs at
once, with lane 3 already enabled) passes.** `THIRD_LANE_ENABLED` is gated separately, by the SPE-172 trial (see "Stage numbering"). Lane 3 itself has not passed its trial (the trial protocol is SPE-172, draft). Raising the ceiling is a further
owner decision taken on that evidence; this draft only fixes what the text and the code would have to say if the owner takes it.

### Related work, not repeated here

- PR #226: CPU accounting fix. The budget's CPU sum (see "How the budgets are enforced") is only as good as that accounting.
- SPE-172 (draft PR #228): the lane-3 trial protocol. It is the stage 2 trial below: its `TRIAL_STAGE_N` is 2, because the evidence for a third
  lane is jobs that ran with two at once while a third was judged. Its PASS gates the lane-3 flip (draft PR #229), not the raise to four.

### What fits today (analysis 2026-11-11)

| Resource | Finding |
|---|---|
| CPU | 16 cores: four 4-slot jobs, or 8+4+4, fit exactly |
| Memory | about 21 GiB free for new work (idle `host_used` about 68 of 128 GiB) |
| Disk | two isolated builds need 2x60+100 = 220 GiB (two 60 GiB targets plus the 100 GiB floor) against about 169-173 GiB free. Four lanes need disk reclaimed: at least 200 GiB free to start a stage |
| Exclusive keys | one shared-target job and one Docker job at a time |

Memory is the tightest budget today and the figure is dominated by two known double counts (see Prerequisites), so it may be pessimistic.

### Job classes that may overlap

- Lanes 1-2: as today (any declared class the budget admits).
- Lanes 3-4: only `build-cold-isolated` jobs with their own `CARGO_TARGET_DIR` exclusion key, or `remote` / `light` jobs.
- `container-suite` (the Docker key) and shared-target builds never overlap with their own kind (decision 5, unchanged).
- Lanes 3-4 also require all of: no holder without a class, no holder missing `granted_jobs` (such a holder is charged every core or the
  whole budget today), band GREEN, tier wide, and `free - reserved - request >= HEAVY_FLOOR + 60 GiB`.
- The fourth lane additionally requires the sum of CPU slots including the request to be at most the core count (no oversubscription).

### How the budgets are enforced per lane

All in `scripts/disk-admission.py`, under the existing lock; the host is sampled before it.

- **Lane count.** `heavy_lane_cap(tier, latch)` (about line 320). Lane 1 never reads the budget. Lanes 2 and up go through
  `_budget_refusal` (about line 2693): a host sample, status ok, `would_admit`, band GREEN.
- **CPU.** `budget_decision` (about lines 2328-2420): `cpu_used + cpu_need > cpu_limit` refuses, where `cpu_limit = cores *
  CPU_BUDGET_PERCENT // 100` (100%, so cores) and `cpu_used` sums each holder's slots (all cores for a legacy or uncapped holder;
  otherwise `min(cpu_slots, granted_jobs)`). The budget, CPU included, gates lanes 2 and up (`_budget_refusal` returns nothing for lane 1), so the fourth lane's no-oversubscription rule is
  today's behaviour made an explicit invariant, not new code.
- **Memory.** `projected = host_used + sum(max(0, est_mem - measured_rss)) + 8 GiB x unmanaged trees + request mem`; refused when
  `projected > RAM x (1 - headroom)`, headroom 30% in GREEN and 40% otherwise (`HEADROOM_PERCENT`); a legacy holder is charged the whole
  limit.
- **Disk.** `free_bytes - reserved_bytes - request bytes < floor` refuses, with `floor = HEAVY_FLOOR` (100 GiB) today. The extra
  60 GiB for lanes 3-4 is new and is not in the code now (`trial_report` applies floor + 60 GiB only as a trial criterion).
- **Exclusion keys.** Any shared key refuses.

### Evidence so far (analysis 2026-11-11)

| | Nominal 1 lane (11-01 to 11-08) | Nominal 2 lanes (to 11-11 15:00Z) | 3 lanes |
|---|---|---|---|
| Completed jobs | 186 | 114 | no data: not run |
| Jobs/hour, wall clock | 1.05 | 1.45 | - |
| Jobs per busy hour | 2.60 | 1.65 | - |
| Queue wait median / p90 | 11 / 182 min | 158 / 381 min | - |
| Crate job median | 2.8 min (n=83) | 2.4 min (n=36) | - |
| Container-suite median | 34 min (n=2) | 57.0 min (n=50) | - |
| Legacy median | 2.2 min (n=71) | 28.5 min (n=18) | - |
| Workspace median | not reported | 10.8 min (n=4) | - |
| Minimum free disk | not reported | 149.0 GiB | - |
| Pressure / swap growth | not reported | always 1 / 0 | - |

Read these plainly:

- There was effectively **no real cap-2 window**: about 93-160 s of two-way overlap in 63 h, and 3 overlapped jobs.
- The job mix is **not comparable**: about 41 h of Circinus container-suite jobs share the Docker key and cannot overlap, and the 1-lane
  window had a different mix.
- Confidence is **low**. These numbers justify the prerequisites below. **They do not justify raising the ceiling.**

### Code consequences (specified, not implemented)

1. `FOURTH_LANE_ENABLED = False` is honoured only when `THIRD_LANE_ENABLED` is on; with the third lane off it has no effect.
2. `LANE_CEILING = 4`. Only the flip PR for each stage changes the flags; the ceiling change itself ships with, or after, the stage 3 pass.
3. `heavy_lane_cap` adds a lane for the wide tier per enabled flag and still returns `min(lanes, LANE_CEILING)`.
4. `fits_if_cap_were_3` is computed from `LANE_CEILING` (line 2416), so raising the ceiling would silently change its meaning and
   `trial_report`'s `fits_if_cap_were_3_share` (line 2114). It must be pinned to the literal 3, and a separate `fits_if_cap_were_4`
   added, with the stage-N criterion reading `fits_if_cap_were_N`.
5. The lane-3/4 class and `+60 GiB` rules above are added to the budget as a lane-index-dependent refusal reason.
6. `trial_report` takes the stage N and applies the per-stage gates below.

**Unchanged ADR 0017 invariants:** atomic admission, headroom, ownership, strict FIFO that stops at the first entry that does not fit
(so an ineligible head, for example a `container-suite` head while Docker is held, blocks lane 4 until it can run: a deliberate
fairness cost), backpressure, no preemption, and nothing is ever signalled.

### Proposed four-lane trial: stages 2, 3, 4

**Stage numbering.** Stage N is the N-way operating point: its evidence is jobs that ran with exactly N at once, and its verdict question is
`fits_if_cap_were_(N+1)`, counted only from verdicts made with exactly N jobs running (`fits()` is `count + 1 <= limit`, so a verdict with fewer
running says nothing about lane N+1). Stage 2 (SPE-172, `TRIAL_STAGE_N = 2`, `fits_if_cap_were_3`) gates the lane-3 flip. Stage 3
(`fits_if_cap_were_4`, three at once, lane 3 enabled) gates the lane-4 flip. Stage 4 re-runs at four to confirm normal operation.

Each stage needs at least 20 finished jobs that ran with exactly N jobs at once, across at least 2 classes with at least 5 each, and at
least 5 h of N-way overlap. Gates, per stage:

- Median queue-to-completion at least 20% better than the solo baseline (the `trial_report` rule; a per-stage baseline is not implemented).
- Worst class slowdown at most 1.6.
- Pressure at most 1 and swap growth at most 2 GiB during overlap.
- `free - reserved >= HEAVY_FLOOR + 60 GiB` during overlap samples (the window-wide minimum free is reported, not gated).
- Share of `fits_if_cap_were_(N+1)` at least 0.5, counted only from verdicts made with exactly N jobs running, once per entry and situation.
- Budget deadline misses (a host sample that misses its deadline means no extra lane): reported per stage, not gated; no `trial_report` criterion exists for them yet.

Stage 4 needs the stage 2 and stage 3 passes.
Missing data is insufficient, never PASS.

### Lane-3 evidence and the lane-4 capacity gap (2026-11-12)

Measured on the lane-3 trial window (to 2026-11-12 12:16 UTC, class estimates 8 / 6 / 4 / 16 GiB in force): the lane-3 verdicts are refused by
memory in 21 of 27 counted verdicts (6 distinct entries; projected 93.1 to 99.8 GiB against the 89.6 GiB limit, an overage of 3.5 to 10.2 GiB, median
7.0) and by cpu in 11 of 27 (12 of 16 slots in use, a third `build-workspace` asks for 8). See "Trial plan" for the counting rule.

What that implies for lane 4 (derived from the constants and the audit above, not separately measured): the host has 128 GiB, the limit is 89.6 GiB,
and `host_used` on an idle host reads about 73 to 77 GiB, so about 12.6 to 16.6 GiB is left for every managed job together, and the 8 GiB unmanaged
allowance per unmanaged tree comes out of that. Two typical running jobs (`build-workspace` 8 plus `build-crate` 6) already charge 14 GiB. A fourth lane
adds two more jobs: 8 GiB for two `container-suite` entries or 12 GiB for two `build-crate` entries, so memory is short by roughly 4 to 13 GiB before
any third lane has passed. CPU is the second limit: 16 cores and 16 slots, with `build-workspace` at 8 and `build-crate` / `container-suite` at 4, so four
concurrent heavy jobs fit only when none is a `build-workspace`.

Consequence for the stage plan: stage 3 cannot reach its `fits_if_cap_were_4` share of 0.5 on this host for build-class work. A fourth lane is only
reachable for light or degraded-parallelism jobs (a class-aware lane rule), or with more memory headroom than the 30% rule leaves. The 30% headroom is
not proposed for change. The owner decision is whether to accept a class-aware ceiling (lanes above two only for `light` and degraded `build-crate`) or to
keep two lanes; the stage 3 trial is not worth running for build classes until that is decided.

### Rollback criteria

Revert the flip PR (running jobs finish untouched; nothing is signalled, released or requeued; the procedure in "Rollback procedure"
applies) on any of: pressure above 1 or swap growth above 2 GiB during overlap; `free - reserved` below floor + 60 GiB; slowdown above
1.6; no improvement over the previous stage; any duplicate grant or broken ownership invariant.

### Prerequisites, in order

1. Stale checkouts (116 of 133 still at `HEAVY_LANES = 1`) brought to revision 2, or one canonical copy.
2. Cargo claims go through `run --queue-id` so `granted_jobs` is recorded.
3. Every job declares a class.
4. Recalibrate `host_used` and the container-memory double counting.
5. Fix misdeclared `build-cold-isolated` entries whose key is the shared target.
6. Keep logs for the whole trial (they rotate at 1 MiB).
7. Reclaim disk to at least 200 GiB free.

### Open questions for the owner

- Whether four lanes are worth the Docker-head blocking cost under strict FIFO.
- Whether the lane-3 and lane-4 flips share one reviewed PR each, as the lane-3 flip does today.

## Consequences

- Text that says "one heavy lane at a time" as a permanent rule is replaced with "up to two heavy lanes (the second only when the resource
  budget passes; ADR 0017 revision 2)" in this repository. The memory gate's reason now reads
  `heavy lanes full (N of cap)`.
- User-level copies (`~/.claude/CLAUDE.md`, installed skills) still carry the old wording until an owner-run deploy.
- Cap 2 is one constant (`HEAVY_LANES`) behind the enforcing code path of revision 2, with the invariants above unchanged; the flip is
  the last, separately reviewable commit and the rollback is its revert.

## Version history

| Rev | Change |
|---|---|
| 1 | Initial decision (SPE-150 PR7) |
| 2 | Two heavy lanes behind the budget, `heavy_lane_cap()`, `LANE_CEILING` 3 with `THIRD_LANE_ENABLED` off, `granted_jobs`, `queue trial-report`, rollback procedure (SPE-160) |
| 3 (draft) | Proposes `LANE_CEILING` 4 and a staged trial 2 -> 3 -> 4; not accepted, no code change (SPE-173) |
