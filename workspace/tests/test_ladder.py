"""The cost ladder: layer 2 (re-apply and compare), layer 3 (continue the agent's conversation), per-task layers,
the per-layer statistics, and the command-line model adapter used to measure them with `claude -p`."""

import json
import re
import sys
import threading
import unittest

from cmws_ws.agents import (MergeQueue, Reply, ScriptedModel, StaticPlanner, Task, LoopWorker, WorkerResult, run_task)
from cmws_ws.agents.llm import CliModel, ModelError, Usage, call, parse_cli_output
from cmws_ws.agents.ladder import (FnReapplier, MAX_CHARS, ModelReapplier, Reapplied, build_prompt, extract_block,
                                   match_style, plausible, validate_layers)
from cmws_ws.agents.orchestrator import task_from_dict, task_to_dict
from cmws_ws.agents.planner import parse_tasks
from cmws_ws.checks import parse_checks
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository
from cmws_ws.serial import check_serializable

INC = parse_checks([{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}])


def bump_text(text):
    m = re.search(r"COUNT = (\d+)", text)
    return re.sub(r"COUNT = \d+", f"COUNT = {int(m.group(1)) + 1}", text, count=1)


# ---- pieces -------------------------------------------------------------------------------------------

class PromptAndParsingTests(unittest.TestCase):
    def test_the_prompt_has_the_task_the_change_the_current_file_and_the_question(self):
        p = build_prompt(Task("t", "Increase COUNT by 1"), "stats.py", "COUNT = 0\n", "COUNT = 1\n", "COUNT = 1\n")
        for needle in ("Increase COUNT by 1", "-COUNT = 0", "+COUNT = 1", "```\nCOUNT = 1\n```", "stats.py",
                       "complete file in one fenced code block"):
            self.assertIn(needle, p)
        self.assertNotIn("cut off", p)
        self.assertNotIn("yes or no", p.lower())                      # never asks "is your goal met?"

    def test_a_long_diff_is_cut(self):
        base = "".join(f"a{i}\n" for i in range(2000))
        mine = "".join(f"b{i}\n" for i in range(2000))
        p = build_prompt(Task("t", "x"), "f", base, mine, "m\n", max_diff=500)
        self.assertIn("[... cut off]", p)
        self.assertLess(len(p), 1500)

    def test_extract_block(self):
        self.assertEqual(extract_block("```\nA = 1\n```"), "A = 1\n")
        self.assertEqual(extract_block("Here:\n```python\nA = 1\nB = 2\n```\nDone."), "A = 1\nB = 2\n")
        self.assertEqual(extract_block("```\n```"), "")
        self.assertEqual(extract_block("```\n\n```"), "\n")
        self.assertIsNone(extract_block("A = 1"))
        self.assertIsNone(extract_block("```\nA = 1"))               # never closed
        self.assertIsNone(extract_block(""))
        self.assertEqual(extract_block("```md\ntext\n```inner```\nmore\n```"), "text\n```inner```\nmore\n")
        self.assertEqual(extract_block("```\r\nA\r\n```"), "A\r\n")

    def test_a_fenced_file_that_itself_contains_fences_comes_back_whole(self):
        reply = "```markdown\n# Title\n\n```python\nx = 1\n```\n\nEnd\n```"
        self.assertEqual(extract_block(reply), "# Title\n\n```python\nx = 1\n```\n\nEnd\n")

    def test_match_style(self):
        self.assertEqual(match_style("a\nb", "x\ny\n"), "a\nb\n")             # the final newline main has
        self.assertEqual(match_style("a\nb\n", "x\ny"), "a\nb")               # and the one main lacks
        self.assertEqual(match_style("a\nb\n", "x\r\ny\r\n"), "a\r\nb\r\n")  # CRLF restored
        self.assertEqual(match_style("a\r\nb\r\n", "x\r\ny\r\n"), "a\r\nb\r\n")
        self.assertEqual(match_style("", "x\n"), "")
        self.assertEqual(match_style("a", "x"), "a")
        self.assertEqual(match_style("a\r\nb", "x\r\ny\r\n"), "a\r\nb\r\n")

    def test_plausible_refuses_a_rewrite_of_the_whole_file(self):
        main = "".join(f"line {i}\n" for i in range(40))
        small = main.replace("line 3\n", "LINE 3\n")
        rewrite = "".join(f"other {i}\n" for i in range(40))
        self.assertTrue(plausible("a\n", "b\n", main, small))
        self.assertFalse(plausible("a\n", "b\n", main, rewrite))
        self.assertTrue(plausible("a\n", "b\n", main, main))                  # agreement is always plausible
        self.assertTrue(plausible("".join(f"x{i}\n" for i in range(30)), "".join(f"y{i}\n" for i in range(30)), main, rewrite))


