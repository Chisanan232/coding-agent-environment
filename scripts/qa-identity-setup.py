#!/usr/bin/python3 -I
"""Owner-run setup of a dedicated, unprivileged QA OS identity (SPE-141). An agent never runs this.

    /usr/bin/python3 -I scripts/qa-identity-setup.py --name NAME --uid UID            set up / repair, then verify isolation
    /usr/bin/python3 -I scripts/qa-identity-setup.py --name NAME --uid UID --check    read-only validation (no change)
    /usr/bin/python3 -I scripts/qa-identity-setup.py --name NAME --uid UID --rollback remove what this script manages

Why: a file permission profile cannot stop a QA tool command from reading the environment of other processes of the SAME
OS user (sysctl KERN_PROCARGS2, kernel-enforced per uid). A guarantee that credentials are unreachable needs the QA run to be
a different OS user. This generalizes the AASM setup (setup_aasmqa.py, reviewed there) without its AASM-specific pieces.

Scope, exactly: the local user and group NAME (id UID), /Users/NAME (mode 0700) and the exchange directory
(default /Users/Shared/NAME-qa). It never edits sudoers, never logs in to Codex, never starts a model session, never reads or
copies any credential, and never touches any other user, process, session or file. Anything unexpected stops it (fail
closed). Re-running is safe: each step reads the current state first and changes only what differs.
"""
import argparse, json, os, platform, pwd, re, secrets, shlex, stat, subprocess, sys
from pathlib import Path

# Constants, not environment: nothing here can be redirected by the caller's environment. Tests replace them on the module.
SUDO, DSCL, DSEDIT, SYSADMIN = "/usr/bin/sudo", "/usr/bin/dscl", "/usr/sbin/dseditgroup", "/usr/sbin/sysadminctl"
ID, LS, CHMOD = "/usr/bin/id", "/bin/ls", "/bin/chmod"
MKDIR, RMDIR, CHOWN, RM = "/bin/mkdir", "/bin/rmdir", "/usr/sbin/chown", "/bin/rm"
PY = "/usr/bin/python3"
ROOT_UID = 0
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
RESERVED = {"root", "admin", "daemon", "nobody", "guest", "shared", "wheel", "staff", "everyone", "_www"}
UID_MIN, UID_MAX = 500, 999
# Allowlist: every group the QA account may be in. A fresh macOS user also carries the sharepoint groups and _lpoperator.
OK_GROUPS = {"everyone", "localaccounts", "_lpoperator"}
OK_GROUP_RE = re.compile(r"^com\.apple\.sharepoint\.group\.\d+$")
PRIV_GIDS = {0, 20, 80}  # wheel, staff, admin
ACL_PERMS = "list,search,readattr,readextattr,readsecurity,file_inherit,directory_inherit"  # how macOS prints it
ACL_SET = "list,search,read,readattr,readextattr,readsecurity,file_inherit,directory_inherit"
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
CODEX = "/opt/homebrew/bin/codex"
DENIED = {"EACCES", "EPERM"}
VERIFY_TIMEOUT = 300


class Stop(Exception):
    pass


def say(msg):
    print(msg, flush=True)


def die(step, msg):
    raise Stop("[%s] %s" % (step, msg))


# ---------------------------------------------------------------- pure checks (unit-tested)
def check_args(name, uid, xroot, home_root=Path("/Users")):
    """Validate the three owner-supplied values before anything is read or changed. Returns the paths."""
    if not NAME_RE.match(name) or name in RESERVED or name.startswith("_"):
        die("args", "--name must be 3-31 characters: a lowercase letter then lowercase letters, digits or _, and not a reserved name")
    if not isinstance(uid, int) or not UID_MIN <= uid <= UID_MAX:
        die("args", "--uid must be an integer between %d and %d" % (UID_MIN, UID_MAX))
    home = home_root / name
    xroot = Path(xroot) if xroot else Path("/Users/Shared") / (name + "-qa")
    if not xroot.is_absolute() or ".." in xroot.parts or xroot.parent != Path("/Users/Shared"):
        die("args", "--exchange must be a direct child of /Users/Shared")
    if xroot.name in ("", "Shared") or home == xroot:
        die("args", "--exchange is not usable")
    return home, xroot


