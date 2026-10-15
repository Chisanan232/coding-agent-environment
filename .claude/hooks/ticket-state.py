#!/usr/bin/env python3
"""Serialized, revisioned per-ticket notes and workflow state operations."""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

TICKET_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]{0,31}-[0-9]{1,12}|[0-9]{1,12})$")
SCHEMA = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
CONTEXT_OWNER = "coding-agent-environment:context:v1"
CONTEXT_READER = "context-v1"
CONTEXT_SCHEMA = 1
CONTEXT_FIELDS = {"schema", "owner", "scope_id", "generation", "state", "ticket",
                  "worktree", "release", "root_dev", "root_ino", "worktree_dev",
                  "worktree_ino", "reader_contract"}


def refuse(message):
    raise ValueError(message)


def check_ancestry(path):
    path = Path(os.path.abspath(path))
    # macOS exposes its system temporary roots through these fixed aliases.
    # Resolve only the verified OS mapping; user-created redirects still refuse.
    if sys.platform == "darwin":
        for alias in ("/var",):
            base = Path(alias)
            if path.is_relative_to(base) and base.is_symlink() and base.resolve() == Path("/private" + alias):
                path = Path("/private" + alias) / path.relative_to(base)
                break
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            refuse(f"symlinked managed path refused: {current}")
    return path


def ensure_directory(path, private=False):
    path = check_ancestry(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or private and stat.S_IMODE(info.st_mode) & 0o077):
        refuse(f"managed directory ownership or permissions refused: {path}")
    return path


