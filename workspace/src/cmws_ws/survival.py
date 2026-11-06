"""The survival check: a landing must not lose lines that either side added or removed on purpose.

Four trees are involved, all as ``{path: bytes}``:

* ``base``  the merge base: where the agent's branch and main last agreed;
* ``mine``  the agent's branch (what the agent wrote);
* ``main``  main immediately before this landing;
* ``result`` the candidate that would become main.

For every text file either side changed, with the changes seen line by line against ``base``:

* **a line inserted by pure insertion** (a hunk that adds lines and replaces none) must be present in ``result``;
* **a line removed by pure deletion** (a hunk that removes lines and adds none) must be gone: ``result`` must hold
  fewer copies of it than ``base`` did.

That catches what a clean merge cannot lose but a *conflict resolution* can: a merger agent, or a person, who keeps
one side and drops the other. It needs no authoring and costs no model tokens.

What it deliberately does not judge:

* **modified lines** (a hunk that replaces lines). A correct resolution of two conflicting modifications often matches
  neither side verbatim (two increments of a counter resolve to ``+2``, which is neither side's line), so a check on
  them would refuse good work. Task-supplied transition checks (``checks.py``) are the tool for those;
* lines that differ only in surrounding whitespace, and blank lines (compared after ``strip``);
* binary or non-UTF-8 files, and files over ``MAX_BYTES``.

A line that appears among the lines *added or written by a replacement* on either side is not held to the deletion
rule, since a copy may legitimately have come back; the cost is that such a case cannot be detected.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Mapping, Optional, Set, Tuple

from .textmerge import hunks, split_lines

MAX_BYTES = 2_000_000
MAX_REPORTED = 8


def _text(data: Optional[bytes]) -> Optional[str]:
    if data is None or len(data) > MAX_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _norm(lines) -> List[str]:
    return [ln.strip() for ln in lines]


def _deltas(base_lines: List[str], side_text: Optional[str]) -> Tuple[Set[str], Set[str], Set[str]]:
    """(pure insertions, pure deletions, new lines of replace hunks) of a side against the base, normalised."""
    if side_text is None:
        return set(), set(), set()
    side = split_lines(side_text)
    ins: Set[str] = set()
    dele: Set[str] = set()
    touched: Set[str] = set()
    for h in hunks(base_lines, side):
        if h.start == h.end:
            ins.update(_norm(h.lines))
        elif not h.lines:
            dele.update(_norm(base_lines[h.start:h.end]))
        else:
            touched.update(_norm(h.lines))
    return ins, dele, touched


def survival_problems(base: Mapping[str, bytes], mine: Mapping[str, bytes], main: Mapping[str, bytes],
                      result: Mapping[str, bytes], mine_name: str = "the branch",
                      main_name: str = "main") -> List[str]:
    """Problems found, each a sentence an agent or a person can act on; empty when everything survived."""
    problems: List[str] = []
    everywhere: Optional[Set[str]] = None                    # every line of every text file in ``result``, built on demand

    def all_lines() -> Set[str]:
        nonlocal everywhere
        if everywhere is None:
            everywhere = set()
            for data in result.values():
                t = _text(data)
                if t is not None:
                    everywhere.update(_norm(split_lines(t)))
        return everywhere

    changed = sorted(p for p in set(mine) | set(main) | set(base)
                     if mine.get(p) != base.get(p) or main.get(p) != base.get(p))
    cut = False
    for path in changed:
        if len(problems) >= MAX_REPORTED:
            cut = True                                       # more files to look at, but eight problems are enough to act on
            break
        if mine.get(path) is None and main.get(path) is None:
            continue
        bt, at, mt = _text(base.get(path)), _text(mine.get(path)), _text(main.get(path))
        xt = _text(result.get(path))
        if any(d is not None and t is None for d, t in ((base.get(path), bt), (mine.get(path), at),
                                                          (main.get(path), mt), (result.get(path), xt))):
            continue                                          # binary or too large: not judged
        base_lines = split_lines(bt or "")
        sides = ((mine_name, _deltas(base_lines, at)), (main_name, _deltas(base_lines, mt)))
        replaced_any: Set[str] = set()
        inserted_any: Set[str] = set()
        for _, (ins, _dele, touched) in sides:
            replaced_any |= touched
            inserted_any |= ins
        x_lines = _norm(split_lines(xt)) if xt is not None else None
        x_set = set(x_lines) if x_lines is not None else None
        for who, (ins, dele, _touched) in sides:
            for line in sorted(ins):
                if not line:
                    continue
                have = x_set if x_set is not None else all_lines()
                if line not in have:
                    problems.append(f"{path}: a line added by {who} is missing from the result: {line[:80]!r}")
            if dele:
                base_count = Counter(_norm(base_lines))
                x_count = Counter(x_lines or [])
                for line in sorted(dele):
                    if not line or line in replaced_any or line in inserted_any:
                        continue
                    if x_count[line] >= base_count[line]:
                        problems.append(f"{path}: a line removed by {who} is back in the result: {line[:80]!r}")
    if cut or len(problems) > MAX_REPORTED:
        problems = problems[:MAX_REPORTED] + ["... and more"]
    return problems