def acl_entries(ls_output):
    """The ACE text of `ls -led` output, one string per entry."""
    return [m.group(1).strip() for m in (re.match(r"^\s*\d+:\s+(.*)$", line) for line in ls_output.splitlines()) if m]


def groups_ok(group_names, name):
    extra = {g for g in group_names if g != name and g not in OK_GROUPS and not OK_GROUP_RE.match(g)}
    return sorted(extra)


def evaluate_probe(o, uid):
    """Pure check of the isolation probe run AS the QA user. A denial counts only if it is an explicit permission error
    (not ENOENT, not a crash); the environment control requires the detector to work on the QA user's own child first."""
    fails = []
    if o.get("uid") != uid or o.get("gid") != uid:
        fails.append("effective uid/gid %s/%s != %s/%s" % (o.get("uid"), o.get("gid"), uid, uid))
    if o.get("ctty") == "ok":
        fails.append("the QA process still has the owner's controlling terminal (/dev/tty opened)")
    groups = o.get("groups")
    if not isinstance(groups, list) or set(groups) & PRIV_GIDS:
        fails.append("the QA process carries a privileged group: %s" % (groups,))
    for k in ("list_inbox", "write_outbox"):
        if o.get(k) != "ok":
            fails.append("%s should work for the QA user, got %s" % (k, o.get(k)))
    for k in ("write_inbox", "write_exchange", "list_producer_home", "stat_producer_auth", "open_producer_ssh"):
        if o.get(k) not in DENIED:
            fails.append("%s must be denied with EACCES/EPERM, got %s" % (k, o.get(k)))
    if o.get("own_env_read") != "ok":
        fails.append("environment detector invalid: the QA user could not read its OWN child's environment (%s)" % o.get("own_env_read"))
    if o.get("foreign_env_leaked") is not False:
        fails.append("the QA user could read the producer's process environment (sentinel %s)" % o.get("foreign_env_leaked"))
    return fails


# The probe runs as the QA user with this tiny script (-I: isolated). It only tests access; it never reads a credential.
PROBE = r"""
import ctypes, ctypes.util, errno, json, os, subprocess, sys, struct
inbox, outbox, xroot, prodhome, pid, sentinel = sys.argv[1:7]
def tryit(f):
    try: f(); return "ok"
    except OSError as e: return errno.errorcode.get(e.errno, str(e.errno))
def wr(d):
    p = os.path.join(d, ".probe-" + str(os.getpid()))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd); os.unlink(p)
def procenv(p):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, int(p))
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]
mine = "mine-" + os.urandom(6).hex()
child = subprocess.Popen(["/bin/sleep", "30"], env={"PATH": "/usr/bin:/bin", "QA_OWN_MARK": mine})
own = procenv(child.pid)
own_ok = "ok" if own is not None and mine.encode() in own else "unreadable"
child.kill(); child.wait()
foreign = procenv(pid)
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": sorted(os.getgroups()),
  "ctty": tryit(lambda: os.close(os.open("/dev/tty", os.O_RDWR))),
  "list_inbox": tryit(lambda: os.listdir(inbox)), "write_inbox": tryit(lambda: wr(inbox)),
  "write_outbox": tryit(lambda: wr(outbox)), "write_exchange": tryit(lambda: wr(xroot)),
  "list_producer_home": tryit(lambda: os.listdir(prodhome)),
  "stat_producer_auth": tryit(lambda: os.stat(os.path.join(prodhome, ".codex", "auth.json"))),
  "open_producer_ssh": tryit(lambda: os.listdir(os.path.join(prodhome, ".ssh"))),
  "own_env_read": own_ok, "foreign_env_leaked": (foreign is not None and sentinel.encode() in foreign)}
print(json.dumps(out))
"""

