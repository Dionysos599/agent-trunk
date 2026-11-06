"""Draw the evaluation figures as standalone SVG files (standard library only).

    python3 workspace/eval/charts.py [OUT_DIR]          # default: docs/eval

Reads the recorded runs in workspace/eval/results/*.jsonl and writes one SVG per figure:

  lost-updates.svg    updates lost per run, by suite and coordination strategy (perfect agents)
  scaling.svg         speedup against the number of agents running at once
  redo-budget.svg     share of runs in which every task landed, by redo budget
  mixed-agents.svg    updates lost per run with a local model plus the claude command line, before and after
                      each of the three fixes
  levels.svg          share of runs that are equivalent to a serial execution, by isolation level (perfect agents)
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# one hue per series, chosen to stay apart for the common kinds of colour blindness
PALETTE = ["#4c78a8", "#e45756", "#54a24b", "#b279a2", "#f58518", "#72b7b2"]
INK, MUTED, GRID = "#1f2933", "#52606d", "#d9dee3"
FONT = "font-family=\"-apple-system, Segoe UI, Helvetica, Arial, sans-serif\""


def load(*names: str) -> List[dict]:
    rows: List[dict] = []
    for n in names:
        p = RESULTS / n
        if p.exists():
            rows += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return rows


def _svg(width: int, height: int, title: str, body: List[str], subtitle: str = "") -> str:
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{escape(title)}">\n<rect width="{width}" height="{height}" fill="#ffffff" rx="8"/>\n'
            f'<text x="24" y="32" {FONT} font-size="17" font-weight="600" fill="{INK}">{escape(title)}</text>\n')
    if subtitle:
        head += f'<text x="24" y="52" {FONT} font-size="12" fill="{MUTED}">{escape(subtitle)}</text>\n'
    return head + "\n".join(body) + "\n</svg>\n"


def _legend(names: Sequence[str], x: int, y: int) -> List[str]:
    out, cx = [], x
    for i, n in enumerate(names):
        out.append(f'<rect x="{cx}" y="{y - 10}" width="12" height="12" rx="2" fill="{PALETTE[i % len(PALETTE)]}"/>')
        out.append(f'<text x="{cx + 18}" y="{y}" {FONT} font-size="12" fill="{INK}">{escape(n)}</text>')
        cx += 30 + 7 * len(n)
    return out


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    for step in (1, 2, 2.5, 5, 10):
        for scale in (0.1, 1, 10, 100):
            if v <= step * scale:
                return step * scale
    return v


def bar_chart(title: str, subtitle: str, groups: Sequence[str], series: Dict[str, Sequence[float]],
              ylabel: str, fmt: str = "{:.1f}", ymax: float = 0) -> str:
    """Grouped bars: one cluster per group, one bar per series."""
    W, H, left, right, top, bottom = 760, 380, 64, 24, 92, 56
    names = list(series)
    top_val = ymax or _nice_max(max((max(v) for v in series.values() if len(v)), default=1))
    pw, ph = W - left - right, H - top - bottom
    body = _legend(names, left, 74)
    for i in range(5):
        y = top + ph - ph * i / 4
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" stroke="{GRID}"/>')
        body.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" {FONT} font-size="11" fill="{MUTED}">'
                    f'{top_val * i / 4:g}</text>')
    body.append(f'<text x="14" y="{top + ph / 2}" transform="rotate(-90 14 {top + ph / 2})" text-anchor="middle" '
                f'{FONT} font-size="12" fill="{MUTED}">{escape(ylabel)}</text>')
    gw = pw / max(1, len(groups))
    bw = min(34, gw * 0.8 / max(1, len(names)))
    for gi, g in enumerate(groups):
        gx = left + gi * gw + gw / 2
        body.append(f'<text x="{gx:.1f}" y="{H - bottom + 20}" text-anchor="middle" {FONT} font-size="12" fill="{INK}">{escape(g)}</text>')
        for si, n in enumerate(names):
            v = series[n][gi]
            h = ph * (v / top_val) if top_val else 0
            x = gx - bw * len(names) / 2 + si * bw
            body.append(f'<rect x="{x + 1:.1f}" y="{top + ph - h:.1f}" width="{bw - 2:.1f}" height="{max(h, 0):.1f}" rx="2" '
                        f'fill="{PALETTE[si % len(PALETTE)]}"><title>{escape(n)} / {escape(g)}: {fmt.format(v)}</title></rect>')
            body.append(f'<text x="{x + bw / 2:.1f}" y="{top + ph - h - 5:.1f}" text-anchor="middle" {FONT} font-size="10" '
                        f'fill="{INK}">{fmt.format(v)}</text>')
    return _svg(W, H, title, body, subtitle)


def line_chart(title: str, subtitle: str, xs: Sequence[float], series: Dict[str, Sequence[float]], xlabel: str, ylabel: str,
               ymax: float = 0, dashed: Sequence[str] = (), fmt: str = "{:.1f}") -> str:
    W, H, left, right, top, bottom = 760, 380, 64, 40, 92, 56
    names = list(series)
    top_val = ymax or _nice_max(max(max(v) for v in series.values()))
    pw, ph = W - left - right, H - top - bottom
    lo, hi = min(xs), max(xs)
    px = lambda x: left + (pw * (x - lo) / (hi - lo) if hi != lo else pw / 2)     # noqa: E731
    py = lambda y: top + ph - ph * y / top_val                                      # noqa: E731
    body = _legend(names, left, 74)
    for i in range(5):
        y = top + ph - ph * i / 4
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" stroke="{GRID}"/>')
        body.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" {FONT} font-size="11" fill="{MUTED}">{top_val * i / 4:g}</text>')
    for x in xs:
        body.append(f'<text x="{px(x):.1f}" y="{H - bottom + 20}" text-anchor="middle" {FONT} font-size="11" fill="{MUTED}">{x:g}</text>')
    body.append(f'<text x="{left + pw / 2}" y="{H - 14}" text-anchor="middle" {FONT} font-size="12" fill="{MUTED}">{escape(xlabel)}</text>')
    body.append(f'<text x="14" y="{top + ph / 2}" transform="rotate(-90 14 {top + ph / 2})" text-anchor="middle" '
                f'{FONT} font-size="12" fill="{MUTED}">{escape(ylabel)}</text>')
    for si, n in enumerate(names):
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, series[n]))
        dash = ' stroke-dasharray="6 4"' if n in dashed else ""
        body.append(f'<polyline points="{pts}" fill="none" stroke="{PALETTE[si % len(PALETTE)]}" stroke-width="2.5"{dash}/>')
        if n not in dashed:
            for x, y in zip(xs, series[n]):
                body.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="4" fill="{PALETTE[si % len(PALETTE)]}">'
                            f'<title>{escape(n)} at {x:g}: {fmt.format(y)}</title></circle>')
    return _svg(W, H, title, body, subtitle)


def mean(rows: List[dict], key: str) -> float:
    return st.mean(r.get(key, 0) or 0 for r in rows) if rows else 0.0


def fig_lost_updates(rows: List[dict]) -> str:
    suites = [s for s in ("counter", "shared", "semantic", "disjoint") if any(r["suite"] == s for r in rows)]
    labels = {"naive": "shared folder", "serial": "one at a time", "forkmerge": "fork + merge queue",
              "forkmerge-honest": "fork + merge queue, honest paths"}
    series = {}
    for strat, label in labels.items():
        series[label] = [mean([r for r in rows if r["suite"] == s and r["strategy"] == strat], "lost_updates") for s in suites]
    runs = min((len([r for r in rows if r["suite"] == suites[0] and r["strategy"] == "naive"]) or 0), 99)
    return bar_chart("Updates lost per run, by coordination strategy",
                     f"Perfect agents, {runs} runs per bar; counter and shared have 5 tasks, semantic 2 (lower is better)",
                     suites, series, "updates lost per run")


def fig_scaling(rows: List[dict]) -> str:
    ps = sorted({r["parallel"] for r in rows})
    by = defaultdict(list)
    for r in rows:
        by[(r["strategy"], r["parallel"])].append(r["seconds"])
    base = st.mean(by[("forkmerge", ps[0])])
    series = {
        "ideal": [p / ps[0] for p in ps],
        "shared folder (no merging)": [st.mean(by[("naive", ps[0])]) / st.mean(by[("naive", p)]) for p in ps],
        "fork + merge queue": [base / st.mean(by[("forkmerge", p)]) for p in ps],
    }
    return line_chart("Speedup with more agents at once",
                      "8 independent tasks of 0.5 s each, perfect agents; speedup over a single agent (higher is better)",
                      ps, series, "agents running at once", "speedup", ymax=_nice_max(max(ps)), dashed=("ideal",), fmt="{:.2f}x")


def fig_redo(rows: List[dict]) -> str:
    budgets = sorted({int(r["tag"].split("=")[1]) for r in rows})
    series = {}
    for suite in ("counter", "shared"):
        series[f"{suite} (5 racing tasks)"] = [
            100 * st.mean(1.0 if r["all_ok"] else 0.0 for r in rows if r["suite"] == suite and r["tag"] == f"redos={b}")
            for b in budgets]
    return line_chart("How many redos does the merge queue need?",
                      "Runs in which all 5 racing tasks landed; a task that runs out of redos goes to a person", budgets, series, "redos allowed per task", "runs in which every task landed (%)",
                      ymax=100, fmt="{:.0f}%")


def fig_mixed() -> str:
    stages = [("first run", "mixed_eco_claude.jsonl"), ("merger guard +\nassumed reads", "mixed_eco_claude_after_fixes.jsonl"),
              ("+ no empty 'done'", "mixed_eco_claude_after_fixes2.jsonl")]
    suites = ("counter", "shared")
    series = {}
    for suite in suites:
        vals = []
        for _, f in stages:
            rows = [r for r in load(f) if r["suite"] == suite and r["strategy"] == "forkmerge"]
            vals.append(mean(rows, "lost_updates") if rows else 0.0)
        series[suite] = vals
    groups = [label.replace("\n", " ") for label, _ in stages]
    return bar_chart("A local model plus the claude command line: updates lost per run",
                     "Fork + merge queue, 3 runs per bar, 5 tasks; each step fixes how work was lost in the run before", groups, series, "updates lost per run")


LEVELS = [("naive", "shared folder"), ("gitlike", "git-like"), ("forkmerge", "+ read set (1.0)"),
          ("gitlike-checks", "git-like + checks"), ("serializable", "+ read set + checks")]


def fig_levels(rows: List[dict]) -> str:
    """Per isolation level and suite: the share of runs whose final tree some serial execution explains."""
    suites = [s for s in ("counter", "shared", "disjoint") if any(r["suite"] == s for r in rows)]
    series = {}
    for strat, label in LEVELS:
        vals = []
        for s in suites:
            sel = [r for r in rows if r["suite"] == s and r["strategy"] == strat and r.get("serializable") is not None]
            vals.append(100.0 * sum(1 for r in sel if r["serializable"]) / len(sel) if sel else 0.0)
        series[label] = vals
    runs = max((len([r for r in rows if r["suite"] == suites[0] and r["strategy"] == "naive"]) if suites else 0), 0)
    return bar_chart("Runs equivalent to a serial execution, by isolation level",
                     f"Perfect agents, {runs} runs per bar; counter has 5 tasks that each add 1 to one line (higher is better)",
                     suites, series, "runs that are serializable (%)", fmt="{:.0f}%", ymax=100)


def main(out: Path) -> List[Path]:
    out.mkdir(parents=True, exist_ok=True)
    figs = {
        "lost-updates.svg": fig_lost_updates(load("oracle_main.jsonl")),
        "scaling.svg": fig_scaling(load("oracle_scale.jsonl")),
        "redo-budget.svg": fig_redo(load("oracle_redos.jsonl")),
        "mixed-agents.svg": fig_mixed(),
    }
    levels = load("oracle_levels.jsonl")
    if levels:
        figs["levels.svg"] = fig_levels(levels)
    paths = []
    for name, svg in figs.items():
        p = out / name
        p.write_text(svg)
        paths.append(p)
    return paths


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "docs" / "eval"
    for p in main(target):
        print("wrote", p)
