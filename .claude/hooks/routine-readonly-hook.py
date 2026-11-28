#!/usr/bin/env python3
"""PreToolUse hook: let provably read-only routine work run without a prompt (SPE-119).

It reads one hook event on stdin and prints nothing (fall through to the normal decision), `allow` (provably read-only), or `ask` (a
credential-store read, a process kill, a recursive or forced delete, a git worktree change). It NEVER prints deny. An existing ask or deny
rule still wins over a hook allow (measured, see docs/ROUTINE-COMMAND-AUTONOMY.md). Any error prints nothing and exits 0.

Covered tools
  Bash, Monitor  the command is split into simple commands (; && || | newline, until/while/for/if bodies) and EVERY stage must be a
                 known read-only operation judged by what it does (argv[0] after parsing, its flags and its paths), not by a name or a
                 prefix. `rtk <verb>` forms are judged like the native verb; `rtk proxy` and `rtk run` are never allowed.
  Agent          dispatch of a built-in read-only subagent type, or of general-purpose when the prompt declares READ-ONLY. The subagent's own tool
                 calls still go through this hook and the normal permission rules, so dispatch grants no write power.

Rejected (fall through): output redirection other than /dev/null, command substitution, backticks, process substitution, heredocs,
background `&`, subshells, env-prefix assignments, unknown variables, xargs/tee/env/ps, any write or exec flag (sed -i / w / e,
find -exec / -delete, sort -o, awk system()/pipes/redirects, rg --pre), and any path that looks like a credential store (see SECRET).
Read-only does not mean harmless to read: the SECRET list keeps credentials, key material, shell history and home-level Claude Code
configuration out of unattended reads.
"""
import glob
import itertools
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LOG = os.environ.get("ROUTINE_READONLY_HOOK_LOG") or os.path.join(HOME, ".cache/coding-agent-environment/routine-hook/decisions.jsonl")

# --------------------------------------------------------------------------- secret paths
_SECRET_RE = re.compile("|".join([
    r"(^|/)[^/]*\.env(rc)?$", r"(^|/)\.(vault-token|boto|s3cfg|oci|gradle|m2|zsh_sessions|bash_sessions|aws-vault|cargo/credentials(\.toml)?)(/|$)",
    r"(^|/)\.(codex|gemini|wrangler|stripe|vercel|composer|ngrok2?|bundle|huggingface|databrickscfg|snowsql|pnpmrc|sentryclirc|dockercfg|openai|fly|cursor|windsurf|mysql_history|rediscli_history|sqlite_history|irb_history|viminfo)(/|$)",
    r"(^|/)\.cache/(gh|gcloud)(/|$)", r"(^|/)(gradle\.properties|settings\.xml|\.cargo/credentials)$", r"(^|/)\.env\.(?!(example|sample|template|dist|defaults?)$)[^/]+$",
    r"(^|/)\.(ssh|aws|gnupg|kube|azure|docker|terraform\.d|op)(/|$)", r"(^|/)\.config(/|$)", r"(^|/)\.local/share/(keyrings|gh)(/|$)",
    r"(^|/)\.(netrc|npmrc|yarnrc|pypirc|pgpass|git-credentials|terraformrc|my\.cnf|gitconfig|envrc)$",
    r"(^|/)\.git/(config|credentials)$", r"(^|/)\.claude/settings\.local\.json$",
    r"\.(pem|key|p12|pfx|jks|keystore|ovpn|tfvars|tfstate|kdbx)$", r"(^|/)id_(rsa|dsa|ecdsa|ed25519)[^/]*$",
    r"(^|/)(credentials|kubeconfig|secrets?)(\.[a-z]+)?$", r"(^|/)[^/]*(token|apikey|api_key|passwd|password|secret)s?\.(txt|json|ya?ml|env|toml|key)$",
    r"(^|/)\.(zshrc|zshenv|zprofile|zlogin|bashrc|bash_profile|bash_login|profile|zsh_history|bash_history|python_history|psql_history|node_repl_history|lesshst)$",
    r"(^|/)Library/(Keychains|Cookies|Safari|Mail|Messages)(/|$)", r"(^|/)Library/Application Support/(Google|Claude|1Password|Firefox|Arc|Slack)(/|$)",
    r"^/(private/)?etc/(shadow|sudoers|master\.passwd)", r"^/proc/", r"^/dev/(?!null$)",
]), re.I)
_HOME_CLAUDE = (os.path.join(HOME, ".claude"), os.path.join(HOME, ".claude.json"))


def _secret_hit(word, cwd):
    """True when `word` (a path-ish argument, a flag value or a glob) names a credential store, as written or after resolving symlinks."""
    if not word:
        return False
    cands = {word}
    for sep in ("=", ":"):  # --file=PATH, and git REV:PATH (`git show HEAD:.env`)
        if sep in word:
            cands.add(word.split(sep, 1)[1])
            cands.add(word.rsplit(sep, 1)[1])
    for cand in list(cands):
        p = os.path.expanduser(cand)
        if not p.startswith("/"):
            p = os.path.join(cwd, p)
        cands.add(os.path.realpath(p) if not re.search(r"[*?\[]", p) else p)
        p = os.path.normpath(p)
        cands.add(p)
        if not re.search(r"[*?\[]", p):
            try:
                cands.add(os.path.realpath(p))
            except OSError:
                pass
        else:  # a glob can expand to a credential file: judge what it expands to (over-approximating bracket classes), fail closed on a flood
            for pat in {p, re.sub(r"\[[^\]]*\]", "?", p)}:
                try:
                    hits = list(itertools.islice(glob.iglob(pat), 501))
                except OSError:
                    hits = []
                if len(hits) > 500:
                    return True
                for hit in hits:
                    cands.add(hit)
                    cands.add(os.path.realpath(hit))
    for c in cands:
        if _SECRET_RE.search(c):
            return True
        cl = c.lower()  # APFS is case-insensitive by default
        if any(cl == r.lower() or cl.startswith(r.lower() + "/") or (r.endswith(".json") and cl.startswith(r.lower())) for r in _HOME_CLAUDE):
            return True
    return False


