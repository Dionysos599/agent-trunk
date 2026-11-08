"""Does the replay predict what real agents do? A small run (12 tasks, 1 and 4 real agents) against its own replay.

    python -m stress.real_agents --model eco [--agents 4] [--out FILE]

Real agents (the tool loop over an Ollama model) do a small version of the stress workload described in plain words: write a few
lines into their own file, append lines to a shared file, add 1 to a counter and to TOTAL, add 1 to VERSION. The same checks as in
the big runs decide whether the result is correct. Then:

1. one real agent does the 12 tasks; each task's seconds and tokens are recorded;
2. ``--agents`` real agents do them at once (with the full system: policies, checks, layer 2 asked of the same model);
3. the scripted agents replay step 1's recorded per-task seconds and tokens at the same number of agents, in the same queue;

and the report compares the speedup and the tokens of step 2 with those of step 3. If they are not close, the 32-wide replay
numbers are not to be trusted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]

from cmws_ws.agents import LoopWorker, Orchestrator, OpenAICompatModel, Task, TaskGraph       # noqa: E402
from cmws_ws.agents.ladder import ModelReapplier                                              # noqa: E402
from cmws_ws.checks import parse_checks                                                       # noqa: E402
from cmws_ws.kv import MemoryKV                                                               # noqa: E402

from stress import checks as C                                                                # noqa: E402
from stress import runner as R                                                                # noqa: E402
from stress import workload as W                                                              # noqa: E402


def mini_tasks() -> List[W.StressTask]:
    """12 small tasks: 5 own, 3 append, 2 counter, 2 version. The apply functions are the truth the checks hold results to."""
    import random
    rng = random.Random(7)
    out: List[W.StressTask] = []
    for i in range(5):
        out.append(W._own(f"t{len(out):03d}", 3))
    for i in range(3):
        out.append(W._append(f"t{len(out):03d}", 3, i % 2))
    for i in range(2):
        t = W._counter(f"t{len(out):03d}", 1, rng)
        out.append(t)
    for i in range(2):
        out.append(W._version(f"t{len(out):03d}", 1))
    rng.shuffle(out)
    return out


def describe(t: W.StressTask) -> str:
    if t.kind == "own":
        return (f"Create the file own/{t.id}.txt containing exactly these {len(t.tokens)} lines, in this order, one per line:\n"
                + "\n".join(t.tokens))
    if t.kind == "append":
        return (f"Append these {len(t.tokens)} lines to the end of {t.paths[0]}, in this order, keeping everything already in the file:\n"
                + "\n".join(t.tokens))
    if t.kind == "counter":
        k = next(iter(t.increments))
        return (f"In hot/counters.txt, increase the number in the line `C{k} = <n>` by 1, and increase the number in the line "
                f"`TOTAL = <n>` by 1. Read the current numbers first. Change nothing else.")
    return "In hot/version.txt, increase the number in the line `VERSION = <n>` by 1. Read the current number first."


def real_tasks(mini: List[W.StressTask]) -> List[Task]:
    return [Task(t.id, describe(t), t.paths if t.kind == "own" else (f".marker/{t.id}",), "", (), (),
                 parse_checks(list(t.accept))) for t in mini]


class Recording:
    """Wraps a worker and records what each task cost."""

    def __init__(self, inner):
        self.inner, self.name, self.rows = inner, getattr(inner, "name", "w"), {}

    def run(self, task, repo, agent_id):
        t0 = time.time()
        r = self.inner.run(task, repo, agent_id)
        tid = C.SUFFIX.sub("", agent_id).split("-", 1)[-1]
        if agent_id == f"{RUN}-{tid}":                                   # the first attempt only
            self.rows[tid] = {"seconds": time.time() - t0, "tokens": r.prompt_tokens + r.completion_tokens}
        return r


RUN = "mini"


def run_real(mini, agents: int, model_name: str, url: str, host: str, with_layer2: bool = True) -> dict:
    kv = MemoryKV()
    repo = R.initial_repo(kv)
    model = OpenAICompatModel(model_name, url, timeout=600, headers={"Host": host} if host else None)
    quiet = OpenAICompatModel(model_name, url, timeout=600, headers={"Host": host} if host else None, extra={"reasoning_effort": "none"})
    worker = Recording(LoopWorker(model, max_steps=20))
    strat = R.STRATEGIES["full"]
    queue = R.build_queue(repo, strat, 0.0, reapply=ModelReapplier(quiet) if with_layer2 else None, redo=worker)
    orch = Orchestrator(repo, worker, queue=queue, owner="o", parallel=agents, poll=0.02, patience=900, claim_ttl=900)
    graph = TaskGraph(real_tasks(mini))
    t0 = time.time()
    summary = orch.run(RUN, graph=graph)
    secs = time.time() - t0
    tokens = sum(o.result.prompt_tokens + o.result.completion_tokens for o in summary.report.outcomes.values() if o.result)
    tokens += sum(getattr(o.landed, "tokens", 0) for o in summary.report.outcomes.values())
    row = {"kind": "real", "agents": agents, "seconds": round(secs, 2), "landed": len(summary.landed), "tasks": len(mini), "tokens": tokens,
           "per_task": worker.rows, "layer2": queue.ladder_report()[2]}
    row.update(C.verdict(repo, kv, RUN, mini, W.initial_files(), True, set(summary.landed)))
    return row


def run_replay(mini, agents: int, per_task: dict) -> dict:
    from stress.trace import Trace
    data = {"model": "recorded", "task": list(per_task.values()), "layer2_tokens": [182.0]}
    trace = Trace(data, seed=1)
    by_id = {t: v for t, v in per_task.items()}
    kv = MemoryKV()
    repo = R.initial_repo(kv)
    worker = R.StressWorker(mini, lambda st_: by_id[st_.id]["seconds"], lambda st_: int(by_id[st_.id]["tokens"]), 1)
    queue = R.build_queue(repo, R.STRATEGIES["full"], 0.0, reapply=R.oracle_reapplier(mini, trace.layer2_tokens), redo=worker)
    orch = Orchestrator(repo, worker, queue=queue, owner="o", parallel=agents, poll=0.005, patience=900, claim_ttl=900)
    graph = TaskGraph(R.to_tasks(mini, True, True))
    t0 = time.time()
    summary = orch.run(RUN, graph=graph)
    secs = time.time() - t0
    tokens = worker.spent + sum(getattr(o.landed, "tokens", 0) for o in summary.report.outcomes.values())
    row = {"kind": "replay", "agents": agents, "seconds": round(secs, 2), "landed": len(summary.landed), "tasks": len(mini), "tokens": tokens}
    row.update(C.verdict(repo, kv, RUN, mini, W.initial_files(), True, set(summary.landed)))
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="eco")
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--url", default=os.environ.get("AGENTTRUNK_LLM_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--host", default=os.environ.get("AGENTTRUNK_LLM_HOST"))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    mini = mini_tasks()
    sink = a.out.open("a") if a.out else None
    rows = []
    try:
        one = run_real(mini, 1, f"{a.model}:latest", a.url, a.host)
        rows.append(one)
        for _ in range(a.reps):
            rows.append(run_real(mini, a.agents, f"{a.model}:latest", a.url, a.host))
        recorded = one["per_task"]
        if len(recorded) == len(mini):
            for _ in range(a.reps):
                rows.append(run_replay(mini, 1, recorded))
                rows.append(run_replay(mini, a.agents, recorded))
        for r in rows:
            r["model"] = a.model
            print(json.dumps({k: r.get(k) for k in ("kind", "agents", "seconds", "landed", "tasks", "tokens", "correct", "edits_lost_silent",
                                                    "edits_pending", "layer2")}), flush=True)
            if sink:
                sink.write(json.dumps(r, default=str) + "\n")
    finally:
        if sink:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
