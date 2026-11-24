#!/usr/bin/env python3
"""Durable session status and meaningful-transition notifications from Claude Code hooks (SPE-120).

  session-notify.py hook                         read one hook event on stdin; ALWAYS exits 0, never blocks Claude
  session-notify.py status [--json]              table of tracked sessions (read-only)
  session-notify.py sweep [--now EPOCH]          stale-session detection, delivery retry, old-record cleanup
  session-notify.py checkpoint --state S ...     write the structured checkpoint a session uses to declare its state
  session-notify.py emit-resource --cause C --recovery R --owner-action A [--signature HEX16] [--detail D] | --clear [--scope admission|auth]   stall and sign-in notices
  session-notify.py test-notice [--strict]       one fixed, clearly labelled delivery-test notice (no arguments carry text)
  session-notify.py render-config [--profile personal|corporate] [--out FILE]

One short-lived process per hook, no daemon. Per-session state lives under
`~/.cache/coding-agent-environment/session-status/` (`SESSION_NOTIFY_STATE_DIR` overrides): 0700 directory, 0600 files, atomic
replace, O_NOFOLLOW, an flock per session; corrupt, oversized, non-regular or symlinked state is never trusted (no change, no
notification, reported by `status`). Backends are OFF unless the config file enables them; the config holds environment-variable
NAMES or file paths for credentials, never values.

Truth model: Stop and SessionEnd are NOT project completion. State changes to COMPLETED or BLOCKED_* come only from the structured
checkpoint file (`checkpoint` subcommand), never from model prose or the hook payload's text fields (`message`,
`last_assistant_message` and `transcript_path` are never read). COMPLETED additionally needs a `verified` record and a checkpoint
written after the current run started (every SessionStart and UserPromptSubmit opens a new run) and not already acted on. Forced termination (kill -9, power loss) cannot run hooks: `sweep` detects the silence afterwards.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

STATES = ("RUNNING", "COMPLETED", "BLOCKED_OWNER", "BLOCKED_DEPENDENCY", "BLOCKED_RESOURCE", "FAILED", "IDLE", "SESSION_ENDED")
NOTIFIABLE = {"COMPLETED", "FAILED", "BLOCKED_OWNER", "BLOCKED_DEPENDENCY", "BLOCKED_RESOURCE"}
BLOCKED = {"BLOCKED_OWNER", "BLOCKED_DEPENDENCY", "BLOCKED_RESOURCE"}
CHECKPOINT_STATES = {"RUNNING", "COMPLETED", "FAILED", "IDLE"} | BLOCKED
# Hook events and notification types verified against Claude Code 2.1.292 (binary strings and the official hooks reference).
OWNER_BLOCK_TYPES = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
STOP_FAILURE_KINDS = {"rate_limit", "overloaded", "authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error",
                      "invalid_request", "model_not_found", "server_error", "max_output_tokens", "cloud_credential_error", "unknown"}
END_NOTIFY_REASONS = {"other", "logout"}
REMOTE_BACKENDS = ("ntfy", "pushover", "telegram")
BLOCKER_OWNERS = {"owner", "dependency", "resource", "ci", "reviewer", "other"}
# Everything a notification can say about a resume condition is one of these fixed phrases; there is no free-text field.
RESUME_TRIGGERS = {"owner_approval": "owner approval", "owner_decision": "owner decision", "dependency_merged": "dependency merged",
                   "ci_green": "CI green", "review_complete": "review complete", "resource_available": "resource available",
                   "credentials_provided": "credentials provided", "manual_restart": "manual restart", "other": "other"}
KIND_TEXT = {"STALE": "no heartbeat", "ENDED": "ended while running", "AGENT": "background agent finished"}
ALLOWED_KINDS = set(KIND_TEXT.values()) | STOP_FAILURE_KINDS
# Admission-monitor notices (SPE-176): every word of the notification comes from these fixed phrases; no ids, numbers, paths or free text.
CAUSE_TEXT = {"disk-floor": "disk below floor", "memory-pressure": "memory pressure", "memory-hysteresis-static-compressor": "memory hysteresis, static compressor",
              "memory-hysteresis": "memory hysteresis", "stale-claim": "stale claim", "frozen-queue": "queue frozen", "failed-continuation": "continuation failed",
              "unclaimed-handoff": "hand-off grant unclaimed", "queue-wait-excessive": "queue wait excessive", "workload-over-deadline": "workload over deadline",
              "outdated-caller": "outdated caller", "stalled-running": "running work stalled", "unobservable": "admission unobservable",
              "state-unreadable": "admission state unreadable",
              "obsolete-wrapper-terminated": "obsolete wrapper terminated", "abandoned-unstarted-cancelled": "abandoned grant cancelled",
              "failed-to-terminate": "wrapper failed to terminate",
              # Claude Code sign-in watch (SPE-176, ADR 0020): own session record, own titles; `detail` is a closed bounded field (AUTH_DETAIL) for these causes only.
              "auth-required": "Claude Code sign-in required", "remote-control-disconnect": "Remote Control disconnected",
              "signin-recovered": "Claude Code sign-in recovered",
              # Closing notice of an admission incident the monitor already raised (SPE-188): spool kind `recovered`, same incident_ref as its opened/escalated notices.
              "incident-resolved": "incident resolved"}
RECOVERY_TEXT = {"none": "none tried", "advance-tried": "queue advance tried", "recovery-exhausted": "recovery exhausted", "observe-only": "observe only",
                 "lapsed": "grant lapsed", "wait-expiry-near": "wait expiry near", "wrapper-terminated": "wrapper terminated",
                 "grant-cancelled": "grant cancelled", "termination-failed": "termination failed",
                 "no-auto-recovery": "no automatic recovery", "remote-control-only": "remote control only", "sessions-resumed": "sessions resumed",
                 "resolved": "resolved by the scheduler"}
OWNER_ACTION_TEXT = {"reclaim-disk": "reclaim disk", "reduce-load": "reduce load", "approve-memory-policy": "approve memory policy",
                     "wait-or-approve-memory-policy": "wait or approve memory policy", "confirm-claim-ended": "confirm claim ended",
                     "restart-waiting-session": "restart waiting session", "restart-outdated-caller": "restart outdated caller",
                     "inspect-admission-state": "inspect admission state", "inspect-running-workload": "inspect running workload",
                     "run-queue-advance": "run queue advance", "inspect-memory-admission": "inspect memory admission",
                     "claim-or-reenqueue-handoff": "claim or re-enqueue hand-off", "check-queued-wait": "check queued wait",
                     "check-over-deadline-workload": "check over-deadline workload", "check-recovery-result": "check recovery result",
                     "inspect-stuck-wrapper": "inspect stuck wrapper",
                     "sign-in-claude": "sign in to Claude Code", "reconnect-remote-control": "reconnect Remote Control",
                     "verify-resumed-work": "verify resumed work", "no-action-needed": "no action needed"}
AUTH_SESSION = "claude-auth-watch"   # synthetic session of the sign-in watch notices: its dedup, pending queue and clear are independent of the admission monitor's
AUTH_CAUSES = {"auth-required", "remote-control-disconnect", "signin-recovered"}
AUTH_RECOVERY = {"no-auto-recovery", "remote-control-only", "sessions-resumed"}
AUTH_ACTIONS = {"sign-in-claude", "reconnect-remote-control", "verify-resumed-work"}
RESOLVED_CAUSE, RESOLVED_RECOVERY, RESOLVED_ACTION = "incident-resolved", "resolved", "no-action-needed"   # the closing notice's fixed phrases: only ever used together
RESOURCE_SESSION = "admission-monitor"
TEST_SESSION = "notify-test"   # own synthetic record for `test-notice`: shares no state with any other synthetic record
RESERVED_SESSIONS = (RESOURCE_SESSION, AUTH_SESSION, TEST_SESSION)   # hook events cannot touch them; sweep never stales or removes them
TEST_NOTICE_WINDOW = 600
AUTH_TITLE = {"auth-required": "sign-in required", "remote-control-disconnect": "Remote Control disconnected", "signin-recovered": "sign-in recovered"}
QUARANTINE_PROBLEMS = {"corrupt", "not-regular", "too-large", "foreign-owner"}
RESOURCE_TICKET = "SPE-176"
RESOURCE_REPO = "coding-agent-environment"
SIGNATURE = re.compile(r"[0-9a-f]{16}")
# Closed `detail` charset of a sign-in notice: letters, digits, space and . _ ( ) , + = - only (no quotes, brackets, slash, colon, newline, markdown or URL syntax).
AUTH_DETAIL = re.compile(r"(?=.*[A-Za-z0-9])[A-Za-z0-9 ._(),+=-]{1,160}")
BACKEND_NAMES = {"macos", "ntfy", "pushover", "telegram", "zeroclaw-spool"}
# ZeroClaw incident spool (SPE-176): a local, credential-free hand-off to the engineering-supervisor. The file schema below is the contract the
# supervisor consumer validates; docs/schemas/zeroclaw-incident-spool-v1.schema.json is its machine-readable twin. Keep the three in step.
SPOOL_BACKEND = "zeroclaw-spool"
SPOOL_KINDS = ("unclaimed-handoff", "queue-wait-excessive", "workload-over-deadline", "auth-interrupted", "recovered", "notify-test")
NOTICE_KINDS = SPOOL_KINDS + ("session",)   # `session`: every notice that has no spool kind (hook-driven session states, disk and memory stalls)
SPOOL_TRANSITIONS = ("opened", "escalated", "recovered", "test")
SPOOL_SEVERITIES = ("info", "action")
SPOOL_KEY = re.compile(r"[0-9a-f]{32}")
SPOOL_PROJECT = re.compile(r"[a-z0-9._-]{1,40}")
SPOOL_REF = re.compile(r"[a-z0-9._:-]{1,64}")
SPOOL_TEXT = re.compile(r"[A-Za-z0-9 .,;:()+-]{1,120}")   # no slash, backslash, quote, equals, at-sign or hash: no path, id, command line or URL can pass
SPOOL_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SPOOL_LEDGER = "zeroclaw-spool.ledger"   # not *.json: sweep and status treat every *.json in the state directory as a session record
SPOOL_UNAVAILABLE = "spool-unavailable"
SPOOL_ESCALATING_RECOVERY = {"lapsed", "wait-expiry-near", "recovery-exhausted"}
SPOOL_KIND_OF_CAUSE = {"unclaimed-handoff": "unclaimed-handoff", "queue-wait-excessive": "queue-wait-excessive",
                       "workload-over-deadline": "workload-over-deadline", "auth-required": "auth-interrupted",
                       "remote-control-disconnect": "auth-interrupted", "signin-recovered": "recovered", "incident-resolved": "recovered"}
DEFAULT_SPOOL_SUBDIR = ".local/share/engineering-supervisor/state/incident-spool"
DELIVERY_STATUSES = {"pending", "sending", "failed", "sent", "dropped", "skipped"}
TERMINAL = {"sent", "dropped", "skipped"}

MAX_STDIN = 256 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_CHECKPOINT_BYTES = 8 * 1024
HEARTBEAT_EVERY = 30
HISTORY_CAP = 60
NOTIFIED_CAP = 500
DEDUP_WINDOW = 86400   # best-effort dedup memory: older or beyond the cap, a repeat of the same transition may notify again
MAX_CWD = 4096
PENDING_CAP = 20
SPOOL_PENDING_CAP = 200   # hard bound on notices still waiting for an unavailable spool directory (one per incident transition); see make_room
SPOOL_TMP_MAX_AGE = 3600
HOOK_DEADLINE = 10
SENDING_STALE = 300
GC_AFTER = 14 * 86400
LOG_CAP = 256 * 1024

DEFAULT_CONFIG = {
    "profile": "personal",
    "corporate_markers": [],
    "known_repos": [],
    "stale_after_seconds": 2700,
    "notify": {"agent_completed": False, "idle": False, "session_ended": True},
    "retry": {"max_attempts": 5, "base_seconds": 30, "max_seconds": 900},
    "backends": {
        "macos": {"enabled": False},
        "ntfy": {"enabled": False, "approved_destination": "", "server": "https://ntfy.sh", "topic_env": "NTFY_TOPIC", "topic_file": "", "token_env": "", "token_file": ""},
        "pushover": {"enabled": False, "approved_destination": "", "token_env": "PUSHOVER_APP_TOKEN", "token_file": "", "user_env": "PUSHOVER_USER_KEY", "user_file": ""},
        "telegram": {"enabled": False, "approved_destination": "", "token_env": "TELEGRAM_BOT_TOKEN", "token_file": "", "chat_id_env": "TELEGRAM_CHAT_ID", "chat_id_file": "",
                     "credentials_json": "", "chat_target": "owner", "kinds": list(NOTICE_KINDS)},
        # Needs no credential and no Claude Code sign-in. `spool_dir` "" means <HOME>/.local/share/engineering-supervisor/state/incident-spool.
        "zeroclaw-spool": {"enabled": False, "spool_dir": "", "project": "coding-agent-environment", "kinds": list(SPOOL_KINDS)},
    },
}

SAFE_TEXT = re.compile(r"[A-Za-z0-9 ._:#@+-]+")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
SAFE_REPO = re.compile(r"[A-Za-z0-9._-]{1,64}")
TICKET = re.compile(r"[A-Z][A-Z0-9]{1,9}-[0-9]{1,7}")
HEX_HEAD = re.compile(r"[0-9a-f]{7,40}")
EVIDENCE = re.compile(r"[A-Za-z0-9._:#@/-]{4,80}")
SECRET_WORDS = re.compile(r"(?i)(ghp_|gho_|ghs_|github_pat|sk-|xox[abprs]-|akia|bearer|passw|secret|token|api[_-]?key|private|eyj[a-z0-9]{8,}|credential)")
SEGMENT_SPLIT = re.compile(r"[-_.: @#+/]")


# ---- boundary validation: the ONLY place a value that came from outside is type-tested, indexed or iterated ----------------------
# Everything external (hook payload, config file, checkpoint file, state file, pending deliveries) is checked against a declarative schema
# BEFORE any other code touches it. Schemas: type, enum, bounds, required keys, closed key sets, nesting. No coercion, no repair.
def v_str(mx=200, rx=None):
    return ("str", mx, rx)


def v_enum(allowed):
    return ("enum", frozenset(allowed))


def v_int(lo, hi):
    return ("int", lo, hi)


def v_bool():
    return ("bool",)


def v_num():
    return ("num",)


def v_opt(inner):
    return ("opt", inner)


def v_list(item, mx, mn=0):
    return ("list", item, mx, mn)


def v_map(key, item, mx):
    return ("map", key, item, mx)


def v_obj(fields, required=(), closed=True):
    return ("obj", fields, frozenset(required), closed)


def v_tag(key, variants):
    """An object whose `key` (a string) selects which object schema it must satisfy."""
    return ("tag", key, variants)


def conforms(schema, value):
    """True when value matches schema. Never raises; bool is never an int, NaN and infinity are never numbers. Nesting depth is the schema's."""
    try:
        return _conforms(schema, value)
    except Exception:  # noqa: BLE001 - a validator that could raise would be a boundary hole
        return False