# --------------------------------------------------------------------------- tokenizer
class Reject(Exception):
    pass


class AskNeeded(Reject):
    """The command does something the owner can authorize but must be asked about: the hook answers ASK, never silence."""


class SecretRead(AskNeeded):
    """A word names a credential store (a disclosure the owner can authorize)."""


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def secret_hit(word, cwd):
    if "**" in word:
        raise Reject("globstar")
    if _secret_hit(word, cwd):
        raise SecretRead(word[:40])
    return False


def tokenize(src):
    """-> list of tokens: ('op', ';'|'&&'|'||'|'|') | ('redir', fd, op, targetparts) | ('word', parts, glob). parts: ('lit', s) | ('var', NAME)."""
    toks, i, n = [], 0, len(src)
    parts, glob, started = [], False, False

    def flush():
        nonlocal parts, glob, started
        if started:
            toks.append(("word", parts, glob))
        parts, glob, started = [], False, False

    def lit(ch):
        nonlocal started
        started = True
        if parts and parts[-1][0] == "lit":
            parts[-1] = ("lit", parts[-1][1] + ch)
        else:
            parts.append(("lit", ch))

    while i < n:
        ch = src[i]
        if ch in " \t":
            flush(); i += 1; continue
        if ch == "\n":
            flush(); toks.append(("op", ";")); i += 1; continue
        if ch == "#" and not started:
            raise Reject("comment")
        if ch == "\\":
            if i + 1 >= n or src[i + 1] == "\n":
                raise Reject("line continuation")
            lit(src[i + 1]); i += 2; continue
        if ch == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise Reject("unterminated quote")
            started = True
            lit(src[i + 1:j]) if j > i + 1 else lit("")
            i = j + 1; continue
        if ch == '"':
            i += 1; started = True
            while True:
                if i >= n:
                    raise Reject("unterminated quote")
                c = src[i]
                if c == '"':
                    i += 1; break
                if c == "`":
                    raise Reject("backtick")
                if c == "\\" and i + 1 < n:
                    nxt = src[i + 1]
                    if nxt in '"\\$`':
                        lit(nxt)
                    elif nxt == "\n":
                        raise Reject("line continuation")
                    else:
                        lit("\\" + nxt)
                    i += 2; continue
                if c == "$":
                    m = NAME.match(src, i + 1) or None
                    if m:
                        parts.append(("var", m.group(0))); i = m.end(); continue
                    if src.startswith("${", i):
                        m2 = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}").match(src, i + 1)
                        if m2:
                            parts.append(("var", m2.group(1))); i = m2.end(); continue
                    raise Reject("expansion")
                lit(c); i += 1
            continue
        if ch == "`":
            raise Reject("backtick")
        if ch == "$":
            m = NAME.match(src, i + 1)
            if m:
                started = True; parts.append(("var", m.group(0))); i = m.end(); continue
            m2 = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}").match(src, i + 1)
            if m2:
                started = True; parts.append(("var", m2.group(1))); i = m2.end(); continue
            raise Reject("expansion")
        if ch in "*?[":
            glob = True; lit(ch); i += 1; continue
        if ch == "{" or ch == "}":
            if started or src[i:i + 1] == "{":
                raise Reject("brace")
            raise Reject("brace")
        if ch == "~" and not started:
            if i + 1 < n and src[i + 1] not in " \t\n/;&|<>":
                raise Reject("tilde form")  # ~user, ~+ and ~- expand to directories this parser does not model
            lit("~"); i += 1; continue
        if ch in ";&|()<>":
            two = src[i:i + 2]
            fd = None
            if ch in "<>" and started and all(p[0] == "lit" for p in parts) and "".join(p[1] for p in parts).isdigit():
                fd = "".join(p[1] for p in parts); parts, glob, started = [], False, False
            elif ch in "<>&|;()" or two in ("&&", "||"):
                flush()
            if two in ("&&", "||") and ch in "&|":
                toks.append(("op", two)); i += 2; continue
            if ch == ";":
                if two == ";;":
                    raise Reject("case")
                toks.append(("op", ";")); i += 1; continue
            if ch == "|":
                if two == "|&":
                    raise Reject("|&")
                toks.append(("op", "|")); i += 1; continue
            if ch in "()":
                raise Reject("subshell")
            if src.startswith("<<", i) or src.startswith("<(", i) or src.startswith(">(", i):
                raise Reject("heredoc/process substitution")
            if ch == "&":
                if two == "&>":
                    op, i = "&>", i + 2
                else:
                    raise Reject("background")
            elif ch == ">":
                if src.startswith(">>", i):
                    op, i = ">>", i + 2
                elif src.startswith(">&", i):
                    op, i = ">&", i + 2
                else:
                    op, i = ">", i + 1
            else:
                if src.startswith("<&", i):
                    raise Reject("<&")
                op, i = "<", i + 1
            while i < n and src[i] in " \t":
                i += 1
            # target word (one word, plain)
            tparts, tstart = [], i
            while i < n and src[i] not in " \t\n;&|()<>":
                if src[i] in "'\"`$\\*?[{~":
                    raise Reject("redirect target")
                i += 1
            target = src[tstart:i]
            if not target:
                raise Reject("redirect target")
            toks.append(("redir", fd, op, target)); continue
        lit(ch); i += 1
    flush()
    return toks


