"""The stress harness: the workload is exact, the checks catch every kind of damage, and a small run on the real cluster (with a
replica and an agent process killed) ends correct."""

import json
import os
import random
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))

from cmws_ws.kv import MemoryKV                              # noqa: E402
from cmws_ws.objects import Commit                           # noqa: E402
from cmws_ws.repo import Repository                          # noqa: E402
from cmws_ws.serial import MemFS                             # noqa: E402

from stress import checks as C                               # noqa: E402
from stress import runner as R                               # noqa: E402
from stress import trace as T                                # noqa: E402
from stress import workload as W                             # noqa: E402


def serial_files(tasks):
    fs = MemFS(W.initial_files())
    for t in tasks:
        t.apply(fs)
    return {p: d.encode() for p, d in fs.files().items()}


class WorkloadTests(unittest.TestCase):
    def test_the_default_run_has_160_tasks_and_1600_edits_in_the_stated_mix(self):
        tasks = W.generate(1)
        self.assertEqual((len(tasks), W.edit_total(tasks)), (160, 1600))
        self.assertEqual(dict(Counter(t.kind for t in tasks)), {"own": 88, "append": 40, "counter": 19, "version": 13})

    def test_a_seed_gives_the_same_run_and_another_seed_a_different_one(self):
        a, b, c = W.generate(5), W.generate(5), W.generate(6)
        self.assertEqual([(t.id, t.kind, t.tokens, t.increments) for t in a], [(t.id, t.kind, t.tokens, t.increments) for t in b])
        self.assertNotEqual([t.kind for t in a], [t.kind for t in c])

    def test_every_written_line_is_unique(self):
        toks = [x for t in W.generate(3) for x in t.tokens]
        self.assertEqual(len(toks), len(set(toks)))

    def test_other_sizes(self):
        for n in (1, 7, 50, 333):
            tasks = W.generate(2, n)
            self.assertEqual(len(tasks), n)
        self.assertEqual(W.generate(1, 0), [])
        self.assertEqual(W.edit_total(W.generate(1, 20, edits=3)), 60)

    def test_replaying_the_tasks_serially_gives_exactly_the_expected_answer(self):
        tasks = W.generate(4, 80)
        files = serial_files(tasks)
        got = C.parse_counters(files)
        exp = W.expected(tasks)
        self.assertEqual((got["counters"], got["total"], got["version"]), (exp["counters"], exp["total"], exp["version"]))
        r = C.edits_lost(files, tasks)
        self.assertEqual((r["edits_lost"], r["edits_duplicated"], r["lines_unknown"]), (0, 0, 0))
        self.assertEqual(W.invariant(files), "")

    def test_the_order_of_tasks_does_not_change_the_answer(self):
        tasks = W.generate(4, 60)
        shuffled = list(tasks)
        random.Random(1).shuffle(shuffled)
        a, b = serial_files(tasks), serial_files(shuffled)
        for p in a:
            if p.startswith(("hot/",)) or p == "README":
                self.assertEqual(a[p], b[p], p)                              # counters and version commute
        self.assertEqual(sorted(C.count_lines(a)), sorted(C.count_lines(b)))

    def test_tasks_declare_narrow_paths_for_the_collision_kinds(self):
        tasks = W.generate(1, 40)
        decl = R.to_tasks(tasks, True, True)
        for t, d in zip(tasks, decl):
            if t.kind == "own":
                self.assertEqual(d.paths, t.paths)
            else:
                self.assertEqual(d.paths, (f".marker/{t.id}",))
        self.assertEqual(R.to_tasks(tasks, True, True, honest=True)[0].paths, tasks[0].paths)
        self.assertTrue(all(d.accept for t, d in zip(tasks, decl) if t.kind in ("counter", "version")))
        self.assertTrue(all(not d.accept for d in R.to_tasks(tasks, True, False)))


