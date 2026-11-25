#!/usr/bin/env python3
"""Fable escalation (SPE-171): one automatic, read-only Fable consult per blocker, inside a shared monthly budget.

The tool decides *eligibility*, keeps *one consult per blocker*, enforces an owner-authored *standing authorization* (scope, retention
state, caps), keeps an *aggregate budget ledger* shared by every session on the host, and trips a *circuit breaker* when the route
fails or the advice does not advance the task. There is no per-consult approval: the owner's standing decision lives in
~/.config/coding-agent-environment/local/fable-authorization.json, which this script only reads (never writes) through
owner-merge-gate.read_owner_file. `attempt` returns CONSULT when a trigger is met and every gate passes; `consult --execute` then
reserves budget, calls Fable once and settles the ledger. Refusal of any kind returns CONTINUE: the baseline is Sonnet +
opus-architect + the existing circuit breaker. No scheduler, hook or background lane calls `consult`.

Route: the claude.ai Max login only (usage credits off). The consult refuses when an API credential or alternate-provider variable is
set in the environment and refuses a result whose `modelUsage` is not exactly `claude-fable-5-2`. Every dollar figure is the client's
list-price estimate (costBasis), not billed money. The ledger gates only invocations that go through this tool.

Stdlib only. State: `$FABLE_ESCALATION_DIR` (default `~/.cache/coding-agent-environment/fable`), mode 0700/0600, one flock around every
read-modify-write. The budget ledger lives under the authorization's `ledger_dir` with its own flock. Lock order is always
blocker-state lock first, then ledger lock; the ledger lock is never held during the model call.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import select
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location

FABLE_MODEL = "claude-fable-5-2"
MIN_CLAUDE = (2, 1, 321)  # Fable 5.2 needs this Claude Code version or later (runbook)
CONSULT_TIMEOUT_S = 540
PER_CONSULT_CEILING = 4.0  # USD; the script ceiling, whatever the authorization file says
MONTHLY_CEILING = 40.0
CONSULT_BUDGET_USD = "4"  # kept for the estimate's cap_usd: the reservation, not the client cap
AUTH_PATH = "~/.config/coding-agent-environment/local/fable-authorization.json"
AUTH_SCHEMA = "fable-authorization/v1"
AUTH_TICKET = "SPE-171"
BILLING_ROUTE = "claude.ai-max-usage-credits-off"
MANAGED_SETTINGS = "/Library/Application Support/ClaudeCode/managed-settings.json"
MAX_PROMPT_BYTES = 8192
TAIPEI = timezone(timedelta(hours=8))  # fixed UTC+8, no DST: the budget month is Asia/Taipei
FALLBACK = "sonnet + opus-architect; existing circuit breaker"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_BIN = os.path.join(REPO_ROOT, "bin", "coding-agent-profile")
DISALLOWED_READS = "Read(**/.env*),Read(~/.ssh/**),Read(~/.config/**),Read(~/.claude/**)"
MIN_ATTEMPTS = 2
CAUSES = {"none", "queue_delay", "ci_unavailable", "missing_permission", "external_outage", "owner_decision"}
# A failure text that names one of these is infrastructure or permission, never a reasoning blocker, whatever `cause` says.
EXCLUDED_TEXT = re.compile(r"CI_UNAVAILABLE_EXTERNAL|HARNESS_PERMISSION_BLOCK|spending limit|billing limit|quota|permission .{0,30}denied|"
                           r"denied.{0,20}permission|not permitted|forbidden|unauthori[sz]ed|EACCES|\bHTTP/?[0-9. ]*(40[13]|50[23])\b|overloaded|"
                           r"queue(d)? (delay|wait)|outage|ECONNRESET|rate.?limit|runner unavailable|"
                           r"owner[- ](held|decision|action|approval|only)\b|(requires?|needs?) (an? |the )?owner (decision|action|approval|to)\b|only the owner\b|"
                           r"credentials? (is|are) (missing|unavailable|inaccessible) .{0,30}(provision|owner|account)|"
                           r"not satisfiable (with|under) (the )?current authori[sz]ation|"
                           r"(gateway|connect(ion)?|network|request) time[sd]? ?-?out", re.I)
# Any of these set means the call may not use the included subscription. Presence only; the values are never read or printed.
API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
           "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
TARGET_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+")
SESSION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
ID_RE = re.compile(r"[0-9a-f]{16}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

READY, CONSULTING, CONSULTED, FAILED, CLOSED = "READY", "CONSULTING", "CONSULTED", "FAILED", "CLOSED"
RESERVED, SETTLED = "RESERVED", "SETTLED"

# Text that must not reach a Fable prompt, nor be stored at `attempt`. First matching rule names the refusal.
PAYLOAD_RULES = (
    ("secret", re.compile(r"(?i)(token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential|authorization)[\w-]*[\"'\]]*\s*[=:]"
                          r"|\bbearer\s+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{6,}|\b(?:password|passwd|passphrase|secret|token|api key)\s+(?:is|was)\s+\S+|sk_(?:live|test)_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{35}|github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}"
                          r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|sk-ant-|AKIA[0-9A-Z]{16}|xox[abprs]-"
                          r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*|-----BEGIN |[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"
                          r"|(?=[A-Za-z0-9+/=]*[0-9])[A-Za-z0-9+/=]{40,}")),
    ("transcript", re.compile(r"(?m)^\s*(Human|Assistant)\s*:|\"role\"\s*:|\"type\"\s*:\s*\"(assistant|user|tool_use)\"|<system-reminder|"
                              r"\.claude/projects/|\.jsonl\b")),
    ("email", re.compile(r"(?i)(?<![\w.+-])(?!noreply@)[\w.+-]+@[A-Za-z0-9-]+(?:\.[\w-]+)*")),
)


class EscalationError(Exception):
    pass


class LedgerError(Exception):
    pass


class Refusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def now_utc():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def state_dir():
    path = os.environ.get("FABLE_ESCALATION_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "coding-agent-environment", "fable")
    os.makedirs(os.path.join(path, "advice"), mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextmanager
def locked(directory):
    fd = os.open(os.path.join(directory, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(path, data, text=False):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        fh.write(data if text else json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _approach_key(text):
    """Two approaches that use the same words in another order are one approach, not two."""
    return " ".join(sorted(set(re.findall(r"\w+", _norm(text)))))


def fingerprint(target, acceptance_check):
    """One blocker = one target and one acceptance check; case, spacing and punctuation do not make a new blocker.

    A genuinely reworded acceptance check does: key the check by its stable id (AC3), not by prose."""
    check = re.sub(r"[^\w ]+", " ", _norm(acceptance_check))
    return hashlib.sha256((target.lower() + "\0" + " ".join(check.split())).encode()).hexdigest()[:16]


def _record_path(directory, blocker_id):
    if not ID_RE.fullmatch(blocker_id or ""):
        raise EscalationError("blocker id must be 16 hex characters")
    return os.path.join(directory, blocker_id + ".json")


def load(directory, blocker_id):
    try:
        with open(_record_path(directory, blocker_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        raise EscalationError(f"no blocker record {blocker_id}")


def save(directory, record):
    _write(_record_path(directory, record["id"]), record)


def receipt(directory, event, record, **detail):
    line = {"at": iso(now_utc()), "event": event, "id": record.get("id"), "state": record.get("state"), **detail}
    fd = os.open(os.path.join(directory, "receipts.log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


# --- circuit breaker ---------------------------------------------------------------------------------------------------

def _shared_breaker_dir():
    """The ledger directory (owner-governed, shared by every state directory), or None when no authorization names one."""
    auth, _reason = load_authorization()
    return auth["ledger_dir"] if auth else None


def _read_breaker(path):
    try:
        with open(path) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {"open": True, "reason": "breaker_unreadable"}
    except FileNotFoundError:
        return {"open": False}
    except (OSError, ValueError):
        return {"open": True, "reason": "breaker_unreadable"}  # a corrupt shared breaker fails closed


def breaker(directory):
    """Open when either the per-state-directory breaker or the shared one (in the ledger directory, so a fresh state directory cannot
    bypass it) is open. The shared one wins when both are."""
    shared = _shared_breaker_dir()
    if shared and os.path.isdir(shared):
        state = _read_breaker(os.path.join(shared, "breaker.json"))
        if state.get("open"):
            return state
    try:
        with open(os.path.join(directory, "breaker.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"open": False}


def _write_shared_breaker(state):
    """Best effort: write the breaker into the ledger directory under the ledger lock (lock order: state lock, then ledger lock)."""
    auth, _reason = load_authorization()
    if not auth:
        return False
    try:
        ledger = ledger_dir_ready(auth["ledger_dir"])
        with locked(ledger):
            _write(os.path.join(ledger, "breaker.json"), state)
    except (LedgerError, OSError):
        return False
    return True


def trip(directory, reason, blocker_id, at):
    if breaker(directory).get("open"):
        return breaker(directory)  # keep the first reason
    state = {"open": True, "reason": reason, "blocker": blocker_id, "since": iso(at)}
    if not _write_shared_breaker(state):  # the shared breaker is the one that counts; the local file is only the fallback
        _write(os.path.join(directory, "breaker.json"), state)
    receipt(directory, "breaker_open", {"id": blocker_id, "state": None}, reason=reason)
    return state


def reset_breaker(directory, note, at):
    """Owner action: nothing closes the breaker automatically. Clears the per-directory and the shared breaker."""
    if not note or len(note.strip()) < 8:
        raise EscalationError("reset needs --note: why the route or the advice problem is resolved")
    cleared = {"open": False, "reset_at": iso(at), "note": note.strip()[:200]}
    with locked(directory):
        _write(os.path.join(directory, "breaker.json"), cleared)
        _write_shared_breaker(cleared)
        receipt(directory, "breaker_reset", {"id": None, "state": None}, note=note.strip()[:200])


# --- payload minimization ----------------------------------------------------------------------------------------------

def payload_violation(text):
    """Name of the first rule the text breaks (secret, transcript, email, size), or None. Offline and deterministic."""
    text = str(text or "")
    for name, pattern in PAYLOAD_RULES:
        if pattern.search(text):
            return name
    if len(text.encode()) > MAX_PROMPT_BYTES:
        return "size"
    return None


def record_payload_violation(record):
    """First rule broken by the assembled prompt or by any single stored text field (a line-prefix marker only shows up alone)."""
    fields = [record.get("blocker"), record.get("acceptance_check"), record.get("question"),
              *(p for a in record.get("attempts") or [] for p in (a.get("model"), a.get("approach"), a.get("failure"))),
              *((record.get("review") or {}).get("positions") or [])]
    return payload_violation(consult_prompt(record)) or next((r for r in map(payload_violation, fields) if r), None)


# --- standing authorization --------------------------------------------------------------------------------------------

def _omg():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = spec_from_file_location("owner_merge_gate", os.path.join(here, "owner-merge-gate.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keys(obj, expected, label):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise ValueError(f"{label} must be an object with exactly the keys {sorted(expected)}")
    return obj


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return float(value)


def validate_authorization(obj):
    """Closed schema v1. Returns the normalized authorization with effective caps (file value, never above the script ceiling)."""
    obj = _keys(obj, ("schema", "decision", "model", "billing_route", "retention", "isolation", "scope", "caps", "ledger_dir"), "authorization")
    if obj["schema"] != AUTH_SCHEMA:
        raise ValueError(f"schema must be {AUTH_SCHEMA}")
    decision = _keys(obj["decision"], ("ticket", "date", "by", "ref"), "decision")
    if decision["ticket"] != AUTH_TICKET or decision["by"] != "owner":
        raise ValueError(f"decision must be the owner's decision on {AUTH_TICKET}")
    if not isinstance(decision["date"], str) or not DATE_RE.fullmatch(decision["date"]):
        raise ValueError("decision.date must be YYYY-MM-DD")
    if not isinstance(decision["ref"], str) or not decision["ref"].strip() or len(decision["ref"]) > 300:
        raise ValueError("decision.ref must be a short non-empty string")
    if obj["model"] != FABLE_MODEL:
        raise ValueError(f"model must be {FABLE_MODEL}")
    if obj["billing_route"] != BILLING_ROUTE:
        raise ValueError(f"billing_route must be {BILLING_ROUTE}")
    retention = _keys(obj["retention"], ("model_improvement_setting", "confirmed_on"), "retention")
    if retention["model_improvement_setting"] not in ("off", "on", "unconfirmed"):
        raise ValueError("retention.model_improvement_setting must be off, on or unconfirmed")
    confirmed = retention["confirmed_on"]
    if confirmed is not None and not (isinstance(confirmed, str) and DATE_RE.fullmatch(confirmed)):
        raise ValueError("retention.confirmed_on must be YYYY-MM-DD or null")
    if retention["model_improvement_setting"] == "off" and confirmed is None:
        raise ValueError("retention 'off' needs confirmed_on: the owner's attested date")
    isolation = _keys(obj["isolation"], ("verified_on",), "isolation")
    verified = isolation["verified_on"]
    if verified is not None and not (isinstance(verified, str) and DATE_RE.fullmatch(verified)):
        raise ValueError("isolation.verified_on must be YYYY-MM-DD or null")
    scope = _keys(obj["scope"], ("host_sha256", "repo_owners", "allowed_profiles"), "scope")
    if not isinstance(scope["host_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", scope["host_sha256"]):
        raise ValueError("scope.host_sha256 must be 64 lowercase hex characters")
    for key in ("repo_owners", "allowed_profiles"):
        if not isinstance(scope[key], list) or not all(isinstance(x, str) and x.strip() for x in scope[key]):
            raise ValueError(f"scope.{key} must be a list of non-empty strings")
    caps = _keys(obj["caps"], ("per_consult_usd", "monthly_usd", "timezone"), "caps")
    if caps["timezone"] != "Asia/Taipei":
        raise ValueError("caps.timezone must be Asia/Taipei")
    per = min(_number(caps["per_consult_usd"], "caps.per_consult_usd"), PER_CONSULT_CEILING)
    monthly = min(_number(caps["monthly_usd"], "caps.monthly_usd"), MONTHLY_CEILING)
    ledger = obj["ledger_dir"]
    if not isinstance(ledger, str) or not ledger:
        raise ValueError("ledger_dir must be a path")
    ledger = os.path.expanduser(ledger)
    home = os.path.realpath(os.path.expanduser("~"))
    if not os.path.isabs(ledger) or os.path.commonpath([home, os.path.realpath(ledger)]) != home or os.path.realpath(ledger) == home:
        raise ValueError("ledger_dir must be an absolute path under the home directory")
    return {"retention": {"setting": retention["model_improvement_setting"], "confirmed_on": confirmed},
            "isolation": {"verified_on": verified},
            "scope": {"host_sha256": scope["host_sha256"], "repo_owners": [x.strip().lower() for x in scope["repo_owners"]],
                      "allowed_profiles": list(scope["allowed_profiles"])},
            "caps": {"per_consult_usd": per, "monthly_usd": monthly}, "ledger_dir": ledger,
            "decision": {k: decision[k] for k in ("ticket", "date", "by")}}


def load_authorization(path=None, root=None):
    """(authorization, None) or (None, reason). Only the owner-merge-gate trust-anchor reader opens the file."""
    try:
        auth = _omg().read_owner_file(path or AUTH_PATH, True, validate_authorization, "fable authorization", root)
    except FileNotFoundError:
        return None, "no_authorization"
    except (ValueError, OSError):
        return None, "authorization_invalid"
    return auth, None


def host_hash():
    """sha256 of the macOS IOPlatformUUID, or None when it cannot be read (then the host scope fails closed)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', out)
    return hashlib.sha256(found.group(1).encode()).hexdigest() if found else None