def expand(parts, vars_):
    out = []
    for kind, val in parts:
        if kind == "lit":
            out.append(val)
        elif val in vars_:
            if re.search(r"[\s*?\[]", vars_[val]):  # unquoted use would word-split or glob, which this parser does not model
                raise Reject("variable value needs splitting")
            out.append(vars_[val])
        else:
            raise Reject("unknown variable")
    return "".join(out)


def tilde(word):
    return HOME + word[1:] if word == "~" or word.startswith("~/") else word


def redir_ok(fd, op, target, cwd):
    if op in (">", ">>", "&>") and target == "/dev/null":
        return True
    if op == ">&" and re.fullmatch(r"[0-9]", target):
        return True
    if op == "<" and not secret_hit(tilde(target), cwd):
        return True
    return False


# --------------------------------------------------------------------------- command judgement
# Every program is judged by an ALLOWLIST of flags (a denylist loses to getopt abbreviations, clustered short flags and attached values).
# A flag that is not listed makes the whole command fall through to the normal decision.
def flags_ok(args, short="", long_exact=(), long_prefix=(), valued=()):
    """short: letters allowed in a cluster; valued: flags that take the next word (a short one may also carry it attached: -A3)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return True
        if a in valued:
            i += 2
            continue
        if a.startswith("--"):
            if not (a in long_exact or (long_prefix and a.startswith(tuple(long_prefix)))):
                return False
        elif a.startswith("-") and len(a) > 1:
            body = a[1:]
            if "-" + body[0] in valued:
                i += 1
                continue
            if not set(body) <= set(short):
                return False
        i += 1
    return True


def operands(args, valued=()):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in valued:
            i += 2
            continue
        if a == "--":
            out += args[i + 1:]
            break
        if not (a.startswith("-") and len(a) > 1):
            out.append(a)
        i += 1
    return out


SED_SCRIPT = re.compile(r"""^\s*(?:(?:\d+|\$|/[^/]*/)(?:,(?:\d+|\$|/[^/]*/|\+\d+))?)?!?\s*(?:[pPdq=ln]|s(.)(?:(?!\1).)*\1(?:(?!\1).)*\1[gIip0-9]*)?\s*$""")


def ok_sed(args):
    if not flags_ok(args, short="nEsrzu", long_exact=("--quiet", "--silent", "--regexp-extended", "--separate", "--unbuffered"),
                    long_prefix=("--expression=",), valued=("-e", "-l")):
        return False
    scripts, files, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "-e":
            scripts.append(args[i + 1]); i += 2; continue
        if a == "-l":
            i += 2; continue
        if a.startswith("-e") and len(a) > 2 and not a.startswith("--"):  # attached script: -e1p
            scripts.append(a[2:]); i += 1; continue
        if a.startswith("-l") and len(a) > 2 and not a.startswith("--"):
            i += 1; continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif not (a.startswith("-") and a != "-"):
            files.append(a)
        i += 1
    if not scripts:
        if not files:
            return False
        scripts = [files.pop(0)]
    return all(not piece.strip() or SED_SCRIPT.match(piece) for s in scripts for piece in s.split(";"))


def ok_awk(args):
    i, prog = 0, None
    while i < len(args):
        a = args[i]
        if a in ("-F", "-v"):
            i += 2
            continue
        if (a.startswith("-F") or a.startswith("-v")) and len(a) > 2:
            i += 1
            continue
        if a.startswith("-"):
            return False
        prog = a
        break
    return prog is not None and not re.search(r"system|getline|ENVIRON|ARGV|ARGC|\||>(?!=)|close\s*\(|fflush|@|/dev/", prog)


def ok_find(args):
    bad = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
    return not any(a in bad for a in args)


def ok_sort(args):
    return flags_ok(args, short="nrukfbsdgMhVRiCc0123456789,", long_exact=("--numeric-sort", "--reverse", "--unique", "--ignore-case", "--stable",
                    "--human-numeric-sort", "--version-sort", "--month-sort", "--general-numeric-sort", "--ignore-leading-blanks", "--dictionary-order"),
                    long_prefix=("--key=", "--field-separator="), valued=("-k", "-t"))


def ok_uniq(args):
    return flags_ok(args, short="cduiD") and len(operands(args)) + args.count("-") <= 1


def ok_grep(args):
    return flags_ok(args, short="ivnclLEFwxqsHhoaIZ0123456789", long_exact=("--line-buffered", "--ignore-case", "--invert-match", "--count", "--files-with-matches",
                    "--no-messages", "--quiet", "--only-matching", "--word-regexp", "--fixed-strings", "--extended-regexp", "--no-filename", "--with-filename",
                    "--line-number", "--null", "--text", "--binary-files=without-match"), long_prefix=("--color=", "--regexp=", "--max-count=", "--after-context=",
                    "--before-context=", "--context="), valued=("-e", "-m", "-A", "-B", "-C"))


def ok_rtk_grep(args, cwd):
    """`rtk grep` searches recursively (hidden files skipped). Allowlisted flags, no `--` and no -g/--glob (rg takes `--pre`, `--hidden` and globs that
    re-include hidden files), and a root inside a project working tree (strictly inside a development or scratch root, never the root itself) that holds no credential-named file."""
    valued = ("-e", "-m", "-A", "-B", "-C", "-t", "--max-len", "--max", "--file-type", "-l", "-T")
    if "--" in args or not flags_ok(args, short="ivnclwxqsHhoFEIa0123456789", long_exact=("--context-only", "--ultra-compact", "--ignore-case", "--invert-match",
                    "--count", "--files-with-matches", "--word-regexp", "--fixed-strings", "--line-buffered", "--no-messages", "--only-matching"),
                    long_prefix=("--color=", "--regexp=", "--max-count=", "--max-len=", "--max=", "--file-type="), valued=valued):
        return False
    ops = operands(args, valued)
    roots = ops[1:] or ["."]  # first operand is the pattern
    base = os.path.realpath(cwd)
    roots_real = [os.path.realpath(r) for r in READ_ROOTS]
    if base in roots_real or not any(base.startswith(r + "/") for r in roots_real):  # strictly inside a known development or scratch root, never the root itself
        return False
    for r in roots:
        p = os.path.realpath(os.path.join(cwd, tilde(r)))
        if not (p == base or p.startswith(base + "/")):
            return False
        tree_secret_scan(p)
    return True


def tree_secret_scan(root, cap=4000):
    """Ask when a non-hidden credential-named file sits under `root` (rg would print it); fall through when the tree is too big to judge."""
    seen = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "target", "__pycache__")]
        for f in files:
            seen += 1
            if seen > cap:
                raise Reject("tree too large to judge")
            if not f.startswith(".") and _SECRET_RE.search(os.path.join(d, f)):
                raise SecretRead(f)


GIT_DANGEROUS = ("output", "ext-diff", "textconv", "no-index", "open-files-in-pager", "exec", "exec-path", "upload-pack", "receive-pack", "git-dir", "work-tree",
                 "namespace", "config-env", "paginate", "pager", "super-prefix", "attr-source", "help")
GIT_READ = {"status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files", "ls-tree", "merge-base", "describe", "shortlog", "name-rev", "for-each-ref",
            "show-ref", "branch", "tag", "stash", "worktree"}


def git_option_dangerous(a):
    if a.startswith("--"):
        name = a[2:].split("=", 1)[0]
        return bool(name) and any(d.startswith(name) for d in GIT_DANGEROUS)
    return a.startswith("-O") or a == "-h"


GIT_HELPER_KEYS = r"^(core\.(fsmonitor|pager)|diff\..*(external|textconv)|gpg\.|log\.showsignature)"


def git_config_runs_helpers(cwd):
    """Repo or global config that makes status/diff/log run a program (fsmonitor, external diff, textconv, gpg, pager): judged from the config itself."""
    try:
        r = subprocess.run(["git", "-C", cwd, "config", "--get-regexp", GIT_HELPER_KEYS], capture_output=True, text=True, timeout=2,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": HOME, "GIT_TERMINAL_PROMPT": "0"})
    except Exception:  # noqa: BLE001
        return True
    return bool(r.stdout.strip())


def ok_git(args, cwd):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("--no-pager", "--no-optional-locks", "--no-replace-objects"):
            i += 1
        elif args[i] == "-C" and i + 1 < len(args):
            secret_hit(tilde(args[i + 1]), cwd)
            i += 2
        else:
            return False
    if i >= len(args) or args[i] not in GIT_READ:
        return False
    verb, rest = args[i], args[i + 1:]
    if any(git_option_dangerous(a) for a in rest) or "--show-signature" in rest or git_config_runs_helpers(cwd):
        return False
    if verb == "branch":
        takes_pattern = ("--contains", "--merged", "--no-merged", "--list", "-l")
        for j, a in enumerate(rest):
            if not a.startswith("-") and not (j and rest[j - 1] in takes_pattern):
                return False  # a bare name would create a branch
        return flags_ok([a for a in rest if a.startswith("-")], short="avrl", long_exact=("--list", "--show-current", "--no-color", "--all", "--remotes", "--verbose",
                        "--contains", "--merged", "--no-merged"), long_prefix=("--sort=", "--format=", "--color="))
    if verb == "tag":
        return all(a in ("-l", "--list") or not a.startswith("-") for a in rest) and (not rest or rest[0] in ("-l", "--list"))
    if verb == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if verb == "worktree":
        return rest == ["list"] or rest == ["list", "--porcelain"]
    return True


def has_flag(args, *names, prefix=()):
    return any(a in names or any(a.startswith(p) for p in prefix) for a in args)


GH_READ = {("pr", "view"), ("pr", "list"), ("pr", "checks"), ("pr", "diff"), ("pr", "status"), ("issue", "view"), ("issue", "list"),
           ("issue", "status"), ("run", "view"), ("run", "list"), ("repo", "view"), ("release", "view"), ("release", "list")}


def ok_gh(args):
    pos = [a for a in args[:2]]
    if len(pos) < 2 or tuple(pos) not in GH_READ:
        return False
    if any("://" in a or re.match(r"^[^/=\s]+\.[^/\s]+/[^/]+/[^/]+", a) or a.count("/") > 1 and not a.startswith(("-", ".", "/")) for a in args[2:]):
        return False  # a URL or HOST/OWNER/REPO operand points gh at another host
    # --jq/--template evaluate an expression that can read the environment; --web opens a browser; nothing else is needed for a read
    return flags_ok(args[2:], short="LsSRlc0123456789", long_exact=("--comments", "--name-only", "--patch", "--stat", "--watch", "--failed", "--log-failed",
                    "--verbose", "--exit-status"), long_prefix=("--json=", "--repo=", "--limit=", "--state=", "--search=", "--author=", "--label=", "--base=", "--head=",
                    "--branch=", "--workflow=", "--status="), valued=("--json", "--repo", "-R", "--limit", "-L", "--state", "-s", "--search", "-S", "--author",
                    "--label", "--base", "--head", "--branch", "--workflow", "--status")) and "-q" not in args and "--jq" not in args


def no_cargo_wrapper(cwd):
    """cargo runs `rustc -vV` through build.rustc-wrapper from any .cargo/config* above the cwd: refuse when one is configured (cwd comes from the event)."""
    d = os.path.realpath(cwd)
    dirs = []
    while True:
        dirs.append(d)
        if d in ("/", HOME):
            break
        d = os.path.dirname(d)
    dirs.append(HOME)
    for d in dirs:
        if os.path.exists(os.path.join(d, "rust-toolchain.toml")) or os.path.exists(os.path.join(d, "rust-toolchain")):
            return False  # the rustup proxy may download a toolchain
        for name in ("config.toml", "config"):
            f = os.path.join(d, ".cargo", name)
            try:
                if os.path.isfile(f) and re.search(r"wrapper|runner|rustc|include|linker", open(f).read()):
                    return False
            except OSError:
                return False
    return True


def ok_cargo(args, cwd):
    if args in (["--version"], ["-V"]):
        return True
    if not args:
        return False
    v, rest = args[0], args[1:]
    if v == "metadata":
        return "--offline" in rest and no_cargo_wrapper(cwd) and flags_ok(rest, long_exact=("--offline", "--no-deps", "--frozen", "--locked"), long_prefix=("--format-version=",),
                                                 valued=("--format-version",))
    if v == "tree":
        return "--offline" in rest and no_cargo_wrapper(cwd) and flags_ok(rest, short="eid", long_exact=("--offline", "--frozen", "--locked", "--workspace"), long_prefix=("--depth=",),
                                                 valued=("--depth", "-p", "--package"))
    return False


CHECKOUT_RE = re.compile(r"^" + re.escape(HOME) + r"/Bryant-Developments/coding-agent-environment(-main-view)?/scripts/")


def ok_script(args, cwd):
    """python3 <scripts/X.py> from the owner's two stable coding-agent-environment checkouts, read-only subcommands only."""
    if not args or args[0].startswith("-"):
        return False
    path = os.path.realpath(os.path.join(cwd, tilde(args[0])))
    if not CHECKOUT_RE.match(path) or not os.path.isfile(path):
        return False
    rest = [a for a in args[1:] if a != "--json"]
    if rest and rest[-1] == ".":  # the rtk rewrite appends a bare ` .` to some commands (measured with the host matrix); it names nothing here
        rest = rest[:-1]
    name = os.path.basename(path)
    if name == "disk-admission.py":
        return rest in (["status"], ["queue", "status"])
    if name == "memory-admission.py":
        return len(rest) in (1, 3) and rest[0] == "check" and (len(rest) == 1 or (rest[1] == "--weight" and rest[2] in ("light", "heavy")))
    return False