class InvariantTests(unittest.TestCase):
    def files(self, **over):
        f = {p: t.encode() for p, t in W.initial_files().items()}
        f.update({k.replace("_dot_", "."): (v.encode() if isinstance(v, str) else v) for k, v in over.items() if v is not None})
        return f

    def test_the_start_state_is_valid(self):
        self.assertEqual(W.invariant(self.files()), "")

    def test_each_kind_of_damage(self):
        base = "".join(f"C{k} = 0\n" for k in range(W.COUNTERS))
        cases = {
            "malformed": ("hot/counters.txt", "C0 = 0\n"),
            "total": ("hot/counters.txt", base.replace("C0 = 0", "C0 = 1") + "TOTAL = 0\n"),
            "version": ("hot/version.txt", "VERSION = x\n"),
            "twice": ("own/t.txt", "tok a 1\ntok a 1\n"),
            "markers": ("own/t.txt", "<<<<<<< ours\nx\n"),
            "shared twice": ("shared/reg_0.txt", "tok a 1\ntok a 1\n"),
        }
        for name, (path, text) in cases.items():
            self.assertNotEqual(W.invariant(self.files(**{path.replace(".", "_dot_"): text})), "", name)

    def test_missing_files(self):
        f = self.files()
        del f["hot/counters.txt"]
        self.assertIn("missing", W.invariant(f))
        f = self.files()
        del f["hot/version.txt"]
        self.assertIn("version", W.invariant(f))

    def test_binary_files_outside_the_checked_folders_are_ignored(self):
        self.assertEqual(W.invariant(self.files(blob_dot_bin=b"\xff\xfe")), "")


