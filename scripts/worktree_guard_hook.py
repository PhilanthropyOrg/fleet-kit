#!/usr/bin/env python3
"""worktree_guard_hook.py -- gh#592: a mechanical PreToolUse hook, not another paragraph.

`members/minion/minion.md` step 1c already asks the model to run `pwd`/`git worktree list`
before its first Edit/Write -- prose, added by PR#577. A minion pass hit the identical failure
again under 12 hours after that fix deployed (gh#592's own writeup). This is the mechanical
layer underneath the prose: a PreToolUse hook that BLOCKS (not warns on) an Edit/Write, or a
mutating Bash command, whose target resolves under the SHARED checkout ($REPO) while this pass
has been isolated into its own worktree ($WT_PATH) -- see run_member.sh's own WORKTREE_ENABLED/
WT_PATH block, which is the ONLY thing that ever sets WT_PATH.

Exempt whenever $WT_PATH is unset/empty: that is exactly run_member.sh's own signal that this
pass is NOT worktree-isolated (`llm.worktree: false`, e.g. jefe's advisory pass -- see
jefe.fleet.json). No new flag invented for that -- this reuses the one the runner already sets,
per gh#592's own instruction not to invent names beyond what run_member.sh actually exports.

`postflight_dirty_check.sh` (gh#78/#183) stays as the second, after-the-fact backstop; this is
the first, preventive layer -- gh#592's own non-goals keep both.

BASH MATCHING IS A BEST-EFFORT HEURISTIC, NOT A PARSER -- gh#592's own PRD flags this as an
open spike ("whether Claude Code's PreToolUse hook API can reliably inspect a Bash command's
shell-parsed target path"). It catches the common mutating shapes named in the issue (git
commit/checkout --/reset/add/mv/rm/sed -i/shell redirection/`git -C $REPO <verb>`) that name a
path under $REPO, and deliberately leaves read-only forms (`git show`, `git diff`, `git log`,
`git status`, `cat`, `ls`, ...) alone rather than false-block them. A sufficiently obfuscated
command (a variable holding the path, a wrapper script) can still slip past it -- this is a
guard rail, not a sandbox, the same caveat postflight_dirty_check.sh's own header states for
its layer.

CREDENTIAL GUARD (fk#1494), the second job of this hook: it also blocks a Bash command, or a
Read/Grep, whose output would be a credential's VALUE (`env`, `echo $GH_TOKEN`, `cat
/root/.gh_token`) -- for every pass, worktree-isolated or not. See the "credential guard"
section below for what it judges, what it deliberately allows, and what it cannot stop.

Reads one PreToolUse hook payload (JSON) from stdin. Exit 0 = allow, exit 2 = block (Claude
Code shows stderr back to the model as the reason) -- the documented hook contract.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Verbs that mutate a git checkout or the filesystem. Deliberately excludes read-only verbs
# (show, diff, log, status, ls, cat, grep, less, head, tail) so a read-only reference to $REPO
# (gh#592 AC3: `git show origin/main:<path>`) is never blocked.
#
# The redirect alternative used to require `>`/`>>` to sit at the very start of the command or
# right after a `;`/`&`/`|` separator -- which never matches an ordinary `cmd > file` (the `>`
# there is preceded by the command's own words, not a separator). Fixed as part of gh#715 AC4:
# require only that `>`/`>>` be preceded by whitespace/start/a separator (so it reads as an
# operator, not `-mmethod>Object` arrows or `>=` comparisons) and allow the usual optional
# space before the target.
#
# gh#834: `checkout` used to require a literal `--` right after it (`git checkout -- <file>`,
# the file-restore form) -- so `git checkout <branch>`/`git switch <branch>`, which move HEAD
# to a different branch entirely, matched nothing at all. That is the exact shape of the
# fleet's own #834 incident (host checkout of the self-hosted instance left on a stray feature
# branch): confirmed live that `git checkout rework-metric` and `git switch rework-metric` both
# returned exit 0 (allowed) against this hook before this fix. `switch` and `pull` are added for
# the same reason -- both move/rewrite HEAD and were simply absent from the verb list.
_MUTATING_BASH_RE = re.compile(
    r"\bgit\s+(commit|checkout|switch|reset|add|merge|rebase|push|pull|stash\s+pop|clean)\b"
    r"|\b(rm|mv|cp|sed\s+-i|mkdir|touch|chmod|chown|tee)\b"
    r"|(?:^|[\s;&|])>>?(?!=)\s*\S"
)
# The redirect branch of _MUTATING_BASH_RE, capturing its target (msg#95).
_REDIRECT_RE = re.compile(r"(?:^|(?<=[\s;&|]))\d?>>?(?!=)\s*([^\s;&|]+)")
_MUTATING_GIT_VERB_RE = re.compile(
    r"\bgit\s+(?:commit|checkout|switch|reset|add|merge|rebase|push|pull|stash\s+pop|clean)\b")
_GIT_DASH_C_RE = re.compile(r"git\s+-C\s+(\S+)\s+(\S+)(?:\s+(\S+))?")
_MUTATING_SUBCOMMANDS = {"commit", "checkout", "switch", "reset", "add", "merge", "rebase", "push", "pull", "stash", "clean", "rm", "mv"}
# gh#894: a leading/compound `cd <path>` segment (`cd $REPO && git checkout <branch>`, or with
# `;`) changes the process's ACTUAL cwd by the time the git verb runs, but the Bash tool's own
# payload['cwd'] is always the pre-execution cwd (the pass's own $WT_PATH per run_member.sh) --
# PR #893's fallback never re-derived cwd from this, so this exact compound shape (the shape the
# PRD's incident theory names) sailed straight through. Only a bare `cd <path>` right after the
# start of the command or a `&&`/`;` separator counts -- PRD non-goal 1: no general shell parser,
# so a variable assigned mid-command, `eval`, or command substitution deciding the path is left
# uncovered and falls back to the existing pre-execution cwd unchanged.
_LEADING_CD_RE = re.compile(r"(?:^|&&|;)\s*cd\s+('[^']*'|\"[^\"]*\"|[^\s;&]+)")
# gh#837: `stash` alone is too coarse -- `stash list`/`stash show` are read-only, `stash pop`
# (and bare `stash`, which git treats as `stash push`) are not. Only `stash` gets this second
# check; every other verb in _MUTATING_SUBCOMMANDS stays decided by the verb alone.
_READONLY_STASH_SUBCOMMANDS = {"list", "show"}
_PATH_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")

# gh#715: a command that merely QUOTES a mutating verb or a shared-checkout path -- prose in a
# `gh issue comment --body "..."` argument, or a heredoc BODY -- must not be treated as if it
# typed that text as a real shell argument. Both are stripped before every regex/token check
# below; only single-token quoted values ('/repo/file', no internal whitespace) are left alone,
# since a real mutating command can legitimately quote its own path argument and blanket-
# stripping quotes would turn that into a new bypass -- the exact trap gh#715 itself names
# ("the workaround...would work just as well for a genuinely unsafe write").
_SQ_RE = re.compile(r"'([^']*)'")
_DQ_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_HEREDOC_RE = re.compile(r"(<<-?\s*['\"]?)(\w+)(['\"]?)(.*?)(\n[ \t]*\2\b)", re.DOTALL)


def _strip_prose(command: str) -> str:
    def _blank_if_multiword(m: "re.Match[str]") -> str:
        if re.search(r"\s", m.group(1)):
            quote = m.group(0)[0]
            return quote + quote
        return m.group(0)

    command = _HEREDOC_RE.sub(lambda m: m.group(1) + m.group(2) + m.group(3), command)
    command = _SQ_RE.sub(_blank_if_multiword, command)
    command = _DQ_RE.sub(_blank_if_multiword, command)
    return command


def _resolve(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _under(path: str, root: str) -> bool:
    path = path.rstrip("/") + "/"
    root = root.rstrip("/") + "/"
    return path == root or path.startswith(root)


def _effective_cwd(command: str, cwd_real: str | None, verb_start: int) -> str | None:
    """gh#894: walks `cd <path>` segments that occur strictly before `verb_start` (the position
    of the matched mutating verb), tracking cwd left to right the way a shell actually would.
    A `cd` with an unresolvable target (e.g. a literal `$REPO` the hook never expands -- PRD's
    own UNKNOWN, left fail-open on purpose) or one after `verb_start` is ignored and the cwd
    tracked so far is kept."""
    effective = cwd_real
    for m in _LEADING_CD_RE.finditer(command, 0, verb_start):
        target = m.group(1).strip("'\"")
        base = effective or os.getcwd()
        try:
            resolved = target if os.path.isabs(os.path.expanduser(target)) else os.path.join(base, target)
            effective = _resolve(resolved)
        except OSError:
            continue
    return effective


# --- credential guard (fk#1494) ---------------------------------------------------------------
# Every member runs with --dangerously-skip-permissions and GH_TOKEN in its environment, so one
# `env` (or one issue body that talks a member into it) prints the fleet's token into the
# transcript, the run log and the next pass's prompt. This blocks the commands whose OUTPUT is a
# credential's VALUE, and nothing else: naming a variable, passing it to a program
# (`curl -H "x: $QA_SESSION_TOKEN"`), testing it (`[ -n "$GH_TOKEN" ]`) all stay allowed.
#
# It judges each simple command on its own, split the way a shell splits (quotes, `;`, `&&`,
# `|`, `$(...)`), never by searching the whole string for a word: the first version of this
# matched `\bset\b` anywhere and so blocked `set -euo pipefail`, `env X=1 git push`, and a PR
# titled "Members can set a goal". A false block here stops every merge the fleet makes; a miss
# is where the fleet stood before. So when in doubt it ALLOWS.
#
# NOT A SANDBOX. A script that reads os.environ, a value piped through base64, a file copied
# and then read, `ssh box 'env'` -- all get past it. The real fix is that the model's
# environment holds no long-lived token at all (short-lived, repo-scoped tokens behind a
# credential helper). Until then this stops the accident and the lazy attack, not the determined
# one. Set FLEET_CREDENTIAL_GUARD=0 to turn it off without a deploy.
_SECRET_NAME_RE = re.compile(
    r"^(?:\w*(?:_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSWD)|\w*OAUTH_TOKEN_\w+|FLEET_WEBHOOK_TOKENS?\w*)$")
_EXPANSION_RE = re.compile(r"\$(?:([A-Za-z_]\w*)|\{([#!]?)([A-Za-z_]\w*)([^}]*)\})")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*\+?=")
# Words that come before the command a segment really runs.
_COMMAND_PREFIXES = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "time",
                     "sudo", "command", "builtin", "exec", "nohup"}
# Print a whole file / stream as-is.
_DUMPERS = {"cat", "head", "tail", "less", "more", "nl", "tac", "bat", "base64", "base32", "xxd",
            "od", "hexdump", "strings", "tee", "rev", "cut", "tr", "fold", "sort", "uniq"}
# Print parts of a file; their first plain argument is a pattern/program, not a file.
_PATTERN_READERS = {"grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "gawk", "jq", "yq"}
_REDIRECT = "\x02"   # marks a redirect operator word produced by _shell_segments
_INERT = "\x00"      # a `$` the shell will not expand (single-quoted or backslash-escaped)

_CREDENTIAL_PATH_RES = [
    re.compile(r"(?:^|/)\.gh_token$"),             # entrypoint.sh writes GH_TOKEN here for cron
    re.compile(r"(?:^|/)fleet-kit/gh_token$"),     # node_up.sh's copy on a worker node
    re.compile(r"(?:^|/)\.credentials\.json$"),    # each account's Claude login (/root/.claude-*/)
    re.compile(r"(?:^|/)\.git-credentials$"),
    re.compile(r"(?:^|/)gh/hosts\.yml$"),
    re.compile(r"(?:^|/)\.webhook_secret$"),
    re.compile(r"^/proc/[^/]+/environ$"),
]

_SAFE_WAYS = ("To list which variables exist: `compgen -e` (names only). To read one setting: "
              "`printenv NAME`. To test a credential is there: `[ -n \"$NAME\" ] && echo set`. "
              "Programs that need a credential read it from the environment themselves.")


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.match(name or ""))


def _holds_secret(name: str, env: dict, paths_too: bool = False) -> bool:
    """True when `$name` would expand to a credential in THIS pass: secret-shaped name, set in
    the environment the hook inherited from the pass, with a real value. Judging by the name
    alone blocked `echo "$DISPATCH_LOCK_KEY"` and every `$CACHE_KEY` a member's own one-liner
    defines. A value that is a path (GSC_SA_KEY points at a key FILE) is not itself the secret
    -- printing it is fine, `cat`-ing it is not (paths_too)."""
    value = env.get(name) or ""
    if not _is_secret_name(name) or len(value) < 12:
        return False
    return paths_too or not value.startswith(("/", "~", "./"))


def _is_credential_path(word: str) -> bool:
    word = word.replace(_INERT, "$")
    return any(rx.search(word) for rx in _CREDENTIAL_PATH_RES)


def _expanded_secrets(word: str, env: dict, paths_too: bool = False) -> list[str]:
    """Credentials this word EXPANDS to their value. `${NAME:+x}` ("x if set") and
    `${#NAME}` (its length) give nothing away and do not count; neither does a `$` the shell
    will not expand."""
    out = []
    for m in _EXPANSION_RE.finditer(word):
        if m.group(1):
            name = m.group(1)
        else:
            name, rest = m.group(3), m.group(4)
            out.extend(_expanded_secrets(rest, env, paths_too))  # ${OTHER:-$GH_TOKEN}
            if m.group(2) == "#" or rest.startswith((":+", "+")):
                continue
        if _holds_secret(name, env, paths_too):
            out.append(name)
    return out


_DECLARERS = {"export", "local", "readonly", "declare", "typeset"}
# Programs that take a credential as an argument and send it where it belongs:
# `curl -H "Authorization: token $(gh auth token)"`, `git push https://x:$(cat ...)@...`.
_CREDENTIAL_USERS = {"curl", "wget", "git", "docker", "podman", "ssh", "scp"}
_CAPTURED, _USED = "captured", "used"


def _shell_segments(command: str) -> list[tuple[list[str], str, str]]:
    """The simple commands in `command`, each as (words, separator that ended it, where its
    output goes).

    The third field is "" for a command whose output reaches the screen. _CAPTURED marks one
    inside `NAME=$( ... )`: the output goes into a variable (`export GH_TOKEN=$(cat
    /root/.gh_token)` is how cron itself hands the token on). _USED marks one inside `$( ... )`
    in the arguments of a program that consumes a credential (_CREDENTIAL_USERS).

    Quote-aware, so text a command merely quotes is never read as a command: a single-quoted
    string or a heredoc body is data, and its `$` is marked inert. A double-quoted string stays
    one word but keeps its `$` -- the shell expands it there. `$( ... )` and backticks run their
    own commands, so their contents come back as their own segments. Redirect operators are
    words prefixed with _REDIRECT; the word after one is its target. Best effort, like every
    other check in this file: no aliases, no functions, no command held in a variable."""
    command = _HEREDOC_RE.sub(lambda m: m.group(1) + m.group(2) + m.group(3), command)
    segs: list[tuple[list[str], str, str]] = []
    words: list[str] = []
    cur: list[str] = []
    started = False          # a word is open (so an empty "" still counts as a word)
    dq = False
    captured = ""
    subshells = 0            # plain `(` still open
    stack: list[tuple] = []  # contexts suspended by $( or ` or an array literal
    i, n = 0, len(command)

    def end_word():
        nonlocal cur, started
        if started:
            words.append("".join(cur))
        cur, started = [], False

    def end_seg(sep: str):
        nonlocal words
        end_word()
        if words:
            segs.append((words, sep, captured))
        words = []

    def push(kind: str):
        nonlocal words, cur, started, dq, captured
        stack.append((kind, words, cur, started, dq, captured))
        outer = [w for w in words if not (w in _COMMAND_PREFIXES or _ASSIGNMENT_RE.match(w)
                                          or w.startswith(_REDIRECT))]
        if captured != _CAPTURED:
            if _ASSIGNMENT_RE.match("".join(cur)) and all(w in _DECLARERS for w in outer):
                captured = _CAPTURED
            elif outer and os.path.basename(outer[0]) in _CREDENTIAL_USERS:
                captured = _USED
            else:
                captured = ""
        words, cur, started, dq = [], [], False, False

    def pop():
        nonlocal words, cur, started, dq, captured
        end_seg(")")
        _kind, words, cur, started, dq, captured = stack.pop()

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if c == "\\" and nxt:
            if nxt != "\n":          # backslash-newline just continues the line
                cur.append(_INERT if nxt == "$" else nxt)
                started = True
            i += 2
            continue
        if c == "$" and nxt == "(":
            push("(")
            i += 2
            continue
        if c == "`":
            if stack and stack[-1][0] == "`":
                pop()
            else:
                push("`")
            i += 1
            continue
        if dq:
            if c == '"':
                dq = False
            else:
                cur.append(c)
            i += 1
            continue
        if c == "'":
            end = command.find("'", i + 1)
            end = n if end < 0 else end
            cur.append(command[i + 1:end].replace("$", _INERT))
            started = True
            i = end + 1
            continue
        if c == '"':
            dq, started = True, True
            i += 1
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "#" and not started:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "\n" or c == ";":
            end_seg(c)
            i += 1
            continue
        if c == "&" and nxt == ">":
            c, i = ">", i + 1    # `&>file`: a redirect, not a separator
        elif c in "&|":
            end_seg(c + nxt if nxt == c else c)
            i += 2 if nxt == c else 1
            continue
        if c == "(":
            if started and "".join(cur).endswith("="):
                push("=(")       # arr=(env printenv): an array's words are data, not commands
                captured = _CAPTURED
            elif nxt == ")" and started:
                cur, started = [], False   # `env() { ...; }` defines a function, runs nothing
                i += 2
                continue
            else:
                end_seg("(")
                subshells += 1
            i += 1
            continue
        if c == ")":
            if stack and stack[-1][0] == "=(":
                words = []
                pop()
            elif stack and stack[-1][0] == "(":
                pop()
            elif subshells:
                subshells -= 1
                end_seg(")")
            else:                # `set)` / `env|printenv)` in a `case`: a pattern, not a command
                end_word()
                words = []
                while segs and segs[-1][1] == "|":
                    segs.pop()
            i += 1
            continue
        if c in "<>":
            fd = ""
            if started and "".join(cur).isdigit():
                fd = "".join(cur)          # the 2 of `2>`: part of the operator, not a word
                cur, started = [], False
            end_word()
            j = i
            while j < n and command[j] in "<>|":
                j += 1
            op = fd + command[i:j]
            m = re.match(r"&(?:\d+|-)", command[j:])
            if m:                          # `>&2`, `2>&1`: complete on its own, no target word
                op += m.group(0)
                j += m.end()
            words.append(_REDIRECT + op)
            i = j
            continue
        cur.append(c)
        started = True
        i += 1
    while stack:
        pop()
    end_seg("")
    return segs


def _split_redirects(words: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(plain words, [(operator, target)])."""
    plain, redirects, i = [], [], 0
    while i < len(words):
        w = words[i]
        if w.startswith(_REDIRECT):
            op = w[1:]
            if "&" in op or i + 1 >= len(words):
                redirects.append((op, ""))
            else:
                redirects.append((op, words[i + 1]))
                i += 1
        else:
            plain.append(w)
        i += 1
    return plain, redirects


