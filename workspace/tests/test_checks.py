"""Transition checks: the declaration language (strict), every kind, commands, awkward inputs, the 'cannot fail'
lint, and how the checks travel with a task (JSON, planner, board) and act inside the merge queue."""

import json
import sys
import unittest

from cmws_ws.agents import MergeQueue, Reply, ScriptedModel, StaticPlanner, Task, merger_worker
from cmws_ws.agents.llm import call
from cmws_ws.agents.orchestrator import namespaced, task_from_dict, task_to_dict
from cmws_ws.agents.planner import parse_tasks
from cmws_ws.agents.graph import TaskGraph
from cmws_ws.checks import (KINDS, MAX_CHECKS, MAX_PATTERN, MAX_TEXT, Check, CheckError, check_to_dict, parse_check,
                            parse_checks, run_check, run_checks, unfalsifiable)
from cmws_ws.kv import MemoryKV
from cmws_ws.policy import PolicyError
from cmws_ws.repo import Repository


def B(**files):
    return {k.replace("_dot_", "."): (v.encode() if isinstance(v, str) else v) for k, v in files.items()}


INC = {"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)"}


class ParsingTests(unittest.TestCase):
    def test_every_kind_parses(self):
        for d in ({"kind": "contains", "path": "a", "text": "x"}, {"kind": "absent", "path": "a", "text": "x"},
                  {"kind": "contains", "path": "a", "text": "x+", "regex": True}, INC,
                  {"kind": "decreases", "path": "a", "pattern": r"(\d+)", "by": 3}, {"kind": "exists", "path": "a"},
                  {"kind": "missing", "path": "a"}, {"kind": "command", "argv": ["true"]},
                  {"kind": "command", "cmd": "python3 -c 'print(1)'"},
                  {"kind": "command", "argv": ["x"], "pattern": r"(\d+)", "by": -2, "timeout": 5}):
            self.assertIsInstance(parse_check(d), Check, d)

    def test_defaults(self):
        c = parse_check(INC)
        self.assertEqual((c.by, c.kind, c.path), (1, "increases", "stats.py"))
        self.assertEqual(parse_check({"kind": "command", "argv": ["a"]}).timeout, 120.0)

    def test_paths_are_normalised(self):
        self.assertEqual(parse_check({"kind": "exists", "path": "a//b/c"}).path, "a/b/c")
        self.assertEqual(parse_check({"kind": "exists", "path": "/abs.txt"}).path, "abs.txt")

    def test_not_an_object(self):
        for bad in (None, 5, "contains", ["contains"], True):
            with self.assertRaises(CheckError):
                parse_check(bad)

    def test_unknown_or_missing_kind(self):
        for d in ({}, {"kind": None}, {"kind": 3}, {"kind": "Contains", "path": "a", "text": "x"}, {"kind": "equals", "path": "a"}):
            with self.assertRaises(CheckError, msg=d):
                parse_check(d)

    def test_unknown_keys_are_refused_so_a_typo_cannot_weaken_a_check(self):
        with self.assertRaises(CheckError) as cm:
            parse_check({"kind": "increases", "path": "a", "pattern": r"(\d)", "bye": 2})
        self.assertIn("bye", str(cm.exception))
        with self.assertRaises(CheckError):
            parse_check({"kind": "exists", "path": "a", "text": "x"})                 # a key of another kind

    def test_path_problems(self):
        for p in (None, 5, "", "   ", "/", "../x", "a/../x", "a/./b\0"):
            with self.assertRaises(CheckError, msg=repr(p)):
                parse_check({"kind": "exists", "path": p})
        with self.assertRaises(CheckError):
            parse_check({"kind": "exists"})

    def test_contains_needs_text(self):
        for bad in ({"kind": "contains", "path": "a"}, {"kind": "contains", "path": "a", "text": ""},
                    {"kind": "contains", "path": "a", "text": 5}, {"kind": "contains", "path": "a", "text": "x" * (MAX_TEXT + 1)},
                    {"kind": "contains", "path": "a", "text": "x", "regex": "yes"},
                    {"kind": "absent", "path": "a", "text": "(", "regex": True}):
            with self.assertRaises(CheckError, msg=bad):
                parse_check(bad)

    def test_increases_pattern_rules(self):
        for pat in (None, "", 5, "(", "COUNT", "(a)(b)", "x" * (MAX_PATTERN + 1), "(?:\\d+)"):
            with self.assertRaises(CheckError, msg=repr(pat)):
                parse_check({"kind": "increases", "path": "a", "pattern": pat})

    def test_by_rules(self):
        for by in (0, -1, True, False, 1.5, "1", None, 10 ** 13):
            with self.assertRaises(CheckError, msg=repr(by)):
                parse_check({**INC, "by": by})
        self.assertEqual(parse_check({**INC, "by": 10 ** 12}).by, 10 ** 12)

    def test_command_rules(self):
        for bad in ({"kind": "command"}, {"kind": "command", "argv": []}, {"kind": "command", "argv": [""]},
                    {"kind": "command", "argv": [1]}, {"kind": "command", "argv": "ls"}, {"kind": "command", "argv": ["a\0"]},
                    {"kind": "command", "argv": ["a"], "cmd": "b"}, {"kind": "command", "cmd": ""},
                    {"kind": "command", "cmd": "a 'unclosed"}, {"kind": "command", "cmd": 5},
                    {"kind": "command", "argv": ["a"], "timeout": 0}, {"kind": "command", "argv": ["a"], "timeout": -3},
                    {"kind": "command", "argv": ["a"], "timeout": 99999}, {"kind": "command", "argv": ["a"], "timeout": True},
                    {"kind": "command", "argv": ["a"], "by": 2},              # by without a pattern
                    {"kind": "command", "argv": ["a"], "pattern": r"(\d+)", "by": 1.5}):
            with self.assertRaises(CheckError, msg=bad):
                parse_check(bad)

    def test_cmd_is_split_like_a_shell_would_without_running_one(self):
        c = parse_check({"kind": "command", "cmd": "python3 -c 'print(\"a b\")'"})
        self.assertEqual(c.argv, ("python3", "-c", 'print("a b")'))

    def test_list_level(self):
        self.assertEqual(parse_checks(None), ())
        self.assertEqual(parse_checks([]), ())
        self.assertEqual(len(parse_checks([INC, {"kind": "exists", "path": "a"}])), 2)
        for bad in ("exists a", {"kind": "exists"}, 5):
            with self.assertRaises(CheckError):
                parse_checks(bad)
        with self.assertRaises(CheckError):
            parse_checks([{"kind": "exists", "path": "a"}] * (MAX_CHECKS + 1))
        self.assertEqual(len(parse_checks([{"kind": "exists", "path": "a"}] * MAX_CHECKS)), MAX_CHECKS)

    def test_the_error_names_the_check(self):
        with self.assertRaises(CheckError) as cm:
            parse_checks([{"kind": "exists", "path": "a"}, {"kind": "nope"}])
        self.assertIn("accept[1]", str(cm.exception))

    def test_check_error_is_a_value_error(self):
        self.assertTrue(issubclass(CheckError, ValueError))

    def test_round_trip_through_dicts(self):
        for d in ({"kind": "contains", "path": "a", "text": "x", "regex": True}, {"kind": "absent", "path": "a", "text": "x"},
                  {**INC, "by": 4}, {"kind": "decreases", "path": "a", "pattern": r"(\d+)", "by": 1},
                  {"kind": "exists", "path": "a"}, {"kind": "missing", "path": "a"},
                  {"kind": "command", "argv": ["a", "b"], "pattern": r"(\d+)", "by": 2, "timeout": 9.0},
                  {"kind": "command", "argv": ["a"]}):
            c = parse_check(d)
            self.assertEqual(parse_check(check_to_dict(c)), c, d)
            json.dumps(check_to_dict(c))

    def test_describe_is_a_sentence_for_every_kind(self):
        for d in ({"kind": "contains", "path": "a", "text": "x"}, {"kind": "contains", "path": "a", "text": "x", "regex": True},
                  {"kind": "absent", "path": "a", "text": "x"}, INC, {"kind": "decreases", "path": "a", "pattern": r"(\d)"},
                  {"kind": "exists", "path": "a"}, {"kind": "missing", "path": "a"}, {"kind": "command", "argv": ["a"]},
                  {"kind": "command", "argv": ["a"], "pattern": r"(\d)", "by": 2}):
            self.assertGreaterEqual(len(parse_check(d).describe()), 8)
        self.assertIn("higher", parse_check(INC).describe())
        self.assertIn("lower", parse_check({"kind": "decreases", "path": "a", "pattern": r"(\d)"}).describe())

    def test_kinds_listed(self):
        self.assertEqual(set(KINDS), {"contains", "absent", "increases", "decreases", "exists", "missing", "command"})

    def test_checks_are_hashable_so_tasks_stay_hashable(self):
        t = Task("a", "d", (), "", (), (), parse_checks([INC]))
        hash(t)


