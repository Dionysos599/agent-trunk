"""The evaluation harness itself: suites are sound, and the strategies behave as the results claim."""

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))

import run_eval                                    # noqa: E402
from suites import SUITES                          # noqa: E402
from workers import DirFS, OracleWorker, base_id   # noqa: E402


def args(**kw):
    d = dict(latency=0.05, seed=1, parallel=4, model="m", llm_url="", llm_host=None, max_steps=5, timeout=10,
             oracle_redo=True, max_redos=6, tag="t", sloppy_merger=False, no_guard=False, untracked_reads=False, assume_reads=False)
    d.update(kw)
    return argparse.Namespace(**d)


class SuiteTests(unittest.TestCase):
    def test_every_suite_starts_valid_and_unsatisfied(self):
        for name, make in SUITES.items():
            s = make()
            self.assertTrue(run_eval._invariant_ok(s, s.files), name)
            sat = run_eval._satisfied(s, s.files)
            self.assertFalse(any(sat.values()), (name, sat))

    def test_applying_the_edits_one_after_another_is_the_right_answer(self):
        for name in ("disjoint", "counter", "shared"):
            s = SUITES[name]()
            with tempfile.TemporaryDirectory() as tmp:
                fs = DirFS(Path(tmp))
                for p, t in s.files.items():
                    fs.write(p, t)
                for t in s.tasks:
                    t.edit(fs)
                final = fs.files()
            self.assertTrue(all(run_eval._satisfied(s, final).values()), name)
            self.assertTrue(run_eval._invariant_ok(s, final), name)

    def test_the_semantic_pair_merges_textually_but_breaks_the_program(self):
        s = SUITES["semantic"]()
        with tempfile.TemporaryDirectory() as tmp:
            fs = DirFS(Path(tmp))
            for p, t in s.files.items():
                fs.write(p, t)
            for t in s.tasks:
                t.edit(fs)
            final = fs.files()
        self.assertFalse(run_eval._invariant_ok(s, final))

    def test_task_ids_are_recovered_from_branch_names(self):
        self.assertEqual([base_id(x) for x in ("r1-t1", "r1-t1-redo2", "r12-t3")], ["t1", "t1", "t3"])

    def test_an_oracle_reads_before_it_waits(self):
        s = SUITES["counter"]()
        with tempfile.TemporaryDirectory() as tmp:
            fs = DirFS(Path(tmp))
            fs.write("stats.py", s.files["stats.py"])
            o = OracleWorker({t.id: t.edit for t in s.tasks}, latency=0.0)
            fs.write("stats.py", "COUNT = 41\n")
            from cmws_ws.agents import Task
            o.run_on(Task("t1", "d"), fs)
            self.assertIn("COUNT = 42", fs.read_text("stats.py"))


class StrategyTests(unittest.TestCase):
    def run_suite(self, suite, strategy, **kw):
        return run_eval.run_one(SUITES[suite](), strategy, "oracle", args(**kw), 1)

    def test_fork_and_merge_loses_no_update_and_never_breaks_main(self):
        for suite in ("counter", "shared"):
            for rep in range(3):
                r = self.run_suite(suite, "forkmerge", seed=rep)
                self.assertEqual((r["lost_updates"], r["main_broken"]), (0, False), (suite, rep, r))
                self.assertTrue(r["all_ok"], (suite, r))                  # with enough redos everything lands
                self.assertGreater(r["conflicts"], 0)                     # and the collisions were real

    def test_without_redos_the_losers_are_escalated_not_lost(self):
        r = self.run_suite("counter", "forkmerge", max_redos=0, oracle_redo=False)
        self.assertEqual((r["lost_updates"], r["main_broken"]), (0, False))
        self.assertGreater(r["escalated"], 0)
        self.assertFalse(r["all_ok"])

    def test_the_naive_shared_folder_loses_updates(self):
        lost = [self.run_suite("counter", "naive", latency=0.1, seed=i)["lost_updates"] for i in range(3)]
        self.assertGreater(sum(lost), 0)

    def test_the_naive_shared_folder_can_end_with_a_broken_program(self):
        r = self.run_suite("semantic", "naive")
        self.assertTrue(r["main_broken"])
        r = self.run_suite("semantic", "forkmerge")
        self.assertFalse(r["main_broken"])                                # the merge queue refuses it

    def test_agents_whose_reads_are_not_recorded_lose_updates_unless_reads_are_assumed(self):
        lost = sum(self.run_suite("counter", "forkmerge", untracked_reads=True, seed=i)["lost_updates"] for i in range(3))
        self.assertGreater(lost, 0)                           # identical edits merge as "agreement"
        for i in range(3):
            r = self.run_suite("counter", "forkmerge", untracked_reads=True, assume_reads=True, seed=i)
            self.assertEqual((r["lost_updates"], r["main_broken"], r["all_ok"]), (0, False, True), r)

    def test_honest_scopes_serialise_the_tasks_and_avoid_conflicts(self):
        r = self.run_suite("counter", "forkmerge-honest")
        self.assertEqual((r["conflicts"], r["all_ok"]), (0, True))

    def test_the_control_suite_is_fine_everywhere(self):
        for strategy in ("naive", "serial", "forkmerge"):
            self.assertTrue(self.run_suite("disjoint", strategy)["all_ok"], strategy)


if __name__ == "__main__":
    unittest.main()
