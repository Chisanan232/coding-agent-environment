# ADR 0017: Resource-aware workload scheduling (SPE-150)

Status: accepted for the repository. **The heavy-lane cap is still 1.** The shadow budget, the reservation-aware veto and the
banded per-job caps are merged; raising the cap above 1 (staged concurrency) is **not enabled** by this ADR or by any merged
change. Supersedes the clause "One heavy lane" in decision 3 of [ADR 0015](0015-memory-aware-admission-and-gh-first.md); the rest of
ADR 0015 stands.

## Context

ADR 0015 fixed "one heavy lane" because it was the only safe rule available: admission knew bytes of disk and process names, not
what a job needs. The rule is safe and wasteful at once. A small crate build and a workspace build both take the whole lane, while
the host may have most of its memory and cores idle. SPE-150 replaced the lane count with a model of what each workload costs, and
shipped it behind the old cap so it could be measured before it decides anything.

## Decision

The intended long-term rule is admission by a measured resource budget; "one heavy lane at a time" is today's cap, not a permanent rule. Today only a shadow budget exists beside the cap.

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
   and an authorized change. The budget is computed beside the lane cap and reported as `fits_if_cap_were_2`.

**Invariants that no step may weaken:** atomic admission (check and record under one lock), headroom, ownership (a lane is freed only
on proof the whole workload ended), FIFO queue order, backpressure (a refusal keeps the grant and the reason names the resource and
numbers) and no preemption (nothing is signalled, released or reaped on behalf of another job).

## Current state (honest)

| Piece | State |
|---|---|
| Lane cap (`HEAVY_LANES`, `MAX_CONCURRENT`) | **1** everywhere; admit/refuse decided by the lane check alone |
| Declared classes and estimates | shipped, record-only for admission (PR #171) |
| Shadow budget and `fits_if_cap_were_2` | shipped (#174); reported in results, receipts, snapshot and status; not enforced |
| Reservation-aware memory veto | shipped (#175); same outcomes at cap 1, reasons name the budget |
| Banded per-job caps in the child environment | shipped (#176); changes the job's parallelism, not admission |
| Cap above 1 (staged concurrency) | **not enabled**, no configuration knob exists |

The constants (class estimates, 8 GiB allowance, headroom, `host_used` = active + wired + compressed pages) are guesses until measured.

## Staged concurrency: what must be measured first

The founder instructed, in a coordinator session on 2026-10-31 (not a repo-durable record), that staged concurrency may proceed once measured. This ADR does not itself authorize it. Raising the cap is a later reviewed change that cites the evidence below, and it does not lower any explicit-confirmation, owner, production, destructive or publication gate or any Decision Delegation level. Without a durable record of the instruction, treat the step as needing the owner's decision. Evidence required:

1. Over N real runs, the shadow `fits_if_cap_were_2` against actual contention (pressure, swap I/O, load, wall time, failures) when
   two workloads did overlap by hand. N is chosen with the first data; `measurements` needs at least three solo runs per class
   before an estimate is trusted.
2. The cost of the host sample (ps, vm_stat, lsof rounds) on `reserve`, `claim` and the 60 s snapshot, and the deadline margin: the
   3 s overall cap against a 2 s window leaves about 0.8 s, which a loaded host may not keep.
3. Calibration of `host_used` and the class estimates against measured resident peaks.
4. When enforcing, **RED must still export `min_jobs`** (and its test threads) rather than nothing; today RED exports nothing because
   nothing is enforced.
5. A guard for oversized integers in environment values and records (over 4300 digits makes `int()` raise in current Python); a
   follow-up, recorded here so it is not forgotten.

## Consequences

- Text that says "one heavy lane at a time" as a permanent rule is replaced with "one heavy lane today (a shadow budget is measured beside it; cap
  raised only after measurement, ADR 0017)" in this repository. The reason string the memory gate prints still contains the old sentence
  because tests pin it; it is accurate while the cap is 1.
- User-level copies (`~/.claude/CLAUDE.md`, installed skills) still carry the old wording until an owner-run deploy.
- A future cap of 2 changes one constant and its enforcing code path, behind this evidence, with the invariants above unchanged.

## Version history

| Rev | Change |
|---|---|
| 1 | Initial decision (SPE-150 PR7) |
