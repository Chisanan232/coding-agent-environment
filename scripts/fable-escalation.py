#!/usr/bin/env python3
"""Fable escalation (SPE-171): offer one read-only Fable consult per blocker, only after the owner approves it.

The tool decides *eligibility*, keeps *one offer and one consult per blocker*, and trips a *circuit breaker* when the route
fails or the advice does not advance the task. It cannot obtain or verify the owner's approval itself: the agent asks the owner
(AskUserQuestion) and records the answer here, so the recorded approval is the agent's own attestation of the owner's native
answer, and the ref is stored as a hash only. A consult runs only from a recorded approval. No scheduler, hook or background lane
calls `consult`; unattended Fable calls are not enabled. A local script cannot stop a session from recording an approval nobody
gave; the receipts log and the pending-ASK ledger are the audit trail.

Route: the included Claude subscription only. The consult refuses to run when an API credential or alternate-provider variable is set in the
environment and refuses a result whose `modelUsage` is not exactly `claude-fable-5-2`. Usage credits stay off (owner setting).

Stdlib only. State: `$FABLE_ESCALATION_DIR` (default `~/.cache/coding-agent-environment/fable`), mode 0700/0600, one flock
around every read-modify-write. Offers are also recorded in the pending-ASK ledger (scripts/ask-pending.py) so sessions that
share it see one open request per blocker.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location

FABLE_MODEL = "claude-fable-5-2"
MIN_CLAUDE = (2, 1, 321)  # Fable 5.2 needs this Claude Code version or later (runbook)
APPROVAL_TTL_S = 24 * 3600  # an approval older than this is stale and must be asked again
CONSULT_TIMEOUT_S = 900
CONSULT_BUDGET_USD = "4"  # client-side list-price cap per consult (runbook), not billed money
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

OFFERED, APPROVED, DECLINED, CONSULTING, CONSULTED, FAILED, CLOSED = (
    "OFFERED", "APPROVED", "DECLINED", "CONSULTING", "CONSULTED", "FAILED", "CLOSED")


class EscalationError(Exception):
    pass


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

def breaker(directory):
    try:
        with open(os.path.join(directory, "breaker.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"open": False}


def trip(directory, reason, blocker_id, at):
    if breaker(directory).get("open"):
        return breaker(directory)  # keep the first reason
    state = {"open": True, "reason": reason, "blocker": blocker_id, "since": iso(at)}
    _write(os.path.join(directory, "breaker.json"), state)
    receipt(directory, "breaker_open", {"id": blocker_id, "state": None}, reason=reason)
    return state


def reset_breaker(directory, note, at):
    """Owner action: nothing closes the breaker automatically."""
    if not note or len(note.strip()) < 8:
        raise EscalationError("reset needs --note: why the route or the advice problem is resolved")
    with locked(directory):
        _write(os.path.join(directory, "breaker.json"), {"open": False, "reset_at": iso(at), "note": note.strip()[:200]})
        receipt(directory, "breaker_reset", {"id": None, "state": None}, note=note.strip()[:200])


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
        reasons.append(f"blocker already {existing['state']}: one offer and one consult per blocker")
    return {"eligible": not reasons, "trigger": trigger, "reasons": reasons, "id": bid,
            "on_ineligible": "continue independent work" if reasons else None}


# --- offer / answer ----------------------------------------------------------------------------------------------------

def default_ask_dir():
    # The pending-ASK ledger the rest of the workflow reads; an unset override must not silently skip the record.
    return os.environ.get("ASK_PENDING_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "coding-agent-environment", "asks")


def _ask_pending():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = spec_from_file_location("ask_pending", os.path.join(here, "ask-pending.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_offer(directory, case, session_id, at, verdict):
    """Write the OFFERED record. The caller holds the lock and has just evaluated `case` as eligible."""
    attempts = [{"model": str(a.get("model"))[:40], "approach": str(a.get("approach"))[:200], "failure": str(a.get("failure"))[:200]}
                for a in case["attempts"]][:8]
    record = {"id": verdict["id"], "state": OFFERED, "created": iso(at), "session_id": session_id, "trigger": verdict["trigger"],
              "target": case["target"], "acceptance_check": str(case["acceptance_check"])[:300],
              "blocker": str(case.get("blocker") or case["acceptance_check"])[:300], "attempts": attempts,
              "question": case["question"].strip(), "review": case.get("review") or {},
              "spent": {k: case.get("spent", {}).get(k) for k in ("rework_rounds", "elapsed_s", "usd")}}
    save(directory, record)
    receipt(directory, "offered", record, trigger=verdict["trigger"])
    return record


def packet_for(record, ledger=None):
    return {"id": record["id"], "trigger": record["trigger"], "blocker": record["blocker"], "attempts_made": record["attempts"],
            "question_for_fable": record["question"], "pending_ledger_item": ledger,
            "route": "included Claude subscription only; usage credits stay off; one read-only consult; advice returns to this session",
            "ask": "Approve one read-only Fable consult for this blocker? (Approve / Decline). No reply: independent work continues."}


def _ledger_packet(record, session_id, ask_dir, at):
    """Record the offer in the pending-ASK ledger (visibility only) and return the packet. Runs outside the lock."""
    ledger = None
    if ask_dir:
        try:
            os.makedirs(ask_dir, mode=0o700, exist_ok=True)
            ap = _ask_pending()
            item, _new = ap.record_decision(ask_dir, "Fable consult: " + record["blocker"][:150], record["target"], session_id, [],
                                            "owner_approval", record["id"], ap.GRACE_MAX_S, "unknown", at)
            ledger = item["id"]
        except Exception as exc:  # noqa: BLE001 - the ledger is visibility only; the offer already stands and must still reach the owner
            ledger = "not-recorded: " + str(exc)[:120]
    return packet_for(record, ledger)


def offer(directory, case, session_id, ask_dir, at):
    if not SESSION_RE.fullmatch(session_id or ""):
        raise EscalationError("--session is required")
    with locked(directory):
        verdict = evaluate(case, directory)
        if not verdict["eligible"]:
            raise EscalationError("not eligible: " + "; ".join(verdict["reasons"]))
        record = _create_offer(directory, case, session_id, at, verdict)
    return _ledger_packet(record, session_id, ask_dir, at)


def show(directory, blocker_id):
    """Re-display the packet of an OFFERED blocker whose first display was lost (a crash after the record was written).
    It never asks anything itself and never changes state."""
    record = load(directory, blocker_id)
    if record["state"] != OFFERED:
        raise EscalationError(f"blocker is {record['state']}: only an OFFERED blocker has an open packet")
    return packet_for(record)


def answer(directory, blocker_id, decision, ref, session_id, ask_dir, at):
    if decision not in ("approve", "decline"):
        raise EscalationError("decision must be approve or decline")
    if not ref or len(ref.strip()) < 8:
        raise EscalationError("--ref is required: the owner's answer as given (quote or message reference)")
    with locked(directory):
        record = load(directory, blocker_id)
        if record["state"] != OFFERED:
            raise EscalationError(f"blocker is {record['state']}: only an OFFERED blocker takes an answer")
        record["state"] = APPROVED if decision == "approve" else DECLINED
        record["answered"] = iso(at)
        record["answer_ref"] = hashlib.sha256(ref.encode()).hexdigest()[:16]  # the text itself may hold anything
        save(directory, record)
        receipt(directory, "answered", record, decision=decision)
    if ask_dir:
        try:
            os.makedirs(ask_dir, mode=0o700, exist_ok=True)
            ap = _ask_pending()
            ap.finish(ask_dir, ap.item_key("DECISION: Fable consult: " + record["blocker"][:150], record["target"], "decision"),
                      "answered" if decision == "approve" else "denied", ref if decision == "approve" else None, at,
                      session_id=session_id)
        except Exception:  # noqa: BLE001 - the answer is already saved; a ledger that cannot be written must not make it look failed
            pass  # the ledger is visibility only; this record is the authority for the consult
    return record


# --- workflow entry: record an attempt, get exactly one action -----------------------------------------------------------

def _attempt_path(directory, blocker_id):
    os.makedirs(os.path.join(directory, "attempts"), mode=0o700, exist_ok=True)
    return os.path.join(directory, "attempts", blocker_id + ".json")


def _uncounted(args):
    return bool(args.get("cause") and args["cause"] != "none") or bool(EXCLUDED_TEXT.search(args.get("failure") or ""))


def attempt(directory, args, session_id, ask_dir, at):
    out = _attempt(directory, args, session_id, ask_dir, at)
    if (args.get("approach") or args.get("failure")) and _uncounted(args):
        out["note"] = "this attempt was not counted: queue, CI-availability, permission or outage trouble cannot trigger an offer"
    return out


def _attempt(directory, args, session_id, ask_dir, at):
    """What the development loop calls after every failed attempt. Never blocks and never raises for a normal outcome.

    Returns {"action": ...}: CONTINUE (do nothing, keep working), ASK_OWNER (ask once now, with `packet`), CONSULT_APPROVED (the owner
    approved earlier: run `consult`). ASK_OWNER comes back once per blocker; every later call for it says CONTINUE. Evaluate and offer
    happen under one lock, so concurrent calls cannot both ask and none of them errors."""
    target, check = args["target"], args["acceptance_check"]
    if not TARGET_RE.fullmatch(target or "") or not _norm(check):
        return {"action": "CONTINUE", "reason": "target owner/repo#N and acceptance_check are required to track a blocker"}
    if not SESSION_RE.fullmatch(session_id or ""):
        return {"action": "CONTINUE", "reason": "--session is required (letters, digits, . _ : -)"}
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
            if state == APPROVED and breaker(directory).get("open"):
                return {"action": "CONTINUE", "id": bid, "reason": "approved, but the circuit breaker is open: no consult, no prompt"}
            if state == APPROVED:
                try:
                    age = (at - parse(existing["answered"])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    return {"action": "CONTINUE", "id": bid, "reason": "approval record is unreadable: no consult and no new prompt"}
                if age > APPROVAL_TTL_S:
                    return {"action": "CONTINUE", "id": bid,
                            "reason": "approval is older than 24h: no consult and no new prompt for this blocker; continue without Fable"}
            if state == APPROVED:
                return {"action": "CONSULT_APPROVED", "id": bid, "next": "run `consult --id` once, then implement and verify"}
            return {"action": "CONTINUE", "id": bid, "reason": f"blocker already {state}: no new prompt, keep working on independent tasks"}
        case = {"target": target, "acceptance_check": check, "blocker": held.get("blocker") or check, "attempts": held["attempts"],
                "review": {"unresolved_architecture_disagreement": held["unresolved"], "positions": held["positions"]},
                "cause": "none", "question": held.get("question") or "",
                "inputs_non_sensitive": bool(args.get("non_sensitive")), "spent": args.get("spent") or {}}
        verdict = evaluate(case, directory)
        if verdict["trigger"] and not verdict["eligible"] and all("question" in r or "non-sensitive" in r for r in verdict["reasons"]):
            return {"action": "CONTINUE", "id": bid, "reason": "trigger met but not offerable yet: " + "; ".join(verdict["reasons"]),
                    "next": "re-run attempt with --question (one concrete question ending in ?) and --non-sensitive"}
        if not verdict["eligible"]:
            return {"action": "CONTINUE", "id": bid, "reason": "; ".join(verdict["reasons"])[:300]}
        record = _create_offer(directory, case, session_id, at, verdict)
    return {"action": "ASK_OWNER", "id": bid, "packet": _ledger_packet(record, session_id, ask_dir, at),
            "how": "ask the owner once with AskUserQuestion (Approve / Decline); record with `answer`; on no reply keep working, never "
                   "re-ask; if this display is lost, `show --id` prints the packet again without asking"}


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
            "Return: conclusion, the 3 strongest reasons, what would falsify it, what you could not verify.")


def consult_argv(record):
    return [claude_bin(), "-p", "--model", FABLE_MODEL, "--tools", "Read,Grep,Glob", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence",
            "--max-budget-usd", CONSULT_BUDGET_USD, "--output-format", "json"]


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


def consult(directory, blocker_id, execute, cwd, at):
    with locked(directory):
        record = load(directory, blocker_id)
        if breaker(directory).get("open"):
            raise EscalationError("circuit breaker open: " + str(breaker(directory).get("reason")))
        if record["state"] != APPROVED:
            raise EscalationError(f"blocker is {record['state']}: a consult runs only from a recorded approval, once")
        try:
            approval_age = (at - parse(record["answered"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise EscalationError("approval record is unreadable: no consult")
        if approval_age > APPROVAL_TTL_S:
            raise EscalationError("approval is older than 24h: offer again after revalidating the blocker")
        if any(os.environ.get(name) for name in API_ENV):
            raise EscalationError("an API credential is present in the environment: refusing, the route is the subscription only")
        argv = consult_argv(record)
        if not execute:
            return {"dry_run": True, "id": record["id"], "model": FABLE_MODEL, "argv": argv, "prompt_via": "stdin"}
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
        # Persist CONSULTING before the call: a crash or kill still leaves the one consult spent.
        record["state"], record["consult_started"] = CONSULTING, iso(at)
        save(directory, record)
        receipt(directory, "consult_started", record)
    try:
        # The prompt goes on stdin so attempt text never appears in a process listing.
        proc = subprocess.run(argv, input=consult_prompt(record), capture_output=True, text=True, cwd=cwd, timeout=CONSULT_TIMEOUT_S)
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("result is not a JSON object")
        problems = ([] if proc.returncode == 0 else [f"exit {proc.returncode}"]) + check_route(result)
    except Exception as exc:  # noqa: BLE001 - every non-success exit must trip the breaker, whatever raised
        result, problems = {}, [type(exc).__name__]
    with locked(directory):
        record = load(directory, blocker_id)
        if problems:
            record["state"], record["failure"] = FAILED, "; ".join(problems)[:300]
            save(directory, record)
            receipt(directory, "consult_failed", record, problems=problems)
            trip(directory, "model_mismatch" if any("effective model" in p for p in problems) else "route_failure", record["id"], now_utc())
            raise EscalationError("consult failed, breaker opened: " + record["failure"])
        advice = os.path.join(directory, "advice", record["id"] + ".txt")
        _write(advice, result["result"], text=True)
        record.update(state=CONSULTED, advice_path=advice, consult_usd=result.get("total_cost_usd"),
                      consult_ms=result.get("duration_ms"), consulted=iso(now_utc()))
        save(directory, record)
        receipt(directory, "consulted", record, usd=record["consult_usd"])
    return {"id": record["id"], "advice_path": advice, "usd_list_estimate": record["consult_usd"], "duration_ms": record["consult_ms"],
            "next": "implement and verify in the original session, then run `verdict`"}


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


def report(directory):
    rows = []
    for name in sorted(os.listdir(directory)):
        if ID_RE.fullmatch(name[:-5] or "") and name.endswith(".json"):
            with open(os.path.join(directory, name)) as fh:
                rows.append(json.load(fh))
    done = [r for r in rows if r["state"] == CLOSED]
    lines = {"blockers": len(rows), "by_state": {s: sum(1 for r in rows if r["state"] == s) for s in sorted({r["state"] for r in rows})},
             "breaker": breaker(directory), "closed": []}
    for r in done:
        before, after = r.get("spent") or {}, r.get("totals") or {}
        lines["closed"].append({"id": r["id"], "verdict": r["verdict"], "consult_usd": r.get("consult_usd"),
                                "spent_at_offer": before, "total_at_acceptance": after})
    lines["note"] = ("no control arm: a closed blocker shows spend before the offer and total at acceptance, not what Opus alone would "
                     "have needed; do not claim a reduction without a comparable Opus-only baseline")
    return lines


# --- CLI ---------------------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("evaluate", "offer"):
        s = sub.add_parser(name)
        s.add_argument("--case", required=True, help="JSON file describing the blocker")
        if name == "offer":
            s.add_argument("--session", required=True)
            s.add_argument("--ask-dir", default=default_ask_dir())
    s = sub.add_parser("answer")
    s.add_argument("--id", required=True)
    s.add_argument("--decision", required=True, choices=("approve", "decline"))
    s.add_argument("--ref", required=True)
    s.add_argument("--session", required=True)
    s.add_argument("--ask-dir", default=default_ask_dir())
    s = sub.add_parser("consult")
    s.add_argument("--id", required=True)
    s.add_argument("--execute", action="store_true", help="without it: print the command shape only")
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
    s.add_argument("--ask-dir", default=default_ask_dir())
    s = sub.add_parser("show", help="print the packet of an OFFERED blocker again (never asks, never changes state)")
    s.add_argument("--id", required=True)
    sub.add_parser("report")
    s = sub.add_parser("reset-breaker")
    s.add_argument("--note", required=True)
    args = p.parse_args(argv)
    directory, at = state_dir(), now_utc()
    try:
        if args.cmd in ("evaluate", "offer"):
            with open(args.case) as fh:
                case = json.load(fh)
        if args.cmd == "evaluate":
            out = evaluate(case, directory)
        elif args.cmd == "offer":
            out = offer(directory, case, args.session, args.ask_dir, at)
        elif args.cmd == "answer":
            out = answer(directory, args.id, args.decision, args.ref, args.session, args.ask_dir, at)
        elif args.cmd == "consult":
            out = consult(directory, args.id, args.execute, args.cwd, at)
        elif args.cmd == "verdict":
            out = verdict(directory, args.id, args.result,
                          {"rework_rounds": args.rework_rounds, "elapsed_s": args.elapsed_s, "usd": args.usd}, at)
        elif args.cmd == "attempt":
            out = attempt(directory, vars(args), args.session, args.ask_dir, at)
        elif args.cmd == "show":
            out = show(directory, args.id)
        elif args.cmd == "report":
            out = report(directory)
        else:
            reset_breaker(directory, args.note, at)
            out = {"breaker": breaker(directory)}
    except (EscalationError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if args.cmd != "evaluate" or out["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
