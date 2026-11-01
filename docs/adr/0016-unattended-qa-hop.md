# ADR 0016: Unattended QA hop: exact-argv sudoers as the QA user plus a pinned helper (SPE-153)

Status: approved design (founder-authorized engineering); **not implemented and not installed on any machine**. This ADR is
docs only. The staged plan below ships in later pull requests; the current Fornax candidate QA stays on the attended path.

Numbering: 0016 was the next free number on `origin/main` (e630b47) when this was written. A parallel SPE-150 pull request may
also want 0016; whichever merges second renumbers.

## Context

`scripts/qa-handoff.py run --qa-identity NAME` runs Codex QA as a dedicated unprivileged OS user (SPE-141, SPE-142). Every hop
is `sudo -n -u NAME ...`, and `sudo -n` only works while the owner's cached credential is valid. The owner therefore types
`sudo -v` in a Terminal before every run and launches the run from that same Terminal, because sudo's timestamp is tied to the
terminal session (`docs/QA-HANDOFF.md`, "Launch it from your own Terminal"). A producer agent session cannot use it, and a
long run needs a keepalive (`SudoKeepalive`) so the credential does not expire before the seal. The result is that an
independent QA run cannot start unattended even though every other part of the hand-off is automated.

The sealing, candidate binding, sweep semantics and fail-closed mapping are not the problem and are **not** changed by this ADR.
The only thing to change is how the producer user is allowed to act as the QA user.

## Privilege audit (origin/main e630b47)

Every place the producer reaches the QA user or root during a run. "Runs as" is the effective user of the command.

