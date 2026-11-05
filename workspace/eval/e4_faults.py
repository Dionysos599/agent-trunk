"""E4: fault injection on the real replicated store.

    PYTHONPATH=workspace/src python3 workspace/eval/e4_faults.py --url http://127.0.0.1:8080 --reps 3 --out e4.jsonl

Scenarios (the `shared` suite, fork + merge queue, perfect agents so that only the faults can cause loss):
  baseline        nothing goes wrong
  replicas        one replica of each shard group is killed while the run is going, and revived later
  agent-kill      the orchestrator process is SIGKILLed mid-run and a new one resumes the same run
  both            both of the above
"""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src")]

from cmws_ws.kv import HttpKV
from cmws_ws.repo import Repository

from run_eval import _invariant_ok, _satisfied
from suites import SUITES

TTL = 3.0


def admin(base, action, node):
    urllib.request.urlopen(urllib.request.Request(f"{base}/admin/{action}?node={node}", method="POST"), timeout=10).read()


def run(base, scenario, suite_name, rep, rng):
    suite = SUITES[suite_name]()
    tag = f"{time.time_ns()}"
    name, run_id = f"e4main{tag}", f"e4r{tag}"
    kv = HttpKV(base, timeout=30)
    Repository.init(kv, {p: t.encode() for p, t in suite.files.items()}, main=name)
    log = f"/tmp/e4-{tag}.log"
    Path(log).write_text("")
    child = [sys.executable, str(HERE / "e4_child.py"), base, name, suite_name, run_id, log, str(TTL)]
    env = dict(os.environ, PYTHONPATH=str(HERE.parent / "src"))
    t0 = time.time()
    faults = []
    killed = [False]

    def replicas():
        time.sleep(0.8)
        for node in ("server1-1", "server2-2"):
            admin(base, "kill", node)
            faults.append(("kill", node, round(time.time() - t0, 2)))
        time.sleep(3.0)
        for node in ("server1-1", "server2-2"):
            admin(base, "revive", node)
            faults.append(("revive", node, round(time.time() - t0, 2)))
    th = None
    if scenario in ("replicas", "both"):
        th = threading.Thread(target=replicas)
        th.start()
    p = subprocess.Popen(child + ["first"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if scenario in ("agent-kill", "both"):
        delay = rng.uniform(1.0, 2.0)                       # somewhere in the middle of the tasks
        try:
            p.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            p.send_signal(signal.SIGKILL)
            p.wait()
            killed[0] = True
            faults.append(("sigkill", "orchestrator", round(time.time() - t0, 2)))
            p = subprocess.Popen(child + ["second"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = "", "timeout"
    if th:
        th.join()
        for node in ("server1-1", "server2-2"):               # make sure nothing stays down
            try:
                admin(base, "revive", node)
            except Exception:
                pass
    seconds = round(time.time() - t0, 2)
    final = {pth: d.decode() for pth, d in Repository(HttpKV(base, timeout=60), main=name).main_files().items()}
    sat = _satisfied(suite, final)
    starts = [l.strip() for l in Path(log).read_text().splitlines() if l.strip()]
    first_try = {s for s in starts if "-redo" not in s}
    return {"scenario": scenario, "suite": suite_name, "rep": rep, "tasks": len(suite.tasks),
            "completed": out.strip() == "ok", "satisfied": sum(sat.values()), "main_broken": not _invariant_ok(suite, final),
            "all_ok": all(sat.values()) and _invariant_ok(suite, final), "seconds": seconds,
            "task_starts": len(first_try) and len([s for s in starts if "-redo" not in s]),
            "repeated_starts": len([s for s in starts if "-redo" not in s]) - len(first_try),
            "redo_starts": len([s for s in starts if "-redo" in s]), "killed_agent": killed[0], "faults": faults,
            "error": err.strip()[-200:] if out.strip() != "ok" else ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--suite", default="shared")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--scenarios", default="baseline,replicas,agent-kill,both")
    ap.add_argument("--out", type=Path, default=Path("e4.jsonl"))
    a = ap.parse_args()
    rng = random.Random(42)
    with a.out.open("a") as f:
        for scenario in a.scenarios.split(","):
            for rep in range(1, a.reps + 1):
                row = run(a.url, scenario, a.suite, rep, rng)
                f.write(json.dumps(row, sort_keys=True) + "\n")
                f.flush()
                print(f"{scenario:11} #{rep}: completed={row['completed']} ok={row['all_ok']} satisfied={row['satisfied']}/{row['tasks']} "
                      f"repeated={row['repeated_starts']} redo={row['redo_starts']} {row['seconds']}s faults={len(row['faults'])}", flush=True)


if __name__ == "__main__":
    main()