# A `grep -o` pattern that can only ever match a variable's NAME: name characters, anchors,
# simple classes and alternation, at most one trailing `=`. No `.`, no negated class, no `\S` --
# nothing that could run past the `=` into the value.
_NAME_ONLY_PATTERN_RE = re.compile(r"^(?:[A-Za-z0-9_^*+?|()\[\]-]|\\[|()])+=?$")


def _no_values(next_words: list[str]) -> bool:
    """True when the next command in the pipe cannot print a value. Seen in real sessions:
    `env | grep -c '^FLEET_'` and `env | wc -l` (a count), `env | grep -q CI=` (nothing),
    `env | grep -o '^PHILANTHROPY_[A-Z_]*'` and `env | cut -d= -f1` (names only)."""
    plain = [w for w in _split_redirects(next_words)[0] if not _ASSIGNMENT_RE.match(w)]
    if not plain:
        return False
    cmd, args = os.path.basename(plain[0]), plain[1:]
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if cmd == "wc":
        return True
    if cmd == "cut":
        return "-d=" in args and ("-f1" in args or args[-2:] == ["-f", "1"])
    if cmd in ("grep", "egrep", "fgrep"):
        if "c" in short or "q" in short or "--count" in args or "--quiet" in args:
            return True
        patterns = [a.replace(_INERT, "$") for a in args if not a.startswith("-")]
        return ("o" in short and len(patterns) == 1 and "[^" not in patterns[0]
                and bool(_NAME_ONLY_PATTERN_RE.match(patterns[0])))
    return False


