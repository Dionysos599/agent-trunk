"""Stress runs against the real replicated store: several OS processes of agents, faults injected, the checks of ``checks.py``.

* the store is the Java cluster behind the HTTP gateway (2 shard groups of 3 replicas), started here unless ``--url`` is given;
* the agents are ``agent_proc`` processes of ``--threads`` threads each (4 x 8 = 32 by default), all on one run id;
* faults (``--faults``): at random moments one replica of each shard group is killed and later revived (at most one per group at a
  time, so a majority always remains), and one whole agent process is killed with SIGKILL mid-task and restarted under a new
  owner name; its tasks are taken over when their claims expire.
"""

from __future__ import annotations

import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
sys.path[:0] = [str(HERE.parent), str(ROOT / "workspace" / "src")]

from cmws_ws.kv import HttpKV                                          # noqa: E402
from cmws_ws.repo import Repository                                    # noqa: E402

from stress import checks as C                                         # noqa: E402
from stress import runner as R                                         # noqa: E402
from stress import workload as W                                       # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    def __init__(self, groups: int = 2, replicas: int = 3) -> None:
        self.groups, self.replicas = groups, replicas
        self.proc: Optional[subprocess.Popen] = None
        self.url = ""

    def start(self) -> "Gateway":
        build = subprocess.run(["make"], cwd=ROOT, capture_output=True, text=True)
        if build.returncode != 0:
            raise RuntimeError("cannot build the store: " + build.stderr[-300:])
        port = free_port()
        self.proc = subprocess.Popen(["make", "run-gateway", f"ARGS=--port {port} --groups {self.groups} --replicas {self.replicas}"],
                                     cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1):
                    return self
            except Exception:                                       # noqa: BLE001
                time.sleep(0.3)
        self.stop()
        raise RuntimeError("the gateway did not start")

    def stop(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=20)
            except Exception:                                       # noqa: BLE001
                pass

    def admin(self, action: str, node: str) -> None:
        req = urllib.request.Request(f"{self.url}/admin/{action}?node={node}", method="POST")
        urllib.request.urlopen(req, timeout=10).read()

    def nodes(self) -> List[str]:
        return [f"server{g}-{r}" for g in range(1, self.groups + 1) for r in range(1, self.replicas + 1)]


class Faults(threading.Thread):
    """Kills and revives replicas and kills one agent process, at random moments inside ``span`` seconds."""

    def __init__(self, gw: Gateway, procs: Dict[str, subprocess.Popen], respawn, span: float, seed: int, log: List[str]) -> None:
        super().__init__(daemon=True)
        self.gw, self.procs, self.respawn, self.span, self.rng, self.log = gw, procs, respawn, span, random.Random(seed), log
        self.stop_flag = threading.Event()
        self.killed_agents = 0

    def run(self) -> None:
        events = []
        for g in range(1, self.gw.groups + 1):
            for _ in range(2):                                      # two kill/revive episodes per shard group
                events.append((self.rng.uniform(0.05, 0.9) * self.span, "replica", g))
        events.append((self.rng.uniform(0.2, 0.7) * self.span, "agent", 0))
        events.sort()
        down: Dict[int, str] = {}
        t0 = time.time()
        for at, kind, g in events:
            while time.time() - t0 < at:
                if self.stop_flag.wait(0.05):
                    return
            try:
                if kind == "replica":
                    if g in down:                                   # never two replicas of one group down at once
                        self.gw.admin("revive", down.pop(g))
                    node = f"server{g}-{self.rng.randint(1, self.gw.replicas)}"
                    self.gw.admin("kill", node)
                    down[g] = node
                    self.log.append(f"{time.time() - t0:5.1f}s killed {node}")
                else:
                    live = [o for o, p in self.procs.items() if p.poll() is None]
                    if live:
                        victim = self.rng.choice(live)
                        self.procs[victim].send_signal(signal.SIGKILL)
                        self.killed_agents += 1
                        self.log.append(f"{time.time() - t0:5.1f}s SIGKILL agent process {victim}")
                        time.sleep(0.3)
                        self.respawn(victim)
            except Exception as e:                                  # noqa: BLE001 - a failed injection is logged, the run goes on
                self.log.append(f"injection failed: {e}")
        for node in down.values():
            try:
                self.gw.admin("revive", node)
            except Exception:                                       # noqa: BLE001
                pass

    def finish(self) -> None:
        self.stop_flag.set()
        for node in self.gw.nodes():
            try:
                self.gw.admin("revive", node)
            except Exception:                                       # noqa: BLE001
                pass


