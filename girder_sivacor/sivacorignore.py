"""The ``.sivacorignore`` rules, and the files they must never delete.

``prune_workspace`` deletes whatever these rules match after the last stage.
Every stage's main file and the governing ``.sivacorignore`` must survive it,
and :func:`find_violations` lets ``create_workspace`` refuse a package whose
rules would delete either, before any image is pulled. Matching is git's own,
translated from ``dir.c`` and ``wildmatch.c``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

IGNORE_FILENAME = ".sivacorignore"

#: The largest ``.sivacorignore`` honoured; one over it is refused, never
#: read in part, since a rule past the cut would be silently dropped.
IGNORE_FILE_READ_LIMIT = 1024 * 1024

#: Applied to every package, before (and so overridable by) the researcher's own.
DEFAULT_PATTERNS = [
    # Python
    "__pycache__/",
    "*.py[cod]",
    "*$py.class",
    ".venv/",
    "venv/",
    ".env",
    # Version Control & IDEs
    ".git/",
    ".svn/",
    ".idea/",
    ".vscode/",
    "*.swp",
    ".DS_Store",  # macOS noise
    "Thumbs.db",  # Windows noise
    # Common Research/Data temporary files
    "*.tmp",
    "node_modules/",
    # Jupyter Notebook checkpoints
    ".ipynb_checkpoints/",
    # Stata/R temporary files
    "*.smcl",
    ".Rhistory",
    ".RData",
    ".Ruserdata",
]


def choose_ignore_file(paths):
    """The governing ``.sivacorignore``: the shallowest, ties broken by name."""
    candidates = [p for p in paths if posixpath.basename(p) == IGNORE_FILENAME]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.count("/"), p))


def decode(data):
    """A ``.sivacorignore``'s bytes as text, BOM dropped as git does.

    ``surrogateescape``, as ``os.walk`` decodes names, so a Latin-1 pattern
    still matches the Latin-1 name it was written for.
    """
    return data.removeprefix(b"\xef\xbb\xbf").decode("utf-8", "surrogateescape")


def printable(text):
    """``text`` safe to put in a message: undecodable bytes as ``\\xNN``."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _trim_trailing_spaces(line):
    # git's trim_trailing_spaces: unescaped trailing spaces only, not tabs.
    last_space = None
    i = 0
    while i < len(line):
        if line[i] == " ":
            if last_space is None:
                last_space = i
        elif line[i] == "\\":
            i += 1
            if i == len(line):
                return line
            last_space = None
        else:
            last_space = None
        i += 1
    return line if last_space is None else line[:last_space]


def read_patterns(text):
    """``(line number, pattern)`` for each rule, split as git's
    ``add_patterns_from_buffer`` does (on ``\\n`` only, unlike splitlines)."""
    patterns = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        line = line.removesuffix("\r")
        if not line or line.startswith("#"):
            continue
        if line := _trim_trailing_spaces(line):
            patterns.append((lineno, line))
    return patterns


class InvalidPattern(ValueError):
    """A line git reads but that matches nothing there, such as a lone ``!``."""

    def __init__(self, lineno, pattern, reason):
        super().__init__(reason)
        self.lineno, self.pattern, self.reason = lineno, pattern, reason


class _Unmatchable(Exception):
    pass


_CLASSES = {
    "alnum": lambda c: c.isascii() and c.isalnum(),
    "alpha": lambda c: c.isascii() and c.isalpha(),
    "blank": lambda c: c in " \t",
    "cntrl": lambda c: ord(c) < 32 or ord(c) == 127,
    "digit": lambda c: "0" <= c <= "9",
    "graph": lambda c: 33 <= ord(c) <= 126,
    "lower": lambda c: "a" <= c <= "z",
    "print": lambda c: 32 <= ord(c) <= 126,
    "punct": lambda c: 33 <= ord(c) <= 126 and not c.isalnum(),
    "space": lambda c: c in " \t\n\r",
    "upper": lambda c: "A" <= c <= "Z",
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}
_BYTES = [chr(b) for b in range(256)]


def _bracket(pattern, i):
    """``(regex, index after the ])`` for the ``[...]`` at ``pattern[i]``."""
    end = len(pattern)
    i += 1
    if i < end and pattern[i] in "!^":
        negated = True
        i += 1
    else:
        negated = False
    chars = set()
    prev = None
    first = True
    while True:
        if i >= end:
            raise _Unmatchable("it has an unterminated '['")
        c = pattern[i]
        if c == "]" and not first:
            break
        first = False
        if c == "\\":
            i += 1
            if i >= end:
                raise _Unmatchable("it has an unterminated '['")
            c = pattern[i]
            chars.add(c)
            prev = c
        elif c == "-" and prev is not None and i + 1 < end and pattern[i + 1] != "]":
            i += 1
            hi = pattern[i]
            if hi == "\\":
                i += 1
                if i >= end:
                    raise _Unmatchable("it has an unterminated '['")
                hi = pattern[i]
            chars.update(_BYTES[ord(prev) : ord(hi) + 1])
            prev = None
        elif c == "[" and i + 1 < end and pattern[i + 1] == ":":
            close = pattern.find("]", i + 2)
            if close == -1:
                raise _Unmatchable("it has an unterminated '['")
            if close - 1 < i + 2 or pattern[close - 1] != ":":
                # No ":]": git reads the "[" as an ordinary member.
                chars.add("[")
                prev = "["
            else:
                name = pattern[i + 2 : close - 1]
                if name not in _CLASSES:
                    raise _Unmatchable(f"'[:{name}:]' is not a character class")
                chars.update(filter(_CLASSES[name], _BYTES))
                prev = None
                i = close
        else:
            chars.add(c)
            prev = c
        i += 1
    # WM_PATHNAME: a bracket never matches "/".
    chars.discard("/")
    members = "".join(f"\\x{ord(c):02x}" for c in sorted(chars))
    if negated:
        return f"[^{members}/]", i + 1
    return (f"[{members}]" if members else "(?!)"), i + 1