class RunTests(unittest.TestCase):
    def ok(self, check, old, new):
        self.assertIsNone(run_check(parse_check(check), B(**old), B(**new)), check)

    def bad(self, check, old, new, *fragments):
        out = run_check(parse_check(check), B(**old), B(**new))
        self.assertIsNotNone(out, check)
        for f in fragments:
            self.assertIn(f, out)
        return out

    def test_contains(self):
        c = {"kind": "contains", "path": "f", "text": "needle"}
        self.ok(c, {}, {"f": "hay needle hay"})
        self.bad(c, {}, {"f": "hay"}, "f does not contain")
        self.bad(c, {}, {}, "does not exist")
        self.bad(c, {}, {"f": b"\xff\xfe"}, "not text")

    def test_contains_regex(self):
        c = {"kind": "contains", "path": "f", "text": r"^COUNT = \d+$", "regex": True}
        self.ok(c, {}, {"f": "a\nCOUNT = 12\nb"})
        self.bad(c, {}, {"f": "COUNT = x"})

    def test_contains_text_is_literal_without_regex(self):
        self.ok({"kind": "contains", "path": "f", "text": "a.b(c"}, {}, {"f": "xa.b(cx"})
        self.bad({"kind": "contains", "path": "f", "text": "a.b"}, {}, {"f": "axb"})

    def test_absent(self):
        c = {"kind": "absent", "path": "f", "text": "bad"}
        self.ok(c, {}, {"f": "good"})
        self.ok(c, {}, {})                                         # a missing file has nothing
        self.bad(c, {}, {"f": "very bad"}, "still contains")
        self.ok({"kind": "absent", "path": "f", "text": "b.d", "regex": True}, {}, {"f": "good"})
        self.bad({"kind": "absent", "path": "f", "text": "b.d", "regex": True}, {}, {"f": "bad"})

    def test_increases(self):
        self.ok(INC, {"stats_dot_py": "COUNT = 4\n"}, {"stats_dot_py": "COUNT = 5\n"})
        self.bad(INC, {"stats_dot_py": "COUNT = 4\n"}, {"stats_dot_py": "COUNT = 4\n"}, "must increase by 1", "main has 4", "gives 4")
        self.bad(INC, {"stats_dot_py": "COUNT = 4\n"}, {"stats_dot_py": "COUNT = 6\n"}, "expected 5")
        self.bad(INC, {"stats_dot_py": "COUNT = 4\n"}, {"stats_dot_py": "COUNT = 3\n"})

    def test_increases_by_more(self):
        c = {**INC, "by": 3}
        self.ok(c, {"stats_dot_py": "COUNT = 0\n"}, {"stats_dot_py": "COUNT = 3\n"})
        self.bad(c, {"stats_dot_py": "COUNT = 0\n"}, {"stats_dot_py": "COUNT = 1\n"})

    def test_decreases(self):
        c = {"kind": "decreases", "path": "f", "pattern": r"left = (\d+)", "by": 2}
        self.ok(c, {"f": "left = 9"}, {"f": "left = 7"})
        self.bad(c, {"f": "left = 9"}, {"f": "left = 9"}, "decrease by 2")

    def test_increases_across_a_digit_boundary_and_big_numbers(self):
        self.ok(INC, {"stats_dot_py": "COUNT = 9\n"}, {"stats_dot_py": "COUNT = 10\n"})
        big = 10 ** 30
        self.ok(INC, {"stats_dot_py": f"COUNT = {big}\n"}, {"stats_dot_py": f"COUNT = {big + 1}\n"})

    def test_increases_negative_numbers_are_not_captured_by_digits_only_patterns(self):
        c = {"kind": "increases", "path": "f", "pattern": r"v = (-?\d+)"}
        self.ok(c, {"f": "v = -3"}, {"f": "v = -2"})
        self.ok(c, {"f": "v = -1"}, {"f": "v = 0"})

    def test_increases_uses_the_first_match(self):
        c = {"kind": "increases", "path": "f", "pattern": r"N = (\d+)"}
        self.ok(c, {"f": "N = 1\nN = 100\n"}, {"f": "N = 2\nN = 7\n"})

    def test_increases_unreadable_values_fail_closed(self):
        self.bad(INC, {"stats_dot_py": "nothing\n"}, {"stats_dot_py": "COUNT = 1\n"}, "cannot read the old value")
        self.bad(INC, {"stats_dot_py": "COUNT = 1\n"}, {"stats_dot_py": "nothing\n"}, "cannot read the new value")
        self.bad(INC, {}, {"stats_dot_py": "COUNT = 1\n"}, "missing before")
        self.bad(INC, {"stats_dot_py": "COUNT = 1\n"}, {}, "missing after")

    def test_increases_with_a_non_numeric_capture(self):
        c = {"kind": "increases", "path": "f", "pattern": r"v = (\w+)"}
        self.bad(c, {"f": "v = abc"}, {"f": "v = abd"}, "not a whole number")

    def test_increases_on_binary_or_huge_files(self):
        self.bad(INC, {"stats_dot_py": b"\xff"}, {"stats_dot_py": "COUNT = 1\n"}, "not text")
        big = "x" * 5_000_000
        self.bad(INC, {"stats_dot_py": big + "COUNT = 1"}, {"stats_dot_py": "COUNT = 2"}, "larger than")

    def test_the_old_tree_is_main_before_not_the_fork_base(self):
        # a task that does `+1` is right against the main it lands on, whatever it saw when it started
        self.ok(INC, {"stats_dot_py": "COUNT = 41\n"}, {"stats_dot_py": "COUNT = 42\n"})

    def test_exists_and_missing(self):
        self.ok({"kind": "exists", "path": "f"}, {}, {"f": ""})
        self.bad({"kind": "exists", "path": "f"}, {"f": "x"}, {}, "does not exist")
        self.ok({"kind": "missing", "path": "f"}, {"f": "x"}, {})
        self.bad({"kind": "missing", "path": "f"}, {}, {"f": ""}, "still exists")

    def test_regex_that_explodes_is_reported_not_raised(self):
        c = Check("contains", "f", "x", True)                        # bypass validation to model a bad stored check
        c2 = Check("contains", "f", "(", True)
        self.assertIsNotNone(run_check(c2, {}, B(f="x")))
        self.assertIsNone(run_check(c, {}, B(f="x")))

    def test_run_checks_reports_every_failure(self):
        checks = parse_checks([{"kind": "exists", "path": "a"}, {"kind": "exists", "path": "b"}, {"kind": "missing", "path": "c"}])
        out = run_checks(checks, {}, B(c=""))
        self.assertEqual(len(out), 3)
        self.assertEqual(run_checks(checks, {}, B(a="", b="")), [])
        self.assertEqual(run_checks((), {}, {}), [])

    def test_crlf_files(self):
        self.ok({"kind": "contains", "path": "f", "text": "a\r\nb"}, {}, {"f": "a\r\nb\r\n"})
        self.ok({"kind": "increases", "path": "f", "pattern": r"n=(\d+)"}, {"f": "n=1\r\n"}, {"f": "n=2\r\n"})

    def test_unicode(self):
        self.ok({"kind": "contains", "path": "ü/ß.txt", "text": "日本語"}, {}, {"ü/ß.txt": "x 日本語 y"})

    def test_multiline_regex_anchors_work_per_line(self):
        self.ok({"kind": "contains", "path": "f", "text": r"^end$", "regex": True}, {}, {"f": "start\nend\nmore"})


