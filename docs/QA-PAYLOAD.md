# QA payload: producer-built tools and vectors, staged and self-tested before the model starts

SPE-145 part 9. A producer can hand the independent QA run a directory of its own tools and test vectors (a CLI built from the
commit under test, fixed input vectors, a transcript of its own runs). The wrapper stages it into the QA outbox, mounts it in the
sandbox profile as one more read-only, executable root, and runs the producer's self-tests under the real confinement profile
**before Codex may start**. A failed self-test means Codex is never launched. This works in attended and `--unattended` identity
runs; it needs `--qa-identity` (the self-tests run as the QA user). The format below is `cae-qa-payload/1`; `cae-qa-payload/2` (see "Daemon-endpoint payloads") adds an optional candidate daemon.

## What it is not

The wrapper's probe and self-tests cover only **offline, library-level checks** under the confinement profile, and (in a daemon-endpoint
run) requests to the candidate daemon through the endpoint's Unix sockets. End-to-end results beyond that are **producer-run** and are not
independently reproduced by this QA. Every RESULT and the QA prompt say so, and
every file named in `producer_run` is labelled `PRODUCER-RUN`.

## Layout and `MANIFEST.json`

```
payload/
  MANIFEST.json            UTF-8 JSON, at most 64 KiB, exact key sets, unknown keys are refused
  bin/verify-probe         every other file is listed in "files"; nothing else may be there
  vectors/admit.json
  harness/transcript.txt
```

```json
{
  "schema": "cae-qa-payload/1",
  "files": [ {"path": "bin/verify-probe", "sha256": "<64 hex>", "size": 30, "kind": "exec"}, ... ],
  "selftests": [ {"id": "probe-selftest", "argv": ["bin/verify-probe", "selftest"], "stdin": null, "expect_exit": 0,
                  "expect_stdout_sha256": "<64 hex>", "timeout_s": 20, "max_output_bytes": 4096} ],
  "scope": "<text, 1..2048 characters, copied verbatim into RESULT and RUNMETA>",
  "producer_run": ["harness/transcript.txt"]
}
```

**Files.** `files` lists every payload file except `MANIFEST.json` itself. A path is relative and normalized: no leading `/`, no empty,
`.` or `..` component, no control character or backslash, at most 200 characters and 8 components; paths are unique and sorted.
`kind` is `exec` (the copy gets mode 0555) or `data` (0444); directories are 0555. At most 2000 files and 256 MiB in total (the 64 KiB
manifest limit allows roughly 550 entries in practice). The directory must contain exactly these files plus the manifest: an extra,
missing, linked, non-regular, wrong-size or wrong-hash file, or any extra or empty directory, is refused. The **manifest digest** is
the SHA-256 of the raw manifest bytes; that digest is bound into the packet, `MODE.json`, `RUNMETA.json` and `RESULT.json`.

**Self-tests.** At most 32, at most 600 s of timeouts in total. `id` matches `[a-z0-9_-]{1,40}` and is unique. `argv[0]` must be a
listed `kind=exec` file (given relative to the payload); every other element is a literal string (at most 256 characters, no NUL)
in which an element that is exactly `@payload` or starts with `@payload/` has that prefix replaced by the absolute `outbox/payload`
path (`@payloadX` is left alone). `stdin` is `null` or a listed `kind=data` file fed as the process's standard input.
`expect_exit` is required (0 to 255, so nonzero is allowed). Standard output is compared as **raw bytes**: its SHA-256 must equal
`expect_stdout_sha256`. Standard error is captured, truncated to `max_output_bytes`, hashed and recorded, not compared.
`timeout_s` is 1 to 120; `max_output_bytes` is 1 to 1048576 and output beyond it fails the test.

**Scope and producer-run.** `scope` is the producer's own statement of what was and was not covered. `producer_run` lists `kind=data`
files that record the producer's own runs; they are shown to QA as `PRODUCER-RUN` evidence, not reproduced.

## How a self-test runs