# Flags whose NEXT word is their value, not a file (`grep -A 3 pattern file`).
_GREP_VALUE_FLAGS = {"-A", "-B", "-C", "-m", "-d", "-D", "-g", "-t", "-T", "-j", "--include",
                     "--exclude", "--exclude-dir", "--glob", "--type", "--max-count", "--context",
                     "--after-context", "--before-context"}
_VALUE_FLAGS = {
    **{c: _GREP_VALUE_FLAGS for c in ("grep", "egrep", "fgrep", "rg", "ag")},
    "awk": {"-F", "-v"}, "gawk": {"-F", "-v"}, "jq": {"--indent"}, "yq": {"--indent"},
    "head": {"-n", "-c"}, "tail": {"-n", "-c"}, "cut": {"-d", "-f", "-c", "-b"},
    "sort": {"-k", "-t", "-o"}, "fold": {"-w"},
}
_PATTERN_FLAGS = {"-e", "-f", "--regexp", "--file", "--expression"}
_TWO_VALUE_FLAGS = {"--arg", "--argjson", "--slurpfile", "--rawfile"}


def _file_args(cmd: str, args: list[str]) -> list[str]:
    """The arguments of a reader that name FILES. For grep/sed/awk/jq the first plain argument
    is the pattern or program (`grep -rn "/root/.gh_token" scripts/` searches FOR that text), so
    it is dropped -- unless -e/-f already supplied it."""
    files, skip, pattern_given = [], 0, cmd not in _PATTERN_READERS
    for a in args:
        if skip:
            skip -= 1
        elif a in _TWO_VALUE_FLAGS:
            skip = 2
        elif a in _PATTERN_FLAGS:
            skip, pattern_given = 1, True
        elif a in _VALUE_FLAGS.get(cmd, ()):
            skip = 1
        elif a.startswith("-") and a != "-":
            continue
        elif not pattern_given:
            pattern_given = True
        else:
            files.append(a)
    return files


