"""The serializability checker, tested on hand-made histories (including ones that are serializable only in an order
other than the one they landed in) and on randomly generated runs."""

import random
import re
import unittest

from cmws_ws.agents import MergeQueue, Task
from cmws_ws.kv import MemoryKV
from cmws_ws.history import ancestors
from cmws_ws.objects import Commit
from cmws_ws.repo import Repository
from cmws_ws.serial import (MemFS, check_final, check_serializable, check_unordered, diff_paths, landing_order, replay)


def append(path, text):
    def edit(fs):
        fs.write(path, fs.read_text(path) + text)
    return edit


def increment(fs):
    t = fs.read_text("n.txt")
    fs.write("n.txt", f"{int(t) + 1}\n")


def land(repo, agent, **files):
    """Fork, write, commit and land one branch (file names use _dot_ for '.')."""
    ws = repo.fork(agent)
    for path, text in files.items():
        ws.write(path.replace("_dot_", "."), text)
    ws.commit(f"{agent} work")
    r = repo.submit(ws, f"{agent}")
    assert r.landed, r.status
    return ws


def edits_of(**fns):
    return dict(fns)


class MemFSTests(unittest.TestCase):
    def test_read_write_delete_move_list(self):
        fs = MemFS({"a/b.txt": "x", "c.txt": b"y"})
        self.assertEqual(fs.read_text("c.txt"), "y")                     # bytes are decoded
        fs.write("a/d.txt", b"z")
        self.assertEqual(fs.list(), [("a", "dir"), ("c.txt", "file")])
        self.assertEqual(fs.list("a"), [("b.txt", "file"), ("d.txt", "file")])
        self.assertEqual(fs.list("/a/"), fs.list("a"))
        fs.move("c.txt", "e.txt")
        self.assertFalse(fs.exists("c.txt"))
        self.assertEqual(fs.read_text("e.txt"), "y")
        fs.delete("e.txt")
        self.assertEqual(sorted(fs.files()), ["a/b.txt", "a/d.txt"])

    def test_missing_files_raise_file_not_found(self):
        fs = MemFS()
        for op in (lambda: fs.read_text("x"), lambda: fs.delete("x"), lambda: fs.move("x", "y")):
            with self.assertRaises(FileNotFoundError):
                op()

    def test_files_returns_a_copy(self):
        fs = MemFS({"a": "1"})
        fs.files()["a"] = "changed"
        self.assertEqual(fs.read_text("a"), "1")

    def test_undecodable_bytes_survive_a_round_trip(self):
        raw = b"\xff\xfe binary"
        fs = MemFS({"b": raw})
        self.assertEqual(fs.read_text("b").encode("utf-8", "surrogateescape"), raw)

    def test_list_of_an_empty_directory_name_is_the_root(self):
        self.assertEqual(MemFS({"a": "1"}).list(""), [("a", "file")])
        self.assertEqual(MemFS().list(), [])


class ReplayAndDiffTests(unittest.TestCase):
    def test_replay_does_not_touch_the_initial_files(self):
        init = {"f": "a"}
        out = replay(init, ["t"], {"t": append("f", "b")})
        self.assertEqual(out, {"f": "ab"})
        self.assertEqual(init, {"f": "a"})

    def test_replay_of_nothing_is_the_initial_tree(self):
        self.assertEqual(replay({"f": "a"}, [], {}), {"f": "a"})

    def test_replay_unknown_task_raises_key_error(self):
        with self.assertRaises(KeyError):
            replay({}, ["nope"], {})

    def test_diff_paths_lists_changed_added_and_removed_files(self):
        self.assertEqual(diff_paths({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "x", "d": "4"}), ["b", "c", "d"])
        self.assertEqual(diff_paths({}, {}), [])


