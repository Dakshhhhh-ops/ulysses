"""The one tool the model gets: a shell that runs only the Flagsmith CLI and the skill's stop rule.

It is not a real shell. It parses the small subset of POSIX sh the skill's own commands use and
refuses everything else before anything runs:

* commands: ``flagsmith ...`` and ``python scripts/stop_rule.py ...`` (``python3`` and ``py`` too);
* pipes between them, brace groups ``{ a; b; }`` (the skill's look), ``;``, ``&&`` and newlines;
* ``NAME=value`` and ``NAME=$(allowed command)`` assignments, kept for the session, and ``$NAME`` /
  ``${NAME}`` expansion (the skill's ``$E``, ``$X`` and ``$A``); an unset variable is refused, as with ``set -u``;
* ``'...'`` and ``"..."`` quoting, ``# comments``, here-documents (``--input - <<'EOF'``) and ``2>&1``.

Anything else (another program, ``>`` or ``<`` redirection, backticks, ``||``, ``&``, subshells) is
refused with a message saying what is allowed. Inside ``flagsmith`` it also refuses the commands and
flags that would print a credential, log in, rebind the checkout or point the CLI somewhere else.

The harness supplies ``runner(argv, stdin) -> (returncode, stdout, stderr)``, which holds the
Flagsmith URL and key in the child's environment; the model never sees them. Every output is passed
through ``scrub`` with the harness's secrets before the model or the transcript gets it.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable

Runner = Callable[[list[str], str | None], tuple[int, str, str]]

ALLOWED_HELP = ("only `flagsmith ...` and `python scripts/stop_rule.py ...` may run here, with pipes, "
                "{ a; b; } groups, ;, &&, NAME=value or NAME=$(...) assignments and here-documents")
FLAGSMITH = ("flagsmith", "flagsmith.exe")
PYTHON = ("python", "python3", "py")
STOP_RULE = "scripts/stop_rule.py"
# flagsmith subcommands (first words) the model may not run: they print or store credentials, or rebind the checkout
BANNED_WORDS = {("init",), ("login",), ("logout",), ("auth", "login"), ("auth", "logout"), ("auth", "token"),
                ("environment", "key"), ("env", "key")}
BANNED_FLAGS = ("--api-url", "--sdk-api-url", "--config-path", "-c")
BANNED_IN_ARGS = re.compile(r"api-keys|master-api-keys|/users?/|/auth/", re.I)
MAX_OUTPUT = 6000


class Refused(Exception):
    """The command is outside the allowlist; nothing was run."""


# ------------------------------------------------------------------------------------------------- parsing


@dataclass
class Word:
    parts: list  # ("lit", str) | ("var", name) | ("sub", Script)
    quoted_any: bool = False

    def literal(self) -> str | None:
        if all(k == "lit" for k, _ in self.parts):
            return "".join(v for _, v in self.parts)
        return None


@dataclass
class Cmd:
    words: list[Word]
    heredoc: tuple[str, bool] | None = None  # (body, expand)


@dataclass
class Group:
    script: "Script"


@dataclass
class Assign:
    name: str
    value: Word


@dataclass
class Pipeline:
    elements: list  # Cmd | Group


@dataclass
class Script:
    items: list = field(default_factory=list)  # (Assign | Pipeline, connector) ; connector is ";" or "&&"


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.pending_heredocs: list[tuple[Cmd, str, bool]] = []

    # -- low level

    def peek(self, n: int = 1) -> str:
        return self.s[self.i:self.i + n]

    def at_end(self) -> bool:
        return self.i >= len(self.s)

    def skip_blanks(self) -> None:
        while not self.at_end():
            c = self.s[self.i]
            if c in " \t":
                self.i += 1
            elif c == "\\" and self.peek(2) in ("\\\n", "\\\r"):
                self.i += 2
                if self.peek() == "\n":
                    self.i += 1
            elif c == "#":
                while not self.at_end() and self.s[self.i] != "\n":
                    self.i += 1
            else:
                return

    def newline(self) -> None:
        """Consume one newline, then the bodies of any here-documents opened on that line."""
        if self.peek() == "\r":
            self.i += 1
        self.i += 1
        for cmd, delim, expand in self.pending_heredocs:
            lines = []
            while True:
                if self.at_end():
                    raise Refused(f"here-document not closed: no line `{delim}`")
                j = self.s.find("\n", self.i)
                line = self.s[self.i:] if j < 0 else self.s[self.i:j]
                self.i = len(self.s) if j < 0 else j + 1
                if line.rstrip("\r").strip() == delim:
                    break
                lines.append(line.rstrip("\r"))
            cmd.heredoc = ("\n".join(lines) + "\n", expand)
        self.pending_heredocs = []

    # -- grammar

    def script(self, closer: str | None = None) -> Script:
        sc = Script()
        while True:
            self.skip_blanks()
            if self.at_end():
                if closer:
                    raise Refused(f"missing `{closer}`")
                break
            c = self.peek()
            if c in "\r\n":
                self.newline()
                continue
            if c == ";":
                self.i += 1
                continue
            if closer and c == closer and closer == "}":
                self.i += 1
                return sc
            if closer == ")" and c == ")":
                self.i += 1
                return sc
            item = self.statement()
            self.skip_blanks()
            conn = ";"
            if self.peek(2) == "&&":
                self.i += 2
                conn = "&&"
            elif self.peek(2) == "||":
                raise Refused("`||` is not allowed; " + ALLOWED_HELP)
            sc.items.append((item, conn))
        return sc

    def statement(self):
        m = _ASSIGN.match(self.s, self.i)
        if m:
            self.i = m.end()
            value = self.word(allow_empty=True)
            self.skip_blanks()
            if not self.at_end() and self.peek() not in ";\r\n}&)":
                raise Refused("`NAME=value command` (an environment prefix) is not allowed; assign on its own")
            return Assign(m.group(1), value)
        elements = [self.element()]
        while True:
            self.skip_blanks()
            if self.peek() == "|" and self.peek(2) != "||":
                self.i += 1
                self.skip_blanks()
                while self.peek() in ("\n", "\r"):
                    self.newline()
                    self.skip_blanks()
                elements.append(self.element())
            else:
                break
        return Pipeline(elements)

    def element(self):
        self.skip_blanks()
        c = self.peek()
        if c == "{" and self.peek(2)[1:2] in (" ", "\t", "\n", "\r"):
            self.i += 1
            return Group(self.script(closer="}"))
        if c == "(":
            raise Refused("subshells `( ... )` are not allowed; use { a; b; }")
        words: list[Word] = []
        cmd = Cmd(words)
        while True:
            self.skip_blanks()
            if self.at_end():
                break
            c = self.peek()
            if c in ";\r\n|)" or self.peek(2) == "&&":
                break
            if c == "}" and words:
                break
            if c == "&":
                raise Refused("background `&` is not allowed; " + ALLOWED_HELP)
            if self.peek(4) == "2>&1":
                self.i += 4
                continue
            if self.peek(2) == "<<":
                self.i += 2
                if self.peek() == "-":
                    self.i += 1
                self.skip_blanks()
                dw = self.word()
                delim = "".join(v for k, v in dw.parts if k == "lit")
                if not delim:
                    raise Refused("here-document needs a delimiter")
                self.pending_heredocs.append((cmd, delim, not dw.quoted_any))
                continue
            if c in "<>":
                raise Refused("redirection with < or > is not allowed; " + ALLOWED_HELP)
            if c == "`":
                raise Refused("backticks are not allowed; use NAME=$(flagsmith ...)")
            words.append(self.word())
        if not words:
            raise Refused("empty command")
        return cmd

    def word(self, allow_empty: bool = False) -> Word:
        parts: list = []
        quoted = False
        buf: list[str] = []

        def flush():
            if buf:
                parts.append(("lit", "".join(buf)))
                buf.clear()

        while not self.at_end():
            c = self.peek()
            if c in " \t\r\n;|&<>)":
                break
            if c == "`":
                raise Refused("backticks are not allowed; use NAME=$(flagsmith ...)")
            if c == "'":
                quoted = True
                j = self.s.find("'", self.i + 1)
                if j < 0:
                    raise Refused("unclosed single quote")
                buf.append(self.s[self.i + 1:j])
                self.i = j + 1
            elif c == '"':
                quoted = True
                self.i += 1
                while True:
                    if self.at_end():
                        raise Refused("unclosed double quote")
                    d = self.peek()
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.peek(2)[1:2] in ('"', "\\", "$", "`"):
                        buf.append(self.s[self.i + 1])
                        self.i += 2
                    elif d == "$":
                        flush()
                        parts.append(self.dollar())
                    elif d == "`":
                        raise Refused("backticks are not allowed; use NAME=$(flagsmith ...)")
                    else:
                        buf.append(d)
                        self.i += 1
            elif c == "\\":
                nxt = self.peek(2)[1:2]
                if nxt in ("\n", "\r"):
                    break
                buf.append(nxt)
                self.i += 2
            elif c == "$":
                flush()
                parts.append(self.dollar())
            else:
                buf.append(c)
                self.i += 1
        flush()
        if not parts and not allow_empty and not quoted:
            raise Refused(f"unexpected character {self.peek()!r}")
        return Word(parts or [("lit", "")], quoted)

    def dollar(self):
        self.i += 1
        if self.peek() == "(":
            if self.peek(2) == "((":
                raise Refused("arithmetic $(( )) is not allowed")
            self.i += 1
            return ("sub", self.script(closer=")"))
        if self.peek() == "{":
            j = self.s.find("}", self.i)
            name = self.s[self.i + 1:j] if j > 0 else ""
            if not _NAME.fullmatch(name):
                raise Refused("only plain ${NAME} expansion is allowed")
            self.i = j + 1
            return ("var", name)
        m = _NAME.match(self.s, self.i)
        if not m:
            return ("lit", "$")
        self.i = m.end()
        return ("var", m.group(0))


def parse(text: str) -> Script:
    p = _Parser(text)
    sc = p.script()
    if p.pending_heredocs:
        raise Refused("here-document body missing")
    return sc


# ------------------------------------------------------------------------------------------------- checking


def _command_kind(words: list[str]) -> str:
    head = words[0]
    if posixpath.basename(head.replace("\\", "/")) in FLAGSMITH:
        return "flagsmith"
    if head in PYTHON:
        if len(words) < 2 or posixpath.normpath(words[1].replace("\\", "/")).split("/")[-2:] != ["scripts",
                                                                                                    "stop_rule.py"]:
            raise Refused(f"python may only run {STOP_RULE}; " + ALLOWED_HELP)
        return "stop_rule"
    raise Refused(f"`{head}` is not allowed; " + ALLOWED_HELP)


def check_flagsmith_args(args: list[str]) -> None:
    positional = [a for a in args if not a.startswith("-")]
    for banned in BANNED_WORDS:
        if tuple(positional[:len(banned)]) == banned:
            raise Refused(f"`flagsmith {' '.join(banned)}` is not allowed here: the harness holds the credentials "
                          "and the checkout is already bound to the project and environment")
    for a in args:
        if a.split("=")[0] in BANNED_FLAGS:
            raise Refused(f"`{a.split('=')[0]}` is not allowed: the harness points the CLI at the lab")
        if BANNED_IN_ARGS.search(a):
            raise Refused("API key, user and auth endpoints are not allowed here")


def _check_static(sc: Script) -> None:
    for item, _ in sc.items:
        if isinstance(item, Assign):
            _check_word(item.value)
            continue
        for el in item.elements:
            if isinstance(el, Group):
                _check_static(el.script)
                continue
            head = el.words[0].literal()
            if head is None:
                raise Refused("the command name must be written out, not taken from a variable")
            second = el.words[1].literal() if len(el.words) > 1 else None
            kind = _command_kind([head] + ([second] if second is not None else []))
            lits = [w.literal() for w in el.words]
            if kind == "flagsmith" and None not in lits:
                check_flagsmith_args(lits[1:])
            for w in el.words:
                _check_word(w)


def _check_word(w: Word) -> None:
    for k, v in w.parts:
        if k == "sub":
            _check_static(v)


def check(text: str) -> Script:
    """Parse and check the whole command before anything runs. Raises Refused."""
    sc = parse(text)
    if not sc.items:
        raise Refused("empty command")
    _check_static(sc)
    return sc


# ------------------------------------------------------------------------------------------------- running


def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "<redacted>")
    text = re.sub(r"\bser\.[A-Za-z0-9]{8,}", "<redacted>", text)
    return re.sub(r"\bsk-[A-Za-z0-9_\-]{16,}", "<redacted>", text)


@dataclass
class Result:
    output: str
    returncode: int
    argvs: list[list[str]]  # every command that ran, as the model wrote it (after expansion)
    refused: str | None = None
    runs: list = field(default_factory=list)  # (argv, returncode, stdout) for each command that ran


class Shell:
    """Session state (variables) plus the allowlist, around a runner the harness provides."""

    def __init__(self, runner: Runner, secrets: list[str] | None = None, max_output: int = MAX_OUTPUT):
        self.runner = runner
        self.secrets = list(secrets or [])
        self.vars: dict[str, str] = {}
        self.max_output = max_output

    def run(self, text: str) -> Result:
        try:
            sc = check(text)
        except Refused as exc:
            msg = f"REFUSED by the harness, nothing ran: {exc}"
            return Result(msg, 126, [], refused=str(exc))
        self._argvs: list[list[str]] = []
        self._runs: list = []
        self._err: list[str] = []
        try:
            out, rc = self._script(sc, None)
        except Refused as exc:
            msg = f"REFUSED by the harness: {exc}"
            return Result(scrub(msg, self.secrets), 126, self._argvs, refused=str(exc), runs=self._runs)
        text_out = out
        if self._err:
            text_out = (out.rstrip("\n") + "\n" if out.strip() else "") + "".join(self._err)
        text_out = scrub(text_out, self.secrets)
        if rc != 0:
            text_out = (text_out.rstrip("\n") + "\n" if text_out.strip() else "") + f"[exit status {rc}]"
        if len(text_out) > self.max_output:
            text_out = text_out[:self.max_output] + f"\n[output cut at {self.max_output} characters; use --jq]"
        return Result(text_out if text_out.strip() else "(no output)", rc, self._argvs, runs=self._runs)

    # -- execution

    def _expand(self, w: Word) -> str:
        out = []
        for k, v in w.parts:
            if k == "lit":
                out.append(v)
            elif k == "var":
                out.append(self._var(v))
            else:
                text, _ = self._script(v, None)
                out.append(text.rstrip("\n"))
        return "".join(out)

    def _expand_heredoc(self, body: str) -> str:
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                      lambda m: self._var(m.group(1) or m.group(2)), body)

    def _var(self, name: str) -> str:
        """Like `set -u`: an unset variable is an error, not an empty string in an API path."""
        if name not in self.vars:
            raise Refused(f"${name} is not set in this session; set it first (for example "
                          f"E=$(flagsmith config --jq .environment.value)); variables persist between calls")
        return self.vars[name]

    def _script(self, sc: Script, stdin: str | None) -> tuple[str, int]:
        outs, rc = [], 0
        for item, conn in sc.items:
            if isinstance(item, Assign):
                self.vars[item.name] = self._expand(item.value)
                rc = 0
            else:
                o, rc = self._pipeline(item, stdin)
                outs.append(o)
            if conn == "&&" and rc != 0:
                break
        return "".join(outs), rc

    def _pipeline(self, pl: Pipeline, stdin: str | None) -> tuple[str, int]:
        data, rc = stdin, 0
        for el in pl.elements:
            if isinstance(el, Group):
                data, rc = self._script(el.script, data)
            else:
                data, rc = self._cmd(el, data)
        return data or "", rc

    def _cmd(self, cmd: Cmd, stdin: str | None) -> tuple[str, int]:
        argv = [self._expand(w) for w in cmd.words]
        kind = _command_kind(argv)
        if kind == "flagsmith":
            check_flagsmith_args(argv[1:])
        if cmd.heredoc is not None:
            body, expand = cmd.heredoc
            stdin = self._expand_heredoc(body) if expand else body
        self._argvs.append(argv)
        rc, out, err = self.runner(argv, stdin)
        self._runs.append((argv, rc, scrub(out, self.secrets)))
        if err:
            self._err.append(err if err.endswith("\n") else err + "\n")
        return out, rc


__all__ = ["Shell", "Result", "Refused", "check", "parse", "scrub", "check_flagsmith_args"]
