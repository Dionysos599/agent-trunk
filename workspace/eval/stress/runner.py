"""One stress run: strategy, agents, optional faults, then the checks.

Two ways to run it:

* in one process on an in-memory store (``run_inprocess``): the 32 agents are threads of one orchestrator; used for the baselines
  and for the tests;
* against the real replicated cluster (``cluster.py``): the agents are threads inside several OS processes, so that a whole group
  of them can be killed with SIGKILL and restarted.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Dict, List, Optional, Sequence

from cmws_ws.agents import BatchQueue, FnValidator, MergeQueue, Orchestrator, Task, TaskGraph, Verdict, WorkerResult
from cmws_ws.agents.ladder import FnReapplier
from cmws_ws.checks import parse_checks
from cmws_ws.kv import MemoryKV
from cmws_ws.policy import rules_from_declaration
from cmws_ws.repo import Repository
from cmws_ws.serial import MemFS

from . import checks as C
from . import workload as W

RUN_ID = "stress"

# strategy -> how the queue is built (see eval/run_eval.py for the meaning of each level)
STRATEGIES = {
    "gitlike": dict(isolation="gitlike", survival=False, accept=False, policies=False, ladder=False, batch=False),
    "snapshot": dict(isolation="snapshot", survival=False, accept=False, policies=False, ladder=False, batch=False),
    "full-redo": dict(isolation="snapshot", survival=True, accept=True, policies=True, ladder=False, batch=False),
    "full": dict(isolation="snapshot", survival=True, accept=True, policies=True, ladder=True, batch=False),
    "full-batch": dict(isolation="snapshot", survival=True, accept=True, policies=True, ladder=True, batch=True),
}


class StressWorker:
    """A scripted agent: reads, takes ``latency`` seconds, then writes what it computed from what it read."""
    name = "stress"

    def __init__(self, tasks: Sequence[W.StressTask], latency=0.05, tokens=lambda t: 0, seed: int = 0,
                 counter: Optional[dict] = None) -> None:
        self.by_id = {t.id: t for t in tasks}
        self.latency, self.tokens, self.rng = latency, tokens, random.Random(seed)
        self.lock = threading.Lock()
        self.runs, self.spent = 0, 0

    def _pause(self, task: W.StressTask) -> float:
        with self.lock:
            return self.latency(task) if callable(self.latency) else self.latency * (0.5 + self.rng.random())

    def run(self, task, repo, agent_id: str) -> WorkerResult:
        tid = C.SUFFIX.sub("", agent_id).split("-", 1)[-1]
        st = self.by_id[tid]
        ws = repo.resume(agent_id)
        if ws.head != ws.fork_base:                      # a durable agent: an earlier attempt already committed this task's work
            return WorkerResult("done", f"resumed {tid}", ws.head, tuple(sorted(c.path for c in ws.changes_since_fork())), 0, 0, 0)

        class View:                                      # reads happen now, the writes after "thinking"
            def __init__(s):
                s.pending = []

            def read_text(s, p):
                return ws.read_text(p)

            def exists(s, p):
                return ws.exists(p)

            def write(s, p, d):
                s.pending.append((p, d))
        view = View()
        st.apply(view)
        time.sleep(self._pause(st))
        for p, d in view.pending:
            ws.write(p, d)
        tok = self.tokens(st)
        with self.lock:
            self.runs += 1
            self.spent += tok
        return WorkerResult("done", f"did {tid}", ws.commit(agent_id), tuple(sorted(ws.status())), 1, tok, 0)


def to_tasks(stress: Sequence[W.StressTask], policies: bool, accept: bool, honest: bool = False) -> List[Task]:
    """The tasks as a planner that missed the overlap would declare them: only a task's own file is named, so tasks that share a
    file run at the same time and collide (``honest`` declares the real paths, which makes the scheduler take turns)."""
    out = []
    for t in stress:
        paths = t.paths if (honest or t.kind == "own") else (f".marker/{t.id}",)
        merge = rules_from_declaration({}) if not policies else ()
        out.append(Task(t.id, f"stress task {t.id}", paths, "", (), merge, parse_checks(list(t.accept)) if accept else ()))
    return out


def build_queue(repo, strat: dict, validate_delay: float, window: float = 0.05, max_batch: int = 8, reapply=None, redo=None):
    def check(files, changed):
        if validate_delay:
            time.sleep(validate_delay)
        problem = W.invariant(files)
        return Verdict(not problem, problem)
    kw = dict(isolation=strat["isolation"], survival=strat["survival"], accept_checks=strat["accept"], policies=strat["policies"],
              max_redos=12, max_reapply=6, backoff=0.02, redo_worker=redo if strat["isolation"] == "snapshot" else None)
    if strat["ladder"]:
        kw["reapply"] = reapply
    if strat["batch"]:
        return BatchQueue(repo, FnValidator(check), window=window, max_batch=max_batch, **kw)
    return MergeQueue(repo, FnValidator(check), **kw)


def oracle_reapplier(stress: Sequence[W.StressTask], tokens=lambda: (0, 0)):
    """Layer 2 for scripted agents: apply the task's own edit to main's file (a perfect answer), at a recorded token cost."""
    by_id = {t.id: t for t in stress}
    from cmws_ws.agents.ladder import Reapplied

    class R:
        def reapply(self, task, path, base, mine, main):
            tid = C.SUFFIX.sub("", task.id).split("-", 1)[-1]
            fs = MemFS({path: main})

            class Only:
                def read_text(s, p):
                    return fs.read_text(p) if p == path else (_ for _ in ()).throw(FileNotFoundError(p))

                def exists(s, p):
                    return p == path

                def write(s, p, d):
                    if p == path:
                        fs.write(p, d)
            try:
                by_id[tid].apply(Only())
            except Exception:                            # noqa: BLE001 - a task touching other files cannot be answered for one
                return Reapplied(None)
            p_tok, c_tok = tokens()
            return Reapplied(fs.read_text(path), p_tok, c_tok)
    return R()


def initial_repo(kv, main: str = "main") -> Repository:
    return Repository.init(kv, {p: t.encode() for p, t in W.initial_files().items()}, main=main)


def run_inprocess(strategy: str, seed: int = 1, tasks: int = 160, agents: int = 32, latency=0.03, validate_delay: float = 0.0,
                  tokens=lambda t: 1000, kv=None, serial: bool = True, window: float = 0.05, max_batch: int = 8) -> dict:
    stress = W.generate(seed, tasks)
    kv = kv or MemoryKV()
    repo = initial_repo(kv)
    strat = STRATEGIES[strategy]
    reapply = oracle_reapplier(stress) if strat["ladder"] else None
    worker = StressWorker(stress, latency, tokens, seed)
    queue = build_queue(repo, strat, validate_delay, window, max_batch, reapply, redo=worker)
    orch = Orchestrator(repo, worker, queue=queue, owner="o1", parallel=agents, poll=0.005, patience=600, claim_ttl=600)
    graph = TaskGraph(to_tasks(stress, strat["policies"], strat["accept"]))
    t0 = time.time()
    summary = orch.run(RUN_ID, graph=graph)
    secs = time.time() - t0
    row = {"strategy": strategy, "seed": seed, "tasks": len(stress), "edits": W.edit_total(stress), "agents": agents,
           "seconds": round(secs, 3), "landed": len(summary.landed), "failed": len(summary.failed),
           "agent_runs": worker.runs, "tokens": worker.spent + sum(l.tokens for o in summary.report.outcomes.values()
                                                                   for l in [o.landed] if hasattr(l, "tokens")),
           "ladder": queue.ladder_report(), "batch": getattr(queue, "batch_stats", None)}
    row.update(C.verdict(repo, kv, RUN_ID, stress, W.initial_files(), serial, {t for t in summary.landed}))
    return row


def run_naive(seed: int = 1, tasks: int = 160, agents: int = 32, latency=0.03) -> dict:
    """The baseline without this project: every agent reads and writes one shared folder, the last writer of a file wins."""
    from concurrent.futures import ThreadPoolExecutor
    stress = W.generate(seed, tasks)
    files = dict(W.initial_files())
    lock = threading.Lock()
    rng = random.Random(seed)

    class FS:
        def __init__(self):
            self.pending = []

        def read_text(self, p):
            with lock:
                if p not in files:
                    raise FileNotFoundError(p)
                return files[p]

        def exists(self, p):
            with lock:
                return p in files

        def write(self, p, d):
            self.pending.append((p, d))

    def do(t):
        fs = FS()
        t.apply(fs)
        time.sleep(latency * (0.5 + rng.random()))
        with lock:
            for p, d in fs.pending:
                files[p] = d
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=agents) as pool:
        list(pool.map(do, stress))
    secs = time.time() - t0
    data = {p: t.encode() for p, t in files.items()}
    row = {"strategy": "shared-folder", "seed": seed, "tasks": len(stress), "edits": W.edit_total(stress), "agents": agents,
           "seconds": round(secs, 3), "landed": len(stress), "agent_runs": len(stress)}
    row.update(C.edits_lost(data, stress))
    problem = W.invariant(data)
    row.update(invariant_violations=[problem] if problem else [], state_invariant_ok=not problem, serial_ok=None, chain=None,
               claims_done=len(stress), claims_expected=len(stress))
    row["correct"] = row["edits_lost"] == 0 and row["edits_duplicated"] == 0 and not problem
    return row
