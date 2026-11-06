"""Merge rules and acceptance checks through the orchestrator: they are stored in the plan, survive a resume, and
reach the worker and the queue under the run's namespaced task names."""

import json
import re
import threading
import time
import unittest

from cmws_ws.agents import MergeQueue, Orchestrator, StaticPlanner, Task, TaskGraph, WorkerResult, run_status
from cmws_ws.agents.orchestrator import plan_key, task_from_dict
from cmws_ws.checks import parse_checks
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository

INC = [{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}]


class Worker:
    name = "w"

    def __init__(self, fn):
        self.fn, self.seen, self.lock = fn, {}, threading.Lock()

    def run(self, task, repo, agent_id):
        with self.lock:
            self.seen[agent_id] = task
        time.sleep(0.05)
        ws = repo.resume(agent_id)
        self.fn(task, ws)
        return WorkerResult("done", f"did {task.id}", ws.commit(agent_id), tuple(sorted(ws.status())))


def graph_of(dicts):
    return TaskGraph([task_from_dict(d) for d in dicts])


class OrchestratedPolicyTests(unittest.TestCase):
    def test_appending_tasks_land_without_collisions_when_declared(self):
        repo = Repository.init(MemoryKV(), {"reg.txt": b"head\n"})
        tasks = [{"id": f"t{i}", "description": "add a line", "paths": [f"{i}.m"], "merge": {"reg.txt": "append"}} for i in range(6)]

        def add(task, ws):
            ws.write("reg.txt", ws.read_text("reg.txt") + task.id.split("-", 1)[1] + "\n")
        worker = Worker(add)
        orch = Orchestrator(repo, worker, owner="o", parallel=6, poll=0.01, patience=5)
        summary = orch.run("r1", graph=graph_of(tasks))
        self.assertTrue(summary.ok, summary.report)
        lines = repo.main_files()["reg.txt"].decode().splitlines()
        self.assertEqual(sorted(lines[1:]), [f"t{i}" for i in range(6)])
        self.assertEqual(len(lines), 7)
        self.assertEqual(len(MergeQueue(repo).escalations.list()), 0)
        for t in worker.seen.values():
            self.assertEqual(t.merge, (("reg.txt", "append"),))               # the worker gets the namespaced task with its rules

    def test_the_same_tasks_without_the_declaration_collide_and_need_help(self):
        repo = Repository.init(MemoryKV(), {"reg.txt": b"head\n"})
        tasks = [{"id": f"t{i}", "description": "add a line", "paths": [f"{i}.m"]} for i in range(4)]

        def add(task, ws):
            ws.write("reg.txt", ws.read_text("reg.txt") + task.id.split("-", 1)[1] + "\n")
        orch = Orchestrator(repo, Worker(add), owner="o", parallel=4, poll=0.01, patience=5)
        summary = orch.run("r1", graph=graph_of(tasks))
        self.assertFalse(summary.ok)                                          # no merger and no redo worker: escalations

    def test_the_stored_plan_carries_rules_and_checks(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        d = {"id": "t1", "description": "bump", "paths": ["stats.py"], "accept": INC, "merge": {"x": "converge"}}

        def bump(task, ws):
            n = int(re.search(r"\d+", ws.read_text("stats.py")).group(0)) + 1
            ws.write("stats.py", f"COUNT = {n}\n")
        w = Worker(bump)
        orch = Orchestrator(repo, w, owner="o", poll=0.01, patience=5)
        self.assertTrue(orch.run("r1", graph=graph_of([d])).ok)
        plan = json.loads(repo.kv.get(plan_key("r1")))
        self.assertEqual(plan["tasks"][0]["accept"], [{"kind": "increases", "path": "stats.py",
                                                        "pattern": r"COUNT = (\d+)", "by": 1}])
        self.assertEqual(plan["tasks"][0]["merge"], {"x": "converge"})
        st = run_status(repo.kv, "r1")
        self.assertEqual(st["plan"]["tasks"][0]["accept"][0]["kind"], "increases")
        again = Orchestrator(repo, Worker(bump), owner="o2", poll=0.01, patience=5).run("r1", graph=graph_of([d]))
        self.assertTrue(again.resumed)                                        # resumed from the stored plan: no new work

    def test_a_worker_that_does_the_arithmetic_wrong_is_refused_and_then_redone(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        d = {"id": "t1", "description": "bump", "paths": ["stats.py"], "accept": INC}
        calls = []

        def bump(task, ws):
            calls.append(task.id)
            n = int(re.search(r"\d+", ws.read_text("stats.py")).group(0))
            ws.write("stats.py", f"COUNT = {n + (5 if len(calls) == 1 else 1)}\n")      # first attempt adds 5
        w = Worker(bump)
        queue = MergeQueue(repo, redo_worker=w, max_redos=1)
        orch = Orchestrator(repo, w, queue=queue, owner="o", poll=0.01, patience=5)
        summary = orch.run("r1", graph=graph_of([d]))
        self.assertTrue(summary.ok, summary.report)
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 1\n")
        self.assertEqual(len(calls), 2)                                       # the redo ran once, after the check refused


if __name__ == "__main__":
    unittest.main()