class ReapplierTests(unittest.TestCase):
    task = Task("t", "bump")

    def test_a_good_answer_and_its_tokens(self):
        m = ScriptedModel([Reply("```\nCOUNT = 2\n```", (), Usage(120, 9))])
        out = ModelReapplier(m).reapply(self.task, "stats.py", "COUNT = 0\n", "COUNT = 1\n", "COUNT = 1\n")
        self.assertEqual((out.text, out.prompt_tokens, out.completion_tokens, out.tokens), ("COUNT = 2\n", 120, 9, 129))

    def test_no_code_block(self):
        out = ModelReapplier(ScriptedModel([Reply("COUNT = 2", (), Usage(5, 2))])).reapply(self.task, "f", "a", "b", "c")
        self.assertIsNone(out.text)
        self.assertIn("no code block", out.note)
        self.assertEqual(out.tokens, 7)

    def test_a_big_file_is_not_sent_to_the_model(self):
        m = ScriptedModel([])
        out = ModelReapplier(m).reapply(self.task, "f", "a", "b", "x" * (MAX_CHARS + 1))
        self.assertIsNone(out.text)
        self.assertEqual(m.calls, [])
        out = ModelReapplier(m).reapply(self.task, "f", "x" * (MAX_CHARS + 1), "b", "c")
        self.assertIsNone(out.text)

    def test_a_model_that_fails(self):
        out = ModelReapplier(ScriptedModel([])).reapply(self.task, "f", "a", "b", "c")        # script exhausted raises ModelError
        self.assertIsNone(out.text)
        self.assertIn("model failed", out.note)

    def test_fn_reapplier(self):
        r = FnReapplier(lambda task, path, main: main.upper())
        self.assertEqual(r.reapply(self.task, "f", "a", "b", "abc").text, "ABC")
        self.assertEqual(r.reapply(self.task, "f", "a", "b", "abc").tokens, 0)
        self.assertIsNone(FnReapplier(lambda *a: None).reapply(self.task, "f", "a", "b", "c").text)