Each test is `codex sandbox -P qa_confined -C <clone> <absolute argv>` as the QA user, the same path the confinement probe uses,
with an allowlist environment only: `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `HOME` and `TMPDIR` inside the run directory, and
`CODEX_HOME` (the profile cannot be found without it); nothing is inherited. The working directory is the run's tmp, there is no
shell and no direct socket access, and a daemon cannot start inside the profile (binding is denied), so self-tests are command-line checks; a candidate daemon runs only through a daemon-endpoint run, outside Codex (below). Each runs in its
own session with a per-file size limit (RLIMIT_FSIZE: its output cap plus slack, at least 16 MiB, so a single-file flood is cut off by the
kernel and not only by the timeout; many small files are not limited by it). The limit applies to every file the command writes, so a
self-test that writes one file larger than 16 MiB (for example a debug build artifact) is killed with SIGXFSZ and recorded as "exited -25,
expected 0": keep self-tests small. Its process group is killed on completion or timeout. A process that left its session (setsid) is not
reached by a group kill, so after the payload code has run and **before Codex starts** (or before the seal in a `--no-model` run) the
QA user is swept; if any process cannot be removed the run is refused as `QA_INCOMPLETE` naming the survivors, with no `LAUNCHED`
record. The guarantee is therefore group kill plus a sweep before launch, not the group kill alone. Each is recorded with its id, argv, exit status, stdout
and stderr SHA-256, elapsed time and verdict in `SELFTEST.json`. Pass or fail is **recomputed by the producer** from the manifest it
holds and the raw observations (exit status, stdout hash, sizes) in the QA side's stage record; the QA side's own `ok` is never used.
The observations themselves come from the QA side: they are trusted because no model has run yet and the payload is hash-bound, and
the independent proof of the payload is the producer's re-hash of `outbox/payload` at the seal. Any mismatch, timeout, oversize output or test that could not run fails the gate:
Codex is not launched, no `LAUNCHED` record is written, and the result is `QA_INCOMPLETE` naming the test id and the reason.

Further manifest rules: duplicate keys anywhere in the JSON are refused; two paths that differ only by letter case are refused (a
case-insensitive volume would merge them); and control characters, Unicode format/bidi characters, line and paragraph separators,
surrogates, private-use and unassigned code points are refused in paths, `producer_run` entries and `scope`. Labels are sanitized again
before they reach a RESULT.

## Order of proof (nothing of yours runs on an unproved profile)

1. The base controls (reads and writes confined, the canary, the Codex home and login denied, the profile not widened by the clone, and
   direct socket access refused: loopback bind, a UNIX-socket bind, IPv4 and IPv6 TEST-NET connects, a UDP send and the system resolver's
   socket, each refused with EPERM) all run first.
2. `neg_write_payload` (the payload root is not writable) joins that set.
3. Only if every one of those passed does `pos_exec_payload` run a payload executable (the first self-test's own command, else the first
   `kind=exec` file with no arguments). If any earlier control failed or could not run, it is recorded `NOT RUN` and no payload code
   starts, in the probe or as a self-test.

A denial counts only when it is EPERM or EACCES. A timeout, an unreachable network or a "path too long" proves nothing and fails the
control. A profile that opens the network fails the probe; the profile is never widened to make a control pass.

## A changed base control set (read before upgrading)

The six network controls (loopback bind, UNIX-socket bind, IPv4 and IPv6 TEST-NET-1 connects, UDP send, resolver socket) are part of the
**base** set for every run, payload or not. A record from an older helper that lacks them is refused, so the helper must be reinstalled for `--unattended` runs; and a host
whose network stack answers one of them with anything but an immediate EPERM (EACCES no longer counts for a socket; for example ENETUNREACH
fails) now fails closed. How the first three behave on real macOS under the real profile is unverified until an owner run; the last three
were measured as the owner's user. They show that direct socket access is restricted, not that the machine is network-isolated: system
helpers such as `trustd` may act on a process's behalf. What stays
unchanged when no payload is given is the packet, `MODE.json`, the prompt, `RUNMETA.json` and `RESULT.json` content and keys.

## Staging, sealing and `--no-model`

`qa-handoff.py prepare ... --payload DIR` verifies DIR against its manifest before creating anything, copies it into the run directory
and puts the manifest digest, scope and producer-run list into the packet (so they are covered by the packet digest). At `run`, the
producer verifies that copy again, copies it into the inbox, and the QA side copies it into `outbox/payload` file by file through
descriptors (no link followed, producer-owned, single link, the exact modes above), verifying the manifest before and after the copy.
After the run the producer re-hashes `outbox/payload` through the opened outbox directory and freezes the result into `RUNMETA.json`.
The payload is not part of the manifest-checked clone.

`run --qa-identity NAME --no-model` stages, proves confinement, runs the self-tests, collects, seals and sweeps, writes
`RESULT.json` and `RUNMETA.json` marked `no_model`, and stops. The result is `QA_INCOMPLETE` ("no model was started, no verdict by
design"), never a pass, also when re-evaluated later with `verdict`; the run directory cannot afterwards be continued with a model.

## Worked example

```
$ mkdir -p payload/bin payload/vectors payload/harness
$ printf '#!/bin/sh\necho "usage ok: $1"\n' > payload/bin/verify-probe && chmod 755 payload/bin/verify-probe
$ printf '{"case": "admit"}\n' > payload/vectors/admit.json
$ printf 'producer ran the daemon-level suite: 12 passed\n' > payload/harness/transcript.txt
$ printf 'usage ok: selftest\n' | shasum -a 256            # SHA-256 of the exact stdout bytes the test must print
48546f001eb094fe97176217b27d7c13a6559f8216dd489f4f989bf313c2e9da  -
$ cat selftests.json
[{"id": "probe-selftest", "argv": ["bin/verify-probe", "selftest"], "stdin": null, "expect_exit": 0,
  "expect_stdout_sha256": "48546f001eb094fe97176217b27d7c13a6559f8216dd489f4f989bf313c2e9da", "timeout_s": 20, "max_output_bytes": 4096}]
