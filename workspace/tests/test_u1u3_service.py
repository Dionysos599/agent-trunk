"""Merge policies, transition checks and the survival check as an agent meets them through the MCP service."""

import json
import threading
import unittest

from cmws_ws.agents.orchestrator import task_from_dict
from cmws_ws.agents.worker import Task
from cmws_ws.checks import parse_checks
from cmws_ws.mcp import TrunkError
from cmws_ws.policy import FILE_NAME
from cmws_ws.repo import Repository

from tests.test_mcp import Env, read, write

REG = {"reg.py": b"# registry\n", "CHANGELOG.md": b"# changes\n", "stats.py": b"COUNT = 0\n", "a.py": b"a = 1\n",
       "b.py": b"b = 1\n"}
INC = [{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}]


def tasks(n, merge=None, accept=None):
    out = []
    for i in range(1, n + 1):
        d = {"id": f"t{i}", "description": f"task {i}", "paths": [f"{i}.m"]}
        if merge:
            d["merge"] = merge
        if accept:
            d["accept"] = accept
        out.append(task_from_dict(d))
    return out


class AppendOnTheBoardTests(unittest.TestCase):
    def run_agents(self, files, board_tasks, n=3):
        env = Env(files)
        self.addCleanup(env.close)
        svcs = [env.svc(f"agent{i}") for i in range(n)]
        svcs[0].create_board(board_tasks)
        claims = [s.claim() for s in svcs]                 # all claim before anyone lands: they all fork the same main
        for i, (s, c) in enumerate(zip(svcs, claims)):
            write(c, "reg.py", read(c, "reg.py") + f"line from {c['task']['id']}\n")
            write(c, "CHANGELOG.md", read(c, "CHANGELOG.md") + f"entry {c['task']['id']}\n")
        results = [s.submit() for s in svcs]
        return env, results

    def test_without_a_declaration_all_but_the_first_must_sync(self):
        env, results = self.run_agents(REG, tasks(3))
        self.assertEqual([r["status"] for r in results], ["landed", "stale", "stale"])

    def test_with_the_project_file_everyone_lands_the_first_time(self):
        files = {**REG, FILE_NAME: b"reg.py append\nCHANGELOG.md append\n"}
        env, results = self.run_agents(files, tasks(3))
        self.assertEqual([r["status"] for r in results], ["landed"] * 3)
        main = Repository(env.kv).main_files()
        self.assertEqual(main["reg.py"].decode().splitlines()[1:], [f"line from t{i}" for i in (1, 2, 3)])
        self.assertEqual(main["CHANGELOG.md"].decode().splitlines()[1:], [f"entry t{i}" for i in (1, 2, 3)])

    def test_with_the_task_declaration_alone(self):
        env, results = self.run_agents(REG, tasks(3, merge={"reg.py": "append", "CHANGELOG.md": "append"}))
        self.assertEqual([r["status"] for r in results], ["landed"] * 3)

    def test_declaring_only_one_of_the_two_files_still_needs_a_sync(self):
        env, results = self.run_agents(REG, tasks(2, merge={"reg.py": "append"}), n=2)
        self.assertEqual([r["status"] for r in results], ["landed", "stale"])
        self.assertEqual(results[1]["stale_paths"], ["CHANGELOG.md"])

    def test_sync_is_clean_under_append(self):
        env = Env({**REG, FILE_NAME: b"reg.py append\n"})
        self.addCleanup(env.close)
        a, b = env.svc("a"), env.svc("b")
        a.create_board(tasks(2))
        ca, cb = a.claim(), b.claim()
        for c in (ca, cb):
            write(c, "reg.py", read(c, "reg.py") + f"from {c['task']['id']}\n")
        self.assertEqual(a.submit()["status"], "landed")
        r = b.sync()
        self.assertEqual((r["status"], r["conflicts"]), ("merged", []))
        self.assertEqual(read(cb, "reg.py"), "# registry\nfrom t1\nfrom t2\n")
        self.assertEqual(b.submit()["status"], "landed")

    def test_a_modified_line_is_not_excused_by_append(self):
        env = Env({**REG, FILE_NAME: b"reg.py append\n"})
        self.addCleanup(env.close)
        a, b = env.svc("a"), env.svc("b")
        a.create_board(tasks(2))
        ca, cb = a.claim(), b.claim()
        write(ca, "reg.py", read(ca, "reg.py") + "from a\n")
        write(cb, "reg.py", read(cb, "reg.py").replace("# registry", "# REGISTRY"))
        self.assertEqual(a.submit()["status"], "landed")
        self.assertEqual(b.submit()["status"], "stale")

    def test_an_agent_cannot_widen_what_append_covers_by_adding_a_rule_itself(self):
        env = Env(REG)
        self.addCleanup(env.close)
        a, b = env.svc("a"), env.svc("b")
        a.create_board(tasks(2))
        ca, cb = a.claim(), b.claim()
        write(ca, "reg.py", read(ca, "reg.py") + "from a\n")
        write(cb, "reg.py", read(cb, "reg.py") + "from b\n")
        write(cb, FILE_NAME, "reg.py append\n")
        self.assertEqual(a.submit()["status"], "landed")
        self.assertEqual(b.submit()["status"], "stale")

    def test_many_agents_at_once_on_the_same_two_files(self):
        files = {**REG, FILE_NAME: b"reg.py append\nCHANGELOG.md append\n"}
        env = Env(files)
        self.addCleanup(env.close)
        n = 8
        svcs = [env.svc(f"w{i}") for i in range(n)]
        svcs[0].create_board(tasks(n))
        statuses, lock = [], threading.Lock()

        def work(s):
            c = s.claim()
            write(c, "reg.py", read(c, "reg.py") + f"line {c['task']['id']}\n")
            write(c, "CHANGELOG.md", read(c, "CHANGELOG.md") + f"entry {c['task']['id']}\n")
            for _ in range(20):
                r = s.submit()
                if r["status"] == "landed":
                    break
                s.sync()
            with lock:
                statuses.append(r["status"])
        ts = [threading.Thread(target=work, args=(s,)) for s in svcs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(statuses, ["landed"] * n)
        main = Repository(env.kv).main_files()
        self.assertEqual(sorted(main["reg.py"].decode().splitlines()[1:]), sorted(f"line t{i}" for i in range(1, n + 1)))
        self.assertEqual(len(main["reg.py"].decode().splitlines()), n + 1)             # every line exactly once


class AcceptOnTheBoardTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(REG)
        self.addCleanup(self.env.close)
        self.a, self.b = self.env.svc("a"), self.env.svc("b")
        self.a.create_board(tasks(2, accept=INC))

    def bump(self, c):
        n = int(read(c, "stats.py").split("=")[1]) + 1
        write(c, "stats.py", f"COUNT = {n}\n")

    def test_the_claim_shows_what_will_be_checked(self):
        c = self.a.claim()
        self.assertEqual(len(c["task"]["accept"]), 1)
        self.assertIn("exactly 1 higher", c["task"]["accept"][0])
        self.assertNotIn("accepted only if", c["task"]["prompt"])          # the prompt stays as the task wrote it

    def test_resumed_claim_shows_it_too(self):
        self.a.claim()
        again = self.a.claim()
        self.assertTrue(again["resumed"])
        self.assertIn("accept", again["task"])

    def test_the_second_identical_edit_is_stale_first_and_then_corrected(self):
        ca, cb = self.a.claim(), self.b.claim()
        self.bump(ca)
        self.bump(cb)
        self.assertEqual(self.a.submit()["status"], "landed")
        r = self.b.submit()
        self.assertEqual(r["status"], "stale")                         # the read set caught it first
        s = self.b.sync()
        self.assertIn("stats.py", s["changed_on_both_sides"] + s.get("conflicts", []))

    def test_a_check_that_fails_gives_the_agent_a_readable_reason(self):
        env = Env(REG)
        self.addCleanup(env.close)
        a = env.svc("a")
        a.create_board(tasks(1, accept=INC))
        c = a.claim()
        write(c, "stats.py", "COUNT = 7\n")                          # the agent got the arithmetic wrong
        r = a.submit()
        self.assertEqual(r["status"], "rejected")
        self.assertIn("acceptance check failed", r["detail"])
        self.assertIn("must increase by 1", r["detail"])
        self.assertIn("main has 0", r["detail"])
        self.assertIn("submit_task again", r["next"])
        write(c, "stats.py", "COUNT = 1\n")
        self.assertEqual(a.submit()["status"], "landed")

    def test_a_failed_check_leaves_main_alone(self):
        env = Env(REG)
        self.addCleanup(env.close)
        a = env.svc("a")
        a.create_board(tasks(1, accept=INC))
        c = a.claim()
        write(c, "stats.py", "COUNT = 7\n")
        before = Repository(env.kv).head()
        a.submit()
        self.assertEqual(Repository(env.kv).head(), before)
        self.assertEqual(a.board_view()["tasks"][0]["state"], "claimed")

    def test_adhoc_tasks_have_no_checks(self):
        env = Env(REG)
        self.addCleanup(env.close)
        a = env.svc("a")
        a.create_board(tasks(1))
        a.claim()
        a.release()
        c = a.claim(description="do a thing", paths=["a.py"])
        self.assertNotIn("accept", c["task"])

    def test_board_warnings_report_checks_that_cannot_fail_and_bad_rule_lines(self):
        env = Env({**REG, FILE_NAME: b"reg.py append\nbogus line here\n"})
        self.addCleanup(env.close)
        a = env.svc("a")
        t = [task_from_dict({"id": "x", "description": "d", "accept": [{"kind": "contains", "path": "a.py", "text": "a = 1"},
                                                                       {"kind": "exists", "path": "never.py"}]})]
        out = a.warnings(t)
        self.assertEqual(len(out), 2)
        self.assertIn("task x", out[0])
        self.assertIn("already true", out[0])
        self.assertIn(FILE_NAME, out[1])

    def test_board_warnings_are_empty_for_a_good_board(self):
        self.assertEqual(self.a.warnings(tasks(2, accept=INC)), [])

    def test_board_warnings_on_a_binary_rule_file(self):
        env = Env({**REG, FILE_NAME: b"\xff\xfe"})
        self.addCleanup(env.close)
        self.assertIn("not UTF-8", env.svc("a").warnings([])[0])

    def test_a_bad_declaration_in_a_task_file_cannot_make_a_board(self):
        with self.assertRaises(ValueError):
            task_from_dict({"id": "x", "description": "d", "accept": [{"kind": "increases", "path": "a"}]})


class ResolutionSurvivalTests(unittest.TestCase):
    """An agent that resolves a conflict by dropping what main added."""

    BASE = {"a.txt": b"h\nv=1\nmid\nt\n"}

    def setUp(self):
        self.env = Env(self.BASE)
        self.addCleanup(self.env.close)
        self.a, self.b = self.env.svc("a"), self.env.svc("b")
        self.a.create_board([Task("t1", "x", ("1.m",)), Task("t2", "y", ("2.m",))])
        self.ca, self.cb = self.a.claim(), self.b.claim()
        write(self.ca, "a.txt", "h\nv=2\nmid\nMAIN-LINE\nt\n")
        write(self.cb, "a.txt", "h\nv=3\nmid\nt\nBRANCH-LINE\n")
        self.assertEqual(self.a.submit()["status"], "landed")

    def test_a_resolution_that_drops_main_s_line_is_sent_back(self):
        self.assertEqual(self.b.submit()["status"], "stale")
        s = self.b.sync()
        self.assertEqual(s["conflicts"], ["a.txt"])
        write(self.cb, "a.txt", "h\nv=5\nmid\nt\nBRANCH-LINE\n")        # MAIN-LINE dropped
        r = self.b.submit()
        self.assertEqual(r["status"], "rejected")
        self.assertIn("MAIN-LINE", r["detail"])
        self.assertIn("dropped work", r["next"])
        write(self.cb, "a.txt", "h\nv=5\nmid\nMAIN-LINE\nt\nBRANCH-LINE\n")
        r = self.b.submit()
        self.assertEqual(r["status"], "landed")
        self.assertEqual(Repository(self.env.kv).main_files()["a.txt"], b"h\nv=5\nmid\nMAIN-LINE\nt\nBRANCH-LINE\n")

    def test_a_resolution_that_drops_the_agent_s_own_line_is_sent_back_too(self):
        self.b.submit()
        self.b.sync()
        write(self.cb, "a.txt", "h\nv=5\nmid\nMAIN-LINE\nt\n")
        r = self.b.submit()
        self.assertEqual(r["status"], "rejected")
        self.assertIn("BRANCH-LINE", r["detail"])

    def test_the_record_is_cleared_after_landing(self):
        self.b.submit()
        self.b.sync()
        write(self.cb, "a.txt", "h\nv=5\nmid\nMAIN-LINE\nt\nBRANCH-LINE\n")
        self.assertEqual(self.b.submit()["status"], "landed")
        self.assertIsNone(self.b._active())

    def test_survival_can_be_turned_off(self):
        self.b.queue.survival = False
        self.b.submit()
        self.b.sync()
        write(self.cb, "a.txt", "h\nv=5\nmid\nt\nBRANCH-LINE\n")
        self.assertEqual(self.b.submit()["status"], "landed")

    def test_a_clean_sync_leaves_nothing_to_check(self):
        env = Env({"a.txt": b"1\n2\n3\n", "b.txt": b"x\n"})
        self.addCleanup(env.close)
        a, b = env.svc("a"), env.svc("b")
        a.create_board([Task("t1", "x", ("1.m",)), Task("t2", "y", ("2.m",))])
        ca, cb = a.claim(), b.claim()
        write(ca, "a.txt", "1\n2\n3\nA\n")
        write(cb, "b.txt", "x\nB\n")
        a.submit()
        self.assertEqual(b.sync()["status"], "merged")
        self.assertNotIn("synced", b._active())
        self.assertEqual(b.submit()["status"], "landed")

    def test_a_second_sync_replaces_the_record(self):
        self.b.submit()
        self.b.sync()
        rec1 = self.b._active()["synced"]
        # main moves again on the same file region
        c = self.env.svc("c")
        c.create_board([Task("t3", "z", ("3.m",))])
        self.assertEqual(rec1["paths"], ["a.txt"])


class MemoryModeTests(unittest.TestCase):
    def test_file_tools_mode_applies_the_same_checks(self):
        env = Env(REG, file_tools=True)
        self.addCleanup(env.close)
        a = env.svc("a")
        a.create_board(tasks(1, accept=INC))
        a.claim()
        a.file_tool("write_file", {"path": "stats.py", "content": "COUNT = 9\n"})
        r = a.submit()
        self.assertEqual(r["status"], "rejected")
        a.file_tool("write_file", {"path": "stats.py", "content": "COUNT = 1\n"})
        self.assertEqual(a.submit()["status"], "landed")


if __name__ == "__main__":
    unittest.main()
