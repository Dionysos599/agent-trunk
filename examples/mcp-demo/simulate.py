"""Play the demo with scripted agents, speaking MCP to real `agenttrunk mcp` processes.

    python3 simulate.py [--agents 4] [--url http://127.0.0.1:8080] [--chaos]

It makes its own copy of the project and board (a fresh main and a fresh board name), so it never touches
what you set up by hand. Each agent is a separate server process, one per agent name, exactly as a Claude
Code window would be. The agents work at the same time and collide on registry.py and CHANGELOG.md; the
output shows who had to sync, and at the end the result is exported and checked.

With --append the two shared files are declared `append` in the project's `.agenttrunk-merge`, so the agents' added
lines are merged instead of colliding (compare the number of syncs with and without it).

With --chaos the run is also attacked while it goes on:
  * one replica of each shard group of the store is killed a moment after the start and revived later
    (needs the gateway's admin endpoint, which `make run-gateway` serves);
  * the first agent kills its own server process right after writing its files, before it submits, and
    starts a new one under the same name: the task, the claim and the edits must still be there.
The result must be exactly as correct as without chaos.

What the scripted agents do is what the real agents are asked to do: claim, write the files, append their
line to the two shared files, submit, and when told main moved, sync, look, and submit again.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT / "workspace" / "src")]

import solutions as S                                    # noqa: E402
from cmws_ws.kv import HttpKV                            # noqa: E402
from cmws_ws.repo import Repository                      # noqa: E402

REPLICAS = ("server1-1", "server2-2")                    # one replica in each of the gateway's two groups


class Client:
    """One MCP server process and the JSON-RPC conversation with it."""

    def __init__(self, url, main, board, agent, workdir, validate):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "workspace" / "src"))
        self.p = subprocess.Popen(
            [sys.executable, "-m", "cmws_ws", "--url", url, "--main", main, "mcp", "--board", board, "--agent", agent,
             "--workdir", str(workdir), "--validate-cmd", validate, "--claim-ttl", "60"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "simulate"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, **args):
        t = time.time()
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        if time.time() - t > 5:
            print(f"      (slow call: {name} took {time.time() - t:.1f}s)", flush=True)
        return r["isError"], r["structuredContent"]

    def kill(self):
        self.p.kill()
        self.p.wait()
        for f in (self.p.stdin, self.p.stdout):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                                  # noqa: BLE001
            self.p.kill()


def ensure_line(path: Path, line: str) -> None:
    """Make the file hold every line that is there plus ``line`` at the end.

    Conflict markers are removed the way a person would: both sides' lines are kept, the base section of a
    three-way marker (between ||||||| and =======) is dropped, and lines that now appear twice are merged.
    """
    kept, seen, in_base = [], set(), False
    for ln in path.read_text().splitlines():
        if ln.startswith("<<<<<<<"):
            continue
        if ln.startswith("|||||||"):
            in_base = True
            continue
        if ln.startswith("======="):
            in_base = False
            continue
        if ln.startswith(">>>>>>>"):
            continue
        if in_base:
            continue
        if ln.strip() and ln in seen:
            continue
        seen.add(ln)
        kept.append(ln)
    if line not in seen:
        kept.append(line)
    path.write_text("\n".join(kept) + "\n")


def agent(name, url, main, board, workdir, validate, log, results, crash):
    make = lambda: Client(url, main, board, name, workdir / name, validate)         # noqa: E731
    cl = make()
    done = []
    try:
        deadline = time.time() + 300
        while time.time() < deadline:
            err, c = cl.tool("claim_task")
            if err:
                if "no task is ready" in c.get("error", ""):
                    view = cl.tool("show_board")[1]
                    if all(t["state"] in ("done", "failed", "blocked") for t in view["tasks"]):
                        break
                    time.sleep(0.2)
                    continue
                log(f"{name}: cannot claim: {c.get('error')}")
                break
            tid = c["task"]["id"]
            t = S.TASKS[tid]
            folder = Path(c["folder"])
            log(f"{name}: claimed {tid}")
            (folder / f"{t['module']}.py").write_text(t["source"])
            (folder / f"test_{t['module']}.py").write_text(t["test"])
            if crash.get(name):
                crash[name] = False
                log(f"{name}: CHAOS: killing my own server process with {tid} unsubmitted")
                cl.kill()
                cl = make()                                  # a new process, the same agent name
                err, again = cl.tool("claim_task")
                log(f"{name}: after the restart claim_task says: ok={again.get('ok')} resumed={again.get('resumed')} {again.get('error', '')}")
                err, st = cl.tool("task_status")
                log(f"{name}: after the restart the folder still has {sorted(st.get('uncommitted', {}))} "
                    f"and the claim is {'held' if st.get('claim_held') else 'LOST'}")
            for attempt in range(1, 40):
                ensure_line(folder / "registry.py", S.registry_line(tid))
                ensure_line(folder / "CHANGELOG.md", S.changelog_line(tid))
                err, r = cl.tool("submit_task", message=f"{tid}")
                if r.get("status") == "landed":
                    log(f"{name}: landed {tid}" + (f" after {attempt - 1} sync(s)" if attempt > 1 else ""))
                    done.append((tid, attempt - 1))
                    break
                if r.get("status") == "stale" or "conflict markers" in r.get("error", ""):
                    err2, s = cl.tool("sync_with_main")
                    log(f"{name}: {tid} was behind main; sync: {s.get('status')}"
                        + (f", conflicts in {s['conflicts']}" if s.get("conflicts") else ""))
                    continue
                if r.get("status") == "rejected":
                    log(f"{name}: {tid} rejected by the check: {str(r.get('detail'))[-120:]!r}")
                    cl.tool("sync_with_main")
                    continue
                log(f"{name}: {tid} unexpected answer {r}")
                cl.tool("release_task")
                break
    finally:
        cl.tool("release_task")                      # a task still held (an error above) must not block others
        results[name] = done
        cl.close()


def admin(url, action, node):
    req = urllib.request.Request(f"{url}/admin/{action}?node={node}", method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def replica_chaos(url, log, stop):
    """Kill one replica of each group soon after the start; bring them back when the run is over or after 6 s."""
    time.sleep(0.3)
    for node in REPLICAS:
        try:
            admin(url, "kill", node)
            log(f"CHAOS: replica {node} killed")
        except Exception as e:                                # noqa: BLE001
            log(f"CHAOS: could not kill {node}: {e}")
    stop.wait(6)
    for node in REPLICAS:
        try:
            admin(url, "revive", node)
            log(f"CHAOS: replica {node} revived")
        except Exception:                                     # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--url", default=os.environ.get("AGENTTRUNK_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--chaos", action="store_true", help="kill replicas and one agent's server process during the run")
    ap.add_argument("--append", action="store_true",
                    help="declare registry.py and CHANGELOG.md as `append` (a .agenttrunk-merge file in the project): "
                         "two agents adding lines at the end no longer collide")
    a = ap.parse_args()
    tag = str(time.time_ns())[-12:]
    main_ref, board = f"demo{tag}", f"sim{tag}"[:32]
    kv = HttpKV(a.url, timeout=30)
    files = {p.name: p.read_bytes() for p in (HERE / "project").iterdir() if p.is_file()}
    if a.append:
        files[".agenttrunk-merge"] = b"registry.py append\nCHANGELOG.md append\n"
    Repository.init(kv, files, main=main_ref)
    tasks = json.loads((HERE / "tasks.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "workspace" / "src"))
        r = subprocess.run([sys.executable, "-m", "cmws_ws", "--url", a.url, "--main", main_ref, "board", board,
                            "--tasks", str(HERE / "tasks.json")], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr or r.stdout)
            return 2
        print(f"project in main '{main_ref}', board '{board}', {len(tasks)} tasks, {a.agents} agents"
              + ("  [chaos on]" if a.chaos else "") + ("  [append on]" if a.append else "") + "\n")
        lock = threading.Lock()
        t0 = time.time()

        def log(msg):
            with lock:
                print(f"{time.time() - t0:6.1f}s  {msg}", flush=True)
        results, stop = {}, threading.Event()
        validate = f"{sys.executable} -m unittest discover"
        crash = {"agent1": a.chaos}
        ts = [threading.Thread(target=agent, args=(f"agent{i + 1}", a.url, main_ref, board, Path(tmp), validate, log, results, crash))
              for i in range(a.agents)]
        chaos = threading.Thread(target=replica_chaos, args=(a.url, log, stop)) if a.chaos else None
        if chaos:
            chaos.start()
        [t.start() for t in ts]
        [t.join() for t in ts]
        stop.set()
        if chaos:
            chaos.join()
        landed = sum(len(v) for v in results.values())
        syncs = sum(n for v in results.values() for _, n in v)
        print(f"\n{landed}/{len(tasks)} tasks landed in {time.time() - t0:.1f}s; agents had to sync {syncs} time(s) because "
              "they collided on the shared files")
        out = Path(tmp) / "result"
        out.mkdir()
        for rel, data in Repository(kv, main=main_ref).main_files().items():
            (out / rel).write_bytes(data)
        print()
        return subprocess.run([sys.executable, str(HERE / "check.py"), str(out)]).returncode


if __name__ == "__main__":
    sys.exit(main())