$ python3 -I scripts/qa-payload-manifest.py plan --dir payload --scope "Offline library-level checks of verify-probe only. Daemon-level \
  and end-to-end results are in harness/transcript.txt and are producer-run." --producer-run harness/transcript.txt \
  --selftests selftests.json --write
files: 3  bytes: 95  self-tests: 1  producer_run: 1
  exec          30  e2bd86b0...98f9e  bin/verify-probe
  data          47  60dbfe12...f2d0f14  harness/transcript.txt
  data          18  7418cc69...3c9782  vectors/admit.json
manifest sha256 ff716d9a4d5b33a83526c69b0d7353fc58476c65f1f3c860b66266f03faa7b65
wrote payload/MANIFEST.json
$ python3 -I scripts/qa-payload-manifest.py verify --dir payload
OK manifest sha256 ff716d9a4d5b33a83526c69b0d7353fc58476c65f1f3c860b66266f03faa7b65 (3 files, 1 self-tests)
$ python3 scripts/qa-handoff.py prepare --repo REPO --sha SHA --ticket T-1 --product prod --criteria criteria.txt --payload payload
$ python3 scripts/qa-handoff.py run --run-dir RUNDIR --qa-identity NAME --no-model        # the model-free gate on its own
$ python3 scripts/qa-handoff.py run --run-dir RUNDIR2 --qa-identity NAME --unattended      # gate first, then the model
```

(The digests shown are the real output of the commands above; yours will differ.) `plan` cannot run your self-tests: you produce
`expect_stdout_sha256` yourself, as in the third command. `qa-payload-manifest.py` loads the manifest rules from `qa-side.py`, the
same code the QA run applies, so there is one definition of the format.

## Unattended (installed) mode

No new helper verb, request key or sudoers shape: the payload is discovered by the existing `stage` verb at
`inbox/<attempt>/payload`, and the self-tests run inside `stage`. The drop-in text is unchanged in shape; because the helper file
changed, its digest in the drop-in changes, so the owner reinstalls (`qa-hop-install.py plan`, then `apply`) after merging.

## Daemon-endpoint payloads (`cae-qa-payload/2`)

For a payload whose subject is a daemon, `prepare --network daemon-endpoint --payload DIR` accepts a `cae-qa-payload/2` manifest:

```json
{ "schema": "cae-qa-payload/2", "files": [...], "scope": "...", "producer_run": [...],
  "endpoint": {"daemon": "bin/daemon", "contract": "fornax-daemon/1"},
  "selftests": [ {"id": "...", "argv": [...], "...": "...", "needs_endpoint": true} ] }