class CommandTests(unittest.TestCase):
    PY = sys.executable

    def cmd(self, code, **kw):
        return parse_check({"kind": "command", "argv": [self.PY, "-c", code], **kw})

    def test_passes_on_exit_zero_in_a_folder_holding_the_result(self):
        c = self.cmd("import os, sys; sys.exit(0 if os.path.exists('f.txt') else 1)")
        self.assertIsNone(run_check(c, {}, B(f_dot_txt="x")))
        out = run_check(c, {}, B(g="y"))
        self.assertIn("failed on the result", out)

    def test_failure_output_is_included(self):
        c = self.cmd("import sys; print('oops message'); sys.exit(3)")
        self.assertIn("oops message", run_check(c, {}, {}))

    def test_old_and_new_folders_are_given_in_the_environment(self):
        c = self.cmd("import os; assert open(os.environ['AGENTTRUNK_OLD'] + '/f').read() == 'old'; "
                     "assert open(os.environ['AGENTTRUNK_NEW'] + '/f').read() == 'new'")
        self.assertIsNone(run_check(c, B(f="old"), B(f="new")))

    def test_output_number_must_change_by(self):
        code = "print('total', len(open('f').read()))"
        c = self.cmd(code, pattern=r"total (\d+)", by=2)
        self.assertIsNone(run_check(c, B(f="ab"), B(f="abcd")))
        out = run_check(c, B(f="ab"), B(f="abc"))
        self.assertIn("must change by 2", out)
        self.assertIn("was 2, now 3", out)

    def test_output_number_may_decrease_with_a_negative_by(self):
        c = self.cmd("print('n', len(open('f').read()))", pattern=r"n (\d+)", by=-1)
        self.assertIsNone(run_check(c, B(f="abc"), B(f="ab")))

    def test_output_without_the_number_fails(self):
        c = self.cmd("print('hello')", pattern=r"n (\d+)", by=1)
        self.assertIn("cannot read a number", run_check(c, {}, {}))

    def test_the_command_failing_on_the_old_tree_blocks_the_comparison(self):
        c = self.cmd("import sys; sys.exit(0 if open('f').read() == 'new' else 1); ", pattern=r"(\d+)", by=1)
        out = run_check(c, B(f="old"), B(f="new"))
        self.assertIn("previous main", out)

    def test_timeout(self):
        c = self.cmd("import time; time.sleep(30)", timeout=0.3)
        self.assertIn("timed out", run_check(c, {}, {}))

    def test_missing_program(self):
        c = parse_check({"kind": "command", "argv": ["/nonexistent/program-xyz"]})
        self.assertIn("cannot run", run_check(c, {}, {}))

    def test_nested_paths_are_written(self):
        c = self.cmd("import os, sys; sys.exit(0 if os.path.exists('a/b/c.txt') else 1)")
        self.assertIsNone(run_check(c, {}, B(**{"a/b/c.txt": "x"})))

    def test_large_output_is_truncated_in_the_message(self):
        c = self.cmd("import sys; print('x' * 100000); sys.exit(1)")
        self.assertLess(len(run_check(c, {}, {})), 2500)

    def test_a_shell_is_never_involved(self):
        c = parse_check({"kind": "command", "cmd": f"{self.PY} -c 'print(1)'; echo $HOME > /tmp/should-not-exist-agenttrunk"})
        run_check(c, {}, {})                                  # the ';' and '$HOME' are plain arguments to python3
        import os
        self.assertFalse(os.path.exists("/tmp/should-not-exist-agenttrunk"))


