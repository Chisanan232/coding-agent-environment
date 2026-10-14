# Managed resources

Managed resource holds are opt-in per launch. Set both
`CODING_AGENT_MANAGED_RESOURCE_ID` and
`CODING_AGENT_MANAGED_RESOURCE_GENERATION` to acquire an already-created managed generation. With both
unset, `ca-claude` and `ca-codex` retain their existing launch behavior. Setting
only one is a refusal. Managed mode also requires an existing session identity:
`CODING_AGENT_RESOURCE_SESSION_ID`, `CODEX_THREAD_ID`, or `CODEX_SESSION_ID`.
The adapter never invents a session ID. IDs must refer to a Glomeris-created
generation; an arbitrary existing directory cannot be enrolled.

This initial resource class covers only standalone non-executable Rust objects
produced by Glomeris from a retained committed input. Magic headers are not provenance, and sealing
cannot adopt arbitrary or fabricated compiled files. Create a generation from
a clean, committed Rust workspace with `Cargo.toml` and a recipe:

```bash
glomeris resource create --source-root GIT_WORKSPACE --recipe RECIPE
glomeris resource produce ID GEN --input TRACKED_RS_FILE
```

For production, Glomeris snapshots bounded tracked Rust source from the pinned
commit into private retained inputs, then runs isolated object-only `rustc`
with dependency information. It rejects dependencies outside that input set
and environment-provided dependencies, and binds produced outputs to the
generation manifest identity. Only those recorded outputs are in scope. This
does not authorize broad cache clearing, source cleanup, or removal of outputs
from another generation. Keep incomplete or unconfirmed child scope preserved.
Sealing cannot restore consumer closure after native-agent exposure.

The adapters resolve Glomeris through the selected host's installed
engineering-runtime bootstrap. Launch proceeds only when that bootstrap reports
the pinned installation as verified by its existing receipt and executable
digest checks. Environment binary overrides do not select the core.

The adapter reads Glomeris's automatic `held` acknowledgment and verifies that
its resource ID, generation, and absolute path match the requested hold. That
single acknowledgment confirms acquisition; there is no second hold request.
The adapter passes the returned path to the native process as
`GLOMERIS_RESOURCE_DIR`. It records pending child
scope before launch, registers the child PID and process-group ID after spawn,
renews the hold every 20 seconds while the child is alive, and requests release
only after observing child termination. Child-pending, registration, renewal,
and release acknowledgments must have status exactly `ok`. Renewal carries no
fabricated progress. A refused or unconfirmed initial hold prevents launch. If
child registration or renewal cannot be confirmed, the adapter leaves the
useful child running, preserves the persisted hold, and leaves its outputs
intact for core reconciliation. A short-lived child that exits before its
registration acknowledgment is confirmed also leaves an unconfirmed claim.
The child has its own process group; the adapter sends no termination signals.
After the direct child exits, Glomeris checks whether that group still has live
detached workers before accepting release. This is not a guarantee that every
host-native descendant is registered or covered; uncertain child coverage
remains a blocker. A failed or unrecognized scoped-operation acknowledgment
prevents a later release attempt.

The shared Glomeris hold and kernel barrier remain authoritative across active
and idle host sessions, including adapter crashes. Registered live and idle
claims always protect their resources. Native orphans with unconfirmed agent
child coverage remain explicit blockers. Incomplete child scope may cause
release to be refused; unknown state remains preserved for native
reconciliation. Exposure to a native agent permanently invalidates the
producer's closed-consumer proof, so release cannot trigger automatic sealing
or cleanup of its outputs. Retained exposed outputs stay blocked until a stronger, approved consumer-closure
protocol exists; manual sealing cannot remove this blocker. Exec-transition and
boot-ID checks protect process identity.
Renewal uses a short per-resource metadata flock. An explicit `busy` reply
means the renewal was not applied and the barrier remains held; the adapter
retries that same request for up to two seconds. Lost or unrecognized replies
are not retried because the operation may have been applied. Lifecycle
decisions use typed classifier evidence, not artifact headers.

`codex/resource-policy.json` supplies schema 1 lifecycle defaults and limits
for Glomeris's `managed_generated` class: a configurable 24-hour idle
threshold, three maximum actions, and 5 GiB maximum bytes. Authorization also
requires the existing native Autopilot grant envelope and all core safety
gates; both the lifecycle policy and grant envelope must allow the class. This
file creates no separate unattended authority. Use `scripts/profile-install.sh --resource-policy --dry-run` to inspect,
`--apply` to enable only this class through native CAS, and `--rollback` to
restore the transaction snapshots. `scripts/sync-check.sh --resource-policy`
verifies effective state. The installer keeps a private resumable journal and
refuses user drift. No global Cargo or temporary-directory override is applied.

`python3 scripts/launcher-install.py` installs the complete reviewed launcher
runtime under `~/.local/lib/coding-agent-environment/`, including the unchanged
Codex routing validator and its canonical role files. Existing regular wrappers
in `~/.local/bin` are preserved. Use the installed private runtime launchers
explicitly when existing aliases are not managed. Native launch without managed
resource variables retains its previous behavior.

Rolling upgrades retain exact verified current/previous artifacts. Deploy the
reviewed resolver to both owned hosts before selecting the new core; then update
both host pins. No artifact-directory search or corrupt-current fallback occurs.
Rollback revokes managed authority first, restores old host pins, then restores
engine selection and the owned monitor binding. Keep both immutable binaries
and private backups until verification finishes.

Interrupted rollback journals the observed policy state and restore intent.
Only that exact pre-restore state or the expected restored result can resume;
unexpected disappearance and user drift remain refusals.

## Core-only artifact exports

`glomeris resource export ID GENERATION --artifact artifact.o --destination
ABSOLUTE_PATH` copies one sealed, producer-verified object into an existing
user-owned directory. It refuses overwrite, redirected paths, changed artifacts
and generations previously exposed through an arbitrary native hold. The core
retains the source barrier until its descriptors close and its access outcome is
durable; native consumers receive an independent copy, not source access.
Exported copies belong to their destination owner and are outside this cleanup
authority. Partial copies remain preserved after errors or interruption.

An interrupted core-only export can reconcile verified holder death as ended
source access, without reporting successful export. Its observed end is a
conservative inactivity bound. Normal 24-hour retention and all existing
class, budget and execution gates still apply. No additional cleanup class is
enabled by export, and it does not establish complete native-session custody,
general Cargo target ownership or worktree release authority.

Access records retain `read_claims`. Older strict readers reject those records
and preserve their resources, including with an enabled cleanup envelope.
Rollback must preserve the records and exported data; do not remove claims to
make an older binary accept them. Both hosts and the owned monitor must use the
reviewed core pin before relying on this additive capability.
