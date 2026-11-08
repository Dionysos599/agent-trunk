"""Run stress runs and append one JSON row per run.

    python -m stress cluster --runs 20 [--faults] [--strategy full-batch] [--out FILE]
    python -m stress inproc --strategies gitlike,snapshot,full-redo,full,full-batch --runs 20 [--out FILE]
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("cluster")
    c.add_argument("--runs", type=int, default=20)
    c.add_argument("--faults", action="store_true")
    c.add_argument("--strategy", default="full-batch")
    c.add_argument("--tasks", type=int, default=160)
    c.add_argument("--procs", type=int, default=4)
    c.add_argument("--threads", type=int, default=8)
    c.add_argument("--latency", type=float, default=0.05)
    c.add_argument("--validate-delay", type=float, default=0.0)
    c.add_argument("--claim-ttl", type=float, default=4.0)
    c.add_argument("--seed0", type=int, default=1)
    c.add_argument("--url")
    c.add_argument("--trace")
    c.add_argument("--model", default="eco")
    c.add_argument("--compress", type=float, default=1.0)
    c.add_argument("--timeout", type=float, default=600.0)
    c.add_argument("--out", type=Path)
    i = sub.add_parser("inproc")
    i.add_argument("--strategies", default="shared-folder,gitlike,snapshot,full-redo,full,full-batch")
    i.add_argument("--runs", type=int, default=20)
    i.add_argument("--tasks", type=int, default=160)
    i.add_argument("--agents", type=int, default=32)
    i.add_argument("--latency", type=float, default=0.03)
    i.add_argument("--validate-delay", type=float, default=0.0)
    i.add_argument("--seed0", type=int, default=1)
    i.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    sink = a.out.open("a") if a.out else None
    try:
        if a.mode == "inproc":
            from stress import runner
            for strat in a.strategies.split(","):
                for k in range(a.runs):
                    if strat == "shared-folder":
                        r = runner.run_naive(a.seed0 + k, a.tasks, a.agents, a.latency)
                    else:
                        r = runner.run_inprocess(strat, seed=a.seed0 + k, tasks=a.tasks, agents=a.agents, latency=a.latency,
                                                 validate_delay=a.validate_delay)
                    _emit(r, sink)
        else:
            from stress import cluster
            gw = None
            if not a.url:
                gw = cluster.Gateway().start()
            url = a.url or gw.url
            try:
                class G:
                    pass
                g = gw or G()
                if not gw:
                    g.url, g.groups, g.replicas = url, 2, 3
                    g.admin = lambda *x: None
                    g.nodes = lambda: []
                for k in range(a.runs):
                    r = cluster.run_cluster(g, a.strategy, a.seed0 + k, a.tasks, a.procs, a.threads, a.faults, a.latency,
                                            a.validate_delay, a.claim_ttl, a.trace, a.model, a.compress, a.timeout)
                    _emit(r, sink)
            finally:
                if gw:
                    gw.stop()
    finally:
        if sink:
            sink.close()
    return 0


def _emit(row, sink):
    short = {k: row.get(k) for k in ("strategy", "seed", "faults", "agents", "seconds", "landed", "tasks", "correct",
                                     "edits_lost_silent", "edits_pending", "edits_duplicated", "serial_ok")}
    print(json.dumps(short), flush=True)
    if sink:
        sink.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        sink.flush()


if __name__ == "__main__":
    sys.exit(main())
