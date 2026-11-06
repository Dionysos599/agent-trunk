"""Transition checks: task-supplied acceptance over the pair *(main before this landing, main after)* (doc 12, 2.4).

A task may declare what must be true because it landed::

    "accept": [{"kind": "increases", "path": "stats.py", "pattern": "COUNT = (\\d+)", "by": 1},
               {"kind": "contains", "path": "registry.py", "text": "REGISTRY['h3']"}]

The reference point is **main immediately before the landing** (``old``), not the agent's fork base: against the
fork base a correct task would fail whenever anyone else landed first. ``new`` is the candidate that would become
main. For two agents that both turned ``COUNT = 0`` into ``COUNT = 1``, ``old`` has 1 and the candidate has 1, so
``increases`` by 1 fails and the landing is refused at once, with no model tokens.

========== ==========================================================================================
kind       passes when
========== ==========================================================================================
contains   the file in ``new`` contains ``text`` (or matches ``pattern`` with ``"regex": true``)
absent     the file in ``new`` does not contain it (a missing file passes)
increases  the integer captured by ``pattern`` (one group) is the old one plus ``by`` (default 1)
decreases  the same with minus
exists     the file is present in ``new``
missing    the file is absent from ``new``
command    ``argv`` exits 0 in a folder holding ``new``; with ``pattern`` and ``by`` its output's number
           also changed by ``by`` between ``old`` and ``new`` (the command runs in both)
========== ==========================================================================================

Everything is checked strictly when a task is *declared* (unknown keys, bad regex, a regex without exactly one
group, a non-positive ``by``): a typo that silently weakened a check is worse than an error. ``unfalsifiable`` reports
checks that already pass on an unchanged tree (they cannot tell whether the task did its work).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .paths import InvalidPath, normalize

KINDS = ("contains", "absent", "increases", "decreases", "exists", "missing", "command")
MAX_CHECKS = 50
MAX_TEXT = 10_000
MAX_PATTERN = 1_000
MAX_BYTES = 4_000_000
MAX_BY = 10 ** 12
DEFAULT_TIMEOUT = 120.0

_ALLOWED = {
    "contains": {"kind", "path", "text", "regex"},
    "absent": {"kind", "path", "text", "regex"},
    "increases": {"kind", "path", "pattern", "by"},
    "decreases": {"kind", "path", "pattern", "by"},
    "exists": {"kind", "path"},
    "missing": {"kind", "path"},
    "command": {"kind", "argv", "cmd", "pattern", "by", "timeout"},
}


class CheckError(ValueError):
    """An acceptance check that is malformed and cannot be used."""


@dataclass(frozen=True)
class Check:
    kind: str
    path: str = ""
    text: str = ""                         # for contains / absent: text or (with regex) a pattern
    regex: bool = False
    pattern: str = ""                      # for increases / decreases / command: one capture group holding an integer
    by: int = 1
    argv: Tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT

    def describe(self) -> str:
        """A plain sentence for the agent (it goes into the task prompt) and for reports."""
        if self.kind == "contains":
            return f"{self.path} contains {'text matching ' if self.regex else ''}{self.text[:60]!r}"
        if self.kind == "absent":
            return f"{self.path} does not contain {'text matching ' if self.regex else ''}{self.text[:60]!r}"
        if self.kind in ("increases", "decreases"):
            way = "higher" if self.kind == "increases" else "lower"
            return (f"the number matched by '{self.pattern}' in {self.path} is exactly {self.by} {way} than it is on main "
                    f"at the moment you land")
        if self.kind == "exists":
            return f"{self.path} exists"
        if self.kind == "missing":
            return f"{self.path} does not exist"
        return (f"the command {' '.join(self.argv)[:60]!r} succeeds on the result"
                + (f" and the number it prints (matched by '{self.pattern}') changes by {self.by}" if self.pattern else ""))


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _regex(pattern: object, what: str, groups: Optional[int] = None) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise CheckError(f"{what}: pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN:
        raise CheckError(f"{what}: pattern is longer than {MAX_PATTERN} characters")
    try:
        compiled = re.compile(pattern, re.M)
    except (re.error, RecursionError, OverflowError) as e:
        raise CheckError(f"{what}: bad regular expression: {e}") from None
    if groups is not None and compiled.groups != groups:
        raise CheckError(f"{what}: the pattern must have exactly {groups} capture group, it has {compiled.groups}")
    return pattern


def parse_check(d: object, n: int = 0) -> Check:
    """One check from its JSON form; raises ``CheckError`` naming the check and the problem."""
    tag = f"accept[{n}]"
    if not isinstance(d, Mapping):
        raise CheckError(f"{tag}: a check must be an object, got {type(d).__name__}")
    kind = d.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise CheckError(f"{tag}: unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    extra = sorted(set(d) - _ALLOWED[kind])
    if extra:
        raise CheckError(f"{tag} ({kind}): unknown key(s) {', '.join(map(repr, extra))}; allowed: {', '.join(sorted(_ALLOWED[kind]))}")
    path = ""
    if kind != "command":
        raw = d.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise CheckError(f"{tag} ({kind}): 'path' is required")
        try:
            path = normalize(raw)
        except InvalidPath as e:
            raise CheckError(f"{tag} ({kind}): {e}") from None
        if not path:
            raise CheckError(f"{tag} ({kind}): 'path' must name a file")
    if kind in ("contains", "absent"):
        text, rx = d.get("text"), d.get("regex", False)
        if not isinstance(rx, bool):
            raise CheckError(f"{tag} ({kind}): 'regex' must be true or false")
        if not isinstance(text, str) or not text:
            raise CheckError(f"{tag} ({kind}): 'text' is required and must not be empty")
        if len(text) > MAX_TEXT:
            raise CheckError(f"{tag} ({kind}): 'text' is longer than {MAX_TEXT} characters")
        if rx:
            _regex(text, f"{tag} ({kind})")
        return Check(kind, path, text, rx)
    if kind in ("increases", "decreases"):
        by = d.get("by", 1)
        if not _is_int(by) or not 1 <= by <= MAX_BY:
            raise CheckError(f"{tag} ({kind}): 'by' must be a whole number from 1 to {MAX_BY}")
        return Check(kind, path, pattern=_regex(d.get("pattern"), f"{tag} ({kind})", 1), by=by)
    if kind in ("exists", "missing"):
        return Check(kind, path)
    # command
    argv, cmd = d.get("argv"), d.get("cmd")
    if (argv is None) == (cmd is None):
        raise CheckError(f"{tag} (command): give exactly one of 'argv' (a list) or 'cmd' (a string)")
    if cmd is not None:
        if not isinstance(cmd, str) or not cmd.strip():
            raise CheckError(f"{tag} (command): 'cmd' must be a non-empty string")
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            raise CheckError(f"{tag} (command): cannot split 'cmd': {e}") from None
    if (not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and a for a in argv)
            or any("\0" in a for a in argv)):
        raise CheckError(f"{tag} (command): 'argv' must be a non-empty list of non-empty strings")
    timeout = d.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise CheckError(f"{tag} (command): 'timeout' must be a number of seconds from 0 to 3600")
    pattern, by = "", 1
    if d.get("pattern") is not None:
        pattern = _regex(d["pattern"], f"{tag} (command)", 1)
        by = d.get("by", 1)
        if not _is_int(by) or abs(by) > MAX_BY:
            raise CheckError(f"{tag} (command): 'by' must be a whole number")
    elif "by" in d:
        raise CheckError(f"{tag} (command): 'by' needs a 'pattern' to read the number from")
    return Check("command", argv=tuple(argv), pattern=pattern, by=by, timeout=float(timeout))


def parse_checks(items: object) -> Tuple[Check, ...]:
    """The checks of a task's ``"accept"`` list, validated (raises ``CheckError``)."""
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)):
        raise CheckError("'accept' must be a list of checks")
    if len(items) > MAX_CHECKS:
        raise CheckError(f"too many checks ({len(items)}; at most {MAX_CHECKS})")
    return tuple(parse_check(d, i) for i, d in enumerate(items))