def ok_python(args, cwd):
    if args in (["--version"], ["-V"]):
        return True
    return ok_script(args, cwd)


def ok_jq(args):
    if not flags_ok(args, short="rcCeSMn", long_exact=("--raw-output", "--compact-output", "--exit-status", "--sort-keys", "--null-input", "--slurp"), valued=("--arg",)):
        return False
    return not any(re.search(r"\benv\b|\$ENV|input|import|include|\$__|debug|stderr|halt", a) for a in args if not a.startswith("-"))


def ok_diff(args):
    return flags_ok(args, short="uqswbBiy0123456789", long_exact=("--brief", "--strip-trailing-cr", "--ignore-space-change", "--ignore-all-space"),
                    long_prefix=("--unified=",), valued=("-U",))


def ok_tree(args):
    """`-L N` as two words only; no other valued flag (tree's own parser is not getopt, so a clustered valued flag cannot be judged)."""
    rest, i = [], 0
    while i < len(args):
        if args[i] == "-L" and i + 1 < len(args) and args[i + 1].isdigit():
            i += 2
            continue
        rest.append(args[i])
        i += 1
    return flags_ok(rest, short="adfFCniD", long_exact=("--noreport",))


SIMPLE_FLAGS = {  # program -> (short letters, valued flags)
    "cat": ("nbsvAeEtTu", ()), "head": ("nqv0123456789", ("-n", "-c")), "tail": ("nfFqv0123456789", ("-n", "-c")), "wc": ("lwcmL", ()),
    "cut": ("sn", ("-c", "-d", "-f", "-b")), "tr": ("dcsC", ()), "echo": ("neE", ()), "printf": ("", ()), "df": ("ghkHPTilm", ()), "du": ("ahksxcdLH0123456789", ("-d",)),
    "ls": ("alhtrSR1FdGisLpAkn", ()), "stat": ("xnlstFqLr", ("-f", "-c", "-t")), "pwd": ("LP", ()), "basename": ("", ()), "dirname": ("", ()), "realpath": ("q", ()),
    "readlink": ("fn", ()), "cmp": ("ls", ()), "sleep": ("", ()), "true": ("", ()), "false": ("", ()), "uname": ("asmnrv", ()), "whoami": ("", ()), "uptime": ("", ()),
    "vm_stat": ("", ()), "break": ("", ()), "continue": ("", ()),
}
RTK_NATIVE = {"ls", "tree", "find", "wc", "df", "du", "diff", "cat", "head", "tail"}