def _credential_dump(words: list[str], sep: str, next_words: list[str], env: dict) -> str | None:
    """Why this one simple command would print a credential's value, or None."""
    reason = _dump_reason(words, sep, next_words, env)
    if reason and "prints every" in reason and sep == "|" and _no_values(next_words):
        return None
    return reason


def _dump_reason(words: list[str], sep: str, next_words: list[str], env: dict) -> str | None:
    plain, redirects = _split_redirects(words)
    next_plain = [w for w in _split_redirects(next_words)[0] if not _ASSIGNMENT_RE.match(w)]
    next_cmd = os.path.basename(next_plain[0]) if next_plain else ""
    while plain and (plain[0] in _COMMAND_PREFIXES or _ASSIGNMENT_RE.match(plain[0])):
        plain = plain[1:]
    if not plain:
        return None
    cmd, args = os.path.basename(plain[0]), plain[1:]
    flags = [a for a in args if a.startswith("-") and a != "-"]
    names = [a for a in args if not a.startswith(("-", "+"))]

    if cmd == "env":
        rest, skip = [], False
        for k, a in enumerate(args):
            if skip:
                skip = False
            elif a in ("-u", "--unset", "-C", "--chdir"):
                skip = True
            elif not (a.startswith("-") or _ASSIGNMENT_RE.match(a)):
                rest = args[k:]
                break
        if not rest:
            return "`env` with no command to run prints every variable's value"
        kept = [w for op, t in redirects for w in ([_REDIRECT + op, t] if t else [_REDIRECT + op])]
        return _dump_reason(rest + kept, sep, next_words, env)
    if cmd == "printenv":
        if not names:
            return "`printenv` with no name prints every variable's value"
        leaked = [a for a in names if _holds_secret(a, env)]
        if leaked:
            return f"`printenv {leaked[0]}` prints that credential's value"
        return None
    if cmd == "set" and not args:
        return "`set` with no arguments prints every variable's value"
    if cmd in ("declare", "typeset"):
        letters = "".join(f.lstrip("-") for f in flags)
        if not names and not (letters and set(letters) <= set("fF")):
            return f"`{cmd}` with no variable name prints every variable's value"
        if "p" in letters and any(_holds_secret(a, env) for a in names):
            return f"`{cmd} -p` prints that credential's value"
        return None
    if cmd == "export" and not names:
        return "`export` with no assignment prints every exported variable's value"
    if cmd in ("bash", "sh", "zsh", "dash") and any("c" in f for f in flags if not f.startswith("--")):
        script = next((a for a in args if not a.startswith("-")), "")
        return credential_block_reason(script.replace(_INERT, "$"), env)
    if cmd == "eval" and args:
        return credential_block_reason(" ".join(args).replace(_INERT, "$"), env)
    if cmd == "gh" and args[:2] == ["auth", "token"]:
        return "`gh auth token` prints the GitHub token"
    if cmd == "gh" and args[:2] == ["auth", "status"] and ("--show-token" in args or "-t" in args):
        return "`gh auth status --show-token` prints the GitHub token"
    if cmd == "git" and names[:1] == ["credential"] and "fill" in names:
        return "`git credential fill` prints the stored password"

    if cmd in ("echo", "printf", "print"):
        leaked = [nm for a in args for nm in _expanded_secrets(a, env)]
        # stdout sent to a real file (`> f`, `1>> f`, `&> f`) -- `2>/dev/null` does not count
        to_file = any(op.lstrip("1") in (">", ">>", ">|") and t not in ("/dev/stdout", "/dev/stderr", "/dev/tty")
                      for op, t in redirects)
        # Piped into a program that USES the value (`| gh auth login --with-token`, `| wc -c`,
        # `| sha256sum`) or written to a file is not a print. Piped into another printer is.
        piped_to_user = sep == "|" and next_cmd not in _DUMPERS | _PATTERN_READERS
        if leaked and not to_file and not piped_to_user:
            return f"this `{cmd}` prints the value of ${leaked[0]}"
        return None

    if cmd in _DUMPERS | _PATTERN_READERS:
        files = _file_args(cmd, args) + [t for op, t in redirects if op.lstrip("0123456789").startswith("<")]
        for f in files:
            if _is_credential_path(f):
                return (f"`{cmd}` on {f.replace(_INERT, '$')} prints a stored credential (for an "
                        f"account's login state run `bash /fleet-kit/scripts/account_status.sh`)")
        if cmd in _DUMPERS:
            leaked = [nm for f in files for nm in _expanded_secrets(f, env, paths_too=True)]
            if leaked:
                return f"`{cmd}` on ${leaked[0]} prints that credential (or the key file it points at)"
    return None