# sudo's credential cache is keyed to the owner's terminal, so sudo itself keeps it; the QA child drops it (new session, cwd "/")
# before running anything, so it cannot inject keystrokes into the owner's shell or open /dev/tty. argv[1] is a wall-clock limit in
# seconds (SIGALRM armed in the child, survives exec). The waiting parent forwards INT/TERM/HUP to the setsid-ed child.
DETACH = ("import os,sys,signal\npid=os.fork()\nif pid:\n"
          " for s in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(s, lambda n,f: os.kill(pid, n))\n"
          " _,st=os.waitpid(pid,0)\n sys.exit(os.waitstatus_to_exitcode(st))\n"
          "os.setsid()\nos.chdir('/')\nn=int(sys.argv[1])\nif n: signal.alarm(n)\nos.execv(sys.argv[2], sys.argv[2:])")


def qa_argv(name, home, argv, alarm=VERIFY_TIMEOUT + 60):
    """The exact command that runs `argv` as the QA user: no inherited environment, fixed PATH, detached from the terminal."""
    return [SUDO, "-n", "-u", name, "-H", "/usr/bin/env", "-i", "HOME=%s" % home, "PATH=%s" % SAFE_PATH,
            PY, "-I", "-c", DETACH, str(alarm), *argv]


# ---------------------------------------------------------------- command runner
def run(argv, check=True, sudo=False, tty=False, timeout=120, input_=None):
    cmd = ([SUDO, "-n"] if sudo else []) + list(argv)
    r = subprocess.run(cmd, stdin=None if tty else subprocess.DEVNULL if input_ is None else None, input=input_,
                       capture_output=not tty, text=not isinstance(input_, bytes), timeout=timeout)
    if check and r.returncode != 0:
        raise Stop("command failed (%s): %s\n%s" % (r.returncode, " ".join(argv), (r.stderr if isinstance(r.stderr, str) else "")[-400:]))
    return r


def sudo_refresh():
    """Asks for the sudo password only when the cached credential has expired."""
    if subprocess.run([SUDO, "-n", "-v"], capture_output=True).returncode != 0:
        say("sudo password needed (used only for the fixed commands this script prints):")
        if subprocess.run([SUDO, "-v"]).returncode != 0:
            die("sudo", "sudo authentication failed")


def dscl(path, key):
    r = run([DSCL, ".", "-read", path, key], check=False)
    if r.returncode != 0:
        return None
    m = re.search(r"%s:\s*(.*)" % re.escape(key), r.stdout)
    return m.group(1).strip() if m else None