def judge(argv, cwd, state):
    """True when one simple command is read-only. argv[0] is never taken from a path (a script or a file named like a tool does not count)."""
    if not argv:
        return False
    prog, args = argv[0], argv[1:]
    if "/" in prog:
        return False
    for a in args:
        secret_hit(tilde(a), cwd)
        if a.startswith("-") and not a.startswith("--") and len(a) > 2:
            secret_hit(tilde(a[2:]), cwd)  # an attached short-option value: -f.env
    if prog == "rtk":
        if not args:
            return False
        sub, rest = args[0], args[1:]
        if sub == "read":
            return bool(operands(rest, ("-l", "--level", "-m", "--max-lines", "--tail-lines"))) and flags_ok(
                rest, short="n", long_exact=("--line-numbers", "--ultra-compact"), valued=("-l", "--level", "-m", "--max-lines", "--tail-lines"))
        if sub == "grep":
            return ok_rtk_grep(rest, cwd)
        if sub in ("git", "gh") or sub in RTK_NATIVE:
            return judge([sub] + rest, cwd, state)
        return False
    if prog == "cd":
        return len(args) <= 1 and (not args or args[0] != "-")
    if prog == "sed":
        return ok_sed(args)
    if prog in ("awk", "gawk"):
        return ok_awk(args)
    if prog in ("grep", "egrep", "fgrep"):
        return ok_grep(args)
    if prog == "find":
        return ok_find(args)
    if prog == "sort":
        return ok_sort(args)
    if prog == "uniq":
        return ok_uniq(args)
    if prog == "date":
        return all(a == "-u" or a.startswith("+") for a in args)
    if prog == "git":
        return ok_git(args, cwd)
    if prog == "gh":
        return ok_gh(args)
    if prog == "cargo":
        return ok_cargo(args, cwd)
    if prog in ("python3", "python"):
        return ok_python(args, cwd)
    if prog == "jq":
        return ok_jq(args)
    if prog == "diff":
        return ok_diff(args)
    if prog == "tree":
        return ok_tree(args)
    if prog in ("test", "["):  # file and string tests only: `-v NAME` evaluates an array subscript
        return all(a in ("-f", "-d", "-e", "-s", "-r", "-w", "-x", "-z", "-n", "-L", "!", "=", "==", "!=", "]") or not (a.startswith("-") or "[" in a or "$" in a) for a in args)
    if prog == "echo":  # prints its operands; `echo ---` is text, not an option
        return True
    if prog == "printf":
        return not any(a.startswith("-") for a in args)  # printf -v 'a[$(cmd)]' evaluates the subscript; no options at all
    if prog in SIMPLE_FLAGS:
        short, valued = SIMPLE_FLAGS[prog]
        return flags_ok(args, short=short, valued=valued)
    return False