class LintTests(unittest.TestCase):
    FILES = B(stats_dot_py="COUNT = 3\n", reg_dot_py="X = 1\n")

    def test_a_check_that_already_holds_cannot_show_the_work(self):
        out = unfalsifiable(parse_checks([{"kind": "contains", "path": "reg.py", "text": "X = 1"}]), self.FILES)
        self.assertEqual(len(out), 1)
        self.assertIn("already true", out[0])

    def test_falsifiable_checks_are_silent(self):
        checks = parse_checks([INC, {"kind": "contains", "path": "reg.py", "text": "Y = 2"}, {"kind": "exists", "path": "new.py"},
                               {"kind": "missing", "path": "reg.py"}, {"kind": "absent", "path": "reg.py", "text": "X = 1"}])
        self.assertEqual(unfalsifiable(checks, self.FILES), [])

    def test_trivially_true_checks_are_reported(self):
        checks = parse_checks([{"kind": "exists", "path": "reg.py"}, {"kind": "missing", "path": "nope.py"},
                               {"kind": "absent", "path": "reg.py", "text": "NEVER"},
                               {"kind": "command", "argv": [sys.executable, "-c", "pass"]}])
        self.assertEqual(len(unfalsifiable(checks, self.FILES)), 4)

    def test_a_command_with_a_counter_is_falsifiable(self):
        c = parse_check({"kind": "command", "argv": [sys.executable, "-c", "print('n 1')"], "pattern": r"n (\d+)", "by": 1})
        self.assertEqual(unfalsifiable([c], self.FILES), [])

    def test_empty(self):
        self.assertEqual(unfalsifiable([], self.FILES), [])