class Setup:
    """One identity. `state` records what THIS run created so a failed first setup undoes only that."""

    def __init__(self, name, uid, home, xroot):
        self.name, self.uid, self.home, self.xroot = name, uid, home, xroot
        self.state = {"created_user": False, "created_group": False}

    # ---- account
    def ensure_account(self, apply):
        n, u = self.name, str(self.uid)
        gid_g, uid_u = dscl("/Groups/%s" % n, "PrimaryGroupID"), dscl("/Users/%s" % n, "UniqueID")
        if gid_g is None and run([DSCL, ".", "-search", "/Groups", "PrimaryGroupID", u], check=False).stdout.strip():
            die("account", "gid %s is already used by another group; refusing to touch it" % u)
        if gid_g not in (None, u):
            die("account", "group %s exists with gid %s, expected %s; refusing to modify it" % (n, gid_g, u))
        if uid_u is None and run([DSCL, ".", "-search", "/Users", "UniqueID", u], check=False).stdout.strip():
            die("account", "uid %s is already used by another user; refusing to touch it" % u)
        if uid_u not in (None, u):
            die("account", "user %s exists with uid %s, expected %s; refusing to modify it" % (n, uid_u, u))
        if gid_g is None:
            if not apply:
                die("account", "group missing (check mode)")
            say("creating group %s (gid %s)" % (n, u))
            self.state["created_group"] = True
            run([DSEDIT, "-o", "create", "-i", u, n], sudo=True)
        if uid_u is None:
            if not apply:
                die("account", "user missing (check mode)")
            if os.path.lexists(self.home):
                die("account", "%s already exists without the account record; refusing to reuse a stale home (inspect it, then remove it yourself)" % self.home)
            self.state["created_user"] = True  # set first so a half-created account is undone, never adopted
            say("creating standard user %s (uid %s); type any throwaway password when asked, it is never needed again" % (n, u))
            sudo_refresh()
            run([SUDO, SYSADMIN, "-addUser", n, "-UID", u, "-GID", u, "-fullName", "QA identity %s" % n,
                 "-shell", "/bin/zsh", "-password", "-"], tty=True, timeout=300)
        if apply and dscl("/Users/%s" % n, "IsHidden") != "1":
            run([DSCL, ".", "-create", "/Users/%s" % n, "IsHidden", "1"], sudo=True)
        self.validate_account(apply)

    def validate_account(self, apply):
        n, u = self.name, str(self.uid)
        if dscl("/Users/%s" % n, "UniqueID") != u or dscl("/Users/%s" % n, "PrimaryGroupID") != u:
            die("account", "uid/gid are not %s/%s" % (u, u))
        if dscl("/Groups/%s" % n, "PrimaryGroupID") != u:
            die("account", "private group missing or wrong gid")
        if dscl("/Users/%s" % n, "NFSHomeDirectory") != str(self.home):
            die("account", "home is not %s" % self.home)
        extra = groups_ok(set(run([ID, "-Gn", n]).stdout.split()), n)
        if extra:
            die("account", "%s is a member of groups outside the allowlist: %s" % (n, extra))
        if "no" not in run([DSEDIT, "-o", "checkmember", "-m", n, "admin"], check=False).stdout.lower().split():
            die("account", "%s is an administrator" % n)
        sl = run([SUDO, "-n", "-l", "-U", n], check=False)
        if "not allowed to run sudo" not in (sl.stdout + sl.stderr):
            die("account", "%s has sudo rights: %s" % (n, (sl.stdout + sl.stderr).strip()[:200]))
        if self.home.is_symlink() or not self.home.is_dir():
            die("account", "%s missing or a symlink" % self.home)
        st = os.lstat(self.home)
        if st.st_uid != self.uid:
            die("account", "%s is owned by uid %s, expected %s" % (self.home, st.st_uid, self.uid))
        if st.st_mode & 0o077:
            if not apply:
                die("account", "%s is mode %s, expected 0700 (check mode)" % (self.home, oct(st.st_mode & 0o777)))
            run([CHMOD, "700", str(self.home)], sudo=True)
            if os.lstat(self.home).st_mode & 0o077:
                die("account", "could not make %s private" % self.home)
        say("ok  account %s: uid/gid %s, own private group, not admin, no sudo rights, private home" % (n, u))

    # ---- exchange area
    def ensure_dir(self, p, owner, group, uid, mode, apply):
        """Create (mkdir fails on an existing symlink) or verify-then-chmod; ownership is set only on a directory we just made."""
        if os.path.lexists(p):
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                die("exchange", "%s is a symlink or not a directory" % p)
            if st.st_uid != uid:
                die("exchange", "%s is owned by uid %s, expected %s; refusing to change it" % (p, st.st_uid, uid))
            if (st.st_mode & 0o7777) != int(mode, 8):
                if not apply:
                    die("exchange", "%s is mode %s, expected %s (check mode)" % (p, oct(st.st_mode & 0o7777), mode))
                run([CHMOD, mode, str(p)], sudo=True)
        else:
            if not apply:
                die("exchange", "%s missing (check mode)" % p)
            run([MKDIR, "-m", mode, str(p)], sudo=True)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                die("exchange", "%s was replaced by a non-directory while being created" % p)
            run([CHOWN, "%s:%s" % (owner, group), str(p)], sudo=True)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != uid or (st.st_mode & 0o7777) != int(mode, 8):
            die("exchange", "%s is not %s owner uid %s after setup" % (p, mode, uid))

    def ensure_acl(self, p, user, apply, empty_required=False):
        want = "user:%s allow %s" % (user, ACL_PERMS)
        have = acl_entries(run([LS, "-led", str(p)]).stdout)
        if have == [want]:
            return
        if have:
            die("exchange", "unexpected ACL on %s: %s; refusing to alter it (use --rollback or inspect it)" % (p, have))
        if not apply:
            die("exchange", "ACL for %s missing on %s (check mode)" % (user, p))
        if empty_required and os.listdir(p):
            die("exchange", "%s already has content that would not inherit the ACL; empty it first" % p)
        run([CHMOD, "+a", "user:%s allow %s" % (user, ACL_SET), str(p)], sudo=True)
        have = acl_entries(run([LS, "-led", str(p)]).stdout)
        if have != [want]:
            die("exchange", "ACL on %s is %s, expected exactly %s" % (p, have, [want]))

    def ensure_no_acl(self, p, apply):
        if not acl_entries(run([LS, "-led", str(p)]).stdout):
            return
        if not apply:
            die("exchange", "unexpected ACL on %s (check mode)" % p)
        run([CHMOD, "-N", str(p)], sudo=True)
        if acl_entries(run([LS, "-led", str(p)]).stdout):
            die("exchange", "could not clear the ACL on %s" % p)

    def ensure_exchange(self, apply, producer, pw):
        shared = Path("/Users/Shared")
        if shared.is_symlink():
            die("exchange", "/Users/Shared is a symlink")
        self.ensure_dir(self.xroot, "root", "wheel", ROOT_UID, "0711", apply)
        self.ensure_no_acl(self.xroot, apply)
        inbox, outbox = self.xroot / "inbox", self.xroot / "outbox"
        pgroup = run([ID, "-gn"]).stdout.strip()
        self.ensure_dir(inbox, producer, pgroup, pw.pw_uid, "0700", apply)
        self.ensure_acl(inbox, self.name, apply, empty_required=True)
        self.ensure_dir(outbox, self.name, self.name, self.uid, "0700", apply)
        self.ensure_acl(outbox, producer, apply)
        say("ok  exchange area %s: root-owned, private inbox (you write, the QA user reads) and outbox (the QA user writes, you read)" % self.xroot)

    # ---- isolation probe, run as the QA user (access tests only)
    def probe_isolation(self, pw):
        sudo_refresh()
        sentinel = "qa-sentinel-" + secrets.token_hex(16)  # random, synthetic: not a credential
        # the sibling process of the PRODUCER's user that holds the sentinel in its environment
        sib = subprocess.Popen([PY, "-I", "-c", "import time; time.sleep(120)"], env={"PATH": "/usr/bin:/bin", "QA_SENTINEL": sentinel},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            r = run(qa_argv(self.name, self.home, [PY, "-I", "-c", PROBE, str(self.xroot / "inbox"), str(self.xroot / "outbox"),
                                                    str(self.xroot), pw.pw_dir, str(sib.pid), sentinel]), check=False, timeout=VERIFY_TIMEOUT)
            try:
                o = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                die("isolation", "QA-user probe did not run (rc %s): %s" % (r.returncode, (r.stderr or r.stdout)[-300:]))
            fails = evaluate_probe(o, self.uid)
            if fails:
                die("isolation", "; ".join(fails))
            if run(["/bin/ls", str(self.xroot / "outbox")], check=False).returncode != 0:
                die("isolation", "the owner cannot read the outbox (ACL not effective)")
        finally:
            sib.kill()
            sib.wait()
        say("ok  isolation as uid %s: reads inbox, writes outbox only; cannot list your home or reach .codex/.ssh; "
            "cannot read your processes' environment (detector validated on its own child first)" % self.uid)

    # ---- rollback
    def rollback(self, only_created=False):
        n, u = self.name, str(self.uid)
        if not only_created:
            if input("Type %s to remove the user, its group and %s: " % (n, self.xroot)).strip() != n:
                die("rollback", "not confirmed; nothing changed")
        sudo_refresh()
        have_user = dscl("/Users/%s" % n, "UniqueID") == u
        if dscl("/Users/%s" % n, "UniqueID") not in (None, u):
            die("rollback", "user %s does not have uid %s; refusing to delete it" % (n, u))
        if have_user and (not only_created or self.state["created_user"]):
            # sysadminctl deletes the home directory named in the record: never run it unless that is exactly our private home
            st = os.lstat(self.home) if os.path.lexists(self.home) else None
            if (dscl("/Users/%s" % n, "NFSHomeDirectory") != str(self.home) or dscl("/Users/%s" % n, "PrimaryGroupID") != u
                    or (st is not None and (stat.S_ISLNK(st.st_mode) or st.st_uid != self.uid))):
                die("rollback", "the %s record does not point at a private %s owned by uid %s; not deleting it automatically "
                                "(remove it yourself after inspecting it)" % (n, self.home, u))
            run([SYSADMIN, "-deleteUser", n], sudo=True, check=False)
        if dscl("/Groups/%s" % n, "PrimaryGroupID") == u and (not only_created or self.state["created_group"]):
            run([DSEDIT, "-o", "delete", n], sudo=True, check=False)
        if not only_created:
            if self.xroot.is_symlink():
                die("rollback", "%s is a symlink; not removing" % self.xroot)
            run([RM, "-rf", str(self.xroot)], sudo=True)
        say("rollback finished; verify with: dscl . -read /Users/%s ; ls -d %s %s" % (n, self.home, self.xroot))

    def login_command(self):
        """Printed for the owner; this script never logs in. Same detach hop as the probe: a plain `sudo -u` would hand the
        QA process the owner's controlling terminal (TIOCSTI keystroke injection into the owner's shell)."""
        return shlex.join(qa_argv(self.name, self.home, [CODEX, "login", "--device-auth"], alarm=900))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--uid", required=True, type=int)
    ap.add_argument("--exchange", help="exchange directory, a direct child of /Users/Shared (default /Users/Shared/NAME-qa)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args(argv)
    try:
        if platform.system() != "Darwin":
            die("preflight", "macOS only")
        if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
            die("preflight", "run this as your normal user, not with sudo; it asks for sudo itself when needed")
        home, xroot = check_args(a.name, a.uid, a.exchange)
        pw = pwd.getpwuid(os.getuid())
        if a.name == pw.pw_name:
            die("args", "--name is your own account")
        if run([DSEDIT, "-o", "checkmember", "-m", pw.pw_name, "admin"], check=False).stdout.lower().split()[:1] != ["yes"]:
            die("preflight", "your account must be an administrator to use sudo")
        sudo_refresh()
        s = Setup(a.name, a.uid, home, xroot)
        if a.rollback:
            s.rollback()
            return 0
        apply = not a.check
        try:
            s.ensure_account(apply)
            s.ensure_exchange(apply, pw.pw_name, pw)
            s.probe_isolation(pw)
        except BaseException as e:
            orig = e if isinstance(e, Stop) else Stop("[internal] %s: %s" % (type(e).__name__, e))
            if s.state["created_user"] or s.state["created_group"]:
                say("setup failed after creating the account in this run; undoing only what this run created")
                try:
                    s.rollback(only_created=True)
                except Stop as e2:
                    raise Stop("%s  (undo also stopped: %s)" % (orig, e2))
            raise orig
        say("DONE: the QA identity %s is set up and its isolation verified. No Codex login or model run happened." % a.name)
        say("Next, as a separate owner step (a Codex device-code login as that user only; run `sudo -K` in the same Terminal afterwards):")
        say("    %s" % s.login_command())
        say("Verify at any time, read-only: /usr/bin/python3 -I %s --name %s --uid %s --check" % (Path(__file__).resolve(), a.name, a.uid))
        return 0
    except Stop as e:
        print("STOPPED %s\nSteps completed before this point stay in place and are safe to re-run (the script is idempotent). "
              "To remove everything it manages: /usr/bin/python3 -I %s --name %s --uid %s --rollback"
              % (e, Path(__file__).resolve(), a.name, a.uid), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as e:
        print("STOPPED timeout: %s" % (e.cmd,), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
