"""Hard cases for the new pieces: the diff they all rest on, many threads and separate queues landing at once,
big inputs, many rules, and hostile files."""

import random
import re
import threading
import time
import unittest

from cmws_ws.agents import MergeQueue, Task, WorkerResult
from cmws_ws.checks import parse_checks
from cmws_ws.kv import MemoryKV
from cmws_ws.policy import FILE_NAME, MAX_RULES, PolicyTable
from cmws_ws.repo import Repository
from cmws_ws.serial import MemFS, check_serializable
from cmws_ws.survival import survival_problems
from cmws_ws.textmerge import hunks, merge3, merge_text, split_lines

INC = parse_checks([{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}])


def apply_hunks(base, hs):
    out, pos = [], 0
    for h in hs:
        out.extend(base[pos:h.start])
        out.extend(h.lines)
        pos = h.end
    out.extend(base[pos:])
    return out


class HunkTests(unittest.TestCase):
    """``hunks`` trims common head and tail before diffing; whatever it returns must rebuild the side exactly."""

    def test_hunks_rebuild_the_side_for_random_texts(self):
        rng = random.Random(42)
        for _ in range(3000):
            alpha = rng.choice(["ab", "abc", "abcdefg"])
            a = [f"{rng.choice(alpha)}\n" for _ in range(rng.randint(0, 12))]
            b = [f"{rng.choice(alpha)}\n" for _ in range(rng.randint(0, 12))]
            hs = hunks(a, b)
            self.assertEqual(apply_hunks(a, hs), b, (a, b, hs))
            for h1, h2 in zip(hs, hs[1:]):
                self.assertLess(h1.end, h2.start + 1)                 # ordered and not overlapping
            for h in hs:
                self.assertTrue(0 <= h.start <= h.end <= len(a))
                self.assertTrue(h.lines or h.end > h.start)           # a hunk changes something

    def test_identical_texts_have_no_hunks(self):
        self.assertEqual(hunks(["a\n"] * 100, ["a\n"] * 100), [])
        self.assertEqual(hunks([], []), [])

    def test_empty_against_something(self):
        self.assertEqual([(h.start, h.end, h.lines) for h in hunks([], ["a\n", "b\n"])], [(0, 0, ("a\n", "b\n"))])
        self.assertEqual([(h.start, h.end, h.lines) for h in hunks(["a\n", "b\n"], [])], [(0, 2, ())])

    def test_head_and_tail_overlap_does_not_double_count(self):
        # inserting a copy of a neighbouring line: prefix and suffix scans could overlap
        a = ["x\n", "x\n"]
        b = ["x\n", "x\n", "x\n"]
        hs = hunks(a, b)
        self.assertEqual(apply_hunks(a, hs), b)
        a = ["p\n", "x\n", "q\n"]
        b = ["p\n", "x\n", "x\n", "q\n"]
        self.assertEqual(apply_hunks(a, hunks(a, b)), b)

    def test_merge3_is_unchanged_for_a_small_known_case(self):
        r = merge_text("a\nb\nc\n", "a\nB\nc\n", "a\nb\nC\n")
        self.assertFalse(r.clean)                                     # adjacent edits are a conflict, as in git
        r = merge_text("a\nb\nc\nd\ne\n", "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n")
        self.assertEqual(r.text, "A\nb\nc\nd\nE\n")

    def test_a_large_file_with_a_small_edit_is_fast(self):
        rng = random.Random(1)
        base = "".join(f"row {rng.randint(0, 10**9)}\n" for _ in range(60_000))
        t = time.time()
        r = merge_text(base, base + "OURS\n", base + "THEIRS\n", append=True)
        self.assertTrue(r.clean)
        self.assertLess(time.time() - t, 3.0)
        self.assertTrue(r.text.endswith("THEIRS\nOURS\n"))

    def test_survival_on_a_large_file_is_fast(self):
        rng = random.Random(2)
        base = "".join(f"row {rng.randint(0, 10**9)}\n" for _ in range(60_000))
        t = time.time()
        got = survival_problems({"f": base.encode()}, {"f": (base + "O\n").encode()}, {"f": (base + "T\n").encode()},
                                {"f": (base + "T\nO\n").encode()})
        self.assertEqual(got, [])
        self.assertLess(time.time() - t, 3.0)