class LayersDeclarationTests(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(validate_layers(None))
        self.assertEqual(validate_layers([]), ())
        self.assertEqual(validate_layers([4, 2]), (4, 2))
        self.assertEqual(validate_layers((2, 3, 4)), (2, 3, 4))

    def test_invalid(self):
        for bad in ([1], [5], [2, 2], ["2"], [True], [2.0], "234", 3, {2: 1}, [None]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_layers(bad)

    def test_travels_with_the_task(self):
        t = task_from_dict({"id": "a", "description": "d", "layers": [2, 4]})
        self.assertEqual(t.layers, (2, 4))
        self.assertEqual(task_from_dict(task_to_dict(t)), t)
        self.assertNotIn("layers", task_to_dict(task_from_dict({"id": "a", "description": "d"})))
        self.assertEqual(task_from_dict({"id": "a", "description": "d", "layers": []}).layers, ())
        with self.assertRaises(ValueError):
            task_from_dict({"id": "a", "description": "d", "layers": [9]})
        self.assertEqual(parse_tasks(json.dumps([{"id": "a", "description": "d", "layers": [3]}]))[0].layers, (3,))
        with self.assertRaises(ValueError):
            parse_tasks(json.dumps([{"id": "a", "description": "d", "layers": ["x"]}]))

    def test_queue_layers_are_checked(self):
        repo = Repository.init(MemoryKV(), {"f": b""})
        with self.assertRaises(ValueError):
            MergeQueue(repo, layers=(1,))
        self.assertEqual(MergeQueue(repo, layers=()).layers, ())


class CliModelTests(unittest.TestCase):
    def test_parse_json_with_usage(self):
        out = json.dumps({"result": "hello", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                                       "cache_read_input_tokens": 100, "output_tokens": 7}})
        r = parse_cli_output(out)
        self.assertEqual((r.content, r.usage.prompt_tokens, r.usage.completion_tokens), ("hello", 115, 7))

    def test_parse_variants(self):
        self.assertEqual(parse_cli_output("just text\n").content, "just text")
        self.assertEqual(parse_cli_output("just text\n").usage, Usage(0, 0))
        self.assertEqual(parse_cli_output("[1, 2]").content, "[1, 2]")             # JSON that is not an object is text
        self.assertEqual(parse_cli_output(json.dumps({"result": "x"})).usage, Usage(0, 0))
        bad = json.dumps({"result": "x", "usage": {"input_tokens": "9", "output_tokens": True, "cache_read_input_tokens": -4}})
        self.assertEqual(parse_cli_output(bad).usage, Usage(0, 0))               # strings, booleans and negatives are not counts
        self.assertEqual(parse_cli_output(json.dumps({"result": "x", "usage": "nope"})).usage, Usage(0, 0))
        with self.assertRaises(ModelError):
            parse_cli_output(json.dumps({"no": "result"}))
        with self.assertRaises(ModelError):
            parse_cli_output(json.dumps({"result": 5}))
        self.assertEqual(parse_cli_output("[" * 100000).content, "[" * 100000)   # absurd nesting does not crash

    def run_script(self, code, **kw):
        return CliModel([sys.executable, "-c", code, "{prompt}"], **kw).chat([{"role": "user", "content": "Q?"}], [])

    def test_chat_over_a_command(self):
        r = self.run_script("import sys, json; print(json.dumps({'result': 'got ' + sys.argv[1], 'usage': {'input_tokens': 3, 'output_tokens': 2}}))")
        self.assertEqual((r.content, r.usage), ("got Q?", Usage(3, 2)))

    def test_the_prompt_can_go_over_stdin(self):
        r = CliModel([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"]).chat(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "ignored"}], [])
        self.assertEqual(r.content, "S\n\nQ")

    def test_failures_are_model_errors(self):
        with self.assertRaises(ModelError):
            self.run_script("import sys; sys.stderr.write('boom'); sys.exit(3)")
        with self.assertRaises(ModelError):
            CliModel(["/nonexistent/claude-xyz", "{prompt}"]).chat([{"role": "user", "content": "q"}], [])
        with self.assertRaises(ModelError):
            self.run_script("import time; time.sleep(20)", timeout=0.3)

    def test_flatten_keeps_system_and_user_messages_only(self):
        self.assertEqual(CliModel.flatten([{"role": "system", "content": "a"}, {"role": "tool", "content": "t"},
                                           {"role": "user", "content": None}, {"role": "user", "content": "b"}]), "a\n\n\n\nb")


# ---- layer 2 in the queue --------------------------------------------------------------------------------

def two_branches(files, edits_a, edits_b):
    """Fork a and b from the same main; each applies its edits and records them as reads (as a folder agent's would be)."""
    repo = Repository.init(MemoryKV(), files)
    out = []
    for name, edits in (("a", edits_a), ("b", edits_b)):
        ws = repo.fork(name)
        for path, fn in edits.items():
            ws.write(path, fn(ws.read_text(path)))
        ws.commit(name)
        out.append(ws)
    return repo, out


def oracle(fn):
    return FnReapplier(lambda task, path, main: fn(main))


class LayerTwoTests(unittest.TestCase):
    def counter(self, reapply, accept=INC, **kw):
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, reapply=reapply, **kw)
        ta = Task("a", "bump", (), "", (), (), accept)
        tb = Task("b", "bump", (), "", (), (), accept)
        return repo, q, q.land(ta), q.land(tb)

    def test_the_identical_edit_is_resolved_at_layer_two_without_a_redo(self):
        repo, q, la, lb = self.counter(oracle(bump_text))
        self.assertTrue(la.landed)
        self.assertEqual((lb.status, lb.via), ("landed", "reapply"))
        self.assertEqual([(s.stage, s.status) for s in lb.steps], [("auto", "stale"), ("reapply", "land:fast-forward")])
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 2\n")
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 1, "tokens": 0})
        self.assertEqual(q.ladder_report()[4]["tried"], 0)

    def test_tokens_are_counted_per_layer_and_per_landing(self):
        class Costly:
            def reapply(self, task, path, base, mine, main):
                return Reapplied(bump_text(main), 100, 20)
        repo, q, la, lb = self.counter(Costly())
        self.assertEqual(lb.tokens, 120)
        self.assertEqual(la.tokens, 0)
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 1, "tokens": 120})

    def test_without_a_reapplier_the_stale_edit_goes_to_redo_as_before(self):
        repo, q, la, lb = self.counter(None, max_redos=0)
        self.assertEqual(lb.status, "escalated")
        self.assertEqual([s.stage for s in lb.steps], ["auto", "human"])

    def test_agreement_when_the_edit_really_is_the_same(self):
        repo, (wa, wb) = two_branches({"u.py": b"x = 1\n"}, {"u.py": lambda t: "x = 1;\n"}, {"u.py": lambda t: "x = 1;\n"})
        q = MergeQueue(repo, reapply=oracle(lambda main: main))
        self.assertTrue(q.land(Task("a", "semicolon")).landed)
        before = repo.head()
        lb = q.land(Task("b", "semicolon"))
        self.assertEqual((lb.status, lb.via), ("noop", "reapply"))
        self.assertEqual(repo.head(), before)                                    # nothing new on main
        self.assertEqual(lb.steps[-1].status, "agreement")

    def test_agreement_is_refused_when_the_task_s_own_check_does_not_hold_on_main(self):
        # a wrong "it is already done" answer for a counter: with an increases check it cannot become agreement
        repo, q, la, lb = self.counter(oracle(lambda main: main), max_redos=0)
        self.assertEqual(lb.status, "escalated")
        self.assertIn("agreement-refused", [s.status for s in lb.steps])
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 1\n")

    def test_a_wrong_agreement_with_no_check_is_a_silent_loss_the_documented_limit(self):
        repo, q, la, lb = self.counter(oracle(lambda main: main), accept=())
        self.assertEqual((lb.status, lb.via), ("noop", "reapply"))
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 1\n")        # one increment gone; only the model's answer said so

    def test_a_contains_check_that_holds_lets_the_agreement_stand(self):
        repo, (wa, wb) = two_branches({"u.py": b"x = 1\n"}, {"u.py": lambda t: "x = 1;\n"}, {"u.py": lambda t: "x = 1;\n"})
        acc = parse_checks([{"kind": "contains", "path": "u.py", "text": "x = 1;"}])
        q = MergeQueue(repo, reapply=oracle(lambda main: main))
        q.land(Task("a", "s", (), "", (), (), acc))
        lb = q.land(Task("b", "s", (), "", (), (), acc))
        self.assertEqual((lb.status, lb.via), ("noop", "reapply"))

    def test_an_answer_that_rewrites_the_whole_file_is_refused_and_the_task_redone(self):
        files = {"big.txt": "".join(f"line {i}\n" for i in range(60)).encode()}
        edit = lambda t: t.replace("line 5\n", "LINE 5\n")                      # noqa: E731
        repo, (wa, wb) = two_branches(files, {"big.txt": edit}, {"big.txt": edit})
        q = MergeQueue(repo, reapply=oracle(lambda main: "".join(f"x{i}\n" for i in range(60))), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertIn("implausible", [s.status for s in lb.steps])
        self.assertEqual(lb.status, "escalated")

    def test_no_answer_falls_through(self):
        repo, q, la, lb = self.counter(FnReapplier(lambda *a: None), max_redos=0)
        self.assertEqual([s.status for s in lb.steps][:2], ["stale", "no-answer"])
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 0, "tokens": 0})

    def test_a_file_the_agent_only_read_cannot_be_reapplied(self):
        repo = Repository.init(MemoryKV(), {"cfg.py": b"N = 1\n", "out.py": b""})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("cfg.py", "N = 2\n")
        wa.commit("a")
        wb.read_text("cfg.py")                                                   # b depends on it but did not change it
        wb.write("out.py", "x\n")
        wb.commit("b")
        q = MergeQueue(repo, reapply=oracle(lambda m: m), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.steps[1].status, "skipped")
        self.assertIn("did not change", lb.steps[1].detail)

    def test_a_listing_that_went_stale_cannot_be_reapplied(self):
        repo = Repository.init(MemoryKV(), {"d/a.txt": b"1\n"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("d/new.txt", "x\n")
        wa.commit("a")
        wb.list("d")
        wb.write("other.txt", "y\n")
        wb.commit("b")
        q = MergeQueue(repo, reapply=oracle(lambda m: m), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.steps[1].status, "skipped")

    def test_a_binary_file_is_skipped(self):
        # read_text on a binary file fails, so the branches are built by hand
        repo = Repository.init(MemoryKV(), {"b.bin": b"\xff\x00"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("b.bin", b"\xff\x01")
        wa.commit("a")
        wb.read("b.bin")
        wb.write("b.bin", b"\xff\x02")
        wb.commit("b")
        q = MergeQueue(repo, reapply=oracle(lambda m: m), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.steps[1].status, "skipped")
        self.assertIn("not text", lb.steps[1].detail)

    def test_conflicts_in_files_that_are_not_stale_stop_layer_two(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n", "w.txt": b"a\n"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("stats.py", "COUNT = 1\n")
        wa.write("w.txt", "A\n")
        wa.commit("a")
        wb.write("stats.py", wb.read_text("stats.py").replace("0", "1"))        # reads and changes stats.py
        wb.write("w.txt", "B\n")                                                 # writes w.txt blind: a conflict, not a stale read
        wb.commit("b")
        q = MergeQueue(repo, reapply=oracle(bump_text), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertIn("conflicts-elsewhere", [s.status for s in lb.steps])

    def test_several_stale_files_are_reapplied_together(self):
        files = {"a.py": b"N = 0\n", "b.py": b"M = 0\n"}
        inc = lambda var: (lambda t: re.sub(rf"{var} = (\d+)", lambda m: f"{var} = {int(m.group(1)) + 1}", t))   # noqa: E731
        both = {"a.py": inc("N"), "b.py": inc("M")}
        repo, (wa, wb) = two_branches(files, both, both)
        q = MergeQueue(repo, reapply=FnReapplier(lambda task, path, main: inc("N" if path == "a.py" else "M")(main)))
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual((lb.status, lb.via), ("landed", "reapply"))
        self.assertEqual(repo.main_files(), {"a.py": b"N = 2\n", "b.py": b"M = 2\n"})

    def test_one_file_agreement_and_another_re_applied(self):
        files = {"a.py": b"x = 1\n", "b.py": b"M = 0\n"}
        edits = {"a.py": lambda t: "x = 1;\n", "b.py": lambda t: t.replace("0", "1")}
        repo, (wa, wb) = two_branches(files, edits, edits)
        reapply = FnReapplier(lambda task, path, main: main if path == "a.py" else main.replace("1", "2"))
        q = MergeQueue(repo, reapply=reapply)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.via, "reapply")
        self.assertEqual(repo.main_files(), {"a.py": b"x = 1;\n", "b.py": b"M = 2\n"})
        self.assertIn("agreed on a.py", lb.steps[-1].detail)
        self.assertIn("re-applied b.py", lb.steps[-1].detail)

    def test_crlf_files_keep_their_line_endings(self):
        repo, (wa, wb) = two_branches({"s.txt": b"COUNT = 0\r\nend\r\n"}, {"s.txt": bump_text}, {"s.txt": bump_text})
        q = MergeQueue(repo, reapply=FnReapplier(lambda task, path, main: bump_text(main).replace("\r\n", "\n")))
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.via, "reapply")
        self.assertEqual(repo.main_files()["s.txt"], b"COUNT = 2\r\nend\r\n")

    def test_a_resolution_that_fails_validation_falls_through_to_redo(self):
        class Redo:
            name = "redo"

            def run(self, task, repo, agent_id):
                ws = repo.resume(agent_id)
                ws.write("stats.py", bump_text(ws.read_text("stats.py")))
                return WorkerResult("done", "redone", ws.commit(agent_id))
        from cmws_ws.agents import FnValidator, Verdict
        calls = []

        def validator(files, changed):
            calls.append(files["stats.py"])
            return Verdict(files["stats.py"] != b"COUNT = 666\n", "no 666")
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, FnValidator(validator), reapply=oracle(lambda m: "COUNT = 666\n"), redo_worker=Redo(), max_redos=1)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual((lb.status, lb.via), ("landed", "redo"))
        self.assertEqual([s.stage for s in lb.steps if s.stage != "redo"], ["auto", "reapply"])
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 0, "tokens": 0})
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 2\n")

    def test_the_per_task_layer_switch(self):
        for layers, expect in (((4,), "auto-only"), ((2,), "reapply"), ((), "auto-only"), ((3, 4), "auto-only")):
            repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
            q = MergeQueue(repo, reapply=oracle(bump_text), max_redos=0)
            q.land(Task("a", "x"))
            lb = q.land(Task("b", "x", (), "", (), (), (), layers))
            self.assertEqual(any(s.stage == "reapply" for s in lb.steps), expect == "reapply", layers)

    def test_the_queue_wide_layer_switch(self):
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, reapply=oracle(bump_text), layers=(4,), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertFalse(any(s.stage == "reapply" for s in lb.steps))
        self.assertEqual(q.ladder_report()[2]["tried"], 0)

    def test_layer_four_can_be_switched_off_for_a_task(self):
        class Redo:
            name = "redo"
            runs = 0

            def run(self, task, repo, agent_id):
                Redo.runs += 1
                return WorkerResult("done", "x", None)
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, redo_worker=Redo(), max_redos=2)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x", (), "", (), (), (), (2, 3)))
        self.assertEqual(Redo.runs, 0)
        self.assertEqual(lb.status, "escalated")

    def test_a_noop_through_agreement_still_counts_as_landed_work_for_the_task(self):
        repo, (wa, wb) = two_branches({"u.py": b"x = 1\n"}, {"u.py": lambda t: "x = 1;\n"}, {"u.py": lambda t: "x = 1;\n"})
        q = MergeQueue(repo, reapply=oracle(lambda m: m))
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertTrue(lb.status == "noop" and lb.commit is None and lb.via == "reapply")

    def test_the_gitlike_level_never_reaches_layer_two(self):
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, isolation="gitlike", reapply=oracle(bump_text))
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.steps[0].status, "merged")                            # no stale check, so nothing to re-apply