```

`endpoint` is `null` or exactly `{daemon, contract}`: `daemon` is a listed `kind=exec` file and `contract` is a contract cae knows:

- `fornax-daemon/1` (transport `tcp-relay`): the daemon reads `FORNAX_HOME` and `FORNAX_HTTP_PORT`, binds that loopback port and creates
  `fornax.sock` in its home. Described below.
- `circinus-daemon/1` (transport `unix`): no port and no relay. See "Unix-socket contracts" below.
Every self-test then carries `needs_endpoint` (strict boolean; `true` only when `endpoint` is not `null`). Schema `/1` is unchanged and a
payload that names an endpoint is refused unless the run asks for one, and the reverse.

What cae does, in order, as the QA user: the Codex profile is the Unix-socket-only one (managed proxy on, no domain allowed, no local binding,
only the two endpoint sockets in its `unix_sockets` table); the base confinement probe runs; then two `sandbox-exec` profiles generated by cae
(never from payload text) are proved with 41 controls each of what the daemon and the relay may and may not touch; only then the candidate daemon
starts, outside Codex, bound by the sandbox to its own state, tmp and the read-only payload, with network access limited to its one loopback port;
cae proves that the daemon is the **only** listener on that port, at `127.0.0.1`, and repeats the proof while the run lasts; a relay joins one Unix
socket to that port. Nine controls then run under the real Codex profile (the relay and the daemon socket reachable; TCP to the port, a decoy
socket and a proxy `CONNECT` refused; creating the exec record directory, writing into the stage record directory and listing the relay and daemon-tmp directories refused) and only after those do the `needs_endpoint` self-tests run, with `QA_ENDPOINT_STATE`,
`QA_ENDPOINT_SOCK` and `QA_ENDPOINT_HTTP_SOCK` (paths only, never the port) in their environment. A model run does the same around `codex exec`: the
payload and the generated daemon profile are held to digests the producer computed, the endpoint is supervised while Codex runs, Codex is stopped with
exit 123 if the endpoint is lost, and the daemon's state is copied into `outbox/endpoint-exec/state` before everything is torn down.

The producer decides for itself: it demands every control by name and recomputes each result, requires the loopback-only single-listener proof and a
clean teardown, regenerates the daemon profile, relay profile and relay source digests and the Codex profile digest from its own values, and takes
the self-test observations from the verified record. Raw evidence stays in `CONFINEMENT.json` (`endpoint`), `SELFTEST.json` and the run's outbox.

The exec-time record (`endpoint-exec/record.json`, including the state-copy result) is written by the QA-side helper, which is installed, root-owned and hash-pinned code, and judged by the producer for consistency; the producer cannot see inside the QA user's processes. `endpoint-exec/state` is a copy to inspect, and a consumer must read `teardown.state_snapshot.copied` first (a failed copy can leave a partial tree). Exit code 123 is the endpoint-failed code; a Codex that exits 123 by itself with a verified endpoint is treated as an ordinary Codex failure.

What this does not do: the daemon's port is reachable by any local process while the run lasts (the sandbox cannot pin a loopback bind; the
listener proof is the control), the read-deny list is a deny list, system helpers such as `trustd` may still act on a process's behalf, and
AppleEvents to a same-user process cannot be blocked by a sandbox profile, so the QA user must have no GUI session (see QA-HANDOFF.md).
Unattended mode needs the helper reinstalled (protocol 3; a helper that still answers protocol 2 is refused with "reinstall"). The owner demo,
as the QA user, is what proves it on the real account.

## Unix-socket contracts (`circinus-daemon/1`, SPE-155)

The daemon serves its own Unix socket under its state directory and binds no port, so there is nothing to relay. The same order of proof
applies, with these differences:

- **Launch.** `daemon` names the payload's interpreter (for Circinus `bin/python3.13`). cae starts it as
  `<daemon> -I -B -c "from circinus.cli import main; import sys; sys.exit(main())" daemon-run`. The bootstrap is a cae constant, never
  payload text. The environment is exactly `HOME=<tmp>`, `XDG_CONFIG_HOME=<tmp>/config`, `XDG_STATE_HOME=<state>`, `TMPDIR=<tmp>` and cae's fixed
  `PATH` and `DEVELOPER_DIR`. `XDG_RUNTIME_DIR` is not set, so the socket is `<state>/circinus/circinusd.sock`. The launch (argv plus
  environment) is bound by `daemon_launch_sha256` beside `daemon_profile_sha256`.
- **Profiles.** The Codex profile is the fornax one except that its `unix_sockets` table names only the daemon's socket. The daemon's
  `sandbox-exec` profile is the fornax one without the two loopback-port lines: every TCP, UDP and DNS path stays denied, and a socket may be bound
  only under the state directory. The controls drop the relay and port rows and add `neg_d_bind_loopback` and `neg_d_bind_tmp_socket` (both EPERM).
- **Ready and proof.** The daemon is up when its socket node passes the path checks and a ping is answered. The ping is the product's exchange:
  `{"event": "ping"}` plus a newline (18 bytes), then the client half-closes, then one JSON object line back, then the daemon closes. The proof, run by the helper
  as the QA user outside every sandbox: every directory from the endpoint directory down is the QA user's at 0700, and the node is a socket of
  the QA user at 0600 with one link. Then, on one connection and before a single byte is sent, the peer (`LOCAL_PEERPID`) is in the
  daemon's process group with the QA uid (`LOCAL_PEERCRED`) and the node is still the inode seen before connect; only then is the ping sent,
  and it must be answered with `ok: true` and a `mode` of `shadow` or `enforce`. The inode must be
  unchanged afterwards, and neither the daemon's process group nor any process below it (found by parent pid) may hold an IPv4 or IPv6 socket (`lsof -g`, `lsof -p`). Each supervision poll repeats the
  inode and inet checks. The full proof runs again as the last step before teardown (`socket_recheck`), and the producer requires both.
- **Codex-side controls.** `pos_uds_daemon_ping` (the exact exchange with half-close, under the Codex seatbelt), `neg_write_endpoint_state`,
  `neg_unlink_daemon_sock`, `neg_tcp_loopback`, plus the decoy, proxy, record-directory and daemon-tmp controls of the fornax set.
  Tool commands and `needs_endpoint` self-tests get `QA_ENDPOINT_STATE`, `QA_ENDPOINT_SOCK` and `XDG_STATE_HOME=<state>`, so `circinus hook ...`
  finds the socket the way it does in production. The state stays read-only to them: a hook client's own writes there fail, and that is expected
  to be best-effort.
- **Teardown.** The daemon's process group gets SIGTERM, a 10 s grace, then SIGKILL, and stragglers are reaped. Then the state is copied
  (exec only; sockets are skipped). The steps are `daemon_stopped`, `socket_refuses` (a connect to the path is refused or finds nothing),
  `sockets_gone` (a leftover socket node is unlinked and reported, not fatal) and `dir_removed`.

**Seeding the state.** A contract with a seed directory (`circinus-daemon/1`: `state-seed`) has its listed `kind=data` files under
`state-seed/<path>` copied by the helper, as the QA user, to `<state>/<path>`. This happens right after the state directory is created, before the
profiles are proved and before the daemon starts. It is a convention, not a schema field: the files are listed in `files` like any other.

Seed rules:
- at most 6 components below the state, and at most 32 files of at most 256 KiB each and 1 MiB in all;
- no target named `qa-probe`, `qa-probe.db` or `qa-probe.sock` (what the profile controls write), and none equal to the daemon's socket path;
- every source is opened without following a link and re-hashed;
- every target directory is created 0700 and every file exclusively at 0600, so nothing is overwritten;
- the content is never interpreted.

The rules are checked when the manifest is parsed (`prepare`, `qa-payload-manifest.py plan`) and again when the state is seeded. The record lists
`seeded` (path and SHA-256 of each file), and the producer re-derives that list from the manifest it holds. Tool commands never seed:
the state stays read-only to them (`neg_write_endpoint_state`).

**Packaging a Circinus payload.**
- **Interpreter.** The payload carries its own interpreter, because the trusted interpreter is Python 3.9 and nothing under `/Users` is
  readable to the daemon except its state, tmp and the payload. For example: `bin/python3.13` (`kind=exec`, the `--endpoint-daemon`),
  `lib/libpython3.13.dylib`, `lib/python313.zip`, and site-packages with the dependency zip and the extension-module directories.
- **Modes.** The tree is read-only in the outbox (exec 0555, data 0444). That is enough: measured under the generated profile, a payload
  extension module of mode 0444 or 0555 is loaded with `dlopen` and needs no extra rule.
- **Limits.** The payload limits stay: 2000 files, 8 path components, 256 MiB, and a manifest of at most 64 KiB (roughly 550 entries).
  Zip the library trees to fit.
- **Building it.** `qa-payload-manifest.py plan --dir DIR --scope TEXT --endpoint-daemon bin/python3.13 --endpoint-contract circinus-daemon/1`
  builds a `/2` manifest and checks the seed rules.
