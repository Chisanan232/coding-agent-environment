# Memory-admission latch policy: P2 (shipped on) and P1 (shipped off) (SPE-177, owner decision)

Status: reviewed draft, not merged, not deployed. It changes when heavy work is admitted, so merging and deploying it is the owner's
decision. No threshold value changes. Two independent switches in `scripts/memory-admission.py`:

| Switch | Default in this change | Effect |
|---|---|---|
| `CONSERVATIVE_RECOVERY` (P2) | `True` | a latch whose only failing exit predicate is a compressor below ENTER can be released after sustained calm |
| `LATCH_ONLY_ON_MEMORY_REASONS` (P1) | `False` | lane refusals stop arming the latch. Off by default because it reverses behaviour pinned by `tests/test-reservation-veto.py` (`CapOneUnchanged`: "hysteresis after a tree ends is unchanged"); enable on its own decision |

Reverting the pull request, or setting `CONSERVATIVE_RECOVERY = False`, is the rollback.

## The defect (measured 2026-11-19)

A latch can be armed by any refusal reason, including lane reasons. A latched gate is released only by the stricter EXIT thresholds
(compressor 25% against ENTER 35%). Compressed pages are a stock owned by long-lived processes and nothing at pressure level 1 evicts
them, so a latch could refuse for hours with pressure 1, swap I/O 0 and load 0.4 to 0.7 per core: 26.1% against 25%. A fresh gate with
the same metrics admits (it uses ENTER). The compressor fell only about 1 MB a minute while the host was idle.

## Exact predicates

Heavy-work refusal reasons are unchanged (ENTER): pressure 2 or more, swap I/O at least 16 MiB/s, compressor at least 35% of RAM, load per
core at least 4.0, swap nearly full and unable to grow, any unreadable input (fail closed). Standard EXIT is untouched: 3 good checks at
least 30 s apart satisfying every EXIT threshold.

**P2 eligible reading** (all required): latched; no refusal reason of any kind (so no lane reason either); no unknown input; pressure level
exactly 1; swap I/O at most 1.0 MiB/s; load per core at most 2.0; compressor reading present (below 35% because ENTER passed).

**P2 run and release:**
- A counted sample is an eligible reading at least 60 s after the previous counted sample (at most once per 60 s whatever the number of callers).
- Release needs all of: 10 counted samples; (last sample time minus first sample time) at least 540 s, taken from recorded times (appends are already
  spaced, so this mainly guards against hand-edited state; a writer of the state file can always write `latched: false` directly); and (maximum minus minimum compressor ratio) at most 0.005 across the run.
- A run is discarded when the next eligible reading arrives more than 300 s after the last counted one (exactly 300 s still counts).
  Consequence: the poller cadence must be between 60 s and 300 s; a poller slower than 300 s can never release.
- Release clears the latch only. The next check is the ordinary ENTER decision, so lane 1 admits as a fresh gate would, and a second lane
  still needs the exit bounds (`concurrent_lane_reasons`, `exit_ok`, `budget_band` are untouched).

**Recovery duration:** at least 9 minutes of calm after the last disturbance; about 10 to 12 minutes with a 60 s poll, about 20 minutes with
a 120 s poll. Measurement can lag the recorded time by up to the 10 s sampling window.

**P1 (off by default):** only a non-lane reason arms the latch (a lane reason is a refusal whose text contains "heavy lanes full" or starts
"a second heavy lane needs"). A lane reason still refuses that check and does nothing to the latch. A mixed check latches.

## Where the run is stored (sidecar)

The P2 run (`p2_hist`, `p2_last`, `p2_first`) lives in `<latch file>.p2`, beside the latch file and written under the same state lock.
Reason, measured on 2026-11-19 right after P2 was deployed: a checkout without P2 (an old feature worktree used by another session's
poller) rewrites the latch file with its four keys on every check, which erased a run stored inside it, so the counter restarted at one
sample each time. With the sidecar an old caller's check cannot touch the run. A missing, corrupt, oversized, symlinked or hostile sidecar
is ignored (no run, so no release); a latch file written by the first P2 layout (keys inside) still loads and migrates on its next save.
The predicates, spacing, dwell and thresholds are unchanged. Residual: a check by an old caller does not clear the run when it sees a busy
lane, so a busy period is caught by the maintained pollers' own checks (they run every 15 s while a waiter exists), not by the old one.

## Worse conditions

- Any ENTER violation (pressure 2 or more, swap I/O 16 MiB/s or more, compressor 35% or more, load 4.0 or more, unreadable input) latches at
  once, even if the gate was released a moment earlier, and clears the P2 run.