class ConcurrentLayerTwoTests(unittest.TestCase):
    def test_many_agents_bumping_one_counter_all_settle_at_layer_two(self):
        for _ in range(4):
            n = 6
            repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
            for i in range(n):
                ws = repo.fork(f"t{i}")
                ws.write("stats.py", bump_text(ws.read_text("stats.py")))
                ws.commit(f"t{i}")
            gate = threading.Barrier(n)
            out = {}

            def land(i):
                q = MergeQueue(repo, reapply=oracle(bump_text), max_redos=0)
                gate.wait()
                out[i] = q.land(Task(f"t{i}", "bump", (), "", (), (), INC))
            ts = [threading.Thread(target=land, args=(i,)) for i in range(n)]
            [t.start() for t in ts]
            [t.join(60) for t in ts]
            self.assertTrue(all(l.landed or l.status == "noop" for l in out.values()), {i: l.status for i, l in out.items()})
            self.assertEqual(repo.main_files()["stats.py"], f"COUNT = {n}\n".encode())
            edits = {f"t{i}": (lambda fs: fs.write("stats.py", bump_text(fs.read_text("stats.py")))) for i in range(n)}
            self.assertTrue(check_serializable(repo, {"stats.py": "COUNT = 0\n"}, edits).ok)


# ---- layer 3 -----------------------------------------------------------------------------------------------

