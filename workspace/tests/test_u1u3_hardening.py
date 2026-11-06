"""Cases the mutation sweep over survival.py, checks.py, policy.py and serial.py showed were not pinned down."""

import random
import sys
import unittest
from unittest import mock

from cmws_ws import checks as checks_mod
from cmws_ws.checks import MAX_PATTERN, Check, CheckError, parse_check, parse_checks, run_check, unfalsifiable
from cmws_ws.kv import MemoryKV
from cmws_ws.policy import PolicyTable
from cmws_ws.repo import Repository
from cmws_ws.serial import MemFS, check_final, check_unordered, landing_order
from cmws_ws.survival import MAX_REPORTED, survival_problems


def B(**files):
    return {k: (v.encode() if isinstance(v, str) else v) for k, v in files.items()}


class DescribeTests(unittest.TestCase):
    """The sentence handed to an agent for every kind, exactly."""

    def test_each_kind(self):
        d = lambda **kw: parse_check(kw).describe()          # noqa: E731
        self.assertEqual(d(kind="contains", path="f", text="x"), "f contains 'x'")
        self.assertEqual(d(kind="contains", path="f", text="x.", regex=True), "f contains text matching 'x.'")
        self.assertEqual(d(kind="absent", path="f", text="x"), "f does not contain 'x'")
        self.assertEqual(d(kind="absent", path="f", text="x", regex=True), "f does not contain text matching 'x'")
        self.assertEqual(d(kind="increases", path="f", pattern=r"(\d)", by=2),
                         r"the number matched by '(\d)' in f is exactly 2 higher than it is on main at the moment you land")
        self.assertIn("exactly 1 lower", d(kind="decreases", path="f", pattern=r"(\d)"))
        self.assertEqual(d(kind="exists", path="f"), "f exists")
        self.assertEqual(d(kind="missing", path="f"), "f does not exist")
        self.assertEqual(d(kind="command", argv=["a", "b"]), "the command 'a b' succeeds on the result")
        self.assertIn("changes by 3", d(kind="command", argv=["a"], pattern=r"(\d)", by=3))

    def test_defaults(self):
        self.assertFalse(Check("contains", "f", "x").regex)
        self.assertEqual(Check("exists", "f").by, 1)


class ParsingBoundaryTests(unittest.TestCase):
    def test_a_valid_but_too_long_regex_is_refused(self):
        long_pattern = "(" + "a" * MAX_PATTERN + ")"
        with self.assertRaises(CheckError):
            parse_check({"kind": "increases", "path": "f", "pattern": long_pattern})
        ok = "(" + "a" * (MAX_PATTERN - 2) + ")"
        self.assertEqual(parse_check({"kind": "increases", "path": "f", "pattern": ok}).pattern, ok)


class CommandOutputTests(unittest.TestCase):
    PY = sys.executable

    def test_stderr_only_and_stdout_only_both_reach_the_message(self):
        c = parse_check({"kind": "command", "argv": [self.PY, "-c", "import sys; sys.stderr.write('ONLY-STDERR'); sys.exit(1)"]})
        self.assertIn("ONLY-STDERR", run_check(c, {}, {}))
        c = parse_check({"kind": "command", "argv": [self.PY, "-c", "print('ONLY-STDOUT'); import sys; sys.exit(1)"]})
        self.assertIn("ONLY-STDOUT", run_check(c, {}, {}))
        c = parse_check({"kind": "command", "argv": [self.PY, "-c", "import sys; sys.exit(1)"]})
        self.assertNotIn(": ", run_check(c, {}, {}).split("failed on the result")[1])

    def test_a_number_missing_on_only_one_side_is_reported(self):
        code = "import os; print('n 5' if open('f').read() == 'new' else 'no number')"
        c = parse_check({"kind": "command", "argv": [self.PY, "-c", code], "pattern": r"n (\d+)", "by": 1})
        self.assertIn("cannot read a number", run_check(c, B(f="old"), B(f="new")))        # missing before only
        code = "print('n 5' if open('f').read() == 'old' else 'no number')"
        c = parse_check({"kind": "command", "argv": [self.PY, "-c", code], "pattern": r"n (\d+)", "by": 1})
        self.assertIn("cannot read a number", run_check(c, B(f="old"), B(f="new")))        # missing after only

    def test_unfalsifiable_reports_a_check_that_blows_up_instead_of_raising(self):
        c = parse_check({"kind": "exists", "path": "f"})
        with mock.patch.object(checks_mod, "run_check", side_effect=RuntimeError("kaboom")):
            out = checks_mod.unfalsifiable([c], {})
        self.assertEqual(len(out), 1)
        self.assertIn("could not be tried", out[0])
        self.assertIn("kaboom", out[0])


class PolicyReprTests(unittest.TestCase):
    def test_repr(self):
        self.assertEqual(repr(PolicyTable([("a", "append")])), "PolicyTable([('a', 'append')])")


