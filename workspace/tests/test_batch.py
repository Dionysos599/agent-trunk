"""The batching merge queue: many branches, one validation, and the guarantees that must not move."""

import random
import re
import threading
import time
import unittest

from cmws_ws.agents import BatchQueue, FnValidator, MergeQueue, Task, Verdict, WorkerResult
from cmws_ws.agents import fence
from cmws_ws.checks import parse_checks
from cmws_ws.history import ancestors
from cmws_ws.kv import MemoryKV
from cmws_ws.repo import Repository

from tests.support import RecordingKV

INC = parse_checks([{"kind": "increases", "path": "stats.py", "pattern": r"COUNT = (\d+)", "by": 1}])


def bump(text):
    return re.sub(r"COUNT = (\d+)", lambda m: f"COUNT = {int(m.group(1)) + 1}", text, count=1)


class Counting:
    """A validator that counts its runs, can be slow, and refuses any tree containing a marker."""

    def __init__(self, delay=0.0, forbid=b"BAD"):
        self.runs, self.seen, self.delay, self.forbid, self.lock = 0, [], delay, forbid, threading.Lock()

    def check(self, files, changed):
        with self.lock:
            self.runs += 1
        time.sleep(self.delay)
        bad = [p for p, d in files.items() if self.forbid in d]
        with self.lock:
            self.seen.append(frozenset(files))
        return Verdict(not bad, f"forbidden content in {bad}")


def branches(repo, n, content=lambda i: f"x{i}\n", path=lambda i: f"f{i}.txt"):
    out = []
    for i in range(n):
        ws = repo.fork(f"t{i}")
        ws.write(path(i), content(i))
        ws.commit(f"t{i}")
        out.append(Task(f"t{i}", f"task {i}"))
    return out


def land_all(queue, tasks, results=None):
    out, gate = {}, threading.Barrier(len(tasks))

    def run(t):
        gate.wait()
        out[t.id] = queue.land(t, results.get(t.id) if results else None)
    ts = [threading.Thread(target=run, args=(t,)) for t in tasks]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out