def counter_model(final_value=None):
    """A scripted agent: read stats.py, write the bumped value, finish. With final_value it writes that instead."""
    def write(msgs):
        text = next(m["content"] for m in reversed(msgs) if m["role"] == "tool" and "COUNT" in m["content"])
        new = f"COUNT = {final_value}\n" if final_value is not None else bump_text(text)
        return Reply("", [call("write_file", "w", path="stats.py", content=new)])
    return [Reply("", [call("read_file", "r", path="stats.py")]), write, Reply("", [call("finish", "f", summary="bumped")])]


class LayerThreeTests(unittest.TestCase):
    def setup_race(self, second_model, **qkw):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        first = LoopWorker(ScriptedModel(counter_model()))
        second = LoopWorker(ScriptedModel(second_model))
        ra = run_task(first, repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=second, **qkw)
        return repo, q, ra, rb

    def test_the_conversation_continues_with_one_message_and_lands(self):
        model = ScriptedModel([Reply("", [call("read_file", "r2", path="stats.py")]),
                               lambda msgs: Reply("", [call("write_file", "w2", path="stats.py", content="COUNT = 2\n")]),
                               Reply("", [call("finish", "f2", summary="bumped again")])])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump", ("stats.py",)), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump", ("stats.py",)), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        self.assertTrue(q.land(Task("a", "bump"), ra, "a").landed)
        lb = q.land(Task("b", "bump"), rb, "b")
        self.assertEqual((lb.status, lb.via), ("landed", "continue"))
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 2\n")
        first_call = model.calls[0]
        self.assertEqual(first_call[0]["role"], "system")
        self.assertEqual(first_call[1]["role"], "user")                          # the original task prompt is still there ...
        self.assertTrue(any(m["role"] == "tool" for m in first_call))             # ... and what the agent did
        last = first_call[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("could not be landed", last["content"])
        self.assertIn("What landed on main", last["content"])
        self.assertEqual(q.ladder_report()[3]["settled"], 1)

    def test_conflict_markers_are_in_the_files_the_agent_resumes_on(self):
        seen = {}

        def look(msgs):
            return Reply("", [call("read_file", "r2", path="stats.py")])

        def write(msgs):
            seen["file"] = [m["content"] for m in msgs if m["role"] == "tool"][-1]
            return Reply("", [call("write_file", "w2", path="stats.py", content="COUNT = 7\n")])
        model = ScriptedModel([look, write, Reply("", [call("finish", "f2", summary="resolved")])])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model(5))), repo, Task("a", "x"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model(6))), repo, Task("b", "x"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        self.assertTrue(q.land(Task("a", "x"), ra, "a").landed)
        lb = q.land(Task("b", "x"), rb, "b")
        self.assertEqual(lb.via, "continue")
        self.assertIn("<<<<<<<", seen["file"])
        self.assertIn("conflict markers", model.calls[0][-1]["content"])
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 7\n")

    def test_no_transcript_means_no_layer_three(self):
        class Cli:
            name = "cli"

            def resume(self, *a):
                raise AssertionError("must not be called")
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        for name in ("a", "b"):
            ws = repo.fork(name)
            ws.read_text("stats.py")
            ws.write("stats.py", "COUNT = 1\n")
            ws.commit(name)
        res = WorkerResult("done", "x", repo.resume("b").head)                  # no transcript, like a command-line agent
        q = MergeQueue(repo, continue_worker=Cli(), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"), res, "b")
        self.assertEqual([s.stage for s in lb.steps], ["auto", "human"])

    def test_a_continuation_that_changes_nothing_falls_through(self):
        model = ScriptedModel([Reply("", [call("finish", "f2", summary="nothing to do")])])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        self.assertEqual(lb.status, "escalated")
        self.assertIn("continue", [s.stage for s in lb.steps])
        self.assertEqual(q.ladder_report()[3]["settled"], 0)

    def test_a_continuation_that_does_the_arithmetic_wrong_is_refused_by_the_check(self):
        model = ScriptedModel([Reply("", [call("write_file", "w2", path="stats.py", content="COUNT = 9\n")]),
                               Reply("", [call("finish", "f2", summary="done")])])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        task = Task("a", "bump", (), "", (), (), INC)
        q.land(task, ra, "a")
        lb = q.land(Task("b", "bump", (), "", (), (), INC), rb, "b")
        self.assertEqual(lb.status, "escalated")
        self.assertIn("acceptance check failed", [s.detail for s in lb.steps if s.stage == "continue"][0])
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 1\n")

    def test_the_order_is_reapply_then_continue_then_redo(self):
        class Redo:
            name = "redo"

            def run(self, task, repo, agent_id):
                ws = repo.resume(agent_id)
                ws.write("stats.py", bump_text(ws.read_text("stats.py")))
                return WorkerResult("done", "redone", ws.commit(agent_id))
        model = ScriptedModel([Reply("", [call("finish", "f2", summary="nothing")])])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, reapply=FnReapplier(lambda *a: None), continue_worker=LoopWorker(model), redo_worker=Redo(), max_redos=1)
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        self.assertEqual([s.stage for s in lb.steps][:4], ["auto", "reapply", "continue", "redo"])
        self.assertEqual((lb.status, lb.via), ("landed", "redo"))
        rep = q.ladder_report()
        self.assertEqual((rep[2]["settled"], rep[3]["settled"], rep[4]["tried"]), (0, 0, 1))

    def test_layer_three_can_be_switched_off(self):
        model = ScriptedModel([])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), layers=(2, 4), max_redos=0)
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        self.assertEqual(model.calls, [])
        self.assertEqual(lb.status, "escalated")

    def test_resume_without_a_note_still_gets_a_message(self):
        model = ScriptedModel([Reply("", [call("finish", "f", summary="ok")])])
        repo = Repository.init(MemoryKV(), {"a": b"1"})
        repo.fork("w")
        LoopWorker(model).resume(Task("t", "x"), repo, "w", [{"role": "system", "content": "s"}], None)
        self.assertEqual(model.calls[0][-1]["role"], "user")
        self.assertTrue(model.calls[0][-1]["content"])


