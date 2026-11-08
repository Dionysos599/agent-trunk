"""The tools for an orchestrating agent: add_tasks, wait_for_tasks, board_digest, explain_escalation."""

import threading
import time
import unittest

from cmws_ws.agents.worker import Task
from cmws_ws.mcp import TrunkError
from cmws_ws.mcp.server import build_server
from cmws_ws.repo import Repository

from tests.test_mcp import Env, rpc, read, write

FILES = {"stats.py": b"COUNT = 0\n", "a.py": b"a = 1\n", "b.py": b"b = 1\n"}


def T(i, **kw):
    return {"id": f"t{i}", "description": f"task {i}", "paths": [f"{i}.m"], **kw}


class AddTasksTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(FILES)
        self.addCleanup(self.env.close)
        self.boss = self.env.svc("boss")

    def test_several_tasks_at_once_with_policies_checks_and_layers(self):
        r = self.boss.add_tasks([T(1, merge={"a.py": "append"}, accept=[{"kind": "exists", "path": "a.py"}], layers=[2, 4]),
                                 T(2, depends_on=["t1"])])
        self.assertEqual(r["added"], ["t1", "t2"])
        tasks = {t.id: t for t in self.boss.tasks()}
        self.assertEqual(tasks["t1"].merge, (("a.py", "append"),))
        self.assertEqual(tasks["t1"].layers, (2, 4))
        self.assertEqual(tasks["t2"].depends_on, ("t1",))
        rows = {x["id"]: x["state"] for x in self.boss.board_view()["tasks"]}
        self.assertEqual(rows, {"t1": "open", "t2": "waiting"})

    def test_tasks_can_depend_on_earlier_ones_and_agents_can_claim_them(self):
        self.boss.add_tasks([T(1)])
        self.boss.add_tasks([T(2, depends_on=["t1"])])
        worker = self.env.svc("w")
        c = worker.claim()
        self.assertEqual(c["task"]["id"], "t1")

    def test_a_bad_list_is_refused_whole(self):
        before = [t.id for t in self.boss.tasks()]
        for bad in ([], "x", None, [1], [{"id": "a"}], [{"description": "x"}], [{"id": "a", "description": 5}],
                    [T(1), T(1)], [T(1, depends_on=["nope"])], [T(1, depends_on=["t1"])],
                    [T(1, accept=[{"kind": "nope"}])], [T(1, merge={"a.py": "sometimes"})], [T(1, layers=[9])],
                    [{"id": "bad id!", "description": "x"}], [{"id": "x" * 80, "description": "x"}],
                    [{"id": "a", "description": "x", "depends_on": ["b"]}, {"id": "b", "description": "x", "depends_on": ["a"]}]):
            with self.assertRaises(TrunkError, msg=repr(bad)[:60]):
                self.boss.add_tasks(bad)
            self.assertEqual([t.id for t in self.boss.tasks()], before)           # nothing was stored

    def test_a_name_already_on_the_board_is_refused(self):
        self.boss.add_tasks([T(1)])
        with self.assertRaises(TrunkError) as cm:
            self.boss.add_tasks([T(2), T(1)])
        self.assertIn("t1", str(cm.exception))
        self.assertEqual([t.id for t in self.boss.tasks()], ["t1"])

    def test_the_size_limit(self):
        with self.assertRaises(TrunkError):
            self.boss.add_tasks([T(i) for i in range(self.boss.MAX_ADD + 1)])
        self.assertEqual(len(self.boss.add_tasks([T(i) for i in range(self.boss.MAX_ADD)])["added"]), self.boss.MAX_ADD)

    def test_warnings_for_checks_that_cannot_fail(self):
        r = self.boss.add_tasks([T(1, accept=[{"kind": "contains", "path": "a.py", "text": "a = 1"}])])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("already true", r["warnings"][0])

    def test_two_orchestrators_adding_the_same_names_do_not_both_win(self):
        other = self.env.svc("boss2")
        results, gate = [], threading.Barrier(2)

        def add(svc):
            gate.wait()
            try:
                svc.add_tasks([T(1), T(2)])
                results.append("ok")
            except TrunkError:
                results.append("refused")
        ts = [threading.Thread(target=add, args=(s,)) for s in (self.boss, other)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertEqual([t.id for t in self.boss.tasks()], ["t1", "t2"])

    def test_over_mcp(self):
        srv = build_server(self.boss)
        r = srv.handle(rpc("tools/call", {"name": "add_tasks", "arguments": {"tasks": [T(1)]}}))["result"]
        self.assertFalse(r["isError"])
        r = srv.handle(rpc("tools/call", {"name": "add_tasks", "arguments": {"tasks": "nope"}}))["result"]
        self.assertTrue(r["isError"])
        r = srv.handle(rpc("tools/call", {"name": "add_tasks", "arguments": {}}))["result"]
        self.assertTrue(r["isError"])


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(FILES)
        self.addCleanup(self.env.close)
        self.boss = self.env.svc("boss")
        self.boss.add_tasks([T(1), T(2), T(3)])
        self.naps = []

    def finish(self, name, i, text="x\n"):
        svc = self.env.svc(name)
        c = svc.claim(f"t{i}")
        write(c, f"f{i}.txt", text)
        self.assertEqual(svc.submit()["status"], "landed")

    def test_returns_at_once_when_everything_is_already_done(self):
        for i in (1, 2, 3):
            self.finish(f"w{i}", i)
        r = self.boss.wait_for_tasks(timeout=5)
        self.assertEqual((r["timed_out"], sorted(r["settled"]), r["pending"]), (False, ["t1", "t2", "t3"], {}))

    def test_times_out_with_what_is_pending(self):
        self.finish("w1", 1)
        slept = []
        self.boss._sleep = lambda s: (slept.append(s), setattr(self.env.clock, "t", self.env.clock.t + s))[0]
        r = self.boss.wait_for_tasks(["t1", "t2"], timeout=2, poll=0.5)
        self.assertTrue(r["timed_out"])
        self.assertEqual((sorted(r["settled"]), sorted(r["pending"])), (["t1"], ["t2"]))
        self.assertGreaterEqual(len(slept), 3)
        self.assertLessEqual(sum(slept), 2.0001)

    def test_any_of_returns_on_the_first(self):
        self.finish("w1", 1)
        r = self.boss.wait_for_tasks(["t1", "t2", "t3"], timeout=5, any_of=True)
        self.assertFalse(r["timed_out"])
        self.assertEqual(sorted(r["settled"]), ["t1"])

    def test_wakes_when_a_task_lands_meanwhile(self):
        env = Env(FILES, clock=time.time)                                 # a real clock for this one
        self.addCleanup(env.close)
        boss = env.svc("boss")
        boss.add_tasks([T(1), T(2)])

        def land():
            time.sleep(0.3)
            svc = env.svc("late")
            c = svc.claim("t2")
            write(c, "f.txt", "x\n")
            svc.submit()
        th = threading.Thread(target=land)
        th.start()
        r = boss.wait_for_tasks(["t2"], timeout=10, poll=0.05)
        th.join(10)
        self.assertEqual((r["timed_out"], sorted(r["settled"])), (False, ["t2"]))
        self.assertGreater(r["waited"], 0.2)

    def test_failed_and_blocked_tasks_count_as_settled(self):
        self.boss.add_tasks([{"id": "child", "description": "x", "paths": ["c.m"], "depends_on": ["t1"]}])
        w = self.env.svc("w")
        w.claim("t1")
        epoch = w._active()["epoch"]
        w.claims.complete("b1-t1", "w", epoch, {"status": "failed", "reason": "gave up"})
        r = self.boss.wait_for_tasks(["t1", "child"], timeout=0)
        self.assertFalse(r["timed_out"])
        self.assertEqual((r["settled"]["t1"]["state"], r["settled"]["child"]["state"]), ("failed", "blocked"))

    def test_unknown_and_bad_arguments(self):
        with self.assertRaises(TrunkError):
            self.boss.wait_for_tasks(["nope"])
        for bad in (-1, "5", True, None):
            with self.assertRaises(TrunkError, msg=repr(bad)):
                self.boss.wait_for_tasks(timeout=bad)

    def test_the_timeout_is_capped(self):
        self.finish("w1", 1)
        self.finish("w2", 2)
        self.finish("w3", 3)
        t0 = time.time()
        self.boss.wait_for_tasks(timeout=10 ** 9)                         # everything is done: returns at once
        self.assertLess(time.time() - t0, 2)

    def test_zero_timeout_is_a_peek(self):
        r = self.boss.wait_for_tasks(timeout=0)
        self.assertTrue(r["timed_out"])
        self.assertEqual(sorted(r["pending"]), ["t1", "t2", "t3"])


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.env = Env({"a.txt": b"1\n2\n3\n"})
        self.addCleanup(self.env.close)
        self.boss = self.env.svc("boss")
        self.boss.add_tasks([{"id": f"t{i}", "description": f"edit {i}", "paths": [f"{i}.m"]} for i in range(1, 4)])

    def test_an_empty_board_digest(self):
        d = self.boss.board_digest()
        self.assertEqual(d["counts"], {"open": 3})
        self.assertEqual((d["landed"], d["failed"], d["needs_decision"]), ([], [], []))
        self.assertIn("Nothing needs a decision", d["next"])
        self.assertIn("board b1: 3 open", d["text"])

    def test_landed_tasks_are_listed_without_diffs(self):
        w = self.env.svc("w")
        c = w.claim("t1")
        write(c, "new.txt", "hello\n")
        w.submit()
        d = self.boss.board_digest()
        self.assertEqual([x["id"] for x in d["landed"]], ["t1"])
        self.assertEqual(d["counts"], {"done": 1, "open": 2})
        self.assertNotIn("hello", d["text"])

    def test_an_escalated_task_shows_up_with_its_conflicts_and_can_be_explained(self):
        from cmws_ws.agents import MergeQueue
        repo = Repository(self.env.kv)
        for name, text in (("a", "X\n2\n3\n"), ("b", "Y\n2\n3\n")):
            ws = repo.fork(name)
            ws.write("a.txt", text)
            ws.commit(name)
        q = MergeQueue(repo, max_redos=0)
        self.assertTrue(q.land(Task("a", "x")).landed)
        landing = q.land(Task("b1-t2", "y"), None, "b")
        self.assertEqual(landing.status, "escalated")
        d = self.boss.board_digest()
        self.assertEqual(len(d["needs_decision"]), 1)
        item = d["needs_decision"][0]
        self.assertEqual((item["task"], item["conflicts"]), ("t2", [["a.txt", "content"]]))
        self.assertIn("NEEDS DECISION", d["text"])
        self.assertIn("explain_escalation", d["next"])
        e = self.boss.explain_escalation(item["escalation"])
        self.assertEqual(e["record"]["task"], "b1-t2")
        self.assertIn("-1", e["branch_changes"] + "-1")
        self.assertIn("Y", e["branch_changes"])

    def test_explaining_an_unknown_escalation(self):
        with self.assertRaises(TrunkError):
            self.boss.explain_escalation("esc-nope")

    def test_the_digest_is_capped_and_says_so(self):
        self.boss.add_tasks([{"id": f"x{i}", "description": "d", "paths": [f"x{i}.m"]} for i in range(30)])
        d = self.boss.board_digest(max_items=5)
        self.assertLessEqual(len(d["active"]), 5)
        self.assertTrue(d["truncated"])
        full = self.boss.board_digest(max_items=100)
        self.assertFalse(full["truncated"])

    def test_digest_over_mcp(self):
        srv = build_server(self.boss)
        r = srv.handle(rpc("tools/call", {"name": "board_digest", "arguments": {}}))["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["counts"], {"open": 3})
        r = srv.handle(rpc("tools/call", {"name": "explain_escalation", "arguments": {"escalation_id": "none"}}))["result"]
        self.assertTrue(r["isError"])
        r = srv.handle(rpc("tools/call", {"name": "wait_for_tasks", "arguments": {"timeout": 0}}))["result"]
        self.assertFalse(r["isError"])
        self.assertTrue(r["structuredContent"]["timed_out"])


if __name__ == "__main__":
    unittest.main()
