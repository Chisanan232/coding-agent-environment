#!/usr/bin/env python3
"""Durable session status and meaningful-transition notifications from Claude Code hooks (SPE-120).

  session-notify.py hook                         read one hook event on stdin; ALWAYS exits 0, never blocks Claude
  session-notify.py status [--json]              table of tracked sessions (read-only)
  session-notify.py sweep [--now EPOCH]          stale-session detection, delivery retry, old-record cleanup
  session-notify.py checkpoint --state S ...     write the structured checkpoint a session uses to declare its state
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
REMOTE_BACKENDS = ("ntfy", "pushover")
BLOCKER_OWNERS = {"owner", "dependency", "resource", "ci", "reviewer", "other"}
# Everything a notification can say about a resume condition is one of these fixed phrases; there is no free-text field.
RESUME_TRIGGERS = {"owner_approval": "owner approval", "owner_decision": "owner decision", "dependency_merged": "dependency merged",
                   "ci_green": "CI green", "review_complete": "review complete", "resource_available": "resource available",
                   "credentials_provided": "credentials provided", "manual_restart": "manual restart", "other": "other"}
KIND_TEXT = {"STALE": "no heartbeat", "ENDED": "ended while running", "AGENT": "background agent finished"}
ALLOWED_KINDS = set(KIND_TEXT.values()) | STOP_FAILURE_KINDS
BACKEND_NAMES = {"macos", "ntfy", "pushover"}
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
        bad.extend(f"{path}.{k}".lstrip(".") for k in user if k not in schema)
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
                       "resume_trigger": _OPT_STR, "kind": _OPT_STR}, required=("state",))
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
                       "notified": v_map(v_str(64), v_list(v_int(-10**12, 10**12), 2, 2), NOTIFIED_CAP), "pending": v_list(PENDING_SCHEMA, PENDING_CAP),
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
                 "pushover": {"enabled": v_bool(), "approved_destination": _S200, "token_env": _S200, "token_file": _S200, "user_env": _S200, "user_file": _S200}},
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
    return cfg, (f"invalid:{','.join(bad[:5])}"[:100] if bad else None)


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


REAL_TRANSPORTS = {"macos": send_macos, "ntfy": send_ntfy, "pushover": send_pushover}


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
    def seen(rec, base, cycle, now):
        """Best-effort dedup: a transition is a repeat only while its tombstone is inside DEDUP_WINDOW (and was not evicted by the cap)."""
        got = rec["notified"].get(base)
        return (got is not None and got[0] >= cycle and got[1] >= now - DEDUP_WINDOW) or any(p["key"] == base and p["cycle"] == cycle for p in rec["pending"])

    @staticmethod
    def mark_notified(rec, base, cycle, now):
        """Remember a finished notification. A tombstone only ever moves to a newer cycle; entries past the window or the cap are forgotten."""
        got = rec["notified"].get(base)
        rec["notified"][base] = [max(int(cycle), got[0]) if got else int(cycle), int(now)]
        for k in [k for k, v in rec["notified"].items() if v[1] < now - DEDUP_WINDOW]:
            del rec["notified"][k]
        while len(rec["notified"]) > NOTIFIED_CAP:
            del rec["notified"][min(rec["notified"], key=lambda k: rec["notified"][k][1])]

    def make_room(self, rec, now):
        """A full queue gives its oldest entry a durable terminal outcome instead of silently discarding it."""
        while len(rec["pending"]) >= PENDING_CAP:
            old = rec["pending"].pop(0)
            for b in old["backends"].values():
                if b["status"] not in TERMINAL:
                    b.update(status="dropped", code="queue-overflow")
            self.mark_notified(rec, old["key"], old["cycle"], now)
            rec["dropped"] += 1
            rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": "queue-overflow-dropped"}])[-HISTORY_CAP:]

    def enqueue(self, rec, state, reason, now, kind=None, extra_key="", extra=None):
        t, label, cycle = rec["task"], kind or state, rec["cycle"]
        base = hashlib.sha256("|".join([rec["session_id"], str(t.get("task") or t.get("ticket") or "-"), label, str(t.get("head") or ""), extra_key]).encode()).hexdigest()[:24]
        if self.seen(rec, base, cycle, now):
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
            backends[name] = {"status": "pending" if enabled else "skipped", "attempts": 0, "next_at": 0, "code": None if enabled else "disabled"}
        if not any(b["status"] == "pending" for b in backends.values()):
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
                if b["status"] not in ("pending", "failed") or b["next_at"] > now:
                    continue
                ok, why = backend_allowed(name, self.cfg, corporate)
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
            if ntype in OWNER_BLOCK_TYPES or (ntype == "idle_prompt" and state in ("RUNNING", "IDLE")):
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
                    if rec["state"] == "RUNNING" and now - rec["last_seen"] > self.cfg["stale_after_seconds"]:
                        self.set_state(rec, "FAILED", "stale-no-heartbeat", now, None, kind="STALE", notify=True)
                        out["stale"] += 1
                        changed = True
                    pending_before = sum(1 for e in rec["pending"] for b in e["backends"].values() if b["status"] in ("pending", "failed"))
                    if self.deliver(rec, now):
                        changed = True
                        out["retried"] += pending_before
                    if rec["state"] in ("SESSION_ENDED", "COMPLETED", "FAILED") and not rec["pending"] and now - rec["last_seen"] > GC_AFTER:
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
        if problem and problem not in (None,) and not str(problem).startswith("unreadable:2"):
            log_line(sdir, f"config {problem}")
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
    cfg, _ = load_config()
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


def cmd_render_config(args):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["profile"] = args.profile
    cfg["_note"] = ("Everything is off until a backend sets enabled to true. Credentials are environment-variable NAMES or file paths, never values. "
                    "Remote backends also need approved_destination equal to the server host, and are refused on the corporate profile. "
                    "Notifications show a repository name only if it is listed in known_repos; otherwise they say 'unlisted'.")
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
    r = sub.add_parser("render-config")
    r.add_argument("--profile", choices=("personal", "corporate"), default="personal")
    r.add_argument("--out")
    a = ap.parse_args(argv)
    return {"hook": cmd_hook, "status": cmd_status, "sweep": cmd_sweep, "checkpoint": cmd_checkpoint, "render-config": cmd_render_config}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
