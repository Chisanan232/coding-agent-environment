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
written during the current run. Forced termination (kill -9, power loss) cannot run hooks: `sweep` detects the silence afterwards.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
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
REMOTE_BLOCKER_OWNERS = {"owner", "dependency", "resource", "ci", "reviewer", "other"}

MAX_STDIN = 256 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_CHECKPOINT_BYTES = 8 * 1024
HEARTBEAT_EVERY = 30
HISTORY_CAP = 60
NOTIFIED_CAP = 200
PENDING_CAP = 20
HOOK_DEADLINE = 10
SENDING_STALE = 300
GC_AFTER = 14 * 86400
LOG_CAP = 256 * 1024

DEFAULT_CONFIG = {
    "profile": "personal",
    "corporate_markers": [],
    "stale_after_seconds": 2700,
    "notify": {"agent_completed": False, "idle": False, "session_ended": True},
    "retry": {"max_attempts": 5, "base_seconds": 30, "max_seconds": 900},
    "backends": {
        "macos": {"enabled": False},
        "ntfy": {"enabled": False, "approved_destination": "", "server": "https://ntfy.sh", "topic_env": "NTFY_TOPIC", "token_env": ""},
        "pushover": {"enabled": False, "approved_destination": "", "token_env": "PUSHOVER_APP_TOKEN", "user_env": "PUSHOVER_USER_KEY"},
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


def clean_repo(value, markers=()):
    """A repository label: plain name characters, and never secret-shaped, a long identifier or a corporate marker."""
    if not isinstance(value, str) or not SAFE_REPO.fullmatch(value) or secret_shaped(value):
        return None
    low = value.lower()
    return None if any(m and m.lower() in low for m in markers) else value


def clean_sid(value):
    if isinstance(value, str) and SAFE_ID.fullmatch(value):
        return value
    return None


def clean_pr(value):
    if isinstance(value, dict):
        n, head = value.get("number"), value.get("head")
    else:
        n, head = value, None
    n = n if type(n) is int and 0 < n < 10**7 else None
    head = head if isinstance(head, str) and HEX_HEAD.fullmatch(head) else None
    return n, head


def is_corporate_path(cwd, cfg):
    if cfg.get("profile") == "corporate":
        return True
    low = str(cwd or "").lower()
    return any(m and m.lower() in low for m in cfg.get("corporate_markers", []) if isinstance(m, str))


# ---- files ---------------------------------------------------------------------------------------------------------------------
def state_dir():
    d = os.environ.get("SESSION_NOTIFY_STATE_DIR") or os.path.expanduser("~/.cache/coding-agent-environment/session-status")
    return pathlib.Path(d)


def config_path():
    return pathlib.Path(os.environ.get("SESSION_NOTIFY_CONFIG") or os.path.expanduser("~/.config/coding-agent-environment/session-notify.json"))


def read_regular(path, limit):
    """Bytes of a regular, non-symlink file no larger than limit; (None, problem) otherwise. Missing file -> (None, None)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"unreadable:{exc.errno}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not-regular"
        if st.st_uid != os.getuid():
            return None, "foreign-owner"
        raw = os.read(fd, limit + 1)
    except OSError as exc:
        return None, f"unreadable:{exc.errno}"
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None, "too-large"
    return raw, None


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
    fd = os.open(str(p.parent / (p.name + ".lock")), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        waited = 0.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waited >= wait:
                    raise TimeoutError("session state busy")
                sleep(0.05)
                waited += 0.05
        yield
    finally:
        os.close(fd)


def log_line(sdir, text):
    """Local diagnostics only: event names and error classes, never payload content. Bounded; failures are swallowed."""
    try:
        path = pathlib.Path(sdir) / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_CAP:
                os.replace(path, str(path) + ".1")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text[:200]}\n")
    except Exception:
        pass


def load_config(path=None):
    """Merge the owner's config over the defaults. A missing or invalid file means 'everything off'."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw, problem = read_regular(path or config_path(), 64 * 1024)
    if raw is None:
        return cfg, problem
    try:
        user = json.loads(raw.decode("utf-8"))
        if not isinstance(user, dict):
            raise ValueError
    except ValueError:
        return cfg, "invalid-json"
    for key in ("profile", "corporate_markers", "stale_after_seconds"):
        if key in user:
            cfg[key] = user[key]
    for key in ("notify", "retry"):
        if isinstance(user.get(key), dict):
            cfg[key].update({k: v for k, v in user[key].items() if k in cfg[key]})
    for name, body in (user.get("backends") or {}).items():
        if name in cfg["backends"] and isinstance(body, dict):
            cfg["backends"][name].update({k: v for k, v in body.items() if k in cfg["backends"][name]})
    if cfg["profile"] not in ("personal", "corporate"):
        cfg["profile"] = "corporate"  # unknown profile fails towards the stricter one
    if not isinstance(cfg["corporate_markers"], list):
        cfg["corporate_markers"] = []
    if type(cfg["stale_after_seconds"]) is not int or cfg["stale_after_seconds"] < 60:
        cfg["stale_after_seconds"] = DEFAULT_CONFIG["stale_after_seconds"]
    return cfg, None


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
        if cand.exists() or cand.is_symlink():
            return cand
        if p.parent == p:
            break
        p = p.parent
    return None


def read_checkpoint(cwd, sid, markers):
    """(sanitised checkpoint dict or None, mtime). Never raises; anything doubtful yields None."""
    path = find_checkpoint(cwd)
    if path is None:
        return None, 0.0
    raw, problem = read_regular(path, MAX_CHECKPOINT_BYTES)
    if raw is None:
        return None, 0.0
    try:
        d = json.loads(raw.decode("utf-8"))
        mtime = path.stat().st_mtime
    except (ValueError, OSError):
        return None, 0.0
    if not isinstance(d, dict) or d.get("schema") != 1 or d.get("state") not in CHECKPOINT_STATES:
        return None, 0.0
    if d.get("session_id") not in (None, sid):
        return None, 0.0
    pr, head = clean_pr(d.get("pr"))
    blocker = d.get("blocker") if isinstance(d.get("blocker"), dict) else {}
    verified = d.get("verified") if isinstance(d.get("verified"), dict) else {}
    ver_by = clean_text(verified.get("by"), 40, markers)
    ver_ev = verified.get("evidence")
    ver_ev = ver_ev if isinstance(ver_ev, str) and EVIDENCE.fullmatch(ver_ev) and not SECRET_WORDS.search(ver_ev) else None  # a reference (may hold a full SHA); never transmitted
    repo = clean_repo(d.get("repo"), markers)
    ticket = d.get("ticket") if isinstance(d.get("ticket"), str) and TICKET.fullmatch(d["ticket"]) else None
    return {
        "state": d["state"], "task": clean_text(d.get("task"), 64, markers), "repo": repo, "ticket": ticket, "pr": pr, "head": head or None,
        "workflow_state": clean_text(d.get("workflow_state"), 48, markers),
        "blocker_owner": clean_text(blocker.get("owner"), 48, markers), "resume_trigger": clean_text(blocker.get("resume_trigger"), 96, markers),
        "verified": bool(ver_by and ver_ev),
    }, mtime


def ticket_from_environment():
    t = os.environ.get("CLAUDE_CURRENT_TICKET", "")
    return t if TICKET.fullmatch(t) else None


# ---- records -------------------------------------------------------------------------------------------------------------------
def new_record(sid, now):
    return {"v": 1, "session_id": sid, "state": "IDLE", "state_since": now, "last_seen": now, "cycle": 0, "cycle_started": now, "agent_done": 0,
            "task": {}, "history": [], "notified": [], "pending": [], "ended_reason": None}


def valid_record(d, sid):
    try:
        return (isinstance(d, dict) and d.get("v") == 1 and d.get("session_id") == sid and d.get("state") in STATES
                and type(d.get("cycle")) is int and d["cycle"] >= 0 and isinstance(d.get("history"), list) and len(d["history"]) <= HISTORY_CAP
                and isinstance(d.get("notified"), list) and len(d["notified"]) <= NOTIFIED_CAP and all(isinstance(k, str) for k in d["notified"])
                and isinstance(d.get("pending"), list) and len(d["pending"]) <= PENDING_CAP and isinstance(d.get("task"), dict)
                and isinstance(d.get("last_seen"), (int, float)) and isinstance(d.get("state_since"), (int, float))
                and isinstance(d.get("cycle_started"), (int, float)) and type(d.get("agent_done")) is int)
    except Exception:
        return False


def record_path(sdir, sid):
    return pathlib.Path(sdir) / f"{sid}.json"


def load_record(sdir, sid):
    """(record, problem). Missing -> (None, None). Anything untrustworthy -> (None, problem): the caller must not act on it."""
    raw, problem = read_regular(record_path(sdir, sid), MAX_STATE_BYTES)
    if raw is None:
        return None, problem
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "corrupt"
    return (d, None) if valid_record(d, sid) else (None, "corrupt")


def save_record(sdir, rec):
    atomic_write(record_path(sdir, rec["session_id"]), json.dumps(rec, separators=(",", ":")))


# ---- delivery ------------------------------------------------------------------------------------------------------------------
def read_secret(env_name=None, file_path=None):
    """Value of a credential from a NAMED env var or a file, or None. The value is only ever passed to a transport."""
    if env_name:
        v = os.environ.get(env_name)
        return v.strip() if v else None
    if file_path:
        raw, _ = read_regular(os.path.expanduser(file_path), 512)
        return raw.decode("utf-8", "replace").strip() if raw else None
    return None


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


def http_post(url, data, headers, timeout=4):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https and approved host checked by the caller
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


def render(payload_fields, remote):
    """Title and body from allowlisted fields only. Remote backends get no free text from the checkpoint."""
    state = payload_fields["state"]
    bits = [payload_fields.get("repo") or "unknown-repo"]
    if payload_fields.get("ticket"):
        bits.append(payload_fields["ticket"])
    if payload_fields.get("pr"):
        bits.append(f"PR #{payload_fields['pr']}")
    body = " ".join(bits)
    kind = payload_fields.get("kind")
    if kind:
        body += f" | {kind}"
    owner = payload_fields.get("blocker_owner")
    if owner:
        body += f" | owner: {owner}"
    if not remote and payload_fields.get("resume_trigger"):
        body += f" | resume: {payload_fields['resume_trigger']}"
    return {"title": f"Claude Code: {state}", "body": body[:200]}


# ---- engine --------------------------------------------------------------------------------------------------------------------
class Engine:
    def __init__(self, sdir, cfg, transports=None, clock=time.time, sleep=time.sleep):
        self.sdir, self.cfg, self.transports, self.clock, self.sleep = pathlib.Path(sdir), cfg, transports or REAL_TRANSPORTS, clock, sleep

    # -- state machine
    def set_state(self, rec, new, reason, now, cp=None, kind=None, notify=True, force=False):
        old = rec["state"]
        if new == "RUNNING" and old != "RUNNING":
            rec["cycle"] += 1
            rec["cycle_started"] = now
        if new != old or force:
            rec["history"] = (rec["history"] + [{"t": int(now), "from": old, "to": new, "why": reason}])[-HISTORY_CAP:]
            rec["state"], rec["state_since"] = new, now
        rec["last_seen"] = now
        if cp:
            rec["task"] = {k: cp[k] for k in ("task", "repo", "ticket", "pr", "head", "workflow_state", "blocker_owner", "resume_trigger", "verified") if cp.get(k) is not None}
        if notify and (new in NOTIFIABLE or kind == "STALE" or kind == "ENDED"):
            self.enqueue(rec, new, reason, now, kind)

    def enqueue(self, rec, state, reason, now, kind=None, extra_key=""):
        t = rec["task"]
        label = kind or state
        key_src = "|".join([rec["session_id"], str(t.get("task") or t.get("ticket") or "-"), label, str(t.get("head") or ""), str(rec["cycle"]), extra_key])
        key = hashlib.sha256(key_src.encode()).hexdigest()[:24]
        if key in rec["notified"] or any(p["key"] == key for p in rec["pending"]):
            return None
        markers = self.cfg.get("corporate_markers", [])
        fields = {"state": "FAILED" if kind == "STALE" else state, "repo": t.get("repo") or rec.get("repo_label") or "unknown-repo", "ticket": t.get("ticket"),
                  "pr": (t.get("pr") or None), "blocker_owner": t.get("blocker_owner") if state in BLOCKED else None,
                  "resume_trigger": t.get("resume_trigger") if state in BLOCKED or kind else None, "kind": None}
        if kind == "STALE":
            fields["kind"] = "no heartbeat"
        elif kind == "ENDED":
            fields["state"], fields["kind"] = "SESSION_ENDED", "ended while running"
        elif kind == "AGENT":
            fields["state"], fields["kind"] = "RUNNING", "background agent finished"
        elif reason.startswith("stop_failure:"):
            fields["kind"] = reason.split(":", 1)[1]
        if state == "BLOCKED_OWNER" and not fields.get("blocker_owner"):
            fields["blocker_owner"] = "owner"
        entry = {"key": key, "created": int(now), "fields": {k: v for k, v in fields.items() if v is not None},
                 "corporate": bool(rec.get("corporate")), "backends": {}}
        for name in self.cfg["backends"]:
            ok, why = backend_allowed(name, self.cfg, entry["corporate"])
            entry["backends"][name] = {"status": "pending" if ok else "skipped", "attempts": 0, "next_at": 0, "code": why}
        if not any(b["status"] == "pending" for b in entry["backends"].values()):
            rec["notified"] = (rec["notified"] + [key])[-NOTIFIED_CAP:]
            return entry
        rec["pending"] = (rec["pending"] + [entry])[-PENDING_CAP:]
        return entry

    def deliver(self, rec, now, budget_end=None):
        """Attempt every due delivery once. Returns True when the record changed."""
        retry, changed = self.cfg["retry"], False
        for entry in rec["pending"]:
            for name, b in entry["backends"].items():
                if b["status"] == "sending" and now - b["next_at"] > SENDING_STALE:
                    b["status"], changed = "failed", True
                if b["status"] not in ("pending", "failed") or b["next_at"] > now:
                    continue
                if budget_end is not None and self.clock() > budget_end:
                    return changed
                ok, why = backend_allowed(name, self.cfg, entry.get("corporate"))
                if not ok:
                    b.update(status="skipped", code=why)
                    changed = True
                    continue
                payload = render(entry["fields"], remote=name in REMOTE_BACKENDS)
                if name in REMOTE_BACKENDS:
                    payload = self.remote_safe(entry["fields"], payload)
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
        done = [e for e in rec["pending"] if all(b["status"] in ("sent", "dropped", "skipped") for b in e["backends"].values())]
        if done:
            rec["notified"] = (rec["notified"] + [e["key"] for e in done if e["key"] not in rec["notified"]])[-NOTIFIED_CAP:]
            rec["pending"] = [e for e in rec["pending"] if e not in done]
            changed = True
        return changed

    @staticmethod
    def remote_safe(fields, payload):
        """Remote backends get the enum blocker owner only; the resume trigger and free text never leave the machine."""
        owner = fields.get("blocker_owner")
        f = dict(fields, blocker_owner=owner if owner in REMOTE_BLOCKER_OWNERS else ("unspecified" if owner else None), resume_trigger=None)
        return render(f, remote=True)

    # -- events
    def handle(self, ev):
        name = ev.get("hook_event_name") if isinstance(ev, dict) else None
        sid = clean_sid(ev.get("session_id")) if isinstance(ev, dict) else None
        if not isinstance(name, str) or not sid:
            return "ignored"
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
                cwd = ev.get("cwd") if isinstance(ev.get("cwd"), str) else None
                rec["corporate"] = bool(is_corporate_path(cwd, self.cfg)) or bool(rec.get("corporate"))
                markers = self.cfg.get("corporate_markers", [])
                cp, cp_mtime = read_checkpoint(cwd, sid, markers)
                if cp is None and not rec["task"] and ticket_from_environment():
                    cp_env = {"ticket": ticket_from_environment()}
                    rec["task"].update(cp_env)
                if not rec["task"].get("repo") and cwd:
                    label = os.path.basename(cwd.rstrip("/"))
                    rec["repo_label"] = clean_repo(label, markers) or "unknown-repo"
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

    def apply(self, rec, name, ev, cp, cp_mtime, now, created):
        state = rec["state"]
        if name == "SessionStart":
            if state == "SESSION_ENDED" or created or ev.get("source") in ("resume", "startup", "clear", "fork", "compact"):
                self.set_state(rec, "RUNNING", f"start:{str(ev.get('source') or 'unknown')[:12]}", now, cp, notify=False)
            return
        if name == "SessionEnd":
            reason = str(ev.get("reason") or "other")
            reason = reason if reason in ("clear", "resume", "logout", "prompt_input_exit", "other") else "other"
            rec["ended_reason"] = reason
            was = state
            self.set_state(rec, "SESSION_ENDED", f"end:{reason}", now, cp, notify=False, force=True)
            if was == "RUNNING" and reason in END_NOTIFY_REASONS and self.cfg["notify"].get("session_ended", True):
                self.enqueue(rec, "SESSION_ENDED", f"end:{reason}", now, kind="ENDED")
            return
        if state == "SESSION_ENDED" and name not in ("UserPromptSubmit",):
            return
        if name == "UserPromptSubmit":
            if state != "RUNNING":
                self.set_state(rec, "RUNNING", "prompt", now, cp, notify=False)
            else:
                self.heartbeat(rec, now)
            return
        if name in ("PostToolUse", "PostToolUseFailure", "SubagentStop", "TaskCompleted", "SubagentStart", "PreToolUse"):
            if name in ("PostToolUse", "PostToolUseFailure") and state in BLOCKED | {"IDLE", "COMPLETED", "FAILED"}:
                self.set_state(rec, "RUNNING", "progress", now, cp, notify=False)
            else:
                self.heartbeat(rec, now)
            if name in ("TaskCompleted", "SubagentStop"):
                rec["history"] = (rec["history"] + [{"t": int(now), "from": rec["state"], "to": rec["state"], "why": name.lower()}])[-HISTORY_CAP:]
            return
        if name == "Notification":
            ntype = str(ev.get("notification_type") or "")
            if ntype in OWNER_BLOCK_TYPES or (ntype == "idle_prompt" and state in ("RUNNING", "IDLE")):
                self.set_state(rec, "BLOCKED_OWNER", ntype[:30], now, self.owner_cp(cp), notify=True)
            elif ntype == "agent_completed":
                rec["agent_done"] += 1
                self.heartbeat(rec, now)
                if self.cfg["notify"].get("agent_completed"):
                    self.enqueue(rec, "RUNNING", "agent_completed", now, kind="AGENT", extra_key=str(rec["agent_done"]))
            else:
                self.heartbeat(rec, now)
            return
        if name == "StopFailure":
            kind = str(ev.get("error_type") or "unknown")
            kind = kind if kind in STOP_FAILURE_KINDS else "unknown"
            self.set_state(rec, "FAILED", f"stop_failure:{kind}", now, cp, notify=True)
            return
        if name == "Stop":
            if ev.get("stop_hook_active") is True:
                self.heartbeat(rec, now)
                return
            target, reason = "IDLE", "stop"
            if cp and cp_mtime > rec["cycle_started"]:  # strictly newer: a leftover file from before this run never counts
                if cp["state"] == "COMPLETED":
                    target, reason = ("COMPLETED", "checkpoint-verified") if cp["verified"] else ("IDLE", "completion-unverified")
                elif cp["state"] in BLOCKED or cp["state"] == "FAILED":
                    target, reason = cp["state"], "checkpoint"
            self.set_state(rec, target, reason, now, cp, notify=target in NOTIFIABLE)
            return
        self.heartbeat(rec, now)

    @staticmethod
    def owner_cp(cp):
        return cp

    def heartbeat(self, rec, now):
        if now - rec["last_seen"] >= HEARTBEAT_EVERY or now < rec["last_seen"]:
            rec["last_seen"] = now

    # -- sweep
    def sweep(self, now=None):
        now = self.clock() if now is None else now
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
        raise TimeoutError("hook deadline")

    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(HOOK_DEADLINE)
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
            signal.alarm(0)
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
                     "repo": t.get("repo") or rec.get("repo_label"), "ticket": t.get("ticket"), "pr": t.get("pr"), "pending_deliveries": pend})
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
        for key, val, limit in (("owner", args.blocker_owner, 48), ("resume_trigger", args.resume_trigger, 96)):
            if val is not None:
                if clean_text(val, limit, markers) is None:
                    raise SystemExit(f"--{key.replace('_', '-')} must be short plain text")
                body["blocker"][key] = val
    if args.state in BLOCKED and not args.resume_trigger:
        raise SystemExit("a BLOCKED_* checkpoint needs --resume-trigger (the exact condition that unblocks it)")
    if args.state == "COMPLETED":
        if not (args.verified_by and args.evidence):
            raise SystemExit("COMPLETED needs --verified-by and --evidence (a durable reference such as PR#101@<sha> or ci:<run>)")
        if clean_text(args.verified_by, 40, markers) is None or not EVIDENCE.fullmatch(args.evidence) or SECRET_WORDS.search(args.evidence):
            raise SystemExit("--verified-by / --evidence are not plain references")
        body["verified"] = {"by": args.verified_by, "evidence": args.evidence}
    target = pathlib.Path(args.file) if args.file else (pathlib.Path(os.environ["SESSION_STATUS_FILE"]) if os.environ.get("SESSION_STATUS_FILE") else pathlib.Path.cwd() / ".claude" / ".session-status.json")
    atomic_write(target, json.dumps(body, indent=1) + "\n")
    print(f"checkpoint written: state={args.state}")
    return 0


def cmd_render_config(args):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["profile"] = args.profile
    cfg["_note"] = ("Everything is off until a backend sets enabled to true. Credentials are environment-variable NAMES or file paths, never values. "
                    "Remote backends also need approved_destination equal to the server host, and are refused on the corporate profile.")
    text = json.dumps(cfg, indent=2) + "\n"
    if args.out:
        with open(args.out, "x") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("sweep")
    w.add_argument("--now", type=float)
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
