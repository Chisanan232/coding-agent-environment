# Shared workflow cleanup contract

Reuse the existing engineering-workflow and post-merge-close shared sources.
Glomeris owns storage discovery, evidence, policy, typed execution, recovery,
audit and its one monitor. The canonical configuration repository owns managed
integration/pins/installation. Host adapters select native configuration and
process handles; they do not implement another cleanup engine.

## Closeout state

The engineering-runtime closeout helper holds one kernel lock per normalized
repository+PR operation across observe/act/verify/record. Both hosts use the same
user-local receipt store, without sharing private session history/auth/state.
The binding includes repository, PR, exact merged head/merge SHA, ticket, common
Git directory, canonical worktree, branch/base and remote. A changed binding,
corrupt receipt, inaccessible state or conflicting holder refuses.

Outcomes remain separate: ticket acceptance/closure, worktree removal, remote
and local branch removal, context and reporter handling. A receipt records
verified outcomes, never supplies merge acceptance or deletion authority.
After a crash, re-observe external state before retrying: an action can succeed
before its receipt was written. Completed receipts remain available. Old
host-private PR-only checkpoints are preserved and cannot be auto-imported
without verified repository/merge binding.

Native background handles must keep the holder stdin open and remain observable.
A missing/dead handle means stop further mutation, reacquire and re-observe.
Use `closeout.py status --binding FILE` to inspect one binding (it may create
its private lock directory/file, but never a receipt or delete a resource).
No started flag is a completion claim. Cooperative callers are serialized;
this lock cannot stop unrelated programs, Git or tracker users from changing
resources. Revalidate exact identities before acting.

## Retirement boundary

Only this workflow's explicitly owned unused worktree can be removed after
fresh exact-head merge/acceptance verification, settled owned jobs, clean Git
state including untracked/ignored/unique artifacts, and complete fresh process
probes. Unknown ownership implies preservation. An empty lsof result alone
cannot prove a long-lived session has released a path. Time, PID alone, expired
display trackers and Done tickets grant no authority. Ordinary non-force Git
removal is the existing executor for an owned worktree; no generic deletion,
force cleanup, broad prune or cache bypass is added.

Context writers remain uncoordinated across older skills and live sessions.
Preserve their markers and report `context_changed_or_uncoordinated`; even a
matching compare-then-unlink can race a concurrent reassignment. Already absent
markers can be verified as complete. Until a coordinated writer contract is
available, pass explicit ENGINEERING_CURRENT_* environment values for new work.
This is a real limitation, not a completed automatic marker-cleanup feature.

## Installation and rollback

Existing profile installation supports a bounded shared-skill selection:

```bash
scripts/profile-install.sh --global --shared-skills=engineering-runtime,post-merge-close --dry-run
scripts/profile-install.sh --global --shared-skills=engineering-runtime,post-merge-close
scripts/profile-install.sh --global --shared-skills=engineering-runtime,post-merge-close --check
scripts/profile-install.sh --claude-skills --shared-skills=engineering-runtime,post-merge-close --dry-run
```

The shared census still validates against the full manifest; selection only
narrows mutation. Instruction blocks, profiles, external packages, unrelated
skills, hooks, notification/status mechanisms, credentials, trust, history and
private overrides are untouched. Existing preflight/backups/atomic replacement
remain the implementation. Restore only the changed selected paths from the
reported backup after checking no intervening local edits. Newly installed files
have no prior version; remove only verified installer-owned new paths if rolling
back. Applying twice produces no further changes/backups.

## Proof limits and lifecycle

Disposable receipt tests prove cross-host contention, repository namespacing,
corruption/conflicting-binding refusal, interruption recovery and retained
completion evidence. Installation tests prove selected-path preservation,
idempotency and restoration. They do not prove that a model obeys the skill,
that every client invokes cleanup, or that arbitrary stale worktrees are unowned.

Codex native hooks support SessionStart/SessionEnd/Interrupt/subagent events,
but non-managed definitions need native exact-definition review/trust. SessionEnd
is advisory and cannot cover crashes; Stop is a turn boundary, not session end.
Do not change trust state or bypass it to claim automatic lifecycle parity.
See the [official hooks contract](https://learn.chatgpt.com/docs/hooks).
Shared receipts survive interrupted holders; arbitrary unattended retirement
requires a supported owner/session contract and separate empirical validation.

## Finalization receipt compatibility

Receipt schema 2 adds three verified finalization outcomes: workflow archival,
durable decision recording and exact-scope note cleanup. All six lifecycle
outcomes and all three finalization outcomes are required by `finish`.
Consumers must use `effective_complete`; a schema-1 historical `complete` flag
proves only the older six-outcome contract, not full finalization.

Status and entering hold preserve legacy bytes. The first valid
`mark_finalization` request upgrades a valid legacy receipt atomically, retaining
its immutable binding and original evidence, setting effective completion false
until finalization is verified. Never replay deletion or append duplicate decisions
merely because an older receipt lacks their checkpoint. Observe existing results.
Corrupt or conflicting receipts refuse unchanged. Install the new helper in both
hosts before upgrade: old strict readers refuse schema 2 rather than guessing.
No helper request executes archive, note deletion, logging or external cleanup;
the workflow retains responsibility for ownership, execution and verification.
