"""E2/E3: run suites under three strategies with different kinds of agents; write one JSON line per run.

    PYTHONPATH=workspace/src python3 workspace/eval/run_eval.py --suites counter,shared --strategies naive,serial,forkmerge \
        --backends oracle --reps 5 --out results.jsonl

Strategies
  serial     one agent at a time, each starting from what the previous one landed (the safe baseline)
  naive      all agents at once in one shared folder, last writer wins (what happens without this project)
  forkmerge  all agents at once, each on its own fork; landed through the validating merge queue

Backends
  oracle     perfect agents with a delay (isolates the strategy)
  ollama     the tool loop over an Ollama model
  claude     the `claude` command line in a folder or checkout (run it from your own terminal: it refuses to
             start inside another Claude Code session)
  mixed      tasks alternate between ollama and claude, so that different kinds of agents collide
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src")]

from cmws_ws.agents import (CliWorker, CommandValidator, LoopWorker, MergeQueue, OpenAICompatModel, Orchestrator, Task,
                            TaskGraph, merger_worker)
from cmws_ws.kv import MemoryKV
from cmws_ws.leases import LeaseTable
from cmws_ws.repo import Repository

from suites import SUITES, Suite
from workers import DirFS, OracleWorker, SloppyMerger, base_id, cli_in_dir, loop_on

CLAUDE = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]


def _probe(code: str, files: Dict[str, str]) -> Optional[dict]:
    with tempfile.TemporaryDirectory(prefix="cmws-probe-") as tmp:
        for p, text in files.items():
            t = Path(tmp) / p
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(text)
        try:
            proc = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, timeout=60)
            return json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else None
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return None


def _invariant_ok(suite: Suite, files: Dict[str, str]) -> bool:
    with tempfile.TemporaryDirectory(prefix="cmws-inv-") as tmp:
        for p, text in files.items():
            t = Path(tmp) / p
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(text)
        try:
            return subprocess.run([sys.executable, "-c", suite.invariant], cwd=tmp, capture_output=True,
                                  timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            return False


def _satisfied(suite: Suite, files: Dict[str, str]) -> Dict[str, bool]:
    got = _probe(suite.probe, files) or {}
    if suite.count_based:                      # the probe says how many tasks took effect, not which
        n = int(got.get("count", 0))
        return {t.id: i < n for i, t in enumerate(suite.tasks)}
    return {t.id: bool(got.get(t.id)) for t in suite.tasks}


class Backend:
    """Chooses, per task, what does the work; knows how to run it on a fork or on a shared folder."""

    def __init__(self, kind: str, suite: Suite, args) -> None:
        self.kind, self.suite, self.args = kind, suite, args
        self.edits = {t.id: t.edit for t in suite.tasks}
        self.oracle = OracleWorker(self.edits, latency=args.latency, seed=args.seed, untracked=args.untracked_reads, assume_reads=args.assume_reads)
        self.model = (OpenAICompatModel(args.model, args.llm_url, timeout=args.timeout,
                                        headers={"Host": args.llm_host} if args.llm_host else None)
                      if kind in ("ollama", "mixed") else None)
        self.order = {t.id: i for i, t in enumerate(suite.tasks)}

    def uses_claude(self, tid: str) -> bool:
        return self.kind == "claude" or (self.kind == "mixed" and self.order[tid] % 2 == 1)

    # a worker for forks (used by serial and forkmerge, including for redo branches)
    def fork_worker(self):
        me = self

        class ByTask:
            name = me.kind

            def run(self, task, repo, agent_id):
                tid = base_id(agent_id)
                if me.kind == "oracle":
                    return me.oracle.run(task, repo, agent_id)
                if me.uses_claude(tid):
                    return CliWorker(CLAUDE, timeout=me.args.timeout).run(task, repo, agent_id)
                return LoopWorker(me.model, max_steps=me.args.max_steps, checkpoint_every=3).run(task, repo, agent_id)
        return ByTask()

    def merger(self):
        if self.args.sloppy_merger:
            return SloppyMerger()
        return merger_worker(self.model) if self.model is not None else None

    # a worker for the shared folder (naive)
    def dir_run(self, task: Task, root: Path):
        tid = task.id
        if self.kind == "oracle":
            return self.oracle.run_on(task, DirFS(root))
        if self.uses_claude(tid):
            return cli_in_dir(CLAUDE, task, root, self.args.timeout)
        return loop_on(self.model, DirFS(root), task, self.args.max_steps)


def run_one(suite: Suite, strategy: str, kind: str, args, rep: int) -> dict:
    be = Backend(kind, suite, args)
    honest = strategy.endswith("-honest")
    strategy = strategy.removesuffix("-honest") if not honest else strategy
    tasks = [Task(t.id, t.description, t.honest if honest else t.paths) for t in suite.tasks]
    base = strategy.removesuffix("-honest")
    row = {"tag": args.tag, "suite": suite.name, "strategy": strategy, "backend": kind, "rep": rep, "tasks": len(tasks),
           "parallel": 1 if base == "serial" else args.parallel}
    t0 = time.time()
    reported: Dict[str, str] = {}
    if base == "naive":
        with tempfile.TemporaryDirectory(prefix="cmws-naive-") as tmp:
            root = Path(tmp)
            for p, text in suite.files.items():
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text(text)
            with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futs = {t.id: pool.submit(be.dir_run, t, root) for t in tasks}
                results = {tid: f.result() for tid, f in futs.items()}
            final = DirFS(root).files()
        reported = {tid: ("done" if r.ok else "failed") for tid, r in results.items()}
        row.update(tokens=sum(r.prompt_tokens + r.completion_tokens for r in results.values()),
                   conflicts=0, escalated=0, via={}, rejected=0, violations=0)
    else:
        repo = Repository.init(MemoryKV(), {p: t.encode() for p, t in suite.files.items()})
        validator = CommandValidator([sys.executable, "-c", suite.invariant], timeout=60)
        worker = be.fork_worker()
        queue = MergeQueue(repo, validator=validator, merger=be.merger(), redo_worker=worker if kind != "oracle" or args.oracle_redo else None,
                           max_redos=args.max_redos, keep_both_sides=not args.no_guard)
        orch = Orchestrator(repo, worker, queue=queue, owner="eval", parallel=1 if base == "serial" else args.parallel,
                            patience=3600, poll=0.01, claim_ttl=3600)
        summary = orch.run(f"r{rep}", graph=TaskGraph(tasks))
        final = {p: d.decode() for p, d in repo.main_files().items()}
        via, conflicts, rejected, detail = {}, 0, 0, []
        for tid, o in summary.report.outcomes.items():
            land0 = o.landed
            detail.append({"task": tid, "status": o.status, "reason": o.reason,
                           "touched": list(o.result.touched) if o.result else [],
                           "worker_summary": (o.result.summary if o.result else "")[:200],
                           "landing": getattr(land0, "status", None), "via": getattr(land0, "via", None),
                           "steps": [[x.stage, x.status, x.detail[:160]] for x in getattr(land0, "steps", [])]})
            reported[tid] = {"done": "done"}.get(o.status, "failed")
            land = o.landed
            if land is not None and hasattr(land, "steps") and land.steps:
                if land.steps[0].status in ("conflict", "stale", "rejected"):
                    conflicts += 1
                rejected += sum(1 for s in land.steps if s.status == "rejected")
                via[land.via if land.landed or land.status == "noop" else land.status] = via.get(
                    land.via if land.landed or land.status == "noop" else land.status, 0) + 1
        row.update(tokens=sum((o.result.prompt_tokens + o.result.completion_tokens) for o in summary.report.outcomes.values() if o.result),
                   conflicts=conflicts, rejected=rejected, via=via, detail=detail,
                   empties=sum(1 for d in detail for st in d["steps"] if st[1] == "empty"),
                   escalated=len(queue.escalations.list()),
                   violations=len(LeaseTable(repo.kv).violations()))
    row["seconds"] = round(time.time() - t0, 2)
    sat = _satisfied(suite, final)
    done_ids = {t for t, s in reported.items() if s == "done"}
    row.update(
        reported_done=len(done_ids),
        satisfied=sum(sat.values()),
        lost_updates=sum(1 for t in done_ids if not sat.get(t, False)) if not suite.count_based
        else max(0, len(done_ids) - sum(sat.values())),
        main_broken=not _invariant_ok(suite, final),
        all_ok=all(sat.values()) and _invariant_ok(suite, final),
    )
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suites", default="disjoint,counter,shared,semantic")
    ap.add_argument("--strategies", default="serial,naive,forkmerge")
    ap.add_argument("--backends", default="oracle")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--latency", type=float, default=0.3, help="oracle: seconds between reading and writing")
    ap.add_argument("--oracle-redo", action="store_true", help="oracle: let the queue redo a task (same edit)")
    ap.add_argument("--max-redos", type=int, default=1)
    ap.add_argument("--untracked-reads", action="store_true",
                    help="oracle: reads are not recorded (like an agent in a checkout), so stale reads are not detected")
    ap.add_argument("--assume-reads", action="store_true",
                    help="with --untracked-reads: record every changed file as a read, as CliWorker does")
    ap.add_argument("--sloppy-merger", action="store_true",
                    help="merger that keeps one side of every conflict (simulates a weak model's mistake)")
    ap.add_argument("--no-guard", action="store_true", help="turn off the check that a resolution keeps both sides")
    ap.add_argument("--tag", default="", help="label stored with every row (for ablations)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default="eco:latest")
    ap.add_argument("--llm-url", default=os.environ.get("CMWS_LLM_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--llm-host", default=os.environ.get("CMWS_LLM_HOST"))
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", type=Path, default=Path("results.jsonl"))
    a = ap.parse_args(argv)
    with a.out.open("a") as out:
        for name in a.suites.split(","):
            for strategy in a.strategies.split(","):
                for kind in a.backends.split(","):
                    for rep in range(1, a.reps + 1):
                        a.seed = rep
                        row = run_one(SUITES[name](), strategy, kind, a, rep)
                        out.write(json.dumps(row, sort_keys=True) + "\n")
                        out.flush()
                        print(f"{name:9} {strategy:9} {kind:7} #{rep}: ok={row['all_ok']} satisfied={row['satisfied']}/{row['tasks']} "
                              f"lost={row['lost_updates']} broken={row['main_broken']} conflicts={row['conflicts']} "
                              f"{row['seconds']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