PROTECTED_VAR = re.compile(r"^(HOME|PATH|CDPATH|FPATH|MANPATH|MODULE_PATH|IFS|SHELL|ENV|BASH_ENV|PS[0-9]|PROMPT|PROMPT_COMMAND|GLOBIGNORE|FIGNORE|GIT_|GH_|GITHUB_|LD_|DYLD_|PYTHON|PYTEST_|CARGO_|RUST|RIPGREP|RTK_|NODE_|NPM_|PAGER|EDITOR|VISUAL|LESS|TMPDIR|XDG_|LC_|LANG|OLDPWD|PWD|SSH_|AWS_|AZURE_|GOOGLE_|CLOUDSDK|DOCKER_|KUBE|SSL_|CURL_|HTTPS?_PROXY|ALL_PROXY|NO_PROXY|BROWSER|HIST|ZDOT|ZSH|BASH|USER|LOGNAME|TERM)", re.I)
KILLERS = {"pkill", "killall", "kill"}
WORKTREE_MUTATING = {"add", "remove", "prune", "move", "lock", "unlock", "repair"}
COARSE_DANGER = re.compile(
    r"\b(pkill|killall|kill)\b|\brm\b.*\s(-\S*[rRf]\S*|--rec\S*|--force)|worktree\s+(add|remove|prune|move)|\bgit\b.*\bpush\b.*(--force|--delete|--mirror|\s-f\b|\s\+\S)"
    r"|reset\s+--ha|\bgit\b.*\b(clean|restore)\b|gh\b.*\bpr\b.*\bmerge\b|\bfind\b.*-delete|xargs\s+(\S+\s+)*rm\b")


def danger(words):
    """Operations the owner can authorize but must be asked about, found in any stage whatever the other stages are."""
    prog, args = words[0], words[1:]
    while prog in ("rtk", "proxy", "command", "nohup", "time", "sudo", "env", "xargs") and args:
        prog, args = args[0], args[1:]
    prog = os.path.basename(prog)
    if prog in KILLERS:
        raise AskNeeded("process-kill")
    if prog == "rm" and any(a in ("-r", "-R", "-f") or a.startswith("--rec") or a.startswith("--for") or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("rRf")) for a in args):
        raise AskNeeded("recursive-or-forced-delete")
    if prog == "find" and "-delete" in args:
        raise AskNeeded("find-delete")
    if prog == "gh" and (("pr" in args and "merge" in args) or ("repo" in args and "delete" in args) or ("release" in args and "delete" in args)):
        raise AskNeeded("gh-destructive")
    if prog == "git":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-C", "-c") else 1
        verb, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
        if verb == "worktree" and rest[:1] and rest[0] in WORKTREE_MUTATING:
            raise AskNeeded("worktree-change")
        if verb == "push" and any(a in ("-f", "-d") or a.startswith(("--force", "--delete", "--mirror", "+", ":")) or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("fd")) for a in rest):
            raise AskNeeded("force-or-delete-push")
        if verb == "reset" and any(a.startswith("--ha") for a in rest) or verb in ("clean", "restore") or verb == "checkout" and any(a in ("-f", "--force", "--", ".") for a in rest) \
                or verb == "update-ref" and "-d" in rest or verb == "gc" and any(a.startswith("--prune") for a in rest):
            raise AskNeeded("discard-work")
        if verb == "branch" and any(a in ("--delete",) or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("dD")) for a in rest) or verb == "tag" and any(a in ("-d", "--delete") for a in rest):
            raise AskNeeded("ref-delete")
        if verb == "stash" and rest[:1] and rest[0] in ("drop", "clear"):
            raise AskNeeded("stash-delete")