class TaskTravelTests(unittest.TestCase):
    D = {"id": "t1", "description": "bump", "paths": ["stats.py"], "accept": [INC], "merge": {"reg.py": "append"}}

    def test_task_from_dict_and_back(self):
        t = task_from_dict(self.D)
        self.assertEqual(t.merge, (("reg.py", "append"),))
        self.assertEqual(t.accept[0].kind, "increases")
        d = task_to_dict(t)
        self.assertEqual(task_from_dict(d), t)
        json.dumps(d)

    def test_tasks_without_either_field_serialise_as_before(self):
        d = task_to_dict(Task("a", "b"))
        self.assertEqual(set(d), {"id", "description", "paths", "context", "depends_on"})

    def test_bad_declarations_in_a_task_file_raise_value_errors(self):
        with self.assertRaises(CheckError):
            task_from_dict({**self.D, "accept": [{"kind": "nope"}]})
        with self.assertRaises(PolicyError):
            task_from_dict({**self.D, "merge": {"a": "sometimes"}})

    def test_namespacing_keeps_the_rules(self):
        g = namespaced("run1", TaskGraph([task_from_dict(self.D), Task("t2", "x", (), "", ("t1",))]))
        t = g.by_id["run1-t1"]
        self.assertEqual(len(t.accept), 1)
        self.assertEqual(t.merge, (("reg.py", "append"),))

    def test_planner_parses_accept_and_merge(self):
        text = json.dumps([{"id": "a", "description": "d", "accept": [INC], "merge": {"x.py": "converge"}}])
        t = parse_tasks(text)[0]
        self.assertEqual(t.accept[0].by, 1)
        self.assertEqual(t.merge, (("x.py", "converge"),))

    def test_planner_rejects_bad_declarations_with_a_value_error(self):
        for bad in ({"accept": [{"kind": "nope"}]}, {"merge": {"x": "sometimes"}}, {"accept": "x"}):
            with self.assertRaises(ValueError):
                parse_tasks(json.dumps([{"id": "a", "description": "d", **bad}]))

    def test_the_task_prompt_tells_the_agent_what_will_be_checked(self):
        from cmws_ws.agents.worker import task_prompt
        t = task_from_dict(self.D)
        import cmws_ws.agents.worker as w
        self.assertNotIn("accepted only if", task_prompt(t))              # off by default
        w.TELL_CHECKS = True
        try:
            p = task_prompt(t)
        finally:
            w.TELL_CHECKS = False
        self.assertIn("accepted only if", p)
        self.assertIn("exactly 1 higher", p)
        self.assertNotIn("accepted only if", task_prompt(Task("a", "b")))