GITHUB_REMOTE_RE = re.compile(r"(?:https://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
                              r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")


def _git(cwd, *argv):
    # A clean environment: an inherited GIT_DIR or GIT_WORK_TREE must not redirect the probe to another repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(["git", "-C", cwd, *argv], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def origin_repo(cwd):
    """(owner, repo), lowercase, of the `origin` remote (first remote if none) of the repository holding `cwd`; None when unknown.

    The URL must be exactly a github.com https/ssh form: lookalike hosts, path-embedded `github.com` and other shapes do not parse."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    remotes = (_git(top, "remote") or "").split()
    if not remotes:
        return None
    found = GITHUB_REMOTE_RE.fullmatch(_git(top, "remote", "get-url", "origin" if "origin" in remotes else remotes[0]) or "")
    if not found:
        return None
    owner, repo = found.group(1).lower(), found.group(2).lower()
    # "." and ".." name no repository, and a doubled `.git.git` suffix is ambiguous: both fail closed (so does ssh://...:22/).
    if owner in (".", "..") or repo in (".", "..") or repo.endswith(".git"):
        return None
    return owner, repo


def origin_owner(cwd):
    repo = origin_repo(cwd)
    return repo[0] if repo else None


def repo_in_scope(target, cwd, owners):
    """The target's owner is authorized and the working tree is a clone of exactly that repository."""
    repo = origin_repo(cwd)
    wanted = tuple(part.lower() for part in target.split("#", 1)[0].split("/", 1))
    return repo is not None and repo == wanted and wanted[0] in owners


PROFILE_RE = re.compile(r"status=(?P<status>none|ok) name=(?P<name>[A-Za-z0-9_.-]*) root=(?P<root>[^\n]*) overlay=(?P<overlay>[^\n]*)")


def managed_session(cwd, allowed_profiles):
    """True when this session looks company-managed: a managed-settings file exists (presence only), or the profile resolver says
    anything other than an exact `none`/allowed-`ok` answer. Unexpected output, a non-zero exit or a failure to run all count as managed."""
    if os.path.exists(MANAGED_SETTINGS):
        return True
    if not os.access(PROFILE_BIN, os.X_OK):
        return True  # the resolver is how a company profile is recognised: without it the session cannot be shown to be personal
    try:
        proc = subprocess.run([PROFILE_BIN, "resolve", "--cwd", cwd], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    found = PROFILE_RE.fullmatch(proc.stdout.strip()) if proc.returncode == 0 else None
    if not found:
        return True
    if found["status"] == "none":
        return found["name"] != ""
    return found["name"] not in allowed_profiles


EXPORT_MAX_ENTRIES, EXPORT_MAX_FILES, EXPORT_MAX_FILE_BYTES, EXPORT_MAX_TOTAL_BYTES = 50000, 10000, 512 << 10, 16 << 20
EXPORT_DEADLINE_S = 120.0
EXPORT_MAX_LISTING_BYTES = 16 << 20
# Matched against EVERY path component, case-insensitively: credential-looking or tool-configuration names are never exported.
EXPORT_SKIP = re.compile(r"(?i)^\.env|\.env($|\.)|^\.(claude|codex|aws|ssh|gnupg|config|npmrc|netrc|pypirc|git-credentials|git)$"
                         r"|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|jks|keystore|kdbx)$"
                         r"|credential|secret|token|passwd|password|private[_-]?key")


def _skipped_name(name):
    return any(EXPORT_SKIP.search(part) for part in name.split("/"))


def _safe_export_name(name):
    """A tree path that can be written under the export root: relative, no empty/'.'/'..'/.git component."""
    return bool(name) and not name.startswith("/") and all(part not in ("", ".", "..") and part.lower() != ".git" for part in name.split("/"))


def _read_blob(top, sha, size, env):
    """The blob's bytes, read at most size+1 bytes (a blob larger than its listed size is refused by the caller)."""
    proc = subprocess.Popen(["git", "-C", top, "cat-file", "blob", sha], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        data = proc.stdout.read(size + 1)
    finally:
        proc.kill()
        proc.stdout.close()
        proc.wait()
    return data


def _stream_listing(top, env, deadline):
    """Rows of `git ls-tree -r -l -z HEAD`, read in bounded chunks. None (git killed and reaped) when the listing passes the entry cap, the
    byte cap or the deadline, or when git fails: the cap applies while the data arrives, not after it is all in memory."""
    proc = subprocess.Popen(["git", "-C", top, "ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    rows, pending, received = [], b"", 0
    try:
        fd = proc.stdout.fileno()
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], min(left, 1.0))[0]:
                if left <= 0:
                    return None
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            received += len(chunk)
            pending += chunk
            *done, pending = pending.split(b"\0")
            rows.extend(done)
            if received > EXPORT_MAX_LISTING_BYTES or len(rows) > EXPORT_MAX_ENTRIES or len(pending) > 8192:
                return None
        if pending:
            return None  # the listing must end on a NUL terminator
        return [r.decode() for r in rows if r] if proc.wait(timeout=10) == 0 else None
    except (OSError, ValueError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()


def export_tracked(cwd, dest):
    """Export the COMMITTED blobs (HEAD) of the repository holding `cwd` into `dest`; never reads the working tree or the index.

    Skipped: symlinks (mode 120000), submodules (160000), credential-looking names (any path component), files over 1 MB, and files whose
    content has a NUL byte, is not strict UTF-8, or breaks any payload rule (secret, transcript marker, email). Returns (ok, content_hits): the paths skipped for content,
    which the caller checks against the prompt. Fail closed (ok False) on an unsafe tree path, on more than the allowed entries, files or
    bytes (judged from the listed sizes before any blob is read), on a git failure or when the overall deadline passes.
    Uncommitted, staged and untracked files are not exported. A repository with no commit exports nothing."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return False, []
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if _git(top, "rev-parse", "--verify", "-q", "HEAD^{commit}") is None:
        return True, []
    deadline = time.monotonic() + EXPORT_DEADLINE_S
    rows = _stream_listing(top, env, deadline)
    if rows is None:
        return False, []
    root = os.path.realpath(dest)
    total = count = 0
    hits = []
    for seen, row in enumerate(rows, 1):
        if seen > EXPORT_MAX_ENTRIES or time.monotonic() > deadline:
            return False, []
        meta, _, name = row.partition("\t")
        fields = meta.split()
        if len(fields) != 4:
            return False, []
        mode, kind, sha, size = fields
        if not _safe_export_name(name):
            return False, []
        if mode not in ("100644", "100755") or kind != "blob" or _skipped_name(name) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        if not size.isdigit():
            return False, []
        size = int(size)
        if size > EXPORT_MAX_FILE_BYTES:
            continue
        count, total = count + 1, total + size
        if count > EXPORT_MAX_FILES or total > EXPORT_MAX_TOTAL_BYTES:
            return False, []
        try:
            blob = _read_blob(top, sha, size, env)
        except (OSError, subprocess.SubprocessError):
            return False, []
        if len(blob) != size:
            return False, []
        try:
            text = blob.decode("utf-8")  # strict: invalid bytes cannot hide a secret from the scan
            if b"\0" in blob:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL byte")
        except UnicodeDecodeError:
            hits.append(name)
            continue
        if any(pattern.search(text) for _name, pattern in PAYLOAD_RULES):  # secret, transcript marker or email: skipped and counted
            hits.append(name)
            continue
        target = os.path.join(root, name)
        if os.path.commonpath([root, os.path.realpath(target)]) != root:
            return False, []
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fout:
                fout.write(blob)
        except OSError:  # includes a name collision on a case-insensitive filesystem: fail closed
            return False, []
    return True, hits


def isolation_verdict():
    """Static verdict (no model call): what the consult relies on and whether this CLI lists the flags. Honouring is UNVERIFIED."""
    try:
        text = subprocess.run([claude_bin(), "--help"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    flags = ("--setting-sources", "--permission-mode", "--disallowedTools")
    listed = all(f in text or f.lower().replace("tools", "-tools") in text.lower() for f in flags)
    return {"cwd": "temporary export of committed (HEAD) credential-free blobs; a smaller working set, not a confinement (an absolute path is still readable unless a flag stops it)",
            "setting_sources": "project (intended to leave user settings and ~/.claude/CLAUDE.md out; UNVERIFIED)",
            "permission_mode": "dontAsk", "disallowed_tools": DISALLOWED_READS, "cli_lists_flags": listed,
            "honoured_in_print_mode": "UNVERIFIED until the live-verification case inspects the stream-json tool-use list"}


def authorize(target, cwd, path=None, root=None, retention=True):
    """(authorization, None) when every standing gate passes for this target and working directory, else (None, reason)."""
    auth, reason = load_authorization(path, root)
    if reason:
        return None, reason
    if retention and auth["retention"]["setting"] != "off":
        return None, "retention_unconfirmed"
    if host_hash() != auth["scope"]["host_sha256"]:
        return None, "scope_host_mismatch"
    if not repo_in_scope(target, cwd, auth["scope"]["repo_owners"]):
        return None, "scope_repo_not_owned"
    if managed_session(cwd, auth["scope"]["allowed_profiles"]):
        return None, "managed_session"
    return auth, None


def authorization_template(cwd):
    owner = origin_owner(cwd)
    candidate = {"schema": AUTH_SCHEMA,
                 "decision": {"ticket": AUTH_TICKET, "date": "2026-11-25", "by": "owner", "ref": "owner decision in chat 2026-11-25 (SPE-171)"},
                 "model": FABLE_MODEL, "billing_route": BILLING_ROUTE,
                 "retention": {"model_improvement_setting": "unconfirmed", "confirmed_on": None},
                 "isolation": {"verified_on": None},
                 "scope": {"host_sha256": host_hash() or "", "repo_owners": [owner] if owner else [], "allowed_profiles": []},
                 "caps": {"per_consult_usd": 4, "monthly_usd": 40, "timezone": "Asia/Taipei"},
                 "ledger_dir": "~/.local/state/coding-agent-environment/fable-budget"}
    text = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
    return {"install_path": AUTH_PATH, "candidate": candidate, "host_sha256": candidate["scope"]["host_sha256"] or None,
            "file_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "file_sha256_of": "json.dumps(candidate, indent=2, sort_keys=True) followed by one newline",
            "owner_steps": ["after the one deliberate `consult --live-verification` case passes its verdict, set isolation.verified_on to that date",
                            "confirm the claude.ai 'help improve Claude' setting is OFF, then set retention.model_improvement_setting to "
                            "'off' and confirmed_on to that date",
                            "install the file yourself with mode 0600 in a directory only you can write; this script never writes it"]}


def authorization_check(cwd, target, at, path=None, root=None):
    auth, reason = load_authorization(path, root)
    if reason:
        return {"authorization": reason}
    owners = auth["scope"]["repo_owners"]
    out = {"authorization": "ok",
           "retention": {"setting": auth["retention"]["setting"], "confirmed_on": auth["retention"]["confirmed_on"],
                         "verdict": "ok" if auth["retention"]["setting"] == "off" else "retention_unconfirmed"},
           "scope": {"host": "match" if host_hash() == auth["scope"]["host_sha256"] else "scope_host_mismatch",
                     "cwd_repo_owner": "owned" if origin_owner(cwd) in owners else "scope_repo_not_owned",
                     "managed_session": managed_session(cwd, auth["scope"]["allowed_profiles"])},
           "caps": auth["caps"]}
    if target:
        out["scope"]["target_repo"] = "owned" if repo_in_scope(target, cwd, owners) else "scope_repo_not_owned"
    out["isolation"] = {**isolation_verdict(), "verified_on": auth["isolation"]["verified_on"],
                        "consults_enabled": auth["isolation"]["verified_on"] is not None}
    try:
        out["month_budget"] = month_budget(auth, at)
    except LedgerError:
        out["month_budget"] = {"error": "ledger_unreadable"}
    return out


# --- aggregate budget ledger -------------------------------------------------------------------------------------------

def month_key(at):
    return at.astimezone(TAIPEI).strftime("%Y-%m")


def ledger_dir_ready(path, create=True):
    """The ledger directory: private (0700), owned by the caller and not a symlink; created when missing."""
    try:
        if not os.path.lexists(path):
            if not create:
                return None
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        st = os.lstat(path)
    except OSError as exc:
        raise LedgerError(str(exc))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise LedgerError("ledger_dir must be a real directory owned by the caller with mode 0700")
    return path


def _money(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def read_month(directory, month):
    path = os.path.join(directory, month + ".json")
    if not os.path.exists(path):
        return {"month": month, "entries": []}
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("month") != month or not isinstance(data.get("entries"), list):
            raise ValueError("bad shape")
        for e in data["entries"]:
            if e["state"] not in (RESERVED, SETTLED) or not _money(e["reserved_usd"]) or not isinstance(e["rid"], str):
                raise ValueError("bad entry")
            if e["state"] == SETTLED and not _money(e["charged_usd"]):
                raise ValueError("bad charge")  # NaN, infinity, negative and non-numbers would all hide spend
            parse(e["reserved_at"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LedgerError(f"ledger for {month} is unreadable: {type(exc).__name__}")
    return data


def committed(data):
    """Settled charges plus the full reservation of every RESERVED entry."""
    return round(sum(e["charged_usd"] if e["state"] == SETTLED else e["reserved_usd"] for e in data["entries"]), 6)


def _journal(directory, event, **detail):
    fd = os.open(os.path.join(directory, "journal.jsonl"), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps({"event": event, **detail}, sort_keys=True) + "\n")


def ledger_reserve(auth, blocker_id, session, at):
    """Reserve one consult's full per-consult cap. Raises Refusal (budget, dedup) or LedgerError (unreadable)."""
    directory = ledger_dir_ready(auth["ledger_dir"])
    month, per = month_key(at), auth["caps"]["per_consult_usd"]
    with locked(directory):
        data = read_month(directory, month)
        if any(e.get("blocker_id") == blocker_id for e in data["entries"]):
            raise Refusal("blocker_already_reserved_this_month")
        if round(committed(data) + per, 6) > auth["caps"]["monthly_usd"]:
            raise Refusal("monthly_budget_exhausted")
        rid = secrets.token_hex(8)
        data["entries"].append({"rid": rid, "blocker_id": blocker_id, "session": session, "reserved_usd": per, "state": RESERVED,
                                "charged_usd": None, "basis": None, "reserved_at": iso(at), "settled_at": None})
        try:
            _write(os.path.join(directory, month + ".json"), data)
            _journal(directory, "reserved", rid=rid, blocker_id=blocker_id, usd=per, at=iso(at), month=month)
        except OSError as exc:
            raise LedgerError(str(exc))
    return rid, month


def ledger_settle(auth, month, rid, charge, basis, at):
    directory = ledger_dir_ready(auth["ledger_dir"])
    with locked(directory):
        data = read_month(directory, month)
        for e in data["entries"]:
            if e["rid"] == rid:
                e.update(state=SETTLED, charged_usd=round(float(charge), 6), basis=basis, settled_at=iso(at))
                break
        else:
            raise LedgerError("reservation not found")
        try:
            _write(os.path.join(directory, month + ".json"), data)
            _journal(directory, "settled", rid=rid, usd=charge, basis=basis, at=iso(at), month=month)
        except OSError as exc:
            raise LedgerError(str(exc))


def month_budget(auth, at):
    """Read-only month summary. A RESERVED entry older than the consult timeout is `orphaned`: still counted, never auto-released."""
    directory = ledger_dir_ready(auth["ledger_dir"], create=False)
    month = month_key(at)
    data = read_month(directory, month) if directory else {"month": month, "entries": []}
    spent = committed(data)
    try:
        orphaned = [e["rid"] for e in data["entries"] if e["state"] == RESERVED
                    and (at - parse(e["reserved_at"])).total_seconds() > CONSULT_TIMEOUT_S]
    except ValueError as exc:
        raise LedgerError(f"ledger timestamp unreadable: {exc}")
    return {"month": month, "committed_usd": spent, "monthly_usd": auth["caps"]["monthly_usd"],
            "remaining_usd": round(auth["caps"]["monthly_usd"] - spent, 6), "entries": len(data["entries"]),
            "reserved": sum(1 for e in data["entries"] if e["state"] == RESERVED), "orphaned": orphaned}


# --- eligibility -------------------------------------------------------------------------------------------------------

def evaluate(case, directory=None):
    """Pure verdict for one blocker. Never writes. `reasons` explains an ineligible verdict; `trigger` names the rule that fired."""
    reasons = []
    target = str(case.get("target") or "")
    check = str(case.get("acceptance_check") or "")
    question = str(case.get("question") or "").strip()
    if not TARGET_RE.fullmatch(target):
        reasons.append("target must be owner/repo#N")
    if not _norm(check):
        reasons.append("acceptance_check is required")
    if case.get("cause", "none") not in CAUSES:
        reasons.append("unknown cause")
    elif case.get("cause", "none") != "none":
        reasons.append(f"excluded cause: {case['cause']}")
    attempts = [a for a in case.get("attempts") or [] if isinstance(a, dict)]
    if any(EXCLUDED_TEXT.search(str(a.get("failure") or "")) for a in attempts):
        reasons.append("failure text names an infrastructure or permission cause")
    if case.get("inputs_non_sensitive") is not True:
        reasons.append("inputs must be attested non-sensitive")
    if "\n" in question or question.count("?") != 1 or not question.endswith("?") or not 12 <= len(question) <= 300:
        reasons.append("exactly one concrete one-line question ending in ? is required")
    trigger = None
    opus = [a for a in attempts if "opus" in _norm(a.get("model"))]
    by_failure = {}
    for a in opus:
        by_failure.setdefault(_norm(a.get("failure")), set()).add(_approach_key(a.get("approach")))
    if any(f and len(approaches - {""}) >= MIN_ATTEMPTS for f, approaches in by_failure.items()):
        trigger = "two_distinct_attempts_same_failure"
    review = case.get("review") or {}
    if review.get("unresolved_architecture_disagreement") is True and len([p for p in review.get("positions") or [] if _norm(p)]) >= 2:
        trigger = trigger or "unresolved_review_disagreement"
    if trigger is None:
        reasons.append("no trigger: need two materially different Opus attempts with the same acceptance failure, "
                       "or an unresolved architecture disagreement with two stated positions")
    existing = None
    if TARGET_RE.fullmatch(target) and _norm(check):
        bid = fingerprint(target, check)
        if directory:
            try:
                existing = load(directory, bid)
            except EscalationError:
                # An unreadable record may be a spent blocker: refuse rather than overwrite it.
                existing = {"state": "UNREADABLE"} if os.path.exists(_record_path(directory, bid)) else None
    else:
        bid = None
    if directory and breaker(directory).get("open"):
        reasons.append("circuit breaker open: " + str(breaker(directory).get("reason")))
    if existing:
        reasons.append(f"blocker already {existing['state']}: one consult per blocker")
    return {"eligible": not reasons, "trigger": trigger, "reasons": reasons, "id": bid,
            "on_ineligible": "continue independent work" if reasons else None}


def _consult_fields(case):
    """The fields of `case` that the consult prompt is built from, truncated exactly as the saved record keeps them."""
    attempts = [{"model": str(a.get("model"))[:40], "approach": str(a.get("approach"))[:200], "failure": str(a.get("failure"))[:200]}
                for a in case["attempts"]][:8]
    return {"acceptance_check": str(case["acceptance_check"])[:300], "blocker": str(case.get("blocker") or case["acceptance_check"])[:300],
            "attempts": attempts, "question": case["question"].strip(), "review": case.get("review") or {}}


def _create_ready(directory, case, session_id, at, verdict):
    """Write the READY record. The caller holds the lock and has just evaluated `case` as eligible and authorized."""
    record = {"id": verdict["id"], "state": READY, "created": iso(at), "session_id": session_id, "trigger": verdict["trigger"],
              "target": case["target"], **_consult_fields(case),
              "spent": {k: case.get("spent", {}).get(k) for k in ("rework_rounds", "elapsed_s", "usd")}}
    save(directory, record)
    receipt(directory, "ready", record, trigger=verdict["trigger"])
    return record


# Conservative bytes-per-token ratio for the consult packet: English and code usually run near 4 bytes per token, so 3 overcounts tokens
# and therefore cost. Deterministic; nothing is tokenized by a model.
BYTES_PER_TOKEN = 3
COST_REFERENCE = os.path.join(REPO_ROOT, "docs", "fable", "consult-cost-reference.json")
COST_KEYS = ("estimated_usd_low", "estimated_usd_high", "cap_usd", "basis", "model", "invoked")


def _cents_up(usd):
    return math.ceil(round(usd * 100, 6)) / 100


def client_cap(per_consult=PER_CONSULT_CEILING):
    """`--max-budget-usd` for the call: $1 of headroom inside the reservation, because the client checks only between turns."""
    cap = per_consult - 1.0 if per_consult > 1.5 else per_consult * 0.75
    return f"{cap:.2f}"


def estimate(record, reference_path=None):
    """List-price estimate of one consult, before any invocation. Pure: no model, no network, no state directory.

    low  = a cold call writing the measured prefix plus the packet, priced at the highest measured cache-write rate;
    high = the per-consult reservation ceiling. The documented consults are listed in `basis` as reference points."""
    cap = PER_CONSULT_CEILING
    basis = [f"model {FABLE_MODEL}; nothing was invoked to produce this estimate; list price, not an invoice"]
    low = cap
    try:
        size = len(consult_prompt(record).encode())
        tokens = -(-size // BYTES_PER_TOKEN)
        basis.append(f"consult packet {size} bytes, counted as {tokens} tokens at a conservative {BYTES_PER_TOKEN} bytes per token")
    except Exception:  # noqa: BLE001 - an estimate must still be returned; fall back to the cap
        tokens = None
        basis.append("consult packet size unreadable: low set to the cap")
    try:
        with open(reference_path or COST_REFERENCE) as fh:
            ref = json.load(fh)
        runs = [r for r in ref.get("cold_prefix_runs") or []
                if isinstance(r.get("usd"), (int, float)) and isinstance(r.get("cache_creation_tokens"), int) and r["cache_creation_tokens"] > 0]
        consults = [c for c in ref.get("consults") or [] if isinstance(c.get("usd"), (int, float))]
    except (OSError, ValueError, AttributeError):
        runs, consults = None, []
        basis.append("reference file unreadable: low set to the cap")
    if runs and tokens is not None:
        rate = max(r["usd"] / r["cache_creation_tokens"] for r in runs)
        prefix = max(runs, key=lambda r: r["usd"])
        low = _cents_up(prefix["usd"] + tokens * rate)
        basis.append(f"low: a cold call writes the measured {prefix['cache_creation_tokens']}-token prefix (${prefix['usd']}, "
                     f"{prefix.get('source')} {prefix.get('date')}) plus the packet at the highest measured cache-write rate "
                     f"(${rate * 1e6:.2f} per million tokens); a warm prefix cache can cost less")
    elif runs == []:
        basis.append("no measured cold-prefix run in the reference file: low set to the cap")
    basis.append(f"high: the per-consult reservation ceiling ${cap:.2f}; the call itself runs with --max-budget-usd {client_cap()}, which the "
                 "client checks only between turns, so a turn already running can end above it (the $1 headroom absorbs that)")
    for c in consults:
        basis.append(f"reference: ${c['usd']} on {c.get('date')}: {c.get('what')} ({c.get('source')})")
    if low > cap:
        low = cap
        basis.append("low clamped to the cap")
    return {"estimated_usd_low": low, "estimated_usd_high": cap, "cap_usd": cap, "basis": basis, "model": FABLE_MODEL, "invoked": False}


def estimate_case(case):
    """`estimate --case`: the same estimate a consult would be reserved against for this case, with no eligibility check and no state."""
    try:
        return estimate(_consult_fields(case))
    except (KeyError, TypeError, AttributeError):
        raise EscalationError("case needs acceptance_check, attempts and question")


# --- workflow entry: record an attempt, get exactly one action -----------------------------------------------------------

def _attempt_path(directory, blocker_id):
    os.makedirs(os.path.join(directory, "attempts"), mode=0o700, exist_ok=True)
    return os.path.join(directory, "attempts", blocker_id + ".json")


def _uncounted(args):
    return bool(args.get("cause") and args["cause"] != "none") or bool(EXCLUDED_TEXT.search(args.get("failure") or ""))


def _continue(bid, reason, **extra):
    out = {"action": "CONTINUE", "reason": reason, "fallback": FALLBACK, **extra}
    if bid:
        out["id"] = bid
    return out


def attempt(directory, args, session_id, at, auth_path=None, auth_root=None):
    """What the development loop calls after every failed attempt. Never blocks and never raises for a normal outcome.

    Returns {"action": ...}: CONTINUE (do nothing, keep working) or CONSULT (run `consult --id X --execute` once)."""
    texts = [args.get("approach"), args.get("failure"), args.get("question"), args.get("blocker"), *(args.get("review_position") or [])]
    rule = payload_violation(args.get("acceptance_check")) or next((r for r in map(payload_violation, texts) if r), None)
    if rule:
        if payload_violation(args.get("acceptance_check")):
            return _continue(None, f"ineligible_payload:{rule}", note=f"not counted: {rule}")
        args = dict(args, approach="", failure="", question="", blocker="", review_position=None)
    out = _attempt(directory, args, session_id, at, auth_path, auth_root)
    if rule:
        out["note"] = f"not counted: {rule}"
    elif (args.get("approach") or args.get("failure")) and _uncounted(args):
        out["note"] = "this attempt was not counted: queue, CI-availability, permission or outage trouble cannot trigger a consult"
    return out


def _isolation_gate(bid, auth_path, auth_root):
    """CONTINUE while the CLI's isolation flags are unverified (isolation.verified_on is null): no consult is offered. The one way
    forward is the owner's single deliberate `consult --id X --execute --live-verification` case."""
    auth, reason = load_authorization(auth_path, auth_root)
    if reason:
        return _continue(bid, reason)
    if auth["isolation"]["verified_on"] is None:
        return _continue(bid, "isolation_unverified", next=f"the owner or coordinator runs the one deliberate `consult --id {bid} --execute "
                                                           "--live-verification` case, inspects its verdict and sets isolation.verified_on")
    return None


def _attempt(directory, args, session_id, at, auth_path, auth_root):
    target, check = args["target"], args["acceptance_check"]
    if not TARGET_RE.fullmatch(target or "") or not _norm(check):
        return _continue(None, "target owner/repo#N and acceptance_check are required to track a blocker")
    if not SESSION_RE.fullmatch(session_id or ""):
        return _continue(None, "--session is required (letters, digits, . _ : -)")
    bid = fingerprint(target, check)
    excluded = _uncounted(args)
    with locked(directory):
        path = _attempt_path(directory, bid)
        try:
            with open(path) as fh:
                held = json.load(fh)
        except (OSError, ValueError):
            held = {"attempts": [], "positions": [], "unresolved": False}
        # Trouble that is not a reasoning failure (queue, CI availability, permissions, outage) is never kept, so it can neither
        # count toward the trigger nor linger and suppress it.
        if (args.get("approach") or args.get("failure")) and not excluded:
            held["attempts"].append({"model": args.get("model") or "", "approach": args.get("approach") or "",
                                     "failure": args.get("failure") or ""})
            held["attempts"] = held["attempts"][-8:]
        if args.get("review_resolved"):
            held["positions"], held["unresolved"] = [], False
        for pos in args.get("review_position") or []:
            if pos not in held["positions"]:
                held["positions"].append(pos)
        held["unresolved"] = held["unresolved"] or bool(args.get("review_unresolved"))
        for key in ("question", "blocker"):
            if args.get(key):
                held[key] = args[key]
        _write(path, held)
        try:
            existing = load(directory, bid)
        except EscalationError:
            existing = {"state": "UNREADABLE"} if os.path.exists(_record_path(directory, bid)) else None
        if existing is not None:
            state = existing["state"]
            if state == READY and breaker(directory).get("open"):
                return _continue(bid, "breaker_open: ready, but the circuit breaker is open: no consult")
            if state == READY:
                gate = _isolation_gate(bid, auth_path, auth_root)
                return gate or {"action": "CONSULT", "id": bid, "next": f"run `consult --id {bid} --execute` once, then implement and "
                                                                          "verify the advice here and record `verdict`"}
            return _continue(bid, f"blocker already {state}: keep working on independent tasks")
        case = {"target": target, "acceptance_check": check, "blocker": held.get("blocker") or check, "attempts": held["attempts"],
                "review": {"unresolved_architecture_disagreement": held["unresolved"], "positions": held["positions"]},
                "cause": "none", "question": held.get("question") or "",
                "inputs_non_sensitive": bool(args.get("non_sensitive")), "spent": args.get("spent") or {}}
        verdict = evaluate(case, directory)
        if verdict["trigger"] and not verdict["eligible"] and all("question" in r or "non-sensitive" in r for r in verdict["reasons"]):
            return _continue(bid, "trigger met but not ready yet: " + "; ".join(verdict["reasons"]),
                             next="re-run attempt with --question (one concrete question ending in ?) and --non-sensitive")
        if not verdict["eligible"]:
            return _continue(bid, "; ".join(verdict["reasons"])[:300])
        rule = record_payload_violation(_consult_fields(case))
        if rule:
            return _continue(bid, f"ineligible_payload:{rule}")
        _auth, reason = authorize(target, args.get("cwd") or os.getcwd(), auth_path, auth_root)
        if reason:
            return _continue(bid, reason)
        _create_ready(directory, case, session_id, at, verdict)
        gate = _isolation_gate(bid, auth_path, auth_root)
        if gate:
            return gate
    return {"action": "CONSULT", "id": bid, "next": f"run `consult --id {bid} --execute` once, then implement and verify the advice here "
                                                    "and record `verdict`; never ask the owner"}


# --- consult -----------------------------------------------------------------------------------------------------------

def claude_bin():
    return os.environ.get("FABLE_CLAUDE_BIN") or "claude"


def claude_version():
    out = subprocess.run([claude_bin(), "--version"], capture_output=True, text=True, timeout=30).stdout
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in found.groups()) if found else None


def consult_prompt(record):
    steps = "\n".join(f"- {a['model']}: tried {a['approach']!r}; failed with {a['failure']!r}" for a in record["attempts"])
    review = ""
    if record["review"].get("unresolved_architecture_disagreement"):
        review = "\nUnresolved review positions:\n" + "\n".join(f"- {str(p)[:300]}" for p in record["review"].get("positions", [])[:4])
    return (f"Blocker: {record['blocker']}\nAcceptance check: {record['acceptance_check']}\nAttempts so far:\n{steps}{review}\n\n"
            f"Question: {record['question']}\n\nYou advise only; you cannot edit. Use the facts above plus read-only repo lookups. "
            "Read only files in this repository; do not open .env*, credentials, ~/.ssh, ~/.config, ~/.claude. "
            "Return: conclusion, the 3 strongest reasons, what would falsify it, what you could not verify.")


def consult_argv(record, per_consult=PER_CONSULT_CEILING, live=False):
    # Whether these flags are honoured in -p mode is UNVERIFIED (docs/fable/escalation.md): they are the controls, the export cwd only
    # shrinks what is within reach. The live-verification case uses stream-json (which needs --verbose) so the tool-use list is saved.
    fmt = ["--output-format", "stream-json", "--verbose"] if live else ["--output-format", "json"]
    return [claude_bin(), "-p", "--model", FABLE_MODEL, "--tools", "Read,Grep,Glob", "--disallowedTools", DISALLOWED_READS,
            "--permission-mode", "dontAsk", "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence",
            "--max-budget-usd", client_cap(per_consult), *fmt]


def parse_stream(stdout):
    """(result event, all events) of a `--output-format stream-json` run. ValueError when it has no result event."""
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "result"]
    if not results:
        raise ValueError("no result event in the stream")
    return results[-1], [e for e in events if isinstance(e, dict)]


def save_stream(directory, blocker_id, stdout):
    folder = os.path.join(directory, "verification")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, blocker_id + ".jsonl")
    _write(path, stdout, text=True)
    return path


ALLOWED_TOOLS = ("Read", "Grep", "Glob")
PATH_KEYS = ("file_path", "path", "pattern", "glob", "notebook_path")
DENIED_PATH_RE = re.compile(r"(?i)(^|/)(\.env[^/]*|\.ssh|\.aws|\.gnupg|\.config|\.claude|\.codex)(/|$)|id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key)$")


def verification_verdict(events, export_root, saved_path):
    """What the one live-verification run actually did, from its stream-json events. `pass` needs positive evidence on every line;
    memory/CLAUDE.md loading that the stream does not show stays `unknown`, which is not a pass."""
    root = os.path.realpath(export_root)
    uses, outside, denied, other = [], [], [], []
    memory = "unknown"
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "init":
            keyed = {k: v for k, v in e.items() if "memory" in k.lower() or "claude_md" in k.lower()}
            if keyed:
                memory = any(bool(v) for v in keyed.values())
        for block in (e.get("message") or {}).get("content") or [] if e.get("type") == "assistant" else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, given = str(block.get("name")), block.get("input") if isinstance(block.get("input"), dict) else {}
            uses.append({"tool": name, "input": {k: str(given[k])[:200] for k in PATH_KEYS if k in given}})
            if name not in ALLOWED_TOOLS:
                other.append(name)
            for key in PATH_KEYS:
                value = given.get(key)
                if not isinstance(value, str):
                    continue
                if DENIED_PATH_RE.search(value):
                    denied.append(value[:200])
                expanded = os.path.expanduser(value)
                if ".." in value.split("/") or (os.path.isabs(expanded) and os.path.commonpath([root, os.path.realpath(expanded)]) != root):
                    outside.append(value[:200])
    return {"tool_uses": uses, "non_allowed_tools": other, "outside_export": outside, "denied_path_access": denied,
            "memory_or_claude_md_loaded": memory, "saved_stream": saved_path,
            "pass": bool(not other and not outside and not denied and memory is False)}


def check_route(result):
    """Problems with a consult result. Empty means: no error, Fable and only Fable answered, advice present."""
    problems = []
    if result.get("is_error"):
        problems.append("is_error")
    models = set((result.get("modelUsage") or {}).keys())
    if models != {FABLE_MODEL}:
        problems.append(f"effective model is not exactly {FABLE_MODEL}: {','.join(sorted(models)) or 'none'}")
    if not isinstance(result.get("result"), str) or not result["result"].strip():
        problems.append("empty advice")
    return problems


def _refused(blocker_id, reason):
    return _continue(blocker_id, reason)


def _reported_cost(result):
    cost = result.get("total_cost_usd") if isinstance(result, dict) else None
    ok = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
    return float(cost) if ok else None


def consult(directory, blocker_id, execute, cwd, at, auth_path=None, auth_root=None, live_verification=False):
    with locked(directory):
        record = load(directory, blocker_id)
        if record["state"] != READY:
            raise EscalationError(f"blocker is {record['state']}: a consult runs only from READY, once")
        if breaker(directory).get("open"):
            return _refused(blocker_id, "breaker_open: " + str(breaker(directory).get("reason")))
        auth, reason = authorize(record["target"], cwd, auth_path, auth_root)
        if reason:
            return _refused(blocker_id, reason)
        if any(os.environ.get(name) for name in API_ENV):
            return _refused(blocker_id, "api_env_present")
        unverified = auth["isolation"]["verified_on"] is None
        if unverified and not live_verification:
            return _refused(blocker_id, "isolation_unverified")
        if live_verification and not unverified:
            return _refused(blocker_id, "live_verification_not_needed")
        prompt = consult_prompt(record)
        rule = record_payload_violation(record)
        if rule:
            return _refused(blocker_id, f"ineligible_payload:{rule}")
        # A hand-forged READY record must not skip the trigger: re-evaluate it from the stored facts (no state directory: that is this record).
        again = evaluate({"target": record["target"], "acceptance_check": record["acceptance_check"], "blocker": record["blocker"],
                          "attempts": record["attempts"], "review": record["review"], "cause": "none", "question": record["question"],
                          "inputs_non_sensitive": True}, None)
        if not again["eligible"]:
            return _refused(blocker_id, "trigger_not_met")
        per = auth["caps"]["per_consult_usd"]
        argv = consult_argv(record, per, live_verification)
        if not execute:
            return {"dry_run": True, "id": record["id"], "model": FABLE_MODEL, "argv": argv, "prompt_via": "stdin", "reserve_usd": per}
        try:
            version = claude_version()
        except (subprocess.TimeoutExpired, OSError):
            version = None
        if version is None or version < MIN_CLAUDE:
            record["state"] = FAILED
            record["failure"] = "claude version unknown or below 2.1.321"
            save(directory, record)
            trip(directory, "route_failure", record["id"], at)
            raise EscalationError(record["failure"])
        export = tempfile.TemporaryDirectory(prefix="fable-export-")
        os.chmod(export.name, 0o700)
        exported, content_hits = export_tracked(cwd, export.name)
        if not exported:
            export.cleanup()
            return _refused(blocker_id, "isolation_export_failed")
        if any(name in prompt or os.path.basename(name) in prompt for name in content_hits):
            export.cleanup()  # the prompt points at a file that holds a secret: the consult cannot be made safe
            return _refused(blocker_id, "isolation_secret_in_referenced_file")
        try:
            rid, month = ledger_reserve(auth, record["id"], record.get("session_id") or "unknown", at)
        except Refusal as exc:
            export.cleanup()
            return _refused(blocker_id, exc.reason)  # quota state, not a route failure: the breaker stays closed
        except LedgerError:
            export.cleanup()
            return _refused(blocker_id, "ledger_unreadable")
        # Persist CONSULTING after the reservation and before the call: a crash or kill leaves both the consult and its $ spent.
        record["state"], record["consult_started"], record["reservation"] = CONSULTING, iso(at), {"rid": rid, "month": month}
        save(directory, record)
        receipt(directory, "consult_started", record, rid=rid, export_content_skipped=len(content_hits))
    cost, problems, verification = None, [], None
    try:
        # The prompt goes on stdin so attempt text never appears in a process listing.
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=export.name, timeout=CONSULT_TIMEOUT_S)
        if live_verification:
            result, events = parse_stream(proc.stdout)
            verification = verification_verdict(events, export.name, save_stream(directory, record["id"], proc.stdout))
        else:
            result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("result is not a JSON object")
        cost = _reported_cost(result)
        problems = ([] if proc.returncode == 0 else [f"exit {proc.returncode}"]) + check_route(result)
    except Exception as exc:  # noqa: BLE001 - every non-success exit must trip the breaker, whatever raised
        result, problems = {}, [type(exc).__name__]
    finally:
        export.cleanup()
    # A KeyboardInterrupt/SystemExit is not caught above: the entry then stays RESERVED at the full amount, never auto-released.
    settled_at = now_utc()
    if cost is None:
        charge, basis = per, "full_reservation_no_reported_cost"
    else:
        charge, basis = cost, "reported_total_cost_usd"
    overrun = charge > per
    if overrun:
        basis += "+cap_overrun"
    with locked(directory):
        record = load(directory, blocker_id)
        try:
            ledger_settle(auth, month, rid, charge, basis, settled_at)
        except LedgerError:
            receipt(directory, "settle_failed", record, rid=rid)  # the entry stays RESERVED at the full amount
        if problems:
            record["state"], record["failure"] = FAILED, "; ".join(problems)[:300]
            save(directory, record)
            receipt(directory, "consult_failed", record, problems=problems, charged_usd=charge)
            if overrun:
                reason_code = "cap_overrun"
            elif any("effective model" in p for p in problems):
                reason_code = "model_mismatch"
            else:
                reason_code = "route_failure"
            trip(directory, reason_code, record["id"], settled_at)
            raise EscalationError("consult failed, breaker opened: " + record["failure"])
        advice = os.path.join(directory, "advice", record["id"] + ".txt")
        _write(advice, result["result"], text=True)
        record.update(state=CONSULTED, advice_path=advice, consult_usd=cost, consult_ms=result.get("duration_ms"), consulted=iso(settled_at))
        save(directory, record)
        receipt(directory, "consulted", record, usd=cost)
        if overrun:  # the advice is kept, but a call that ran past its reservation stops further consults until the owner resets
            trip(directory, "cap_overrun", record["id"], settled_at)
    out = {"id": record["id"], "advice_path": advice, "usd_list_estimate": cost, "duration_ms": record["consult_ms"],
           "next": "implement and verify in the original session, then run `verdict`"}
    if verification is not None:
        out["verification"] = verification
        out["next"] = ("read the verification verdict and the saved stream; only if it passes, the owner sets isolation.verified_on in "
                       "the authorization file")
    return out


# --- verdict / report --------------------------------------------------------------------------------------------------

def verdict(directory, blocker_id, result, totals, at):
    if result not in ("advanced", "not_advanced"):
        raise EscalationError("result must be advanced or not_advanced")
    with locked(directory):
        record = load(directory, blocker_id)
        if record["state"] != CONSULTED:
            raise EscalationError(f"blocker is {record['state']}: a verdict follows a consult")
        record.update(state=CLOSED, verdict=result, closed=iso(at), totals=totals)
        save(directory, record)
        receipt(directory, "verdict", record, result=result)
        if result == "not_advanced":
            trip(directory, "no_advance", record["id"], at)
    return record


def report(directory, budget=None):
    rows = []
    for name in sorted(os.listdir(directory)):
        if ID_RE.fullmatch(name[:-5] or "") and name.endswith(".json"):
            with open(os.path.join(directory, name)) as fh:
                rows.append(json.load(fh))
    done = [r for r in rows if r["state"] == CLOSED]
    lines = {"blockers": len(rows), "by_state": {s: sum(1 for r in rows if r["state"] == s) for s in sorted({r["state"] for r in rows})},
             "breaker": breaker(directory), "closed": [], "month_budget": budget or {"unavailable": "no readable authorization"}}
    for r in done:
        before, after = r.get("spent") or {}, r.get("totals") or {}
        lines["closed"].append({"id": r["id"], "verdict": r["verdict"], "consult_usd": r.get("consult_usd"),
                                "spent_at_offer": before, "total_at_acceptance": after})
    lines["note"] = ("no control arm: a closed blocker shows spend before the consult and total at acceptance, not what Opus alone would "
                     "have needed; do not claim a reduction without a comparable Opus-only baseline")
    return lines


# --- CLI ---------------------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("estimate", help="list-price estimate of one consult for a case; never invokes, never reads or writes state")
    s.add_argument("--case", required=True, help="JSON file describing the blocker")
    s = sub.add_parser("evaluate")
    s.add_argument("--case", required=True, help="JSON file describing the blocker")
    s = sub.add_parser("consult")
    s.add_argument("--id", required=True)
    s.add_argument("--execute", action="store_true", help="without it: print the command shape only")
    s.add_argument("--live-verification", action="store_true",
                   help="the one deliberate case that verifies the CLI isolation flags: saves the stream-json tool-use list and prints a verdict")
    s.add_argument("--cwd", default=os.getcwd())
    s = sub.add_parser("verdict")
    s.add_argument("--id", required=True)
    s.add_argument("--result", required=True, choices=("advanced", "not_advanced"))
    s.add_argument("--rework-rounds", type=int)
    s.add_argument("--elapsed-s", type=int)
    s.add_argument("--usd", type=float)
    s = sub.add_parser("attempt", help="record one failed attempt; prints the one action to take")
    s.add_argument("--target", required=True)
    s.add_argument("--check", required=True, dest="acceptance_check")
    s.add_argument("--session", required=True)
    s.add_argument("--model", default="")
    s.add_argument("--approach", default="")
    s.add_argument("--failure", default="")
    s.add_argument("--cause", choices=sorted(CAUSES), default=None)
    s.add_argument("--question", default="")
    s.add_argument("--blocker", default="")
    s.add_argument("--non-sensitive", action="store_true", help="attest the inputs hold no secret, customer or private data")
    s.add_argument("--review-position", action="append")
    s.add_argument("--review-unresolved", action="store_true")
    s.add_argument("--review-resolved", action="store_true", help="clear a review disagreement that has been settled")
    s.add_argument("--cwd", default=os.getcwd(), help="the working tree the consult would read; its origin owner must be authorized")
    sub.add_parser("report")
    s = sub.add_parser("reset-breaker")
    s.add_argument("--note", required=True)
    s = sub.add_parser("authorization", help="read-only: print a candidate authorization file or check the installed one")
    s.add_argument("what", choices=("template", "check"))
    s.add_argument("--cwd", default=os.getcwd())
    s.add_argument("--target", default="", help="check: also verify this owner/repo#N is in scope")
    args = p.parse_args(argv)
    if args.cmd == "estimate":  # before state_dir(): an estimate never creates or reads the state directory
        try:
            with open(args.case) as fh:
                out = estimate_case(json.load(fh))
        except (EscalationError, OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    if args.cmd == "authorization":  # read-only: no state directory, never writes the authorization file
        out = authorization_template(args.cwd) if args.what == "template" else authorization_check(args.cwd, args.target, now_utc())
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    directory, at = state_dir(), now_utc()
    try:
        if args.cmd == "evaluate":
            with open(args.case) as fh:
                case = json.load(fh)
            out = evaluate(case, directory)
        elif args.cmd == "consult":
            out = consult(directory, args.id, args.execute, args.cwd, at, live_verification=args.live_verification)
        elif args.cmd == "verdict":
            out = verdict(directory, args.id, args.result,
                          {"rework_rounds": args.rework_rounds, "elapsed_s": args.elapsed_s, "usd": args.usd}, at)
        elif args.cmd == "attempt":
            out = attempt(directory, vars(args), args.session, at)
        elif args.cmd == "report":
            auth, _reason = load_authorization()
            budget = None
            if auth:
                try:
                    budget = month_budget(auth, at)
                except LedgerError:
                    budget = {"error": "ledger_unreadable"}
            out = report(directory, budget)
        else:
            reset_breaker(directory, args.note, at)
            out = {"breaker": breaker(directory)}
    except (EscalationError, OSError, ValueError) as exc:
        failure = {"error": str(exc)}
        if args.cmd == "consult":
            failure["fallback"] = FALLBACK  # a failed consult never blocks the work: continue on the baseline
        print(json.dumps(failure))
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if args.cmd != "evaluate" or out["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
