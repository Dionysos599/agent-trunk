"""One orchestrator process for the fault-injection runs (E4). Logs every task start so repeats can be counted.

    python3 e4_child.py URL MAIN SUITE RUN_ID LOGFILE CLAIM_TTL OWNER
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src")]

from cmws_ws.agents import CommandValidator, MergeQueue, Orchestrator, Task, TaskGraph
from cmws_ws.kv import HttpKV
from cmws_ws.repo import Repository

from suites import SUITES
from workers import OracleWorker, base_id


class Logged(OracleWorker):
    def __init__(self, edits, log, **kw):
        super().__init__(edits, **kw)
        self.log = log

    def run(self, task, repo, agent_id):
        with open(self.log, "a") as f:
            f.write(f"{agent_id}\n")
        return super().run(task, repo, agent_id)


def main(url, name, suite_name, run_id, log, ttl, owner):
    suite = SUITES[suite_name]()
    repo = Repository(HttpKV(url, timeout=30), main=name)
    worker = Logged({t.id: t.edit for t in suite.tasks}, log, latency=0.6, seed=1)
    queue = MergeQueue(repo, validator=CommandValidator([sys.executable, "-c", suite.invariant], timeout=60),
                       redo_worker=worker, max_redos=6)
    orch = Orchestrator(repo, worker, queue=queue, owner=owner, parallel=4, claim_ttl=float(ttl), poll=0.02, patience=600)
    out = orch.run(run_id, graph=TaskGraph([Task(t.id, t.description, t.paths) for t in suite.tasks]))
    print("ok" if out.ok else "not-ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:8]))