def _conforms(schema, v):
    kind = schema[0]
    if kind == "str":
        return type(v) is str and len(v) <= schema[1] and (schema[2] is None or schema[2].fullmatch(v) is not None)
    if kind == "enum":
        return type(v) is str and v in schema[1]
    if kind == "int":
        return type(v) is int and schema[1] <= v <= schema[2]
    if kind == "bool":
        return type(v) is bool
    if kind == "num":
        return type(v) in (int, float) and math.isfinite(v)
    if kind == "opt":
        return v is None or _conforms(schema[1], v)
    if kind == "list":
        return type(v) is list and schema[3] <= len(v) <= schema[2] and all(_conforms(schema[1], x) for x in v)
    if kind == "map":
        return type(v) is dict and len(v) <= schema[3] and all(_conforms(schema[1], k) and _conforms(schema[2], x) for k, x in v.items())
    if kind == "obj":
        fields, required, closed = schema[1], schema[2], schema[3]
        return (type(v) is dict and required <= set(v) and (not closed or set(v) <= set(fields))
                and all(_conforms(fields[k], x) for k, x in v.items() if k in fields))
    if kind == "tag":
        return type(v) is dict and type(v.get(schema[1])) is str and v[schema[1]] in schema[2] and _conforms(schema[2][v[schema[1]]], v)
    return False


MISSING = object()


def overlay(default, schema, user, bad, path=""):
    """Config merge: `default` (a nested dict of leaves) overlaid with `user`, leaf by leaf. A leaf that does not conform, or a subtree of the
    wrong container type, takes the default and is recorded in `bad`; unknown keys are recorded too (never silently dropped)."""
    if type(schema) is dict:
        if type(user) is not dict:
            if user is not MISSING:
                bad.append(path or "root")
            user = {}
        bad.extend(f"{path}.{k}".lstrip(".") for k in user if k not in schema and not k.startswith("_"))  # "_note"-style comment keys are ignored
        return {k: overlay(default[k], schema[k], user.get(k, MISSING), bad, f"{path}.{k}".lstrip(".")) for k in default}
    if user is not MISSING and conforms(schema, user):
        return copy.deepcopy(user)
    if user is not MISSING:
        bad.append(path)
    return copy.deepcopy(default)


EVENT_SCHEMA = v_obj({"hook_event_name": v_str(64), "session_id": v_str(80, SAFE_ID), "cwd": v_opt(v_str(100_000)), "source": v_opt(v_str(100)),
                      "reason": v_opt(v_str(100)), "notification_type": v_opt(v_str(100)), "error_type": v_opt(v_str(100)), "stop_hook_active": v_opt(v_bool())},
                     required=("hook_event_name", "session_id"), closed=False)  # unknown payload keys are ignored, consumed ones must conform
_CP_FIELDS = {"schema": v_int(1, 1), "state": v_enum(CHECKPOINT_STATES), "updated": v_opt(v_str(40)), "task": v_opt(v_str(200)),
              "repo": v_opt(v_str(200)), "ticket": v_opt(v_str(200)), "workflow_state": v_opt(v_str(200)), "session_id": v_opt(v_str(80)),
              "pr": v_opt(v_obj({"number": v_int(1, 10**7 - 1), "head": v_opt(v_str(100))}, required=("number",))),
              "blocker": v_opt(v_obj({"owner": v_opt(v_str(64)), "resume_trigger": v_opt(v_str(64))})),
              "verified": v_opt(v_obj({"by": v_str(100), "evidence": v_str(200)}, required=("by", "evidence")))}
# One schema, both directions (the checkpoint CLI validates what it writes against this): a BLOCKED_* checkpoint must name a fixed resume trigger.
CHECKPOINT_SCHEMA = v_tag("state", {
    **{st: v_obj(_CP_FIELDS, required=("schema", "state")) for st in CHECKPOINT_STATES - BLOCKED},
    **{st: v_obj({**_CP_FIELDS, "blocker": v_obj({"owner": v_opt(v_str(64)), "resume_trigger": v_enum(RESUME_TRIGGERS)}, required=("resume_trigger",))},
                 required=("schema", "state", "blocker")) for st in BLOCKED}})
_OPT_STR = v_opt(v_str(200))
FIELDS_SCHEMA = v_obj({"state": v_str(200), "repo": _OPT_STR, "ticket": _OPT_STR, "pr": v_opt(v_int(0, 10**9)), "blocker_owner": _OPT_STR,
                       "resume_trigger": _OPT_STR, "kind": _OPT_STR, "cause": v_opt(v_enum(CAUSE_TEXT)),
                       "recovery": v_opt(v_enum(RECOVERY_TEXT)), "owner_action": v_opt(v_enum(OWNER_ACTION_TEXT)),
                       "detail": v_opt(v_str(160, AUTH_DETAIL)),
                       "notice": v_opt(v_enum(("test",))), "incident_ref": v_opt(v_str(64, SPOOL_REF))}, required=("state",))
EMIT_SCHEMA = v_obj({"cause": v_enum(CAUSE_TEXT), "recovery": v_enum(RECOVERY_TEXT), "owner_action": v_enum(OWNER_ACTION_TEXT),
                     "signature": v_opt(v_str(16, SIGNATURE)), "detail": v_opt(v_str(160, AUTH_DETAIL)),
                     "incident_ref": v_opt(v_str(64, SPOOL_REF))}, required=("cause", "recovery", "owner_action"))
BACKEND_SCHEMA = v_obj({"status": v_enum(DELIVERY_STATUSES), "attempts": v_int(0, 100), "next_at": v_num(), "code": v_opt(v_str(40))},
                       required=("status", "attempts", "next_at"))
PENDING_SCHEMA = v_obj({"key": v_str(64, re.compile(r".+")), "cycle": v_int(0, 10**9), "created": v_int(-10**12, 10**12), "fields": FIELDS_SCHEMA,
                        "backends": v_map(v_enum(BACKEND_NAMES), BACKEND_SCHEMA, 8)}, required=("key", "cycle", "created", "fields", "backends"))
TASK_SCHEMA = v_obj({"task": v_str(100), "repo": v_str(100), "ticket": v_str(100), "pr": v_int(0, 10**9), "head": v_str(100), "workflow_state": v_str(100),
                     "blocker_owner": v_str(100), "resume_trigger": v_str(100), "verified": v_bool()})
HISTORY_SCHEMA = v_obj({"t": v_int(-10**12, 10**12), "from": v_enum(STATES), "to": v_enum(STATES), "why": v_str(100)}, required=("t", "from", "to", "why"))
RECORD_SCHEMA = v_obj({"v": v_int(3, 3), "session_id": v_str(80, SAFE_ID), "state": v_enum(STATES), "state_since": v_num(), "last_seen": v_num(),
                       "run_started": v_num(), "cp_floor": v_num(), "cycle": v_int(0, 10**9), "agent_done": v_int(0, 10**9), "dropped": v_int(0, 10**9),
                       "task": TASK_SCHEMA, "history": v_list(HISTORY_SCHEMA, HISTORY_CAP),
                       "notified": v_map(v_str(64), v_list(v_int(-10**12, 10**12), 2, 2), NOTIFIED_CAP), "pending": v_list(PENDING_SCHEMA, SPOOL_PENDING_CAP),
                       "ended_reason": v_opt(v_str(30)), "cwd": v_str(MAX_CWD), "corporate": v_bool(), "repo_label": v_opt(v_str(64))},
                      required=("v", "session_id", "state", "state_since", "last_seen", "run_started", "cp_floor", "cycle", "agent_done", "dropped", "task",
                                "history", "notified", "pending", "ended_reason", "cwd", "corporate"))