class VerdictTests(unittest.TestCase):
    """Run a small correct run, then damage the result in every way the checks are meant to catch."""

    def setUp(self):
        self.tasks = W.generate(2, 30)
        self.kv = MemoryKV()
        self.repo = R.initial_repo(self.kv)
        self.initial = W.initial_files()

    def land_serially(self, tasks=None, skip_claim=()):
        run = "stress"
        for t in tasks or self.tasks:
            ws = self.repo.fork(f"{run}-{t.id}")
            t.apply(ws)
            ws.commit(f"{run}-{t.id}")
            self.assertTrue(self.repo.submit(ws).landed)
        # complete the claims the way the scheduler does
        from cmws_ws.claims import TaskClaims
        claims = TaskClaims(self.kv, ttl=1e9, prefix=f"runs/{run}/")
        for t in self.tasks:
            got = claims.claim(f"{run}-{t.id}", "o")
            if t.id not in skip_claim:
                claims.complete(f"{run}-{t.id}", "o", got.epoch, {"status": "done", "reason": "", "commit": None})

    def verdict(self, **kw):
        return C.verdict(self.repo, self.kv, "stress", self.tasks, self.initial, **kw)

    def damage(self, fn):
        self._n = getattr(self, "_n", 0) + 1
        ws = self.repo.fork(f"tamper{self._n}")
        files = {p: d.decode() for p, d in self.repo.main_files().items()}
        fn(files)
        for p, d in files.items():
            ws.write(p, d)
        for p in set(self.repo.main_files()) - set(files):
            ws.delete(p)
        ws.commit("tamper")
        self.assertTrue(self.repo.submit(ws).landed)

    def test_a_correct_serial_run_is_correct_in_every_respect(self):
        self.land_serially()
        v = self.verdict()
        self.assertTrue(v["correct"], {k: v[k] for k in ("edits_lost", "edits_duplicated", "claims_done", "serial_ok", "chain", "invariant_violations")})
        self.assertEqual((v["serial_landed"], v["claims_done"], v["state_invariant_ok"], v["chain"]), (30, 30, True, True))

    def test_a_dropped_line_is_a_lost_edit_and_silent_when_the_task_was_reported_done(self):
        self.land_serially()
        victim = next(t for t in self.tasks if t.kind == "own")
        self.damage(lambda f: f.update({f"own/{victim.id}.txt": f[f"own/{victim.id}.txt"].replace(victim.tokens[0] + "\n", "")}))
        v = self.verdict(landed={t.id for t in self.tasks})
        self.assertFalse(v["correct"])
        self.assertEqual((v["lines_missing"], v["edits_lost_silent"], v["edits_pending"]), (1, 1, 0))
        self.assertFalse(v["serial_ok"])
        v = self.verdict(landed={t.id for t in self.tasks if t.id != victim.id})
        self.assertEqual((v["edits_lost_silent"], v["edits_pending"]), (0, 1))          # a task handed to a person is not silent

    def test_a_line_that_appears_twice(self):
        self.land_serially()
        victim = next(t for t in self.tasks if t.kind == "append")
        path = f"shared/reg_{[k for k in range(4) if victim.tokens[0] in self.repo.main_files()[f'shared/reg_{k}.txt'].decode()][0]}.txt"
        self.damage(lambda f: f.update({path: f[path] + victim.tokens[0] + "\n"}))
        v = self.verdict()
        self.assertEqual(v["lines_twice"], 1)
        self.assertFalse(v["correct"])
        self.assertFalse(v["state_invariant_ok"])

    def test_a_counter_one_short_and_one_over(self):
        self.land_serially()
        self.damage(lambda f: f.update({"hot/counters.txt": f["hot/counters.txt"].replace("C0 = ", "C0 = 9").replace("TOTAL = ", "TOTAL = 9")}))
        v = self.verdict()
        self.assertGreater(v["increments_extra"], 0)
        self.assertFalse(v["correct"])

    def test_a_missing_increment(self):
        self.land_serially()

        def short(f):
            m = {k: int(v) for k, v in __import__("re").findall(r"^(C\d+|TOTAL) = (\d+)$", f["hot/counters.txt"], __import__("re").M)}
            k = next(k for k in m if k != "TOTAL" and m[k] > 0)
            f["hot/counters.txt"] = "".join(f"{n} = {m[n] - (1 if n in (k, 'TOTAL') else 0)}\n" for n in sorted(m, key=lambda x: (x == "TOTAL", x)))
        self.damage(short)
        v = self.verdict()
        self.assertEqual(v["increments_lost"], 1)
        self.assertFalse(v["correct"])

    def test_a_missing_version_bump(self):
        self.land_serially()
        n = W.expected(self.tasks)["version"]
        self.damage(lambda f: f.update({"hot/version.txt": f"VERSION = {n - 1}\n"}))
        v = self.verdict()
        self.assertEqual((v["versions_lost"], v["correct"]), (1, False))

    def test_a_task_nobody_asked_for_leaves_an_unknown_line(self):
        self.land_serially()
        self.damage(lambda f: f.update({"own/stranger.txt": "tok stranger 0\n"}))
        v = self.verdict()
        self.assertEqual(v["lines_unknown"], 1)
        self.assertFalse(v["correct"])

    def test_a_bad_state_in_history_is_found_even_if_the_final_tree_is_fine(self):
        self.land_serially()
        good = {p: d.decode() for p, d in self.repo.main_files().items()}
        self.damage(lambda f: f.update({"hot/version.txt": "VERSION = garbage\n"}))     # main briefly held a broken state ...
        self.damage(lambda f: f.update({"hot/version.txt": good["hot/version.txt"]}))   # ... and was repaired
        v = self.verdict()
        self.assertFalse(v["state_invariant_ok"])
        self.assertTrue(v["invariant_violations"])
        self.assertFalse(v["correct"])

    def test_a_task_whose_claim_never_completed_is_not_a_correct_run(self):
        self.land_serially(skip_claim={self.tasks[0].id})
        v = self.verdict()
        self.assertEqual((v["claims_done"], v["claims_expected"]), (29, 30))
        self.assertIn(self.tasks[0].id, v["claims_not_done"])
        self.assertFalse(v["correct"])
        self.assertEqual(v["edits_lost"], 0)                                     # nothing was lost: the accounting is what is wrong

    def test_a_branch_landed_twice_by_hand_breaks_serializability(self):
        self.land_serially()
        t = next(t for t in self.tasks if t.kind == "counter")
        ws = self.repo.fork("stress-" + t.id + "-redo1")                    # the same task applied again on top of the final state
        t.apply(ws)
        ws.commit("again")
        self.assertTrue(self.repo.submit(ws, allow_stale=True).landed)
        v = self.verdict()
        self.assertFalse(v["correct"])
        self.assertGreater(v["increments_extra"] + v["lines_twice"], 0)

    def test_without_the_serial_check(self):
        self.land_serially()
        v = self.verdict(serial=False)
        self.assertIsNone(v["serial_ok"])
        self.assertTrue(v["correct"])

    def test_the_author_to_task_mapping(self):
        f = C.task_of("s1")
        self.assertEqual((f("s1-t007"), f("s1-t007-redo2"), f("s1-t007-hedge"), f("system"), f("other-t007")), ("t007", "t007", "t007", None, None))


