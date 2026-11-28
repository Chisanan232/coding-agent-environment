#!/usr/bin/env python3
"""PreToolUse hook: let provably read-only routine work run without a prompt (SPE-119).

It reads one hook event on stdin and prints either nothing or one `permissionDecision: allow`. It NEVER prints deny or ask, so it can only
remove prompts: an existing ask or deny rule still wins over a hook allow (measured, see docs/ROUTINE-COMMAND-AUTONOMY.md), and anything
the parser does not fully understand falls through to the normal decision. Any error prints nothing and exits 0.

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
import hashlib
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
LOG = os.environ.get("ROUTINE_READONLY_HOOK_LOG") or os.path.join(HOME, ".cache/coding-agent-environment/routine-hook/decisions.jsonl")

# --------------------------------------------------------------------------- secret paths
_SECRET_RE = re.compile("|".join([
    r"(^|/)\.env(rc)?$", r"(^|/)\.env\.(?!(example|sample|template|dist|defaults?)$)[^/]+$",
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


def secret_hit(word, cwd):
    """True when `word` (a path-ish argument, a flag value or a glob) names a credential store, as written or after resolving symlinks."""
    if not word:
        return False
    cands = {word}
    if "=" in word and word.startswith("-"):
        cands.add(word.split("=", 1)[1])
    for cand in list(cands):
        p = os.path.expanduser(cand)
        if not p.startswith("/"):
            p = os.path.join(cwd, p)
        p = os.path.normpath(p)
        cands.add(p)
        if not re.search(r"[*?\[]", p):
            try:
                cands.add(os.path.realpath(p))
            except OSError:
                pass
    for c in cands:
        if _SECRET_RE.search(c):
            return True
        if any(c == r or c.startswith(r + "/") for r in _HOME_CLAUDE):
            return True
    return False


# --------------------------------------------------------------------------- tokenizer
class Reject(Exception):
    pass


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


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
def has_flag(args, *names, prefix=()):
    for a in args:
        if a in names or any(a.startswith(p) for p in prefix):
            return True
    return False


def short_flags(args):
    s = set()
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            s.update(a[1:])
    return s


SED_SCRIPT = re.compile(r"""^\s*(?:(?:\d+|\$|/[^/]*/)(?:,(?:\d+|\$|/[^/]*/|\+\d+))?)?!?\s*(?:[pPdq=ln]|s(.)(?:(?!\1).)*\1(?:(?!\1).)*\1[gIip0-9]*)?\s*$""")


def ok_sed(args):
    if any(a.startswith(("--in-place", "--file")) for a in args):
        return False
    if short_flags([a for a in args if not a.startswith("--")]) & set("if"):
        return False
    scripts, i, files = [], 0, []
    while i < len(args):
        a = args[i]
        if a == "-e":
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1]); i += 2; continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1]); i += 1; continue
        if a.startswith("-") and a != "-":
            i += 1; continue
        files.append(a); i += 1
    if not scripts:
        if not files:
            return False
        scripts = [files.pop(0)]
    for s in scripts:
        for piece in s.split(";"):
            if piece.strip() and not SED_SCRIPT.match(piece):
                return False
    return True


def ok_awk(args):
    i, prog = 0, None
    while i < len(args):
        a = args[i]
        if a in ("-F", "-v"):
            i += 2; continue
        if a.startswith("-F") or a.startswith("-v"):
            i += 1; continue
        if a in ("--", ):
            i += 1; continue
        if a.startswith("-"):
            return False
        prog = a
        break
    if prog is None:
        return False
    return not re.search(r"system|getline|ENVIRON|\||>(?!=)|close\s*\(|fflush", prog)


def ok_find(args):
    bad = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
    return not any(a in bad for a in args)


def ok_sort(args):
    return not has_flag(args, "-o", "--output", "--compress-program", prefix=("--output=", "--compress-program=", "-o"))


def ok_uniq(args):
    return len([a for a in args if not a.startswith("-")]) <= 1


def ok_date(args):
    return not has_flag(args, "-s", "--set", prefix=("--set=", "-s")) and all(a.startswith("-") or a.startswith("+") for a in args)


def ok_grep(args):
    flags = short_flags([a for a in args if not a.startswith("--")])
    if flags & set("rRdD"):
        return False
    return not has_flag(args, "--recursive", "--dereference-recursive", prefix=("--directories", "--devices", "--include-dir", "--exclude-dir", "--pre", "--exclude-from"))


RG_SAFE_LONG = ("--max-len", "--max", "--context-only", "--file-type", "--glob", "--ignore-case", "--invert-match", "--count", "--line-number",
                "--files-with-matches", "--word-regexp", "--fixed-strings", "--regexp", "--line-buffered", "--no-heading", "--with-filename",
                "--no-filename", "--only-matching", "--max-count", "--quiet", "--context", "--before-context", "--after-context", "--type",
                "--smart-case", "--case-sensitive", "--ultra-compact", "--extended-regexp", "--basic-regexp", "--no-messages", "--null")


def ok_rtk_grep(args):
    for a in args:
        if not a.startswith("-") or a == "-":
            continue
        if a.startswith("--"):
            if not any(a == k or a.startswith(k + "=") for k in RG_SAFE_LONG):
                return False
            continue
        if short_flags([a]) & set("rRdDuLzf"):
            return False
    return True


GIT_READ = {"status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files", "ls-tree", "merge-base", "describe", "shortlog", "name-rev",
            "for-each-ref", "show-ref", "remote", "branch", "tag", "stash", "worktree"}
GIT_BAD_PREFIX = ("--ou", "--ext", "--tex", "--no-i", "--op", "-O", "--exec", "--upload", "--receive", "--git-dir", "--work-tree")


def ok_git(args, cwd):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("--no-pager", "--no-optional-locks", "-P", "--no-replace-objects"):
            i += 1
        elif args[i] == "-C" and i + 1 < len(args) and not secret_hit(tilde(args[i + 1]), cwd):
            i += 2
        else:
            return False
    if i >= len(args) or args[i] not in GIT_READ:
        return False
    verb, rest = args[i], args[i + 1:]
    if any(a.startswith(GIT_BAD_PREFIX) for a in rest):
        return False
    if verb == "remote":
        return rest in ([], ["-v"], ["--verbose"]) or rest[:1] in (["show"], ["get-url"])
    if verb == "branch":
        return all(a.startswith("-") or rest[j - 1] in ("--contains", "--merged", "--no-merged", "--list", "-l") for j, a in enumerate(rest)) and not has_flag(
            rest, "-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-f", "--force", "--set-upstream-to", "-u", "--unset-upstream", "--edit-description")
    if verb == "tag":
        return not rest or rest[0] in ("-l", "--list", "-n")
    if verb == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if verb == "worktree":
        return bool(rest) and rest[0] == "list"
    return True


GH_READ = {("pr", "view"), ("pr", "list"), ("pr", "checks"), ("pr", "diff"), ("pr", "status"), ("issue", "view"), ("issue", "list"),
           ("issue", "status"), ("run", "view"), ("run", "list"), ("repo", "view"), ("release", "view"), ("release", "list")}


def ok_gh(args):
    pos = [a for a in args if not a.startswith("-")]
    return len(pos) >= 2 and (pos[0], pos[1]) in GH_READ and args[:2] == pos[:2] and not has_flag(args, "--web", "-w", "--edit", "--comment", "--merge")


def ok_cargo(args):
    if not args:
        return False
    v = args[0]
    if v in ("--version", "-V", "locate-project"):
        return True
    if v == "metadata":
        return has_flag(args, "--offline", "--frozen")
    if v == "tree":
        return has_flag(args, "--offline", "--frozen")
    if v in ("test", "check", "build", "clippy"):
        return not has_flag(args, "--fix", "--target-dir", prefix=("--target-dir=", "--config", "--manifest-path=/"))
    if v == "fmt":
        return has_flag(args, "--check")
    if v == "nextest":
        return len(args) > 1 and args[1] == "run"
    return False


CHECKOUT_RE = re.compile(r"^" + re.escape(HOME) + r"/Bryant-Developments/coding-agent-environment[^/]*/scripts/")
TRUSTED_SCRIPT_ARGS = {"disk-admission.py": ({"status"}, {"queue status"}), "memory-admission.py": (set(), {"check"}), "classify-step.py": (set(), set())}


def ok_script(args, cwd):
    """python3 <trusted scripts/X.py> <read-only subcommand>: only the scheduler/admission status checks, from a coding-agent-environment checkout."""
    if not args or args[0].startswith("-"):
        return False
    path = os.path.realpath(os.path.join(cwd, tilde(args[0])))
    if not CHECKOUT_RE.match(path) or not os.path.isfile(path):
        return False
    name = os.path.basename(path)
    if name not in TRUSTED_SCRIPT_ARGS:
        return False
    rest = [a for a in args[1:] if a not in ("--json",)]
    if name == "disk-admission.py":
        return rest in (["status"], ["queue", "status"])
    if name == "memory-admission.py":
        return bool(rest) and rest[0] == "check"
    return False


def ok_python(args, cwd):
    if args == ["--version"] or args == ["-V"]:
        return True
    if args[:2] == ["-m", "pytest"]:
        return not has_flag(args, "-p", "--rootdir=/", prefix=("-p",))
    return ok_script(args, cwd)


def ok_pytest(args):
    return not has_flag(args, "-p", prefix=("-p", "--basetemp=/"))


SIMPLE_READ = {"cat", "head", "tail", "wc", "nl", "cut", "tr", "echo", "printf", "df", "du", "ls", "stat", "file", "pwd", "basename", "dirname",
               "realpath", "readlink", "diff", "cmp", "shasum", "md5", "column", "jq", "sleep", "true", "false", "test", "[", "uname", "whoami",
               "uptime", "vm_stat", "tree", "rev", "paste", "comm", "expand", "fold", "od", "xxd", "seq", "which"}
RTK_NATIVE = {"ls", "tree", "find", "wc", "df", "du", "diff", "cat", "head", "tail"}


def judge(argv, cwd, state):
    """True when one simple command is read-only. argv[0] is never taken from a path (a script or a file named like a tool does not count)."""
    if not argv:
        return False
    prog, args = argv[0], argv[1:]
    if "/" in prog:
        return False
    for a in args:
        if secret_hit(tilde(a), cwd):
            return False
    if prog == "rtk":
        if not args:
            return False
        sub, rest = args[0], args[1:]
        if sub == "read":
            return bool(rest) and not has_flag(rest, "--skip-env")
        if sub == "grep":
            return ok_rtk_grep(rest)
        if sub in ("git", "gh"):
            return judge([sub] + rest, cwd, state)
        if sub in RTK_NATIVE:
            return judge([sub] + rest, cwd, state)
        return False
    if prog == "cd":
        return len(args) <= 1 and (not args or not secret_hit(tilde(args[0]), cwd))
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
        return ok_date(args)
    if prog == "git":
        return ok_git(args, cwd)
    if prog == "gh":
        return ok_gh(args)
    if prog == "cargo":
        return ok_cargo(args)
    if prog in ("python3", "python"):
        return ok_python(args, cwd)
    if prog == "pytest":
        return ok_pytest(args)
    if prog == "jq" and any(re.search(r"\benv\b|\$ENV|input_filename|\$__loc__", a) for a in args):
        return False
    if prog in SIMPLE_READ:
        if prog == "tree" and has_flag(args, "-o"):
            return False
        if prog == "xxd" and len([a for a in args if not a.startswith("-")]) > 1:
            return False
        return True
    return False


STRUCT_PREFIX = {"until", "while", "if", "elif", "then", "else", "do", "!"}


def classify_command(src, cwd):
    """-> (True, reason) when every stage of `src` is read-only, else (False, why)."""
    try:
        toks = tokenize(src)
        vars_, loopvars, cur = {}, set(), cwd
        seg, segs = [], []
        for t in toks + [("op", ";")]:
            if t[0] == "op":
                segs.append(seg); seg = []
            else:
                seg.append(t)
        opened = closed = ifs = fis = 0
        stages = 0
        for seg in segs:
            if not seg:
                continue
            if any(t[0] == "redir" for t in seg) and not all(redir_ok(t[1], t[2], t[3], cwd) for t in seg if t[0] == "redir"):
                return False, "redirect"
            words = [t for t in seg if t[0] == "word"]
            seg_words = [expand_word(w, vars_) for w in words]
            while seg_words and seg_words[0] in STRUCT_PREFIX:
                if seg_words[0] in ("until", "while"):
                    opened += 0
                if seg_words[0] == "if":
                    ifs += 1
                seg_words.pop(0)
            if not seg_words:
                continue
            first = seg_words[0]
            if first == "done":
                closed += 1
                if len(seg_words) > 1:
                    return False, "done args"
                continue
            if first == "fi":
                fis += 1
                if len(seg_words) > 1:
                    return False, "fi args"
                continue
            if first == "for":
                if len(seg_words) < 3 or seg_words[2] != "in" or not NAME.fullmatch(seg_words[1]):
                    return False, "for"
                loopvars.add(seg_words[1])
                for w in seg_words[3:]:
                    if secret_hit(tilde(w), cwd):
                        return False, "secret"
                opened += 1
                continue
            if all(ASSIGN.match(w) for w in seg_words):
                for w in seg_words:
                    m = ASSIGN.match(w)
                    if m.group(1) in loopvars:
                        return False, "assign loopvar"
                    vars_[m.group(1)] = tilde(m.group(2))
                continue
            if ASSIGN.match(first):
                return False, "env prefix"
            if first == "cd" and len(seg_words) == 2 and not seg_words[1].startswith("-"):
                nxt = os.path.normpath(os.path.join(cur, tilde(seg_words[1])))
                if secret_hit(nxt, cur):
                    return False, "secret"
                cur = nxt
                stages += 1
                continue
            if not judge(seg_words, cur, vars_):
                return False, "stage: " + first
            stages += 1
            if "do" in [expand_word(w, vars_) for w in words[:1]]:
                pass
        # an until/while body is introduced by `do` and closed by `done`; count `do` words conservatively
        do_count = sum(1 for seg in segs if seg and seg[0][0] == "word" and seg[0][1] == [("lit", "do")])
        if closed != do_count or fis != ifs:
            return False, "unbalanced"
        if stages == 0:
            return False, "empty"
        return True, "readonly-pipeline"
    except Reject as e:
        return False, "reject: %s" % e
    except Exception as e:  # noqa: BLE001 - never raise out of a permission hook
        return False, "error: %s" % type(e).__name__


def expand_word(tok, vars_):
    return expand(tok[1], vars_)


# --------------------------------------------------------------------------- agent dispatch
READONLY_AGENTS = {"Explore", "Plan", "claude-code-guide", "caveman:cavecrew-investigator", "caveman:cavecrew-reviewer"}
DECLARES_READONLY = re.compile(r"^\s*(\*\*)?(independent\s+)?read[- ]only\b", re.I)


def classify_agent(tin):
    st = (tin.get("subagent_type") or "").strip()
    if st in READONLY_AGENTS:
        return True, "readonly-agent-type"
    if st in ("general-purpose", "") and DECLARES_READONLY.match(str(tin.get("prompt") or "")):
        return True, "readonly-declared-dispatch"
    return False, "agent-not-readonly"


# --------------------------------------------------------------------------- entry
def decide(event):
    tool, tin, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd") or os.getcwd()
    if tool in ("Bash", "Monitor"):
        cmd = tin.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None, "no-command"
        ok, why = classify_command(cmd, cwd)
        return ok, why
    if tool in ("Agent", "Task"):
        return classify_agent(tin)
    return None, "tool-not-covered"


def log(event, ok, why):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        tin = event.get("tool_input") or {}
        basis = tin.get("command") or tin.get("prompt") or ""
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": event.get("tool_name"), "decision": "allow" if ok else "fallthrough",
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
    if ok:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                  "permissionDecisionReason": "routine read-only operation (%s)" % why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