# Config: same shape as DEFAULT_CONFIG, one leaf schema per default value.
_S200 = v_str(200)
CONFIG_SCHEMA = {
    "profile": v_enum(("personal", "corporate")), "corporate_markers": v_list(v_str(100, re.compile(r".+")), 100),
    "known_repos": v_list(v_str(64, SAFE_REPO), 200), "stale_after_seconds": v_int(60, 10**7),
    "notify": {"agent_completed": v_bool(), "idle": v_bool(), "session_ended": v_bool()},
    "retry": {"max_attempts": v_int(1, 100), "base_seconds": v_int(1, 86400), "max_seconds": v_int(1, 86400)},
    "backends": {"macos": {"enabled": v_bool()},
                 "ntfy": {"enabled": v_bool(), "approved_destination": _S200, "server": _S200, "topic_env": _S200, "topic_file": _S200, "token_env": _S200, "token_file": _S200},
                 "pushover": {"enabled": v_bool(), "approved_destination": _S200, "token_env": _S200, "token_file": _S200, "user_env": _S200, "user_file": _S200},
                 "telegram": {"enabled": v_bool(), "approved_destination": _S200, "token_env": _S200, "token_file": _S200, "chat_id_env": _S200, "chat_id_file": _S200,
                              "credentials_json": _S200, "chat_target": v_enum(("owner",)), "kinds": v_list(v_enum(NOTICE_KINDS), len(NOTICE_KINDS))},
                 "zeroclaw-spool": {"enabled": v_bool(), "spool_dir": v_str(1024, re.compile(r"(/[^\x00]*)?")), "project": v_str(40, SPOOL_PROJECT),
                                    "kinds": v_list(v_enum(SPOOL_KINDS), len(SPOOL_KINDS))}},
}


def secret_shaped(value):
    """Token-shaped content: a known credential prefix or word, a very long unbroken run, or a long mixed letter-and-digit segment.

    Segments are split on separators so ordinary long repository names ("coding-agent-environment") pass.
    """
    if SECRET_WORDS.search(value):
        return True
    for seg in SEGMENT_SPLIT.split(value):
        if len(seg) >= 32 or (len(seg) >= 20 and re.search(r"[0-9]", seg) and re.search(r"[A-Za-z]", seg)):
            return True
    return False


# ---- sanitising: every outbound field is rebuilt from an allowlist, never copied -------------------------------------------
def clean_text(value, limit, markers=()):
    """A short string of safe characters, or None. No paths, quotes, shell metacharacters, secrets or long identifiers."""
    if not isinstance(value, str) or not value.isprintable():
        return None  # control characters and newlines are rejected, not normalised
    v = " ".join(value.split())
    if not v or len(v) > limit or not SAFE_TEXT.fullmatch(v):
        return None
    if secret_shaped(v):
        return None
    low = v.lower()
    if any(m and m.lower() in low for m in markers):
        return None
    return v


def clean_ticket(value, markers=()):
    """A ticket label (ABC-123): never secret-shaped and never carrying a corporate marker."""
    if not isinstance(value, str) or not TICKET.fullmatch(value) or secret_shaped(value):
        return None
    low = value.lower()
    return None if any(m and m.lower() in low for m in markers) else value


def clean_repo(value, markers=()):
    """A repository label: plain name characters, and never secret-shaped, a long identifier or a corporate marker."""
    if not isinstance(value, str) or not SAFE_REPO.fullmatch(value) or secret_shaped(value):
        return None
    low = value.lower()
    return None if any(m and m.lower() in low for m in markers) else value


def sanitize_fields(fields, cfg):
    """Rebuild every outbound field from a strict validator or a fixed enum. None when `fields` is not a well-formed field set.

    Used when a notification is queued AND again at every delivery attempt, so nothing read back from disk is trusted.
    Repository and ticket labels pass the same secret and corporate-marker checks as every other label, with the markers of the
    config in force NOW; a repository must also be listed in `known_repos`. Free text has no field to travel in.
    """
    if not conforms(FIELDS_SCHEMA, fields) or fields["state"] not in STATES:
        return None
    markers, known = cfg["corporate_markers"], set(cfg["known_repos"])
    repo = clean_repo(fields.get("repo"), markers)
    out = {"state": fields["state"], "repo": repo if repo in known else "unlisted"}
    ticket = clean_ticket(fields.get("ticket"), markers)
    if ticket:
        out["ticket"] = ticket
    if fields.get("pr"):
        out["pr"] = fields["pr"]
    if fields.get("blocker_owner") in BLOCKER_OWNERS:
        out["blocker_owner"] = fields["blocker_owner"]
    if fields.get("resume_trigger") in RESUME_TRIGGERS:
        out["resume_trigger"] = fields["resume_trigger"]
    if fields.get("kind") in ALLOWED_KINDS:
        out["kind"] = fields["kind"]
    for key, table in (("cause", CAUSE_TEXT), ("recovery", RECOVERY_TEXT), ("owner_action", OWNER_ACTION_TEXT)):
        if fields.get(key) in table:
            out[key] = fields[key]
    if fields.get("cause") in AUTH_CAUSES and isinstance(fields.get("detail"), str) and AUTH_DETAIL.fullmatch(fields["detail"]):
        out["detail"] = fields["detail"]   # the only free-looking field, and only for the sign-in causes: closed charset, length-capped
    if fields.get("notice") == "test":
        out["notice"] = "test"
    if isinstance(fields.get("incident_ref"), str) and SPOOL_REF.fullmatch(fields["incident_ref"]):
        out["incident_ref"] = fields["incident_ref"]
    return out


def is_corporate_path(cwd, cfg):
    if cfg["profile"] == "corporate":
        return True
    low = (cwd or "").lower()
    return any(m.lower() in low for m in cfg["corporate_markers"])


# ---- files ---------------------------------------------------------------------------------------------------------------------
def state_dir():
    d = os.environ.get("SESSION_NOTIFY_STATE_DIR") or os.path.expanduser("~/.cache/coding-agent-environment/session-status")
    return pathlib.Path(d)


def config_path():
    return pathlib.Path(os.environ.get("SESSION_NOTIFY_CONFIG") or os.path.expanduser("~/.config/coding-agent-environment/session-notify.json"))


def read_regular(path, limit):
    raw, problem, _ = read_regular_ex(path, limit)
    return raw, problem