def credential_block_reason(command: str, env: dict | None = None) -> str | None:
    """A block message if this Bash command would print a credential's value, else None."""
    env = os.environ if env is None else env
    segs = _shell_segments(command or "")
    for idx, (words, sep, goes_to) in enumerate(segs):
        if goes_to == _CAPTURED:
            continue
        next_words = segs[idx + 1][0] if sep == "|" and idx + 1 < len(segs) else []
        why = _credential_dump(words, sep, next_words, env)
        if why and goes_to == _USED and "prints every" not in why:
            continue   # one credential, handed straight to the program that needs it
        if why:
            if why.startswith("BLOCKED"):
                return why  # already a full message, from a `bash -c` / `eval` body
            return (f"BLOCKED by worktree_guard_hook.py (credential guard): {why}, and that would "
                    f"land in the run log and later prompts. {_SAFE_WAYS}")
    return None


def _credential_guard(payload: dict, env: dict) -> str | None:
    """Fails OPEN: a bug in the guard must never block the fleet's own work."""
    if (env.get("FLEET_CREDENTIAL_GUARD") or "1").strip() == "0":
        return None
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    try:
        if tool_name == "Bash":
            return credential_block_reason(tool_input.get("command") or "", env)
        if tool_name in ("Read", "Grep"):
            path = tool_input.get("file_path") or tool_input.get("path") or ""
            if path and _is_credential_path(os.path.expanduser(path)):
                return (f"BLOCKED by worktree_guard_hook.py (credential guard): {path} holds a "
                        f"stored credential, and reading it would land the value in the run log "
                        f"and later prompts. {_SAFE_WAYS}")
    except Exception as exc:  # noqa: BLE001
        print(f"worktree_guard_hook.py: credential guard error ({exc}), allowing", file=sys.stderr)
    return None


