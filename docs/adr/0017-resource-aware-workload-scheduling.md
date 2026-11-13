# ADR 0017: Resource-aware workload scheduling (SPE-150)

Status: accepted for the repository. **Revision 3 (SPE-172, draft, awaiting owner sign-off) records the first trial evidence and revises the
third-lane protocol; it does not enable a third lane.** Revision 2 (SPE-160) adds the machinery for a **second heavy lane** and the evidence tooling for
a conditional third. **The cap is 2 in the stable and wide tiers, set by the single constant `HEAVY_LANES` in the final "flip"
commit described below; reverting that commit restores 1 and is the rollback.** A third lane stays off behind
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

## Revision 3: trial evidence and the third-lane protocol (SPE-172, draft)

Status of this revision: **proposed**. Until the owner signs it off, the text above ("Trial plan", step 3) is the protocol in force. The
code in this change implements the revised criteria below; `THIRD_LANE_ENABLED` stays `False` in every commit except the last, which is
the flip and must not merge until the owner has signed off and a `trial-report` PASS is attached.

### Trial evidence 2026-11-11

Measured by a read-only analysis of the queue state, receipts and measurement logs (no state was written). Logs rotate at 1 MiB, so
the verdicts cover about 12 h; the job and overlap figures cover the 63 h since cap 2 was enabled.

| Measure | Result | Reading |
|---|---|---|
| Budget verdicts | 2,887 | 88% came from stale cap-1 copies of the script: 116 of 133 checkouts still have `HEAVY_LANES` = 1 |
| `fits_if_cap_were_3` true | 3.4%, i.e. 12 of 358 revision-2 verdicts | Weighted by polling (one queue entry alone produced 125 of the 358), so it measures re-polling, not distinct situations |
| Reason: request had no class | 58.2% | Artifact: the request is charged as legacy (whole budget) |
| Reason: running holder charged 16 CPU slots, no `granted_jobs` | 25.4% | Artifact, fixed by PR #226 (not part of this change) |
| Reason: holder with no class charged the whole memory budget | 12.6% | Artifact |
| Reason: memory projected over the limit | 14.8% | Real in the model but uncalibrated. Container-suite memory lives in the Docker VM, so it measures about 0 while its 12 GiB estimate is charged on top of `host_used`: likely double counting |
| Reason: Docker exclusion key | 13.1% | Real |
| Reason: shared Cargo target exclusion key | 9.3% | Real |
| Reason: disk floor | 0% | Not binding |
| Minimum free disk | 149.0 GiB (159,936,757,760 B) at 2026-11-11T14:03:03Z | One job running, no overlap. Builds ran serially and grew the shared target by about 383 GiB; free fell from 203.7 to 149.0 GiB. The old criterion takes the minimum over all samples, so it measured the host, not concurrency |
| Overlap | 3 overlapped jobs, about 93 to 160 s of two-way overlap in 63 h | Peak concurrency 2, never 3; peak pressure 1; swap growth 0 |
| Throughput, nominal cap 1 vs cap 2 | 1.05 vs 1.45 jobs/h wall clock | Nominal, not a demonstrated gain |
| Wait median / p90 | 11 / 182 min at cap 1 vs 158 / 381 min at cap 2 | Dominated by about 41 h of Circinus container-suite jobs that share the Docker key |

Reasons overlap (one verdict can list several), so the percentages do not sum to 100.

**What the evidence shows.** The trial has not produced a third-lane signal in either direction: almost every refusal is an artifact of
stale copies, missing classes or an unrecorded `granted_jobs`; the real exclusions (Docker, shared target) are the declared limits of the
design; and there has been essentially no concurrency (a few minutes) to judge safety by. It does show that two criteria of revision 2
measured the wrong thing: the disk criterion (host-wide minimum, driven by serial shared-target growth) and the `fits_if_cap_were_3`
share (polling-weighted, and counting verdicts made on an empty host, which trivially fit).

### Revised stage-3 trial protocol

Criteria names are unchanged. Changes, all in `trial_report`:

1. **Stage gates** (`overlapped_jobs`, `overlapped_classes`, new `overlap_hours`) count finished jobs that ran with **exactly N jobs at
   once** (N = 2, `TRIAL_STAGE_N`, the stage that evidences a third lane), a job's figure being the larger of its sampled `concurrent`
   and the claim intervals. Required: at least 20 such jobs; at least 2 declared classes with at least 5 each (`unclassified` does not
   count); at least 5 h during which exactly N jobs were running. Previously: 10 overlapped jobs across 2 classes. A stage with less is
   `insufficient`, never PASS.
