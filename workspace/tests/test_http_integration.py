"""The workspace engine on the real replicated store, through the HTTP gateway.

Starts ``make run-gateway`` (2 groups x 3 replicas) in a subprocess. Skipped when make or java is
not available. Set AGENTTRUNK_SKIP_INTEGRATION=1 (or the old CMWS_SKIP_INTEGRATION) to skip explicitly.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

from cmws_ws.kv import HttpKV
from cmws_ws.repo import Repository

from tests.support import check_linear_history

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "workspace", "src")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf("1" in (os.environ.get("AGENTTRUNK_SKIP_INTEGRATION"), os.environ.get("CMWS_SKIP_INTEGRATION")), "integration tests disabled")
@unittest.skipUnless(shutil.which("make") and shutil.which("java"), "needs make and java")
class GatewayIntegrationTest(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        build = subprocess.run(["make"], cwd=ROOT, capture_output=True, text=True)
        if build.returncode != 0:
            raise unittest.SkipTest("cannot build the store: " + build.stderr[-300:])
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            ["make", "run-gateway", f"ARGS=--port {cls.port} --groups 2 --replicas 3"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.3)
        cls.tearDownClass()
        raise RuntimeError("gateway did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.proc is not None:
            try:
                os.killpg(cls.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            cls.proc.wait(timeout=20)

    def kv(self):
        return HttpKV(self.base, timeout=30)

    def admin(self, action, node):
        req = urllib.request.Request(f"{self.base}/admin/{action}?node={node}", method="POST")
        urllib.request.urlopen(req, timeout=10).read()

    def test_kv_semantics_with_awkward_keys_and_values(self):
        kv = self.kv()
        prefix = f"it-{time.time_ns()}/"
        for key, value in [(prefix + "a b/c?d&e=f", "x\ny\r\n\t\"quoted\" \\ ☃ é"),
                           (prefix + "empty", ""), (prefix + "plus+sign", "1+1")]:
            kv.put(key, value)
            self.assertEqual(kv.get(key), value, key)
        self.assertIsNone(kv.get(prefix + "missing"))
        self.assertEqual(kv.put_if_absent(prefix + "pia", "one"), (True, "one"))
        self.assertEqual(kv.put_if_absent(prefix + "pia", "two"), (False, "one"))
        self.assertEqual(kv.cas(prefix + "c", None, "v1"), (True, "v1"))
        self.assertEqual(kv.cas(prefix + "c", None, "v2"), (False, "v1"))
        self.assertEqual(kv.cas(prefix + "c", "v1", "v2"), (True, "v2"))

    def test_agents_land_work_concurrently_through_the_cluster(self):
        kv = self.kv()
        repo = Repository.init(kv, {"counter.txt": "0\n", "notes/readme.md": "# notes\n"},
                               main=f"main-{time.time_ns()}")
        agents, per = 5, 3
        landed, redone = [], []

        def agent(k):
            for i in range(per):
                attempt = 0
                while True:
                    ws = repo.fork(f"it{time.time_ns()}-{k}-{i}-{attempt}")
                    n = int(ws.read_text("counter.txt"))
                    ws.write("counter.txt", f"{n + 1}\n")
                    ws.write(f"out/{k}-{i}.txt", f"{k}:{i}\n")
                    res = repo.submit(ws, f"agent {k} step {i}")
                    if res.landed:
                        landed.append(res.commit)
                        break
                    redone.append(res.status)
                    attempt += 1

        ts = [threading.Thread(target=agent, args=(k,)) for k in range(agents)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        files = repo.main_files()
        self.assertEqual(files["counter.txt"], f"{agents * per}\n".encode())
        for k in range(agents):
            for i in range(per):
                self.assertIn(f"out/{k}-{i}.txt", files)
        self.assertEqual(len(landed), agents * per)

    def test_a_large_binary_file_goes_through_in_chunks(self):
        import os as _os
        name = f"main-{time.time_ns()}"
        repo = Repository.init(self.kv(), {"a.txt": "1\n"}, main=name)
        data = _os.urandom(1_300_000)
        ws = repo.fork(f"big-{time.time_ns()}")
        ws.write("data/model.bin", data)
        self.assertTrue(repo.submit(ws).landed)
        fresh = Repository(self.kv(), main=name)
        self.assertEqual(fresh.main_files()["data/model.bin"], data)

    def test_folder_sync_and_cli_against_the_real_cluster(self):
        import tempfile
        from pathlib import Path
        name = f"main-{time.time_ns()}"
        agent = f"cli{time.time_ns()}"
        work = Path(tempfile.mkdtemp())
        project = work / "project"
        (project / "src").mkdir(parents=True)
        (project / "src/app.py").write_text("print('v1')\n")
        (project / "README.md").write_text("# demo\n")
        (project / "__pycache__").mkdir()
        (project / "__pycache__/junk.pyc").write_text("junk")

        def cli(*args):
            r = subprocess.run([sys.executable, "-m", "cmws_ws.cli", "--url", self.base,
                                "--main", name, *args], capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": SRC}, cwd=work)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return r.stdout

        cli("init", "--from", str(project))
        cli("clone", agent, str(work / "mine"))
        mine = work / "mine"
        self.assertEqual((mine / "src/app.py").read_text(), "print('v1')\n")
        self.assertFalse((mine / "__pycache__").exists())
        (mine / "src/app.py").write_text("print('v2')\n")
        (mine / "notes.txt").write_text("hello\n")
        self.assertIn("M src/app.py", cli("status", str(mine)))
        self.assertIn("pushed", cli("push", str(mine), "-m", "my edits"))
        out = cli("submit", str(mine))
        self.assertRegex(out, "fast-forward|merged")
        repo = Repository(self.kv(), main=name)
        self.assertEqual(repo.main_files()["src/app.py"], b"print('v2')\n")
        self.assertIn("my edits", cli("log"))        # the pushed commit is what landed on main
        shutil.rmtree(work, ignore_errors=True)

    def test_work_survives_losing_a_replica_per_group_and_a_fresh_client_sees_it(self):
        kv = self.kv()
        name = f"main-{time.time_ns()}"
        repo = Repository.init(kv, {"a.txt": "1\n"}, main=name)
        ws = repo.fork(f"k-{time.time_ns()}")
        ws.write("b.txt", "2\n")
        self.assertTrue(repo.submit(ws).landed)
        self.admin("kill", "server1-1")
        self.admin("kill", "server2-2")
        try:
            ws2 = repo.fork(f"k2-{time.time_ns()}")
            ws2.write("c.txt", "3\n")
            self.assertTrue(repo.submit(ws2).landed)
            # a client with a cold cache reads everything back from the degraded cluster
            fresh = Repository(self.kv(), main=name)
            self.assertEqual(sorted(fresh.main_files()), ["a.txt", "b.txt", "c.txt"])
        finally:
            self.admin("revive", "server1-1")
            self.admin("revive", "server2-2")

    def test_leases_and_a_scheduled_run_on_the_cluster_with_replicas_down(self):
        from cmws_ws.agents import LoopWorker, Reply, ScriptedModel, Task, TaskGraph, Scheduler
        from cmws_ws.agents.llm import call
        from cmws_ws.leases import LeaseTable

        tag = f"{time.time_ns()}"
        leases = LeaseTable(self.kv(), prefix=f"it-{tag}/", ttl=120)
        wins = []
        barrier = threading.Barrier(6)

        def claim(i):
            barrier.wait()
            if LeaseTable(self.kv(), prefix=f"it-{tag}/", ttl=120).acquire(f"w{i}", ["shared", f"own{i}"]).granted:
                wins.append(i)
        ts = [threading.Thread(target=claim, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1, "overlapping claims must be serialized by the replicated store")
        leases.release(f"w{wins[0]}")

        class ByTask:
            name = "by-task"

            def run(self, task, repo, agent_id):
                model = ScriptedModel([
                    Reply("", [call("write_file", "1", path=task.paths[0], content=f"{task.id}\n")]),
                    Reply("", [call("finish", "2", summary=f"wrote {task.paths[0]}")])])
                return LoopWorker(model).run(task, repo, agent_id)

        name = f"main-{tag}"
        repo = Repository.init(self.kv(), {"base.txt": "b\n"}, main=name)
        ids = [f"s{tag}-{i}" for i in range(5)]
        tasks = [Task(i, f"write {i}", (f"out/{i}.txt",)) for i in ids]
        self.admin("kill", "server1-1")
        self.admin("kill", "server2-2")
        try:
            rep = Scheduler(repo, leases, ByTask(), parallel=3, poll=0.01).run(TaskGraph(tasks))
            self.assertTrue(rep.ok, {k: v.reason for k, v in rep.outcomes.items()})
            fresh = Repository(self.kv(), main=name)
            files = fresh.main_files()
            for i in ids:
                self.assertEqual(files[f"out/{i}.txt"], f"{i}\n".encode())
            self.assertEqual(leases.holders(), {})
        finally:
            self.admin("revive", "server1-1")
            self.admin("revive", "server2-2")

    def test_an_orchestrator_killed_mid_task_is_taken_over_and_finishes_the_run(self):
        tag = f"{time.time_ns()}"
        name, run_id = f"main-{tag}", f"run_{tag}"
        repo = Repository.init(self.kv(), {"base.txt": "b\n"}, main=name)
        runner = os.path.join(os.path.dirname(__file__), "crash_runner.py")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]))
        args = [sys.executable, runner, self.base, name, run_id, tag]
        first = subprocess.run(args + ["crash"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(first.returncode, 9, first.stderr[-500:])      # the process died inside task x
        self.assertEqual(repo.main_files(), {"base.txt": b"b\n"})       # nothing had landed
        import json
        doc = json.loads(self.kv().get(f"session/{run_id}-x{tag}"))
        self.assertEqual(doc["seq"], 3)                                  # its three writes were checkpointed
        second = subprocess.run(args + ["finish"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual((second.returncode, second.stdout.strip()), (0, "ok"), second.stderr[-500:])
        files = Repository(self.kv(), main=name).main_files()
        for t in (f"x{tag}", f"y{tag}"):
            for i in range(3):
                self.assertEqual(files[f"out/{t}/f{i}.txt"], f"{t} {i}\n".encode())
        claims = json.loads(self.kv().get(f"runs/{run_id}/claims"))
        self.assertEqual((claims[f"{run_id}-x{tag}"]["epoch"], claims[f"{run_id}-x{tag}"]["state"]), (2, "completed"))
        self.assertEqual(claims[f"{run_id}-y{tag}"]["epoch"], 1)

    # ---- MCP servers as separate processes ------------------------------------------------

    class McpProc:
        """A real `agenttrunk mcp` process spoken to over its stdin/stdout, as a client would."""

        def __init__(self, test, name, board, workdir, tasks=None, main=None):
            args = [sys.executable, "-m", "cmws_ws", "--url", test.base]
            if main:
                args += ["--main", main]
            args += ["mcp", "--board", board, "--agent", name, "--workdir", str(workdir), "--claim-ttl", "2"]
            if tasks:
                args += ["--tasks", str(tasks)]
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]))
            self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, env=env, bufsize=1)
            self.n = 0
            r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}})
            assert r["result"]["serverInfo"]["name"] == "agenttrunk", r
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            self.p.stdin.flush()

        def rpc(self, method, params=None):
            self.n += 1
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
            self.p.stdin.flush()
            line = self.p.stdout.readline()
            assert line, "the server closed its output"
            reply = json.loads(line)
            assert reply["id"] == self.n, reply
            return reply

        def tool(self, name, **args):
            r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
            return r["isError"], r["structuredContent"]

        def _release(self):
            for f in (self.p.stdin, self.p.stdout):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass

        def kill(self):
            self.p.kill()
            self.p.wait()
            self._release()

        def close(self):
            if self.p.poll() is None:
                try:
                    self.p.stdin.close()
                    self.p.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    self.p.kill()
                    self.p.wait()
            self._release()

    def test_mcp_agents_in_separate_processes_lose_no_update_and_one_can_be_killed_and_resume(self):
        import tempfile
        from pathlib import Path
        tag = f"{time.time_ns()}"
        name, board = f"main-{tag}", f"mb{tag}"[:32]
        Repository.init(self.kv(), {"stats.py": b"COUNT = 0\n"}, main=name)
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.json"
            tasks.write_text(json.dumps([{"id": f"t{i}", "description": "add one to COUNT", "paths": [f"{i}.m"]} for i in range(4)]))
            procs = {n: self.McpProc(self, n, board, Path(tmp) / n, tasks, name) for n in ("alice", "bob", "carol", "dave")}
            try:
                names = [t["name"] for t in procs["alice"].rpc("tools/list")["result"]["tools"]]
                self.assertIn("claim_task", names)
                self.assertIn("submit_task", names)
                claimed = {}
                for n, pr in procs.items():
                    err, c = pr.tool("claim_task")
                    self.assertFalse(err, c)
                    claimed[n] = c
                self.assertEqual(sorted(c["task"]["id"] for c in claimed.values()), ["t0", "t1", "t2", "t3"])
                self.admin("kill", "server1-1")                        # a replica of each group goes down mid-work
                self.admin("kill", "server2-2")
                try:
                    # dave edits but his process dies before submitting
                    dave_folder = Path(claimed["dave"]["folder"])
                    (dave_folder / "stats.py").write_text("COUNT = 1\n")
                    procs["dave"].kill()
                    for n in ("alice", "bob", "carol"):
                        pr = procs[n]
                        for _ in range(30):
                            main_count = int(pr.rpc("resources/read", {"uri": "agenttrunk://main/stats.py"})
                                             ["result"]["contents"][0]["text"].split("=")[1])
                            (Path(claimed[n]["folder"]) / "stats.py").write_text(f"COUNT = {main_count + 1}\n")
                            err, r = pr.tool("submit_task", message=n)
                            if r.get("status") == "landed":
                                break
                            self.assertEqual(r.get("status"), "stale", r)
                            err, s = pr.tool("sync_with_main")
                            self.assertFalse(err, s)
                        else:
                            self.fail(f"{n} never landed")
                finally:
                    self.admin("revive", "server1-1")
                    self.admin("revive", "server2-2")
                # dave's process comes back under the same name and finds his task and his edit
                procs["dave"] = self.McpProc(self, "dave", board, Path(tmp) / "dave", None, name)
                err, again = procs["dave"].tool("claim_task")                # asking again hands his task and folder back
                self.assertFalse(err, again)
                self.assertTrue(again["resumed"])
                self.assertEqual(again["folder"], claimed["dave"]["folder"])
                err, st = procs["dave"].tool("task_status")
                self.assertEqual(st["uncommitted"], {"stats.py": "M"})
                dave = procs["dave"]
                for _ in range(30):
                    main_count = int(dave.rpc("resources/read", {"uri": "agenttrunk://main/stats.py"})
                                     ["result"]["contents"][0]["text"].split("=")[1])
                    (dave_folder / "stats.py").write_text(f"COUNT = {main_count + 1}\n")
                    err, r = dave.tool("submit_task", message="dave")
                    if r.get("status") == "landed":
                        break
                    dave.tool("sync_with_main")
                else:
                    self.fail("dave never landed")
                self.assertEqual(Repository(self.kv(), main=name).main_files()["stats.py"], b"COUNT = 4\n")
                err, board_view = procs["alice"].tool("show_board")
                self.assertEqual({t["state"] for t in board_view["tasks"]}, {"done"})
            finally:
                for pr in procs.values():
                    pr.close()

    def test_the_agenttrunk_command_warns_about_checks_that_cannot_fail_and_refuses_bad_declarations(self):
        import tempfile
        from pathlib import Path
        tag = f"{time.time_ns()}"
        name, board = f"main-{tag}", f"cw{tag}"[:32]
        Repository.init(self.kv(), {"a.py": b"x = 1\n", ".agenttrunk-merge": b"a.py append\nnonsense\n"}, main=name)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]), AGENTTRUNK_URL=self.base)
        wrapper = os.path.join(ROOT, "bin", "agenttrunk")
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps([{"id": "one", "description": "x", "paths": ["a.py"], "merge": {"a.py": "append"},
                                         "accept": [{"kind": "contains", "path": "a.py", "text": "x = 1"}]}]))
            r = subprocess.run([wrapper, "--main", name, "board", board, "--tasks", str(good)], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("board created", r.stdout)
            self.assertIn("warning: task one:", r.stderr)
            self.assertIn("already true", r.stderr)
            self.assertIn("warning: .agenttrunk-merge:", r.stderr)
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps([{"id": "one", "description": "x", "accept": [{"kind": "increases", "path": "a.py"}]}]))
            r = subprocess.run([wrapper, "--main", name, "board", board + "b", "--tasks", str(bad)], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("accept[0]", r.stderr)
            bad.write_text(json.dumps([{"id": "one", "description": "x", "merge": {"a.py": "sometimes"}}]))
            r = subprocess.run([wrapper, "--main", name, "board", board + "c", "--tasks", str(bad)], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("sometimes", r.stderr)

    def test_orchestrate_refuses_bad_ladder_options(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]), AGENTTRUNK_URL=self.base)
        wrapper = os.path.join(ROOT, "bin", "agenttrunk")
        name = f"main-{time.time_ns()}"
        Repository.init(self.kv(), {"a.py": b"1\n"}, main=name)
        for extra, needle in ((["--layers", "9"], "--layers"), (["--layers", "x"], "--layers"), (["--reapply", "model"], "--reapply model needs --model")):
            r = subprocess.run([wrapper, "--main", name, "orchestrate", "r1", *extra, "--exec", "true"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(needle, r.stderr)

    def test_the_agenttrunk_command_creates_and_shows_a_board(self):
        import tempfile
        from pathlib import Path
        tag = f"{time.time_ns()}"
        name, board = f"main-{tag}", f"cb{tag}"[:32]
        Repository.init(self.kv(), {"a.py": b"1\n"}, main=name)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]), AGENTTRUNK_URL=self.base)
        wrapper = os.path.join(ROOT, "bin", "agenttrunk")
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "t.json"
            tasks.write_text(json.dumps([{"id": "one", "description": "x", "paths": ["a.py"]},
                                         {"id": "two", "description": "y", "paths": ["a.py"]}]))
            r = subprocess.run([wrapper, "--main", name, "board", board, "--tasks", str(tasks)], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("board created", r.stdout)
            self.assertRegex(r.stdout, r"one\s+open")
            self.assertRegex(r.stdout, r"two\s+waiting\s+one")
            again = subprocess.run([wrapper, "--main", name, "board", board, "--tasks", str(tasks)], env=env,
                                   capture_output=True, text=True, timeout=60)
            self.assertIn("already exists", again.stdout)
            bad = subprocess.run([wrapper, "--main", name, "board", "bad-name"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(bad.returncode, 2)
            out = Path(tmp) / "exported"
            ex = subprocess.run([wrapper, "--main", name, "export", str(out)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(ex.returncode, 0, ex.stderr)
            self.assertIn("1 files of", ex.stdout)
            self.assertEqual((out / "a.py").read_bytes(), b"1\n")

    def test_the_mcp_demo_scripted_agents_finish_the_board_and_the_setup_script_works(self):
        import tempfile
        demo = os.path.join(ROOT, "examples", "mcp-demo")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, ROOT]), AGENTTRUNK_URL=self.base)
        sim = subprocess.run([sys.executable, os.path.join(demo, "simulate.py"), "--agents", "4", "--url", self.base],
                             env=env, capture_output=True, text=True, timeout=170)
        self.assertEqual(sim.returncode, 0, sim.stdout[-1500:] + sim.stderr[-500:])
        self.assertIn("10/10 tasks landed", sim.stdout)
        self.assertIn("ALL DONE", sim.stdout)
        self.assertRegex(sim.stdout, r"had to sync [1-9]\d* time")                 # they really did collide
        chaos = subprocess.run([sys.executable, os.path.join(demo, "simulate.py"), "--agents", "4", "--chaos", "--url", self.base],
                               env=env, capture_output=True, text=True, timeout=170)
        self.assertEqual(chaos.returncode, 0, chaos.stdout[-1500:] + chaos.stderr[-500:])
        for expected in ("replica server1-1 killed", "replica server2-2 killed", "killing my own server process",
                         "after the restart claim_task says: ok=True resumed=True", "the claim is held", "replica server1-1 revived",
                         "10/10 tasks landed", "ALL DONE"):
            self.assertIn(expected, chaos.stdout)
        self.admin("revive", "server1-1")                                              # in case the script was cut short
        self.admin("revive", "server2-2")
        appended = subprocess.run([sys.executable, os.path.join(demo, "simulate.py"), "--agents", "4", "--append", "--chaos",
                                   "--url", self.base], env=env, capture_output=True, text=True, timeout=170)
        self.assertEqual(appended.returncode, 0, appended.stdout[-1500:] + appended.stderr[-500:])
        self.assertIn("[append on]", appended.stdout)
        self.assertIn("ALL DONE", appended.stdout)
        self.assertIn("had to sync 0 time(s)", appended.stdout)                        # declared `append`: nothing collides, even under chaos
        self.admin("revive", "server1-1")
        self.admin("revive", "server2-2")
        name = f"demo{time.time_ns()}"[:30]
        try:
            for tasks, expect_waiting in (("tasks.json", False), ("tasks-honest.json", True)):
                n = name + ("h" if expect_waiting else "c")
                r = subprocess.run([os.path.join(demo, "setup.sh"), n, os.path.join(demo, tasks)], env=env,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("board created", r.stdout)
                self.assertEqual("waiting" in r.stdout.split("Ready.")[0], expect_waiting, r.stdout)
                with open(os.path.join(demo, "agents", n, "alice", ".mcp.json")) as f:
                    cfg = json.load(f)["mcpServers"]["agenttrunk"]
                self.assertIn(n, cfg["args"])
                self.assertEqual(sorted(os.listdir(os.path.join(demo, "agents", n))), ["alice", "bob", "carol"])
        finally:
            import shutil
            shutil.rmtree(os.path.join(demo, "agents"), ignore_errors=True)

    def test_merge_queue_on_the_cluster_resolves_a_conflict_and_gates_main_with_a_real_command(self):
        from cmws_ws.agents import CommandValidator, Escalations, MergeQueue, Reply, ScriptedModel, Task, merger_worker
        from cmws_ws.agents.llm import call

        tag = f"{time.time_ns()}"
        name = f"main-{tag}"
        pad = lambda *xs: "".join(f"{x}\n" for x in xs)
        base = pad("A = 1", "# p1", "# p2", "# p3", "B = 2", "# p4", "# p5", "# p6", "C = 3")
        repo = Repository.init(self.kv(), {"cfg.py": base, "check.py": "import cfg\nassert cfg.A < cfg.C\n"}, main=name)
        edits = {"one": pad("A = 10", "# p1", "# p2", "# p3", "B = 2", "# p4", "# p5", "# p6", "C = 30"),
                 "two": pad("A = 1", "# p1", "# p2", "D = 4", "# p3", "B = 2", "# p4", "# p5", "# p6", "C = 3"),
                 "three": pad("A = 1", "# p1", "# p2", "# p3", "B = 20", "# p4", "# p5", "# p6", "C = 31")}
        for who, text in edits.items():
            ws = repo.fork(f"{who}-{tag}")
            ws.write("cfg.py", text)
            ws.commit(who)
        resolved = pad("A = 10", "# p1", "# p2", "D = 4", "# p3", "B = 20", "# p4", "# p5", "# p6", "C = 30")
        merger = merger_worker(ScriptedModel([
            Reply("", [call("write_file", "1", path="cfg.py", content=resolved)]),
            Reply("", [call("finish", "2", summary="kept both")])]))
        q = MergeQueue(repo, CommandValidator([sys.executable, "check.py"]), merger=merger,
                       escalations=Escalations(self.kv(), prefix=f"it-{tag}/"))
        self.assertTrue(q.land(Task(f"one-{tag}", "x")).landed)             # A=10, C=30
        two = q.land(Task(f"two-{tag}", "x"))                              # adds D: merges cleanly
        self.assertEqual((two.landed, two.via), (True, "auto"), two.steps)
        three = q.land(Task(f"three-{tag}", "x"))                          # B=20 next to D=4: conflicts
        self.assertTrue(three.landed, three.steps)
        final = Repository(self.kv(), main=name).main_files()["cfg.py"].decode()
        self.assertEqual(final, resolved)
        self.assertEqual(three.via, "merger")


if __name__ == "__main__":
    unittest.main()