class QueueTests(unittest.TestCase):
    """The checks inside the real merge queue."""

    def setUp(self):
        self.repo = Repository.init(MemoryKV(), B(stats_dot_py="COUNT = 0\n"))
        self.task = lambda name: Task(name, "bump", (), "", (), (), parse_checks([INC]))

    def branch(self, name, value):
        ws = self.repo.fork(name)
        ws.write("stats.py", f"COUNT = {value}\n")
        ws.commit(name)

    def test_identical_edits_are_refused_by_the_transition_check_with_no_model(self):
        self.branch("a", 1)
        self.branch("b", 1)                                  # same base, same edit
        q = MergeQueue(self.repo, isolation="gitlike")
        self.assertTrue(q.land(self.task("a")).landed)
        lb = q.land(self.task("b"))
        self.assertEqual(lb.status, "escalated")
        self.assertEqual(lb.steps[0].status, "rejected")
        self.assertIn("must increase by 1", lb.steps[0].detail)
        self.assertEqual(self.repo.main_files()["stats.py"], b"COUNT = 1\n")

    def test_the_agent_that_got_it_right_lands(self):
        self.branch("a", 1)
        self.branch("b", 2)
        q = MergeQueue(self.repo, isolation="gitlike", max_redos=0)
        self.assertTrue(q.land(self.task("a")).landed)
        lb = q.land(self.task("b"))
        self.assertIn(lb.status, ("landed", "escalated"))                          # 2 vs 1 is a text conflict; a person decides
        # the merged text conflicts, so it is never checked as a clean merge
        self.assertEqual(self.repo.main_files()["stats.py"], b"COUNT = 1\n")

    def test_accept_checks_can_be_switched_off(self):
        self.branch("a", 1)
        self.branch("b", 1)
        q = MergeQueue(self.repo, isolation="gitlike", accept_checks=False)
        self.assertTrue(q.land(self.task("a")).landed)
        self.assertTrue(q.land(self.task("b")).landed)                             # the silent lost update, reproduced

    def test_a_redo_is_judged_against_the_main_it_lands_on(self):
        self.branch("a", 1)
        self.branch("b", 1)

        class Redo:
            name = "redo"

            def run(self, task, repo, agent_id):
                from cmws_ws.agents import WorkerResult
                ws = repo.resume(agent_id)
                n = int(ws.read_text("stats.py").split("=")[1]) + 1
                ws.write("stats.py", f"COUNT = {n}\n")
                return WorkerResult("done", "redone", ws.commit(task.id))
        q = MergeQueue(self.repo, isolation="gitlike", redo_worker=Redo(), max_redos=1)
        self.assertTrue(q.land(self.task("a")).landed)
        lb = q.land(self.task("b"))
        self.assertEqual((lb.status, lb.via), ("landed", "redo"))
        self.assertEqual(self.repo.main_files()["stats.py"], b"COUNT = 2\n")

    def test_no_check_declared_means_no_change_in_behaviour(self):
        self.branch("a", 1)
        self.branch("b", 1)
        q = MergeQueue(self.repo, isolation="gitlike")
        self.assertTrue(q.land(Task("a", "x")).landed)
        self.assertTrue(q.land(Task("b", "x")).landed)

    def test_for_task_scopes_the_task_to_the_calling_thread(self):
        self.branch("a", 1)
        self.branch("b", 1)
        q = MergeQueue(self.repo, isolation="gitlike")
        with q.for_task(self.task("a")):
            self.assertTrue(q.attempt("a").landed)
        with q.for_task(self.task("b")):
            r = q.attempt("b")
            self.assertEqual(r.status, "rejected")
        self.assertIsNone(getattr(q._ctx, "task", None))

    def test_attempt_with_a_task_argument(self):
        self.branch("a", 1)
        self.branch("b", 1)
        q = MergeQueue(self.repo, isolation="gitlike")
        self.assertTrue(q.attempt("a", task=self.task("a")).landed)
        self.assertEqual(q.attempt("b", task=self.task("b")).status, "rejected")

    def test_unknown_isolation_is_refused(self):
        with self.assertRaises(ValueError):
            MergeQueue(self.repo, isolation="serializable")

    def test_snapshot_isolation_refuses_the_same_edit_without_any_check(self):
        wa, wb = self.repo.fork("a"), self.repo.fork("b")
        for ws in (wa, wb):
            n = int(ws.read_text("stats.py").split("=")[1]) + 1       # reads are recorded
            ws.write("stats.py", f"COUNT = {n}\n")
            ws.commit(ws.agent_id)
        q = MergeQueue(self.repo)
        self.assertTrue(q.land(Task("a", "x")).landed)
        r = q.attempt("b")
        self.assertEqual(r.status, "stale")


if __name__ == "__main__":
    unittest.main()