class ManyRulesTests(unittest.TestCase):
    def test_a_table_with_the_maximum_number_of_rules_answers_quickly(self):
        table = PolicyTable([(f"dir{i}/*.txt", "append") for i in range(MAX_RULES)])
        t = time.time()
        for i in range(3000):
            table.policy_for(f"src/pkg{i}/file{i}.py")
        self.assertLess(time.time() - t, 2.0)

    def test_a_tree_of_many_files_merges_with_a_large_rule_file(self):
        rules = "".join(f"zone{i}/*.log append\n" for i in range(MAX_RULES - 1)) + "*.log append\n"
        files = {FILE_NAME: rules.encode(), "keep.txt": b"k\n"}
        for i in range(300):
            files[f"d{i % 10}/f{i}.log"] = b"head\n"
        repo = Repository.init(MemoryKV(), files)
        a, b = repo.fork("a"), repo.fork("b")
        for ws, tag in ((a, "A"), (b, "B")):
            for i in range(0, 300, 7):
                p = f"d{i % 10}/f{i}.log"
                ws.write(p, ws.read_text(p) + tag + "\n")
            ws.commit(tag)
        t = time.time()
        self.assertTrue(repo.submit(a).landed)
        r = repo.submit(b)
        self.assertTrue(r.landed, r.status)
        self.assertLess(time.time() - t, 15.0)
        self.assertEqual(repo.main_files()["d0/f0.log"], b"head\nA\nB\n")


class RedoCounter:
    """A redo that bumps the counter on top of the current main (a perfect agent)."""
    name = "redo"

    def run(self, task, repo, agent_id):
        ws = repo.resume(agent_id)
        n = int(re.search(r"COUNT = (\d+)", ws.read_text("stats.py")).group(1)) + 1
        ws.write("stats.py", f"COUNT = {n}\n")
        return WorkerResult("done", "redone", ws.commit(agent_id))


