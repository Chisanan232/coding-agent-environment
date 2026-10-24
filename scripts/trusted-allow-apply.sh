#!/usr/bin/env bash
# Owner-run: plan, apply, verify and roll back the single owner-merge-runner allow rule (SPE-118 Trusted ALLOW).
# Same modes and safeguards as ask-first-apply.sh (plan by default; --apply; --rollback). Requires the ASK-first candidate to be applied and the
# runner installed (scripts/install-owner-merge-runner.sh); the plan refuses otherwise.
set -euo pipefail
ASK_FIRST_KIND=trusted-allow exec "$(dirname "${BASH_SOURCE[0]}")/ask-first-apply.sh" "$@"