class RunnerTests(unittest.TestCase):
    def test_the_full_system_is_correct_in_memory_at_32_agents(self):
        for strat in ("full", "full-batch"):
            r = R.run_inprocess(strat, seed=3, tasks=80, agents=32, latency=0.02)
            self.assertTrue(r["correct"], (strat, {k: r[k] for k in ("edits_lost", "edits_duplicated", "claims_done", "serial_ok")}))
            self.assertEqual((r["landed"], r["edits_lost_silent"], r["edits_duplicated"]), (80, 0, 0))

    def test_the_baselines_fail_their_checks_where_the_levels_say_they_must(self):
        naive = R.run_naive(3, 80, 32, 0.02)
        self.assertFalse(naive["correct"])
        self.assertGreater(naive["edits_lost"], 0)
        bad = [R.run_inprocess("gitlike", seed=s, tasks=80, agents=32, latency=0.02) for s in (1, 2, 3)]
        self.assertTrue(all(not r["correct"] for r in bad))
        self.assertTrue(any(r["edits_lost_silent"] > 0 or r["serial_ok"] is False for r in bad))
        for r in (R.run_inprocess("snapshot", seed=s, tasks=80, agents=32, latency=0.02) for s in (1, 2)):
            self.assertEqual((r["edits_lost_silent"], r["edits_duplicated"]), (0, 0))        # 1.0 never loses silently ...
            self.assertTrue(r["serial_ok"])                                                  # ... and stays serializable

    def test_a_durable_worker_does_not_apply_a_task_twice(self):
        tasks = W.generate(1, 3)
        repo = R.initial_repo(MemoryKV())
        w = R.StressWorker(tasks, 0.0)
        t = next(t for t in tasks if t.kind == "append")
        ws = repo.fork(f"s-{t.id}")
        from cmws_ws.agents import Task
        first = w.run(Task(f"s-{t.id}", "x"), repo, f"s-{t.id}")
        again = w.run(Task(f"s-{t.id}", "x"), repo, f"s-{t.id}")
        self.assertEqual(again.summary, f"resumed {t.id}")
        self.assertEqual(again.commit, first.commit)
        self.assertEqual(repo.resume(f"s-{t.id}").read_text(f"shared/reg_{t.paths[0][-5]}.txt").count(t.tokens[0]), 1)

    def test_the_strategy_table(self):
        self.assertEqual(set(R.STRATEGIES), {"gitlike", "snapshot", "full-redo", "full", "full-batch"})
        with self.assertRaises(KeyError):
            R.run_inprocess("nope", tasks=1)