class CheckFinalTests(unittest.TestCase):
    ED = {"a": append("f", "A"), "b": append("f", "B"), "c": append("f", "C")}

    def test_matching_order_is_ok(self):
        v = check_final({"f": ""}, {"f": "AB"}, self.ED, ["a", "b"])
        self.assertTrue(v.ok and v.serializable)
        self.assertIsNone(v.witness)
        self.assertEqual(v.differing, [])

    def test_a_different_order_gives_a_witness(self):
        v = check_final({"f": ""}, {"f": "BA"}, self.ED, ["a", "b"])
        self.assertFalse(v.ok)
        self.assertEqual(v.witness, ["b", "a"])
        self.assertTrue(v.serializable and v.searched)
        self.assertEqual(v.differing, ["f"])

    def test_no_order_matches(self):
        v = check_final({"f": ""}, {"f": "A"}, self.ED, ["a", "b"])        # b's update was lost
        self.assertFalse(v.ok or v.serializable)
        self.assertTrue(v.searched)

    def test_search_can_be_turned_off(self):
        v = check_final({"f": ""}, {"f": "BA"}, self.ED, ["a", "b"], search=False)
        self.assertFalse(v.searched)
        self.assertFalse(v.serializable)

    def test_search_is_skipped_above_the_limit(self):
        names = [f"t{i}" for i in range(5)]
        eds = {n: append("f", n) for n in names}
        v = check_final({"f": ""}, {"f": "garbage"}, eds, names, max_search=4)
        self.assertFalse(v.searched)
        self.assertFalse(v.serializable)

    def test_search_at_the_limit_finds_a_witness(self):
        names = ["t0", "t1", "t2", "t3"]
        eds = {n: append("f", n) for n in names}
        final = {"f": "t3t1t0t2"}
        v = check_final({"f": ""}, final, eds, names, max_search=4)
        self.assertEqual(v.witness, ["t3", "t1", "t0", "t2"])

    def test_an_edit_that_raises_is_reported_not_raised(self):
        def boom(fs):
            raise RuntimeError("nope")
        v = check_final({"f": ""}, {"f": ""}, {"a": boom}, ["a"])
        self.assertFalse(v.ok)
        self.assertIn("RuntimeError: nope", v.error)
        self.assertIn("a", v.error)

    def test_an_order_in_which_an_edit_cannot_run_is_no_witness(self):
        def needs_g(fs):
            fs.write("f", fs.read_text("g"))          # fails unless g exists
        eds = {"mk": lambda fs: fs.write("g", "x"), "use": needs_g}
        v = check_final({}, {"g": "x", "f": "x"}, eds, ["mk", "use"])
        self.assertTrue(v.ok)
        v = check_final({}, {"g": "x", "f": "y"}, eds, ["mk", "use"])       # no order explains f == y
        self.assertFalse(v.serializable)

    def test_final_with_extra_or_missing_files_differs(self):
        v = check_final({"f": ""}, {"f": "A", "extra": "1"}, {"a": append("f", "A")}, ["a"])
        self.assertEqual(v.differing, ["extra"])
        v = check_final({"f": "", "g": "1"}, {"f": "A"}, {"a": append("f", "A")}, ["a"])
        self.assertEqual(v.differing, ["g"])

    def test_empty_run(self):
        self.assertTrue(check_final({"f": "x"}, {"f": "x"}, {}, []).ok)
        v = check_final({"f": "x"}, {"f": "y"}, {}, [])
        self.assertFalse(v.ok)
        self.assertTrue(v.searched)            # no permutation of nothing but itself
        self.assertIsNone(v.witness)

    def test_bytes_in_final_are_compared_as_text(self):
        v = check_final({"f": "é"}, {"f": "é".encode()}, {}, [])
        self.assertTrue(v.ok)

    def test_non_text_final_does_not_crash(self):
        v = check_final({"f": "x"}, {"f": b"\xff\xfe"}, {}, [])
        self.assertFalse(v.ok)

    def test_check_unordered_finds_any_order(self):
        v = check_unordered({"f": ""}, {"f": "CAB"}, self.ED, ["b", "a", "c"])
        self.assertEqual(v.witness, ["c", "a", "b"])
        self.assertTrue(v.serializable)

    def test_check_unordered_detects_lost_updates(self):
        v = check_unordered({"f": ""}, {"f": "AC"}, self.ED, ["a", "b", "c"])
        self.assertFalse(v.serializable)

    def test_check_unordered_with_no_tasks(self):
        self.assertTrue(check_unordered({"f": ""}, {"f": ""}, {}, []).serializable)

    def test_the_counter_in_a_shared_folder_is_not_serializable(self):
        eds = {f"t{i}": increment for i in range(4)}
        v = check_unordered({"n.txt": "0\n"}, {"n.txt": "2\n"}, eds, list(eds))
        self.assertFalse(v.serializable)
        v = check_unordered({"n.txt": "0\n"}, {"n.txt": "4\n"}, eds, list(eds))
        self.assertTrue(v.ok)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository.init(MemoryKV(), {"f": b"", "n.txt": b"0\n"})

    def task_of(self, a):
        return a if a in ("a", "b", "c", "d") else None

    def test_landing_order_follows_ancestry_not_names(self):
        land(self.repo, "c", f="C")
        land(self.repo, "a", f="CA")
        land(self.repo, "b", f="CAB")
        order, linear = landing_order(self.repo, self.task_of)
        self.assertEqual(order, ["c", "a", "b"])
        self.assertTrue(linear)

    def test_no_tasks_no_order(self):
        self.assertEqual(landing_order(self.repo, self.task_of), ([], True))

    def test_the_initial_commit_is_not_a_task(self):
        order, _ = landing_order(self.repo, lambda a: a)         # every author counts, including "system"
        self.assertEqual(order, ["system"])

    def test_a_branch_that_never_landed_is_left_out(self):
        land(self.repo, "a", f="A")
        ws = self.repo.fork("b")
        ws.write("g", "never")
        ws.commit("b work")                                   # not submitted
        order, _ = landing_order(self.repo, self.task_of)
        self.assertEqual(order, ["a"])

    def test_several_commits_by_one_agent_count_once_at_the_last(self):
        ws = self.repo.fork("a")
        ws.write("f", "1")
        ws.commit("one")
        ws.write("f", "12")
        ws.commit("two")
        self.assertTrue(self.repo.submit(ws, "a").landed)
        land(self.repo, "b", f="12B")
        order, linear = landing_order(self.repo, self.task_of)
        self.assertEqual(order, ["a", "b"])
        self.assertTrue(linear)

    def test_a_branch_that_synced_main_in_lands_after_the_work_it_absorbed(self):
        wb = self.repo.fork("b")
        wb.write("g", "B")
        wb.commit("b work")
        land(self.repo, "a", f="A")
        self.assertEqual(wb.sync().status, "merged")           # b now contains a, and b's own merge commit is authored by b
        self.assertTrue(self.repo.submit(wb, "b").landed)
        order, linear = landing_order(self.repo, self.task_of)
        self.assertEqual(order, ["a", "b"])
        self.assertTrue(linear)

    def test_fast_forward_landing_after_a_sync(self):
        wa = self.repo.fork("a")
        land(self.repo, "c", f="C")
        wa.write("g", "A")
        wa.commit("a work")
        wa.sync()
        self.assertTrue(self.repo.submit(wa, "a").landed)
        order, _ = landing_order(self.repo, self.task_of)
        self.assertEqual(order, ["c", "a"])

    def test_authors_are_mapped_through_task_of(self):
        land(self.repo, "run1-a", f="A")
        land(self.repo, "run1-b-redo2", f="AB")
        order, _ = landing_order(self.repo, lambda au: au.split("-")[1] if au.startswith("run1-") else None)
        self.assertEqual(order, ["a", "b"])

    def test_concurrent_tips_are_reported_as_not_linear(self):
        # two tasks whose tips are not ordered by ancestry: both merged into a third commit by hand
        base = self.repo.head()
        store = self.repo.store
        tree = store.get_commit(base).tree
        ca = store.put_commit(Commit(tree, (base,), "a", "a", 1.0))
        cb = store.put_commit(Commit(tree, (base,), "b", "b", 2.0))
        top = store.put_commit(Commit(tree, (ca, cb), "system", "merge", 3.0))
        order, linear = landing_order(self.repo, self.task_of, head=top)
        self.assertEqual(sorted(order), ["a", "b"])
        self.assertFalse(linear)

    def test_head_argument_looks_at_an_older_state(self):
        land(self.repo, "a", f="A")
        old = self.repo.head()
        land(self.repo, "b", f="AB")
        self.assertEqual(landing_order(self.repo, self.task_of, head=old)[0], ["a"])