STRUCT_PREFIX = {"until", "while", "if", "elif", "then", "else", "do", "!"}
SAFE_LOOP_CMD = ("read",)


def classify_command(src, cwd):
    """-> (True, reason) when every stage of `src` is read-only, (False, why) when it is not provable, ("ask", why) for an operation the owner must authorize."""
    res = _classify(src, cwd)
    if res[0] is not True and res[0] != "ask" and COARSE_DANGER.search(src):  # a reject before a dangerous stage must not hide it
        return "ask", "dangerous-keyword"
    return res


def _classify(src, cwd):
    try:
        toks = tokenize(src)
        vars_, loopvars, failed = {}, set(), None
        cwds, pending = [cwd], None
        seg, segs, op = [], [], ";"
        for t in toks + [("op", ";")]:
            if t[0] == "op":
                segs.append((op, seg)); seg, op = [], t[1]
            else:
                seg.append(t)
        closed = ifs = fis = stages = 0
        in_body = any(t[0] == "word" and t[1] in ([("lit", "do")], [("lit", "then")], [("lit", "for")], [("lit", "while")], [("lit", "until")], [("lit", "if")])
                      for _, sg in segs for t in sg[:1])
        for op_before, seg in segs:
            if not seg:
                continue
            if pending is not None:  # the cd before this stage may have failed: keep both directories unless `&&` guarantees it succeeded
                old, new = pending
                cwds = list(dict.fromkeys(new if op_before == "&&" else old + new))
                pending = None
            if any(t[0] == "redir" for t in seg) and not all(redir_ok(t[1], t[2], t[3], c) for c in cwds for t in seg if t[0] == "redir"):
                failed = failed or (False, "redirect")
            words = [t for t in seg if t[0] == "word"]
            seg_words = [expand_word(w, vars_) for w in words]
            while seg_words and seg_words[0] in STRUCT_PREFIX:
                if seg_words[0] == "if":
                    ifs += 1
                seg_words.pop(0)
            if not seg_words:
                continue
            first = seg_words[0]
            if first == "done":
                closed += 1
                if len(seg_words) > 1:
                    failed = failed or (False, "done args")
                continue
            if first == "fi":
                fis += 1
                if len(seg_words) > 1:
                    failed = failed or (False, "fi args")
                continue
            if first == "for":
                if len(seg_words) < 3 or seg_words[2] != "in" or not NAME.fullmatch(seg_words[1]) or seg_words[1] in os.environ or PROTECTED_VAR.match(seg_words[1]):
                    failed = failed or (False, "for")
                    continue
                loopvars.add(seg_words[1])
                for w in seg_words[3:]:
                    for c in cwds:
                        secret_hit(tilde(w), c)
                continue
            if all(ASSIGN.match(w) for w in seg_words):
                for w in seg_words:
                    m = ASSIGN.match(w)
                    if m.group(1) in loopvars or m.group(1) in os.environ or PROTECTED_VAR.match(m.group(1)):
                        failed = failed or (False, "assign to an existing or protected variable")
                    else:
                        vars_[m.group(1)] = tilde(m.group(2))
                continue
            if ASSIGN.match(first):
                failed = failed or (False, "env prefix")
                continue
            if first == "cd":
                if in_body or len(seg_words) > 2 or (len(seg_words) == 2 and seg_words[1].startswith("-")):
                    failed = failed or (False, "cd form")  # a cd inside a loop or conditional body runs a number of times this parser does not model
                    continue
                raw = tilde(seg_words[1]) if len(seg_words) == 2 else HOME
                news = []
                for c in cwds:  # every directory the shell may be in at this point
                    nxt = os.path.normpath(os.path.join(c, raw))
                    real = os.path.realpath(nxt)
                    if ".." in raw.split("/") and real != nxt:  # logical and physical `..` disagree through a symlink: do not guess
                        failed = failed or (False, "cd dotdot through symlink")
                    secret_hit(real, c)
                    news.append(real)
                pending = (list(cwds), news)
                stages += 1
                continue
            danger(seg_words)
            if failed is None and not all(judge(seg_words, c, vars_) for c in cwds):
                failed = (False, "stage: " + first)
                continue
            stages += 1
        do_count = sum(1 for _, sg in segs if sg and sg[0][0] == "word" and sg[0][1] == [("lit", "do")])
        if failed:
            return failed
        if closed != do_count or fis != ifs:
            return False, "unbalanced"
        if stages == 0:
            return False, "empty"
        return True, "readonly-pipeline"
    except SecretRead:
        return "ask", "credential-path"
    except AskNeeded as e:
        return "ask", str(e)
    except Reject as e:
        return False, "reject: %s" % e
    except Exception as e:  # noqa: BLE001 - never raise out of a permission hook
        return False, "error: %s" % type(e).__name__


def expand_word(tok, vars_):
    return expand(tok[1], vars_)


