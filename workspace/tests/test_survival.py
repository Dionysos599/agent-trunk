"""The survival check: unit cases, awkward inputs, a property that clean merges never trip it, and its use inside the
merge queue (a merger that drops one side) and the MCP service (an agent that drops main's lines while resolving)."""

import random
import unittest

from cmws_ws.agents import MergeQueue, Reply, ScriptedModel, Task, merger_worker
from cmws_ws.agents.llm import call
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository
from cmws_ws.survival import MAX_BYTES, MAX_REPORTED, survival_problems
from cmws_ws.textmerge import merge3, split_lines


def P(base, mine, main, result, **kw):
    enc = lambda d: {k: (v.encode() if isinstance(v, str) else v) for k, v in d.items()}      # noqa: E731
    return survival_problems(enc(base), enc(mine), enc(main), enc(result), **kw)


class UnitTests(unittest.TestCase):
    def test_everything_survived(self):
        self.assertEqual(P({"f": "a\n"}, {"f": "a\nO\n"}, {"f": "a\nT\n"}, {"f": "a\nT\nO\n"}), [])

    def test_a_dropped_line_of_the_branch(self):
        out = P({"f": "a\n"}, {"f": "a\nO\n"}, {"f": "a\nT\n"}, {"f": "a\nT\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("f: a line added by the branch is missing", out[0])
        self.assertIn("'O'", out[0])

    def test_a_dropped_line_of_main(self):
        out = P({"f": "a\n"}, {"f": "a\nO\n"}, {"f": "a\nT\n"}, {"f": "a\nO\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("added by main", out[0])

    def test_names_can_be_given(self):
        out = P({"f": "a\n"}, {"f": "a\nO\n"}, {"f": "a\nT\n"}, {"f": "a\n"}, mine_name="agent 7", main_name="the trunk")
        self.assertTrue(any("agent 7" in o for o in out) and any("the trunk" in o for o in out))

    def test_modified_lines_are_not_judged(self):
        base, mine, main = {"f": "COUNT = 0\n"}, {"f": "COUNT = 1\n"}, {"f": "COUNT = 1\n"}
        self.assertEqual(P(base, mine, main, {"f": "COUNT = 1\n"}), [])          # the counter case: invisible to this check
        self.assertEqual(P(base, mine, main, {"f": "COUNT = 2\n"}), [])          # a correct resolution matches neither side
        self.assertEqual(P(base, mine, main, {"f": "garbage\n"}), [])            # replace hunks are the transition checks' job

    def test_a_removed_line_must_stay_removed(self):
        base = {"f": "a\nb\nc\n"}
        self.assertEqual(P(base, {"f": "a\nc\n"}, {"f": "a\nb\nc\n"}, {"f": "a\nc\n"}), [])
        out = P(base, {"f": "a\nc\n"}, {"f": "a\nb\nc\nd\n"}, {"f": "a\nb\nc\nd\n"})
        self.assertTrue(any("removed by the branch is back" in o and "'b'" in o for o in out), out)

    def test_main_removed_lines_must_stay_removed(self):
        out = P({"f": "a\nb\n"}, {"f": "a\nb\nX\n"}, {"f": "a\n"}, {"f": "a\nb\nX\n"})
        self.assertTrue(any("removed by main is back" in o for o in out), out)

    def test_both_removing_the_same_line_is_agreement(self):
        self.assertEqual(P({"f": "a\nb\n"}, {"f": "a\n"}, {"f": "a\n"}, {"f": "a\n"}), [])

    def test_removing_one_of_two_identical_lines_counts_copies(self):
        base = {"f": "x\nm\nx\n"}
        self.assertEqual(P(base, {"f": "m\nx\n"}, base, {"f": "m\nx\n"}), [])
        out = P(base, {"f": "m\nx\n"}, base, {"f": "x\nm\nx\n"})            # the removal was undone
        self.assertTrue(out)

    def test_a_line_that_came_back_through_the_other_side_is_not_flagged(self):
        # the branch deleted 'b'; main replaced a neighbouring line with 'b' again
        self.assertEqual(P({"f": "a\nb\nc\n"}, {"f": "a\nc\n"}, {"f": "a\nb\nb\n"}, {"f": "a\nb\nb\n"}), [])

    def test_identical_insertions_survive_once(self):
        self.assertEqual(P({"f": "a\n"}, {"f": "a\nS\n"}, {"f": "a\nS\n"}, {"f": "a\nS\n"}), [])

    def test_blank_lines_and_indentation_are_not_judged(self):
        self.assertEqual(P({"f": "a\n"}, {"f": "a\n\n\n"}, {"f": "a\n"}, {"f": "a\n"}), [])
        self.assertEqual(P({"f": "a\n"}, {"f": "a\n    x\n"}, {"f": "a\n"}, {"f": "a\nx\n"}), [])
        self.assertEqual(P({"f": "a\n"}, {"f": "a\nx \n"}, {"f": "a\n"}, {"f": "a\nx\n"}), [])

    def test_crlf(self):
        self.assertEqual(P({"f": "a\r\n"}, {"f": "a\r\nO\r\n"}, {"f": "a\r\nT\r\n"}, {"f": "a\r\nT\r\nO\r\n"}), [])
        self.assertTrue(P({"f": "a\r\n"}, {"f": "a\r\nO\r\n"}, {"f": "a\r\nT\r\n"}, {"f": "a\r\nT\r\n"}))

    def test_no_final_newline(self):
        self.assertEqual(P({"f": "a"}, {"f": "a\nO"}, {"f": "a"}, {"f": "a\nO"}), [])

    def test_a_new_file_the_result_lacks(self):
        out = P({}, {"n.txt": "one\ntwo\n"}, {}, {})
        self.assertTrue(any("one" in o for o in out))

    def test_a_new_file_is_found_under_another_name(self):
        self.assertEqual(P({}, {"n.txt": "one\ntwo\n"}, {}, {"moved/n.txt": "one\ntwo\n"}), [])

    def test_missing_result_file_with_lines_elsewhere(self):
        self.assertEqual(P({"f": "a\n"}, {"f": "a\nO\n"}, {"f": "a\n"}, {"g": "a\nO\n"}), [])

    def test_a_file_deleted_by_both_is_not_judged(self):
        self.assertEqual(P({"f": "a\n"}, {}, {}, {}), [])

    def test_a_file_deleted_by_one_side_and_unchanged_by_the_other_is_not_judged(self):
        self.assertEqual(P({"f": "a\n"}, {}, {"f": "a\n"}, {}), [])

    def test_binary_and_huge_files_are_skipped(self):
        self.assertEqual(P({"b": b"\xff"}, {"b": b"\xff\xfe"}, {"b": b"\xff"}, {"b": b"\xff"}), [])
        big = "x\n" * (MAX_BYTES // 2 + 10)
        self.assertEqual(P({"f": big}, {"f": big + "O\n"}, {"f": big}, {"f": big}), [])

    def test_nothing_changed(self):
        self.assertEqual(P({"f": "a\n"}, {"f": "a\n"}, {"f": "a\n"}, {"f": "a\n"}), [])
        self.assertEqual(P({}, {}, {}, {}), [])

    def test_unrelated_files_are_not_looked_at(self):
        self.assertEqual(P({"f": "a\n", "g": "g\n"}, {"f": "a\nO\n", "g": "g\n"}, {"f": "a\n", "g": "g\n"},
                           {"f": "a\nO\n", "g": "LOST\n"}), [])

    def test_the_number_of_problems_is_capped(self):
        mine = {"f": "".join(f"O{i}\n" for i in range(50))}
        out = P({"f": ""}, mine, {"f": ""}, {"f": ""})
        self.assertEqual(len(out), MAX_REPORTED + 1)
        self.assertEqual(out[-1], "... and more")

    def test_long_lines_are_shortened_in_the_message(self):
        out = P({"f": ""}, {"f": "L" * 500 + "\n"}, {"f": ""}, {"f": ""})
        self.assertLess(len(out[0]), 200)

    def test_unicode(self):
        self.assertEqual(P({"f": "α\n"}, {"f": "α\nβ\n"}, {"f": "α\nγ\n"}, {"f": "α\nγ\nβ\n"}), [])
        self.assertTrue(P({"f": "α\n"}, {"f": "α\nβ\n"}, {"f": "α\nγ\n"}, {"f": "α\nγ\n"}))

    def test_paths_with_directories(self):
        out = P({"a/b/c.txt": "x\n"}, {"a/b/c.txt": "x\nO\n"}, {"a/b/c.txt": "x\n"}, {"a/b/c.txt": "x\n"})
        self.assertTrue(out[0].startswith("a/b/c.txt:"))

    def test_a_merger_that_rewrote_both_sides_into_one_combined_block_passes_only_if_lines_remain(self):
        base = {"f": "h\nt\n"}
        mine, main = {"f": "h\nOURS\nt\n"}, {"f": "h\nTHEIRS\nt\n"}
        self.assertEqual(P(base, mine, main, {"f": "h\nTHEIRS\nOURS\nt\n"}), [])
        self.assertTrue(P(base, mine, main, {"f": "h\nTHEIRS+OURS\nt\n"}))          # merged into a new line: refused, redo


class CleanMergesNeverTripIt(unittest.TestCase):
    """The check must have no false refusals on what the merge engine accepts on its own."""

    @staticmethod
    def mutate(rng, lines, alphabet):
        out = list(lines)
        for _ in range(rng.randint(0, 4)):
            op = rng.choice(["ins", "ins", "del", "mod", "dup"])
            if op == "ins" or not out:
                out.insert(rng.randint(0, len(out)), f"{rng.choice(alphabet)}\n")
            elif op == "del":
                del out[rng.randrange(len(out))]
            elif op == "dup":
                i = rng.randrange(len(out))
                out.insert(i, out[i])
            else:
                out[rng.randrange(len(out))] = f"{rng.choice(alphabet)}\n"
        return out

    def test_random_clean_merges(self):
        rng = random.Random(31337)
        checked = 0
        for _ in range(3000):
            alphabet = rng.choice(["ab", "abc", "abcdef", ["x", "y", "", "  x", "}"]])
            base = [f"{rng.choice(alphabet)}\n" for _ in range(rng.randint(0, 9))]
            ours, theirs = self.mutate(rng, base, alphabet), self.mutate(rng, base, alphabet)
            for append in (False, True):
                r = merge3(base, ours, theirs, append=append)
                if not r.clean:
                    continue
                checked += 1
                got = survival_problems({"f": "".join(base).encode()}, {"f": "".join(ours).encode()},
                                        {"f": "".join(theirs).encode()}, {"f": "".join(r.lines).encode()})
                self.assertEqual(got, [], (base, ours, theirs, r.lines, append))
        self.assertGreater(checked, 1000)

    def test_dropping_a_pure_insertion_is_always_caught(self):
        rng = random.Random(8)
        caught = 0
        for _ in range(300):
            base = [f"b{i}\n" for i in range(rng.randint(1, 6))]
            pos = rng.randint(0, len(base))
            mine_ins = f"MINE{rng.randint(0, 99)}\n"
            main_ins = f"MAIN{rng.randint(0, 99)}\n"
            mine = base[:pos] + [mine_ins] + base[pos:]
            main = base[:pos] + [main_ins] + base[pos:]
            sloppy_keep_main = "".join(main).encode()
            got = survival_problems({"f": "".join(base).encode()}, {"f": "".join(mine).encode()},
                                    {"f": "".join(main).encode()}, {"f": sloppy_keep_main})
            self.assertTrue(any("added by the branch" in g for g in got), got)
            caught += 1
        self.assertEqual(caught, 300)


# ---- inside the merge queue ----------------------------------------------------------------------------

def edit(repo, name, **files):
    ws = repo.fork(name)
    for path, text in files.items():
        ws.write(path.replace("_dot_", "."), text)
    ws.commit(f"{name} work")
    return Task(name, f"task {name}")


class QueueSurvivalTests(unittest.TestCase):
    BASE = {"a.txt": b"h\nv=1\nmid\nt\n"}

    def setUp(self):
        self.repo = Repository.init(MemoryKV(), self.BASE)
        self.main_task = edit(self.repo, "m", **{"a_dot_txt": "h\nv=2\nmid\nMAIN-LINE\nt\n"})
        self.branch_task = edit(self.repo, "b", **{"a_dot_txt": "h\nv=3\nmid\nt\nBRANCH-LINE\n"})

    def merger(self, resolved):
        model = ScriptedModel([Reply("", [call("write_file", "1", path="a.txt", content=resolved)]),
                               Reply("", [call("finish", "2", summary="merged")])])
        return merger_worker(model)

    def redo(self):
        class Redo:
            name = "redo"

            def run(self, task, repo, agent_id):
                from cmws_ws.agents import WorkerResult
                ws = repo.resume(agent_id)
                ws.write("a.txt", "h\nv=3\nmid\nMAIN-LINE\nt\nBRANCH-LINE\n")
                return WorkerResult("done", "redone", ws.commit(task.id))
        return Redo()

    def land_both(self, resolved, **kw):
        q = MergeQueue(self.repo, merger=self.merger(resolved), redo_worker=self.redo(), max_redos=1, **kw)
        self.assertTrue(q.land(self.main_task).landed)
        return q, q.land(self.branch_task)

    def test_a_resolution_that_keeps_both_sides_lines_lands(self):
        q, lb = self.land_both("h\nv=5\nmid\nMAIN-LINE\nt\nBRANCH-LINE\n")
        self.assertEqual((lb.status, lb.via), ("landed", "merger"))

    def test_a_resolution_that_drops_main_s_line_is_refused_and_redone(self):
        q, lb = self.land_both("h\nv=5\nmid\nt\nBRANCH-LINE\n")
        stages = [(s.stage, s.status) for s in lb.steps]
        self.assertIn(("merger", "discarded-side"), stages)
        self.assertIn("MAIN-LINE", [s.detail for s in lb.steps if s.status == "discarded-side"][0])
        self.assertEqual((lb.status, lb.via), ("landed", "redo"))
        self.assertIn(b"MAIN-LINE", self.repo.main_files()["a.txt"])
        self.assertIn(b"BRANCH-LINE", self.repo.main_files()["a.txt"])

    def test_a_resolution_that_drops_the_branch_s_line_is_refused(self):
        q, lb = self.land_both("h\nv=5\nmid\nMAIN-LINE\nt\n")
        self.assertIn(("merger", "discarded-side"), [(s.stage, s.status) for s in lb.steps])

    def test_with_the_guard_off_the_drop_lands_silently(self):
        q, lb = self.land_both("h\nv=5\nmid\nt\nBRANCH-LINE\n", keep_both_sides=False)
        self.assertEqual((lb.status, lb.via), ("landed", "merger"))
        self.assertNotIn(b"MAIN-LINE", self.repo.main_files()["a.txt"])

    def test_with_survival_off_only_the_whole_file_guard_remains(self):
        q, lb = self.land_both("h\nv=5\nmid\nt\nBRANCH-LINE\n", survival=False)
        self.assertEqual((lb.status, lb.via), ("landed", "merger"))                 # not equal to either side's file: unseen

    def test_after_a_validation_failure_the_merger_may_rewrite_any_line(self):
        repo = Repository.init(MemoryKV(), {"x.py": b"def helper():\n    return 1\n"})
        ta = edit(repo, "a", **{"x_dot_py": "def helper():\n    return 1\n# a\n"})
        tb = edit(repo, "b", **{"y_dot_py": "def helper():\n    return 2\n"})
        from cmws_ws.agents import FnValidator, Verdict

        def no_dup(files, changed):
            names = [l for d in files.values() for l in d.decode().splitlines() if l.startswith("def ")]
            return Verdict(len(names) == len(set(names)), "duplicate definitions")
        model = ScriptedModel([Reply("", [call("write_file", "1", path="y.py", content="def helper_two():\n    return 2\n")]),
                               Reply("", [call("finish", "2", summary="renamed")])])
        q = MergeQueue(repo, FnValidator(no_dup), merger=merger_worker(model))
        self.assertTrue(q.land(ta).landed)
        lb = q.land(tb)
        self.assertEqual((lb.status, lb.via), ("landed", "merger"))

    def test_a_plain_landing_with_survival_on_is_not_slowed_to_failure(self):
        repo = Repository.init(MemoryKV(), {"f": b"a\n", "g": b"b\n"})
        ta = edit(repo, "a", f="a\nx\n")
        tb = edit(repo, "b", g="b\ny\n")
        q = MergeQueue(repo)
        self.assertTrue(q.land(ta).landed)
        self.assertTrue(q.land(tb).landed)
        self.assertEqual(repo.main_files(), {"f": b"a\nx\n", "g": b"b\ny\n"})


class QueueRefusesLostWorkOnTheNormalPath(unittest.TestCase):
    def test_a_commit_that_deletes_mains_additions_cannot_come_from_a_clean_merge(self):
        # a clean merge of random edits is never refused: land many random branches through the queue
        rng = random.Random(5)
        for run in range(20):
            repo = Repository.init(MemoryKV(), {"f": "".join(f"l{i}\n" for i in range(8)).encode()})
            q = MergeQueue(repo, max_redos=0)
            tasks = []
            for i in range(4):
                ws = repo.fork(f"w{i}")
                lines = ws.read_text("f").splitlines(keepends=True)
                lines.insert(rng.randint(0, len(lines)), f"new{i}\n")
                ws.write("f", "".join(lines))
                ws.commit(f"w{i}")
                tasks.append(Task(f"w{i}", "x"))
            for t in tasks:
                lt = q.land(t)
                # either it landed, or it needed a person (adjacent edits conflict); nothing is ever lost
                self.assertIn(lt.status, ("landed", "escalated"))
            text = repo.main_files()["f"].decode()
            for t in tasks:
                if repo.refs.get("main") and q.escalations.list() == []:
                    self.assertIn(f"new{t.id[1:]}\n", text)


if __name__ == "__main__":
    unittest.main()
