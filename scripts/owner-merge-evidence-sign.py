#!/usr/bin/env python3
"""Sign a review-evidence record for scripts/owner-merge-runner.py (SPE-118).

  owner-merge-evidence-sign.py --key-file PATH --in EVIDENCE.json --out SIGNED.json

Run this with the reviewer's key file. It adds an HMAC-SHA256 over the canonical evidence JSON as the
`hmac` field. This is tamper evidence only. The runner verifies with the same key, so the key file must be
readable by the OS user that runs the runner; anything running as that user can read the key and sign
forged evidence. Only holding the key in a different OS user or trust domain would help, and the runner
cannot verify without having the key. The key is never printed; the key file must be a regular, user-owned
file with no group/other permissions. The output is created with mode 0600 and never overwrites a file.
"""
import argparse
import importlib.util
import json
import os
import pathlib
import sys

_SPEC = importlib.util.spec_from_file_location("omr", pathlib.Path(__file__).resolve().parent / "owner-merge-runner.py")
omr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(omr)


def sign(key_file, src, dst):
    key = omr.load_key(key_file)
    with open(src, "r", encoding="utf-8") as f:
        ev = json.load(f)
    if not isinstance(ev, dict):
        raise omr.Refused(["evidence is not a JSON object"])
    ev = {k: v for k, v in ev.items() if k != "hmac"}
    ev["hmac"] = omr.evidence_mac(key, ev)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ev, f, sort_keys=True)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        sign(a.key_file, a.src, a.out)
    except omr.Refused as e:
        sys.stderr.write("error: " + "; ".join(e.reasons) + "\n")
        return 2
    except (OSError, ValueError) as e:
        sys.stderr.write(f"error: {type(e).__name__}\n")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
