---
name: release-watch
description: "Monitor the automated release CI pipeline after `release-preparation` completes. Report pipeline state at each observation interval. Summarize the final outcome when the pipeline completes or fails."
---

# SKILL.md — release-watch

## Purpose
Monitor the automated release CI pipeline after `release-preparation` completes.
Report pipeline state at each observation interval. Summarize the final outcome
when the pipeline completes or fails.

## Type
Auto-used. Run after `release-preparation` signals completion, and at each
polling interval until the release pipeline resolves.

## Do Not Assume
- Do not assume the pipeline is progressing just because it started.
- Do not assume a long-running pipeline is hung — check the last active step first.
- Do not assume a failed pipeline step is permanent — check if it is retryable.

## Steps

### Phase 1 — Establish observation baseline
1. Identify the release pipeline trigger:
   - Tag created by the automated workflow, or
   - Workflow dispatch on a release branch.
2. Locate the CI pipeline run linked to the release:
   - Use GitHub MCP to find the workflow run associated with the release tag or branch.
3. Record the pipeline start time, trigger ref, and workflow name.

### Phase 2 — Observe at each polling interval
4. Fetch the current pipeline status via GitHub MCP.
5. Record the state of each job: queued / in-progress / success / failure / cancelled.
6. If all jobs are success → proceed to Phase 3 (success summary).
7. If any job is failure → proceed to Phase 4 (failure report).
8. If pipeline is still in-progress → record current state and wait for next interval.

### Phase 3 — Success summary
9. Confirm the exact release tag/ref associated with the authoritative run.
10. Confirm any published artifacts are accessible (if applicable).
11. Confirm the changelog or release notes are published on GitHub Releases.
12. Produce the success summary (see Output format).
13. If Slack MCP is available, post the success summary to the configured release channel.

### Phase 4 — Failure report
14. Identify the failing job and step.
15. Retrieve the log excerpt for the failure.
16. Classify the failure:
    - Transient (network, runner): recommend manual re-run.
    - Configuration error: identify the misconfigured step.
    - Code error: identify the commit that introduced the failure.
17. Produce the failure report (see Output format).
18. Do not attempt to repair the pipeline — escalate to engineer with the report.
19. If Slack MCP is available, post the failure alert to the configured release channel.

## Output

Apply [evidence-first-briefing](../evidence-first-briefing/SKILL.md) to persistent/shared output.
On publication, report shipped behavior/capability and breaking or migration risk,
with release/artifact and run references. Confirm the exact release ref against
its authoritative run; do not infer publication from the newest local tag.

On failure, report impact, failed gate and relevant run/log reference, material
uncertainty and required owner action. Include only the log excerpt needed to
understand or reproduce the failure. Do not copy every successful job or duration
unless it changes the decision. Routine polling stays in observation state;
notify only when decision-relevant state changes and posting is authorized.

## Safe-Fix Guidance
- Do not re-trigger the pipeline without engineer confirmation.
- Do not attempt to manually complete a partially failed release.
- If the pipeline is stuck (no progress for more than 30 minutes), report it and
  escalate — do not assume it will self-recover.