def race(n, make_queue, rounds=1):
    """n threads each fork the same main, bump the counter and land through a queue at the same moment."""
    repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
    wss = [repo.fork(f"t{i}") for i in range(n)]
    for ws in wss:
        m = re.search(r"COUNT = (\d+)", ws.read_text("stats.py"))
        ws.write("stats.py", f"COUNT = {int(m.group(1)) + 1}\n")
        ws.commit(ws.agent_id)
    gate = threading.Barrier(n)
    out = {}

    def land(i):
        q = make_queue(repo)
        gate.wait()
        out[i] = q.land(Task(f"t{i}", "bump", (), "", (), (), INC))
    ts = [threading.Thread(target=land, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return repo, out


class ConcurrentLandingTests(unittest.TestCase):
    EDITS = {}

    def serial_ok(self, repo, n):
        def bump(fs):
            m = re.search(r"COUNT = (\d+)", fs.read_text("stats.py"))
            fs.write("stats.py", f"COUNT = {int(m.group(1)) + 1}\n")
        edits = {f"t{i}": bump for i in range(n)}
        task_of = lambda a: (re.match(r"(t\d+)", a).group(1) if re.match(r"t\d+(-redo\d+)?$", a) else None)         # noqa: E731
        return check_serializable(repo, {"stats.py": "COUNT = 0\n"}, edits, task_of)

    def test_one_shared_queue_never_loses_an_increment(self):
        for _ in range(6):
            n = 8
            repo, out = race(n, lambda repo, q=[]: q[0] if q else (q.append(MergeQueue(repo, redo_worker=RedoCounter(), max_redos=n + 2)) or q[0]))
            self.assertTrue(all(l.landed for l in out.values()), {i: l.status for i, l in out.items()})
            self.assertEqual(repo.main_files()["stats.py"], f"COUNT = {n}\n".encode())
            self.assertTrue(self.serial_ok(repo, n).ok)

    def test_separate_queues_on_one_store_correctness_does_not_rely_on_the_lock(self):
        for _ in range(6):
            n = 6
            repo, out = race(n, lambda repo: MergeQueue(repo, redo_worker=RedoCounter(), max_redos=n + 2))
            landed = [i for i, l in out.items() if l.landed]
            count = int(re.search(r"\d+", repo.main_files()["stats.py"].decode()).group(0))
            self.assertEqual(count, len(landed))                           # every landed task is in the count, none is lost
            self.assertTrue(self.serial_ok(repo, n).ok)

    def test_gitlike_with_checks_never_lands_a_wrong_count(self):
        for _ in range(6):
            n = 6
            repo, out = race(n, lambda repo: MergeQueue(repo, isolation="gitlike", survival=True, accept_checks=True,
                                                         redo_worker=RedoCounter(), max_redos=n + 2))
            self.assertEqual(repo.main_files()["stats.py"], f"COUNT = {n}\n".encode())
            self.assertTrue(self.serial_ok(repo, n).ok)

    def test_gitlike_without_checks_does_lose_them(self):
        lost = 0
        for _ in range(6):
            n = 6
            repo, out = race(n, lambda repo: MergeQueue(repo, isolation="gitlike", survival=False, accept_checks=False))
            count = int(re.search(r"\d+", repo.main_files()["stats.py"].decode()).group(0))
            lost += n - count
        self.assertGreater(lost, 0)

    def test_appends_and_a_counter_in_the_same_landing_stream(self):
        files = {"stats.py": b"COUNT = 0\n", "reg.txt": b"head\n", FILE_NAME: b"reg.txt append\n"}
        repo = Repository.init(MemoryKV(), files)
        n = 8
        wss = [repo.fork(f"t{i}") for i in range(n)]
        for i, ws in enumerate(wss):
            m = re.search(r"COUNT = (\d+)", ws.read_text("stats.py"))
            ws.write("stats.py", f"COUNT = {int(m.group(1)) + 1}\n")
            ws.write("reg.txt", ws.read_text("reg.txt") + f"entry {i}\n")
            ws.commit(ws.agent_id)
        gate = threading.Barrier(n)
        results = {}

        def land(i):
            q = MergeQueue(repo, redo_worker=RedoCounter(), max_redos=2 * n)
            gate.wait()
            results[i] = q.land(Task(f"t{i}", "bump", (), "", (), (), INC))
        ts = [threading.Thread(target=land, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join(120) for t in ts]
        self.assertTrue(all(r.landed for r in results.values()), {i: (r.status, r.steps[-1].detail if r.steps else "") for i, r in results.items()})
        main = repo.main_files()
        self.assertEqual(main["stats.py"], f"COUNT = {n}\n".encode())
        entries = main["reg.txt"].decode().splitlines()[1:]
        # a redo rebuilds the file from main, so the entries of redone tasks are not in it: that is the declared cost of a
        # redo worker that only bumps the counter. Every entry that is present appears exactly once.
        self.assertEqual(len(entries), len(set(entries)))


class HostileInputTests(unittest.TestCase):
    def test_binary_and_nul_bytes_in_a_declared_append_file(self):
        repo = Repository.init(MemoryKV(), {"blob.bin": b"\x00\x01", FILE_NAME: b"*.bin append\n"})
        a, b = repo.fork("a"), repo.fork("b")
        a.write("blob.bin", b"\x00\x01\x02")
        a.commit("a")
        b.write("blob.bin", b"\x00\x01\x03")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertIn(repo.submit(b).status, ("stale", "conflict"))        # binary is never appended

    def test_a_rule_file_with_nul_bytes_or_huge_lines_changes_nothing(self):
        for blob in (b"a.py append\x00\n", b"x" * 100_000, b"\n" * 100_000, b"a.py\tappend\r" * 1000):
            repo = Repository.init(MemoryKV(), {"a.py": b"x\n", FILE_NAME: blob})
            a, b = repo.fork("a"), repo.fork("b")
            for ws, tag in ((a, "A"), (b, "B")):
                ws.write("a.py", ws.read_text("a.py") + tag + "\n")
                ws.commit(tag)
            self.assertTrue(repo.submit(a).landed)
            r = repo.submit(b)
            self.assertIn(r.status, ("stale", "landed", "merged", "fast-forward", "conflict"))     # no exception is the point

    def test_pathological_file_names(self):
        names = ["sp ace.txt", "ünï.txt", "-dash.txt", "a#b.txt", "[x].txt", "*.txt", "?.txt", "a\\b.txt"]
        files = {n: b"h\n" for n in names}
        files[FILE_NAME] = b"*.txt append\n"
        repo = Repository.init(MemoryKV(), files)
        a, b = repo.fork("a"), repo.fork("b")
        for ws, tag in ((a, "A"), (b, "B")):
            for n in names:
                ws.write(n, ws.read_text(n) + tag + "\n")
            ws.commit(tag)
        self.assertTrue(repo.submit(a).landed)
        r = repo.submit(b)
        self.assertTrue(r.landed, r.status)
        for n in names:
            self.assertEqual(repo.main_files()[n], b"h\nA\nB\n", n)

    def test_deep_directories_and_unicode_content(self):
        deep = "/".join(["d"] * 30) + "/log.txt"
        repo = Repository.init(MemoryKV(), {deep: "α\n".encode(), FILE_NAME: b"log.txt append\n"})
        a, b = repo.fork("a"), repo.fork("b")
        a.write(deep, a.read_text(deep) + "β\n")
        a.commit("a")
        b.write(deep, b.read_text(deep) + "γ\n")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertTrue(repo.submit(b).landed)
        self.assertEqual(repo.main_files()[deep].decode(), "α\nβ\nγ\n")

    def test_survival_with_a_line_that_is_all_whitespace_and_a_tab_file(self):
        self.assertEqual(survival_problems({"f": b"a\n"}, {"f": b"a\n \t \n"}, {"f": b"a\n"}, {"f": b"a\n"}), [])

    def test_a_file_without_any_newline(self):
        r = merge_text("a", "a\nb", "a\nc", append=True)
        self.assertTrue(r.clean or r.conflicts)                         # no crash on a first line without a terminator

    def test_empty_files_everywhere(self):
        r = merge_text("", "", "", append=True)
        self.assertTrue(r.clean)
        self.assertEqual(r.text, "")
        self.assertEqual(survival_problems({"f": b""}, {"f": b""}, {"f": b""}, {"f": b""}), [])

    def test_huge_single_line(self):
        line = "x" * 3_000_000
        r = merge_text("a\n", "a\n" + line + "\n", "a\nT\n", append=True)
        self.assertTrue(r.clean)
        self.assertEqual(survival_problems({"f": b"a\n"}, {"f": ("a\n" + line + "\n").encode()}, {"f": b"a\nT\n"},
                                           {"f": r.text.encode()}), [])


class RandomisedPipelineTests(unittest.TestCase):
    """Random tasks through the queue with every protection on: whatever lands is never lost and main stays one line."""

    def test_random_inserts_into_two_declared_files(self):
        rng = random.Random(77)
        for run in range(15):
            files = {"x.txt": b"x0\n", "y.txt": b"y0\n", FILE_NAME: b"*.txt append\n"}
            repo = Repository.init(MemoryKV(), files)
            n = rng.randint(2, 7)
            wss, expect = [], {}
            for i in range(n):
                ws = repo.fork(f"w{i}")
                p = rng.choice(["x.txt", "y.txt"])
                ws.write(p, ws.read_text(p) + f"w{i}\n")
                ws.commit(ws.agent_id)
                wss.append(ws)
                expect.setdefault(p, []).append(f"w{i}")
            q = MergeQueue(repo, max_redos=0)
            order = list(range(n))
            rng.shuffle(order)
            for i in order:
                landing = q.land(Task(f"w{i}", "x"))
                self.assertTrue(landing.landed, (run, i, landing.status, [(s.stage, s.status, s.detail) for s in landing.steps]))
            for p, names in expect.items():
                got = repo.main_files()[p].decode().splitlines()[1:]
                self.assertEqual(got, [f"w{i}" for i in order if f"w{i}" in names], (run, p))


if __name__ == "__main__":
    unittest.main()