2. **`min_free_bytes`** is evaluated over overlap samples only (`concurrent` of at least 2) as `free - reserved >= floor + 60 GiB`, where
   `reserved` is the declared bytes of the jobs live at that sample; a sample whose live jobs have no byte figure is not evaluated. The
   window-wide minimum free disk and its time are reported (`window_min_free_bytes`, `window_min_free_at`) and are **not gated**. No
   evaluable overlap sample is `insufficient`.
3. **`fits_if_cap_were_3_share`** counts only verdicts made with exactly `TRIAL_STAGE_N` jobs running (`count == 2`: `fits()` is `count + 1 <= limit`, so a verdict with fewer
   running says nothing about a third lane), once per queue entry and situation (entry, running count, band and the *kinds* of resource reason: the text before the first colon, such as memory, cpu, byte floor or band RED; `excl_key overlap with ...` has no colon and keeps its clashing labels; the live figures in a reason's text do not separate situations); the required share (at least half) is unchanged.
   Known limits of the report, to be read with a PASS: reserved bytes are rebuilt from jobs claimed inside the window only (a job already
   running at the window start, GRANTED-not-yet-claimed entries and orphans are not counted, so the margin can be optimistic), and
   `overlap_hours` counts finished jobs only (an unfinished third job hides a three-way period as two-way).
4. Unchanged: median queue-to-completion at least 20% better than the solo baseline, worst class slowdown at most 1.6, no pressure sample
   above 1 during an overlap, swap growth at most 2 GiB.

### Prerequisites before a trial can say anything

1. Every checkout runs revision 2 code: bring the 116 stale checkouts to it, or route every job through one canonical copy of the script.
2. Every Cargo job goes through `run --queue-id`, so `granted_jobs` is recorded.
3. Every job declares a class; an unclassified job is charged as a legacy request and holds the second lane closed.
4. Recalibrate `host_used` and the container-suite memory so the Docker VM is not charged twice (open item 3 of revision 1).
5. Reject a misdeclaration such as q000389: a job declared `build-cold-isolated` whose exclusion key is nevertheless the shared target
   (it holds the key every other build needs while claiming isolation). Declare the key the job really uses.
6. Keep the logs for the whole trial: copy or raise the 1 MiB rotation so the verdicts of the full window survive (today about 12 h).
7. Reclaim disk to at least 200 GiB free before the trial starts; the shared target grew about 383 GiB in the observed window.

### The remaining gap

The only direct evidence for the third lane is a PASS from the revised `queue trial-report` over a window that meets the prerequisites.
Today there are 3 overlapped jobs and minutes of overlap against 20 jobs and 5 h required, and the two real exclusions (Docker key,
shared target) still serialise most work. Whether a third lane would help at all is therefore open; the trial of the second lane has to
run first, with the prerequisites in place.

### Consolidated sign-off statement

The owner is asked to sign off on this: (a) the evidence table is accepted as the record of the first trial; (b) the revised criteria
and prerequisites replace step 3 of the trial plan; (c) `THIRD_LANE_ENABLED` is flipped only by a separate pull request carrying a
PASS from the revised report; and (d) the flip, which lives alone in its own draft pull request, is the rollback
unit, must not merge before (c) is satisfied, and is reverted to restore today's state. Sign-off of (a) and (b) does not authorise (d).

## Revision 4: class memory estimates recalibrated against measured peaks (SPE-160)

Owner approval to recalibrate was given on 2026-11-12 with two conditions that this revision keeps: the 30% host headroom and every
existing admission safeguard are unchanged, and the overlap trial is rerun on real queued work after rollout.

### Why

The trial of revision 3 ran 23 real jobs and none overlapped (peak concurrent 1). The receipts show why: while q000391 (`container-suite`)
ran, q000396 (`build-crate`) was refused with `memory: projected 90.7 GiB > 89.6 GiB (RAM x (1 - 30%))`. The host sat at about 77 GiB
`host_used` when idle (about 73 GiB at the moment of the refused decision: 90.7 - 12 - 6 = 72.7 GiB), so the budget left was only 13 to 17 GiB, and the
12 GiB `container-suite` charge plus the 6 GiB request consumed it.
`host_used` was audited and is not the defect: it counts active + wired + compressed pages, which is lower than an Activity-Monitor-style
figure (about 73 GiB against about 88 GiB when audited). The host figure moves by a few GiB with other applications (73 to 77 GiB seen). The class estimates were the part not backed by measurement.

### Measured peaks (`disk-admission.py measurements`, process-tree RSS, solo runs, pressure 1 throughout)

| Class | Runs | Measured max (= p95) | Previous estimate | New estimate | Margin over measured |
|---|---|---|---|---|---|
| `build-crate` | 15 | 4.39 GiB | 6 GiB (2 + 1 x 4) | unchanged, 6 GiB | 1.37x |
| `build-workspace` | 18 | 4.90 GiB | 16 GiB (4 + 1.5 x 8) | 8 GiB (2 + 0.75 x 8) | 1.63x |
| `container-suite` | 17 | 1.98 GiB | 12 GiB | 4 GiB | 2.02x |
| `build-cold-isolated` | 2 | 4.51 GiB | 16 GiB | unchanged (fewer than `MIN_SOLO_RUNS` = 3 qualifying runs) | n/a |

Peaks are lower bounds (sampled about every 60 s), which is why every changed value keeps a margin of at least 1.6x (`build-crate` was not
changed and keeps its existing 1.37x). `container-suite` gets
the widest margin because its RSS excludes the Docker VM; the VM is resident memory already inside `host_used`, so it is not charged a
second time, but its growth during a build is not in the RSS peak. **That growth is an accepted risk of the 4 GiB value**: the 2x margin
and the revert trigger below are the only cover, so the overlap trial must also compare `host_used` before and during every
`container-suite` job, and a rise above 3 GiB reverts this row. No concurrent run has been measured for any class
(`concurrent=0`), so these values are solo evidence and the trial must confirm them under overlap.

### Effect, same inputs as the refused decision

With about 77 GiB `host_used` and a limit of 89.6 GiB: `build-crate` (6 GiB) next to a running `container-suite` projects to about
77 + 2 (residual of its 4 GiB charge over about 2 GiB RSS) + 6 = 85 GiB, inside the limit; two `build-workspace` jobs project to about
88.1 GiB, inside the limit by about 1.5 GiB. The second is deliberately tight: it admits only while the host is as quiet as it was, and
refuses otherwise.

### Safeguards and rollback

Unchanged: the 30% headroom, strict FIFO, the exclusion keys, the disk floor and the band logic. The constants stay in one table
(`WORKLOAD_CLASSES`). **Revert trigger:** any overlap sample with pressure above 1, or swap growth beyond the trial threshold, reverts
the two changed rows. Rollback is reverting this change.

### Trial report: overlap share and minimum samples (found by the first overlap, 2026-11-12)

The first two overlapped jobs produced `FAIL` with a "102x slowdown". It was a measurement artifact, not contention: q000419 was a 58-minute
`container-suite` job (about 315 samples, one at `concurrent` 2) compared against a baseline of short solo runs, and one concurrent sample
made the whole job count as overlapped. Two rules fix the report. Independent review found the first version of the change could turn a
FAIL into a PASS (a pressure sample inside a briefly overlapped job went unseen, and partly overlapped jobs joined the solo baseline), so the
final rules keep every concurrent sample of every job in the pressure check and keep any job that ever overlapped out of the solo baseline:

- A job counts as overlapped only when at least half of its sampled life (or, without samples, of its time) ran with another job
  (`overlap_share` 0.5).
- The share is counted in samples, not time, so uneven sampling intervals skew it; the 0.5 threshold is deliberately coarse.
- A comparison or share judged on a handful of samples is `insufficient`, not `FAIL`: slowdown and queue-to-completion need
  `jobs_per_class` (5) solo and overlapped runs in the class, and the fits share needs `min_verdicts` (10) distinct verdicts.

A later audit of the lane-3 verdicts (window to 2026-11-12 12:16 UTC, class estimates 8 / 6 / 4 / 16 GiB deployed and used by every entry enqueued
after 08:51 UTC) found the same polling weight again: the situation key included the reason text, whose "projected N GiB" changes on every poll,
so one entry waiting behind two running jobs produced a new "situation" per poll. Of 27 counted verdicts, 12 came from `la-cargo-25106` and 10 from
`la-cargo-51643`; the distinct entries were 6. Memory refused 21 of 27 (projected 93.1 to 99.8 GiB against the 89.6 GiB limit, an overage of 3.5 to
10.2 GiB, median 7.0), cpu refused 11 of 27 (12 of 16 slots in use, the third `build-workspace` asks for 8), and 10 were both. The key now uses the
reason kinds, which leaves 9 distinct verdicts on that window (4 of them fits, 0.444), one short of `min_verdicts` (10), so the share reads `insufficient`, not FAIL. A refusal that worsens between polls is still one verdict. This changes how verdicts are counted, not the share, the minimum, the 30% headroom or any admission check.

On the same window the corrected report reads `INSUFFICIENT`. The pass thresholds are unchanged, and the run-time comparison is still
within a class, so a mixed-length class remains a known limit that a larger trial averages out.

### What this does not do

It does not enable a third lane and does not change a trial criterion. #229 stays unmerged until a trial report passes on real data.

## Revision 5: a class-aware third lane, trialled in shadow (SPE-160)

### Why

The lane-3 trial window 2026-11-11T23:27:03Z to 2026-11-13T00:48:36Z (25.4 h, 142 jobs) asked a flat question: would any third job fit. The
refusals were almost all `build-workspace` (8 GiB, 8 slots): memory over the 89.6 GiB limit by 3.5 to 10.2 GiB, or 12 of 16 cpu slots already
held. A light job holds no memory or slots, and a `build-crate` cut to its class `min_jobs` (2) needs 4 GiB and 2 slots instead of 6 GiB and 4.
The owner asked for that narrower policy to be evaluated on real queued work before anything is enabled.

### Policy under trial (shadow only)

A third lane could admit only a request of class `light`, or of class `build-crate` that has not been raised above its class default, charged
and granted at `min_jobs`. Every other class stays on two lanes. Memory, cpu slots, the byte floor, exclusive keys, the band and the 30% headroom are
checked exactly as before; the policy removes no check, it changes only what the third job is charged. A request whose estimate was raised above
the class default is not cut, because an estimate may be raised and never lowered.

`budget_decision` records `class_aware_eligible`, `class_aware_jobs` and `fits_if_class_aware_3` beside `fits_if_cap_were_3`, and the receipts carry
them. `queue trial-report --policy class-aware` replaces the flat fits criterion with `fits_if_class_aware_3_share`: at least 0.5 of the distinct
verdicts of an eligible request made with exactly two jobs running, at least 10 of them. Only receipts that carry the new field count, so history
made at four jobs is not reused, and verdicts of other classes are set aside and reported. Every other criterion, threshold and the default policy are unchanged.

### What this does not do

It does not touch `heavy_lane_cap`, `HEAVY_LANES`, `THIRD_LANE_ENABLED`, the memory gate's lane reading, a trial threshold or the headroom.
Enabling is a later, separately reviewed change that must also (a) grant `min_jobs` through `granted_jobs` and charge the reservation at the reduced
estimate, otherwise the next verdict overcharges the lane, and (b) pass `trial-report --policy class-aware` and the gate. #229 and #227 stay open.

### Overlap hours, reconciled

The same window and the same finished jobs give three figures for time with exactly two jobs running:

| Measure | Value |
|---|---|
| Gate: each job ends at its last measurement sample (lower bound) | 28 s (0.008 h; the report rounds it to 0.01) |
| Samples with `concurrent` = 2: 41 samples at the 10 s median gap | 6.8 min (3.4 min counting each moment once) |
| Release time as the end (upper bound; a release is recorded when a queue-aware command next runs) | 9.1 min (0.152 h) |

The 10 to 20 minute estimate sits above even the upper bound. At the upper-bound rate, 5 h of overlap takes about 35 days of the same traffic, and
far longer at the gate's own measure. The criterion is unchanged here; whether to keep it is an owner decision.

### Evidence on the historic window (not counted by the new policy)

Replaying the light and `build-crate` entries of that window at `min_jobs`: five of eight entries fit throughout. `aasm6463-clippy` (q000544) stayed 0.2 to
2.7 GiB over the limit, `aasm6463-push` (q000545) up to 0.6 GiB and `orgF4run` (q000483) up to 1.0 GiB, so memory still refuses a reduced crate when the host is
that full, and the eligible share is not guaranteed to reach 0.5.

### Known limit of `min_free_bytes`

16 of the 60 overlap samples are `reserve` lines of 16 jobs started without a queue entry; the line carries no byte figure, so the margin cannot be
computed and the criterion reads `insufficient`, not FAIL. The 44 evaluable samples have a minimum margin of 177,979,600,896 bytes against 171,798,691,840 needed.

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
| 3 | Draft (SPE-172): trial evidence 2026-11-11, revised stage-3 criteria (exact-N stage gates, overlap-window disk margin, running-only deduplicated `fits_if_cap_were_3`), prerequisites, sign-off statement |
| 4 | Class memory estimates recalibrated against measured solo peaks: `build-workspace` 16 to 8 GiB, `container-suite` 12 to 4 GiB; `build-crate` and `build-cold-isolated` unchanged; 30% headroom kept (SPE-160) |
| 4a | Trial report: a job overlaps only when at least half its life did; slowdown, queue-to-completion and the fits share need minimum samples before they can FAIL (SPE-172) |
| 5 | Class-aware third lane trialled in shadow: `fits_if_class_aware_3`, `trial-report --policy class-aware`; overlap-hours reconciliation; no lane, threshold or headroom change (SPE-160) |
