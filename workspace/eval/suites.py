"""E1: the task sets.

Each suite is a small project plus a list of tasks that are meant to be done at the same time. A suite
says three things that do not depend on who does the work:

* ``invariant``: Python code that must exit 0 on any state worth keeping (the project still imports and
  runs). It is what the merge queue validates and what "main is broken" means in the results.
* ``probe``: Python code that prints a JSON object {task id: bool}, whether each task's intended effect is
  present in a tree. It is run on the final state to find out what survived.
* ``edit`` of every task: the change a perfect agent would make, as a read-modify-write over a file system.
  Perfect agents ("oracle") isolate what the *strategy* does from what the *model* does: if even they lose
  updates, the loss is the strategy's.

Tasks deliberately collide. Their declared paths are narrower than what they really touch (as a planner
that did not see the overlap would declare them), so the scheduler runs them at the same time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple


@dataclass
class EvalTask:
    id: str
    description: str
    paths: Tuple[str, ...]
    edit: Callable[[object], None]
    honest: Tuple[str, ...] = ()     # what the task really touches; used by the "-honest" strategies
    accept: Tuple[dict, ...] = ()    # transition checks a landing of this task must pass (checks.py)
    merge: Tuple[Tuple[str, str], ...] = ()   # merge policies the "-append" strategy declares for this task


@dataclass
class Suite:
    name: str
    about: str
    files: Dict[str, str]
    tasks: List[EvalTask]
    invariant: str
    probe: str
    count_based: bool = False        # the probe reports how many tasks took effect, not which (see counter)


def _ids(n: int) -> List[str]:
    return [f"t{i}" for i in range(1, n + 1)]


# ---- disjoint: tasks that do not interact (the control) -----------------------------------------------

def disjoint(n: int = 4) -> Suite:
    shapes = ["x + {i}", "x * {i}", "x - {i}", "x ** {i}"]
    bodies = {i: shapes[(i - 1) % 4].format(i=i) for i in range(1, n + 1)}
    files = {f"m{i}.py": "def f(x):\n    raise NotImplementedError\n" for i in bodies}
    tasks = []
    for i, body in bodies.items():
        def edit(fs, i=i, body=body):
            fs.write(f"m{i}.py", f"def f(x):\n    return {body}\n")
        tasks.append(EvalTask(f"t{i}", f"In m{i}.py implement f(x) so that it returns {body}. Change only m{i}.py.",
                              (f"m{i}.py",), edit, (f"m{i}.py",),
                              accept=({"kind": "contains", "path": f"m{i}.py", "text": f"return {body}"},)))
    probe = ("import json\nr={}\n" + "".join(
        f"try:\n    import m{i}\n    r['t{i}']=m{i}.f(5)=={eval(b, {'x': 5})}\nexcept Exception:\n    r['t{i}']=False\n"
        for i, b in bodies.items()) + "print(json.dumps(r))")
    return Suite("disjoint" if n == 4 else f"disjoint{n}", f"{n} independent files; nothing can conflict", files, tasks,
                 "import " + ", ".join(f"m{i}" for i in bodies), probe)


# ---- counter: read-modify-write of one line (the lost update) ------------------------------------------

def counter(n: int = 5) -> Suite:
    files = {"stats.py": "COUNT = 0\n\n\ndef total():\n    return COUNT\n"}

    def edit(fs):
        text = fs.read_text("stats.py")
        value = int(re.search(r"COUNT = (\d+)", text).group(1))
        fs.write("stats.py", re.sub(r"COUNT = \d+", f"COUNT = {value + 1}", text, count=1))
    tasks = [EvalTask(i, "In stats.py the line `COUNT = <number>` holds a counter. Increase that number by exactly 1 "
                         "(read the current value first and write the value plus one). Change nothing else.",
                      (f"{i}.marker",), edit, ("stats.py",),
                      accept=({"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1},))
             for i in _ids(n)]
    probe = ("import json, stats\nprint(json.dumps({'count': stats.COUNT}))")
    return Suite("counter", f"{n} tasks each add 1 to the same line; the right answer is {n}", files, tasks,
                 "import stats; assert isinstance(stats.COUNT, int) and stats.total() == stats.COUNT", probe,
                 count_based=True)


# ---- shared: appending to the same file (adjacent edits) -----------------------------------------------

def shared(n: int = 5) -> Suite:
    files = {"handlers.py": "REGISTRY = {}\n"}
    tasks = []
    for i in range(1, n + 1):
        name = f"h{i}"

        def edit(fs, name=name, k=i + 1):
            text = fs.read_text("handlers.py")
            if not text.endswith("\n"):
                text += "\n"
            fs.write("handlers.py", text + f"\n\ndef handle_{name}(x):\n    return x * {k}\n\n\n"
                                            f"REGISTRY[\"{name}\"] = handle_{name}\n")
        tasks.append(EvalTask(f"t{i}", f"Append to the end of handlers.py a function handle_{name}(x) that returns "
                                       f"x * {i + 1}, then on its own line directly below it the line "
                                       f"REGISTRY[\"{name}\"] = handle_{name}. Keep all existing code.",
                              (f"{name}.marker",), edit, ("handlers.py",),
                              accept=({"kind": "contains", "path": "handlers.py", "text": f"def handle_{name}(x):"},
                                      {"kind": "contains", "path": "handlers.py",
                                       "text": f'REGISTRY["{name}"] = handle_{name}'}),
                              merge=(("handlers.py", "append"),)))
    probe = ("import json, handlers\nr={}\n" + "".join(
        f"r['t{i}']=handlers.REGISTRY.get('h{i}', lambda x: None)(3)=={3 * (i + 1)}\n" for i in range(1, n + 1))
        + "print(json.dumps(r))")
    return Suite("shared", f"{n} tasks append a function and a registry line to the same file", files, tasks,
                 "import handlers; [f(1) for f in handlers.REGISTRY.values()]", probe)


# ---- semantic: a clean text merge that breaks the program ----------------------------------------------

def semantic() -> Suite:
    files = {"geometry.py": "def area(w, h):\n    return w * h\n",
             "app.py": "from geometry import area\n\n\ndef main():\n    return area(2, 3)\n"}

    def rename(fs):
        fs.write("geometry.py", fs.read_text("geometry.py").replace("def area(", "def rect_area("))
        fs.write("app.py", fs.read_text("app.py").replace("area", "rect_area"))

    def report(fs):
        fs.write("report.py", "from geometry import area\n\n\ndef summary(w, h):\n"
                              "    return f\"area={area(w, h)}\"\n")
    tasks = [EvalTask("t1", "Rename the function area in geometry.py to rect_area, and update app.py so that it still "
                            "works.", ("geometry.py", "app.py"), rename, ("geometry.py", "app.py"),
                      accept=({"kind": "contains", "path": "geometry.py", "text": "def rect_area("},
                              {"kind": "absent", "path": "geometry.py", "text": "def area("})),
             EvalTask("t2", "Create report.py with a function summary(w, h) that returns the string "
                            "f\"area={...}\" where ... is the result of the function from geometry.py that "
                            "computes the area of a w by h rectangle.", ("report.py",), report, ("report.py", "geometry.py"),
                      accept=({"kind": "exists", "path": "report.py"},))]
    probe = ("import json\nr={}\ntry:\n    import geometry, app\n    r['t1']=hasattr(geometry,'rect_area') and app.main()==6\n"
             "except Exception:\n    r['t1']=False\ntry:\n    import report\n    r['t2']=report.summary(2,3)=='area=6'\n"
             "except Exception:\n    r['t2']=False\nprint(json.dumps(r))")
    invariant = ("import app, geometry\nassert app.main() == 6\nimport os\n"
                 "if os.path.exists('report.py'):\n    import report\n    assert report.summary(2, 3) == 'area=6'")
    return Suite("semantic", "a rename and a new caller of the old name: the text merges, the program does not",
                 files, tasks, invariant, probe)


SUITES: Dict[str, Callable[[], Suite]] = {"disjoint": disjoint, "disjoint8": lambda: disjoint(8), "counter": counter,
                                          "shared": shared, "semantic": semantic}