class SerialHardeningTests(unittest.TestCase):
    def test_memfs_exists(self):
        fs = MemFS({"a": "1"})
        self.assertIs(fs.exists("a"), True)
        self.assertIs(fs.exists("b"), False)

    def test_defaults_of_the_verdict(self):
        edits = {"a": lambda fs: fs.write("f", "A")}
        self.assertTrue(check_final({}, {"f": "A"}, edits, ["a"]).linear)
        self.assertFalse(check_unordered({}, {"f": "A"}, edits, ["a"]).linear)
        self.assertFalse(check_final({}, {"f": "B"}, edits, ["a"]).serializable)

    def test_the_tip_of_a_branch_is_its_last_commit_whatever_the_iteration_order(self):
        rng = random.Random(3)
        for _ in range(8):
            repo = Repository.init(MemoryKV(), {"f": b""})
            names = [f"t{i}" for i in range(6)]
            wss = {n: repo.fork(n) for n in names}
            for n, ws in wss.items():                          # three commits each on a private file
                for k in range(3):
                    ws.write(f"{n}.txt", f"{k}\n")
                    ws.commit(f"{n} {k}")
            order = names[:]
            rng.shuffle(order)
            for n in order:
                self.assertTrue(repo.submit(wss[n], n).landed)
            got, linear = landing_order(repo, lambda a: a if a in names else None)
            self.assertEqual(got, order)
            self.assertTrue(linear)


class SurvivalHardeningTests(unittest.TestCase):
    def test_a_binary_file_elsewhere_in_the_result_does_not_break_the_search_for_a_moved_file(self):
        out = survival_problems(B(f="a\n"), B(f="a\nO\n"), B(f="a\n"), B(bin=b"\xff\xfe", other="a\nO\n"))
        self.assertEqual(out, [])
        out = survival_problems(B(f="a\n"), B(f="a\nO\n"), B(f="a\n"), B(bin=b"\xff\xfe"))
        self.assertTrue(out)

    def test_many_files_each_with_a_problem_are_cut_at_the_cap(self):
        n = MAX_REPORTED * 3
        mine = {f"f{i:03}": b"a\nlost\n" for i in range(n)}
        base = {f"f{i:03}": b"a\n" for i in range(n)}
        out = survival_problems(base, mine, base, base)
        self.assertEqual(len(out), MAX_REPORTED + 1)
        self.assertEqual(out[-1], "... and more")

    def test_exactly_at_the_cap_is_not_marked_as_cut(self):
        n = MAX_REPORTED
        mine = {f"f{i}": b"a\nlost\n" for i in range(n)}
        base = {f"f{i}": b"a\n" for i in range(n)}
        out = survival_problems(base, mine, base, base)
        self.assertEqual(len(out), MAX_REPORTED)

    def test_a_line_removed_by_one_side_and_replaced_away_by_the_other_stays_removed(self):
        # both sides took the 'x' lines out in different ways; a result that has them back undid both edits
        base = B(f="x\na\nx\n")
        out = survival_problems(base, B(f="a\nx\n"), B(f="x\na\ny\n"), B(f="x\na\nx\n"))
        self.assertTrue(any("removed by the branch is back" in o for o in out), out)


if __name__ == "__main__":
    unittest.main()


class FailClosedTests(unittest.TestCase):
    """A check that cannot run must refuse the landing, never let it through."""

    def setUp(self):
        from cmws_ws.agents import MergeQueue, Task
        self.MergeQueue, self.Task = MergeQueue, Task
        self.repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ws = self.repo.fork("a")
        ws.write("stats.py", "COUNT = 1\n")
        ws.commit("a")
        self.task = Task("a", "bump", (), "", (), (), parse_checks([{"kind": "increases", "path": "stats.py",
                                                                     "pattern": r"COUNT = (\d+)"}]))

    def test_a_crashing_acceptance_check_refuses(self):
        import cmws_ws.agents.mergequeue as mq
        q = self.MergeQueue(self.repo, max_redos=0)
        with mock.patch.object(mq, "run_checks", side_effect=OSError("disk gone")):
            landing = q.land(self.task)
        self.assertEqual(landing.status, "escalated")
        self.assertIn("acceptance checks could not run", landing.steps[0].detail)
        self.assertIn("disk gone", landing.steps[0].detail)
        self.assertEqual(self.repo.main_files()["stats.py"], b"COUNT = 0\n")

    def test_a_crashing_survival_check_refuses(self):
        q = self.MergeQueue(self.repo, max_redos=0)
        with mock.patch.object(MergeQueueSurvival.target(), "_survival", side_effect=KeyError("missing object")):
            r = q.attempt("a", task=self.task)
        self.assertEqual(r.status, "rejected")
        self.assertIn("survival check could not run", r.detail)
        self.assertEqual(self.repo.main_files()["stats.py"], b"COUNT = 0\n")

    def test_the_same_landing_works_without_the_mocks(self):
        q = self.MergeQueue(self.repo, max_redos=0)
        self.assertTrue(q.land(self.task).landed)


class MergeQueueSurvival:
    @staticmethod
    def target():
        from cmws_ws.agents import MergeQueue
        return MergeQueue