def _bash_targets_repo(command: str, repo_real: str, wt_real: str, cwd_real: str | None) -> bool:
    if not command:
        return False

    command = _strip_prose(command)

    # ALL `git -C <dir> <verb>` occurrences, not just the first -- a chained command like
    # `git -C $REPO log && git -C $REPO add -A && git -C $REPO commit -m wip` has an earlier,
    # innocent `-C $REPO log` before the mutating one; stopping at the first match (the
    # original bug here, caught in review) let the real mutation through.
    for m in _GIT_DASH_C_RE.finditer(command):
        subcmd = m.group(2).strip("'\"")
        try:
            target_dir = _resolve(m.group(1).strip("'\""))
        except OSError:
            target_dir = None
        if not (target_dir and subcmd in _MUTATING_SUBCOMMANDS and _under(target_dir, repo_real)):
            continue
        if subcmd == "stash":
            stash_sub = (m.group(3) or "").strip("'\"")
            if stash_sub in _READONLY_STASH_SUBCOMMANDS:
                continue  # `stash list`/`stash show` are reads, not writes (gh#837)
        return True

    if not _MUTATING_BASH_RE.search(command):
        return False

    # jefe msg#95: `cd /repo && gh issue list ... > /tmp/x.json` matched only on its redirect, and
    # the loop below then blocked on the unrelated `cd /repo` token. When a redirect is the ONLY
    # mutating trigger, judge the redirect targets alone: every one absolute and outside $REPO
    # is allowed. A relative target (written under whatever the cd chose) still falls through.
    if not _MUTATING_BASH_RE.search(_REDIRECT_RE.sub(" ", command)):
        targets = [t.strip("'\"") for t in _REDIRECT_RE.findall(command)]
        try:
            if targets and all(os.path.isabs(os.path.expanduser(t))
                               and not _under(_resolve(t), repo_real) for t in targets):
                return False
        except OSError:
            pass

    for raw_tok in _PATH_TOKEN_RE.findall(command):
        tok = raw_tok.strip("'\"")
        if not tok or not (tok.startswith("/") or tok.startswith("./") or tok.startswith("../") or tok.startswith("~")):
            continue
        if ":" in tok.split("/")[-1] and not os.path.exists(tok):
            continue  # e.g. origin/main:path -- a git ref, not a real filesystem path
        try:
            if _under(_resolve(tok), repo_real):
                return True
        except OSError:
            continue

    # gh#834: none of the above requires an explicit path at all -- `cd $REPO && git checkout
    # <branch>` never names $REPO inside the git command itself, so every check above sees only
    # "git checkout <branch>" and finds no path token to test. A mutating git verb with no `-C`
    # override implicitly operates on the process's cwd; if that cwd is the shared checkout
    # (and not this pass's own worktree), the command targets $REPO regardless of what it spells
    # out. `-C` is excluded here because it already redirects git elsewhere, and that case is
    # fully handled by the loop above (including the "allowed" case of `-C $WT_PATH`).
    #
    # gh#894: `cwd_real` itself is always the Bash tool's PRE-EXECUTION cwd (this pass's own
    # $WT_PATH per run_member.sh) -- it is never updated by a `cd` that lives INSIDE the command
    # string. `_effective_cwd` walks any leading/compound `cd <path>` segments before the
    # matched mutating verb so `cd $REPO && git checkout <branch>` (one command, cwd starts at
    # $WT_PATH) is judged by the cwd the git verb actually runs under, not the shell's starting
    # cwd -- PR #893's own comment claimed this closed, and it did not (judge-judy on #893).
    git_verb_match = _MUTATING_GIT_VERB_RE.search(command)
    if git_verb_match and not _GIT_DASH_C_RE.search(command):
        effective_cwd = _effective_cwd(command, cwd_real, git_verb_match.start())
        if effective_cwd and _under(effective_cwd, repo_real) and not _under(effective_cwd, wt_real):
            return True
    return False