| # | Operation | Where (file:line) | Runs as | Purpose | Why normal permissions are not enough | New path |
|---|---|---|---|---|---|---|
| 1 | `sudo -n -v` credential refresh, every 60 s, at start, before the seal and before the final sweep | `qa-handoff.py:1050-1056` (started `:1414`, `:1372`, `:1517`); owner types `sudo -v` first (`qa-handoff.py:8`) | root (no command runs) | keep the owner's cached sudo credential alive for one run | the credential is tty/session-coupled, so only the Terminal that typed it can use it | **eliminated**: the sudoers rules below need no credential |
| 2 | `kill -KILL -1` | `qa-handoff.py:1114` (`sweep_identity`) | QA user | zero-survivor sweep before the run, at the seal, after the seal (respawn recheck) and in the final cleanup | only the owner of a process (or root) can signal it; the producer cannot signal QA-uid processes | sudoers rule 1 |
| 3 | hop 0 isolation probe: `python3 -I -c PROBE ...` | `qa-identity-setup.py:357` via `qa_argv` (`:179`), called from `qa-handoff.py:1436` | QA user | prove the QA uid cannot read the producer's process environment (sentinel in a producer sibling) | the claim is about what the QA uid can read, so the probe must execute as the QA uid | helper verb `probe` |
| 4 | hop 1 `qa-stage` | `qa-handoff.py:1465` through `hop()` (`:1249`, `:1254`) | QA user | create the outbox, copy the clone, build the private Codex home, run the confinement probe | the outbox and Codex home must be QA-owned, and the probe must run under the QA uid's sandbox | helper verb `stage` |
| 5 | hop 2 `qa-exec` (`codex exec`) | `qa-handoff.py:1500`, launched at `:1507` | QA user | run Codex with the QA user's login; stdout is the producer-held pipe | Codex must run as the QA uid to use its login and to be confined from the producer's files | helper verb `exec` |
| 6 | `launchctl print user/<uid>` and `user/<uid>/<label>` | `qa-handoff.py:1888` (`verify_user_domain`) | QA user | verify every loaded service of the QA user's launchd domain is a sealed system service | a per-uid launchd domain is printed only to that uid (or root) | sudoers rules 5 and 7 |
| 7 | `crontab -l`, `atq`, `ls -A <QA home>/Library/LaunchAgents` | `qa-handoff.py:1932-1937` (`persistence_problems`) | QA user | no scheduled job or launch agent survives the run | crontab and the at queue are per user and the QA home is mode 0700 | sudoers rules 2, 3, 4 |
| 8 | `launchctl print gui/<uid>` | `qa-handoff.py:1952` (`persistence_problems`) | QA user | a GUI session of the QA user must be absent | per-uid domain | sudoers rule 6 |
| 9 | `/usr/bin/stat -f %u /dev/console` (added by cae PR #172, replaces `launchctl managername`) | `qa-handoff.py` (console owner check) | producer | learn which uid owns the console, to corroborate that an exit-125 answer for `gui/<uid>` means "no GUI session" | **no privilege needed**: it runs as the producer and reads only the console device owner | none (unprivileged) |
| - | one-time account, group, home and exchange creation, ACLs, rollback | `qa-identity-setup.py:235-436` | root via the owner's sudo password | create the QA identity | administrative | **unchanged**: owner-run, still password-gated |
| - | QA user Codex device login | `qa-identity-setup.py:442` (printed for the owner) | QA user via the owner's `sudo -v` | give the QA user its own Codex login | interactive device-code login | **unchanged**: owner step |

Reads that need no privilege stay as they are: `ps -U <uid>` (process ids only) and the kernel executable path of a process.

## Alternatives compared

| Option | Verdict |
|---|---|
| **Status quo** (owner types `sudo -v` and runs from Terminal) | Safe and verified, but cannot run unattended. Kept as the attended mode and as the fallback. |
| **`NOPASSWD: ALL` as the QA user** | Rejected. Any code that runs as the producer can run anything as the QA user (read its Codex login, plant jobs, defeat the sweep and the seal). It turns the QA identity from a boundary into a convenience. |
| **A QA-user worker daemon** | Rejected. (a) Codex's stdout would land in a file the QA user can write, so `events.jsonl` stops being producer ground truth (today it is the pipe the producer holds and hashes). (b) A resident QA-user process breaks the zero-survivor sweep: the daemon is a survivor by construction. (c) Post-run inspection (sweep, persistence, launchd checks) would still need sudoers, so it removes none of the privilege. |
| **Root-owned helper with verbs** | Rejected. Nothing in the audit needs root: every operation is "act as the QA user". A root helper adds a root attack surface for no capability. |
| **Chosen: exact-argv sudoers as the QA user plus a root-owned, pinned helper run as the QA user** | One drop-in grants the producer seven fixed-argv inspection and sweep commands and one helper path with four fixed verbs, all as the QA user only. The helper replaces the three code-running hops. Codex stdout stays on the producer-held pipe. |

## Decision

Add an optional **installed mode** of the QA hop. The attended mode is unchanged and stays the default.

### Trust boundaries

- **Producer** (the owner's login, where agent sessions run): trusted for what it already is today, the source of the prompt,
  packet and clone, the holder of the stdout pipe and the sealer of results. It gains the ability to act as the QA user for the
  commands below, nothing else.
- **QA user** (unprivileged, no sudo rights, private home, not admin): untrusted after the run starts, exactly as today. Its
  outbox is never read again after the seal.
- **Root-owned install** (`/Library/cae-qa/`, `/etc/sudoers.d/cae-qa`): written once by the owner as administrator; the
  producer and the QA user cannot change it. Everything the helper trusts about itself comes from here.
- The helper never runs as root and the sudoers rules never name root or `ALL` as the target user.

### Sudoers drop-in: the seven fixed-argv commands

`/etc/sudoers.d/cae-qa`, shown with `PRODUCER`, `QA` and `QAUID` as the values for the machine:

```
# cae-qa (SPE-153, ADR 0016). Managed file: reinstall, never edit by hand.
Defaults>QA env_reset, !setenv, secure_path="/usr/bin:/bin:/usr/sbin:/sbin", always_set_home

Cmnd_Alias CAE_QA_SWEEP = /bin/kill -KILL -1
Cmnd_Alias CAE_QA_SEE   = /usr/bin/crontab -l, \
                          /usr/bin/atq "", \
                          /bin/ls -A /Users/QA/Library/LaunchAgents, \
                          /bin/launchctl print user/QAUID, \
                          /bin/launchctl print gui/QAUID, \
                          /bin/launchctl ^print[[:blank:]]user/QAUID/[A-Za-z0-9_][A-Za-z0-9_.-]*$
Cmnd_Alias CAE_QA_SIDE  = sha256:HELPERHEX /Library/cae-qa/qa-side version, \
                          sha256:HELPERHEX /Library/cae-qa/qa-side ^(probe|stage|exec)[[:blank:]][A-Za-z0-9][A-Za-z0-9._-]*$

PRODUCER ALL = (QA) NOPASSWD: NOSETENV: CAE_QA_SWEEP, CAE_QA_SEE, CAE_QA_SIDE
```

The seven commands, numbered as the audit table refers to them (all as the QA user, nothing else, never root):

| Rule | Exact argv |
|---|---|
| 1 | `/bin/kill -KILL -1` |
| 2 | `/usr/bin/crontab -l` |
| 3 | `/usr/bin/atq ""` (no arguments) |
| 4 | `/bin/ls -A <QA home>/Library/LaunchAgents` |
| 5 | `/bin/launchctl print user/<uid>` |
| 6 | `/bin/launchctl print gui/<uid>` |
| 7 | `/bin/launchctl print user/<uid>/<label>`, label matching `[A-Za-z0-9_][A-Za-z0-9_.-]*` in sudoers; the 200-character cap of the producer's `SERVICE_LABEL_RE` is enforced by the producer, not by sudoers |

The helper is granted separately (`CAE_QA_SIDE`): its argv is also fixed, a verb from `{version, probe, stage, exec}` and, for the
last three, an attempt id.

`Defaults>runasuser`, the `sha256:` digest form (SHA-2 hex or base64, placed before the command) and the `NOSETENV` tag are
documented in sudoers(5) of the sudo 1.9.17 on the authoring Mac; the full file is still only validated by `visudo -c -f` at
install. `HELPERHEX` is the digest of the installed helper, so **the drop-in is regenerated whenever the helper changes**.
The digest check is racy: macOS has no `fexecve(2)`, so sudo verifies the file and then executes the path. That is why the
helper lives where neither the producer nor Homebrew can write (below) and the producer verifies the path before every run;
the digest is defence in depth, not the barrier.

**Regular-expression arguments (rev 4, 2026-11-01).** The two regex rules are written exactly as sudoers(5) defines them for sudo 1.9.10+:
the argument string starts with `^`, ends with `$`, is NOT quoted, and is matched against all arguments joined by single spaces, so the
verb belongs to the expression (`^print[[:blank:]]user/QAUID/...$`) and a blank is `[[:blank:]]`. The earlier revisions wrote them in
double quotes, which sudo reads as a literal string that never matches: `visudo -c` accepted it, the owner readback passed (it only ran the
literal `version` rule), and the first real producer run stopped at `launchd domain user/530 could not be read (sudo -n refused)`.
`tests/test-qa-hop-install.py` now checks every rendered rule for this syntax and matches sample argv against the expressions.
Parsing alone proves nothing about matching: the post-install readback must run one allowed shape of each rule for real.

Sudoers regular expressions here contain no comma: a comma inside `{m,n}` is read as a list separator even in double quotes
(`visudo` rejects it), so lengths are not limited in sudoers. The helper enforces the attempt-name cap (150 characters, validation
item 2), and the producer enforces the service-label cap (200). Sudoers still lets a name contain dots, which is harmless: the
helper rejects `..` and paths are never built from argv.

Validation note (2026-10-31): the exact drop-in above, rendered with a throwaway user `qauser`, uid 599, producer `bryant` and a
placeholder 64-hex digest, was run through `/usr/sbin/visudo -c -f <file>` unprivileged (sudo 1.9.17p2, macOS) and reported
`parsed OK`. Re-validated 2026-10-31 after `launchctl managername` was removed (seven commands, rule 5 gone): the exact rendered
text, with the same throwaway values, again reported `parsed OK` under an unprivileged `visudo -c -f`. This proves syntax only, not behaviour under a real `sudo`.

Two things in this text are **not verified until the installer runs on the real host**: regular-expression arguments need
sudo 1.9.10 or newer (the installer checks `sudo -V`), and the rendered file for the real host is checked by `visudo` on that machine. If the host's sudo is older, the per-label `launchctl print` moves into a helper verb and this ADR is amended
first; no wildcard (`*`) is ever substituted, because sudoers wildcards also match spaces and slashes.

### Pinned helper

The location is `/Library/cae-qa/`, not `/usr/local/libexec`: `/usr/local` can be writable by the producer (Homebrew, a stray
`chown`), and a sudoers grant that matches a path runs whatever sits at that path as the QA user. The helper checking its own
chain cannot protect against its own replacement, so integrity is enforced from outside it: the root-only location, the sudoers
digest pin, and a producer-side check (below).

`/Library/cae-qa/qa-side` is `scripts/qa-side.py` (PR 1 creates it as a standard-library-only module with the same
code the attended hop copies today) installed root-owned. It is run as the QA user with the pinned Command Line Tools interpreter (`#!/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9 -I`; see the interpreter addendum), not the `/usr/bin/python3` shim. Verbs:

| Verb | Replaces | Does |
|---|---|---|
| `version` | n/a | prints protocol version, the SHA-256 of its own file, the SHA-256 of the pinned Codex, the QA uid |
| `probe <attempt>` | hop 0 probe | the isolation probe, with the sentinel sibling's pid and value taken from `request.json` |
| `stage <attempt>` | hop 1 `qa-stage` | create the outbox, clone, private Codex home, confinement probe; prints one JSON record |
| `exec <attempt>` | hop 2 `qa-exec` | detach from the terminal, run the pinned Codex; **stdout stays the producer-held pipe**, stderr goes to the outbox |

The helper also takes over what the terminal-detaching `env -i ... python3 -I -c DETACH` wrapper does today: it runs with no
inherited environment (it builds one from scratch), starts a new session and closes everything but stdout, so the QA process has
no controlling terminal.

How it detaches, and what the producer's signals reach: like the attended `DETACH` wrapper, the helper **forks first** and calls
`setsid` only in the child. A process that leads its process group cannot call `setsid` (EPERM), and the privilege tool may
start the helper as such a leader, so an in-place `setsid` could fail on a real host while passing every offline test. The
resulting process tree is: producer's `sudo` child, then the helper parent (the process sudo started), then the detached
child in its own session (it supervises Codex, kills Codex's group on timeout or signal, and is the one with SIGALRM armed).
The parent holds no descriptor above 2, forwards TERM, INT and HUP to the child, waits for it and exits with its status (128
plus the signal number if a signal ended it). The producer therefore still stops a run by signalling its sudo child, sudo
relays to the helper parent, and the parent relays into the new session; stdout is inherited by the child, so the producer's
pipe is unchanged. The held prompt descriptor lives only in the child.

### Protocol

The producer writes `X/inbox/<attempt>/request.json` (producer-owned, read-only to the QA user through the inherited ACE, beside
`packet.json` and `prompt.txt` as today). `X` is the exchange directory. The helper validates, in this order, and fails closed
on any miss: **a silent exit 2** (nothing printed, nothing started; the producer maps it to `QA_INCOMPLETE`). The one exception is
`stage` after validation has passed: an exception inside the stage work prints the record `{"error": "<ExceptionName>"}` and exits 0, which
the producer also maps to `QA_INCOMPLETE`. As implemented, the order is argv shape, attempt, the configuration chain and its contents
(the QA uid comes from it), uids, the caller-uid variable, the Codex chain and digest, then the request:

1. verb is in `{version, probe, stage, exec}` and the request names the same verb as argv;
2. attempt matches `[A-Za-z0-9][A-Za-z0-9._-]{0,150}` and contains no `..`;
3. real and effective uid both equal the configured QA uid (the helper refuses to run as root or as any other user);
4. `SUDO_UID` equals the configured producer uid (like `RUN.lock` in item 8, this only guards against stray calls; sudoers is the authorization);
5. the configuration chain and the Codex chain are root-owned and not group or world writable, none a link, each component from
   `/` down: `/Library`, `/Library/cae-qa`, `qa-side.json`, and the pinned Codex path (a tripwire only). The helper's own path is
   **not** checked by the helper (it cannot vouch for its own replacement); the producer verifies it before every run and sudoers
   pins its digest (see "Helper integrity");
6. the Codex binary digest equals the pinned digest in `qa-side.json` (decision D1);
7. every open of `request.json`, `prompt.txt`, `packet.json` and the clone uses `O_NOFOLLOW`, rejects non-regular files and
   enforces size caps (request 4096 B, prompt 1 MiB; the helper does not read `packet.json`), requires the request, the attempt
   directory and the inbox to be owned by the producer uid and not group or world writable, requires a single link on the
   request and the prompt, and copies the clone only through descriptors with entry, byte and depth caps;
8. `X/inbox/RUN.lock` exists (a run is in progress under the producer's lock protocol; this is a guard against a stray call, not
   authentication);
9. all paths (exchange, inbox, outbox, QA home, Codex home, Codex binary) are derived from `qa-side.json` and the validated
   attempt id, never from argv or from the request;
10. the child environment is built from an allowlist and **nothing is taken from the caller**: `HOME` comes from
    `pwd.getpwuid(<configured QA uid>)`, `PATH` is the fixed `/usr/bin:/bin:/usr/sbin:/sbin`, `CODEX_HOME`, `TMPDIR` and the
    other variables are derived from `qa-side.json` and the attempt id, and `CODEX_HOME`, `PATH`, `LANG`, `TERM`, `SHELL`
    and everything else in the inherited environment are ignored. The attended code (`isolated_env`) copies `PATH`, `LANG`,
    `TERM`, `SHELL` and a `CODEX_HOME` from `os.environ`, so the installed helper needs its own environment builder (PR 2).
    sudoers backs this with `env_reset`, `!setenv`, the `NOSETENV` tag, a fixed `secure_path` and `always_set_home`.

`qa-side.json` (root:wheel 0444) holds the QA name, QA uid, producer uid, exchange path, the pinned Codex path and digest.

### File ownership and modes

| Path | Owner | Mode | Notes |
|---|---|---|---|
| `/etc/sudoers.d/cae-qa` | root:wheel | 0440 | written last, after `visudo -c -f` |
| `/Library`, `/Library/cae-qa/` | root:wheel | 0755 | directories; nothing under them writable by group or other |
| `/Library/cae-qa/qa-side` | root:wheel | 0555 | `scripts/qa-side.py`, first line `#!/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9 -I` |
| `/Library/cae-qa/qa-side.json` | root:wheel | 0444 | digests and ids, no secret |
| `/Library/cae-qa/codex` | root:wheel | 0555 | pinned copy of the Codex binary (about 300 MB) |
| `/Library/cae-qa/codex-code-mode-host` | root:wheel | 0555 | pinned copy of the tool host Codex starts from next to its own binary (about 65 MB); its digest is in `qa-side.json` |
| `X` exchange | root | 0711 | unchanged (qa-identity-setup.py) |
| `X/inbox` | producer | 0700 + read ACE for the QA user | unchanged |
| `X/outbox` | QA user | 0700 + ACE for the producer | unchanged |

### Install procedure (owner, as administrator; a later PR provides the tool)

1. Review the plan the installer prints (no change is made by `--plan`).
2. Copy the helper, `qa-side.json` and the pinned Codex into `/Library/cae-qa/` with the modes above; compute and record
   the digests.
3. Write the candidate sudoers text to an owner-owned temporary file and run `visudo -c -f <file>`. Stop on any error.
4. Install the sudoers file **last**: `install -o root -g wheel -m 0440 <file> /etc/sudoers.d/cae-qa`.
5. Run `visudo -c` on the live configuration. If it fails, remove `/etc/sudoers.d/cae-qa` immediately (an administrator Terminal
   stays open for this) and stop.
6. Run the install-time controls below.
7. Run `sudo -K` so no administrator credential outlives the install.

### Install-time controls

Must pass (each run as the producer, non-interactively):

- `sudo -n -u QA` of each of the seven commands exits with the command's own status and no "password is required" message;
- `qa-side version` prints the digest of the installed file and the pinned Codex digest, both equal to what the installer recorded;
- the ownership and mode of every path in the table above, and the root-owned chain, are as specified;
- `sudo -V` is 1.9.10 or newer;
- `visudo -c -f` on the exact rendered text reports `parsed OK` (a test of the installer, run before the file is installed).

Must be refused (non-zero with a sudo refusal, nothing executed): `sudo -n -u QA -g wheel ...`; `-u '#0'`; `-u '#<QA uid>'`
(numeric spellings); `-u ALL`; `-i`; `-s`; `sudo -l`; the helper invoked by a relative path, through a symlink or after a
`cd`; `sudo -n -u QA /usr/bin/id`; `sudo -n -u QA /bin/sh -c id`;
`sudo -n -u QA /usr/bin/env`; `sudo -n -u root /usr/bin/true`; `sudo -n /usr/bin/true`; `sudo -n -u QA -E ...`;
`kill -KILL 1`, `kill -TERM -1`, `kill -KILL -1 123`; `crontab -r`, `crontab -e`, `atq x`; `ls -A /Users/QA` and
`ls -A /Users/QA/Library/LaunchAgents/x`; `launchctl print system`, `launchctl print user/0`, `launchctl print gui/QAUID/x`,
`launchctl print user/QAUID/a b`, `launchctl print user/QAUID/../x`; `qa-side bogus`, `qa-side exec ../x`, `qa-side exec` with a
152-character attempt, `qa-side exec a extra`; the pinned `codex` run directly.

### Rollback

Remove `/etc/sudoers.d/cae-qa` (one step; the producer immediately loses every grant) and confirm `sudo -n -u QA /usr/bin/atq ""`
is refused. Removing `/Library/cae-qa/` is optional and independent. The attended mode never depended on either, so it
works unchanged afterwards.

## What this changes for the owner

Eliminated: typing `sudo -v` before every run, launching from the Terminal that typed it, and the keepalive (installed mode
takes no sudo credential and sends no `sudo -n -v`).

Remains: the one-time administrator install; reinstalling when `qa-side.py` or the pinned Codex changes (the digests are part of
the install), and regenerating the sudoers drop-in whenever the helper digest pin changes; the QA user's Codex device login (`qa-identity-setup.py` prints the command); rechecking the install after a macOS
major upgrade (sudo version, `launchctl` output format, SIP/Python path).

## Payload staging and the self-test gate (SPE-145 part 9)

A producer may stage a manifest-described payload (`docs/QA-PAYLOAD.md`) that is mounted read-only and executable in the permission
profile and exercised by model-free self-tests under the real profile before Codex may start. In installed mode it needs **no new
verb, request key or sudoers shape**: `stage` discovers `inbox/<attempt>/payload` through the validated attempt descriptor, copies it
file by file (descriptor-relative, no link, producer-owned, single link, exact modes by `kind`, manifest verified before and after),
adds it to the profile, runs the controls and then the self-tests, and reports both in its one JSON record. The producer derives the
required control set from the manifest it holds and recomputes every self-test verdict from the manifest and the QA side's raw
observations (exit status, stdout hash). Those observations come from the QA side and are trusted because no model has run yet and the
payload is hash-bound; the independent proof of the payload itself is the seal's re-hash.

**Ordering rule.** Producer-supplied code runs under the sandbox only on a profile that has just been proved. The base controls
(including the six socket controls, each of which must be refused with EPERM: loopback bind, UNIX-socket bind, IPv4 and IPv6 TEST-NET-1 connects, a UDP send and the system resolver's socket; they show that direct socket access is restricted, not that the machine is isolated) and the non-executing payload
control `neg_write_payload` run first; `pos_exec_payload` and every self-test run only if all of them ran and passed, otherwise they
are recorded `NOT RUN`. Payload processes use the self-test allowlist environment, start in their own session with a file-size limit and have their
process group killed on completion or timeout. A process that leaves its session is not reached by that, so the producer sweeps the QA
user after the payload code ran and before the exec hop (or the seal, for `--no-model`) and refuses the run, with no `LAUNCHED`, if
anything remains: the guarantee is group kill plus a sweep before launch, and the seal's sweep must still report zero QA-user processes.

**Seal and result.** The producer re-hashes `outbox/payload` through the opened outbox descriptor and binds the manifest digest in
the packet, `MODE.json`, `RUNMETA.json` (the digest is not written into `events.jsonl`, which is Codex's own hashed stream) and
`RESULT.json`, which also carries the producer's scope text, the `PRODUCER-RUN` labels and the statement that the probe covers only
offline, library-level checks. `--no-model` runs everything up to and including the seal and sweep and stops with `QA_INCOMPLETE`
marked `no_model`.

**Changed base control set.** Six socket controls (loopback bind, UNIX-socket bind, IPv4 and IPv6 TEST-NET connects, UDP send, resolver socket)
are in the base set of every run, so an older helper's probe record (without them) is rejected and the helper must be reinstalled for
unattended runs; a host that answers any of them with anything but an immediate EPERM (EACCES no longer counts for a socket) fails closed.
Their real macOS behaviour under the QA identity is unverified until an owner run; the last three were measured as the owner's user.

Because `qa-side.py` changed, its SHA-256, and therefore `HELPERHEX` in the drop-in, changes: the owner reinstalls. The command
shapes do not change.

## Statement against earlier documentation

`docs/QA-HANDOFF.md` says "No sudoers change is made or needed." That is true of the attended mode and stays true of it. This ADR
**reverses it for the installed mode**: the unattended hop does need a sudoers drop-in, installed by the owner. It does not
change `qa-identity-setup.py`, which still never edits sudoers; the drop-in is written by a separate owner-run installer
(PR 3 below) and never by an agent.

### Helper integrity

A grant that matches only a path would run whatever sits there. Controls, in order of strength: (1) the install lives under
`/Library/cae-qa/`, root:wheel, no group/other write on any component, a place Homebrew never touches; (2) the sudoers entries
pin the helper's SHA-256 (racy without `fexecve`, stated above); (3) before **every** run the producer `lstat`-verifies each
component of the helper path, `qa-side.json` and the pinned Codex (root-owned, no group/other write, no symlinks) and fails closed
with `QA_INCOMPLETE`, codex not started (PR 3: `qa-handoff.py --unattended`; the helper path, `qa-side.json` and the Codex path are all covered); (4) the helper's own chain check, which can only
detect, not prevent, its replacement.

## Risks and unknowns

- **Residual risk, stated plainly.** A compromised producer can choose the prompt and packet the pinned Codex runs. That is
  effectively a sandboxed arbitrary-command primitive as the QA user, using the QA user's Codex login and quota. The limits are
  those of the permission profile (read allowlist, writes only to the clone and scratch, direct socket access refused; system helpers such as `trustd` may still act on a process's behalf) and the seal; they are not
  a promise that the QA user cannot do anything. Anything the QA user can read, a command it runs can read.

- **The human gate moves.** Today a person types the password per run. After install, any process running as the producer user
  can start a QA run, and so can drive the QA user's Codex login with a prompt it chooses (the prompt is the producer's input
  today as well). Mitigations: fixed verbs, pinned Codex, the confinement controls re-proved on every run, the seal and sweep
  unchanged, `RUN.lock`, and sudo's own syslog record of every grant used. Not mitigated: a compromised producer can still spend
  QA quota.
- **Sudoers syntax is a lockout risk.** A bad file can break sudo for administrators. Mitigated by `visudo -c -f` first, installing
  last, `visudo -c` after, an open administrator Terminal, and one-step rollback.
- **Regex arguments and exit codes are unverified.** sudoers regex matching (syntax parses, matching is untested) and the exit status of `kill -KILL -1` with no QA
  process are observed at install time, not assumed here.
- **Codex pin staleness.** A Codex update on the producer does not reach the QA run until the owner reinstalls; the run fails
  closed (digest mismatch) rather than running an unpinned binary.
- **Helper bugs are privileged code.** The helper is small, standard-library only, tested offline against a fake sudo, and reviewed
  as security-sensitive; its trust is the root-owned install, not its location.
- The behaviour of real `sudo` with `NOPASSWD` regex rules, real signal relay through the new session and the first unattended run
  are not verified until the owner installs and a pilot runs.

## Decision D1: pin Codex in the root-owned directory (default yes, about 300 MB)

Without the pin the helper would run whichever `codex` is on a path the producer can write (Homebrew's prefix is owned by the
producer). Whoever can replace that file could then run arbitrary code as the QA user through a sudoers grant, which is
`NOPASSWD: ALL` again. Pinning costs disk (about 300 MB) and a reinstall when Codex changes. **Default: yes.** Declining D1 means
installed mode is not offered.

## Staged plan

| PR | Content | Privilege |
|---|---|---|
| 0 | this ADR and the `docs/QA-HANDOFF.md` corrections (docs only) | none |
| 1 | move the QA-side code into a self-contained `scripts/qa-side.py`; the attended mode copies and runs it with identical behaviour | none |
| 2 | helper verbs and `request.json` validation in `qa-side.py`, **an allowlist environment builder for the helper (HOME from `pwd.getpwuid`, fixed PATH, no caller environment, `CODEX_HOME` ignored) with tests that poison `PATH`, `HOME`, `CODEX_HOME`, `LANG`, `TERM`, `SHELL`**, and the installed-mode producer path in `qa-handoff.py`, tested against a fake sudo | none (offline tests) |
| 3 | owner-run installer with `--plan`, `--check`, `--rollback`, the sudoers text generator (digest pin) and the install-time controls, including a test that `visudo -c -f` parses the exact rendered text; the producer-side pre-run `lstat` verification of the helper path | writes root files only when the owner runs it with sudo |
| 4 | runbook, then a pilot unattended run after the owner installs | owner install |

The current Fornax candidate QA stays on the attended path: its bundle and run directory are not touched, and attended
`MODE.json` bytes do not change (installed mode, when it exists, records itself as a different mode value).

## Addendum: the daemon endpoint (SPE-145 PR-b, rev 2)

The owner rejected broad local TCP access and required the smallest daemon-capable path: a dedicated per-run Unix socket, isolated test state, the
independent QA submitting its own inputs to the actual candidate daemon, no access to unrelated host services or other runs' sockets, and no firewall
change. The design that meets it:

- **The daemon runs outside Codex.** A process started inside a sandboxed command does not outlive it, and the candidate daemon binds a TCP port that
  the Codex profile refuses. cae therefore starts it under a `sandbox-exec` profile it generates (state, tmp and the read-only payload; one loopback
  port; no launchd, keychain or system network helpers by name), and a cae relay joins one Unix socket to that one port.
- **Codex gets Unix sockets only.** `[features] network_proxy = true` with `[permissions.qa_confined.network] enabled = true, mode = "limited",
  allow_local_binding = false` and a `unix_sockets` table naming the daemon socket and the relay socket (with the network table disabled the
  `unix_sockets` table is ignored; measured). Every TCP, UDP, DNS and other-socket path stays refused with EPERM; the managed proxy's loopback
  listener allows no domain.
- **The proof is the listening socket.** SBPL cannot pin a bind to `127.0.0.1` (`localhost:P` also admits wildcard), so cae proves with `lsof` that the
  daemon is the only listener on its port, at `127.0.0.1`, before it starts the relay and at every poll, and stops the relay and daemon when it stops
  holding. No `pf` rule is used.
- **Two lifecycles per run** (stage and exec), because the sweep after the self-tests kills everything of the QA user, including the stage's endpoint.
  The exec one is bound by digest to the payload manifest and the generated daemon profile the producer computed.
- **The producer decides.** Every control is demanded by name and recomputed; generated profile digests and the Codex profile digest are recomputed from
  values the producer holds; a QA side that omits or alters anything is `QA_INVALID`; a lost endpoint is `QA_INCOMPLETE` (exit 123).
- **Teardown is time-bounded step by step.** While the endpoint is torn down SIGTERM, SIGHUP, SIGINT and SIGALRM are held so a second signal cannot abandon it half way; the helper's alarm therefore cannot cut a hung teardown, so every step has its own bound (process-group waits, `proc.wait`, `ps` and `lsof` timeouts, the state copy's entry cap and 120 s deadline).
- **The tool host is part of the pin.** `codex exec` starts `codex-code-mode-host` from next to its own binary and has no tool commands without it ("Code Mode is unavailable", measured). It is pinned beside the binary, its digest is in `qa-side.json`, and the helper and the producer verify it on every run. A model-free check cannot prove that a tool command runs (only a model asks for one); `tests/test-qa-endpoint-exec-model-live.py` is the one-call proof, and the owner demo repeats it as the QA user: one trivial `codex exec` whose command prints a value.
- **Accepted residuals, disclosed:** the port is reachable by any local process during the run; the read-deny list is a deny list; `trustd` and similar
  helpers may still act for a process; AppleEvents to a same-uid process cannot be blocked by any profile (measured), so the QA user must have no GUI
  session, which the owner demo must show (`osascript` against System Events and Finder, and a `tell application` that would start one; no application of that user may be running afterwards). Nothing here is
  proven for the real account until that demo.

## Addendum rev 3: the helper's interpreter is pinned (SPE-153)

`/usr/bin/python3` is a shim that resolves its interpreter through `xcode-select` (`DEVELOPER_DIR`, `/var/db/xcode_select_link`). On the owner's host that link
named an Xcode directory that did not exist, under `/Applications` (group-writable by `admin`, which includes the invoking user). Whoever creates that path
chooses the interpreter that runs every root-pinned Python script, including the helper as the QA user. The sudo digest pins script bytes, not the interpreter.

- **Decision:** the helper's first line and every interpreter it chooses outside the Codex sandbox (probe, relay, daemon profile checks) is the real,
  link-free Command Line Tools binary `TRUSTED_PYTHON`. The Codex-side confinement probes use it too (so a redirected shim cannot forge the evidence); only a host with no
  Command Line Tools falls back to the shim, and such a host cannot pass the unattended install check. `xcode-select` is not changed (it affects the
  owner's Xcode work).
- **Check (`interpreter_problems`):** every component from `/` to the binary is root-owned, not group/other writable and not a link; every entry of the
  framework version tree is root-owned and not group/other writable; a link stays inside the tree or under `/System` or `/usr/lib`; at most 50000 entries.
  Run by `plan`, by `apply` (as root, which also refuses to run under any other interpreter), by `verify` and before every `--unattended` run; the installed
  helper's first line must equal `SHEBANG`. About 20 ms on the measured tree (2006 entries).
- **Rollback is covered too:** `apply` and `rollback` both refuse to run as root under any other interpreter (the installer carries its own copy of the constant, asserted equal to the helper's); the refusal message names the manual `rm -f` of the drop-in.
- **Residual:** `qa-identity-setup.py` (the attended identity setup) and `qa-payload-manifest.py` still use `/usr/bin/python3`; they are outside the unattended helper's interpreter choice and are not run under the sudo delegation. ACLs are not visible to this Python check (the owner readback lists them with `ls -le`); a Command Line Tools update changes the path or
  tree and fails closed until the install is redone.

## Version history

| Rev | Change |
|---|---|
| 1 | Initial decision (SPE-153) |
| 2 | Daemon endpoint addendum (SPE-145 PR-b) |
| 3 | Helper interpreter pinned to the Command Line Tools binary (SPE-153) |