def run_cluster(gw: Gateway, strategy: str = "full-batch", seed: int = 1, tasks: int = 160, procs: int = 4, threads: int = 8,
                faults: bool = False, latency: float = 0.05, validate_delay: float = 0.0, claim_ttl: float = 4.0,
                trace: Optional[str] = None, model: str = "eco", compress: float = 1.0, timeout: float = 600.0,
                window: float = 0.05, max_batch: int = 8) -> dict:
    stamp = time.time_ns()
    main, run_id = f"stress{stamp}", f"s{stamp % 10 ** 12}"
    kv = HttpKV(gw.url, timeout=30)
    repo = R.initial_repo(kv, main)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(HERE.parent), str(ROOT / "workspace" / "src")]))
    base = [sys.executable, "-m", "stress.agent_proc", "--url", gw.url, "--main", main, "--run", run_id, "--threads", str(threads), "--seed", str(seed),
            "--tasks", str(tasks), "--strategy", strategy, "--latency", str(latency), "--validate-delay", str(validate_delay),
            "--claim-ttl", str(claim_ttl), "--window", str(window), "--max-batch", str(max_batch)]
    if trace:
        base += ["--trace", trace, "--model", model, "--compress", str(compress)]
    running: Dict[str, subprocess.Popen] = {}
    outputs: List[str] = []

    def spawn(owner: str) -> None:
        running[owner] = subprocess.Popen(base + ["--owner", owner], cwd=HERE.parent, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)

    def respawn(old: str) -> None:
        spawn(old + "r")
    t0 = time.time()
    for i in range(procs):
        spawn(f"p{i}")
    log: List[str] = []
    f = Faults(gw, running, respawn, span=max(3.0, tasks * 0.05), seed=seed, log=log) if faults else None
    if f:
        f.start()
    deadline = time.time() + timeout
    timed_out = False
    while time.time() < deadline:
        if all(p.poll() is not None for p in list(running.values())):
            break
        time.sleep(0.2)
    else:
        timed_out = True
        for p in running.values():
            if p.poll() is None:
                p.kill()
    secs = time.time() - t0
    if f:
        f.finish()
    stats: List[dict] = []
    for owner, p in running.items():
        try:
            out = (p.communicate(timeout=5)[0] or "").strip()
            outputs.append(out[-400:])
            for line in out.splitlines():
                if line.startswith("STATS "):
                    stats.append(json.loads(line[6:]))
        except Exception:                                           # noqa: BLE001
            pass
    stress = W.generate(seed, tasks)
    repo = Repository(HttpKV(gw.url, timeout=60), main=main)
    kv2 = HttpKV(gw.url, timeout=60)
    summary_landed = _landed(kv2, run_id, stress)
    row = {"strategy": strategy, "seed": seed, "tasks": tasks, "edits": W.edit_total(stress), "agents": procs * threads,
           "procs": procs, "faults": faults, "seconds": round(secs, 2), "timed_out": timed_out, "landed": len(summary_landed),
           "fault_log": log, "agents_killed": (f.killed_agents * threads) if f else 0}
    row.update(C.verdict(repo, kv2, run_id, stress, W.initial_files(), True, summary_landed))
    row["process_output"] = [o for o in outputs if o][-2:]
    if stats:                                                       # a killed process reports nothing, so only fault-free runs are exact
        row["agent_runs"] = sum(x["agent_runs"] for x in stats)
        row["worker_tokens"] = sum(x["worker_tokens"] for x in stats)
        l = lambda n, k: sum((x["ladder"].get(str(n)) or x["ladder"].get(n) or {}).get(k, 0) for x in stats)       # noqa: E731
        row["ladder_tokens"] = l(2, "tokens") + l(3, "tokens")
        row["layer2"] = {"tried": l(2, "tried"), "settled": l(2, "settled")}
        row["redo_landings"] = l(4, "settled")
        row["tokens_total"] = row["worker_tokens"] + row["ladder_tokens"]
        row["batches"] = sum((x.get("batch") or {}).get("batches", 0) for x in stats)
        row["validations"] = sum((x.get("batch") or {}).get("validations", 0) for x in stats)
    from cmws_ws.agents import Escalations
    row["escalations"] = [{"task": e["task"], "reason": e["reason"], "detail": str(e.get("detail", ""))[:120],
                           "steps": [s[:2] for s in e.get("steps", [])][-4:]}
                          for e in Escalations(kv2).list() if str(e.get("task", "")).startswith(run_id)][:6]
    return row


def _landed(kv, run_id: str, stress) -> set:
    from cmws_ws.claims import TaskClaims
    snap = TaskClaims(kv, ttl=1e9, prefix=f"runs/{run_id}/").snapshot()
    return {t.id for t in stress if snap.get(f"{run_id}-{t.id}", {}).get("state") == "completed"
            and snap[f"{run_id}-{t.id}"]["outcome"].get("status") == "done"}