- Any reading that is not eligible (swap I/O above 1.0 MiB/s, pressure not 1, load above 2.0, any lane reason) clears the run; the latch stays
  and the run restarts from zero.
- A compressor that oscillates or creeps more than 0.005 across the run never releases.
- While any heavy lane is busy P2 cannot run. Practical consequence: recovery needs roughly ten idle minutes in which every poller sees a
  quiet host; under constant churn neither P2 nor EXIT releases (the latch only matters when no lane is running).

## Risks and what was tested

| Risk | Control | Test (tests/test-memory-admission.py) |
|---|---|---|
| a single good sample releases | 10 samples, 540 s recorded dwell | `test_p2_needs_the_spacing`, `test_release_needs_the_recorded_dwell_not_just_ten_samples`, `test_a_forged_nine_sample_run_cannot_release_in_one_check` |
| concurrent callers accelerate the streak | state lock around read-decide-write, clock read inside it, spacing in wall time | `test_eight_callers_at_the_same_instant_count_as_one_good_check`, `test_callers_inside_the_spacing_do_not_advance_the_streak`, `test_the_p2_run_grows_one_sample_per_spacing_not_per_caller`, `test_critical_sections_never_overlap_under_a_burst`, `test_a_burst_never_releases_a_latch_on_one_instant` |
| release while the host worsens | ENTER latches at once; any ineligible reading clears the run | `test_a_memory_reason_still_latches_and_a_pressure_spike_rearms_it`, `test_any_blocker_other_than_the_compressor_resets_p2`, `test_worsening_pressure_in_a_burst_relatches_and_clears_the_run`, `test_p2_never_releases_at_or_above_the_enter_threshold`, `test_an_oscillating_compressor_never_releases` |
| stale evidence | run discarded after 300 s of silence | `test_sparse_checks_days_apart_never_release` |
| a release admits too much | release only un-latches; the next check equals a fresh gate | `test_a_released_gate_admits_only_what_a_fresh_gate_admits`, `test_a_second_lane_after_a_release_is_still_judged_by_the_exit_bounds` |
| tampered or old state | field validation, p2_first/p2_last/hist checks, old files still load | `test_tampered_p2_state_cannot_skip_the_run`, `test_state_file_round_trips_the_p2_fields_and_old_files_still_load` |
| rollback | both switches | `test_both_rollback_switches_restore_the_old_behaviour`, `test_shipped_defaults_are_p2_on_and_p1_off` |
| P1 semantics (off by default) | explicit tests with the switch on | `test_p1_*` |

Risk accepted with P2: compressed memory that stays flat for 10 calm minutes no longer holds lane 1 shut at 25 to 35%. The earlier design
accepted ENTER 35% for a fresh gate; a release returns exactly there. If the first job then pushes pressure to level 2 the gate latches
again immediately.

## Options for recovering the current latch (available options, estimated outcomes)

Figures below were measured by the author on 2026-11-19 on this host and are not backed by repository evidence; the 10 to 12 minute estimate
follows from the code (540 s of recorded dwell plus polling and sampling lag).

1. **Deploy P2** (this change). Estimated: released about 10 to 12 minutes after the first polling caller runs on the new code, if the host
   stays calm (it has been: pressure 1, swap 0, load below 1). Needs a merged change in the canonical checkout.
2. **Owner stops idle memory holders** (no code). Five other idle rust-analyzer processes hold about 6.8 GB of footprint in other sessions.
   Stopping the first one recovered about 37% of its footprint (2.7 of 7.3 GB); the compressor needs about 1.5 GB less, so stopping the
   largest one or two would probably, not certainly, reach 25% within minutes. It interrupts those sessions' language servers.
3. **Natural decay.** Observed about 1 MB per minute: not a plan (about a day).
4. **Owner resets the latch** (explicitly not done by this session): immediate, but it discards the protection state without a reason
   recorded.

## Deployment and rollback (after approval)

```bash
# merge through the normal gate, then update the canonical checkout (fast-forward only)
cd ~/Bryant-Developments/coding-agent-environment-main-view && git pull --ff-only
python3 scripts/memory-admission.py status        # read-only: confirms the new code runs
# recovery needs a poller: the existing queue callers (heavy-run, la-cargo) and the stall monitor's `queue advance` call the gate
# rollback: git revert the merge commit (or set CONSERVATIVE_RECOVERY = False) and pull; the state file keeps working (extra keys ignored)
```

## Shadow measurement and revert trigger (P1 and later tuning)

Record what P1 would have done beside the live decision for at least three days before enabling it. Revert P1 if pressure level 2 or
higher, or swap I/O above 16 MiB/s, appears within 30 minutes of a release the shadow would have made.