class BatchBasicsTests(unittest.TestCase):
    def test_eight_independent_branches_land_with_one_validation(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 8)
        v = Counting()
        q = BatchQueue(repo, FnValidator(v.check), window=0.2)
        out = land_all(q, tasks)
        self.assertTrue(all(l.landed and l.via == "batch" for l in out.values()), {t: l.status for t, l in out.items()})
        self.assertEqual(v.runs, 1)
        self.assertEqual(sorted(repo.main_files()), ["README"] + [f"f{i}.txt" for i in range(8)])
        self.assertEqual(q.batch_stats["landed"], 8)
        self.assertEqual(len(q.approved), 1)

    def test_it_is_much_faster_than_validating_one_at_a_time(self):
        def timed(make):
            repo = Repository.init(MemoryKV(), {"README": b"x\n"})
            tasks = branches(repo, 8)
            q = make(repo, FnValidator(Counting(delay=0.1).check))
            t0 = time.time()
            land_all(q, tasks)
            return time.time() - t0
        plain = timed(lambda r, v: MergeQueue(r, v))
        batched = timed(lambda r, v: BatchQueue(r, v, window=0.05))
        self.assertGreater(plain, 0.7)
        self.assertLess(batched, plain / 2.5)

    def test_every_member_is_a_landing_the_scheduler_understands(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 3)
        q = BatchQueue(repo, window=0.1)
        out = land_all(q, tasks)
        head = repo.head()
        for l in out.values():
            self.assertEqual((l.status, l.via, l.commit), ("landed", "batch", head))
            self.assertEqual(l.steps[0].stage, "batch")

    def test_the_branches_follow_main_afterwards(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 3)
        land_all(BatchQueue(repo, window=0.1), tasks)
        for t in tasks:
            ws = repo.resume(t.id)
            self.assertEqual((ws.head, ws.fork_base), (repo.head(), repo.head()))

    def test_a_single_branch_works(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        t = branches(repo, 1)
        q = BatchQueue(repo, window=0.0)
        self.assertTrue(q.land(t[0]).landed)

    def test_main_is_a_chain_and_contains_every_branch(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 5)
        land_all(BatchQueue(repo, window=0.1), tasks)
        reach = ancestors(repo.store, repo.head())
        for t in tasks:
            self.assertIn(repo.resume(t.id).head, reach)

    def test_validation_arguments(self):
        repo = Repository.init(MemoryKV(), {"a": b"1"})
        for kw in ({"max_batch": 0}, {"retries": 0}, {"window": -1}):
            with self.assertRaises(ValueError):
                BatchQueue(repo, **kw)

    def test_max_batch_limits_a_batch(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 6)
        v = Counting()
        q = BatchQueue(repo, FnValidator(v.check), window=0.2, max_batch=2)
        out = land_all(q, tasks)
        self.assertTrue(all(l.landed for l in out.values()))
        self.assertGreaterEqual(v.runs, 3)
        self.assertLessEqual(v.runs, 4)

    def test_a_worker_result_without_changes_goes_to_the_ordinary_chain(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        repo.fork("e")
        q = BatchQueue(repo, window=0.0)
        l = q.land(Task("e", "x"), WorkerResult("done", "nothing", None))
        self.assertEqual(l.status, "escalated")
        self.assertEqual(l.steps[0].status, "empty")


class BatchRefusalTests(unittest.TestCase):
    def test_a_conflicting_branch_leaves_and_the_others_still_land_together(self):
        repo = Repository.init(MemoryKV(), {"shared.txt": b"1\n"})
        tasks = branches(repo, 4, path=lambda i: f"f{i}.txt")
        for name, text in (("c1", "a\n"), ("c2", "b\n")):
            ws = repo.fork(name)
            ws.write("shared.txt", text)
            ws.commit(name)
        tasks += [Task("c1", "x"), Task("c2", "y")]
        v = Counting()
        q = BatchQueue(repo, FnValidator(v.check), window=0.2, max_redos=0)
        out = land_all(q, tasks)
        winner = "c1" if out["c1"].landed else "c2"                     # whichever reached the batch first; the other conflicts
        loser = "c2" if winner == "c1" else "c1"
        self.assertEqual(sorted(t for t, l in out.items() if l.landed), sorted([winner, "t0", "t1", "t2", "t3"]))
        self.assertEqual(out[loser].status, "escalated")
        self.assertEqual(out[winner].via, "batch")
        self.assertEqual(repo.main_files()["shared.txt"], b"a\n" if winner == "c1" else b"b\n")

    def test_the_validator_refusing_one_branch_costs_a_few_extra_validations(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 8, content=lambda i: "BAD\n" if i == 5 else f"x{i}\n")
        v = Counting()
        q = BatchQueue(repo, FnValidator(v.check), window=0.2, max_redos=0)
        out = land_all(q, tasks)
        self.assertEqual(sorted(t for t, l in out.items() if not l.landed), ["t5"])
        self.assertEqual(out["t5"].status, "escalated")
        self.assertEqual(out["t5"].steps[0].status, "rejected")
        self.assertNotIn(b"BAD", b"".join(repo.main_files().values()))
        self.assertLessEqual(v.runs, 1 + 2 * 3 + 1)                    # about 2 log2 N, not N
        self.assertGreaterEqual(q.batch_stats["splits"], 1)

    def test_main_never_holds_a_state_the_validator_did_not_see(self):
        kv = RecordingKV()
        repo = Repository.init(kv, {"README": b"x\n"})
        tasks = branches(repo, 8, content=lambda i: "BAD\n" if i in (2, 6) else f"x{i}\n")
        v = Counting()
        q = BatchQueue(repo, FnValidator(v.check), window=0.2, max_redos=0)
        land_all(q, tasks)
        states = {new for _, new in kv.transitions[1:]}
        self.assertEqual(states, set(q.approved) & states)             # every state main held was approved as it stands
        self.assertTrue(states)
        for new in states:
            self.assertNotIn(b"BAD", b"".join(repo.files_at(new).values()))

    def test_two_bad_branches_both_end_up_outside(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 6, content=lambda i: "BAD\n" if i in (0, 5) else f"x{i}\n")
        q = BatchQueue(repo, FnValidator(Counting().check), window=0.2, max_redos=0)
        out = land_all(q, tasks)
        self.assertEqual(sorted(t for t, l in out.items() if not l.landed), ["t0", "t5"])

    def test_a_stale_read_inside_the_batch_sends_the_branch_to_the_chain(self):
        repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n"})
        tasks = []
        for i in range(4):
            ws = repo.fork(f"t{i}")
            ws.write("stats.py", bump(ws.read_text("stats.py")))
            ws.commit(f"t{i}")
            tasks.append(Task(f"t{i}", "bump", (), "", (), (), INC))

        class Redo:
            name = "redo"

            def run(self, task, repo_, agent_id):
                ws = repo_.resume(agent_id)
                ws.write("stats.py", bump(ws.read_text("stats.py")))
                return WorkerResult("done", "redone", ws.commit(agent_id))
        q = BatchQueue(repo, window=0.2, redo_worker=Redo(), max_redos=6)
        out = land_all(q, tasks)
        self.assertTrue(all(l.landed for l in out.values()), {t: l.status for t, l in out.items()})
        self.assertEqual(repo.main_files()["stats.py"], b"COUNT = 4\n")          # nothing lost, nothing counted twice
        self.assertEqual(sum(1 for l in out.values() if l.via == "batch"), 1)      # only one of the racers fits in a batch

    def test_a_lost_claim_removes_one_member_only(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 4)
        q = BatchQueue(repo, window=0.2)
        out, gate = {}, threading.Barrier(4)

        def run(t):
            gate.wait()
            guard = (lambda: fence.LOST) if t.id == "t2" else (lambda: None)
            with fence.fenced(guard):
                out[t.id] = q.land(t)
        ts = [threading.Thread(target=run, args=(t,)) for t in tasks]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(out["t2"].status, "lost")
        self.assertNotIn("f2.txt", repo.main_files())
        self.assertTrue(all(out[t].landed for t in ("t0", "t1", "t3")))

    def test_a_claim_lost_while_validating_rebuilds_without_that_member(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 3)
        state = {"lost": False}
        v = Counting(delay=0.15)
        q = BatchQueue(repo, FnValidator(v.check), window=0.2)
        out, gate = {}, threading.Barrier(3)

        def run(t):
            gate.wait()
            guard = (lambda: fence.LOST if state["lost"] else None) if t.id == "t1" else (lambda: None)
            with fence.fenced(guard):
                out[t.id] = q.land(t)
        ts = [threading.Thread(target=run, args=(t,)) for t in tasks]
        [t.start() for t in ts]
        time.sleep(0.3)
        state["lost"] = True                                            # taken over while the validator runs
        [t.join(30) for t in ts]
        self.assertEqual(out["t1"].status, "lost")
        self.assertNotIn("f1.txt", repo.main_files())
        self.assertTrue(out["t0"].landed and out["t2"].landed)
        self.assertGreaterEqual(q.batch_stats["rebuilds"], 1)                     # the batch was rebuilt without it

    def test_main_moving_during_validation_rebuilds_the_batch(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 3)
        other = repo.fork("other")
        other.write("other.txt", "o\n")
        other.commit("other")
        v = Counting(delay=0.3)
        q = BatchQueue(repo, FnValidator(v.check), window=0.2)
        out = {}
        done = threading.Event()

        def land_batch():
            out.update(land_all(q, tasks))
            done.set()
        th = threading.Thread(target=land_batch)
        th.start()
        time.sleep(0.4)                                                  # the batch is in validation now
        self.assertTrue(repo.submit(other).landed)                       # someone else moves main
        th.join(60)
        self.assertTrue(done.is_set() and all(l.landed for l in out.values()), {t: l.status for t, l in out.items()})
        self.assertGreaterEqual(q.batch_stats["rebuilds"], 1)
        self.assertEqual(sorted(repo.main_files()), ["README", "f0.txt", "f1.txt", "f2.txt", "other.txt"])

    def test_a_crash_inside_the_batch_never_leaves_a_caller_waiting(self):
        repo = Repository.init(MemoryKV(), {"README": b"x\n"})
        tasks = branches(repo, 3)
        q = BatchQueue(repo, window=0.1, max_redos=0)
        q._build = lambda members, main0: (_ for _ in ()).throw(RuntimeError("boom"))        # type: ignore[assignment]
        out = land_all(q, tasks)
        self.assertTrue(all(l.landed and l.via == "auto" for l in out.values()))                 # each fell back to the chain

    def test_acceptance_checks_run_per_branch_in_the_batch(self):
        repo = Repository.init(MemoryKV(), {"a.txt": b"1\n", "b.txt": b"1\n"})
        ws = repo.fork("good")
        ws.write("a.txt", "2\n")
        ws.commit("g")
        ws = repo.fork("bad")
        ws.write("b.txt", "9\n")
        ws.commit("b")
        acc = parse_checks([{"kind": "contains", "path": "b.txt", "text": "expected"}])
        q = BatchQueue(repo, window=0.2, max_redos=0)
        out = land_all(q, [Task("good", "x"), Task("bad", "y", (), "", (), (), acc)])
        self.assertTrue(out["good"].landed)
        self.assertEqual(out["bad"].status, "escalated")
        self.assertIn("acceptance check failed", out["bad"].steps[0].detail)


class BatchPropertyTests(unittest.TestCase):
    def test_random_workloads_lose_nothing_and_publish_only_validated_states(self):
        rng = random.Random(2025)
        for run in range(12):
            kv = RecordingKV()
            repo = Repository.init(kv, {"shared.txt": b"head\n", "README": b"x\n"})
            n = rng.randint(3, 9)
            tasks, tokens = [], {}
            for i in range(n):
                ws = repo.fork(f"t{i}")
                kind = rng.choice(["own", "own", "own", "shared", "bad"])
                token = f"tok{i}"
                if kind == "shared":
                    ws.write("shared.txt", ws.read_text("shared.txt") + token + "\n")
                else:
                    ws.write(f"f{i}.txt", ("BAD " if kind == "bad" else "") + token + "\n")
                ws.commit(f"t{i}")
                tokens[f"t{i}"] = (kind, token)
                tasks.append(Task(f"t{i}", "x"))
            v = Counting()
            q = BatchQueue(repo, FnValidator(v.check), window=0.1, max_redos=0, max_batch=rng.choice([2, 4, 8]))
            out = land_all(q, tasks)
            final = repo.main_files()
            blob = b"\n".join(final.values())
            for tid, (kind, token) in tokens.items():
                if kind == "bad":
                    self.assertFalse(out[tid].landed, (run, tid))
                    self.assertNotIn(token.encode(), blob)
                elif out[tid].landed:
                    self.assertIn(token.encode(), blob, (run, tid))
            self.assertNotIn(b"BAD", blob)
            states = {new for _, new in kv.transitions[1:]}
            self.assertTrue(states <= set(q.approved) | set(), (run, states - set(q.approved)))


if __name__ == "__main__":
    unittest.main()


from cmws_ws.agents.batch import _Member                                    # noqa: E402


def member(repo, name, task=None, result=None, guard=None):
    return _Member(task or Task(name, "x"), result, name, f"{name}: msg", guard)


class BatchInternalsTests(unittest.TestCase):
    """What leaves a batch and why, and the counters, pinned one by one (found by a mutation sweep)."""

    def setUp(self):
        self.repo = Repository.init(MemoryKV(), {"stats.py": b"COUNT = 0\n", "README": b"x\n"})
        self.q = BatchQueue(self.repo, window=0.0)

    def fork(self, name, **files):
        ws = self.repo.fork(name)
        for p, t in files.items():
            ws.write(p.replace("_dot_", "."), t)
        ws.commit(name)
        return ws

    def test_a_clean_branch_joins_and_a_stale_one_leaves_with_its_reason(self):
        a = self.fork("a", stats_dot_py="COUNT = 1\n")
        b = self.repo.fork("b")
        b.write("stats.py", bump(b.read_text("stats.py")))                  # reads, then writes: depends on COUNT = 0
        b.commit("b")
        c = self.fork("c", c_dot_txt="c\n")
        ma, mb, mc = member(self.repo, "a"), member(self.repo, "b"), member(self.repo, "c")
        ws_a = self.repo.resume("a")
        wa = self.repo.resume("a")
        # a changes stats.py without a read, so b (which read it) is stale against the candidate that holds a
        joined, cand = self.q._build([ma, mb, mc], self.repo.head())
        self.assertEqual([m.agent_id for m in joined], ["a", "c"])
        self.assertEqual((mb.reason, ma.reason, mc.reason), ("stale reads", "", ""))
        self.assertEqual(self.q.batch_stats["left"], 1)
        self.assertNotEqual(cand, self.repo.head())

    def test_a_conflicting_branch_leaves(self):
        self.fork("a", stats_dot_py="COUNT = 1\n")
        self.fork("b", stats_dot_py="COUNT = 2\n")
        ma, mb = member(self.repo, "a"), member(self.repo, "b")
        joined, _ = self.q._build([ma, mb], self.repo.head())
        self.assertEqual(([m.agent_id for m in joined], mb.reason), (["a"], "conflict"))
        self.assertEqual(self.q.batch_stats["left"], 1)

    def test_uncommitted_edits_and_work_already_on_main_leave(self):
        ws = self.repo.fork("d")
        ws.write("d.txt", "uncommitted\n")                                   # not committed
        md = member(self.repo, "d")
        done = self.fork("e", e_dot_txt="e\n")
        self.assertTrue(self.repo.submit(done).landed)
        me = member(self.repo, "e")                                          # its work is already on main
        joined, _ = self.q._build([md, me], self.repo.head())
        self.assertEqual(joined, [])
        self.assertEqual((md.reason, me.reason), ("nothing to land or uncommitted edits",) * 2)
        self.assertEqual(self.q.batch_stats["left"], 2)

    def test_a_failed_precheck_leaves_with_the_check_s_words(self):
        acc = parse_checks([{"kind": "contains", "path": "g.txt", "text": "needle"}])
        self.fork("g", g_dot_txt="hay\n")
        mg = member(self.repo, "g", Task("g", "x", (), "", (), (), acc))
        joined, _ = self.q._build([mg], self.repo.head())
        self.assertEqual(joined, [])
        self.assertIn("acceptance check failed", mg.reason)
        self.assertEqual(self.q.batch_stats["left"], 1)

    def test_a_lost_claim_is_a_landing_of_its_own(self):
        self.fork("h", h_dot_txt="h\n")
        mh = member(self.repo, "h", guard=lambda: fence.LOST)
        joined, cand = self.q._build([mh], self.repo.head())
        self.assertEqual((joined, cand), ([], self.repo.head()))
        self.assertEqual((mh.landing.status, mh.landing.detail), ("lost", fence.LOST))
        self.assertEqual(mh.landing.steps[0].stage, "fence")

    def test_a_branch_that_already_contains_main_is_fast_forwarded_not_merged(self):
        self.fork("i", i_dot_txt="i\n")
        mi = member(self.repo, "i")
        joined, cand = self.q._build([mi], self.repo.head())
        self.assertEqual(cand, self.repo.resume("i").head)                  # the branch tip itself, no merge commit
        other = self.fork("j", j_dot_txt="j\n")
        mj = member(self.repo, "j")
        joined, cand2 = self.q._build([member(self.repo, "i"), mj], self.repo.head())
        self.assertEqual(len(self.repo.store.get_commit(cand2).parents), 2)  # the second needed a merge

    def test_process_sets_aside_failed_and_empty_results(self):
        bad = member(self.repo, "k", result=WorkerResult("error", error="boom"))
        empty = member(self.repo, "l", result=WorkerResult("done", "nothing", None))
        failed_no_error = member(self.repo, "m", result=WorkerResult("gave_up"))
        good = self.fork("n", n_dot_txt="n\n")
        mn = member(self.repo, "n", result=WorkerResult("done", "ok", self.repo.resume("n").head))
        self.q._process([bad, empty, failed_no_error, mn])
        self.assertEqual(bad.reason, "boom")
        self.assertEqual(failed_no_error.reason, "gave_up")
        self.assertEqual(empty.reason, "the worker finished without changing anything")
        self.assertEqual(mn.landing.status, "landed")
        self.assertTrue(all(m.landing is None for m in (bad, empty, failed_no_error)))
        self.assertEqual(self.q.batch_stats["batches"], 1)

    def test_require_changes_can_be_off(self):
        q = BatchQueue(self.repo, window=0.0, require_changes=False)
        empty = member(self.repo, "l", result=WorkerResult("done", "nothing", None))
        self.fork("l", l_dot_txt="l\n")
        q._process([empty])
        self.assertEqual(empty.reason, "")

    def test_the_counters_of_a_clean_run_and_of_a_split(self):
        for i in range(4):
            self.fork(f"w{i}", **{f"w{i}_dot_txt": f"{i}\n"})
        q = BatchQueue(self.repo, FnValidator(Counting().check), window=0.0)
        q._process([member(self.repo, f"w{i}") for i in range(4)])
        self.assertEqual(q.batch_stats, {"batches": 1, "landed": 4, "validations": 1, "left": 0, "splits": 0, "rebuilds": 0})
        for i in range(4, 8):
            self.fork(f"w{i}", **{f"w{i}_dot_txt": "BAD\n" if i == 4 else f"{i}\n"})
        ms = [member(self.repo, f"w{i}") for i in range(4, 8)]
        q._process(ms)
        self.assertEqual(q.batch_stats["landed"], 4 + 3)                   # the second half of the split also landed in the batch
        self.assertEqual(q.batch_stats["left"], 1)
        self.assertIn("forbidden", ms[0].reason)
        self.assertGreaterEqual(q.batch_stats["splits"], 1)
        # the first group: 1 validation; the second: whole, first half, its two singles, second half = 5
        self.assertEqual(q.batch_stats["validations"], 1 + 5)

    def test_main_moving_every_time_exhausts_the_retries_and_hands_over_to_the_chain(self):
        self.fork("o", o_dot_txt="o\n")
        q = BatchQueue(self.repo, window=0.0, retries=1)
        real = self.repo.refs.cas
        calls = []

        def cas(name, old, new):
            calls.append(1)
            if len(calls) == 1:
                return type("R", (), {"ok": False})()                       # somebody else moved main
            return real(name, old, new)
        self.repo.refs.cas = cas
        mo = member(self.repo, "o")
        q._process([mo])
        self.assertIsNone(mo.landing)
        self.assertEqual(mo.reason, "main kept moving")
        self.assertEqual(q.batch_stats["rebuilds"], 1)
        landing = BatchQueue.land(q, Task("o", "x"))                        # the ordinary chain still lands it
        self.assertTrue(landing.landed)

    def test_nothing_waits_and_the_leader_flag_is_clear_afterwards(self):
        for i in range(3):
            self.fork(f"p{i}", **{f"p{i}_dot_txt": "p\n"})
        out = land_all(self.q, [Task(f"p{i}", "x") for i in range(3)])
        self.assertTrue(all(l.landed for l in out.values()))
        self.assertFalse(self.q._leading)
        self.assertEqual(self.q._pending, [])
        self.fork("p9", p9_dot_txt="p\n")
        self.assertTrue(self.q.land(Task("p9", "x")).landed)                # a later arrival becomes the leader again

    def test_an_empty_group_does_nothing_and_asks_nobody(self):
        v = Counting()
        q = BatchQueue(self.repo, FnValidator(v.check), window=0.0)
        q._land_group([])
        self.assertEqual((v.runs, q.batch_stats["validations"]), (0, 0))
