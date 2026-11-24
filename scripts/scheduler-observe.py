#!/usr/bin/env python3
"""Read-only scheduler/supervisor observer that reports only material change (SPE-188).

A scheduled check used to restate an unchanged picture on every run. This tool reduces what the check reads to a
fingerprint of MATERIAL state, compares it with the previous fingerprint and prints exactly one line:

  NO_CHANGE            nothing material changed since the last observation
  QUIET                routine churn (claims, releases, cancels, an unconfirmed first error, the first baseline)
  CHANGE: <delta>      a new failure, a recovery or a decision-relevant fact; the caller should tell the owner once
  (the same observation again prints NO_CHANGE)

Material state: live claimed ids, backfill and cap-refusal counts, the monitor's mode and incident ids, the supervisor's telegram
status and open incident refs, provider-maintenance presence and the open scheduler PR numbers. Volatile counters (poll batches,
timestamps, free-space jitter) never enter the fingerprint. The tool only reads: the scheduler queue, the monitor status and the
supervisor state are opened read-only and the single file it writes is its own fingerprint (0600, atomic). It prints no path, command
line, token or message id. Exit code is always 0.

The harness goal evaluator (the `Stop hook feedback` and `Goal check-in` notices) is not part of this tool and cannot be changed by it.
"""
import argparse
import json
import os
from pathlib import Path
import re
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
LIVE_END = ("released", "expired", "cancelled")
SAFE = re.compile(r"[^A-Za-z0-9 :,.;_#>()+-]")


def clean(text, limit=160):
    return SAFE.sub("", str(text))[:limit]


def read_json(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def observe_queue(queue_dir):
    claimed, backfill_ids, backfill_events, cap_refusals, floor_grants = set(), set(), 0, 0, 0
    with open(Path(queue_dir) / "receipts.jsonl", "rb") as handle:
        for raw in handle:
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
    sources = (("queue", lambda: observe_queue(args.queue_dir)), ("monitor", lambda: observe_monitor(args.monitor_status)),
               ("supervisor", lambda: observe_supervisor(args.supervisor_dir)), ("prs", lambda: pr_provider(args.repo)))
    for name, call in sources:
        try:
            fingerprint[name] = call()
        except Exception as exc:  # a source that cannot be read keeps its previous value and counts toward an error report
            errors[name] = type(exc).__name__
    return fingerprint, errors


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
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("fingerprint"), dict):
        return None
    errors = data.get("errors")
    return {"fingerprint": data["fingerprint"], "errors": errors if isinstance(errors, dict) else {}}


def save_state(path, fingerprint, errors):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".observer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"schema": SCHEMA, "fingerprint": fingerprint, "errors": errors}, handle, sort_keys=True)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def run(args, pr_provider=gh_prs):
    state = load_state(args.state)
    previous = state["fingerprint"] if state else None
    previous_errors = state["errors"] if state else {}
    current, errors = collect(args, pr_provider)
    merged = dict(previous or {})
    merged.update(current)                                  # a failed source keeps its last known value
    error_counts = {name: previous_errors.get(name, {}).get("count", 0) + 1 if isinstance(previous_errors.get(name), dict) else 1
                    for name in errors}
    stored_errors = {name: {"class": errors[name], "count": error_counts[name]} for name in errors}
    lines_notify, lines_quiet = [], []
    if previous is not None:
        lines_notify, lines_quiet = delta(previous, merged)
    for name, count in error_counts.items():
        if count == 2:
            lines_notify.append(f"source {name} unreadable ({errors[name]}) twice running")
    for name in previous_errors:
        if name not in errors and isinstance(previous_errors[name], dict) and previous_errors[name].get("count", 0) >= 2:
            lines_notify.append(f"source {name} readable again")
    try:
        save_state(args.state, merged, stored_errors)
    except Exception:
        print("QUIET")
        return 0
    if previous is None:
        print("QUIET")
    elif lines_notify:
        print(clean("CHANGE: " + "; ".join(lines_notify + lines_quiet)))
    elif lines_quiet or (errors and any(c == 1 for c in error_counts.values())):
        print("QUIET")
    else:
        print("NO_CHANGE")
    return 0


def main(argv=None, environ=None, pr_provider=gh_prs):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=environ.get("SCHEDULER_OBSERVE_STATE", str(DEFAULT_STATE)))
    parser.add_argument("--queue-dir", default=environ.get("SCHEDULER_OBSERVE_QUEUE", str(DEFAULT_QUEUE)))
    parser.add_argument("--monitor-status", default=environ.get("SCHEDULER_OBSERVE_MONITOR", str(DEFAULT_MONITOR)))
    parser.add_argument("--supervisor-dir", default=environ.get("SCHEDULER_OBSERVE_SUPERVISOR", str(DEFAULT_SUPERVISOR)))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)
    try:
        return run(args, pr_provider)
    except Exception:                                      # the observer never fails the caller and never prints details
        print("QUIET")
        return 0


if __name__ == "__main__":
    sys.exit(main())