class CheckSerializableTests(unittest.TestCase):
    INIT = {"f": "", "n.txt": "0\n"}

    def setUp(self):
        self.repo = Repository.init(MemoryKV(), {k: v.encode() for k, v in self.INIT.items()})
        self.edits = {"a": append("f", "A"), "b": append("f", "B"), "c": increment}

    def test_a_correct_serial_run_passes(self):
        land(self.repo, "a", f="A")
        land(self.repo, "b", f="AB")
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertTrue(v.ok)
        self.assertEqual(v.order, ["a", "b"])

    def test_serializable_in_another_order_only(self):
        land(self.repo, "a", f="A")
        land(self.repo, "b", f="BA")                         # b's edit was applied before a's
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertFalse(v.ok)
        self.assertEqual(v.witness, ["b", "a"])
        self.assertTrue(v.serializable)

    def test_a_lost_update_is_not_serializable(self):
        wa, wc = self.repo.fork("a"), self.repo.fork("c")      # both read 0 and wrote 1
        for ws in (wa, wc):
            ws.write("n.txt", "1\n")
            ws.commit(ws.agent_id)
        self.assertTrue(self.repo.submit(wa, "a").landed)
        self.assertTrue(self.repo.submit(wc, "c", allow_stale=True).landed)      # merged as agreement: one increment is gone
        v = check_serializable(self.repo, self.INIT, {"a": increment, "c": increment, "b": self.edits["b"]})
        self.assertFalse(v.serializable)
        self.assertEqual(v.differing, ["n.txt"])

    def test_no_edit_known_for_a_landed_task(self):
        land(self.repo, "a", f="A")
        v = check_serializable(self.repo, self.INIT, {"b": self.edits["b"]}, task_of=lambda au: au if au in "ab" else None)
        self.assertFalse(v.ok)
        self.assertIn("no edit known for a", v.error)

    def test_task_of_default_ignores_unknown_authors(self):
        land(self.repo, "a", f="A")                          # "system" wrote the initial commit
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertEqual(v.order, ["a"])

    def test_empty_history(self):
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertTrue(v.ok)
        self.assertEqual(v.order, [])

    def test_a_manual_change_to_main_is_a_difference(self):
        land(self.repo, "a", f="A")
        land(self.repo, "zzz", f="AX")                       # an author that is not a task changed f
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertFalse(v.ok)

    def test_verdict_keeps_expected_and_actual(self):
        land(self.repo, "a", f="Q")
        v = check_serializable(self.repo, self.INIT, self.edits)
        self.assertEqual(v.expected["f"], "A")
        self.assertEqual(v.actual["f"], "Q")


