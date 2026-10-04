#!/usr/bin/env bash
# circuit-breaker-gate.sh — Track consecutive failures per ticket and block
# when the threshold is exceeded (circuit open).
#
# This script is a UTILITY, not a hook. Skills call it explicitly at phase
# boundaries. It is NOT wired into settings.json PostToolUse[Bash].
#
# Sub-commands:
#   check          <ticket> [threshold]  — exit 0 (closed) or exit 1 (open)
#   record-failure <ticket> [threshold]  — increment count; open at threshold
#   record-success <ticket>              — reset consecutive count to 0
#   reset          <ticket>              — manual clear (engineer use only)
#
# State files: ${CLAUDE_CIRCUIT_BREAKER_DIR}/<ticket>.json
# Format: {"ticket":"...","consecutive_failures":N,"state":"closed|open","threshold":N,"timestamp":"..."}

set -euo pipefail

[ -f "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}" ] && source "${ENGINEERING_CONFIG_ENV:-${HOME}/.claude/config.env}"

BREAKER_DIR="${ENGINEERING_CIRCUIT_BREAKER_DIR-${CLAUDE_CIRCUIT_BREAKER_DIR:-${ENGINEERING_STATE_DIR:-${HOME}/.claude}/circuit-breaker}}"
DEFAULT_THRESHOLD="${ENGINEERING_CIRCUIT_BREAKER_THRESHOLD-${CLAUDE_CIRCUIT_BREAKER_THRESHOLD:-5}}"
mkdir -p "$BREAKER_DIR"

# ── Helpers ──────────────────────────────────────────────────────────────────

_read_field() {
  local file="$1" field="$2" default="$3"
  # Shell-to-Python boundary: pass file path and field name via environment
  # variables — never interpolate them into Python source strings. A ticket
  # name containing a single quote (e.g. PROJ-123') would close the string
  # literal and cause a Python syntax error, silently returning the default
  # and preventing the circuit breaker from ever opening.
  _CB_FILE="$file" _CB_FIELD="$field" _CB_DEFAULT="$default" python3 - <<'PYEOF' 2>/dev/null || echo "$default"
import json, os, sys
f       = os.environ["_CB_FILE"]
field   = os.environ["_CB_FIELD"]
default = os.environ["_CB_DEFAULT"]
try:
    with open(f) as fh:
        d = json.load(fh)
    print(d.get(field, default))
except Exception:
    print(default)
PYEOF
}

_write_state() {
  local ticket="$1" failures="$2" state="$3" threshold="$4"
  local timestamp state_file tmp
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  state_file="${BREAKER_DIR}/${ticket}.json"
  tmp="$(mktemp "${state_file}.XXXXXX")"
  # Shell-to-Python boundary: pass ALL values via environment variables and
  # use a quoted heredoc (<<'PYEOF') so that no shell interpolation occurs
  # inside the Python source. failures/threshold are integers but may come
  # from user-supplied ticket context in future callers; passing them via
  # env vars is safer and consistent with the codebase convention.
  _CB_TICKET="$ticket" _CB_TIMESTAMP="$timestamp" \
  _CB_STATE="$state" _CB_FAILURES="$failures" _CB_THRESHOLD="$threshold" \
  python3 - > "$tmp" <<'PYEOF'
import json, os
print(json.dumps({
    "ticket":               os.environ["_CB_TICKET"],
    "consecutive_failures": int(os.environ["_CB_FAILURES"]),
    "state":                os.environ["_CB_STATE"],
    "threshold":            int(os.environ["_CB_THRESHOLD"]),
    "timestamp":            os.environ["_CB_TIMESTAMP"],
}, indent=2))
PYEOF
  mv "$tmp" "$state_file"
}

# ── check ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "check" ]]; then
  ticket="${2:-}"
  threshold="${3:-$DEFAULT_THRESHOLD}"
  [[ -z "$ticket" ]] && { echo "circuit-breaker check: ticket required" >&2; exit 1; }

  state_file="${BREAKER_DIR}/${ticket}.json"
  [[ ! -f "$state_file" ]] && { echo "[circuit-breaker] no state for ${ticket} — closed"; exit 0; }

  state="$(_read_field "$state_file" state closed)"
  failures="$(_read_field "$state_file" consecutive_failures 0)"

  if [[ "$state" == "open" ]]; then
    echo "[circuit-breaker] OPEN for ${ticket} — ${failures} consecutive failures (threshold: ${threshold})" >&2
    echo "Reset with: bash "${ENGINEERING_RUNTIME:-${HOME}/.claude/hooks}/circuit-breaker-gate.sh" reset ${ticket}" >&2
    exit 1
  fi

  echo "[circuit-breaker] closed for ${ticket} — ${failures} consecutive failures"
  exit 0
fi

# ── record-failure ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "record-failure" ]]; then
  ticket="${2:-}"
  threshold="${3:-$DEFAULT_THRESHOLD}"
  [[ -z "$ticket" ]] && { echo "circuit-breaker record-failure: ticket required" >&2; exit 1; }

  state_file="${BREAKER_DIR}/${ticket}.json"
  failures=0
  [[ -f "$state_file" ]] && failures="$(_read_field "$state_file" consecutive_failures 0)"
  failures=$((failures + 1))

  if [[ "$failures" -ge "$threshold" ]]; then
    new_state="open"
  else
    new_state="closed"
  fi

  _write_state "$ticket" "$failures" "$new_state" "$threshold"
  echo "[circuit-breaker] failure recorded for ${ticket}: ${failures}/${threshold} — state: ${new_state}"

  if [[ "$new_state" == "open" ]]; then
    echo "[circuit-breaker] CIRCUIT OPEN — escalate to the engineer before retrying" >&2
    exit 1
  fi
  exit 0
fi

# ── record-success ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "record-success" ]]; then
  ticket="${2:-}"
  [[ -z "$ticket" ]] && { echo "circuit-breaker record-success: ticket required" >&2; exit 1; }
  _write_state "$ticket" 0 "closed" "$DEFAULT_THRESHOLD"
  echo "[circuit-breaker] success recorded for ${ticket} — reset to closed"
  exit 0
fi

# ── reset ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "reset" ]]; then
  ticket="${2:-}"
  [[ -z "$ticket" ]] && { echo "circuit-breaker reset: ticket required" >&2; exit 1; }
  rm -f "${BREAKER_DIR}/${ticket}.json"
  echo "[circuit-breaker] reset for ${ticket}"
  exit 0
fi

# ── Unknown sub-command ───────────────────────────────────────────────────────
echo "circuit-breaker-gate: unknown sub-command '${1:-}'.
Usage: circuit-breaker-gate.sh <check|record-failure|record-success|reset> <ticket> [threshold]" >&2
exit 1
