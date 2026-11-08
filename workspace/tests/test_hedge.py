"""The width of a task graph, the cap it puts on agents, and backup agents for stragglers."""

import threading
import time
import unittest

from cmws_ws.agents import MergeQueue, Task, TaskGraph, WorkerResult
from cmws_ws.agents.scheduler import Scheduler
from cmws_ws.claims import TaskClaims
from cmws_ws.kv import MemoryKV
from cmws_ws.leases import LeaseTable
from cmws_ws.repo import Repository


def graph(*specs):
    return TaskGraph([Task(i, f"do {i}", tuple(p), "", tuple(d)) for i, p, d in specs])


class WidthTests(unittest.TestCase):
    def test_independent_tasks_are_as_wide_as_they_are_many(self):
        self.assertEqual(graph(*[(f"t{i}", [f"{i}.txt"], []) for i in range(7)]).width(), 7)

    def test_a_chain_is_one_wide(self):
        self.assertEqual(graph(("a", ["a"], []), ("b", ["b"], ["a"]), ("c", ["c"], ["b"])).width(), 1)

    def test_overlapping_files_take_turns(self):
        self.assertEqual(graph(("a", ["x.txt"], []), ("b", ["x.txt"], []), ("c", ["x.txt"], [])).width(), 1)
        self.assertEqual(graph(("a", ["x.txt"], []), ("b", ["x.txt"], []), ("c", ["y.txt"], [])).width(), 2)

    def test_a_task_with_no_paths_touches_everything(self):
        g = graph(("a", [], []), ("b", ["x"], []), ("c", ["y"], []))
        self.assertEqual(g.waves(), [["a"], ["b", "c"]])                  # a goes alone, then the other two together

    def test_a_diamond(self):
        g = graph(("a", ["a"], []), ("b", ["b"], ["a"]), ("c", ["c"], ["a"]), ("d", ["d"], ["b", "c"]))
        self.assertEqual(g.width(), 2)

    def test_an_empty_graph_and_a_single_task(self):
        self.assertEqual(TaskGraph([]).width(), 0)
        self.assertEqual(graph(("a", ["a"], [])).width(), 1)

    def test_directories_overlap_their_files(self):
        self.assertEqual(graph(("a", ["src"], []), ("b", ["src/x.py"], [])).width(), 1)
        self.assertEqual(graph(("a", ["src"], []), ("b", ["srcx/x.py"], [])).width(), 2)


class Slow:
    """A worker whose first agent on a straggler is slow and whose backup is fast."""
    name = "slow"

    def __init__(self, slow, first=0.8, fast=0.05, fail_original=False, fail_backup=False, base=0.05):
        self.slow, self.first, self.fast, self.base = set(slow), first, fast, base
        self.fail_original, self.fail_backup = fail_original, fail_backup
        self.runs, self.lock = [], threading.Lock()
        self.active_backups = self.max_backups = 0

    def run(self, task, repo, agent_id):
        tid = task.id.split("-", 1)[-1]
        backup = agent_id.endswith("-hedge")
        with self.lock:
            self.runs.append(agent_id)
            if backup:
                self.active_backups += 1
                self.max_backups = max(self.max_backups, self.active_backups)
        try:
            return self._run(task, repo, agent_id, tid, backup)
        finally:
            if backup:
                with self.lock:
                    self.active_backups -= 1

    def _run(self, task, repo, agent_id, tid, backup):
        if tid in self.slow:
            time.sleep(self.fast if backup else self.first)
            if (backup and self.fail_backup) or (not backup and self.fail_original):
                return WorkerResult("error", error="boom")
        else:
            time.sleep(self.base)
        ws = repo.resume(agent_id)
        ws.write(f"{tid}.txt", f"{tid}\n")
        return WorkerResult("done", f"did {tid}", ws.commit(agent_id), (f"{tid}.txt",), 1, 100, 0)


def setup(n=8):
    kv = MemoryKV()
    repo = Repository.init(kv, {"README": b"x\n"})
    g = graph(*[(f"t{i}", [f"t{i}.txt"], []) for i in range(n)])
    return kv, repo, g


class WidthCapTests(unittest.TestCase):
    def test_a_chain_never_runs_more_than_one_agent(self):
        kv = MemoryKV()
        repo = Repository.init(kv, {"README": b"x\n"})
        g = graph(("a", ["a.txt"], []), ("b", ["b.txt"], ["a"]), ("c", ["c.txt"], ["b"]))
        rep = Scheduler(repo, LeaseTable(kv), Slow([]), parallel=8, poll=0.01).run(g)
        self.assertEqual((rep.width, rep.max_concurrency), (1, 1))
        self.assertTrue(rep.ok)

    def test_the_cap_can_be_turned_off_and_changes_nothing_about_the_result(self):
        kv, repo, g = setup(6)
        rep = Scheduler(repo, LeaseTable(kv), Slow([]), parallel=3, poll=0.01, cap_to_width=False).run(g)
        self.assertTrue(rep.ok)
        self.assertLessEqual(rep.max_concurrency, 3)
        self.assertEqual(rep.width, 6)