if __name__ == "__main__":
    unittest.main()


class CliWorkerUsageTests(unittest.TestCase):
    """`claude -p --output-format json` reports its token use; CliWorker passes it on so layer costs can be compared."""

    def worker_result(self, argv):
        from cmws_ws.agents import CliWorker
        repo = Repository.init(MemoryKV(), {"a.txt": b"1\n"})
        return run_task(CliWorker(argv), repo, Task("t", "do it"), "t"), repo

    SCRIPT = ("import json; open('a.txt','w').write('2\\n'); "
              "print(json.dumps({'result': 'all done', 'usage': {'input_tokens': 11, 'cache_read_input_tokens': 100, 'output_tokens': 7}}))")

    def test_tokens_and_the_answer_text_come_from_the_json(self):
        res, repo = self.worker_result([sys.executable, "-c", self.SCRIPT, "--output-format", "json"])
        self.assertEqual((res.status, res.summary, res.prompt_tokens, res.completion_tokens), ("done", "all done", 111, 7))

    def test_without_the_flag_the_output_is_left_alone(self):
        res, repo = self.worker_result([sys.executable, "-c", self.SCRIPT])
        self.assertEqual((res.prompt_tokens, res.completion_tokens), (0, 0))
        self.assertIn('"result"', res.summary)

    def test_a_non_json_output_with_the_flag_still_works(self):
        res, repo = self.worker_result([sys.executable, "-c", "open('a.txt','w').write('3\\n'); print('plain words')", "--output-format", "json"])
        self.assertEqual((res.status, res.summary, res.prompt_tokens), ("done", "plain words", 0))

    def test_a_json_object_without_a_result_is_kept_as_text(self):
        res, repo = self.worker_result([sys.executable, "-c", "open('a.txt','w').write('3\\n'); print('{\"other\": 1}')", "--output-format", "json"])
        self.assertEqual(res.status, "done")
        self.assertIn("other", res.summary)

    def test_failing_commands_report_no_usage(self):
        res, repo = self.worker_result([sys.executable, "-c", "import sys; print('{}'); sys.exit(2)", "--output-format", "json"])
        self.assertEqual(res.status, "error")
        self.assertEqual(res.prompt_tokens, 0)


