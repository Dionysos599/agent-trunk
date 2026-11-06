"""Turn results.jsonl into markdown tables (E3).

    python3 workspace/eval/report.py results.jsonl [more.jsonl ...] > report.md
"""

import json
import statistics as st
import sys
from collections import defaultdict


def load(paths):
    rows = []
    for p in paths:
        rows += [json.loads(line) for line in open(p) if line.strip()]
    return rows


def fmt(x, nd=1):
    return f"{x:.{nd}f}"


def table(rows, group, cols):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(k, "") for k in group)].append(r)
    head = list(group) + ["runs"] + [c[0] for c in cols]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for key in sorted(groups):
        g = groups[key]
        cells = [str(k) for k in key] + [str(len(g))]
        for _, fn in cols:
            cells.append(fn(g))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def mean(field, nd=1):
    return lambda g: fmt(st.mean(r.get(field, 0) or 0 for r in g), nd)


COLS = [
    ("all correct", lambda g: f"{sum(r['all_ok'] for r in g)}/{len(g)}"),
    ("tasks satisfied", lambda g: f"{sum(r['satisfied'] for r in g)}/{sum(r['tasks'] for r in g)}"),
    ("lost updates", mean("lost_updates")),
    ("serializable", lambda g: (lambda ks: f"{sum(1 for r in ks if r['serializable'])}/{len(ks)}" if ks else "n/a")(
        [r for r in g if r.get("serializable") is not None])),
    ("main broken", lambda g: f"{sum(r['main_broken'] for r in g)}/{len(g)}"),
    ("conflicts", mean("conflicts")),
    ("human escalations", mean("escalated")),
    ("seconds", mean("seconds", 2)),
    ("tokens", mean("tokens", 0)),
]

if __name__ == "__main__":
    rows = load(sys.argv[1:])
    print(table(rows, ("backend", "tag", "suite", "strategy"), COLS))
