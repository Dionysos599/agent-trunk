"""One OS process of scripted agents: ``--threads`` agents (an orchestrator with that many workers) on the real cluster.

    python -m stress.agent_proc --url URL --main NAME --run stress --owner p0 --threads 8 --seed 1 --tasks 160 --strategy full-batch

Several of these run side by side on the same run id; the claims in the replicated store keep them off each other's tasks, and a
task whose owner was killed is taken over once its claim expires.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]

from cmws_ws.agents import Orchestrator, TaskGraph                       # noqa: E402
from cmws_ws.kv import HttpKV                                            # noqa: E402
from cmws_ws.repo import Repository                                      # noqa: E402

from stress import runner as R                                           # noqa: E402
from stress import workload as W                                         # noqa: E402
from stress.trace import Trace                                           # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--main", required=True)
    ap.add_argument("--run", default=R.RUN_ID)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tasks", type=int, default=160)
    ap.add_argument("--strategy", default="full-batch")
    ap.add_argument("--latency", type=float, default=0.05)
    ap.add_argument("--validate-delay", type=float, default=0.0)
    ap.add_argument("--claim-ttl", type=float, default=4.0)
    ap.add_argument("--trace", help="JSON trace file: task time and tokens are replayed from it")
    ap.add_argument("--model", default="eco")
    ap.add_argument("--compress", type=float, default=1.0, help="multiply every replayed duration by this")
    ap.add_argument("--window", type=float, default=0.05)
    ap.add_argument("--max-batch", type=int, default=8)
    a = ap.parse_args(argv)
    kv = HttpKV(a.url, timeout=30)
    repo = Repository(kv, main=a.main)
    stress = W.generate(a.seed, a.tasks)
    strat = R.STRATEGIES[a.strategy]
    trace = Trace.load(a.trace, a.model, seed=a.seed, compress=a.compress) if a.trace else None
    worker = R.StressWorker(stress, trace.task_latency if trace else a.latency, trace.task_tokens if trace else (lambda t: 1000), a.seed)
    reapply = R.oracle_reapplier(stress, trace.layer2_tokens if trace else (lambda: (0, 0))) if strat["ladder"] else None
    queue = R.build_queue(repo, strat, a.validate_delay, a.window, a.max_batch, reapply, redo=worker)
    orch = Orchestrator(repo, worker, queue=queue, owner=a.owner, parallel=a.threads, poll=0.01, patience=120,
                        claim_ttl=a.claim_ttl, lease_ttl=a.claim_ttl)
    graph = TaskGraph(R.to_tasks(stress, strat["policies"], strat["accept"]))
    t0 = time.time()
    summary = orch.run(a.run, graph=graph)
    print(f"{a.owner} done in {time.time() - t0:.1f}s: landed {len(summary.landed)}, failed {len(summary.failed)}, "
          f"agent runs {worker.runs}, tokens {worker.spent}", flush=True)
    import json
    print("STATS " + json.dumps({"owner": a.owner, "agent_runs": worker.runs, "worker_tokens": worker.spent,
                                 "ladder": queue.ladder_report(), "batch": getattr(queue, "batch_stats", None)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
