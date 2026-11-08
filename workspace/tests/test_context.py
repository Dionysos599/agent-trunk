"""Shared context (the project map), and the new options of the orchestrator working together."""

import threading
import time
import unittest

from cmws_ws.agents import BatchQueue, FnValidator, MergeQueue, Orchestrator, Task, TaskGraph, Verdict, WorkerResult
from cmws_ws.agents.context import MAX_FILES, build_map, shared_context
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository


class MapTests(unittest.TestCase):
    def test_paths_sizes_names_and_first_lines(self):
        m = build_map({"app.py": b"import os\n\ndef main():\n    pass\n\nclass App:\n    pass\n", "README": b"\n\nHello world\n",
                       "data.bin": b"\xff\xfe", "empty.txt": b""})
        self.assertIn("app.py  (7 lines)  defines main, App", m)
        self.assertIn("README  (3 lines)  starts: Hello world", m)
        self.assertIn("data.bin  (2 bytes, binary)", m)
        self.assertIn("empty.txt  (0 lines)", m)
        self.assertTrue(m.startswith("Project files (a map"))

    def test_sorted_and_deterministic(self):
        files = {"b.py": b"def f(): pass\n", "a.py": b"def g(): pass\n"}
        self.assertEqual(build_map(files), build_map(dict(reversed(list(files.items())))))
        self.assertLess(build_map(files).index("a.py"), build_map(files).index("b.py"))

    def test_the_map_is_cut_at_the_size_limit_and_says_how_many_files_are_missing(self):
        files = {f"f{i:03}.py": b"def x(): pass\n" for i in range(100)}
        m = build_map(files, max_chars=500)
        self.assertLessEqual(len(m), 560)
        self.assertRegex(m, r"\.\.\. and \d+ more files")
        self.assertIn("f000.py", m)

    def test_a_very_large_project_is_capped_by_count(self):
        files = {f"f{i}.txt": b"x\n" for i in range(MAX_FILES + 50)}
        m = build_map(files, max_chars=10 ** 9)
        self.assertIn("... and 50 more files", m)

    def test_an_empty_project(self):
        self.assertEqual(build_map({}), "Project files (a map, not their contents; read what you will change):")

    def test_top_level_names_only(self):
        m = build_map({"a.py": b"class A:\n    def method(self): pass\n\nasync def run(): pass\n"})
        self.assertIn("defines A, run", m)
        self.assertNotIn("method", m)

    def test_unicode_and_awkward_names(self):
        m = build_map({"dir one/ünï.py": "def héllo(): pass\n".encode()})
        self.assertIn("dir one/ünï.py", m)