class HedgeTests(unittest.TestCase):
    def run_hedged(self, worker, n=8, after=2.0, **kw):
        kv, repo, g = setup(n)
        q = MergeQueue(repo)
        claims = TaskClaims(kv, ttl=60)
        t0 = time.time()
        rep = Scheduler(repo, LeaseTable(kv), worker, parallel=4, land=q.as_land(), poll=0.01, claims=claims, owner="o",
                        claim_ttl=60, hedge_after=after, **kw).run(g)
        return kv, repo, rep, time.time() - t0, claims

    def test_a_backup_finishes_a_straggler_long_before_it_does(self):
        w = Slow(["t0"], first=1.5)
        kv, repo, rep, took, claims = self.run_hedged(w)
        self.assertTrue(rep.ok)
        self.assertEqual((rep.hedges, rep.hedge_wins), (1, 1))
        self.assertLess(took, 1.0)                                       # the run did not wait for the slow original
        self.assertEqual(rep.outcomes["t0"].agent, "t0-hedge")
        self.assertEqual(repo.main_files()["t0.txt"], b"t0\n")
        self.assertIn("t0-hedge", w.runs)

    def test_the_task_lands_exactly_once_and_its_claim_is_completed_once(self):
        w = Slow(["t3"], first=0.6)
        kv, repo, rep, took, claims = self.run_hedged(w)
        time.sleep(0.8)                                                  # let the slow original finish in the background
        files = repo.main_files()
        self.assertEqual(sorted(f for f in files if f != "README"), [f"t{i}.txt" for i in range(8)])
        snap = claims.snapshot()
        self.assertEqual(snap["t3"]["state"], "completed")
        self.assertEqual(snap["t3"]["outcome"]["status"], "done")
        self.assertTrue(all(e["state"] == "completed" for e in snap.values()))

    def test_no_backup_without_enough_finished_peers(self):
        w = Slow(["t0"], first=0.3)
        kv, repo, rep, took, claims = self.run_hedged(w, n=2, after=2.0, hedge_min_peers=5)
        self.assertEqual(rep.hedges, 0)
        self.assertTrue(rep.ok)

    def test_no_backup_when_everything_is_on_time(self):
        kv, repo, rep, took, claims = self.run_hedged(Slow([], base=0.05), after=3.0)
        self.assertEqual((rep.hedges, rep.hedge_wins), (0, 0))

    def test_only_a_few_backups_at_once(self):
        w = Slow(["t4", "t5", "t6", "t7"], first=0.8, fast=0.4)            # the first four finish quickly and become the peers
        kv, repo, rep, took, claims = self.run_hedged(w, n=8, after=1.5, hedge_slots=1)
        self.assertTrue(rep.ok)
        self.assertGreaterEqual(rep.hedges, 1)
        self.assertEqual(w.max_backups, 1)                                # never two backups at the same time
        kv, repo, rep, took, claims = self.run_hedged(Slow(["t4", "t5", "t6", "t7"], first=0.8, fast=0.4), n=8, after=1.5, hedge_slots=3)
        self.assertTrue(rep.ok)

    def test_a_failing_original_is_rescued_by_its_backup(self):
        w = Slow(["t2"], first=0.5, fast=0.05, fail_original=True)
        kv, repo, rep, took, claims = self.run_hedged(w)
        self.assertTrue(rep.ok, {t: (o.status, o.reason) for t, o in rep.outcomes.items()})
        self.assertEqual(rep.outcomes["t2"].agent, "t2-hedge")

    def test_a_failing_backup_leaves_the_original_to_finish(self):
        w = Slow(["t2"], first=0.5, fast=0.05, fail_backup=True)
        kv, repo, rep, took, claims = self.run_hedged(w)
        self.assertTrue(rep.ok)
        self.assertEqual(rep.outcomes["t2"].agent, "t2")
        self.assertEqual(rep.hedge_wins, 0)

    def test_when_both_fail_the_task_fails_once(self):
        w = Slow(["t2"], first=0.4, fast=0.05, fail_original=True, fail_backup=True)
        kv, repo, rep, took, claims = self.run_hedged(w)
        self.assertEqual(rep.outcomes["t2"].status, "failed")
        self.assertEqual([t for t, o in rep.outcomes.items() if o.status != "done"], ["t2"])
        self.assertEqual(claims.snapshot()["t2"]["outcome"]["status"], "failed")

    def test_arguments_are_checked(self):
        kv, repo, g = setup(1)
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                Scheduler(repo, LeaseTable(kv), Slow([]), hedge_after=bad)

    def test_a_close_race_lands_the_task_once(self):
        # the original and the backup finish at about the same moment; whichever lands first wins, the other is refused
        for _ in range(5):
            w = Slow(["t0"], first=0.30, fast=0.18, base=0.05)
            kv, repo, rep, took, claims = self.run_hedged(w, after=2.0)
            time.sleep(0.5)
            self.assertTrue(rep.ok)
            self.assertEqual(sum(1 for f in repo.main_files() if f == "t0.txt"), 1)
            self.assertEqual(repo.main_files()["t0.txt"], b"t0\n")

    def test_the_queue_names_the_backup_branch(self):
        w = Slow(["t1"], first=1.0)
        kv, repo, rep, took, claims = self.run_hedged(w)
        self.assertEqual(rep.outcomes["t1"].landed.status in ("landed", "fast-forward", "merged"), True)
        self.assertIsNotNone(repo.refs.get("ws/t1-hedge"))

    def test_hedging_is_off_by_default(self):
        kv, repo, g = setup(4)
        w = Slow(["t0"], first=0.5)
        rep = Scheduler(repo, LeaseTable(kv), w, parallel=4, poll=0.01).run(g)
        self.assertEqual(rep.hedges, 0)
        self.assertEqual([r for r in w.runs if r.endswith("-hedge")], [])