def check_file(path):
    check_ancestry(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1):
        refuse(f"managed file must be user-owned, regular, and unlinked: {path}")
    return info


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, raw, mode=0o600):
    if len(raw) > MAX_FILE_BYTES:
        refuse("managed content exceeds the 16 MiB file limit")
    check_ancestry(path.parent)
    check_file(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            intended = os.fstat(stream.fileno())
            os.replace(temp, path)
            fsync_directory(path.parent)
            published = os.fstat(stream.fileno())
            if (published.st_mtime_ns, published.st_size) != (intended.st_mtime_ns, len(raw)):
                refuse("published file changed during replacement")
            return fingerprint_metadata(published, raw)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint_metadata(info, raw):
    return {"dev": info.st_dev, "ino": info.st_ino, "ctime_ns": info.st_ctime_ns,
            "mtime_ns": info.st_mtime_ns, "len": info.st_size,
            "sha256": hashlib.sha256(raw).hexdigest()}


def file_fingerprint(path):
    before = check_file(path)
    if before is None:
        return None, b""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink) != (
                before.st_dev, before.st_ino, before.st_uid, before.st_nlink):
            refuse(f"managed file changed while being read: {path}")
        if opened.st_size > MAX_FILE_BYTES:
            refuse(f"managed file exceeds the 16 MiB read limit: {path}")
        chunks = []
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES - total + 1))
            if not block:
                break
            total += len(block)
            if total > MAX_FILE_BYTES:
                refuse(f"managed file exceeds the 16 MiB read limit: {path}")
            chunks.append(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if (opened.st_ctime_ns, opened.st_mtime_ns, opened.st_size) != (
            after.st_ctime_ns, after.st_mtime_ns, after.st_size) or len(raw) != after.st_size:
        refuse(f"managed file changed while being read: {path}")
    return fingerprint_metadata(after, raw), raw


@contextlib.contextmanager
def ticket_lock(root, ticket):
    locks = ensure_directory(root / ".ticket-state-locks", private=True)
    path = locks / f"{ticket}.lock"
    check_file(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            refuse("ticket lock ownership or permissions refused")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class TicketStore:
    def __init__(self, kind, root, ticket):
        if not TICKET_RE.fullmatch(ticket):
            refuse("ticket key is malformed")
        self.kind = kind
        self.root = ensure_directory(Path(root).absolute())
        self.ticket = ticket
        self.suffix = ".md" if kind == "notes" else ".json"
        self.source = self.root / f"{ticket}{self.suffix}"
        self.revision_dir = ensure_directory(self.root / ".ticket-state-revisions", private=True)
        self.revision_path = self.revision_dir / f"{ticket}.json"
        self.archive_dir = None

    def state(self):
        info = check_file(self.revision_path)
        if info is None:
            return {"revision": 0, "pending": None, "committed": None}, False
        if stat.S_IMODE(info.st_mode) & 0o077:
            refuse("ticket revision metadata is not private; preserve state")
        try:
            _fingerprint, raw = file_fingerprint(self.revision_path)
            value = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("ticket revision metadata is corrupt; preserve state") from exc
        if (not isinstance(value, dict) or set(value) != {"schema", "revision", "pending", "committed"}
                or type(value["schema"]) is not int or value["schema"] != SCHEMA
                or type(value["revision"]) is not int or value["revision"] < 0
                or value["pending"] is not None and not isinstance(value["pending"], dict)
                or value["committed"] is not None and not isinstance(value["committed"], dict)):
            refuse("ticket revision metadata is malformed; preserve state")
        if value["pending"] is not None:
            self.validate_pending(value["pending"], value["revision"])
        return value, True

    def validate_pending(self, pending, revision):
        fingerprint_fields = {"dev", "ino", "ctime_ns", "mtime_ns", "len", "sha256"}
        action = "clear" if self.kind == "notes" else "archive"
        archive_value = pending.get("archive") if isinstance(pending, dict) else None
        if not isinstance(archive_value, str):
            refuse("pending ticket archive metadata is malformed; preserve state")
        archive = Path(archive_value)
        if (set(pending) != {"token_sha256", "fingerprint", "source_sha256", "archive", "action", "staging"}
                or type(revision) is not int or revision < 1
                or pending["action"] != action
                or not isinstance(pending["token_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", pending["token_sha256"]) is None
                or not isinstance(pending["source_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", pending["source_sha256"]) is None
                or not isinstance(pending["fingerprint"], dict)
                or set(pending["fingerprint"]) != fingerprint_fields
                or any(type(pending["fingerprint"][key]) is not int or pending["fingerprint"][key] < 0
                       for key in fingerprint_fields - {"sha256"})
                or not isinstance(pending["fingerprint"]["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", pending["fingerprint"]["sha256"]) is None
                or pending["source_sha256"] != pending["fingerprint"]["sha256"]
                or archive.is_absolute() or len(archive.parts) != 2 or archive.parts[0] != "archive"
                or archive.name != (f"{self.ticket}_r{revision - 1}_"
                                    f"{pending['token_sha256'][:20]}_{action}{self.suffix}")):
            refuse("pending ticket archive metadata is malformed; preserve state")
        staging = pending["staging"]
        if (not isinstance(staging, dict) or set(staging) != {"name", "dev", "ino", "len", "sha256"}
                or not isinstance(staging["name"], str)
                or Path(staging["name"]).name != staging["name"]
                or not staging["name"].startswith(f".{archive.name}.")
                or any(type(staging[key]) is not int or staging[key] < 0 for key in ("dev", "ino", "len"))
                or staging["sha256"] != pending["source_sha256"]):
            refuse("pending publication identity is malformed; preserve state")

    def save_state(self, state):
        state = {"schema": SCHEMA, "committed": None, **state}
        atomic_bytes(self.revision_path, (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode())

    def parse_token(self, token):
        if not token.startswith("ts1."):
            refuse("snapshot token is malformed")
        try:
            encoded = token[4:]
            value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("snapshot token is malformed") from exc
        if (not isinstance(value, dict) or set(value) != {"schema", "kind", "root", "ticket", "revision", "fingerprint"}
                or value["schema"] != SCHEMA or value["kind"] != self.kind
                or value["root"] != str(self.root) or value["ticket"] != self.ticket):
            refuse("snapshot token belongs to another managed state")
        return value

    def snapshot_locked(self):
        state, versioned = self.state()
        fingerprint, _raw = file_fingerprint(self.source)
        payload = {"schema": SCHEMA, "kind": self.kind, "root": str(self.root),
                   "ticket": self.ticket,
                   "revision": state["revision"] if (fingerprint is None or
                       versioned and state["committed"] == fingerprint) else None,
                   "fingerprint": fingerprint}
        token = "ts1." + base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        return token

    def read(self):
        with ticket_lock(self.root, self.ticket):
            _fingerprint, raw = file_fingerprint(self.source)
            if raw or self.source.exists():
                text = raw.decode("utf-8")
                if self.kind == "workflow":
                    try:
                        json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"CORRUPT state file for ticket: {self.ticket}") from exc
                return text
            if self.kind == "notes":
                return f"(no session notes for {self.ticket})\n"
            refuse(f"state file not found for ticket: {self.ticket}")

    def write(self, values):
        with ticket_lock(self.root, self.ticket):
            _fingerprint, existing_raw = file_fingerprint(self.source)
            state, _versioned = self.state()
            # Intent alone never attests the identity of bytes left by a crashed writer.
            state = {"revision": state["revision"] + 1, "pending": None, "committed": None}
            self.save_state(state)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if self.kind == "notes":
                raw = existing_raw
                if not raw and not self.source.exists():
                    raw = (f"---\nticket: {self.ticket}\ncreated: {now}\n---\n\n"
                           f"# Session Notes — {self.ticket}\n").encode()
                section, body = values
                raw += f"\n## {section} — {now}\n\n{body}\n".encode()
            else:
                workflow, step, total, status = values
                raw = (json.dumps({"ticket": self.ticket, "workflow": workflow, "step": step,
                                   "total_steps": total, "status": status, "timestamp": now},
                                  indent=2) + "\n").encode()
            published = atomic_bytes(self.source, raw)
            observed, actual = file_fingerprint(self.source)
            if observed != published or actual != raw:
                refuse("writer publication replaced; current state remains unverified")
            state["committed"] = published
            self.save_state(state)
            if self.kind == "notes":
                return f"[session-memory] Appended '{values[0]}' to {self.ticket}\n"
            return (f"[workflow-state] wrote: {self.ticket} | {values[0]} | "
                    f"step {values[1]}/{values[2]} | {values[3]} | {now}\n")

    def archive_path(self, token, revision):
        if self.archive_dir is None:
            self.archive_dir = ensure_directory(self.root / "archive", private=True)
        suffix = self.suffix
        action = "clear" if self.kind == "notes" else "archive"
        key = hashlib.sha256(token.encode()).hexdigest()[:20]
        return self.archive_dir / f"{self.ticket}_r{revision}_{key}_{action}{suffix}"

    def prepare_archive(self, destination, raw):
        ensure_directory(destination.parent, private=True)
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(destination.parent)
            fingerprint, _ = file_fingerprint(temp)
            return {"name": temp.name, **{key: fingerprint[key] for key in ("dev", "ino", "len", "sha256")}}
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def publish_archive(self, destination, pending):
        staging = pending["staging"]
        temp = destination.parent / staging["name"]
        check_ancestry(destination.parent)
        staged = temp.lstat() if temp.exists() else None
        if staged is not None:
            # Only this durably journaled staging identity may have the known second link.
            if (not stat.S_ISREG(staged.st_mode) or staged.st_uid != os.getuid()
                    or stat.S_IMODE(staged.st_mode) & 0o077 or staged.st_nlink not in (1, 2)
                    or (staged.st_dev, staged.st_ino, staged.st_size) !=
                       (staging["dev"], staging["ino"], staging["len"])):
                refuse("archive staging identity changed; preserve state")
            fd = os.open(temp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                        or (opened.st_dev, opened.st_ino) != (staged.st_dev, staged.st_ino)):
                    refuse("archive staging descriptor changed; preserve state")
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != staging["sha256"]:
                refuse("archive staging bytes changed; preserve state")
            if not destination.exists():
                os.link(temp, destination)
                fsync_directory(destination.parent)
            target = destination.lstat()
            same = (target.st_dev, target.st_ino) == (staged.st_dev, staged.st_ino)
            if same:
                if target.st_nlink != 2:
                    refuse("unexpected publication links; preserve state")
            else:
                _fingerprint, existing = file_fingerprint(destination)
                if existing != raw:
                    refuse("archive destination collision; source preserved")
            temp.unlink()
            fsync_directory(destination.parent)
        info = check_file(destination)
        if info is None or stat.S_IMODE(info.st_mode) & 0o077:
            refuse("durable archive is missing or nonprivate; source preserved")
        _fingerprint, raw = file_fingerprint(destination)
        if hashlib.sha256(raw).hexdigest() != pending["source_sha256"]:
            refuse("archive publication bytes differ; source preserved")
        fsync_directory(destination.parent)

    def finish_pending(self, state, token_hash):
        pending = state["pending"]
        if pending is None or pending.get("token_sha256") != token_hash:
            return False
        if self.kind == "notes" and pending["action"] != "clear" or self.kind == "workflow" and pending["action"] != "archive":
            refuse("archive replay action mismatch; preserve state")
        destination = self.root / pending["archive"]
        ensure_directory(destination.parent, private=True)
        self.publish_archive(destination, pending)
        info = check_file(destination)
        if info is None:
            refuse("durable archive is missing; source preserved")
        if stat.S_IMODE(info.st_mode) & 0o077:
            refuse("durable archive permissions changed; preserve state")
        _destination_fingerprint, destination_raw = file_fingerprint(destination)
        source_fingerprint, source_raw = file_fingerprint(self.source)
        if source_fingerprint is None:
            if hashlib.sha256(destination_raw).hexdigest() != pending["source_sha256"]:
                refuse("archive replay identity mismatch; preserve state")
            fsync_directory(self.root)
            return True
        if (source_fingerprint != pending["fingerprint"] or source_raw != destination_raw
                or hashlib.sha256(source_raw).hexdigest() != pending["source_sha256"]):
            refuse("archive replay source changed; preserve both copies")
        self.source.unlink()
        fsync_directory(self.root)
        return True

    def archive_or_clear(self, token):
        with ticket_lock(self.root, self.ticket):
            state, versioned = self.state()
            token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
            if token and state["pending"] and self.finish_pending(state, token_hash):
                return f"[ticket-state] {self.kind} archived for {self.ticket}\n"
            fingerprint, raw = file_fingerprint(self.source)
            if fingerprint is None:
                if token:
                    self.parse_token(token)
                    if token != self.snapshot_locked():
                        refuse("snapshot token is stale; absence generation changed")
                return f"[ticket-state] verified absent for {self.ticket}; no archive created\n"
            if not token:
                refuse("snapshot token required; existing state preserved")
            if not versioned or state["committed"] != fingerprint:
                refuse("legacy writer revision unknown; preserve existing state")
            self.parse_token(token)
            current_token = self.snapshot_locked()
            if token != current_token:
                refuse("snapshot token is stale; state preserved")
            revision = state["revision"]
            destination = self.archive_path(token, revision)
            staging = self.prepare_archive(destination, raw)
            pending = {"token_sha256": token_hash, "fingerprint": fingerprint,
                       "source_sha256": hashlib.sha256(raw).hexdigest(),
                       "archive": str(destination.relative_to(self.root)),
                       "action": "clear" if self.kind == "notes" else "archive", "staging": staging}
            self.save_state({"revision": revision + 1, "pending": pending, "committed": fingerprint})
            self.finish_pending({"pending": pending}, token_hash)
            return f"[ticket-state] {self.kind} archived for {self.ticket}\n"


def canonical_worktree(value):
    requested = Path(value)
    if not requested.is_absolute():
        refuse("worktree must be an absolute path")
    checked = check_ancestry(requested)
    resolved = checked.resolve(strict=True)
    if not resolved.is_dir():
        refuse("worktree root must be a directory")
    info = resolved.stat()
    if info.st_uid != os.getuid():
        refuse("worktree root is not owned by the current user")
    env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0"})
    try:
        result = subprocess.run(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=5, check=True)
        inside = subprocess.run(["git", "-C", str(resolved), "rev-parse", "--is-inside-work-tree"],
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("worktree is not a verified Git worktree") from exc
    git_root = Path(result.stdout.strip()).resolve(strict=True)
    if inside.stdout.strip() != "true" or git_root != resolved:
        refuse("caller path is not the Git worktree root")
    after = resolved.stat()
    if (after.st_dev, after.st_ino, after.st_uid) != (info.st_dev, info.st_ino, info.st_uid):
        refuse("worktree identity changed during Git verification")
    return resolved, info


@contextlib.contextmanager
def context_lock(root):
    path = root / ".context-slot.lock"
    check_ancestry(path.parent)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077):
            refuse("context lock ownership or permissions refused")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class ContextStore:
    """CAS reader/writer for a fresh, exclusively-created host context root."""

    def __init__(self, root):
        self.root = check_ancestry(Path(os.path.abspath(root)))
        self.object_path = self.root / ".current-context.json"

    def _root_info(self, private=True):
        check_ancestry(self.root)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or private and stat.S_IMODE(info.st_mode) & 0o077):
            refuse("context root ownership or permissions refused")
        return info

    def _legacy_markers(self):
        return [self.root / name for name in (".current-ticket", ".current-worktree", ".current-release")
                if (self.root / name).exists() or (self.root / name).is_symlink()]

    def _read(self):
        root_info = self._root_info()
        fingerprint, raw = file_fingerprint(self.object_path)
        if fingerprint is None:
            return None
        info = check_file(self.object_path)
        if info is None or stat.S_IMODE(info.st_mode) & 0o077:
            refuse("context object permissions refused")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("context object is invalid; preserve state") from exc
        if (not isinstance(value, dict) or set(value) != CONTEXT_FIELDS
                or type(value.get("schema")) is not int or value["schema"] != CONTEXT_SCHEMA
                or value.get("owner") != CONTEXT_OWNER or value.get("reader_contract") != CONTEXT_READER
                or not isinstance(value.get("scope_id"), str) or not isinstance(value.get("generation"), str)
                or not isinstance(value.get("state"), str)
                or value["state"] not in ("active", "cleared")
                or any(type(value.get(key)) is not int or value[key] < 0 for key in
                       ("root_dev", "root_ino", "worktree_dev", "worktree_ino"))):
            refuse("context object schema or identity is unsupported; preserve state")
        try:
            if (str(uuid.UUID(value["scope_id"])) != value["scope_id"]
                    or str(uuid.UUID(value["generation"])) != value["generation"]):
                raise ValueError("noncanonical UUID")
        except (ValueError, AttributeError) as exc:
            raise ValueError("context object identifiers are invalid; preserve state") from exc
        release = value.get("release")
        if release is not None and (not isinstance(release, str) or not release or len(release) > 128
                                    or any(ord(char) < 32 for char in release)):
            refuse("context release field is malformed; preserve state")
        if ((root_info.st_dev, root_info.st_ino) != (value["root_dev"], value["root_ino"])
                or self._root_info().st_ino != root_info.st_ino):
            refuse("context root identity changed; preserve state")
        host = self.root.name.removeprefix(".")
        if host not in ("claude", "codex"):
            refuse("context host root is unsupported")
        if value["state"] == "active":
            if (not isinstance(value.get("ticket"), str) or not TICKET_RE.fullmatch(value["ticket"])
                    or not isinstance(value.get("worktree"), str)
                    or not Path(value["worktree"]).is_absolute()):
                refuse("active context tuple is malformed; preserve state")
            worktree, worktree_info = canonical_worktree(value["worktree"])
            if (str(worktree) != value["worktree"]
                    or (worktree_info.st_dev, worktree_info.st_ino) !=
                       (value["worktree_dev"], value["worktree_ino"])
                    or self.root != worktree / f".{host}"):
                refuse("context tuple or worktree identity changed; preserve state")
        elif any(value.get(key) is not None for key in ("ticket", "worktree", "release")):
            refuse("cleared context contains a tuple; preserve state")
        stable = self._root_info()
        if (stable.st_dev, stable.st_ino) != (value["root_dev"], value["root_ino"]):
            refuse("context root identity changed; preserve state")
        return value, fingerprint

    @staticmethod
    def _token(value, fingerprint):
        payload = {"schema": CONTEXT_SCHEMA, "scope_id": value["scope_id"],
                   "generation": value["generation"], "root_dev": value["root_dev"],
                   "root_ino": value["root_ino"], "fingerprint": fingerprint}
        encoded = base64.urlsafe_b64encode(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        return "ctx1." + encoded

    @classmethod
    def _receipt(cls, value, fingerprint):
        return {"status": "managed", "state": value["state"], "scope_id": value["scope_id"],
                "generation": value["generation"], "ticket": value["ticket"],
                "worktree": value["worktree"], "release": value["release"],
                "token": cls._token(value, fingerprint)}

    def inspect(self):
        try:
            try:
                self.root.lstat()
            except FileNotFoundError:
                return {"status": "legacy_uncoordinated", "ticket": None, "worktree": None,
                        "release": None}
            item = self._read()
            if item is None:
                return {"status": "legacy_uncoordinated", "ticket": None, "worktree": None,
                        "release": None}
            value, fingerprint = item
            return {"status": "managed", "state": value["state"], "scope_id": value["scope_id"],
                    "generation": value["generation"], "ticket": value["ticket"],
                    "worktree": value["worktree"], "release": value["release"],
                    "token": self._token(value, fingerprint)}
        except (OSError, ValueError):
            return {"status": "unavailable", "ticket": None, "worktree": None, "release": None}

    def initialize(self, host, worktree, ticket, release=None, fresh_owned_scope=False):
        if not fresh_owned_scope:
            refuse("--fresh-owned-scope is required")
        if host not in ("claude", "codex") or not TICKET_RE.fullmatch(ticket):
            refuse("host or ticket is malformed")
        worktree, worktree_info = canonical_worktree(worktree)
        expected_root = worktree / f".{host}"
        if self.root != expected_root:
            refuse("context root must be the canonical worktree host directory")
        if release is not None and (not release or len(release) > 128 or any(ord(c) < 32 for c in release)):
            refuse("release value is malformed")
        try:
            os.mkdir(self.root, 0o700)
        except FileExistsError as exc:
            raise ValueError("existing context root is legacy/unowned; adoption refused") from exc
        fsync_directory(worktree)
        root_info = self._root_info()
        verified_worktree, verified_info = canonical_worktree(worktree)
        if (verified_worktree != worktree or (verified_info.st_dev, verified_info.st_ino) !=
                (worktree_info.st_dev, worktree_info.st_ino)):
            refuse("worktree identity changed during context initialization; preserve orphan root")
        lock_path = self.root / ".context-slot.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        value = {"schema": CONTEXT_SCHEMA, "owner": CONTEXT_OWNER,
                 "scope_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()), "state": "active",
                 "ticket": ticket, "worktree": str(worktree), "release": release,
                 "root_dev": root_info.st_dev, "root_ino": root_info.st_ino,
                 "worktree_dev": worktree_info.st_dev, "worktree_ino": worktree_info.st_ino,
                 "reader_contract": CONTEXT_READER}
        with context_lock(self.root):
            current_root = self._root_info()
            if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                refuse("context root identity changed during initialization; preserve orphan root")
            fingerprint = atomic_bytes(
                self.object_path,
                (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
            receipt = self._receipt(value, fingerprint)
        return receipt

    def _change(self, action, generation, token, ticket=None, worktree=None, release=None):
        with context_lock(self.root):
            item = self._read()
            if item is None:
                refuse("managed context object is absent; preserve state")
            value, fingerprint = item
            if (not isinstance(generation, str) or generation != value["generation"]
                    or not isinstance(token, str) or token != self._token(value, fingerprint)):
                refuse("context snapshot is stale; preserve state")
            if action == "clear" and value["state"] == "cleared":
                return self._receipt(value, fingerprint)
            if self._legacy_markers():
                refuse("legacy context markers are present; preserve state")
            if action == "publish":
                if value["state"] != "active":
                    refuse("cleared context cannot be republished; initialize a fresh scope")
                if not isinstance(ticket, str) or not TICKET_RE.fullmatch(ticket):
                    refuse("ticket is malformed")
                canonical, info = canonical_worktree(worktree or "")
                if str(canonical) != value["worktree"] or self.root != canonical / f".{self.root.name.removeprefix('.')}":
                    refuse("publish worktree does not match the managed scope")
                if ticket != value["ticket"]:
                    refuse("active ticket handover is unsupported; preserve context")
                value.update(ticket=ticket, release=release,
                             worktree_dev=info.st_dev, worktree_ino=info.st_ino)
            else:
                value.update(state="cleared", ticket=None, worktree=None, release=None)
            value["generation"] = str(uuid.uuid4())
            current_root = self._root_info()
            if (current_root.st_dev, current_root.st_ino) != (value["root_dev"], value["root_ino"]):
                refuse("context root identity changed before publication; preserve state")
            fingerprint = atomic_bytes(
                self.object_path,
                (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
            receipt = self._receipt(value, fingerprint)
        return receipt

    def publish(self, generation, token, ticket, worktree, release=None):
        if release is not None and (not release or len(release) > 128 or any(ord(c) < 32 for c in release)):
            refuse("release value is malformed")
        return self._change("publish", generation, token, ticket, worktree, release)

    def clear(self, generation, token):
        return self._change("clear", generation, token)


def run(args):
    kind, root, action, ticket = args.kind, Path(args.root), args.action, args.ticket
    store = TicketStore(kind, root, ticket)
    if action == "read":
        print(store.read(), end="")
    elif action == "snapshot":
        with ticket_lock(store.root, ticket):
            print(store.snapshot_locked())
    elif action == "append" and kind == "notes":
        print(store.write((args.section, args.body)), end="", file=__import__("sys").stderr)
    elif action == "write" and kind == "workflow":
        print(store.write((args.workflow, args.step, args.total, args.status)), end="")
    elif action in {"clear", "archive"}:
        output = store.archive_or_clear(args.token)
        print(output, end="", file=sys.stderr if kind == "notes" else sys.stdout)
    else:
        refuse(f"unsupported {kind} operation: {action}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "context":
        context_main(argv[1:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("notes", "workflow"))
    parser.add_argument("root")
    parser.add_argument("action", choices=("read", "append", "snapshot", "clear", "write", "archive"))
    parser.add_argument("ticket")
    parser.add_argument("args", nargs="*")
    values = parser.parse_args(argv)
    if values.action == "append" and values.kind == "notes" and len(values.args) == 2:
        values.section, values.body = values.args
    elif values.action == "write" and values.kind == "workflow" and len(values.args) == 4:
        values.workflow, values.step, values.total, values.status = values.args
    elif values.action in {"clear", "archive"} and len(values.args) <= 1:
        values.token = values.args[0] if values.args else ""
    elif values.action in {"read", "snapshot"} and not values.args:
        pass
    else:
        parser.error("invalid arguments for ticket-state operation")
    try:
        run(values)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ticket-state: refused: {exc}\n")


def context_main(argv):
    parser = argparse.ArgumentParser(prog="ticket-state.py context")
    parser.add_argument("root")
    parser.add_argument("action", choices=("initialize", "inspect", "publish", "clear"))
    parser.add_argument("--host", choices=("claude", "codex"))
    parser.add_argument("--worktree")
    parser.add_argument("--ticket")
    parser.add_argument("--release")
    parser.add_argument("--fresh-owned-scope", action="store_true")
    parser.add_argument("--expected-generation")
    parser.add_argument("--expected-token")
    args = parser.parse_args(argv)
    try:
        store = ContextStore(args.root)
        if args.action == "inspect":
            result = store.inspect()
        elif args.action == "initialize":
            if not args.host or not args.worktree or not args.ticket:
                parser.error("initialize requires --host, --worktree, and --ticket")
            result = store.initialize(args.host, args.worktree, args.ticket, args.release,
                                      args.fresh_owned_scope)
        elif args.action == "publish":
            if not all((args.worktree, args.ticket, args.expected_generation, args.expected_token)):
                parser.error("publish requires --worktree, --ticket, --expected-generation, and --expected-token")
            result = store.publish(args.expected_generation, args.expected_token, args.ticket,
                                   args.worktree, args.release)
        else:
            if not args.expected_generation or not args.expected_token:
                parser.error("clear requires --expected-generation and --expected-token")
            result = store.clear(args.expected_generation, args.expected_token)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ticket-state context: refused: {exc}\n")


if __name__ == "__main__":
    main()
