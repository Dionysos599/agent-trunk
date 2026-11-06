"""Serializability of a finished run: is the final ``main`` what running the landed tasks one by one would give?

The property (doc 12, section 2.1): *the final tree equals the result of executing the landed tasks one after
another in landing order, each starting from the result of the one before.* It says the run is equivalent to
**some** serial run; it does not say the order is the order a person wanted.

This applies to agents that are deterministic functions of what they read (the oracle agents of the evaluation,
or any test double), because a model's edit cannot be replayed. For real agents the same property is approximated
by the acceptance checks of ``checks.py``.

* ``landing_order`` reads the order from history: a task's *tip* is the last commit its branch made (every commit
  of a branch carries the agent's name as its author, merger and sync commits included); the tips of landed tasks are
  totally ordered by ancestry because each new ``main`` contains the previous one.
* ``replay`` runs the edits serially over an in-memory file system.
* ``check_serializable`` compares, and when the landing order does not reproduce the final tree it can also say
  whether *some other* order does (``witness``), which separates "not serializable at all" from "serializable
  but in a different order than it landed".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .history import ancestors

MAX_SEARCH = 8            # permutations are tried up to 8! = 40320 replays
Edit = Callable[[object], None]


class MemFS:
    """A tiny file system with the interface the evaluation's edits use (read_text, exists, write, ...)."""

    def __init__(self, files: Optional[Mapping[str, object]] = None) -> None:
        self._f: Dict[str, str] = {p: (d.decode("utf-8", "surrogateescape") if isinstance(d, bytes) else d)
                                   for p, d in (files or {}).items()}

    def read_text(self, path: str) -> str:
        try:
            return self._f[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def exists(self, path: str) -> bool:
        return path in self._f

    def write(self, path: str, data) -> None:
        self._f[path] = data.decode("utf-8", "surrogateescape") if isinstance(data, bytes) else data

    def delete(self, path: str) -> None:
        if path not in self._f:
            raise FileNotFoundError(path)
        del self._f[path]

    def move(self, src: str, dst: str) -> None:
        self._f[dst] = self.read_text(src)
        del self._f[src]

    def list(self, directory: str = "") -> List[Tuple[str, str]]:
        prefix = directory.strip("/") + "/" if directory.strip("/") else ""
        found: Dict[str, str] = {}
        for p in self._f:
            if p.startswith(prefix):
                first, _, rest = p[len(prefix):].partition("/")
                found[first] = "dir" if rest else "file"
        return sorted(found.items())

    def files(self) -> Dict[str, str]:
        return dict(self._f)


@dataclass
class SerialVerdict:
    ok: bool                                   # the landing order reproduces the final tree
    order: List[str]                           # landed tasks, in landing order
    expected: Dict[str, str] = field(default_factory=dict)
    actual: Dict[str, str] = field(default_factory=dict)
    differing: List[str] = field(default_factory=list)      # paths whose content differs (or exist on one side only)
    linear: bool = True                        # the landed tasks' tips form a chain (history is one line)
    witness: Optional[List[str]] = None        # another order that does reproduce the tree (searched when ok is False)
    searched: bool = False                     # whether other orders were tried
    error: str = ""                            # an edit raised while replaying

    @property
    def serializable(self) -> bool:
        """Equivalent to some serial order (the landing order or a witness)."""
        return self.ok or self.witness is not None


def _text(files: Mapping[str, object]) -> Dict[str, str]:
    return {p: (d.decode("utf-8", "surrogateescape") if isinstance(d, bytes) else d) for p, d in files.items()}


def replay(initial: Mapping[str, object], order: Sequence[str], edits: Mapping[str, Edit]) -> Dict[str, str]:
    """Run ``edits[t]`` for t in ``order`` over a copy of ``initial``; raises KeyError for an unknown task and
    lets an edit's own exception through."""
    fs = MemFS(initial)
    for t in order:
        edits[t](fs)
    return fs.files()


def diff_paths(a: Mapping[str, str], b: Mapping[str, str]) -> List[str]:
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def landing_order(repo, task_of: Callable[[str], Optional[str]], head: Optional[str] = None) -> Tuple[List[str], bool]:
    """(tasks in the order they landed on ``head``, whether their tips form a single chain).

    ``task_of`` maps a commit's author to a task id, or None for authors that are not tasks (the initial commit).
    """
    store = repo.store
    head = head or repo.head()
    reach = ancestors(store, head)
    rank: Dict[str, int] = {}
    tips: Dict[str, str] = {}
    sets: Dict[str, frozenset] = {}
    for cid in reach:
        t = task_of(store.get_commit(cid).author)
        if t is None:
            continue
        anc = sets[cid] = frozenset(ancestors(store, cid))
        rank[cid] = len(anc)
        best = tips.get(t)
        if best is None or (rank[cid], cid) > (rank[best], best):
            tips[t] = cid
    order = sorted(tips, key=lambda t: (rank[tips[t]], tips[t]))
    linear = all(sets[tips[a]] <= sets[tips[b]] for a, b in zip(order, order[1:]))
    return order, linear


def check_final(initial: Mapping[str, object], final: Mapping[str, object], edits: Mapping[str, Edit],
                order: Sequence[str], linear: bool = True, search: bool = True, max_search: int = MAX_SEARCH) -> SerialVerdict:
    """Compare ``final`` with the serial replay of ``order``; if they differ, look for another order that matches."""
    actual = _text(final)
    v = SerialVerdict(False, list(order), actual=actual, linear=linear)
    try:
        v.expected = replay(initial, order, edits)
    except Exception as e:                                          # noqa: BLE001 - the edit's own failure is the verdict
        v.error = f"replaying {', '.join(order)}: {type(e).__name__}: {e}"
        v.differing = sorted(actual)
        return v
    v.differing = diff_paths(v.expected, actual)
    v.ok = not v.differing
    if v.ok or not search:
        return v
    if len(order) > max_search:
        return v
    v.searched = True
    for perm in itertools.permutations(order):
        if list(perm) == list(order):
            continue
        try:
            if not diff_paths(replay(initial, perm, edits), actual):
                v.witness = list(perm)
                break
        except Exception:                                           # noqa: BLE001 - an order that cannot run is no witness
            continue
    return v


def check_serializable(repo, initial: Mapping[str, object], edits: Mapping[str, Edit],
                       task_of: Optional[Callable[[str], Optional[str]]] = None, head: Optional[str] = None,
                       search: bool = True) -> SerialVerdict:
    """The serializability verdict of the run recorded in ``repo`` (see the module docstring)."""
    task_of = task_of or (lambda author: author if author in edits else None)
    order, linear = landing_order(repo, task_of, head)
    unknown = [t for t in order if t not in edits]
    if unknown:
        v = SerialVerdict(False, order, linear=linear, error="no edit known for " + ", ".join(unknown))
        return v
    final = repo.files_at(head or repo.head())
    return check_final(initial, final, edits, order, linear, search)


def check_unordered(initial: Mapping[str, object], final: Mapping[str, object], edits: Mapping[str, Edit],
                    done: Iterable[str], max_search: int = MAX_SEARCH) -> SerialVerdict:
    """For a run with no history (a shared folder): is the final tree what *some* serial order of the tasks
    that reported done would give?"""
    done = sorted(done)
    return check_final(initial, final, edits, done, linear=False, search=True, max_search=max_search)