class TraceTests(unittest.TestCase):
    def data(self):
        return {"model": "m", "task": [{"seconds": 4.0, "tokens": 1000}, {"seconds": 8.0, "tokens": 3000}], "layer2_tokens": [200.0],
                "redo_tokens": [4000.0], "tokens_per_second": 300.0}

    def test_samples_come_from_the_recorded_values_and_repeat_for_a_seed(self):
        a, b = T.Trace(self.data(), seed=5), T.Trace(self.data(), seed=5)
        ids = [f"t{i:03d}" for i in range(30)]
        xs = [a.task_latency(i) for i in ids]
        self.assertEqual(xs, [b.task_latency(i) for i in ids])
        self.assertTrue(set(xs) <= {4.0, 8.0})
        self.assertEqual(len(set(xs)), 2)
        self.assertTrue(all(a.task_tokens(i) in (1000, 3000) for i in ids))

    def test_compress_scales_durations_only(self):
        t = T.Trace(self.data(), seed=1, compress=0.1)
        ids = [f"t{i:03d}" for i in range(40)]
        self.assertEqual({round(t.task_latency(i), 6) for i in ids}, {0.4, 0.8})
        self.assertIn(t.task_tokens("t001"), (1000, 3000))                          # tokens are not compressed

    def test_layer_two_tokens_split_into_prompt_and_completion(self):
        p, c = T.Trace(self.data()).layer2_tokens()
        self.assertEqual((p, c), (180, 20))

    def test_an_empty_trace_is_refused(self):
        with self.assertRaises(ValueError):
            T.Trace({"model": "x", "task": []})

    def test_the_recorded_traces_can_be_built(self):
        d = T.build("eco")
        self.assertEqual(d["model"], "eco")
        if d["task"]:
            self.assertGreater(d["tokens_per_second"], 0)
            self.assertTrue(all(x > 0 for x in d["layer2_tokens"]))

    def test_a_task_gets_the_same_sample_in_every_instance_and_different_tasks_differ(self):
        d = {"model": "m", "task": [{"seconds": float(i), "tokens": 100 * i} for i in range(1, 9)], "layer2_tokens": [1.0]}
        a, b = T.Trace(d, seed=3), T.Trace(d, seed=3)

        class Tk:
            def __init__(self, i):
                self.id = i
        ids = [f"t{i:03d}" for i in range(40)]
        self.assertEqual([a.task_latency(Tk(i)) for i in ids], [b.task_latency(Tk(i)) for i in ids])
        self.assertEqual([a.task_tokens(Tk(i)) for i in ids], [b.task_tokens(Tk(i)) for i in ids])
        self.assertEqual(a.task_latency(Tk("t001")), a.task_latency(Tk("t001")))              # asked twice (a redo): the same
        self.assertGreater(len({a.task_latency(Tk(i)) for i in ids}), 3)
        other = T.Trace(d, seed=4)
        self.assertNotEqual([a.task_latency(Tk(i)) for i in ids], [other.task_latency(Tk(i)) for i in ids])
        self.assertAlmostEqual(a.task_latency(Tk("t001")) * 100, T.Trace(d, seed=3, compress=100).task_latency(Tk("t001")))

    def test_the_first_slow_run_after_loading_a_model_is_dropped(self):
        from unittest import mock
        rows = [{"suite": "disjoint", "seconds": s, "tokens": 4000, "tasks": 4} for s in (19.4, 5.9, 6.8, 4.9)]
        with mock.patch.object(T, "_rows", side_effect=lambda name: rows if name == "ollama_quality.jsonl" else []):
            d = T.build("quality")
        self.assertEqual(sorted(t["seconds"] for t in d["task"]), [4.9, 5.9, 6.8])
        self.assertTrue(all(t["tokens"] == 1000 for t in d["task"]))

    def test_load_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.json"
            p.write_text(json.dumps({"m": self.data()}))
            self.assertEqual(T.Trace.load(str(p), "m", seed=1).mean_task_tokens(), 2000)
            with self.assertRaises(KeyError):
                T.Trace.load(str(p), "other")