# --------------------------------------------------------------------------- agent dispatch
READONLY_AGENTS = {"Explore", "Plan", "claude-code-guide", "caveman:cavecrew-investigator", "caveman:cavecrew-reviewer"}
DECLARES_READONLY = re.compile(r"^\s*(\*\*)?(independent\s+)?read[- ]only\b", re.I)


AGENT_KEYS = {"description", "prompt", "subagent_type", "name", "run_in_background"}


def classify_agent(tin):
    if not set(tin) <= AGENT_KEYS:  # mode, isolation, model overrides and anything else this hook has not judged
        return False, "agent-extra-fields"
    st = (tin.get("subagent_type") or "").strip()
    if st in READONLY_AGENTS:
        return True, "readonly-agent-type"
    if st in ("general-purpose", "") and DECLARES_READONLY.match(str(tin.get("prompt") or "")):
        return True, "readonly-declared-dispatch"
    return False, "agent-not-readonly"


# --------------------------------------------------------------------------- entry
JIRA_BASES = ("searchJiraIssuesUsingJql", "getJiraIssue", "createJiraIssue", "editJiraIssue", "addCommentToJiraIssue", "transitionJiraIssue", "getTransitionsForJiraIssue",
              "getVisibleJiraProjects", "getJiraProjectIssueTypesMetadata", "getJiraIssueTypeMetaWithFields", "createIssueLink", "getIssueLinkTypes", "lookupJiraAccountId",
              "atlassianUserInfo", "getAccessibleAtlassianResources", "getTeamworkGraphContext", "getTeamworkGraphObject", "addTeamworkGraphContext", "addWorklogToJiraIssue",
              "getJiraIssueRemoteIssueLinks")  # exactly the Jira tools the owner's settings already allow; nothing new is authorized here
JIRA_TOOLS = {"mcp__%s__%s" % (srv, b) for srv in ("claude_ai_Atlassian_Rovo", "plugin_atlassian_atlassian") for b in JIRA_BASES}
INTERNAL_TOOLS = {"ScheduleWakeup", "ReadNotifications", "ToolSearch"}  # resume this session / read its own queue / load tool schemas: no effect outside it
PEER_SOCK = re.compile(r"^uds:(/tmp/cc-socks/\d+\.sock)$")
SUBAGENT_ID = re.compile(r"^[0-9a-f]{16,40}$")
READ_ROOTS = (os.path.join(HOME, "Bryant-Developments"), "/tmp", "/private/tmp", "/private/var/folders", os.path.join(HOME, ".cache/coding-agent-environment"),
              os.path.join(HOME, ".cargo/shared-target"), os.path.join(HOME, "Library/Application Support/rtk/tee"))


def local_peer(to):
    """A subagent id, or a Claude Code session socket under /tmp/cc-socks that exists and is owned by this user (the owner's own local sessions)."""
    if SUBAGENT_ID.match(to):
        return True
    m = PEER_SOCK.match(to)
    if not m:
        return False
    try:
        st = os.stat(m.group(1))
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


def read_root_ok(path, cwd):
    p = os.path.realpath(os.path.join(cwd, path))
    roots = [os.path.realpath(cwd)] if os.path.realpath(cwd) not in (os.path.realpath(HOME), "/") else []
    roots += [os.path.realpath(r) for r in READ_ROOTS]
    return any(p == r or p.startswith(r + "/") for r in roots)
SECRET_TEXT = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[abp]-|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{10,}\.")


def decide(event):
    tool, tin, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd") or os.getcwd()
    if tool in ("Bash", "Monitor"):
        cmd = tin.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None, "no-command"
        return classify_command(cmd, cwd)
    if tool in ("Agent", "Task"):
        return classify_agent(tin)
    if tool == "Read":
        path = tin.get("file_path")
        if not isinstance(path, str) or not path:
            return None, "no-path"
        try:
            secret_hit(tilde(path), cwd)
        except SecretRead:
            return "ask", "credential-path"
        except Reject:
            return False, "unjudgeable-path"
        if not read_root_ok(tilde(path), cwd):
            return None, "read-outside-project-roots"  # the normal permission decision applies
        return True, "native-read"
    if tool in INTERNAL_TOOLS:
        return True, "internal-" + tool
    if tool == "SendMessage":
        to, text = str(tin.get("to", "")), json.dumps(tin)
        if SECRET_TEXT.search(text):
            return "ask", "credential-in-message"
        if local_peer(to):
            return True, "internal-message"
        return False, "message-target-not-a-known-local-peer"
    if tool in JIRA_TOOLS:
        return (True, "authorized-jira") if not SECRET_TEXT.search(json.dumps(tin)) else ("ask", "credential-in-jira-write")
    return None, "tool-not-covered"


def log(event, ok, why):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        tin = event.get("tool_input") or {}
        basis = tin.get("command") or tin.get("prompt") or ""
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": event.get("tool_name"), "decision": ok if ok == "ask" else ("allow" if ok else "fallthrough"),
               "why": why, "sid": str(event.get("session_id", ""))[:8], "sha": hashlib.sha256(str(basis).encode()).hexdigest()[:12]}
        with open(LOG, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main():
    try:
        event = json.load(sys.stdin)
        ok, why = decide(event)
    except Exception:  # noqa: BLE001
        return 0
    if ok is None:
        return 0
    log(event, ok, why)
    if ok == "ask":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                  "permissionDecisionReason": "this command reads a credential or key store; approve only if intended"}}))
    elif ok:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                  "permissionDecisionReason": "routine read-only operation (%s)" % why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