def read_regular_ex(path, limit):
    """(bytes, problem, mtime) of a regular, non-symlink file no larger than limit, all taken from ONE opened descriptor."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None, 0.0
    except OSError as exc:
        return None, f"unreadable:{exc.errno}", 0.0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not-regular", 0.0
        if st.st_uid != os.getuid():
            return None, "foreign-owner", 0.0
        raw = os.read(fd, limit + 1)
    except OSError as exc:
        return None, f"unreadable:{exc.errno}", 0.0
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None, "too-large", 0.0
    return raw, None, st.st_mtime


def atomic_write(path, text):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def session_lock(path, wait=3.0, sleep=time.sleep):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = str(p.parent / (p.name + ".lock"))
    waited = 0.0
    while True:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            if waited >= wait:
                raise TimeoutError("session state busy")
            sleep(0.05)
            waited += 0.05
            continue
        try:  # stable identity: the lock only counts if its path still names the inode we hold (garbage collection may have replaced it)
            same = os.stat(lock_path).st_ino == os.fstat(fd).st_ino
        except OSError:
            same = False
        if same:
            break
        os.close(fd)
        if waited >= wait:
            raise TimeoutError("session state busy")
        waited += 0.05
    try:
        yield
    finally:
        os.close(fd)


def log_line(sdir, text):
    """Local diagnostics only: event names and error classes, never payload content. Bounded; failures are swallowed."""
    try:
        path = pathlib.Path(sdir) / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_size > LOG_CAP:
                os.replace(path, str(path) + ".1")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):  # a FIFO or device here must never be written to or waited on
                return
            os.write(fd, f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text[:200]}\n".encode("ascii", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


def load_config(path=None):
    """The owner's config over the defaults, leaf by leaf through CONFIG_SCHEMA. Returns (config, problem). A missing or invalid file means
    everything off; an invalid leaf takes its default; an invalid `profile` or `corporate_markers` makes the profile corporate (stricter)."""
    raw, problem = read_regular(path or config_path(), 64 * 1024)
    if raw is None:
        return copy.deepcopy(DEFAULT_CONFIG), problem
    try:
        user = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return copy.deepcopy(DEFAULT_CONFIG), "invalid-json"
    bad = []
    cfg = overlay(DEFAULT_CONFIG, CONFIG_SCHEMA, user if type(user) is dict else MISSING, bad)
    if type(user) is not dict:
        return cfg, "invalid-json"
    if "profile" in bad or "corporate_markers" in bad:
        cfg["profile"] = "corporate"
    if "backends.zeroclaw-spool.spool_dir" in bad:
        cfg["backends"][SPOOL_BACKEND]["enabled"] = False   # an unusable (relative) spool directory is never replaced by the default one
    clash = routing_conflict(cfg)
    if clash:
        for name in ("telegram", SPOOL_BACKEND):   # fail closed for the clashing pair only: no notice may go to two destinations
            cfg["backends"][name]["enabled"] = False
        return cfg, f"{CONFLICT_PREFIX}telegram+{SPOOL_BACKEND} both enabled for kinds {','.join(clash)}; both are now OFF until fixed: set backends.telegram.kinds to a list without those kinds (for example [\"session\"]), or set backends.telegram.enabled or backends.{SPOOL_BACKEND}.enabled to false"
    return cfg, (f"invalid:{','.join(bad[:5])}"[:100] if bad else None)


CONFLICT_PREFIX = "conflict:"


def effective_kinds(name, backend):
    """The notice kinds a `kinds`-routed backend carries: its configured list as it is (an empty list carries nothing), else the default. The guard and the
    routing both read it here, so they can never disagree."""
    kinds = (backend or {}).get("kinds")
    return tuple(kinds) if isinstance(kinds, list) else (SPOOL_KINDS if name == SPOOL_BACKEND else NOTICE_KINDS)


def routing_conflict(cfg):
    """Notice kinds that BOTH `telegram` and `zeroclaw-spool` would carry while both are enabled (sorted, empty when the routing is disjoint).
    Each transition goes to one destination only, so the config is rejected rather than guessed at."""
    b = cfg.get("backends") or {}
    if (b.get("telegram") or {}).get("enabled") is not True or (b.get(SPOOL_BACKEND) or {}).get("enabled") is not True:
        return []
    return sorted(set(effective_kinds("telegram", b["telegram"])) & set(effective_kinds(SPOOL_BACKEND, b[SPOOL_BACKEND])))


def notice_kind(fields):
    """The notice kind of sanitised fields: a spool kind, or `session`."""
    return spool_kind(fields) or "session"


def backend_routed(name, cfg, fields):
    """False when `kinds` of this backend leaves the notice to another destination (telegram and zeroclaw-spool only)."""
    b = (cfg.get("backends") or {}).get(name) or {}
    if fields.get("cause") == RESOLVED_CAUSE and name != SPOOL_BACKEND:
        return False   # the closing notice only tells the supervisor an incident is over: a banner or message for it would be a new owner notification
    if name == SPOOL_BACKEND:
        return spool_kind(fields) in effective_kinds(name, b)
    if name == "telegram":
        return notice_kind(fields) in effective_kinds(name, b)
    return True


# ---- checkpoint: the only source of task identity, blocker and completion ----------------------------------------------------
def find_checkpoint(cwd):
    env = os.environ.get("SESSION_STATUS_FILE")
    if env:
        return pathlib.Path(env)
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return None
    p = pathlib.Path(cwd)
    for _ in range(8):
        cand = p / ".claude" / ".session-status.json"
        try:
            if cand.exists() or cand.is_symlink():
                return cand
        except OSError:  # e.g. ENAMETOOLONG: older Pythons answer False, 3.13 raises; either way there is no checkpoint here
            pass
        if p.parent == p:
            break
        p = p.parent
    return None


def read_checkpoint(cwd, sid, markers):
    """(sanitised checkpoint dict or None, mtime of the SAME opened file). Never raises; anything doubtful yields None.

    Only shapes and enums survive: `task` and `workflow_state` are short plain text kept for local identity and never sent anywhere.
    """
    path = find_checkpoint(cwd)
    if path is None:
        return None, 0.0
    raw, problem, mtime = read_regular_ex(path, MAX_CHECKPOINT_BYTES)
    if raw is None:
        return None, 0.0
    try:
        d = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None, 0.0
    if not conforms(CHECKPOINT_SCHEMA, d) or d.get("session_id") not in (None, sid):
        return None, 0.0  # any wrong type, unknown key or foreign session anywhere: the whole checkpoint is absent
    pr, blocker, verified = d.get("pr") or {}, d.get("blocker") or {}, d.get("verified") or {}
    head = pr.get("head")
    ver_by = clean_text(verified.get("by"), 40, markers)
    ver_ev = verified.get("evidence")
    ver_ev = ver_ev if ver_ev is not None and EVIDENCE.fullmatch(ver_ev) and not SECRET_WORDS.search(ver_ev) else None  # a reference; never transmitted
    owner, trigger = blocker.get("owner"), blocker.get("resume_trigger")
    return {
        "state": d["state"], "task": clean_text(d.get("task"), 64, markers), "repo": clean_repo(d.get("repo"), markers),
        "ticket": clean_ticket(d.get("ticket"), markers), "pr": pr.get("number"), "head": head if head and HEX_HEAD.fullmatch(head) else None,
        "workflow_state": clean_text(d.get("workflow_state"), 48, markers),
        "blocker_owner": owner if owner in BLOCKER_OWNERS else ("other" if owner else None),
        "resume_trigger": trigger if trigger in RESUME_TRIGGERS else None,
        "verified": bool(ver_by and ver_ev),
    }, mtime


def ticket_from_environment():
    t = os.environ.get("CLAUDE_CURRENT_TICKET", "")
    return t if TICKET.fullmatch(t) else None


# ---- records -------------------------------------------------------------------------------------------------------------------
def new_record(sid, now):
    return {"v": 3, "session_id": sid, "state": "IDLE", "state_since": now, "last_seen": now, "cycle": 0, "run_started": now, "cp_floor": 0, "agent_done": 0,
            "task": {}, "history": [], "notified": {}, "pending": [], "ended_reason": None, "dropped": 0, "cwd": "", "corporate": False}


def reject_constant(name):
    raise ValueError(name)  # NaN, Infinity and -Infinity are not JSON; they are never trusted state


def record_path(sdir, sid):
    return pathlib.Path(sdir) / f"{sid}.json"


def load_record(sdir, sid):
    """(record, problem). Missing -> (None, None). Anything untrustworthy -> (None, problem): the caller must not act on it."""
    raw, problem = read_regular(record_path(sdir, sid), MAX_STATE_BYTES)
    if raw is None:
        return None, problem
    try:
        d = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None, "corrupt"
    return (d, None) if conforms(RECORD_SCHEMA, d) and d["session_id"] == sid else (None, "corrupt")


MAX_EPOCH = 4_102_444_800   # 2100-01-01


def valid_epoch(x):
    """A usable wall-clock time: a real finite number within [0, 2100). Used for every externally supplied time."""
    return type(x) in (int, float) and math.isfinite(x) and 0 <= x <= MAX_EPOCH


def save_record(sdir, rec):
    if not conforms(RECORD_SCHEMA, rec):
        raise ValueError("refusing to persist a record that does not conform")  # a bad value must never become durable, untrusted state
    atomic_write(record_path(sdir, rec["session_id"]), json.dumps(rec, separators=(",", ":")))


# ---- delivery ------------------------------------------------------------------------------------------------------------------
def read_secret_file(path):
    """Value from a credential file, or None. The path must be absolute and name a regular, user-owned file that is neither group- nor
    world-accessible; it is opened O_NOFOLLOW and nothing about the path or the value is ever logged."""
    if not isinstance(path, str) or not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return None
        raw = os.read(fd, 513)
    except OSError:
        return None
    finally:
        os.close(fd)
    return raw.decode("utf-8", "replace").strip() if 0 < len(raw) <= 512 else None


def read_secret(env_name=None, file_path=None):
    """Value of a credential from a NAMED env var, else from a file, or None. The value is only ever passed to a transport."""
    if env_name:
        v = os.environ.get(env_name)
        if v and v.strip():
            return v.strip()
    return read_secret_file(file_path) if file_path else None


def host_of(url):
    m = re.match(r"^https://([A-Za-z0-9.-]+)(:[0-9]+)?(/|$)", str(url or ""))
    return m.group(1).lower() if m else ""


def send_macos(payload, cfg):
    title, body = payload["title"], payload["body"]
    script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run"]
    try:
        r = subprocess.run(["/usr/bin/osascript", *script, body, title], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "osascript-failed"
    return None if r.returncode == 0 else "osascript-rc"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # approval covers the first URL only; a redirect is an error, never followed (it would carry the Authorization header)


def http_post(url, data, headers, timeout=4):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())  # no environment proxies either
    try:
        with opener.open(req, timeout=timeout) as resp:  # https and approved host checked by the caller
            return None if 200 <= resp.status < 300 else f"http-{resp.status}"
    except urllib.error.HTTPError as exc:
        return f"http-{exc.code}"
    except (urllib.error.URLError, OSError, ValueError):
        return "network"


def send_ntfy(payload, cfg):
    server = str(cfg.get("server") or "")
    topic = read_secret(cfg.get("topic_env") or None, cfg.get("topic_file") or None)
    if not topic or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", topic) or host_of(server) != str(cfg.get("approved_destination", "")).lower():
        return "not-configured"
    headers = {"Title": payload["title"].encode("ascii", "replace").decode(), "Priority": "default"}
    token = read_secret(cfg.get("token_env") or None, cfg.get("token_file") or None)
    if token:
        headers["Authorization"] = "Bearer " + token
    return http_post(f"{server.rstrip('/')}/{topic}", payload["body"].encode("utf-8"), headers)


def send_pushover(payload, cfg):
    token, user = read_secret(cfg.get("token_env") or None, cfg.get("token_file") or None), read_secret(cfg.get("user_env") or None, cfg.get("user_file") or None)
    if not token or not user or str(cfg.get("approved_destination", "")).lower() != "api.pushover.net":
        return "not-configured"
    data = urllib.parse.urlencode({"token": token, "user": user, "title": payload["title"], "message": payload["body"]}).encode()
    return http_post("https://api.pushover.net/1/messages.json", data, {"Content-Type": "application/x-www-form-urlencoded"})


TELEGRAM_TOKEN = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{5,100}")
TELEGRAM_CHAT = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{5,64}")


MAX_CREDENTIALS_JSON = 65536
# Open object: the supervisor file carries other keys (the supergroup id) that are neither read nor validated here.
SUPERVISOR_CREDENTIALS_SCHEMA = v_obj({"TELEGRAM_BOT_TOKEN": v_str(200, re.compile(r".+")), "TELEGRAM_OWNER_ID": v_int(1, 10**18)},
                                      required=("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"), closed=False)


def _private_path(path):
    """True when no component of an absolute path is a symlink."""
    cur = pathlib.Path(path)
    while True:
        if cur.is_symlink():
            return False
        if cur == cur.parent:
            return True
        cur = cur.parent


def read_supervisor_credentials(path):
    """(token, owner chat id) from the engineering-supervisor credential file, or (None, None) on ANY problem (one fixed outcome, no detail).

    Same checks as the supervisor loader: absolute path without symlinks, owner-only 0700 parent, fd-based open with O_NOFOLLOW, a regular
    file we own with mode exactly 0600 and at most 64 KiB, a JSON object with a string TELEGRAM_BOT_TOKEN and a positive integer
    TELEGRAM_OWNER_ID. Only those two keys are read; the supergroup id is deliberately ignored (direct message to the owner only)."""
    try:
        if not isinstance(path, str) or not path or not os.path.isabs(path) or not _private_path(path):
            return None, None
        parent = os.lstat(os.path.dirname(path))
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            return None, None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600
                    or st.st_size > MAX_CREDENTIALS_JSON):
                return None, None
            raw = os.read(fd, MAX_CREDENTIALS_JSON + 1)
        finally:
            os.close(fd)
        if not 0 < len(raw) <= MAX_CREDENTIALS_JSON:
            return None, None
        doc = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        if not conforms(SUPERVISOR_CREDENTIALS_SCHEMA, doc):
            return None, None
        return doc["TELEGRAM_BOT_TOKEN"], str(doc["TELEGRAM_OWNER_ID"])
    except (OSError, ValueError, RecursionError):
        return None, None


def send_telegram(payload, cfg):
    """Bot API sendMessage over plain https. The token is part of the URL path, so it must never leave this function: http_post returns only
    fixed codes and any exception is reduced to its type name."""
    token = read_secret(cfg.get("token_env") or None, cfg.get("token_file") or None)
    chat = read_secret(cfg.get("chat_id_env") or None, cfg.get("chat_id_file") or None)
    if not token and not chat and cfg.get("credentials_json"):  # lowest precedence, and only when neither explicit source produced anything
        if cfg.get("chat_target", "owner") != "owner":
            return "not-configured"
        token, chat = read_supervisor_credentials(cfg["credentials_json"])
    if (not token or not chat or not TELEGRAM_TOKEN.fullmatch(token) or not TELEGRAM_CHAT.fullmatch(chat)
            or str(cfg.get("approved_destination", "")).lower() != "api.telegram.org"):
        return "not-configured"
    data = urllib.parse.urlencode({"chat_id": chat, "text": payload["title"] + "\n" + payload["body"]}).encode()
    try:
        return http_post(f"https://api.telegram.org/bot{token}/sendMessage", data, {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001 - never let exception text (which may embed the URL) escape
        return f"error-{type(exc).__name__}"[:40]


# ---- ZeroClaw incident spool (SPE-176) -----------------------------------------------------------------------------------------------
SPOOL_SCHEMA = v_obj({"schema": v_int(1, 1), "key": v_str(32, SPOOL_KEY), "project": v_str(40, SPOOL_PROJECT), "incident_ref": v_str(64, SPOOL_REF),
                      "kind": v_enum(SPOOL_KINDS), "transition": v_enum(SPOOL_TRANSITIONS), "severity": v_enum(SPOOL_SEVERITIES),
                      "progress": v_str(120, SPOOL_TEXT), "action": v_str(120, SPOOL_TEXT), "ts": v_str(20, SPOOL_TS)},
                     required=("schema", "key", "project", "incident_ref", "kind", "transition", "severity", "progress", "action", "ts"))
LEDGER_SCHEMA = v_obj({"v": v_int(1, 1), "keys": v_map(v_str(32, SPOOL_KEY), v_num(), NOTIFIED_CAP)}, required=("v", "keys"))


def spool_kind(fields):
    """The spool kind of sanitised notice fields, or None when this notice has no incident kind (it is then never spooled)."""
    if not conforms(FIELDS_SCHEMA, fields):
        return None
    if fields.get("notice") == "test":
        return "notify-test"
    return SPOOL_KIND_OF_CAUSE.get(fields.get("cause"))


def spool_event(fields, project, now):
    """The closed-schema spool document for sanitised notice fields, or None. Every word comes from the fixed phrase tables: nothing free-form."""
    if not conforms(FIELDS_SCHEMA, fields):
        return None
    kind = spool_kind(fields)
    if kind is None or not isinstance(fields.get("incident_ref"), str):
        return None
    ref = fields["incident_ref"]
    if kind == "notify-test":
        transition, severity, progress, action = "test", "info", "delivery test notice", "no action needed"
    else:
        cause, recovery, owner_action = fields.get("cause"), fields.get("recovery"), fields.get("owner_action")
        if cause not in CAUSE_TEXT or recovery not in RECOVERY_TEXT or owner_action not in OWNER_ACTION_TEXT:
            return None
        transition = "recovered" if kind == "recovered" else "escalated" if recovery in SPOOL_ESCALATING_RECOVERY else "opened"
        severity = "info" if transition == "recovered" else "action"
        progress, action = f"{CAUSE_TEXT[cause]}; recovery: {RECOVERY_TEXT[recovery]}", OWNER_ACTION_TEXT[owner_action]
    event = {"schema": 1, "key": hashlib.sha256(f"{ref}|{transition}".encode()).hexdigest()[:32], "project": project, "incident_ref": ref, "kind": kind,
             "transition": transition, "severity": severity, "progress": progress, "action": action, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    return event if conforms(SPOOL_SCHEMA, event) else None


def open_spool_dir(path):
    """Directory fd of an EXISTING spool directory, or None. Absolute normalised path with no symlink component, a directory we own, mode exactly 0700.
    Never created here: the supervisor owns it, and a wrong one must be refused rather than made."""
    try:
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or not _private_path(path):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        return None
    return fd


def sweep_stale_tmp(dfd, now):
    """Unlink our own regular `.tmp-*` files older than SPOOL_TMP_MAX_AGE (left by a crashed writer). Nothing else in the directory is touched."""
    with contextlib.suppress(OSError):
        for name in os.listdir(dfd):
            if not name.startswith(".tmp-"):
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime > SPOOL_TMP_MAX_AGE:
                os.unlink(name, dir_fd=dfd)


def _ledger_keys(sdir, now):
    raw, _ = read_regular(pathlib.Path(sdir) / SPOOL_LEDGER, MAX_STATE_BYTES)
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=reject_constant) if raw else None
    except (ValueError, RecursionError):
        doc = None
    keys = doc["keys"] if doc is not None and conforms(LEDGER_SCHEMA, doc) else {}
    return {k: t for k, t in keys.items() if t >= now - DEDUP_WINDOW}


def send_zeroclaw_spool(payload, cfg):
    """Write ONE spool file per incident transition. None = the transition is in the spool (written now, or already there, or written within the
    dedup window); SPOOL_UNAVAILABLE = the directory is missing, wrong or unwritable (the caller keeps the notice and retries without a cap);
    any other code is final. Needs no credential and no Claude Code sign-in."""
    ctx = payload.get("context") or {}
    fields, sdir, now = ctx.get("fields"), ctx.get("state_dir"), time.time()
    event = spool_event(fields, cfg.get("project"), now) if isinstance(fields, dict) else None
    if event is None or not sdir:
        return "invalid-fields"
    path = cfg.get("spool_dir") or os.path.join(os.path.expanduser("~"), DEFAULT_SPOOL_SUBDIR)
    if not os.path.isabs(path):
        return "not-configured"
    key, name = event["key"], event["key"] + ".json"
    try:
        with session_lock(pathlib.Path(sdir) / SPOOL_LEDGER):   # one writer at a time: the dedup check and the write below are one step
            keys = _ledger_keys(sdir, now)
            if key in keys:
                return None
            dfd = open_spool_dir(path)
            if dfd is None:
                return SPOOL_UNAVAILABLE
            sweep_stale_tmp(dfd, now)
            tmp = f".tmp-{os.urandom(8).hex()}"
            try:
                try:
                    os.stat(name, dir_fd=dfd, follow_symlinks=False)   # present (consumer has not taken it yet): never a second file
                except FileNotFoundError:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
                    try:
                        os.fchmod(fd, 0o600)
                        os.write(fd, (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode())
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dfd)
                return SPOOL_UNAVAILABLE
            finally:
                os.close(dfd)
            keys[key] = now
            with contextlib.suppress(OSError):   # the file is the authority; a lost ledger line only widens the dedup window to the file's lifetime
                keep = dict(sorted(keys.items(), key=lambda kv: kv[1])[-NOTIFIED_CAP:])
                atomic_write(pathlib.Path(sdir) / SPOOL_LEDGER, json.dumps({"v": 1, "keys": keep}, separators=(",", ":")))
            return None
    except TimeoutError:
        return SPOOL_UNAVAILABLE


REAL_TRANSPORTS = {"macos": send_macos, "ntfy": send_ntfy, "pushover": send_pushover, "telegram": send_telegram, SPOOL_BACKEND: send_zeroclaw_spool}


def backend_allowed(name, cfg, corporate_session):
    """(allowed, reason). Remote backends need enabled AND an approved destination and are refused for corporate sessions."""
    b = (cfg.get("backends") or {}).get(name) or {}
    if b.get("enabled") is not True:
        return False, "disabled"
    if name in REMOTE_BACKENDS:
        if cfg.get("profile") == "corporate" or corporate_session:
            return False, "refused-corporate"
        if not str(b.get("approved_destination") or "").strip():
            return False, "no-approved-destination"
    return True, None


def render(fields):
    """Title and body from sanitised fields and fixed phrases only. Identical for every backend: there is no free-text field."""
    if fields.get("notice") == "test":
        return {"title": "Claude Code: test notice, ignore", "body": "notification delivery test | no action needed"}
    if fields.get("cause") in CAUSE_TEXT and fields.get("recovery") in RECOVERY_TEXT and fields.get("owner_action") in OWNER_ACTION_TEXT:
        body = f"{CAUSE_TEXT[fields['cause']]} | recovery: {RECOVERY_TEXT[fields['recovery']]} | action: {OWNER_ACTION_TEXT[fields['owner_action']]}"
        if fields["cause"] in AUTH_CAUSES:
            if fields.get("detail"):
                body += f" | {fields['detail']}"
            return {"title": "Claude Code: " + AUTH_TITLE[fields["cause"]], "body": body[:300]}
        if fields["cause"] == RESOLVED_CAUSE:
            return {"title": "Claude Code: admission incident resolved", "body": body}
        return {"title": "Claude Code: admission stalled", "body": body}
    bits = [fields.get("repo", "unlisted")]
    if fields.get("ticket"):
        bits.append(fields["ticket"])
    if fields.get("pr"):
        bits.append(f"PR #{fields['pr']}")
    body = " ".join(bits)
    if fields.get("kind"):
        body += f" | {fields['kind']}"
    if fields.get("blocker_owner"):
        body += f" | owner: {fields['blocker_owner']}"
    if fields.get("resume_trigger"):
        body += f" | resume: {RESUME_TRIGGERS[fields['resume_trigger']]}"
    return {"title": f"Claude Code: {fields['state']}", "body": body[:200]}


# ---- engine --------------------------------------------------------------------------------------------------------------------
class Engine:
    def __init__(self, sdir, cfg, transports=None, clock=time.time, sleep=time.sleep):
        self.sdir, self.cfg, self.transports, self.clock, self.sleep = pathlib.Path(sdir), cfg, transports or REAL_TRANSPORTS, clock, sleep
        self.last_key, self.last_delivery = None, None   # of the latest emit_resource request: its dedup key and what became of its delivery

    # -- state machine
    def set_state(self, rec, new, reason, now, cp=None, kind=None, notify=True, force=False, bump=True, extra=None):
        """Move the session. `cycle` counts entries into RUNNING for notification dedup. Checkpoint freshness is separate: see `apply`."""
        old = rec["state"]
        if new == "RUNNING" and old != "RUNNING" and bump:
            rec["cycle"] += 1
        if new != old or force:
            rec["history"] = (rec["history"] + [{"t": int(now), "from": old, "to": new, "why": reason}])[-HISTORY_CAP:]
            rec["state"], rec["state_since"] = new, now
        rec["last_seen"] = now
        if cp:
            rec["task"] = {k: cp[k] for k in ("task", "repo", "ticket", "pr", "head", "workflow_state", "blocker_owner", "resume_trigger", "verified") if cp.get(k) is not None}
        if notify and (new in NOTIFIABLE or kind in ("STALE", "ENDED")):
            self.enqueue(rec, new, reason, now, kind, extra=extra)

    @staticmethod
    def seen(rec, base, cycle, now, any_cycle=False, window=DEDUP_WINDOW):
        """Best-effort dedup: a transition is a repeat only while its tombstone is inside DEDUP_WINDOW (and was not evicted by the cap). For the admission-monitor
        session (`any_cycle`) a still-pending entry of the same key is a repeat whatever the cycle: a clear bumps the cycle and must not let it be queued twice."""
        got = rec["notified"].get(base)
        return (got is not None and got[0] >= cycle and got[1] >= now - window) or any(p["key"] == base and (any_cycle or p["cycle"] == cycle) for p in rec["pending"])

    @staticmethod
    def mark_notified(rec, base, cycle, now):
        """Remember a finished notification. A tombstone only ever moves to a newer cycle; entries past the window or the cap are forgotten."""
        got = rec["notified"].get(base)
        rec["notified"][base] = [max(int(cycle), got[0]) if got else int(cycle), int(now)]
        for k in [k for k, v in rec["notified"].items() if v[1] < now - DEDUP_WINDOW]:
            del rec["notified"][k]
        while len(rec["notified"]) > NOTIFIED_CAP:
            del rec["notified"][min(rec["notified"], key=lambda k: rec["notified"][k][1])]

    @staticmethod
    def spool_waiting(pend):
        b = pend.get("backends", {}).get(SPOOL_BACKEND)
        return bool(b) and b.get("status") in ("pending", "failed", "sending")

    def make_room(self, rec, now):
        """A full queue gives its oldest entry a durable terminal outcome instead of silently discarding it."""
        while len(rec["pending"]) >= PENDING_CAP:
            # An incident notice still waiting for the spool is kept past PENDING_CAP (they already dedup per incident transition); the oldest other entry goes first.
            victim = next((i for i, e in enumerate(rec["pending"]) if not self.spool_waiting(e)), None)
            if victim is None:
                if len(rec["pending"]) < SPOOL_PENDING_CAP:
                    return
                victim = 0
                log_line(self.sdir, "spool-queue-overflow: oldest undelivered incident notice dropped")   # owner-visible: hook.log, `dropped` in status, history
            code = "queue-overflow" if not self.spool_waiting(rec["pending"][victim]) else "spool-queue-overflow"
            old = rec["pending"].pop(victim)
            for b in old["backends"].values():
                if b["status"] not in TERMINAL:
                    b.update(status="dropped", code=code)
            self.mark_notified(rec, old["key"], old["cycle"], now)
            rec["dropped"] += 1
            rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": "spool-overflow-dropped" if code == "spool-queue-overflow" else "queue-overflow-dropped"}])[-HISTORY_CAP:]

    def enqueue(self, rec, state, reason, now, kind=None, extra_key="", extra=None, strict=False, window=DEDUP_WINDOW):
        t, label, cycle = rec["task"], kind or state, rec["cycle"]
        base = hashlib.sha256("|".join([rec["session_id"], str(t.get("task") or t.get("ticket") or "-"), label, str(t.get("head") or ""), extra_key]).encode()).hexdigest()[:24]
        self.last_key = base
        if self.seen(rec, base, cycle, now, any_cycle=rec["session_id"] in RESERVED_SESSIONS, window=window):
            return None
        raw = {"state": "FAILED" if kind == "STALE" else ("SESSION_ENDED" if kind == "ENDED" else ("RUNNING" if kind == "AGENT" else state)),
               "repo": t.get("repo") or rec.get("repo_label"), "ticket": t.get("ticket"), "pr": t.get("pr")}
        if state in BLOCKED:
            raw.update(blocker_owner=t.get("blocker_owner") or ("owner" if state == "BLOCKED_OWNER" else None), resume_trigger=t.get("resume_trigger"))
        if kind in KIND_TEXT:
            raw["kind"] = KIND_TEXT[kind]
        elif reason.startswith("stop_failure:"):
            raw["kind"] = reason.split(":", 1)[1]
        raw.update(extra or {})
        fields = sanitize_fields(raw, self.cfg)
        if fields is None:
            return None
        backends = {}
        for name in self.cfg["backends"]:
            enabled = (self.cfg["backends"][name] or {}).get("enabled") is True
            routed = enabled and backend_routed(name, self.cfg, fields)
            backends[name] = {"status": "pending" if routed else "skipped", "attempts": 0, "next_at": 0, "code": None if routed else ("disabled" if not enabled else "kind-not-routed")}
        if not any(b["status"] == "pending" for b in backends.values()):
            if not strict:       # strict: nothing was handed to any backend, so a later retry (a backend enabled meanwhile) must not hit a dedup tombstone
                self.mark_notified(rec, base, cycle, now)
            return None
        self.make_room(rec, now)
        rec["pending"].append({"key": base, "cycle": cycle, "created": int(now), "fields": fields, "backends": backends})
        return base

    def deliver(self, rec, now, budget_end=None):
        """Attempt every due delivery once. Fields and corporate status are re-evaluated NOW, never taken from the queued entry."""
        retry, changed = self.cfg["retry"], False
        corporate = bool(rec.get("corporate")) or is_corporate_path(rec.get("cwd"), self.cfg)
        unusable = [e for e in rec["pending"] if not conforms(PENDING_SCHEMA, e)]
        if unusable:  # dropped and counted up front, so one bad entry can neither be delivered nor stop (or block persisting) the others
            rec["pending"] = [e for e in rec["pending"] if e not in unusable]
            rec["dropped"] += len(unusable)
            rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": "entry-error-dropped"}])[-HISTORY_CAP:]
            changed = True
        for entry in list(rec["pending"]):
            try:
                result = self.deliver_entry(rec, entry, now, budget_end, corporate)
            except Exception:  # one malformed entry must not stall the others or raise after an earlier one was sent
                rec["pending"] = [e for e in rec["pending"] if e is not entry]  # unusable: gone, counted, never retried
                rec["dropped"] += 1
                rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": "entry-error-dropped"}])[-HISTORY_CAP:]
                result = True
            if result is None:
                return True
            changed = changed or result
        done = [e for e in rec["pending"] if all(b["status"] in TERMINAL for b in e["backends"].values())]
        for e in done:
            self.mark_notified(rec, e["key"], e["cycle"], now)
        for e in done:
            if any(b["status"] == "dropped" for b in e["backends"].values()):
                rec["dropped"] += 1  # retries exhausted (or unusable): the notification is dropped and counted, never silently lost
        if done:
            rec["pending"] = [e for e in rec["pending"] if e not in done]
            changed = True
        return changed

    def deliver_entry(self, rec, entry, now, budget_end, corporate):
        """One pending entry. Returns whether state changed, or None when the time budget ran out (caller stops)."""
        retry, changed = self.cfg["retry"], False
        fields = sanitize_fields(entry["fields"], self.cfg)
        if fields is None:
            for b in entry["backends"].values():
                if b["status"] not in TERMINAL:
                    b.update(status="dropped", code="invalid-fields")
                    changed = True
        else:
            if fields != entry["fields"]:
                entry["fields"], changed = fields, True
            for name, b in entry["backends"].items():
                if b["status"] == "sending" and now - b["next_at"] > SENDING_STALE:
                    b["status"], changed = "failed", True
                    if name == SPOOL_BACKEND:   # an interrupted spool write is idempotent (an existing key is success): the interrupted attempt does not count
                        b["attempts"] = max(0, b["attempts"] - 1)
                if b["status"] not in ("pending", "failed") or b["next_at"] > now:
                    continue
                ok, why = backend_allowed(name, self.cfg, corporate)
                if ok and not backend_routed(name, self.cfg, fields):
                    ok, why = False, "kind-not-routed"
                if not ok:
                    b.update(status="skipped", code=why)
                    changed = True
                    continue
                if b["attempts"] >= int(retry["max_attempts"]):  # interrupted-send recoveries count: the cap is enforced BEFORE sending
                    b.update(status="dropped", code="max-attempts")
                    changed = True
                    continue
                if budget_end is not None and self.clock() > budget_end:
                    return None
                payload = render(fields)
                if name == SPOOL_BACKEND:   # the local spool writer alone gets the sanitised fields and the ledger location; every other transport sees title and body only
                    payload = dict(payload, context={"fields": fields, "state_dir": str(self.sdir)})
                b.update(status="sending", attempts=b["attempts"] + 1, next_at=now)
                save_record(self.sdir, rec)  # persisted BEFORE sending: a crash here retries once instead of looping or losing
                transport = self.transports.get(name)
                try:
                    code = transport(payload, self.cfg["backends"][name]) if transport else "no-transport"
                except Exception as exc:
                    code = f"error-{type(exc).__name__}"[:40]
                changed = True
                if code is None:
                    b.update(status="sent", code=None)
                elif code == SPOOL_UNAVAILABLE:   # a missing or unwritable spool directory is a local condition that ends: keep the notice, retry, never drop it
                    b.update(status="failed", code=code, attempts=b["attempts"] - 1, next_at=now + int(retry["base_seconds"]))
                elif b["attempts"] >= int(retry["max_attempts"]):
                    b.update(status="dropped", code=str(code)[:40])
                else:
                    delay = min(int(retry["max_seconds"]), int(retry["base_seconds"]) * (2 ** (b["attempts"] - 1)))
                    b.update(status="failed", code=str(code)[:40], next_at=now + delay)
        return changed

    # -- events
    def handle(self, raw):
        if not conforms(EVENT_SCHEMA, raw):
            return "ignored"  # not an object, or a consumed field has the wrong type/size: nothing is read from it, nothing changes
        ev = raw
        name, sid = ev["hook_event_name"], ev["session_id"]
        if sid.lower() in RESERVED_SESSIONS:  # case-folded: APFS would map Admission-Monitor onto the same file
            return "ignored"  # reserved for emit-resource: a hook event can neither write nor spoof this record
        path = record_path(self.sdir, sid)
        try:
            with session_lock(path, sleep=self.sleep):
                rec, problem = load_record(self.sdir, sid)
                if problem:
                    log_line(self.sdir, f"state-untrusted {problem} event={name[:30]}")
                    return f"untrusted:{problem}"
                now = self.clock()
                created = rec is None
                rec = rec or new_record(sid, now)
                before = json.dumps(rec, sort_keys=True)
                cwd = ev.get("cwd")
                if cwd and len(cwd) <= MAX_CWD:
                    rec["cwd"] = cwd  # whole path, never truncated: corporate markers are re-evaluated against it at every delivery
                rec["corporate"] = bool(is_corporate_path(cwd, self.cfg)) or bool(rec["corporate"]) or bool(cwd and len(cwd) > MAX_CWD)  # unstorable path: fail closed
                markers = self.cfg["corporate_markers"]
                cp, cp_mtime = read_checkpoint(cwd, sid, markers)
                if cp is None and not rec["task"] and ticket_from_environment():
                    rec["task"].update({"ticket": ticket_from_environment()})
                if not rec["task"].get("repo") and cwd:
                    rec["repo_label"] = clean_repo(os.path.basename(cwd.rstrip("/")), markers) or "unlisted"
                self.apply(rec, name, ev, cp, cp_mtime, now, created)
                changed = json.dumps(rec, sort_keys=True) != before
                if self.deliver(rec, now, budget_end=now + 6):
                    changed = True
                if changed or created:
                    save_record(self.sdir, rec)
                return "ok"
        except TimeoutError:
            log_line(self.sdir, f"lock-timeout event={name[:30]}")
            return "busy"

    def apply(self, rec, name, event, cp, cp_mtime, now, created):
        """`event` is an EVENT_SCHEMA-conforming payload (validated in `handle`); `cp` is a sanitised checkpoint or None."""
        state = rec["state"]
        if name == "SessionStart":
            source = event.get("source") or "unknown"
            # a resume restores the same run: no new cycle, so an identical block already announced is not announced again
            rec["run_started"] = now
            self.set_state(rec, "RUNNING", f"start:{source[:12]}", now, cp, notify=False, bump=source != "resume")
            return
        if name == "SessionEnd":
            reason = event.get("reason") or "other"
            reason = reason if reason in ("clear", "resume", "logout", "prompt_input_exit", "other") else "other"
            rec["ended_reason"] = reason
            was = state
            self.set_state(rec, "SESSION_ENDED", f"end:{reason}", now, cp, notify=False, force=True)
            if was == "RUNNING" and reason in END_NOTIFY_REASONS and self.cfg["notify"]["session_ended"]:
                self.enqueue(rec, "SESSION_ENDED", f"end:{reason}", now, kind="ENDED")
            return
        if state == "SESSION_ENDED" and name != "UserPromptSubmit":
            return
        if name == "UserPromptSubmit":
            rec["run_started"] = now  # a prompt always opens a new run, even while RUNNING: earlier checkpoints stop counting
            if state != "RUNNING":
                self.set_state(rec, "RUNNING", "prompt", now, cp, notify=False)
            else:
                self.heartbeat(rec, now)
            return
        if name in ("PostToolUse", "PostToolUseFailure", "SubagentStop", "TaskCompleted", "SubagentStart", "PreToolUse"):
            if name in ("PostToolUse", "PostToolUseFailure") and state in BLOCKED | {"IDLE", "COMPLETED", "FAILED"}:
                # progress only changes the state: it never touches checkpoint freshness, so the checkpoint written by this very tool call counts
                self.set_state(rec, "RUNNING", "progress", now, cp, notify=False)
            else:
                self.heartbeat(rec, now)
            if name in ("TaskCompleted", "SubagentStop"):
                rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": name.lower()}])[-HISTORY_CAP:]
            return
        if name == "Notification":
            ntype = event.get("notification_type") or ""
            block = {"blocker_owner": "owner", "resume_trigger": "owner_decision" if ntype != "permission_prompt" else "owner_approval"}
            if (ntype in OWNER_BLOCK_TYPES or ntype == "idle_prompt") and state in ("RUNNING", "IDLE"):  # a finished or already-blocked session is never overwritten
                self.set_state(rec, "BLOCKED_OWNER", ntype[:30], now, cp, notify=True, extra=block)
            elif ntype == "agent_completed":
                rec["agent_done"] += 1
                self.heartbeat(rec, now)
                if self.cfg["notify"]["agent_completed"]:
                    self.enqueue(rec, "RUNNING", "agent_completed", now, kind="AGENT", extra_key=str(rec["agent_done"]))
            else:
                self.heartbeat(rec, now)
            return
        if name == "StopFailure":
            kind = event.get("error_type") or "unknown"
            kind = kind if kind in STOP_FAILURE_KINDS else "unknown"
            self.set_state(rec, "FAILED", f"stop_failure:{kind}", now, cp, notify=True)
            return
        if name == "Stop":
            if event.get("stop_hook_active") is True:
                self.heartbeat(rec, now)
                return
            target, reason = "IDLE", "stop"
            if cp and cp_mtime > max(rec["run_started"], rec["cp_floor"]):  # newer than this run's start and than any checkpoint already acted on
                rec["cp_floor"] = cp_mtime
                if cp["state"] == "COMPLETED":
                    target, reason = ("COMPLETED", "checkpoint-verified") if cp["verified"] else ("IDLE", "completion-unverified")
                elif cp["state"] in BLOCKED or cp["state"] == "FAILED":
                    target, reason = cp["state"], "checkpoint"
            self.set_state(rec, target, reason, now, cp, notify=target in NOTIFIABLE)
            return
        self.heartbeat(rec, now)

    def heartbeat(self, rec, now):
        if now - rec["last_seen"] >= HEARTBEAT_EVERY or now < rec["last_seen"]:
            rec["last_seen"] = now

    # -- admission-monitor notices
    def emit_resource(self, req, clear=False, strict=False, scope=None):
        """One deduplicated BLOCKED_RESOURCE notice on the synthetic `admission-monitor` session, or (clear) its return to IDLE.

        `req` is an EMIT_SCHEMA-shaped dict. Returns queued, deduped, no-backend, cleared, invalid, busy, error or untrusted:<problem>. Dedup key is cause + signature.
        """
        if not clear and not conforms(EMIT_SCHEMA, req):
            return "invalid"
        if not clear and (((req["cause"] in AUTH_CAUSES) != (req["recovery"] in AUTH_RECOVERY)) or ((req["cause"] in AUTH_CAUSES) != (req["owner_action"] in AUTH_ACTIONS))
                          or (req.get("detail") and req["cause"] not in AUTH_CAUSES)):
            return "invalid"   # sign-in phrases only go with sign-in causes, and `detail` exists for those causes alone
        if not clear and ((req["cause"] == RESOLVED_CAUSE) != (req["recovery"] == RESOLVED_RECOVERY) or (req["cause"] == RESOLVED_CAUSE) != (req["owner_action"] == RESOLVED_ACTION)
                          or (req["cause"] == RESOLVED_CAUSE and not req.get("incident_ref"))):
            return "invalid"   # the closing notice's phrases go together and always name the incident it closes
        self.last_delivery, self.last_key = None, None
        sid = AUTH_SESSION if (req["cause"] in AUTH_CAUSES if not clear else scope == "auth") else RESOURCE_SESSION
        try:
            with session_lock(record_path(self.sdir, sid), sleep=self.sleep):
                rec, problem = load_record(self.sdir, sid)
                if problem and not (problem in QUARANTINE_PROBLEMS):
                    return f"untrusted:{problem}"  # transient (e.g. unreadable:<errno>): never destroy state over it
                if problem:  # the synthetic record only: quarantine it (one kept) and rebuild, so alerting cannot fail forever
                    log_line(self.sdir, f"state-quarantined {problem} event=emit-resource")
                    try:
                        os.replace(record_path(self.sdir, sid), str(record_path(self.sdir, sid)) + ".corrupt")
                    except OSError:
                        return f"untrusted:{problem}"
                    rec = None
                now = self.clock()
                if rec is None and clear:
                    return "cleared"
                rec = rec or new_record(sid, now)
                if clear:
                    self.set_state(rec, "IDLE", "resource-cleared", now, notify=False)
                    rec["cycle"] += 1  # an in-flight pre-clear notice may still finish and write a tombstone; it is of the old cycle and cannot dedup a recurrence
                    rec["notified"] = {}  # dedup only suppresses a continuous stall: a recurrence after a clear notifies again
                    rec["pending"] = [e for e in rec["pending"] if not all(b["status"] in TERMINAL for b in e["backends"].values())]
                    result = "cleared"
                else:
                    resume = ("credentials_provided" if req["cause"] == "auth-required" else "manual_restart" if sid == AUTH_SESSION else
                              "owner_decision" if "approve" in req["owner_action"] else "resource_available")
                    cp = {"task": sid, "repo": RESOURCE_REPO, "ticket": RESOURCE_TICKET, "blocker_owner": "resource", "resume_trigger": resume}
                    state = "IDLE" if req["cause"] == "signin-recovered" else "BLOCKED_RESOURCE"
                    if req["cause"] == RESOLVED_CAUSE:
                        state = rec["state"]   # closing one incident says nothing about any other blockage on this record: its state is left as it is
                    else:
                        self.set_state(rec, state, "resource-stall", now, cp, notify=False)
                    extra = {"cause": req["cause"], "recovery": req["recovery"], "owner_action": req["owner_action"], "detail": req.get("detail"),
                             "incident_ref": req.get("incident_ref") or "inc:" + (req.get("signature") or hashlib.sha256(req["cause"].encode()).hexdigest()[:16])}
                    queued = self.enqueue(rec, state, "resource-stall", now, extra_key=req["cause"] + ":" + (req.get("signature") or ""), extra=extra,
                                          strict=strict)
                    tracked = next((p for p in rec["pending"] if p["key"] == self.last_key), None)
                    any_backend = any((b or {}).get("enabled") is True for b in self.cfg["backends"].values())
                    result = "no-backend" if not any_backend else ("queued" if queued else "deduped")
                self.deliver(rec, now, budget_end=now + 6)
                if not clear and result != "no-backend":
                    # What this request's own pending entry looks like after the attempt; an entry that finished in an earlier call is unknown.
                    states = {b["status"] for b in tracked["backends"].values()} if tracked else set()
                    self.last_delivery = ("unknown" if tracked is None else "sent" if "sent" in states else
                                          "pending" if states - TERMINAL else "dropped")
                save_record(self.sdir, rec)
                return result
        except TimeoutError:
            log_line(self.sdir, "lock-timeout event=emit-resource")
            return "busy"
        except OSError as exc:
            log_line(self.sdir, f"emit-resource os-error errno={exc.errno}")  # errno only: no path reaches stderr
            return "error"

    # -- delivery-test notice
    def test_notice(self, strict=False):
        """One fixed-text delivery-test notice on its own synthetic `notify-test` record, deduplicated for TEST_NOTICE_WINDOW seconds.

        Takes no content. Returns queued, deduped, no-backend, busy, error or untrusted:<problem>; `last_delivery` is set as for emit_resource.
        """
        self.last_delivery, self.last_key = None, None
        sid = TEST_SESSION
        try:
            with session_lock(record_path(self.sdir, sid), sleep=self.sleep):
                rec, problem = load_record(self.sdir, sid)
                if problem and problem not in QUARANTINE_PROBLEMS:
                    return f"untrusted:{problem}"
                if problem:
                    log_line(self.sdir, f"state-quarantined {problem} event=test-notice")
                    try:
                        os.replace(record_path(self.sdir, sid), str(record_path(self.sdir, sid)) + ".corrupt")
                    except OSError:
                        return f"untrusted:{problem}"
                    rec = None
                now = self.clock()
                rec = rec or new_record(sid, now)
                # The window also bounds a still-pending entry (a failing backend must not read as `deduped` forever): an older one is dropped, counted, and replaced.
                old = [e for e in rec["pending"] if e["created"] < now - TEST_NOTICE_WINDOW]
                if old:
                    rec["pending"] = [e for e in rec["pending"] if e not in old]
                    rec["dropped"] += len(old)
                queued = self.enqueue(rec, "IDLE", "test-notice", now, extra_key="test-notice", extra={"notice": "test", "incident_ref": f"test:{int(now // TEST_NOTICE_WINDOW)}"},
                                  strict=strict, window=TEST_NOTICE_WINDOW)
                tracked = next((p for p in rec["pending"] if p["key"] == self.last_key), None)
                any_backend = any((b or {}).get("enabled") is True for b in self.cfg["backends"].values())
                result = "no-backend" if not any_backend else ("queued" if queued else "deduped")
                self.deliver(rec, now, budget_end=now + 6)
                if result != "no-backend":
                    states = {b["status"] for b in tracked["backends"].values()} if tracked else set()
                    self.last_delivery = ("unknown" if tracked is None else "sent" if "sent" in states else
                                          "pending" if states - TERMINAL else "dropped")
                if not (strict and result == "no-backend" and rec["pending"] == [] and not rec["notified"]):
                    save_record(self.sdir, rec)
                return result
        except TimeoutError:
            log_line(self.sdir, "lock-timeout event=test-notice")
            return "busy"
        except OSError as exc:
            log_line(self.sdir, f"test-notice os-error errno={exc.errno}")
            return "error"

    # -- sweep
    def sweep(self, now=None):
        now = self.clock() if now is None else now
        if not valid_epoch(now):
            raise ValueError("sweep time must be a finite epoch in [0, 2100)")
        out = {"sessions": 0, "stale": 0, "retried": 0, "removed": 0, "untrusted": 0, "busy": 0}
        if not self.sdir.is_dir():
            return out
        for p in sorted(self.sdir.glob("*.json")):
            sid = p.stem
            if not SAFE_ID.fullmatch(sid):
                continue
            try:
                with session_lock(p, wait=0.5, sleep=self.sleep):
                    rec, problem = load_record(self.sdir, sid)
                    if problem or rec is None:
                        out["untrusted"] += 1 if problem else 0
                        continue
                    out["sessions"] += 1
                    changed = False
                    if rec["state"] == "RUNNING" and sid.lower() not in RESERVED_SESSIONS and now - rec["last_seen"] > self.cfg["stale_after_seconds"]:
                        self.set_state(rec, "FAILED", "stale-no-heartbeat", now, None, kind="STALE", notify=True)
                        out["stale"] += 1
                        changed = True
                    pending_before = sum(1 for e in rec["pending"] for b in e["backends"].values() if b["status"] in ("pending", "failed"))
                    if self.deliver(rec, now):
                        changed = True
                        out["retried"] += pending_before
                    if sid.lower() not in RESERVED_SESSIONS and rec["state"] in ("SESSION_ENDED", "COMPLETED", "FAILED") and not rec["pending"] and now - rec["last_seen"] > GC_AFTER:
                        # removed while HOLDING the lock; session_lock re-checks the inode, so a waiter that opened the old file retries on the new one
                        for q in (p, p.with_name(p.name + ".lock")):
                            with contextlib.suppress(OSError):
                                os.unlink(q)
                        out["removed"] += 1
                        continue
                    if changed:
                        save_record(self.sdir, rec)
            except TimeoutError:
                out["busy"] += 1
        return out


# ---- CLI -----------------------------------------------------------------------------------------------------------------------
CONFLICT_MARKER = "config-conflict.marker"   # not *.json: never read as a session record


def mark_conflict(sdir, problem):
    """Hook path, no stderr: leave (or remove) a marker that `status` shows, because a rejected config silently turns telegram and the spool off."""
    path = pathlib.Path(sdir) / CONFLICT_MARKER
    with contextlib.suppress(OSError):
        if isinstance(problem, str) and problem.startswith(CONFLICT_PREFIX):
            atomic_write(path, problem[len(CONFLICT_PREFIX):][:400] + "\n")
        elif path.is_file() and not path.is_symlink():
            path.unlink()


def cmd_hook(args):
    sdir = state_dir()

    def on_alarm(signum, frame):
        # Hard stop at the FIRST deadline: nothing the engine could catch or swallow gets a chance to keep the hook alive. State writes are
        # atomic and a delivery is persisted as `sending` before it runs, so leaving here never corrupts or loses state.
        log_line(sdir, "hook deadline")
        os._exit(0)

    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, HOOK_DEADLINE)
        raw = sys.stdin.buffer.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            log_line(sdir, "stdin too large")
            return 0
        ev = json.loads(raw.decode("utf-8", "replace"))
        cfg, problem = load_config()
        if problem and problem not in (None,) and str(problem) != "unreadable:2":
            log_line(sdir, f"config {problem}")
        mark_conflict(sdir, problem)
        result = Engine(sdir, cfg).handle(ev)
        if result not in ("ok", "ignored"):
            log_line(sdir, f"result {result}")
    except BaseException as exc:  # noqa: BLE001 - a hook must never break or block the session
        log_line(sdir, f"hook error {type(exc).__name__}")
    finally:
        with contextlib.suppress(Exception):
            signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


def age(now, t):
    s = int(max(0, now - t))
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def cmd_status(args):
    sdir, rows, now = state_dir(), [], time.time()
    config_problem = load_config()[1]
    if isinstance(config_problem, str) and config_problem.startswith(CONFLICT_PREFIX):
        rows.append({"session": "(config)", "state": "CONFIG_CONFLICT", "problem": config_problem[len(CONFLICT_PREFIX):]})   # telegram AND zeroclaw-spool are off
    elif config_problem and str(config_problem) != "unreadable:2":
        rows.append({"session": "(config)", "state": "CONFIG_PROBLEM", "problem": config_problem})  # surfaced here, not only in hook.log
    marker, _ = read_regular(sdir / CONFLICT_MARKER, 4096)
    if marker and not (isinstance(config_problem, str) and config_problem.startswith(CONFLICT_PREFIX)):
        rows.append({"session": "(config)", "state": "CONFIG_CONFLICT_SEEN_BY_HOOK", "problem": marker.decode("utf-8", "replace").strip()})
    for p in sorted(sdir.glob("*.json")) if sdir.is_dir() else []:
        sid = p.stem
        rec, problem = load_record(sdir, sid) if SAFE_ID.fullmatch(sid) else (None, "bad-name")
        if rec is None:
            rows.append({"session": sid[:12], "state": "UNTRUSTED", "problem": problem})
            continue
        t = rec["task"]
        pend = sum(1 for e in rec["pending"] for b in e["backends"].values() if b["status"] in ("pending", "failed", "sending"))
        rows.append({"session": sid[:12], "state": rec["state"], "age": age(now, rec["state_since"]), "seen": age(now, rec["last_seen"]), "cycle": rec["cycle"],
                     "repo": t.get("repo") or rec.get("repo_label"), "ticket": t.get("ticket"), "pr": t.get("pr"), "pending_deliveries": pend,
                     "dropped": rec["dropped"] or None})
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print("  ".join(f"{k}={v}" for k, v in r.items() if v is not None))
        if not rows:
            print("no tracked sessions")
    return 0


def cmd_sweep(args):
    cfg, problem = load_config()
    config_rejected(problem)   # said once on stderr; the sweep still retries what the remaining backends hold
    out = Engine(state_dir(), cfg).sweep(now=args.now)
    print(json.dumps(out))
    return 0


def cmd_checkpoint(args):
    markers = load_config()[0].get("corporate_markers", [])
    if args.state not in CHECKPOINT_STATES:
        raise SystemExit(f"state must be one of {sorted(CHECKPOINT_STATES)}")
    body = {"schema": 1, "state": args.state, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for key, val, limit in (("task", args.task, 64), ("workflow_state", args.workflow_state, 48)):
        if val is not None:
            if clean_text(val, limit, markers) is None:
                raise SystemExit(f"--{key.replace('_', '-')} must be short plain text (letters, digits, space and ._:#@+-)")
            body[key] = val
    if args.repo is not None:
        if clean_repo(args.repo, markers) is None:
            raise SystemExit("--repo must be a plain repository name ([A-Za-z0-9._-], at most 64, not secret-shaped)")
        body["repo"] = args.repo
    if args.ticket is not None:
        if not TICKET.fullmatch(args.ticket):
            raise SystemExit("--ticket must look like ABC-123")
        body["ticket"] = args.ticket
    if args.head is not None and not HEX_HEAD.fullmatch(args.head):
        raise SystemExit("--head must be 7-40 hex characters")
    if args.pr is not None:
        pr = {"number": args.pr}
        if args.head:
            pr["head"] = args.head
        body["pr"] = pr
    elif args.head is not None:
        raise SystemExit("--head needs --pr")
    if args.blocker_owner or args.resume_trigger:
        body["blocker"] = {}
        if args.blocker_owner is not None:
            if args.blocker_owner not in BLOCKER_OWNERS:
                raise SystemExit(f"--blocker-owner must be one of {sorted(BLOCKER_OWNERS)}")
            body["blocker"]["owner"] = args.blocker_owner
        if args.resume_trigger is not None:
            if args.resume_trigger not in RESUME_TRIGGERS:
                raise SystemExit(f"--resume-trigger must be one of {sorted(RESUME_TRIGGERS)}")
            body["blocker"]["resume_trigger"] = args.resume_trigger
    if args.state in BLOCKED and not args.resume_trigger:
        raise SystemExit("a BLOCKED_* checkpoint needs --resume-trigger (one of the fixed conditions that unblocks it)")
    if args.state == "COMPLETED":
        if not (args.verified_by and args.evidence):
            raise SystemExit("COMPLETED needs --verified-by and --evidence (a durable reference such as PR#101@<sha> or ci:<run>)")
        if clean_text(args.verified_by, 40, markers) is None or not EVIDENCE.fullmatch(args.evidence) or SECRET_WORDS.search(args.evidence):
            raise SystemExit("--verified-by / --evidence are not plain references")
        body["verified"] = {"by": args.verified_by, "evidence": args.evidence}
    if not conforms(CHECKPOINT_SCHEMA, body):
        raise SystemExit("the checkpoint would not be accepted by the reader (check --pr is 1..9999999 and every field's size and type)")
    target = pathlib.Path(args.file) if args.file else (pathlib.Path(os.environ["SESSION_STATUS_FILE"]) if os.environ.get("SESSION_STATUS_FILE") else pathlib.Path.cwd() / ".claude" / ".session-status.json")
    atomic_write(target, json.dumps(body, indent=1) + "\n")
    print(f"checkpoint written: state={args.state}")
    return 0


def config_rejected(problem):
    """True (after saying why on stderr) when the config was rejected by the routing guard: nothing is emitted then."""
    if isinstance(problem, str) and problem.startswith(CONFLICT_PREFIX):
        print(f"session-notify: config rejected: {problem[len(CONFLICT_PREFIX):]}", file=sys.stderr)
        return True
    return False


def cmd_emit_resource(args):
    cfg, problem = load_config()
    if config_rejected(problem):
        return 2
    req = {"cause": args.cause, "recovery": args.recovery, "owner_action": args.owner_action, "signature": args.signature, "detail": args.detail,
           "incident_ref": args.incident_ref}
    engine = Engine(state_dir(), cfg)
    result = engine.emit_resource(req, clear=args.clear, strict=args.strict, scope=args.scope)
    print(f"emit-resource: {result}" + (f" delivery={engine.last_delivery}" if engine.last_delivery else ""))
    return 0 if result in ("queued", "deduped", "no-backend", "cleared") else 1


def cmd_test_notice(args):
    cfg, problem = load_config()
    if config_rejected(problem):
        return 2
    engine = Engine(state_dir(), cfg)
    result = engine.test_notice(strict=args.strict)
    print(f"test-notice: {result}" + (f" delivery={engine.last_delivery}" if engine.last_delivery else ""))
    return 0 if result in ("queued", "deduped", "no-backend") else 1


def signature_arg(text):
    if not SIGNATURE.fullmatch(text):
        raise argparse.ArgumentTypeError("--signature must be 16 lowercase hex characters")
    return text


def incident_ref_arg(text):
    if not SPOOL_REF.fullmatch(text):
        raise argparse.ArgumentTypeError("--incident-ref must be 1..64 characters of a-z 0-9 . _ : -")
    return text


def detail_arg(text):
    if not AUTH_DETAIL.fullmatch(text):
        raise argparse.ArgumentTypeError("--detail must be 1..160 characters of letters, digits, space and . _ ( ) , + = -")
    return text


def cmd_render_config(args):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["profile"] = args.profile
    cfg["_note"] = ("Everything is off until a backend sets enabled to true. Credentials are environment-variable NAMES or file paths, never values. "
                    "Remote backends also need approved_destination equal to the server host, and are refused on the corporate profile. "
                    "Notifications show a repository name only if it is listed in known_repos; otherwise they say 'unlisted'. "
                    "If you enable zeroclaw-spool, set backends.telegram.kinds to [\"session\"] (or disable telegram): both enabled for one notice kind is rejected and turns both off.")
    text = json.dumps(cfg, indent=2) + "\n"
    if args.out:
        with open(args.out, "x") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def epoch_arg(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--now must be a number of seconds since the epoch") from None
    if not valid_epoch(value):
        raise argparse.ArgumentTypeError("--now must be finite and between 0 and the year 2100")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("sweep")
    w.add_argument("--now", type=epoch_arg)
    c = sub.add_parser("checkpoint")
    c.add_argument("--state", required=True)
    for opt in ("task", "repo", "ticket", "head", "workflow-state", "verified-by", "evidence", "blocker-owner", "resume-trigger", "file"):
        c.add_argument(f"--{opt}")
    c.add_argument("--pr", type=int)
    e = sub.add_parser("emit-resource")
    e.add_argument("--clear", action="store_true")
    for opt, table in (("cause", CAUSE_TEXT), ("recovery", RECOVERY_TEXT), ("owner-action", OWNER_ACTION_TEXT)):
        e.add_argument(f"--{opt}", choices=sorted(table))
    e.add_argument("--signature", type=signature_arg)
    e.add_argument("--incident-ref", type=incident_ref_arg, help="stable id of the incident across its transitions (spool backend); default inc:<signature>")
    e.add_argument("--detail", type=detail_arg, help="sign-in causes only: closed charset, at most 160 characters")
    e.add_argument("--scope", choices=("admission", "auth"), default="admission", help="with --clear: which synthetic session returns to IDLE")
    e.add_argument("--strict", action="store_true", help="a no-backend outcome leaves no dedup record, so a retry after a backend is enabled still notifies")
    t = sub.add_parser("test-notice")
    t.add_argument("--strict", action="store_true", help="a no-backend outcome leaves no dedup record")
    r = sub.add_parser("render-config")
    r.add_argument("--profile", choices=("personal", "corporate"), default="personal")
    r.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "emit-resource" and not a.clear and not (a.cause and a.recovery and a.owner_action):
        ap.error("emit-resource needs --cause, --recovery and --owner-action (or --clear)")
    return {"hook": cmd_hook, "status": cmd_status, "sweep": cmd_sweep, "checkpoint": cmd_checkpoint, "emit-resource": cmd_emit_resource, "test-notice": cmd_test_notice, "render-config": cmd_render_config}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