class QueueRunsAreSerializable(unittest.TestCase):
    """Real landings through the merge queue, with the counter edit as oracle agents."""

    def run_counter(self, n, **qkw):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        accept = [{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}]
        from cmws_ws.checks import parse_checks

        def bump(fs):
            m = re.search(r"COUNT = (\d+)", fs.read_text("stats.py"))
            fs.write("stats.py", f"COUNT = {int(m.group(1)) + 1}\n")
        edits = {f"t{i}": bump for i in range(n)}
        wss = {}
        for i in range(n):
            wss[f"t{i}"] = repo.fork(f"t{i}")
        for name, ws in wss.items():                       # all read the same base, then write: the race
            fs = MemFS({"stats.py": ws.read_text("stats.py")})
            bump(fs)
            ws.write("stats.py", fs.read_text("stats.py"))
            ws.commit(name)
        q = MergeQueue(repo, **qkw)
        results = {}
        for i in range(n):
            t = Task(f"t{i}", "bump", (), "", (), (), parse_checks(accept))
            results[f"t{i}"] = q.land(t)
        return repo, edits, results

    def test_gitlike_loses_updates_and_the_checker_says_so(self):
        repo, edits, results = self.run_counter(4, isolation="gitlike", survival=False, accept_checks=False)
        self.assertTrue(all(r.landed for r in results.values()))
        v = check_serializable(repo, {"stats.py": "COUNT = 0\n"}, edits)
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 1\n")
        self.assertFalse(v.serializable)

    def test_transition_checks_refuse_what_gitlike_loses(self):
        repo, edits, results = self.run_counter(4, isolation="gitlike", survival=False, accept_checks=True)
        landed = [t for t, r in results.items() if r.landed]
        self.assertEqual(landed, ["t0"])                      # the others raise 1 -> 1 and are refused (no redo worker: escalated)
        v = check_serializable(repo, {"stats.py": "COUNT = 0\n"}, edits)
        self.assertTrue(v.ok)

    def test_snapshot_isolation_refuses_stale_work_and_stays_serializable(self):
        repo, edits, results = self.run_counter(4, isolation="snapshot", survival=False, accept_checks=False)
        for t, r in results.items():
            if t != "t0":
                self.assertFalse(r.landed)
        v = check_serializable(repo, {"stats.py": "COUNT = 0\n"}, edits)
        self.assertTrue(v.ok)


class RandomRunTests(unittest.TestCase):
    """Random sets of tasks landed in random order through the real repository: whatever the edits are, a run that
    went through compare-and-set with fresh forks is serializable, and a tampered tree is caught."""

    def test_random_serial_runs_and_a_random_tamper(self):
        rng = random.Random(1234)
        for run in range(40):
            names = [f"t{i}" for i in range(rng.randint(1, 6))]
            files = {f"f{i}": "" for i in range(3)}
            eds = {}
            repo = Repository.init(MemoryKV(), {k: v.encode() for k, v in files.items()})
            order = names[:]
            rng.shuffle(order)
            for n in names:
                path = rng.choice(list(files))
                token = f"[{n}]"
                eds[n] = append(path, token)
            for n in order:                                   # each task forks the latest main and lands at once
                ws = repo.fork(n)
                fs = MemFS({p: ws.read_text(p) for p in files})
                eds[n](fs)
                for p, text in fs.files().items():
                    ws.write(p, text)
                ws.commit(n)
                self.assertTrue(repo.submit(ws, n).landed)
            v = check_serializable(repo, files, eds)
            self.assertTrue(v.ok, (run, v.order, v.differing))
            self.assertEqual(v.order, order)
            ws = repo.fork("tamper")                          # someone edits a file by hand: not explained by any order
            ws.write("f0", repo.main_files()["f0"].decode() + "??")
            ws.commit("tamper")
            self.assertTrue(repo.submit(ws, "tamper").landed)
            v = check_serializable(repo, files, eds)
            self.assertFalse(v.ok, run)
            self.assertFalse(v.serializable)


if __name__ == "__main__":
    unittest.main()