class CacheTests(unittest.TestCase):
    def test_computed_once_per_state_of_main(self):
        kv = MemoryKV()
        repo = Repository.init(kv, {"a.py": b"def f(): pass\n"})
        old_main = repo.head()
        first = shared_context(repo)
        key = [k for k in kv._data if k.startswith("ctx/")] if hasattr(kv, "_data") else []
        again = shared_context(repo)
        self.assertEqual(first, again)
        self.assertEqual(len(key), 1)
        ws = repo.fork("w")
        ws.write("b.py", "def g(): pass\n")
        ws.commit("b")
        repo.submit(ws)
        new = shared_context(repo)
        self.assertIn("b.py", new)
        self.assertNotEqual(new, first)
        self.assertEqual(shared_context(repo, commit=old_main), first)                     # the old state is still there

    def test_many_threads_share_one_computation(self):
        kv = MemoryKV()
        repo = Repository.init(kv, {f"f{i}.py": b"def x(): pass\n" for i in range(20)})
        out = []

        def get():
            out.append(shared_context(Repository(kv)))
        ts = [threading.Thread(target=get) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(len(set(out)), 1)
        self.assertEqual(len([k for k in kv._data if k.startswith("ctx/")]), 1)

    def test_a_different_size_limit_is_a_different_entry(self):
        repo = Repository.init(MemoryKV(), {"a.py": b"def f(): pass\n"})
        self.assertNotEqual(len(shared_context(repo, 40)), len(shared_context(repo, 3000)))


class Collect:
    name = "collect"

    def __init__(self, delay=0.02):
        self.contexts, self.delay, self.lock = {}, delay, threading.Lock()

    def run(self, task, repo, agent_id):
        with self.lock:
            self.contexts[task.id] = task.context
        time.sleep(self.delay)
        ws = repo.resume(agent_id)
        ws.write(f"{task.id.split('-', 1)[-1]}.txt", "x\n")
        return WorkerResult("done", "ok", ws.commit(agent_id), (), 1, 50, 0)


class OrchestratorOptionsTests(unittest.TestCase):
    def graph(self, n):
        return TaskGraph([Task(f"t{i}", f"task {i}", (f"t{i}.txt",), "own background" if i == 0 else "") for i in range(n)])

    def test_every_task_gets_the_map_and_keeps_its_own_context(self):
        repo = Repository.init(MemoryKV(), {"app.py": b"def main(): pass\n"})
        w = Collect()
        out = Orchestrator(repo, w, share_context=True, poll=0.01, patience=5).run("r1", graph=self.graph(3))
        self.assertTrue(out.ok)
        self.assertEqual(len(w.contexts), 3)
        for tid, ctx in w.contexts.items():
            self.assertIn("app.py  (1 lines)  defines main", ctx)
        self.assertTrue(w.contexts["r1-t0"].startswith("own background"))

    def test_off_by_default(self):
        repo = Repository.init(MemoryKV(), {"app.py": b"def main(): pass\n"})
        w = Collect()
        Orchestrator(repo, w, poll=0.01, patience=5).run("r1", graph=self.graph(2))
        self.assertTrue(all("Project files" not in c for c in w.contexts.values()))

    def test_a_batching_queue_inside_an_orchestrated_run(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        v = []
        validator = FnValidator(lambda files, changed: (v.append(1), time.sleep(0.05), Verdict(True))[2])
        q = BatchQueue(repo, validator, window=0.05, max_batch=16)
        w = Collect(delay=0.05)
        out = Orchestrator(repo, w, queue=q, parallel=12, poll=0.005, patience=30).run("r1", graph=self.graph(24))
        self.assertTrue(out.ok, out.report.outcomes)
        self.assertEqual(len([f for f in repo.main_files() if f.endswith(".txt")]), 24)
        self.assertLess(len(v), 24)                                    # fewer validations than landings
        self.assertEqual(out.report.width, 24)

    def test_the_same_run_with_the_plain_queue_validates_every_landing(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        v = []
        validator = FnValidator(lambda files, changed: (v.append(1), Verdict(True))[1])
        out = Orchestrator(repo, Collect(), queue=MergeQueue(repo, validator), parallel=6, poll=0.005, patience=30).run("r1", graph=self.graph(10))
        self.assertTrue(out.ok)
        self.assertEqual(len(v), 10)

    def test_hedging_through_the_orchestrator_reports_in_the_summary(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})

        class Straggle(Collect):
            def run(self, task, repo_, agent_id):
                if task.id.endswith("t5") and not agent_id.endswith("-hedge"):
                    time.sleep(1.5)
                return super().run(task, repo_, agent_id)
        t0 = time.time()
        out = Orchestrator(repo, Straggle(delay=0.05), parallel=3, hedge_after=2.0, poll=0.01, patience=30).run("r1", graph=self.graph(8))
        self.assertTrue(out.ok)
        self.assertEqual((out.report.hedges, out.report.hedge_wins), (1, 1))
        self.assertLess(time.time() - t0, 1.3)


if __name__ == "__main__":
    unittest.main()


class MapDetailTests(unittest.TestCase):
    def test_an_empty_file_has_no_starts_clause(self):
        self.assertEqual(build_map({"empty.txt": b"   \n\n"}).splitlines()[1], "empty.txt  (2 lines)")

    def test_the_size_limit_is_exact(self):
        files = {"a.txt": b"x\n", "b.txt": b"y\n", "c.txt": b"z\n"}
        header = "Project files (a map, not their contents; read what you will change):"
        rows = ["a.txt  (1 lines)  starts: x", "b.txt  (1 lines)  starts: y", "c.txt  (1 lines)  starts: z"]
        exact = len(header) + sum(len(r) + 1 for r in rows[:2])
        m = build_map(files, max_chars=exact)
        self.assertEqual(m.splitlines(), [header, rows[0], rows[1], "... and 1 more files"])
        m = build_map(files, max_chars=exact - 1)
        self.assertEqual(m.splitlines(), [header, rows[0], "... and 2 more files"])
        m = build_map(files, max_chars=exact + len(rows[2]) + 1)
        self.assertEqual(m.splitlines(), [header] + rows)

    def test_the_cache_is_used(self):
        import cmws_ws.agents.context as ctx
        from unittest import mock
        repo = Repository.init(MemoryKV(), {"a.py": b"def f(): pass\n"})
        with mock.patch.object(ctx, "build_map", wraps=ctx.build_map) as spy:
            shared_context(repo)
            shared_context(repo)
            shared_context(repo)
        self.assertEqual(spy.call_count, 1)