@unittest.skipIf(os.environ.get("CMWS_SKIP_INTEGRATION"), "integration tests skipped")
class ClusterTests(unittest.TestCase):
    gw = None

    @classmethod
    def setUpClass(cls):
        from stress import cluster
        cls.cluster = cluster
        try:
            cls.gw = cluster.Gateway().start()
        except RuntimeError as e:
            raise unittest.SkipTest(str(e))

    @classmethod
    def tearDownClass(cls):
        if cls.gw:
            cls.gw.stop()

    def test_a_small_run_on_the_real_cluster_is_correct(self):
        r = self.cluster.run_cluster(self.gw, "full-batch", seed=11, tasks=40, procs=2, threads=4, faults=False, timeout=120)
        self.assertTrue(r["correct"], {k: r[k] for k in ("landed", "edits_lost", "edits_duplicated", "claims_not_done", "serial_ok")})
        self.assertEqual((r["landed"], r["agents"]), (40, 8))
        self.assertEqual(r["agent_runs"] >= 40, True)                           # the processes reported their own counters
        self.assertGreaterEqual(r["tokens_total"], 40 * 1000)
        self.assertEqual(r["layer2"]["settled"] <= r["layer2"]["tried"], True)

    def test_with_a_replica_and_an_agent_process_killed(self):
        r = self.cluster.run_cluster(self.gw, "full-batch", seed=12, tasks=80, procs=2, threads=4, faults=True, timeout=180, claim_ttl=3.0)
        self.assertTrue(r["correct"], {k: r[k] for k in ("landed", "edits_lost", "edits_duplicated", "claims_not_done", "serial_ok", "fault_log")})
        self.assertGreaterEqual(r["agents_killed"], 4)
        self.assertTrue(any("SIGKILL" in x for x in r["fault_log"]))

    def test_the_gitlike_level_fails_its_checks_on_the_cluster_too(self):
        rs = [self.cluster.run_cluster(self.gw, "gitlike", seed=s, tasks=60, procs=2, threads=8, faults=False, timeout=120) for s in (21, 22)]
        self.assertTrue(all(not r["correct"] for r in rs))


if __name__ == "__main__":
    unittest.main()


class RealAgentValidationTests(unittest.TestCase):
    """The pieces of the real-agent validation that need no model."""

    def test_the_mini_workload_and_its_descriptions(self):
        from stress import real_agents as RA
        mini = RA.mini_tasks()
        self.assertEqual(dict(Counter(t.kind for t in mini)), {"own": 5, "append": 3, "counter": 2, "version": 2})
        self.assertEqual(len({t.id for t in mini}), 12)
        for t in mini:
            d = RA.describe(t)
            if t.kind in ("own", "append"):
                self.assertTrue(all(tok in d for tok in t.tokens))
            if t.kind == "counter":
                self.assertIn("TOTAL", d)
            if t.kind == "version":
                self.assertIn("VERSION", d)
        tasks = RA.real_tasks(mini)
        self.assertEqual([x.id for x in tasks], [t.id for t in mini])
        self.assertTrue(all(x.accept for x, t in zip(tasks, mini) if t.kind in ("counter", "version")))

    def test_the_replay_of_recorded_tasks_is_correct_and_costs_what_was_recorded(self):
        from stress import real_agents as RA
        mini = RA.mini_tasks()
        recorded = {t.id: {"seconds": 0.01, "tokens": 500} for t in mini}
        one = RA.run_replay(mini, 1, recorded)
        four = RA.run_replay(mini, 4, recorded)
        self.assertTrue(one["correct"] and four["correct"])
        self.assertEqual(one["tokens"], 12 * 500)
        self.assertGreaterEqual(four["tokens"], 12 * 500)                      # redone tasks and layer 2 answers add to it
        self.assertEqual((one["landed"], four["landed"]), (12, 12))

    def test_a_scripted_agent_replays_a_recorded_agent_exactly_in_one_agent_mode(self):
        from stress import real_agents as RA
        rec = RA.Recording(R.StressWorker(RA.mini_tasks(), 0.0, lambda t: 7, 1))
        self.assertEqual(rec.rows, {})