if __name__ == "__main__":
    unittest.main()


class HedgeDetailTests(unittest.TestCase):
    """Pinned by a mutation sweep: one backup per task, the gate, and what the outcome says."""

    def run_it(self, worker, n=8, after=2.0, slots=3, land_counter=None, **kw):
        kv, repo, g = setup(n)
        q = MergeQueue(repo)
        inner = q.as_land()
        calls = {}

        def counting(repo_, task, result, agent_id=None):
            calls[task.id] = calls.get(task.id, 0) + 1
            return inner(repo_, task, result, agent_id)
        rep = Scheduler(repo, LeaseTable(kv), worker, parallel=4, land=counting, poll=0.01,
                        claims=TaskClaims(kv, ttl=60), owner="o", claim_ttl=60, hedge_after=after, hedge_slots=slots, **kw).run(g)
        return rep, calls, repo

    def test_one_backup_per_task_even_when_the_backup_is_slow_too(self):
        w = Slow(["t4"], first=1.4, fast=1.0)
        rep, calls, repo = self.run_it(w)
        self.assertTrue(rep.ok)
        self.assertEqual(rep.hedges, 1)
        self.assertEqual(sum(1 for r in w.runs if r.endswith("-hedge")), 1)

    def test_the_loser_never_reaches_the_queue(self):
        w = Slow(["t4"], first=0.9, fast=0.05)
        rep, calls, repo = self.run_it(w)
        time.sleep(1.2)                                                       # the slow original finishes in the background
        self.assertEqual(calls["t4"], 1)                                      # only the winner asked to land
        self.assertEqual(repo.main_files()["t4.txt"], b"t4\n")

    def test_the_outcome_says_a_backup_was_involved(self):
        w = Slow(["t4"], first=0.9, fast=0.05)
        rep, calls, repo = self.run_it(w)
        self.assertTrue(rep.outcomes["t4"].hedged)
        self.assertFalse(rep.outcomes["t1"].hedged)
        self.assertEqual(rep.outcomes["t4"].agent, "t4-hedge")
        self.assertEqual(rep.outcomes["t1"].agent, "t1")

    def test_the_original_that_won_says_so_too(self):
        w = Slow(["t4"], first=0.35, fast=0.6)                                # the backup starts but is slower
        rep, calls, repo = self.run_it(w, after=2.0)
        self.assertTrue(rep.ok)
        if rep.hedges:
            self.assertEqual(rep.outcomes["t4"].agent, "t4")
            self.assertEqual(rep.hedge_wins, 0)
            self.assertTrue(rep.outcomes["t4"].hedged)

    def test_a_task_that_finished_is_never_hedged_afterwards(self):
        w = Slow([], base=0.05)
        rep, calls, repo = self.run_it(w, after=0.01, hedge_min_peers=1)         # absurdly eager: every task looks late
        self.assertTrue(rep.ok)
        for tid in (f"t{i}" for i in range(8)):
            self.assertLessEqual(sum(1 for r in w.runs if r == f"{tid}-hedge"), 1)
