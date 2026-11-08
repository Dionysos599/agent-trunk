"""Speed and token cost of 32 agents against one agent, with the cost of a model replayed from recorded real runs.

    python -m stress.efficiency --model eco --compress 0.15 --validate-ratio 0.05 --reps 3 --out FILE

* one agent (1 process, 1 thread, the plain queue) does the 160 tasks one after another;
* 32 agents (4 processes of 8 threads, the batched queue and the ladder) do the same tasks, against the real cluster;
* every task takes the recorded time and tokens of a real task of ``--model`` (the same sample for the same task in both runs, so
  the work is identical), and every collision costs what the recorded ladder runs cost (layer 2 tokens per answer, a whole task
  per redo); validation takes ``--validate-ratio`` of a task's time.

``--compress`` multiplies every replayed duration (real tasks take about 6 s; without it a single agent needs 16 minutes for
160 of them). Coordination is not compressed, so relative to the work it is more expensive here than in reality: **the speedups
are conservative**.

Speedup = seconds(one agent) / seconds(32 agents). Token increase = tokens(32 agents) / tokens(one agent) - 1, where tokens are
the replayed worker tokens (redone tasks included) plus layer 2's answers.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]

from stress import cluster, trace                                           # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="eco")
    ap.add_argument("--compress", type=float, default=0.15)
    ap.add_argument("--validate-ratio", type=float, default=0.05, help="validation time as a share of a task's time")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", type=int, default=160)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=1500.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    tfile = HERE.parent / "results" / "traces.json"
    data = json.loads(tfile.read_text())[a.model]
    mean_s = st.mean(t["seconds"] for t in data["task"])
    v = a.validate_ratio * mean_s * a.compress
    sink = a.out.open("a") if a.out else None
    gw = cluster.Gateway().start()
    rows = []
    try:
        def go(label, strategy, procs, threads, seed):
            r = cluster.run_cluster(gw, strategy, seed, a.tasks, procs, threads, False, 0.0, v, 8.0, str(tfile), a.model, a.compress,
                                    a.timeout, window=min(0.05, max(0.005, v)), max_batch=8)
            r.update(label=label, model=a.model, compress=a.compress, validate_ratio=a.validate_ratio, validate_seconds=round(v, 4))
            rows.append(r)
            print(json.dumps({k: r.get(k) for k in ("label", "model", "agents", "seconds", "landed", "correct", "tokens_total", "agent_runs",
                                                    "layer2", "redo_landings", "validations")}), flush=True)
            if sink:
                sink.write(json.dumps(r, default=str) + "\n")
                sink.flush()
        go("one-agent", "full", 1, 1, a.seed)
        for k in range(a.reps):
            go("32-agents", "full-batch", 4, 8, a.seed)
        one = [r for r in rows if r["label"] == "one-agent"][0]
        many = [r for r in rows if r["label"] == "32-agents"]
        sp = one["seconds"] / st.mean(r["seconds"] for r in many)
        inc = st.mean(r["tokens_total"] for r in many) / one["tokens_total"] - 1
        print(json.dumps({"summary": True, "model": a.model, "compress": a.compress, "validate_ratio": a.validate_ratio,
                          "speedup": round(sp, 1), "token_increase_pct": round(100 * inc, 1)}), flush=True)
    finally:
        gw.stop()
        if sink:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