def decide(payload: dict, env: dict) -> str | None:
    """Returns a block reason, or None to allow."""
    # Before the worktree checks, and for EVERY pass: a pass with no worktree of its own (jefe)
    # holds the same token.
    reason = _credential_guard(payload, env)
    if reason:
        return reason

    wt_path = (env.get("WT_PATH") or "").strip()
    repo = (env.get("REPO") or "").strip()
    if not wt_path or not repo:
        return None  # not worktree-isolated this pass -- nothing to enforce (AC4)

    try:
        repo_real = _resolve(repo)
        wt_real = _resolve(wt_path)
    except OSError:
        return None  # can't resolve either path -- fail open rather than block on our own error

    if repo_real == wt_real:
        return None  # WT_PATH somehow equals REPO -- not actually isolated

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return None
        try:
            target_real = _resolve(file_path)
        except OSError:
            return None
        if _under(target_real, repo_real) and not _under(target_real, wt_real):
            return (f"BLOCKED by worktree_guard_hook.py (gh#592): {file_path} resolves under the "
                     f"SHARED checkout {repo}, but this pass is isolated in its own worktree "
                     f"{wt_path}. Make this change under {wt_path} instead.")
        return None

    if tool_name == "Bash":
        command = tool_input.get("command") or ""
        # gh#834: Claude Code's PreToolUse payload carries the command's own `cwd` -- read it so
        # a bare mutating command with no explicit path (`cd $REPO && git checkout <branch>`)
        # can be caught via cwd, not just via a path token spelled out in the command string.
        cwd = payload.get("cwd") or ""
        try:
            cwd_real = _resolve(cwd) if cwd else None
        except OSError:
            cwd_real = None
        if _bash_targets_repo(command, repo_real, wt_real, cwd_real):
            return (f"BLOCKED by worktree_guard_hook.py (gh#592): this Bash command appears to "
                     f"mutate the SHARED checkout {repo} directly, but this pass is isolated in "
                     f"its own worktree {wt_path}. Target {wt_path} instead (a read-only "
                     f"reference like `git show origin/main:<path>` is not blocked).")
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed input from the CLI itself is not this pass's mutation to judge -- fail
        # open rather than block every tool call fleet-wide on a hook bug.
        print(f"worktree_guard_hook.py: could not parse stdin ({exc}), allowing", file=sys.stderr)
        return 0

    reason = decide(payload, os.environ)
    if reason:
        import hook_blocks
        hook_blocks.record(payload, "worktree_guard_hook", reason)
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
