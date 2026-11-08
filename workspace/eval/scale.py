"""Speed against the number of agents, with the validation floor visible, and what backups for stragglers cost (simulation).

    PYTHONPATH=workspace/src python3 workspace/eval/scale.py [--tasks 48] [--work 0.1] [--validate 0.03] [--out FILE]

T independent tasks (data parallel: each writes its own file), N agents, a validator that takes ``--validate`` seconds, a
task that takes ``--work`` seconds. Perfect agents with simulated latency: nothing here says anything about a model, only
about what the coordination does to time. Rows are JSON lines; with ``--table`` the markdown is printed.

* ``plain``    the 1.0 queue: one validation per landing, one after another, so T x validate is a floor under the run;
* ``batched``  ``BatchQueue``: branches that finish together share one validation;
* ``hedge``    a few tasks run far too long on their first agent; with ``--hedge`` a backup agent is started once a task has
               run k times the median of the others, and the first to land wins. Tokens are counted per agent run.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src")]

from cmws_ws.agents import BatchQueue, FnValidator, MergeQueue, Orchestrator, Task, TaskGraph, Verdict, WorkerResult   # noqa: E402
from cmws_ws.kv import MemoryKV                                                                                          # noqa: E402
from cmws_ws.repo import Repository                                                                                      # noqa: E402

TOKENS_PER_RUN = 1000


class FanoutWorker:
    name = "fanout"

    def __init__(self, work: float, stragglers=None, backup_work=None, delay_for=None) -> None:
        self.work, self.stragglers = work, dict(stragglers or {})
        self.backup_work = work if backup_work is None else backup_work
        self.runs, self.lock = 0, threading.Lock()

    def run(self, task, repo, agent_id):
        with self.lock:
            self.runs += 1
        tid = task.id.split("-", 1)[-1]
        backup = agent_id.endswith("-hedge")
        time.sleep(self.backup_work if backup else self.work * self.stragglers.get(tid, 1.0))
        ws = repo.resume(agent_id)
        ws.write(f"{tid}.txt", f"done {tid}\n")
        return WorkerResult("done", f"did {tid}", ws.commit(agent_id), (f"{tid}.txt",), 1, TOKENS_PER_RUN, 0)


def run_once(tasks: int, agents: int, work: float, validate: float, mode: str, stragglers=None, hedge=None) -> dict:
    repo = Repository.init(MemoryKV(), {"README": b"x\n"})
    validator = FnValidator(lambda files, changed: (time.sleep(validate), Verdict(True))[1])
    queue = (BatchQueue(repo, validator, window=min(0.05, validate * 2 + 0.01), max_batch=16) if mode == "batched"
             else MergeQueue(repo, validator))
    worker = FanoutWorker(work, stragglers)
    orch = Orchestrator(repo, worker, queue=queue, parallel=agents, poll=0.005, patience=600, claim_ttl=600,
                        hedge_after=hedge)
    graph = TaskGraph([Task(f"t{i}", f"task {i}", (f"t{i}.txt",)) for i in range(tasks)])
    t0 = time.time()
    summary = orch.run("scale", graph=graph)
    secs = time.time() - t0
    return {"mode": mode, "tasks": tasks, "agents": agents, "work": work, "validate": validate, "seconds": round(secs, 3),
            "landed": len(summary.landed), "ok": summary.ok, "validations": len(queue.approved) if hasattr(queue, "approved") else 0,
            "agent_runs": worker.runs, "tokens": worker.runs * TOKENS_PER_RUN, "hedges": summary.report.hedges,
            "hedge_wins": summary.report.hedge_wins, "width": summary.report.width}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", type=int, default=48)
    ap.add_argument("--work", type=float, default=0.1)
    ap.add_argument("--validate", type=float, default=0.03)
    ap.add_argument("--agents", default="1,2,4,8,16,32")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--stragglers", type=int, default=0, help="how many tasks run --slow times too long on their first agent (hedging rows)")
    ap.add_argument("--slow", type=float, default=30.0)
    ap.add_argument("--hedge", type=float, default=3.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    rows = []
    sink = a.out.open("a") if a.out else None
    for n in (int(x) for x in a.agents.split(",")):
        for mode in ("plain", "batched"):
            for _ in range(a.reps):
                r = run_once(a.tasks, n, a.work, a.validate, mode)
                rows.append(r)
                print(json.dumps(r), flush=True)
                if sink:
                    sink.write(json.dumps(r) + "\n")
    if a.stragglers:
        slow = {f"t{i * 7 % a.tasks}": a.slow for i in range(a.stragglers)}
        for hedge in (None, a.hedge):
            r = run_once(a.tasks, 8, a.work, a.validate, "batched", slow, hedge)
            r["mode"] = "hedge" if hedge else "no-hedge"
            r["stragglers"] = a.stragglers
            rows.append(r)
            print(json.dumps(r), flush=True)
            if sink:
                sink.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
