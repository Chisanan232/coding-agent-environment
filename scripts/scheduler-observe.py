#!/usr/bin/env python3
"""Read-only scheduler/supervisor observer that reports only material change (SPE-188).

A scheduled check used to restate an unchanged picture on every run. This tool reduces what the check reads to a
fingerprint of MATERIAL state, compares it with the previous fingerprint and prints exactly one line:

  NO_CHANGE            nothing material changed since the last observation (or a failure was already reported)
  QUIET                routine churn (claims, releases, cancels, an unconfirmed first error, the first baseline)
  CHANGE: <delta>      a new failure, a recovery or a decision-relevant fact; the caller should tell the owner once

Material state: live claimed ids, backfill and cap-refusal counts, the monitor's mode and incident ids, the supervisor's telegram
status and open incident refs, provider-maintenance presence and the open scheduler PR numbers. Volatile counters (poll batches,
timestamps, free-space jitter) never enter the fingerprint. The tool only reads: the scheduler queue, the monitor status and the
supervisor state are opened read-only; the files it writes are its own fingerprint and a small failure counter (both 0600, atomic).
The only network use is the default PR provider, a read-only `gh pr list`; `--no-prs` (or SCHEDULER_OBSERVE_NO_PRS=1) turns it off.
Output is a closed character set: no paths and no shell metacharacters. Exit code is always 0.

A broken observer must not look like a quiet one: an internal failure (unwritable state, an unexpected exception) is QUIET the first
time, `CHANGE: observer failing (<class>)` the second consecutive time, and `CHANGE: observer recovered` once when it works again. When
even the failure counter cannot be stored in the private observer directory (it never falls back to a shared temp directory) the failure is reported on every run, because it cannot be deduplicated.

The harness goal evaluator (the `Stop hook feedback` and `Goal check-in` notices) is not part of this tool and cannot be changed by it.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

HOME = Path.home()
DEFAULT_STATE = HOME / ".cache/coding-agent-environment/observer/last.json"
DEFAULT_QUEUE = HOME / ".cache/coding-agent-environment/admission/queue"
DEFAULT_MONITOR = HOME / ".cache/coding-agent-environment/admission-monitor/status.json"
DEFAULT_SUPERVISOR = HOME / ".local/share/engineering-supervisor/state"
DEFAULT_REPO = "Chisanan232/coding-agent-environment"
SCHEMA = 1
MAX_JSON_BYTES = 4 << 20
DEFAULT_TAIL_BYTES = 8 << 20
LIVE_END = ("released", "expired", "cancelled")
SAFE = re.compile(r"[^A-Za-z0-9 :,.;_#>()+-]")


FAILURE_NAME = "observer-failures.json"
MAX_FAILURE_BYTES = 4096
_MEMORY_FAILURE = {}                                       # last resort when no private directory is usable; never a shared temp path


def clean(text, limit=160):
    return SAFE.sub("", str(text))[:limit]


def read_json(path, own=False, limit=None):
    """Read a small JSON file. Never blocks on a FIFO and only reads regular files. `own` is for the observer's own files: no symlink is
    followed, the file must belong to this user and be private (no group or other access)."""
    limit = MAX_JSON_BYTES if limit is None else limit
    flags = os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if own else 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if own and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("not a private file of this user")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(raw) > limit:
        raise ValueError("file too large")
    return json.loads(raw.decode("utf-8"))


def ensure_private_dir(path):
    """Create `path` as a 0700 directory owned by this user, or tighten an existing one that is ours; refuse a symlink or a foreign directory."""
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("not a directory of this user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def read_tail_lines(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            handle.seek(start)
            data = handle.read()
    finally:
        os.close(descriptor)
    lines = data.split(b"\n")
    return lines[1:] if start > 0 else lines                # the first line of a tail read may be cut


def observe_queue(queue_dir, tail_bytes=DEFAULT_TAIL_BYTES):
    claimed, backfill_ids, backfill_events, cap_refusals, floor_grants = set(), set(), 0, 0, 0
    for raw in read_tail_lines(Path(queue_dir) / "receipts.jsonl", tail_bytes):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        event, ident = row.get("event"), row.get("id")
        if event == "claimed" and isinstance(ident, str):
            claimed.add(ident)
        elif event in LIVE_END and isinstance(ident, str):
            claimed.discard(ident)
        elif event == "backfill":
            backfill_events += 1
            if isinstance(ident, str):
                backfill_ids.add(ident)
            if row.get("reason") == "head-floor-only":
                floor_grants += 1
        elif event == "backfill-revoked":
            backfill_events += 1
        elif event == "backfill-decision" and row.get("reason") == "cumulative-loss-cap":
            cap_refusals += 1
    return {"claimed": sorted(claimed), "backfilled_claimed": sorted(claimed & backfill_ids),
            "backfill_events": backfill_events, "cap_refusals": cap_refusals, "floor_only_grants": floor_grants}


def observe_monitor(path):
    data = read_json(path)
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    acting = any(isinstance(a, dict) and (a.get("mode") == "act" or a.get("executed") is True) for a in actions)
    incidents = data.get("handoff_incidents") if isinstance(data.get("handoff_incidents"), list) else []
    open_ids = sorted(i["id"] for i in incidents if isinstance(i, dict) and isinstance(i.get("id"), str) and not i.get("resolved_at"))
    resolved = sorted(i["id"] for i in incidents if isinstance(i, dict) and isinstance(i.get("id"), str) and i.get("resolved_at"))
    return {"mode": "act" if acting else "observe", "open_handoff": open_ids, "resolved_handoff": resolved}


def observe_supervisor(state_dir):
    state_dir = Path(state_dir)
    health = read_json(state_dir / "telegram-health.json")
    status = health.get("status")
    incidents = read_json(state_dir / "incident-status.json")
    refs = sorted(i["incident_ref"] for i in incidents.get("open_incidents", [])
                  if isinstance(i, dict) and isinstance(i.get("incident_ref"), str))
    return {"telegram": status if isinstance(status, str) else "unknown", "open_incidents": refs,
            "provider_maintenance": (state_dir / "provider-maintenance.json").exists()}


def gh_prs(repo):
    out = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", "number", "--jq", ".[].number"],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return sorted(int(n) for n in out.split())


def collect(args, pr_provider):
    fingerprint, errors = {}, {}
    sources = [("queue", lambda: observe_queue(args.queue_dir, args.tail_bytes)), ("monitor", lambda: observe_monitor(args.monitor_status)),
               ("supervisor", lambda: observe_supervisor(args.supervisor_dir))]
    if not args.no_prs:
        sources.append(("prs", lambda: pr_provider(args.repo)))
    for name, call in sources:
        try:
            fingerprint[name] = call()
        except Exception as exc:  # a source that cannot be read keeps its previous value and counts toward an error report
            errors[name] = type(exc).__name__
    return fingerprint, errors


def _strs(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


VALIDATORS = {
    "queue": lambda v: isinstance(v, dict) and _strs(v.get("claimed")) and _strs(v.get("backfilled_claimed"))
    and all(_int(v.get(k)) for k in ("backfill_events", "cap_refusals", "floor_only_grants")),
    "monitor": lambda v: isinstance(v, dict) and v.get("mode") in ("observe", "act") and _strs(v.get("open_handoff"))
    and _strs(v.get("resolved_handoff")),
    "supervisor": lambda v: isinstance(v, dict) and isinstance(v.get("telegram"), str) and _strs(v.get("open_incidents"))
    and isinstance(v.get("provider_maintenance"), bool),
    "prs": lambda v: isinstance(v, list) and all(_int(n) for n in v),
}


def valid_fingerprint(fingerprint):
    return isinstance(fingerprint, dict) and all(key in VALIDATORS and VALIDATORS[key](value) for key, value in fingerprint.items())


def valid_errors(errors):
    return isinstance(errors, dict) and all(
        key in VALIDATORS and isinstance(value, dict) and isinstance(value.get("class"), str) and _int(value.get("count")) and value["count"] >= 1
        for key, value in errors.items())


def delta(old, new):
    """Return (notify, quiet) lists of short phrases describing how `new` differs from `old`."""
    notify, quiet = [], []
    oq, nq = old.get("queue"), new.get("queue")
    if oq is not None and nq is not None:
        for ident in sorted(set(nq["backfilled_claimed"]) - set(oq["backfilled_claimed"])):
            notify.append(f"live backfill claimed {ident}")
        if oq["cap_refusals"] == 0 and nq["cap_refusals"] > 0:
            notify.append("first cumulative-loss-cap refusal")
        for field, label in (("floor_only_grants", "floor-only backfill grant"), ("backfill_events", "backfill event")):
            if nq[field] > oq[field]:
                quiet.append(f"{label} count {oq[field]}->{nq[field]}")
        if nq["claimed"] != oq["claimed"]:
            quiet.append("claims changed")
    om, nm = old.get("monitor"), new.get("monitor")
    if om is not None and nm is not None:
        if om["mode"] != nm["mode"]:
            notify.append(f"monitor mode {om['mode']}->{nm['mode']}")
        if om["open_handoff"] != nm["open_handoff"] or om["resolved_handoff"] != nm["resolved_handoff"]:
            quiet.append("monitor handoff incidents changed")
    os_, ns = old.get("supervisor"), new.get("supervisor")
    if os_ is not None and ns is not None:
        if os_["telegram"] != ns["telegram"]:
            notify.append(f"supervisor telegram {os_['telegram']}->{ns['telegram']}")
        for ref in sorted(set(ns["open_incidents"]) - set(os_["open_incidents"])):
            notify.append(f"incident opened {ref}")
        for ref in sorted(set(os_["open_incidents"]) - set(ns["open_incidents"])):
            notify.append(f"incident resolved {ref}")
        if os_["provider_maintenance"] != ns["provider_maintenance"]:
            notify.append("provider maintenance " + ("present" if ns["provider_maintenance"] else "absent"))
    op, np_ = old.get("prs"), new.get("prs")
    if op is not None and np_ is not None:
        for number in sorted(set(np_) - set(op)):
            notify.append(f"new open PR #{number}")
        for number in sorted(set(op) - set(np_)):
            quiet.append(f"PR #{number} closed")
    return notify, quiet


def load_state(path):
    """The stored state, or None when it is missing, unreadable or any part of it has the wrong shape (it is then rebuilt)."""
    try:
        data = read_json(path, own=True)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    fingerprint, errors = data.get("fingerprint"), data.get("errors", {})
    if not valid_fingerprint(fingerprint) or not valid_errors(errors):
        return None
    return {"fingerprint": fingerprint, "errors": errors}


def write_private(path, payload):
    """Atomic write of a small JSON file, created 0600 (mkstemp does that; the chmod is defense in depth against a changed umask or platform)."""
    path = Path(path)
    ensure_private_dir(path.parent)
    descriptor, temp = tempfile.mkstemp(prefix=".observer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def save_state(path, fingerprint, errors):
    write_private(path, {"schema": SCHEMA, "fingerprint": fingerprint, "errors": errors})


def run(args, pr_provider=gh_prs):
    """One observation. Returns (line, failure_class); failure_class is set when the observer itself misbehaved."""
    failure = None
    state = load_state(args.state)
    previous = state["fingerprint"] if state else None
    previous_errors = state["errors"] if state else {}
    current, errors = collect(args, pr_provider)
    merged = dict(previous or {})
    merged.update(current)                                  # a failed source keeps its last value
    if args.no_prs:
        merged.pop("prs", None)
    error_counts = {name: previous_errors[name]["count"] + 1 if name in previous_errors else 1 for name in errors}
    stored_errors = {name: {"class": errors[name], "count": error_counts[name]} for name in errors}
    notify, quiet = [], []
    if previous is not None:
        try:
            notify, quiet = delta(previous, merged)
        except Exception as exc:                            # never stay stuck on a bad comparison: report, and persist what we just built
            failure = type(exc).__name__
            merged = dict(current)
    for name, count in error_counts.items():
        if count == 2:
            notify.append(f"source {name} unreadable ({errors[name]}) twice running")
    for name, entry in previous_errors.items():
        if name not in errors and entry["count"] >= 2:
            notify.append(f"source {name} readable again")
    try:
        save_state(args.state, merged, stored_errors)
    except Exception as exc:
        failure = failure or type(exc).__name__
    if previous is None:
        line = "QUIET"
    elif notify:
        line = clean("CHANGE: " + "; ".join(notify + quiet))
    elif quiet or any(count == 1 for count in error_counts.values()):
        line = "QUIET"
    else:
        line = "NO_CHANGE"
    return line, failure


def read_failure(path):
    try:
        data = read_json(path, own=True, limit=MAX_FAILURE_BYTES)
    except (OSError, ValueError, RecursionError):
        data = _MEMORY_FAILURE.get(str(path))
    if isinstance(data, dict) and isinstance(data.get("class"), str) and _int(data.get("count")) and data["count"] >= 1:
        return data
    return None


def write_failure(path, value):
    """Best effort, in the private observer directory only. value None clears the counter. Returns whether it is stored durably;
    when it is not, the count is kept in memory for this process only (never in a shared temp directory)."""
    key = str(path)
    if value is None:
        _MEMORY_FAILURE.pop(key, None)
    else:
        _MEMORY_FAILURE[key] = value
    try:
        if value is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                write_private(path, {"class": "", "count": 0})
        else:
            write_private(path, value)
        return True
    except (OSError, ValueError, RecursionError):
        return False


def settle(path, line, failure):
    """Turn internal failures into one visible report instead of a silent QUIET, and report recovery once."""
    previous = read_failure(path)
    if failure:
        count = (previous["count"] if previous else 0) + 1
        if not write_failure(path, {"class": failure, "count": count}) or count == 2:
            return clean(f"CHANGE: observer failing ({failure})")   # an undeduplicable failure is reported every run
        return "QUIET" if count == 1 else "NO_CHANGE"
    if previous:
        write_failure(path, None)
        if previous["count"] >= 2:
            tail = "; " + line[len("CHANGE: "):] if line.startswith("CHANGE: ") else ""
            return clean("CHANGE: observer recovered" + tail)
    return line


def main(argv=None, environ=None, pr_provider=gh_prs):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=environ.get("SCHEDULER_OBSERVE_STATE", str(DEFAULT_STATE)))
    parser.add_argument("--failure-file", default=environ.get("SCHEDULER_OBSERVE_FAILURES"),
                        help="failure counter file (default: next to the state file, in the same private directory)")
    parser.add_argument("--queue-dir", default=environ.get("SCHEDULER_OBSERVE_QUEUE", str(DEFAULT_QUEUE)))
    parser.add_argument("--monitor-status", default=environ.get("SCHEDULER_OBSERVE_MONITOR", str(DEFAULT_MONITOR)))
    parser.add_argument("--supervisor-dir", default=environ.get("SCHEDULER_OBSERVE_SUPERVISOR", str(DEFAULT_SUPERVISOR)))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--no-prs", action="store_true", default=environ.get("SCHEDULER_OBSERVE_NO_PRS") == "1",
                        help="do not run the read-only `gh pr list` (network) PR provider")
    parser.add_argument("--receipts-tail-bytes", dest="tail_bytes", type=int, default=int(environ.get("SCHEDULER_OBSERVE_TAIL", DEFAULT_TAIL_BYTES)))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        if exit_.code == 0:
            raise                                          # --help
        print("QUIET")
        return 0
    failure_file = args.failure_file or str(Path(args.state).parent / FAILURE_NAME)
    try:
        line, failure = run(args, pr_provider)
    except (Exception, RecursionError) as exc:
        line, failure = "QUIET", type(exc).__name__
    print(settle(failure_file, line, failure))
    return 0


if __name__ == "__main__":
    sys.exit(main())
