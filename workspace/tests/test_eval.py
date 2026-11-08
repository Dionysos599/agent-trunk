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
             oracle_redo=True, max_redos=6, tag="t", reapply="none", layers="2,3,4", continue_layer=False, sloppy_merger=False, no_guard=False, untracked_reads=False, assume_reads=False)
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


class IsolationLevelTests(unittest.TestCase):
    """The strategies of doc 12 as levels, and the serializability column."""

    def run_level(self, suite, strategy, reps=3, **kw):
        return [run_eval.run_one(SUITES[suite](), strategy, "oracle", args(**kw), rep) for rep in range(1, reps + 1)]

    def test_suite_checks_are_valid_declarations(self):
        from cmws_ws.checks import parse_checks
        from cmws_ws.policy import rules_from_declaration
        for name, make in SUITES.items():
            for t in make().tasks:
                parse_checks(list(t.accept))
                rules_from_declaration(dict(t.merge))

    def test_every_suite_check_holds_on_the_right_answer_and_fails_before(self):
        from cmws_ws.checks import parse_checks, run_checks
        from cmws_ws.serial import MemFS
        for name in ("disjoint", "counter", "shared", "semantic"):
            s = SUITES[name]()
            before = {p: t.encode() for p, t in s.files.items()}
            fs = MemFS(s.files)
            for t in s.tasks:
                old = {p: x.encode() for p, x in fs.files().items()}
                t.edit(fs)
                new = {p: x.encode() for p, x in fs.files().items()}
                checks = parse_checks(list(t.accept))
                self.assertEqual(run_checks(checks, old, new), [], (name, t.id))          # applied serially, every check passes
                self.assertTrue(run_checks(checks, old, old), (name, t.id))                # and fails if nothing changed

    def test_gitlike_loses_the_counter_and_is_not_serializable(self):
        rows = self.run_level("counter", "gitlike")
        self.assertTrue(all(r["serializable"] is False for r in rows))
        self.assertTrue(all(r["lost_updates"] > 0 for r in rows))

    def test_snapshot_and_forkmerge_are_serializable_on_the_counter(self):
        for strategy in ("forkmerge", "snapshot", "serializable"):
            rows = self.run_level("counter", strategy)
            self.assertTrue(all(r["serializable"] is True and r["lost_updates"] == 0 for r in rows), strategy)

    def test_transition_checks_alone_make_gitlike_serializable(self):
        rows = self.run_level("counter", "gitlike-checks")
        self.assertTrue(all(r["serializable"] is True and r["lost_updates"] == 0 and r["all_ok"] for r in rows))

    def test_the_shared_suite_is_serializable_at_every_fork_level(self):
        for strategy in ("gitlike", "forkmerge", "serializable", "serializable-append"):
            rows = self.run_level("shared", strategy)
            self.assertTrue(all(r["serializable"] is True for r in rows), strategy)

    def test_append_removes_the_collisions_on_the_shared_suite(self):
        rows = self.run_level("shared", "serializable-append", max_redos=0)
        self.assertTrue(all(r["all_ok"] and r["conflicts"] == 0 and r["escalated"] == 0 for r in rows), rows[0])

    def test_without_append_the_same_run_needs_redos(self):
        rows = self.run_level("shared", "serializable", max_redos=0)
        self.assertTrue(any(r["conflicts"] > 0 for r in rows))

    def test_the_shared_folder_is_not_serializable_when_updates_are_lost(self):
        rows = self.run_level("counter", "naive")
        self.assertTrue(any(r["serializable"] is False for r in rows))
        self.assertTrue(all(r["serializable"] is not None for r in rows))

    def test_serial_is_always_serializable_in_landing_order(self):
        for suite in ("counter", "shared", "disjoint"):
            for r in self.run_level(suite, "serial"):
                self.assertTrue(r["serial_landing_order"], suite)

    def test_disjoint_is_serializable_for_everyone(self):
        for strategy in ("naive", "gitlike", "forkmerge", "serializable", "serializable-append", "gitlike-checks"):
            self.assertTrue(all(r["serializable"] for r in self.run_level("disjoint", strategy)), strategy)

    def test_model_backends_have_no_serial_verdict(self):
        s = SUITES["disjoint"]()
        cols = run_eval._serial_columns(s, "ollama", "forkmerge", None, {}, {})
        self.assertIsNone(cols["serializable"])

    def test_semantic_suite_ends_with_a_working_program_at_every_level(self):
        for strategy in ("gitlike", "forkmerge", "serializable", "gitlike-checks"):
            for r in self.run_level("semantic", strategy, reps=2):
                self.assertFalse(r["main_broken"], strategy)

    def test_report_has_a_serializable_column(self):
        import report
        rows = self.run_level("counter", "gitlike", reps=2) + self.run_level("counter", "forkmerge", reps=2)
        out = report.table(rows, ("suite", "strategy"), report.COLS)
        self.assertIn("serializable", out)
        self.assertIn("0/2", out)
        self.assertIn("2/2", out)
        rows[0]["serializable"] = None
        self.assertIn("n/a", report.table([dict(rows[0], serializable=None)], ("suite", "strategy"), report.COLS))


class LadderColumnTests(unittest.TestCase):
    def run_row(self, suite, **kw):
        return run_eval.run_one(SUITES[suite](), "serializable", "oracle", args(**kw), 1)

    def test_an_oracle_reapplier_settles_the_collisions_at_layer_two(self):
        row = self.run_row("counter", reapply="oracle", max_redos=0)
        self.assertTrue(row["all_ok"])
        self.assertEqual(row["escalated"], 0)
        self.assertGreaterEqual(row["ladder"]["2"]["settled"] if "2" in row["ladder"] else row["ladder"][2]["settled"], 1)
        self.assertEqual(row["via"].get("reapply"), sum(1 for d in row["detail"] if d["via"] == "reapply"))

    def test_without_a_reapplier_the_same_run_needs_redos(self):
        row = self.run_row("counter", reapply="none", max_redos=0)
        self.assertGreater(row["escalated"], 0)
        self.assertEqual(sum(v["tried"] for v in row["ladder"].values()), 0)

    def test_layers_can_be_switched_off_from_the_harness(self):
        row = self.run_row("counter", reapply="oracle", layers="4", max_redos=0)
        self.assertGreater(row["escalated"], 0)

    def test_the_oracle_reapplier_gives_up_on_a_multi_file_edit(self):
        row = self.run_row("semantic", reapply="oracle")
        self.assertFalse(row["main_broken"])

    def test_tokens_in_the_row_include_the_ladder(self):
        row = self.run_row("counter", reapply="oracle")
        self.assertIn("ladder_tokens", row)
        self.assertGreaterEqual(row["tokens"], row["ladder_tokens"])