class LadderBookkeepingTests(unittest.TestCase):
    """Statistics and token sums per layer, and the exact reason each layer gives for stepping aside (found by a mutation sweep)."""

    def race(self, **kw):
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, **kw)
        q.land(Task("a", "x"))
        return repo, q

    def test_an_agreement_counts_as_settled(self):
        repo, (wa, wb) = two_branches({"u.py": b"x = 1\n"}, {"u.py": lambda t: "x = 1;\n"}, {"u.py": lambda t: "x = 1;\n"})
        q = MergeQueue(repo, reapply=FnReapplier(lambda t, p, m: m))
        q.land(Task("a", "x"))
        q.land(Task("b", "x"))
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 1, "tokens": 0})

    def test_every_way_layer_two_can_fail_is_counted_with_its_tokens(self):
        class Costly:
            def __init__(self, text):
                self.text = text

            def reapply(self, task, path, base, mine, main):
                return Reapplied(self.text, 30, 5)
        # an answer that rewrites the file
        files = {"big.txt": "".join(f"line {i}\n" for i in range(60)).encode()}
        edit = lambda t: t.replace("line 5\n", "LINE 5\n")                      # noqa: E731
        repo, (wa, wb) = two_branches(files, {"big.txt": edit}, {"big.txt": edit})
        q = MergeQueue(repo, reapply=Costly("".join(f"x{i}\n" for i in range(60))), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 0, "tokens": 35})
        self.assertEqual(lb.steps[1].tokens, 35)
        # agreement refused by the task's own check
        repo, q = self.race(reapply=Costly("COUNT = 1\n"), max_redos=0)
        lb = q.land(Task("b", "x", (), "", (), (), INC))
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 0, "tokens": 35})
        self.assertEqual([s.status for s in lb.steps][:2], ["stale", "agreement-refused"])
        self.assertIn("must increase by 1", lb.steps[1].detail)
        # an agreement that lands and one that is a plain re-application
        repo, q = self.race(reapply=Costly("COUNT = 2\n"), max_redos=0)
        lb = q.land(Task("b", "x", (), "", (), (), INC))
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 1, "tokens": 35})
        self.assertEqual(lb.tokens, 35)

    def test_conflicts_elsewhere_are_counted(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n", "w.txt": b"a\n"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("stats.py", "COUNT = 1\n")
        wa.write("w.txt", "A\n")
        wa.commit("a")
        wb.write("stats.py", wb.read_text("stats.py").replace("0", "1"))
        wb.write("w.txt", "B\n")
        wb.commit("b")
        q = MergeQueue(repo, reapply=FnReapplier(lambda t, p, m: bump_text(m)), max_redos=0)
        q.land(Task("a", "x"))
        q.land(Task("b", "x"))
        self.assertEqual(q.ladder_report()[2], {"tried": 1, "settled": 0, "tokens": 0})

    def test_a_listing_is_named_as_the_reason_and_a_deleted_file_too(self):
        repo = Repository.init(MemoryKV(), {"d/a.txt": b"1\n"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("d/new.txt", "x\n")
        wa.commit("a")
        wb.list("d")
        wb.write("other.txt", "y\n")
        wb.commit("b")
        q = MergeQueue(repo, reapply=FnReapplier(lambda t, p, m: m), max_redos=0)
        q.land(Task("a", "x"))
        self.assertIn("directory listing", q.land(Task("b", "x")).steps[1].detail)
        repo = Repository.init(MemoryKV(), {"f.txt": b"1\n"})
        wa, wb = repo.fork("a"), repo.fork("b")
        wa.write("f.txt", "2\n")
        wa.commit("a")
        wb.read_text("f.txt")
        wb.delete("f.txt")                                                       # the agent removed what main changed
        wb.commit("b")
        q = MergeQueue(repo, reapply=FnReapplier(lambda t, p, m: m), max_redos=0)
        q.land(Task("a", "x"))
        lb = q.land(Task("b", "x"))
        self.assertEqual(lb.steps[1].status, "skipped")
        self.assertIn("added or removed", lb.steps[1].detail)

    def test_checks_switched_off_let_a_wrong_agreement_stand(self):
        repo, q = self.race(reapply=FnReapplier(lambda t, p, m: m), accept_checks=False, max_redos=0)
        lb = q.land(Task("b", "x", (), "", (), (), INC))
        self.assertEqual((lb.status, lb.via), ("noop", "reapply"))

    def test_redo_tokens_are_counted_per_layer_and_per_landing(self):
        class Redo:
            name = "redo"

            def run(self, task, repo, agent_id):
                ws = repo.resume(agent_id)
                ws.write("stats.py", bump_text(ws.read_text("stats.py")))
                return WorkerResult("done", "redone", ws.commit(agent_id), (), 3, 400, 25)
        repo, q = self.race(redo_worker=Redo(), max_redos=1)
        lb = q.land(Task("b", "x"))
        self.assertEqual((lb.via, lb.tokens), ("redo", 425))
        self.assertEqual(q.ladder_report()[4], {"tried": 1, "settled": 1, "tokens": 425})

    def test_continuation_tokens_and_the_step_texts(self):
        model = ScriptedModel([Reply("", [call("write_file", "w", path="stats.py", content="COUNT = 2\n")], Usage(300, 20)),
                               Reply("", [call("finish", "f", summary="bumped on top")], Usage(320, 8))])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        self.assertEqual(lb.via, "continue")
        self.assertEqual(q.ladder_report()[3], {"tried": 1, "settled": 1, "tokens": 648})
        self.assertEqual(lb.tokens, 648)
        step = [s for s in lb.steps if s.stage == "continue"][0]
        self.assertIn("bumped on top", step.detail)
        self.assertEqual(step.tokens, 648)

    def test_an_empty_continuation_says_so_and_is_counted(self):
        model = ScriptedModel([Reply("", [call("finish", "f", summary="")], Usage(10, 1))])
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(model), max_redos=0)
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        step = [s for s in lb.steps if s.stage == "continue"][0]
        self.assertEqual((step.status, step.detail, step.tokens), ("empty", "the worker finished without changing anything", 11))
        self.assertEqual(q.ladder_report()[3], {"tried": 1, "settled": 0, "tokens": 11})

    def test_a_failed_continuation_model_is_counted(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ra = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("a", "bump"), "a")
        rb = run_task(LoopWorker(ScriptedModel(counter_model())), repo, Task("b", "bump"), "b")
        q = MergeQueue(repo, continue_worker=LoopWorker(ScriptedModel([])), max_redos=0)       # the model errors at once
        q.land(Task("a", "bump"), ra, "a")
        lb = q.land(Task("b", "bump"), rb, "b")
        step = [s for s in lb.steps if s.stage == "continue"][0]
        self.assertEqual(step.status, "error")
        self.assertEqual(q.ladder_report()[3]["settled"], 0)
        self.assertEqual(q.ladder_report()[3]["tried"], 1)


class RepeatedLayerTwoTests(unittest.TestCase):
    def test_a_main_that_moves_again_while_the_model_answers_is_followed(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        wss = []
        for i in range(3):
            ws = repo.fork(f"t{i}")
            ws.write("stats.py", bump_text(ws.read_text("stats.py")))
            ws.commit(f"t{i}")
            wss.append(ws)
        q = MergeQueue(repo, max_redos=0)
        q.land(Task("t0", "x"))
        moved = []

        def reapply(task, path, main):
            if not moved:                                                       # while the model "thinks", t1 lands first
                moved.append(1)
                other = MergeQueue(repo, reapply=FnReapplier(lambda t, p, m: bump_text(m)))
                assert other.land(Task("t1", "x")).landed
            return bump_text(main)
        q2 = MergeQueue(repo, reapply=FnReapplier(reapply), max_redos=0)
        lt = q2.land(Task("t2", "x"))
        self.assertEqual((lt.status, lt.via), ("landed", "reapply"))
        self.assertEqual([s.stage for s in lt.steps], ["auto", "reapply", "reapply"])
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 3\n")                  # t1 moved main meanwhile: no increment lost
        self.assertEqual(q2.ladder_report()[2]["tried"], 2)

    def test_the_number_of_repeats_is_bounded(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        ws = repo.fork("t")
        ws.write("stats.py", bump_text(ws.read_text("stats.py")))
        ws.commit("t")
        q0 = MergeQueue(repo)
        n = []

        def always_behind(task, path, main):
            n.append(1)
            o = repo.fork(f"o{len(n)}")                                         # somebody else lands before every answer is used
            o.write("stats.py", bump_text(o.read_text("stats.py")))
            o.commit("o")
            assert repo.submit(o).landed
            return bump_text(main)
        q = MergeQueue(repo, reapply=FnReapplier(always_behind), max_redos=0, max_reapply=2)
        # make t stale first
        other = repo.fork("first")
        other.write("stats.py", "COUNT = 5\n")
        other.commit("first")
        assert repo.submit(other).landed
        lt = q.land(Task("t", "x"))
        self.assertEqual(len(n), 2)
        self.assertEqual(lt.status, "escalated")


class ExtraRequestFieldsTests(unittest.TestCase):
    def test_extra_fields_reach_the_request(self):
        import http.server
        import threading
        from cmws_ws.agents import OpenAICompatModel
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/v1"
            OpenAICompatModel("m", url, extra={"reasoning_effort": "none"}).chat([{"role": "user", "content": "q"}], [])
            self.assertEqual(seen["body"]["reasoning_effort"], "none")
            self.assertEqual(seen["body"]["model"], "m")
            OpenAICompatModel("m", url).chat([{"role": "user", "content": "q"}], [])
            self.assertNotIn("reasoning_effort", seen["body"])
        finally:
            srv.shutdown()


class StarvationAndBackoffTests(unittest.TestCase):
    def race(self, queue_kw, reapply):
        repo, (wa, wb) = two_branches({"stats.py": b"COUNT = 0\n"}, {"stats.py": bump_text}, {"stats.py": bump_text})
        q = MergeQueue(repo, reapply=reapply, max_redos=0, **queue_kw)
        q.land(Task("a", "x"))
        return repo, q

    def test_a_task_that_keeps_losing_holds_the_queue_from_its_nth_attempt(self):
        owned = []
        outer = {}

        def answer(task, path, main):
            owned.append(outer["q"]._lock._is_owned())
            # make main move on, so that this answer is stale and the next attempt is needed
            o = outer["repo"].fork(f"o{len(owned)}")
            o.write("stats.py", bump_text(o.read_text("stats.py")))
            o.commit("o")
            assert outer["repo"].submit(o).landed
            return bump_text(main)
        repo, q = self.race(dict(max_reapply=4, starve_after=2), FnReapplier(answer))
        outer.update(q=q, repo=repo)
        q.land(Task("b", "x"))
        self.assertEqual(owned, [False, False, True, True])               # attempts 0 and 1 are free, 2 and 3 hold the queue

    def test_starve_after_is_configurable_and_zero_means_always(self):
        seen = []
        repo, q = self.race(dict(starve_after=0), FnReapplier(lambda t, p, m: (seen.append(q._lock._is_owned()), bump_text(m))[1]))
        q.land(Task("b", "x"))
        self.assertEqual(seen, [True])

    def test_backoff_waits_a_random_time_that_grows_and_never_before_the_first_try(self):
        from unittest import mock
        import cmws_ws.agents.mergequeue as mq
        repo = Repository.init(MemoryKV(), {"a": b"1"})
        q = MergeQueue(repo, backoff=0.01)
        waits = []
        with mock.patch.object(mq.time, "sleep", side_effect=waits.append), mock.patch.object(mq.random, "uniform", side_effect=lambda lo, hi: hi):
            for attempt in (0, 1, 2, 3, 6, 40):
                q._pause(attempt)
        self.assertEqual(waits, [0.02, 0.04, 0.08, 0.32, 0.32])          # nothing for attempt 0; doubles; capped at 2**5
        self.assertEqual(MergeQueue(repo)._pause(3), None)                # no backoff configured: no sleep at all

    def test_the_defaults_are_as_before(self):
        q = MergeQueue(Repository.init(MemoryKV(), {"a": b"1"}))
        self.assertEqual((q.max_reapply, q.starve_after, q.backoff), (4, 2, 0.0))
