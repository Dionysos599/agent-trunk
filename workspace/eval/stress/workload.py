"""The stress workload: a generated project and a task list, seeded so a run can be repeated, with an exact answer.

About 1,600 line edits in 160 tasks of about 10 edits each (defaults). Every edit is a deterministic function of the file it
edits, so the right answer is known by construction and by serial replay.

==========  =====  ========================================  ===========================================================
kind        share  what a task does (10 edits)               what it collides on
==========  =====  ========================================  ===========================================================
own         55%    writes 10 unique lines into its own file  nothing: the cost of the machinery when nothing is shared
append      25%    appends 10 unique lines to 1 of 4 shared  adjacent inserts at the end of the file (policy `append`)
                   files
counter     12%    adds 1 to 10 of 6 hot counters and 10 to   the same lines changed by many agents: lost updates
                   a TOTAL line
version      8%    adds 1 to VERSION and writes 9 lines of    one hot line (a version number)
                   its own
==========  =====  ========================================  ===========================================================

Every written line carries a unique token (``tok <task> <n>``), so "this edit is present exactly once" is checkable without a
model. The counters file has an invariant (TOTAL equals the sum of the counters) that the validator checks on every candidate.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

SHARED_FILES = 4
COUNTERS = 6
EDITS_PER_TASK = 10
MIX = (("own", 0.55), ("append", 0.25), ("counter", 0.12), ("version", 0.08))

COUNTER_FILE = "hot/counters.txt"
VERSION_FILE = "hot/version.txt"
MERGE_RULES = "".join(f"shared/reg_{k}.txt append\n" for k in range(SHARED_FILES))


def initial_files() -> Dict[str, str]:
    files = {f"shared/reg_{k}.txt": f"# registry {k}\n" for k in range(SHARED_FILES)}
    files[COUNTER_FILE] = "".join(f"C{k} = 0\n" for k in range(COUNTERS)) + "TOTAL = 0\n"
    files[VERSION_FILE] = "VERSION = 0\n"
    files[".agenttrunk-merge"] = MERGE_RULES
    files["README"] = "stress project\n"
    return files


@dataclass
class StressTask:
    id: str
    kind: str
    edits: int
    apply: Callable[[object], None]          # fs -> None; fs has read_text / write / exists (Workspace or MemFS)
    tokens: List[str]                        # the unique lines this task writes
    increments: Dict[int, int] = field(default_factory=dict)      # counter index -> times incremented
    paths: Tuple[str, ...] = ()
    accept: Tuple[dict, ...] = ()


def _bump(text: str, pattern: str, by: int) -> str:
    return re.sub(pattern, lambda m: f"{m.group(1)}{int(m.group(2)) + by}", text, count=1, flags=re.M)


def _own(tid: str, n: int) -> StressTask:
    lines = [f"tok {tid} {j}" for j in range(n)]
    path = f"own/{tid}.txt"

    def apply(fs):
        fs.write(path, "".join(f"{ln}\n" for ln in lines))
    return StressTask(tid, "own", n, apply, lines, paths=(path,))


def _append(tid: str, n: int, shared: int) -> StressTask:
    lines = [f"tok {tid} {j}" for j in range(n)]
    path = f"shared/reg_{shared}.txt"

    def apply(fs):
        text = fs.read_text(path)
        fs.write(path, text + "".join(f"{ln}\n" for ln in lines))
    return StressTask(tid, "append", n, apply, lines, paths=(path,))


def _counter(tid: str, n: int, rng: random.Random) -> StressTask:
    picks = [rng.randrange(COUNTERS) for _ in range(n)]
    inc = {k: picks.count(k) for k in set(picks)}

    def apply(fs):
        text = fs.read_text(COUNTER_FILE)
        for k, times in inc.items():
            text = _bump(text, rf"^(C{k} = )(\d+)$", times)
        text = _bump(text, r"^(TOTAL = )(\d+)$", n)
        fs.write(COUNTER_FILE, text)
    accept = ({"kind": "increases", "path": COUNTER_FILE, "pattern": r"TOTAL = (\d+)", "by": n},)
    return StressTask(tid, "counter", n, apply, [], inc, (COUNTER_FILE,), accept)


def _version(tid: str, n: int) -> StressTask:
    lines = [f"tok {tid} {j}" for j in range(n - 1)]
    path = f"own/{tid}.txt"

    def apply(fs):
        fs.write(VERSION_FILE, _bump(fs.read_text(VERSION_FILE), r"^(VERSION = )(\d+)$", 1))
        fs.write(path, "".join(f"{ln}\n" for ln in lines))
    accept = ({"kind": "increases", "path": VERSION_FILE, "pattern": r"VERSION = (\d+)", "by": 1},)
    return StressTask(tid, "version", n, apply, lines, paths=(VERSION_FILE, path), accept=accept)


def generate(seed: int = 1, tasks: int = 160, edits: int = EDITS_PER_TASK) -> List[StressTask]:
    """The tasks of a run, in a random but seeded order."""
    rng = random.Random(seed)
    counts = {k: int(round(tasks * share)) for k, share in MIX}
    counts["own"] += tasks - sum(counts.values())                       # rounding goes to the independent files
    kinds = [k for k, n in counts.items() for _ in range(n)]
    rng.shuffle(kinds)
    out: List[StressTask] = []
    for i, kind in enumerate(kinds):
        tid = f"t{i:03d}"
        if kind == "own":
            out.append(_own(tid, edits))
        elif kind == "append":
            out.append(_append(tid, edits, rng.randrange(SHARED_FILES)))
        elif kind == "counter":
            out.append(_counter(tid, edits, rng))
        else:
            out.append(_version(tid, edits))
    return out


def edit_total(tasks: Sequence[StressTask]) -> int:
    return sum(t.edits for t in tasks)


def expected(tasks: Sequence[StressTask]) -> dict:
    """What any correct final tree must satisfy, independent of the order the tasks landed in."""
    counters = {k: 0 for k in range(COUNTERS)}
    for t in tasks:
        for k, n in t.increments.items():
            counters[k] += n
    return {"counters": counters, "total": sum(counters.values()), "version": sum(1 for t in tasks if t.kind == "version"),
            "tokens": [tok for t in tasks for tok in t.tokens]}


def invariant(files: Dict[str, bytes]) -> str:
    """The validator's rule for any state of ``main``: '' when it holds, else what is wrong."""
    text = lambda p: files[p].decode("utf-8", "replace") if p in files else None            # noqa: E731
    c = text(COUNTER_FILE)
    if c is None:
        return f"{COUNTER_FILE} is missing"
    vals = dict(re.findall(r"^(C\d+|TOTAL) = (\d+)$", c, re.M))
    if len(vals) != COUNTERS + 1:
        return "the counters file is malformed"
    if sum(int(v) for k, v in vals.items() if k != "TOTAL") != int(vals["TOTAL"]):
        return "TOTAL is not the sum of the counters"
    v = text(VERSION_FILE)
    if v is None or not re.fullmatch(r"VERSION = \d+\n", v):
        return "the version file is malformed"
    for path, data in files.items():
        t = data.decode("utf-8", "replace")
        if "<<<<<<<" in t or ">>>>>>>" in t:
            return f"conflict markers in {path}"
        if path.startswith(("own/", "shared/")):
            lines = [ln for ln in t.splitlines() if ln.startswith("tok ")]
            if len(lines) != len(set(lines)):
                return f"a line appears twice in {path}"
    return ""
