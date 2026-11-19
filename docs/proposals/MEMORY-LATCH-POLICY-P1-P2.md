# Memory-admission latch policy: P1 and P2 (SPE-177, draft for owner decision)

Status: DRAFT, not merged, not deployed. It changes when heavy work is admitted, so it needs the owner's decision. No threshold value
changes. Both parts have a rollback switch (`LATCH_ONLY_ON_MEMORY_REASONS`, `CONSERVATIVE_RECOVERY` in `scripts/memory-admission.py`);
reverting the pull request is the full rollback.

## The defect (measured 2026-11-19)

`decide()` latched on any refusal reason, including lane reasons ("heavy lanes full", "a second heavy lane needs ..."). A latched gate is
released only by the stricter EXIT thresholds (compressor 25% against ENTER 35%). Compressed pages are a stock owned by long-lived
processes and nothing at pressure level 1 evicts them, so a latch armed by a lane refusal could refuse forever with pressure 1, swap I/O 0
and load 0.4 per core: 26.1% against 25% for hours. A fresh gate with the same metrics admits (it uses ENTER).

## Exact predicates

Heavy-work refusal reasons are unchanged (ENTER): pressure level 2 or more, swap I/O at least 16 MiB/s, compressor at least 35% of RAM,
load per core at least 4.0, swap nearly full and unable to grow, any unreadable input (fail closed).

| | Rule |
|---|---|
| **P1** latch trigger | Only a non-lane reason arms the latch (`latched=True`, `ok_streak=0`, P2 run cleared). A lane reason (text contains "heavy lanes full" or starts "a second heavy lane needs") still refuses that check, and does nothing to the latch. If both kinds occur in one check, the memory reason latches. |
| **P1** second lane | Unchanged: `concurrent_lane_reasons` and `exit_ok` still require swap I/O at most 4 MiB/s, compressor at most 25%, load per core at most 2.0 and pressure 1. |
| **P2** eligible reading | Latched, and all of: no refusal reason of any kind (so no lane reason either), no unknown input, pressure level exactly 1, swap I/O at most 1.0 MiB/s, load per core at most 2.0, compressor reading present (below 35% because ENTER passed). |
| **P2** run | A counted sample is an eligible reading at least 60 s after the previous counted sample. The run holds the last 10 compressor ratios. |
| **P2** release | 10 counted samples and (maximum minus minimum compressor ratio) at most 0.005 across the whole run. Release clears the latch; the next check is the ordinary ENTER decision. |
| **P2** dwell | At least 9 spacings between the first and the last sample: 540 s minimum, about 10 to 12 minutes at a 60 s poll, about 20 minutes at a 120 s poll. |
| **P2** sampling interval | Counted at most once per 60 s whatever the number of callers. A run is discarded when the next eligible reading arrives more than 300 s after the last counted one, so ten snapshots days apart never release. |
| Standard EXIT | Untouched: 3 good checks at least 30 s apart satisfying every EXIT threshold. |

## Worse conditions

- Any ENTER violation (pressure 2 or more, swap I/O 16 MiB/s or more, compressor 35% or more, load 4.0 or more, unreadable input) latches at
  once, even if the gate was released a moment earlier, and clears the P2 run.
- An eligible-reading failure (swap I/O above 1.0 MiB/s, pressure not 1, load above 2.0, any lane reason) clears the P2 run without
  latching further; the latch stays on and the run restarts.
- A compressor that oscillates or creeps more than 0.005 across the run never releases.
- While any heavy lane is busy P2 cannot run (a lane reason makes the reading ineligible), so a loaded host cannot be released by P2.

## Evidence in the tests (tests/test-memory-admission.py, class `LatchPolicy` and `ConcurrentPolling`)

- Lane-capacity refusal cannot create a latch: `test_a_lane_reason_refuses_but_does_not_latch`; the old behaviour is reproduced behind the
  switch (`test_the_2026_11_19_shape_a_fresh_gate_admits_what_a_lane_latched_gate_refused`).
- Sustained healthy evidence: `test_a_latch_with_only_the_compressor_blocking_releases_after_ten_calm_spaced_checks`; one good sample
  never releases (`test_p2_needs_the_spacing`, `test_a_burst_never_releases_a_latch_on_one_instant`).
- Deterioration: `test_a_memory_reason_still_latches_and_a_pressure_spike_rearms_it`, `test_any_blocker_other_than_the_compressor_resets_p2`,
  `test_worsening_pressure_in_a_burst_relatches_and_clears_the_run`, `test_p2_never_releases_at_or_above_the_enter_threshold`,
  `test_an_oscillating_compressor_never_releases`, `test_sparse_checks_days_apart_never_release`.
- A release admits only what a fresh gate admits: `test_a_released_gate_admits_only_what_a_fresh_gate_admits`; a second lane stays on the
  exit bounds: `test_a_second_lane_after_a_release_is_still_judged_by_the_exit_bounds`.
- Coordinated polling: the whole read-decide-write runs under the state lock and the clock is read inside it, so concurrent callers are
  serialized and the 30 s (EXIT) and 60 s (P2) spacings apply to wall time, not to caller count:
  `test_eight_callers_at_the_same_instant_count_as_one_good_check`, `test_callers_inside_the_spacing_do_not_advance_the_streak`,
  `test_the_p2_run_grows_one_sample_per_spacing_not_per_caller`. The state file stays parseable after every burst.
- State safety: `test_tampered_p2_state_cannot_skip_the_run`, `test_state_file_round_trips_the_p2_fields_and_old_files_still_load`, and the
  existing atomic write plus advisory lock.

## Consequence for the 2026-11-19 latch

P1 alone does not release the latch that is already on (nothing arms it again, but only EXIT or P2 clears it). With P2 and a poller, the
current state (pressure 1, swap I/O 0, load 0.4 to 0.6, compressor 26.1% and flat) would release after about ten minutes of calm. Release
is not admission: lane 1 then admits as a fresh gate would, and a second lane still needs the exit bounds.

## Shadow measurement and revert trigger

Before enforcement: log what P1 and P2 would have done beside the current decision for at least three days. Revert if pressure level 2 or
higher, or swap I/O above 16 MiB/s, appears within 30 minutes of a release the shadow would have made.