def _wildmatch(pattern):
    """A regex matching what git's ``wildmatch(pattern, ..., WM_PATHNAME)`` does."""
    out = []
    i, end = 0, len(pattern)
    while i < end:
        c = pattern[i]
        if c == "\\":
            i += 1
            if i == end:
                raise _Unmatchable(
                    "it ends in a single backslash (write '\\ ' for a trailing space)"
                )
            out.append(re.escape(pattern[i]))
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            regex, i = _bracket(pattern, i)
            out.append(regex)
        elif c == "*":
            start = i
            while i < end and pattern[i] == "*":
                i += 1
            at_boundary = (start == 0 or pattern[start - 1] == "/") and (
                i == end or pattern[i] == "/" or pattern.startswith("\\/", i)
            )
            if i - start == 1 or not at_boundary:
                out.append("[^/]*")
            elif i == end:
                out.append(".*")  # "dir/**": everything inside
            else:
                out.append("(?:.*/)?")  # "**/": zero or more directories
                i += 2 if pattern[i] == "\\" else 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def _byte_view(text):
    """``text`` as one character per byte, which is what git compares."""
    return text.encode("utf-8", "surrogateescape").decode("latin-1")


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern
    negative: bool
    must_be_dir: bool
    basename_only: bool

    @classmethod
    def parse(cls, line):
        """git's ``parse_path_pattern`` and ``match_basename``/``match_pathname``."""
        body = _byte_view(line)
        negative = body.startswith("!")
        if negative:
            body = body[1:]
        must_be_dir = body.endswith("/")
        if must_be_dir:
            body = body[:-1]
        basename_only = "/" not in body
        if not basename_only:
            body = body.removeprefix("/")
        if not body:
            raise _Unmatchable(
                "a '!' must be followed by a pattern"
                if negative and not must_be_dir
                else "it has nothing but slashes"
            )
        return cls(_wildmatch(body), negative, must_be_dir, basename_only)

    def matches(self, view, is_dir):
        if self.must_be_dir and not is_dir:
            return False
        target = view.rpartition("/")[2] if self.basename_only else view
        return self.regex.fullmatch(target) is not None


@dataclass
class Rules:
    """The pruning rules for one package.

    ``source`` is the governing ``.sivacorignore``'s package-relative path, or
    ``None`` for the defaults alone; nothing outside its directory is pruned.
    """

    source: str | None = None
    user_patterns: list[tuple[int, str]] = field(default_factory=list)

    def __post_init__(self):
        self.base = posixpath.dirname(self.source) if self.source else ""
        self._patterns = [_Pattern.parse(line) for line in DEFAULT_PATTERNS]
        for lineno, pattern in self.user_patterns:
            try:
                self._patterns.append(_Pattern.parse(pattern))
            except _Unmatchable as exc:
                raise InvalidPattern(lineno, pattern, str(exc)) from None

    @classmethod
    def from_text(cls, source, text):
        return cls(source, read_patterns(text))

    def relative(self, path):
        """``path`` relative to the rules' base, or ``None`` if outside it."""
        if not self.base:
            return path
        if not path.startswith(self.base + "/"):
            return None
        return path[len(self.base) + 1 :]

    def _excluded_by(self, view, is_dir):
        # The last matching pattern decides, as in git.
        for index in reversed(range(len(self._patterns))):
            pattern = self._patterns[index]
            if pattern.matches(view, is_dir):
                return None if pattern.negative else index
        return None

    def excluded_by(self, path, is_dir=False):
        """The rule that excludes ``path`` itself, or ``None``, assuming no
        directory above it is excluded -- as the pruning walk knows."""
        relative = self.relative(path)
        if relative is None:
            return None
        index = self._excluded_by(_byte_view(relative), is_dir)
        return None if index is None else self._describe(index)

    def pruned_by(self, path, is_dir=False):
        """The rule that deletes ``path`` (package-relative), or ``None``.

        An excluded directory takes everything below it, whatever a later
        negation says, as in git.
        """
        parts = path.split("/")
        for depth in range(1, len(parts)):
            if rule := self.excluded_by("/".join(parts[:depth]), is_dir=True):
                return rule
        return self.excluded_by(path, is_dir)

    def _describe(self, index):
        if index < len(DEFAULT_PATTERNS):
            return f"SIVACOR's default rule '{DEFAULT_PATTERNS[index]}'"
        lineno, pattern = self.user_patterns[index - len(DEFAULT_PATTERNS)]
        return f"'{printable(pattern)}' on line {lineno} of {printable(self.source)}"


@dataclass(frozen=True)
class Violation:
    path: str
    what: str
    rule: str

    def __str__(self):
        return f"{printable(self.path)} ({self.what}) is matched by {self.rule}"


def find_violations(rules, main_file_paths):
    """The main files, and the governing ``.sivacorignore``, that ``rules``
    would delete."""
    protected = [(p, "main file") for p in sorted(set(main_file_paths))]
    if rules.source:
        protected.append((rules.source, "ignore file"))
    violations = []
    for path, what in protected:
        if rule := rules.pruned_by(path):
            violations.append(Violation(path, what, rule))
    return violations
