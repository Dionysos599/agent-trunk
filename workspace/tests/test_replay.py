"""The git-history replay: each class of concurrent work is recognised on a small repository built for the purpose."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))

import replay                                    # noqa: E402

HAVE_GIT = shutil.which("git") is not None


class Repo:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="replay-"))
        self.run("init", "-q", "-b", "main")
        self.run("config", "user.email", "t@example.com")
        self.run("config", "user.name", "t")
        self.run("config", "commit.gpgsign", "false")

    def run(self, *args, check=True):
        p = subprocess.run(["git", "-C", str(self.dir), *args], capture_output=True, text=True)
        if check and p.returncode != 0:
            raise AssertionError(p.stderr)
        return p

    def write(self, path, data):
        t = self.dir / path
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(data if isinstance(data, bytes) else data.encode())

    def commit(self, msg):
        self.run("add", "-A")
        self.run("commit", "-q", "-m", msg)

    def concurrent(self, name, side_a, side_b, resolve=None):
        """From main: branch a and branch b each apply their edits (dict path -> bytes/None for delete); then merge b into a."""
        self.run("checkout", "-q", "main")
        self.run("checkout", "-q", "-b", f"{name}-a")
        for p, d in side_a.items():
            (self.dir / p).unlink() if d is None else self.write(p, d)
        self.commit(f"{name} a")
        self.run("checkout", "-q", "main")
        self.run("checkout", "-q", "-b", f"{name}-b")
        for p, d in side_b.items():
            (self.dir / p).unlink() if d is None else self.write(p, d)
        self.commit(f"{name} b")
        self.run("checkout", "-q", f"{name}-a")
        m = self.run("merge", "--no-ff", "-q", "-m", f"merge {name}", f"{name}-b", check=False)
        if m.returncode != 0 and resolve:
            for p, d in resolve.items():
                self.write(p, d)
            self.run("add", "-A")
            self.run("commit", "-q", "-m", f"merge {name}")
        elif m.returncode != 0:
            raise AssertionError("merge conflicted and no resolution given")
        self.run("checkout", "-q", "-B", "main")                    # main moves to the merge

    def classes(self):
        return {r["merge"]: r for r in (replay.classify(self.dir, m) for m in replay.merge_commits(self.dir, 100))}


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(shutil.rmtree, self.r.dir, True)
        self.r.write("README", "readme\n")
        self.r.write("counter.py", "COUNT = 0\n")
        self.r.write("reg.py", "# registry\n")
        self.r.write("notes.txt", "one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
        self.r.write("bin.dat", b"\xff\x00\x01")
        self.r.commit("base")

    def only(self):
        c = list(self.r.classes().values())
        self.assertEqual(len(c), 1)
        return c[0]

    def test_disjoint(self):
        self.r.concurrent("d", {"a.txt": "a\n"}, {"b.txt": "b\n"})
        row = self.only()
        self.assertEqual((row["class"], row["common"]), ("disjoint", 0))

    def test_same_file_with_a_clean_merge(self):
        self.r.concurrent("c", {"notes.txt": "ONE\ntwo\nthree\nfour\nfive\nsix\nseven\n"},
                          {"notes.txt": "one\ntwo\nthree\nfour\nfive\nsix\nSEVEN\n"})
        row = self.only()
        self.assertEqual((row["class"], row["clean_same_file"], row["human_edited"]), ("same-file-clean", 1, 0))

    def test_identical_edits_are_the_counter_case(self):
        self.r.concurrent("i", {"counter.py": "COUNT = 1\n"}, {"counter.py": "COUNT = 1\n"})
        row = self.only()
        self.assertEqual((row["class"], row["identical"]), ("identical-edit", 1))

    def test_a_textual_conflict_and_what_append_would_do(self):
        self.r.concurrent("x", {"counter.py": "COUNT = 1\n"}, {"counter.py": "COUNT = 2\n"}, resolve={"counter.py": "COUNT = 3\n"})
        row = self.only()
        self.assertEqual((row["class"], row["conflicts"], row["append_resolvable"]), ("conflict", 1, 0))

    def test_two_appends_conflict_in_git_but_append_resolves_them(self):
        self.r.concurrent("p", {"reg.py": "# registry\nfrom_a\n"}, {"reg.py": "# registry\nfrom_b\n"},
                          resolve={"reg.py": "# registry\nfrom_a\nfrom_b\n"})
        row = self.only()
        self.assertEqual((row["class"], row["conflicts"], row["append_resolvable"]), ("conflict", 1, 1))

    def test_modify_against_delete_is_structural(self):
        self.r.concurrent("m", {"README": None}, {"README": "changed\n"}, resolve={"README": "changed\n"})
        self.assertEqual(self.only()["class"], "structural")

    def test_binary_on_both_sides_is_structural(self):
        self.r.concurrent("b", {"bin.dat": b"\xff\x00\x02"}, {"bin.dat": b"\xff\x00\x03"}, resolve={"bin.dat": b"\xff\x00\x04"})
        self.assertEqual(self.only()["class"], "structural")

    def test_a_person_editing_a_clean_merge_is_noticed(self):
        self.r.concurrent("h", {"notes.txt": "ONE\ntwo\nthree\nfour\nfive\nsix\nseven\n"},
                          {"notes.txt": "one\ntwo\nthree\nfour\nfive\nsix\nSEVEN\n"})
        self.r.write("notes.txt", "ONE\ntwo\nthree\nfour\nfive\nsix\nSEVEN\nextra\n")
        self.r.commit("amend after merge")                          # not part of the merge commit itself: no effect
        self.assertEqual(self.only()["human_edited"], 0)

    def test_a_merge_with_a_hand_edit_in_the_merge_commit(self):
        r = self.r
        r.run("checkout", "-q", "-b", "ea")
        r.write("notes.txt", "ONE\ntwo\nthree\nfour\nfive\nsix\nseven\n")
        r.commit("ea")
        r.run("checkout", "-q", "main")
        r.run("checkout", "-q", "-b", "eb")
        r.write("notes.txt", "one\ntwo\nthree\nfour\nfive\nsix\nSEVEN\n")
        r.commit("eb")
        r.run("checkout", "-q", "ea")
        r.run("merge", "--no-ff", "--no-commit", "-q", "eb")
        r.write("notes.txt", "ONE\ntwo\nthree\nfour\nfive\nsix\nSEVEN\nhand edit\n")
        r.run("add", "-A")
        r.run("commit", "-q", "-m", "merge with an edit")
        row = self.only()
        self.assertEqual((row["class"], row["human_edited"]), ("same-file-clean", 1))

    def test_octopus_merges_and_linear_history_are_left_out(self):
        self.r.write("l.txt", "linear\n")
        self.r.commit("linear commit")
        self.assertEqual(replay.merge_commits(self.r.dir, 100), [])

    def test_files_with_spaces_and_unicode_names(self):
        self.r.concurrent("u", {"dir one/ünï code.txt": "a\n"}, {"dir one/ünï code.txt": "a\n"})
        row = self.only()
        self.assertEqual((row["class"], row["identical"]), ("identical-edit", 1))

    def test_the_summary_and_the_table(self):
        self.r.concurrent("d", {"a.txt": "a\n"}, {"b.txt": "b\n"})
        self.r.concurrent("i", {"counter.py": "COUNT = 1\n"}, {"counter.py": "COUNT = 1\n"})
        self.r.concurrent("p", {"reg.py": "# registry\nfrom_a\n"}, {"reg.py": "# registry\nfrom_b\n"},
                          resolve={"reg.py": "# registry\nfrom_a\nfrom_b\n"})
        s = replay.summarize(list(self.r.classes().values()))
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["classes"], {"disjoint": 1, "identical-edit": 1, "conflict": 1})
        self.assertEqual((s["git_stops"], s["readset_would_refuse"], s["append_would_resolve"], s["conflict_pairs"]), (1, 1, 1, 1))
        t = replay.table(s, "demo")
        self.assertIn("| demo |", t)
        self.assertIn("3 merge commits", t)

    def test_cli_writes_rows(self):
        self.r.concurrent("d", {"a.txt": "a\n"}, {"b.txt": "b\n"})
        out = Path(tempfile.mkdtemp()) / "rows.jsonl"
        self.addCleanup(shutil.rmtree, out.parent, True)
        self.assertEqual(replay.main(["--repo", str(self.r.dir), "--out", str(out), "--table"]), 0)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual([r["class"] for r in rows], ["disjoint"])
        self.assertEqual(rows[0]["repo"], self.r.dir.name)

    def test_a_repository_that_does_not_exist_raises(self):
        with self.assertRaises(RuntimeError):
            replay.merge_commits(Path("/nonexistent/nowhere"), 5)


if __name__ == "__main__":
    unittest.main()