def check_to_dict(c: Check) -> dict:
    if c.kind in ("contains", "absent"):
        out = {"kind": c.kind, "path": c.path, "text": c.text}
        if c.regex:
            out["regex"] = True
        return out
    if c.kind in ("increases", "decreases"):
        return {"kind": c.kind, "path": c.path, "pattern": c.pattern, "by": c.by}
    if c.kind in ("exists", "missing"):
        return {"kind": c.kind, "path": c.path}
    out = {"kind": "command", "argv": list(c.argv), "timeout": c.timeout}
    if c.pattern:
        out.update(pattern=c.pattern, by=c.by)
    return out


# ---- running ---------------------------------------------------------------------------------------------

def _text(files: Mapping[str, bytes], path: str) -> Optional[str]:
    """The file as text, '' marker avoided: None when absent, a ValueError when it is not text."""
    data = files.get(path)
    if data is None:
        return None
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path} is larger than {MAX_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not text") from None


def _number(pattern: str, text: str) -> Optional[int]:
    m = re.search(pattern, text, re.M)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        raise ValueError(f"the match {m.group(1)!r} is not a whole number") from None


def _materialize(root: Path, files: Mapping[str, bytes]) -> None:
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _run(c: Check, folder: Path, other: Path) -> Tuple[Optional[int], str]:
    env = dict(os.environ, AGENTTRUNK_OLD=str(other), AGENTTRUNK_NEW=str(folder))
    try:
        proc = subprocess.run(list(c.argv), cwd=folder, capture_output=True, text=True, timeout=c.timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {c.timeout:g}s"
    except OSError as e:
        return None, f"cannot run {c.argv[0]}: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()[-1500:]


def run_check(c: Check, old: Mapping[str, bytes], new: Mapping[str, bytes],
              _trees: Optional[Dict[str, Path]] = None) -> Optional[str]:
    """None when the check passes, else a sentence saying what is wrong."""
    try:
        if c.kind in ("contains", "absent"):
            text = _text(new, c.path)
            if c.kind == "contains":
                if text is None:
                    return f"{c.path} does not exist after this landing (expected it to contain {c.text[:60]!r})"
                hit = re.search(c.text, text, re.M) is not None if c.regex else c.text in text
                return None if hit else f"{c.path} does not contain {c.text[:60]!r} after this landing"
            if text is None:
                return None
            hit = re.search(c.text, text, re.M) is not None if c.regex else c.text in text
            return f"{c.path} still contains {c.text[:60]!r}" if hit else None
        if c.kind in ("increases", "decreases"):
            ot, nt = _text(old, c.path), _text(new, c.path)
            if ot is None or nt is None:
                return f"{c.path} is missing {'before' if ot is None else 'after'} this landing, so {c.kind} by {c.by} cannot be checked"
            before, after = _number(c.pattern, ot), _number(c.pattern, nt)
            if before is None:
                return f"cannot read the old value in {c.path} (nothing matches {c.pattern!r})"
            if after is None:
                return f"cannot read the new value in {c.path} (nothing matches {c.pattern!r})"
            want = before + c.by if c.kind == "increases" else before - c.by
            if after != want:
                return (f"{c.path}: the value must {c.kind[:-1]} by {c.by} (main has {before}, this landing gives {after}, "
                        f"expected {want}); another change landed first and was not taken into account")
            return None
        if c.kind == "exists":
            return None if c.path in new else f"{c.path} does not exist after this landing"
        if c.kind == "missing":
            return f"{c.path} still exists after this landing" if c.path in new else None
        # command
        with tempfile.TemporaryDirectory(prefix="agenttrunk-accept-") as tmp:
            root = Path(tmp)
            (root / "old").mkdir()
            (root / "new").mkdir()
            _materialize(root / "old", old)
            _materialize(root / "new", new)
            code, out = _run(c, root / "new", root / "old")
            if code != 0:
                return f"the command {' '.join(c.argv)[:80]!r} failed on the result" + (f": {out}" if out else "")
            if not c.pattern:
                return None
            ocode, oout = _run(c, root / "old", root / "new")
            if ocode != 0:
                return f"the command {' '.join(c.argv)[:80]!r} failed on the previous main, so its output cannot be compared"
            before, after = _number(c.pattern, oout), _number(c.pattern, out)
            if before is None or after is None:
                return f"cannot read a number with {c.pattern!r} from the command's output"
            if after != before + c.by:
                return f"the command's number must change by {c.by} (was {before}, now {after})"
            return None
    except ValueError as e:
        return f"{c.describe()}: {e}"
    except (re.error, RecursionError) as e:
        return f"{c.describe()}: regular expression failed: {e}"


def run_checks(checks: Sequence[Check], old: Mapping[str, bytes], new: Mapping[str, bytes]) -> List[str]:
    """Every failing check as a sentence (all are run, so the agent sees everything at once)."""
    out = []
    for c in checks:
        problem = run_check(c, old, new)
        if problem is not None:
            out.append(problem)
    return out


def unfalsifiable(checks: Iterable[Check], files: Mapping[str, bytes]) -> List[str]:
    """Checks that already pass on an unchanged tree: they cannot tell whether the task did anything."""
    out = []
    for c in checks:
        try:
            if run_check(c, files, files) is None:
                out.append(f"{c.describe()}: already true on the current main, so it cannot show that the task did its work")
        except Exception as e:                                            # noqa: BLE001 - linting never blocks
            out.append(f"{c.describe()}: could not be tried ({e})")
    return out
