"""Merge policies (relative / converge / append): the rule language, the text merge, the tree merge, stale-read
relaxation and landing through the repository, with property tests over random edits."""

import random
import unittest

from cmws_ws.kv import MemoryKV
from cmws_ws.objects import Commit
from cmws_ws.policy import (APPEND, CONVERGE, FILE_NAME, MAX_FILE_BYTES, MAX_RULES, POLICIES, RELATIVE, PolicyError,
                            PolicyTable, matches, parse_rules, project_rules, rules_from_declaration,
                            rules_to_declaration, validate_rule)
from cmws_ws.repo import Repository
from cmws_ws.textmerge import hunks, merge3, merge_text, split_lines
from cmws_ws.treemerge import merge_trees


class RuleParsingTests(unittest.TestCase):
    def test_basic_file(self):
        rules, problems = parse_rules("registry.py append\n*.md converge\n# a comment\n\n  src/x.py   relative  \n")
        self.assertEqual(rules, [("registry.py", "append"), ("*.md", "converge"), ("src/x.py", "relative")])
        self.assertEqual(problems, [])

    def test_bad_lines_are_skipped_and_reported_never_fatal(self):
        rules, problems = parse_rules("good.py append\nonlyonetoken\nbad.py sometimes\n../up.py append\n")
        self.assertEqual(rules, [("good.py", "append")])
        self.assertEqual(len(problems), 3)
        self.assertIn("line 2", problems[0])
        self.assertIn("sometimes", problems[1])
        self.assertIn("line 4", problems[2])

    def test_crlf_bom_and_tabs(self):
        rules, _ = parse_rules("﻿a.py\tappend\r\nb.py converge\r\n")
        self.assertEqual(rules, [("a.py", "append"), ("b.py", "converge")])

    def test_pattern_with_spaces_uses_the_last_token_as_policy(self):
        rules, _ = parse_rules("my notes/todo list.txt append")
        self.assertEqual(rules, [("my notes/todo list.txt", "append")])

    def test_comment_only_after_a_line_start(self):
        rules, problems = parse_rules("a#b.py append\n#c.py append")
        self.assertEqual(rules, [("a#b.py", "append")])
        self.assertEqual(problems, [])

    def test_empty_and_whitespace_input(self):
        self.assertEqual(parse_rules(""), ([], []))
        self.assertEqual(parse_rules("\n \n\t\n"), ([], []))

    def test_policy_names_are_case_sensitive(self):
        rules, problems = parse_rules("a.py Append")
        self.assertEqual(rules, [])
        self.assertEqual(len(problems), 1)

    def test_leading_slash_is_ignored(self):
        self.assertEqual(parse_rules("/a/b.py append")[0], [("a/b.py", "append")])
        self.assertEqual(parse_rules("///a.py append")[0], [("a.py", "append")])

    def test_only_a_slash_is_not_a_pattern(self):
        rules, problems = parse_rules("/ append")
        self.assertEqual(rules, [])
        self.assertEqual(len(problems), 1)

    def test_too_many_rules_are_cut_off_and_reported(self):
        text = "\n".join(f"f{i}.py append" for i in range(MAX_RULES + 5))
        rules, problems = parse_rules(text)
        self.assertEqual(len(rules), MAX_RULES)
        self.assertTrue(problems)

    def test_unicode_patterns(self):
        rules, _ = parse_rules("日本語/*.txt append")
        self.assertEqual(rules, [("日本語/*.txt", "append")])

    def test_policies_constant(self):
        self.assertEqual(set(POLICIES), {RELATIVE, CONVERGE, APPEND})


class DeclarationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(rules_from_declaration({"a.py": "append", "/b.py": "converge"}),
                         (("a.py", "append"), ("b.py", "converge")))
        self.assertEqual(rules_from_declaration(None), ())
        self.assertEqual(rules_from_declaration({}), ())

    def test_round_trip(self):
        rules = (("a.py", "append"), ("*.md", "relative"))
        self.assertEqual(rules_from_declaration(rules_to_declaration(rules)), rules)

    def test_invalid_declarations_raise_policy_error(self):
        for bad in ({"a.py": "sometimes"}, {"a.py": None}, {"a.py": 1}, {"a.py": True}, {"": "append"}, {"   ": "append"},
                    {"../a.py": "append"}, {"a/../b": "append"}, {"a\nb": "append"}, {"a\0b": "append"}, {1: "append"},
                    ["a.py"], "a.py append", 5):
            with self.assertRaises(PolicyError, msg=repr(bad)):
                rules_from_declaration(bad)

    def test_too_many_rules(self):
        with self.assertRaises(PolicyError):
            rules_from_declaration({f"f{i}": "append" for i in range(MAX_RULES + 1)})

    def test_policy_error_is_a_value_error(self):
        self.assertTrue(issubclass(PolicyError, ValueError))

    def test_validate_rule(self):
        self.assertEqual(validate_rule(" a.py ", "append"), ("a.py", "append"))


class MatchingTests(unittest.TestCase):
    def test_a_pattern_without_slash_matches_the_name_at_any_depth(self):
        self.assertTrue(matches("*.md", "README.md"))
        self.assertTrue(matches("*.md", "docs/deep/README.md"))
        self.assertFalse(matches("*.md", "README.md.bak"))
        self.assertTrue(matches("registry.py", "pkg/registry.py"))

    def test_a_pattern_with_slash_matches_the_whole_path(self):
        self.assertTrue(matches("docs/*.md", "docs/a.md"))
        self.assertTrue(matches("docs/*.md", "docs/x/a.md"))              # * crosses /
        self.assertFalse(matches("docs/*.md", "other/docs/a.md"))
        self.assertFalse(matches("docs/a.md", "docs/a.mdx"))

    def test_question_mark_and_classes(self):
        self.assertTrue(matches("v?.txt", "v1.txt"))
        self.assertFalse(matches("v?.txt", "v10.txt"))
        self.assertTrue(matches("[ab].txt", "a.txt"))

    def test_case_sensitive(self):
        self.assertFalse(matches("README.md", "readme.md"))

    def test_dotfiles_match_a_star(self):
        self.assertTrue(matches("*", ".env"))


class TableTests(unittest.TestCase):
    def test_default_is_relative(self):
        t = PolicyTable()
        self.assertEqual(t.policy_for("anything"), RELATIVE)
        self.assertFalse(t)

    def test_last_matching_rule_wins(self):
        t = PolicyTable([("*.py", "append"), ("keep.py", "relative"), ("*.py", "converge")])
        self.assertEqual(t.policy_for("a.py"), CONVERGE)
        self.assertEqual(t.policy_for("keep.py"), CONVERGE)
        t = PolicyTable([("*.py", "converge"), ("keep.py", "relative")])
        self.assertEqual(t.policy_for("keep.py"), RELATIVE)
        self.assertEqual(t.policy_for("x.py"), CONVERGE)

    def test_truthiness_needs_a_relaxing_rule(self):
        self.assertFalse(PolicyTable([("a", "relative")]))
        self.assertTrue(PolicyTable([("a", "append")]))

    def test_no_match_means_relative(self):
        self.assertEqual(PolicyTable([("a.py", "append")]).policy_for("b.py"), RELATIVE)


class ProjectFileTests(unittest.TestCase):
    def tree_with(self, name, data):
        repo = Repository.init(MemoryKV(), {name: data, "x.py": b"x\n"})
        return repo.store, repo.store.get_commit(repo.head()).tree

    def test_read_from_a_tree(self):
        store, tree = self.tree_with(FILE_NAME, b"x.py append\n")
        self.assertEqual(project_rules(store, tree), [("x.py", "append")])
        self.assertEqual(PolicyTable.for_tree(store, tree, [("x.py", "relative")]).policy_for("x.py"), RELATIVE)

    def test_missing_file_means_no_rules(self):
        store, tree = self.tree_with("other.txt", b"")
        self.assertEqual(project_rules(store, tree), [])
        self.assertEqual(project_rules(store, None), [])

    def test_binary_or_huge_file_is_ignored(self):
        store, tree = self.tree_with(FILE_NAME, b"\xff\xfe\x00 append")
        self.assertEqual(project_rules(store, tree), [])
        store, tree = self.tree_with(FILE_NAME, b"a.py append\n" + b"#" * MAX_FILE_BYTES)
        self.assertEqual(project_rules(store, tree), [])

    def test_a_directory_with_that_name_is_ignored(self):
        repo = Repository.init(MemoryKV(), {FILE_NAME + "/inner": b"a append"})
        tree = repo.store.get_commit(repo.head()).tree
        self.assertEqual(project_rules(repo.store, tree), [])

    def test_the_file_in_a_subdirectory_is_not_the_project_file(self):
        repo = Repository.init(MemoryKV(), {"sub/" + FILE_NAME: b"a append"})
        self.assertEqual(project_rules(repo.store, repo.store.get_commit(repo.head()).tree), [])


# ---- text level ------------------------------------------------------------------------------------------

class AppendTextTests(unittest.TestCase):
    def test_two_appends_keep_both_theirs_first(self):
        r = merge_text("a\n", "a\nours\n", "a\ntheirs\n", append=True)
        self.assertEqual(r.text, "a\ntheirs\nours\n")
        self.assertTrue(r.clean)
        self.assertEqual(r.appended, 1)

    def test_default_is_still_a_conflict(self):
        r = merge_text("a\n", "a\nours\n", "a\ntheirs\n")
        self.assertFalse(r.clean)
        self.assertEqual(r.appended, 0)

    def test_insert_in_the_middle(self):
        r = merge_text("a\nb\n", "a\nX\nb\n", "a\nY\nb\n", append=True)
        self.assertEqual(r.text, "a\nY\nX\nb\n")

    def test_insert_at_the_top(self):
        r = merge_text("a\n", "O\na\n", "T\na\n", append=True)
        self.assertEqual(r.text, "T\nO\na\n")

    def test_into_an_empty_base(self):
        r = merge_text("", "o\n", "t\n", append=True)
        self.assertEqual(r.text, "t\no\n")
        r = merge_text(None, "o\n", "t\n", append=True)
        self.assertEqual(r.text, "t\no\n")

    def test_missing_final_newline_does_not_join_lines(self):
        r = merge_text("a\n", "a\nours", "a\ntheirs", append=True)
        self.assertEqual(r.text, "a\ntheirs\nours")
        r = merge_text("a\n", "a\nours\n", "a\ntheirs", append=True)
        self.assertEqual(r.text, "a\ntheirs\nours\n")

    def test_identical_insertions_are_agreement_not_duplicated(self):
        r = merge_text("a\n", "a\nsame\n", "a\nsame\n", append=True)
        self.assertEqual(r.text, "a\nsame\n")
        self.assertEqual(r.appended, 0)

    def test_modification_against_insertion_stays_a_conflict(self):
        r = merge_text("a\nb\n", "a\nb\nours\n", "a\nB\n", append=True)       # theirs changed the last line, ours appended after it
        self.assertFalse(r.clean)

    def test_deletion_against_insertion_stays_a_conflict(self):
        r = merge_text("a\nb\n", "a\nb\nours\n", "a\n", append=True)
        self.assertFalse(r.clean)

    def test_counter_style_edits_are_not_appends(self):
        r = merge_text("COUNT = 0\n", "COUNT = 1\n", "COUNT = 2\n", append=True)
        self.assertFalse(r.clean)

    def test_same_line_changed_two_ways_stays_a_conflict(self):
        self.assertFalse(merge_text("a\n", "b\n", "c\n", append=True).clean)

    def test_a_clean_merge_is_unchanged_by_append(self):
        for base, ours, theirs in (("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n"), ("a\n", "a\n", "a\nz\n"), ("", "", "x\n")):
            self.assertEqual(merge_text(base, ours, theirs).text, merge_text(base, ours, theirs, append=True).text)

    def test_two_separate_append_places_both_merge(self):
        base = "h1\n\nm\n\nt\n"
        ours = "h1\nOA\n\nm\n\nt\nOB\n"
        theirs = "h1\nTA\n\nm\n\nt\nTB\n"
        r = merge_text(base, ours, theirs, append=True)
        self.assertTrue(r.clean)
        self.assertEqual(r.text, "h1\nTA\nOA\n\nm\n\nt\nTB\nOB\n")
        self.assertEqual(r.appended, 2)

    def test_append_with_a_conflict_elsewhere_keeps_the_conflict(self):
        base = "a\nb\nc\n"
        ours = "a\nX\nc\nOURS_END\n"
        theirs = "a\nY\nc\nTHEIRS_END\n"
        r = merge_text(base, ours, theirs, append=True)
        self.assertEqual(len(r.conflicts), 1)
        self.assertIn("OURS_END", r.text)
        self.assertIn("THEIRS_END", r.text)

    def test_crlf_lines(self):
        r = merge_text("a\r\n", "a\r\nours\r\n", "a\r\ntheirs\r\n", append=True)
        self.assertEqual(r.text, "a\r\ntheirs\r\nours\r\n")

    def test_long_runs_of_blank_lines(self):
        r = merge_text("a\n", "a\n\n\n\nO\n", "a\n\nT\n", append=True)
        self.assertEqual(r.text, "a\n\nT\n\n\n\nO\n")

    def test_labels_are_accepted(self):
        r = merge_text("a\n", "b\n", "c\n", ours_label="x", theirs_label="y", append=True)
        self.assertIn("<<<<<<< x", r.text)
        self.assertIn(">>>>>>> y", r.text)


def _random_lines(rng, n, alphabet="abcdefgh"):
    return [f"{rng.choice(alphabet)}{rng.randint(0, 99)}\n" for _ in range(n)]


class AppendPropertyTests(unittest.TestCase):
    def test_random_pure_insertions_at_the_same_place(self):
        rng = random.Random(7)
        for _ in range(400):
            base = _random_lines(rng, rng.randint(0, 8))
            pos = rng.randint(0, len(base))
            ours_ins = [f"O{i}-{rng.randint(0, 9)}\n" for i in range(rng.randint(1, 4))]
            theirs_ins = [f"T{i}-{rng.randint(0, 9)}\n" for i in range(rng.randint(1, 4))]
            ours = base[:pos] + ours_ins + base[pos:]
            theirs = base[:pos] + theirs_ins + base[pos:]
            r = merge3(base, ours, theirs, append=True)
            self.assertEqual(r.lines, base[:pos] + theirs_ins + ours_ins + base[pos:], (base, pos, ours_ins, theirs_ins))
            self.assertTrue(r.clean)
            # and the default mode refuses exactly these: they are adjacent edits at one place
            self.assertFalse(merge3(base, ours, theirs).clean)

    def test_every_inserted_line_survives_exactly_as_often_as_inserted(self):
        rng = random.Random(11)
        for _ in range(300):
            base = _random_lines(rng, rng.randint(0, 6))
            pos = rng.randint(0, len(base))
            ours_ins, theirs_ins = ([f"{w}{rng.randint(0, 3)}\n" for _ in range(rng.randint(1, 5))] for w in "OT")
            r = merge3(base, base[:pos] + ours_ins + base[pos:], base[:pos] + theirs_ins + base[pos:], append=True)
            if ours_ins == theirs_ins:
                continue                          # identical insertions are agreement and appear once
            for line in set(ours_ins + theirs_ins):
                self.assertGreaterEqual(r.lines.count(line), (ours_ins + theirs_ins).count(line) if line not in base else 1)

    def test_append_never_changes_what_the_default_merges_cleanly(self):
        rng = random.Random(99)
        for _ in range(500):
            base = _random_lines(rng, rng.randint(0, 8), "abc")
            ours = self.mutate(rng, base)
            theirs = self.mutate(rng, base)
            plain = merge3(base, ours, theirs)
            relaxed = merge3(base, ours, theirs, append=True)
            if plain.clean:
                self.assertEqual(relaxed.lines, plain.lines)
            else:
                # relaxing may resolve conflicts, never add new ones, and never change text outside them
                self.assertLessEqual(len(relaxed.conflicts), len(plain.conflicts))

    def test_append_conflicts_are_a_subset_of_default_conflicts(self):
        rng = random.Random(5)
        for _ in range(300):
            base = _random_lines(rng, rng.randint(0, 6), "ab")
            ours, theirs = self.mutate(rng, base), self.mutate(rng, base)
            plain = merge3(base, ours, theirs)
            relaxed = merge3(base, ours, theirs, append=True)
            self.assertTrue(set(c.base_start for c in relaxed.conflicts) <= set(c.base_start for c in plain.conflicts))

    @staticmethod
    def mutate(rng, lines):
        out = list(lines)
        for _ in range(rng.randint(0, 3)):
            op = rng.choice(["ins", "ins", "del", "mod"])
            if op == "ins" or not out:
                out.insert(rng.randint(0, len(out)), f"n{rng.randint(0, 5)}\n")
            elif op == "del":
                del out[rng.randrange(len(out))]
            else:
                out[rng.randrange(len(out))] = f"m{rng.randint(0, 5)}\n"
        return out


# ---- tree and repository level -----------------------------------------------------------------------------

def fork_edit(repo, name, **files):
    ws = repo.fork(name)
    for p, text in files.items():
        ws.write(p.replace("_dot_", "."), text)
    ws.commit(name)
    return ws


class TreeMergeTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository.init(MemoryKV(), {"reg.py": b"A = 1\n", "other.py": b"x\n"})
        self.base = self.repo.head()

    def merge(self, ours_text, theirs_text, policy=None, path="reg.py"):
        store = self.repo.store
        t0 = store.get_commit(self.base).tree
        mk = lambda text: store.update(t0, {path: store.put_blob(text.encode())})         # noqa: E731
        return merge_trees(store, t0, mk(ours_text), mk(theirs_text), policy=policy)

    def test_policy_function_is_consulted_per_path(self):
        r = self.merge("A = 1\nO\n", "A = 1\nT\n", policy=lambda p: "append")
        self.assertTrue(r.clean)
        self.assertEqual(r.appended, ["reg.py"])
        self.assertEqual(r.auto_merged, ["reg.py"])
        r = self.merge("A = 1\nO\n", "A = 1\nT\n", policy=lambda p: "relative")
        self.assertFalse(r.clean)
        r = self.merge("A = 1\nO\n", "A = 1\nT\n", policy=lambda p: "append" if p == "other.py" else "relative")
        self.assertFalse(r.clean)

    def test_converge_does_not_change_text_merging(self):
        r = self.merge("A = 1\nO\n", "A = 1\nT\n", policy=lambda p: "converge")
        self.assertFalse(r.clean)

    def test_add_add_in_an_append_file_keeps_both(self):
        store = self.repo.store
        t0 = store.get_commit(self.base).tree
        o = store.update(t0, {"new.log": store.put_blob(b"o\n")})
        t = store.update(t0, {"new.log": store.put_blob(b"t\n")})
        r = merge_trees(store, t0, o, t, policy=lambda p: "append")
        self.assertTrue(r.clean)
        self.assertEqual(store.read_file(r.tree, "new.log"), b"t\no\n")
        self.assertFalse(merge_trees(store, t0, o, t).clean)

    def test_binary_files_never_append(self):
        store = self.repo.store
        t0 = store.get_commit(self.base).tree
        o = store.update(t0, {"reg.py": store.put_blob(b"A = 1\n\xff")})
        t = store.update(t0, {"reg.py": store.put_blob(b"A = 1\n\xfe")})
        self.assertFalse(merge_trees(store, t0, o, t, policy=lambda p: "append").clean)


class LandingPolicyTests(unittest.TestCase):
    """Two agents append to one file through ``Repository.submit``."""

    def setUp(self):
        self.files = {"reg.py": b"# registry\n", "doc.md": b"# doc\n"}

    def repo_with(self, rules=None):
        files = dict(self.files)
        if rules is not None:
            files[FILE_NAME] = rules.encode()
        return Repository.init(MemoryKV(), files)

    def two_agents(self, repo, path="reg.py", read=True):
        a, b = repo.fork("a"), repo.fork("b")
        for ws, line in ((a, "from_a"), (b, "from_b")):
            if read:
                text = ws.read_text(path)
            else:
                text = ws._peek(path).decode()
            ws.write(path, text + line + "\n")
            ws.commit(ws.agent_id)
        return a, b

    def test_without_a_declaration_the_second_agent_is_stale(self):
        repo = self.repo_with()
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_declared_append_lets_both_land_in_landing_order(self):
        repo = self.repo_with("reg.py append\n")
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        r = repo.submit(b)
        self.assertTrue(r.landed, r.status)
        self.assertEqual(repo.main_files()["reg.py"], b"# registry\nfrom_a\nfrom_b\n")
        self.assertEqual(r.auto_merged, ["reg.py"])

    def test_landing_order_decides_the_line_order(self):
        repo = self.repo_with("reg.py append\n")
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(b).landed)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.main_files()["reg.py"], b"# registry\nfrom_b\nfrom_a\n")

    def test_task_rules_work_without_a_project_file(self):
        repo = self.repo_with()
        a, b = self.two_agents(repo)
        rules = (("reg.py", "append"),)
        self.assertTrue(repo.submit(a, task_rules=rules).landed)
        self.assertTrue(repo.submit(b, task_rules=rules).landed)
        self.assertEqual(repo.main_files()["reg.py"], b"# registry\nfrom_a\nfrom_b\n")

    def test_task_rule_overrides_the_project_file(self):
        repo = self.repo_with("reg.py append\n")
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b, task_rules=(("reg.py", "relative"),)).status, "stale")

    def test_only_the_named_file_is_relaxed(self):
        repo = self.repo_with("reg.py append\n")
        a, b = repo.fork("a"), repo.fork("b")
        for ws, line in ((a, "from_a"), (b, "from_b")):
            ws.write("doc.md", ws.read_text("doc.md") + line + "\n")
            ws.commit(ws.agent_id)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_an_agent_cannot_relax_its_own_landing_by_editing_the_rules(self):
        repo = self.repo_with()
        a, b = self.two_agents(repo)
        b.write(FILE_NAME, "reg.py append\n")
        b.commit("b changes the rules")
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")        # main's file, not b's, decides

    def test_rules_landed_by_someone_else_apply_to_later_landings(self):
        repo = self.repo_with()
        c = repo.fork("c")
        c.write(FILE_NAME, "reg.py append\n")
        c.commit("rules")
        self.assertTrue(repo.submit(c).landed)
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        self.assertTrue(repo.submit(b).landed)

    def test_append_does_not_excuse_a_modified_line(self):
        repo = self.repo_with("reg.py append\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.write("reg.py", a.read_text("reg.py") + "from_a\n")
        a.commit("a")
        b.write("reg.py", b.read_text("reg.py").replace("# registry", "# REGISTRY"))
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_append_does_not_excuse_main_modifying_a_line(self):
        repo = self.repo_with("reg.py append\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.write("reg.py", a.read_text("reg.py").replace("# registry", "# REGISTRY"))
        a.commit("a")
        b.write("reg.py", b.read_text("reg.py") + "from_b\n")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_append_excuses_a_read_when_the_agent_did_not_change_the_file(self):
        repo = self.repo_with("reg.py append\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.write("reg.py", a.read_text("reg.py") + "from_a\n")
        a.commit("a")
        b.read_text("reg.py")                                   # b only looked
        b.write("other.txt", "x")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertTrue(repo.submit(b).landed)

    def test_converge_ignores_the_read_set_for_that_file_only(self):
        repo = self.repo_with("*.md converge\n")
        a, b = repo.fork("a"), repo.fork("b")
        for ws in (a, b):
            ws.write("doc.md", ws.read_text("doc.md").replace("# doc", "# Doc"))     # the same edit
            ws.commit(ws.agent_id)
        self.assertTrue(repo.submit(a).landed)
        r = repo.submit(b)
        self.assertIn(r.status, ("merged", "noop"))             # identical edits are agreement here
        self.assertEqual(repo.main_files()["doc.md"], b"# Doc\n")

    def test_without_converge_identical_edits_are_stale_when_read(self):
        repo = self.repo_with()
        a, b = repo.fork("a"), repo.fork("b")
        for ws in (a, b):
            ws.write("doc.md", ws.read_text("doc.md").replace("# doc", "# Doc"))
            ws.commit(ws.agent_id)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_converge_still_conflicts_on_different_edits(self):
        repo = self.repo_with("*.md converge\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.write("doc.md", "# A\n")
        a.commit("a")
        b.write("doc.md", "# B\n")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "conflict")

    def test_append_when_the_file_was_created_by_main_after_the_fork(self):
        repo = self.repo_with("log.txt append\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.write("log.txt", "a\n")
        a.commit("a")
        self.assertFalse(b.exists("log.txt"))                   # b looked and found nothing
        b.write("log.txt", "b\n")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        r = repo.submit(b)
        self.assertTrue(r.landed, r.status)
        self.assertEqual(repo.main_files()["log.txt"], b"a\nb\n")

    def test_append_when_main_deleted_the_file_stays_stale(self):
        repo = self.repo_with("reg.py append\n")
        a, b = repo.fork("a"), repo.fork("b")
        a.delete("reg.py")
        a.commit("a")
        b.write("reg.py", b.read_text("reg.py") + "from_b\n")
        b.commit("b")
        self.assertTrue(repo.submit(a).landed)
        self.assertIn(repo.submit(b).status, ("stale", "conflict"))

    def test_many_agents_appending_all_land_with_no_redo(self):
        repo = self.repo_with("reg.py append\n")
        wss = [repo.fork(f"w{i}") for i in range(12)]
        for i, ws in enumerate(wss):
            ws.write("reg.py", ws.read_text("reg.py") + f"line{i}\n")
            ws.commit(ws.agent_id)
        for ws in wss:
            self.assertTrue(repo.submit(ws).landed)
        lines = repo.main_files()["reg.py"].decode().splitlines()
        self.assertEqual(lines, ["# registry"] + [f"line{i}" for i in range(12)])

    def test_sync_applies_the_policy_too(self):
        repo = self.repo_with("reg.py append\n")
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        r = b.sync()
        self.assertEqual(r.status, "merged")
        self.assertEqual(r.stale_reads, [])                      # append excused the read
        self.assertEqual(b.read_text("reg.py"), "# registry\nfrom_a\nfrom_b\n")

    def test_sync_without_a_policy_conflicts(self):
        repo = self.repo_with()
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        r = b.sync()
        self.assertEqual(r.status, "conflict")
        self.assertTrue(r.stale_reads)

    def test_sync_with_task_rules(self):
        repo = self.repo_with()
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(b.sync(task_rules=(("reg.py", "append"),)).status, "merged")

    def test_policy_table_is_read_from_the_given_commit(self):
        repo = self.repo_with("reg.py append\n")
        self.assertEqual(repo.policy_table(repo.head()).policy_for("reg.py"), APPEND)
        self.assertEqual(repo.policy_table(repo.head(), (("reg.py", "relative"),)).policy_for("reg.py"), RELATIVE)

    def test_a_broken_project_file_falls_back_to_relative(self):
        repo = self.repo_with("reg.py sometimes\nnonsense\n")
        a, b = self.two_agents(repo)
        self.assertTrue(repo.submit(a).landed)
        self.assertEqual(repo.submit(b).status, "stale")

    def test_noop_and_fast_forward_are_unaffected(self):
        repo = self.repo_with("reg.py append\n")
        a = fork_edit(repo, "a", reg_dot_py="# registry\nx\n")
        r = repo.submit(a)
        self.assertEqual(r.status, "fast-forward")
        self.assertEqual(repo.submit(a).status, "noop")


class RandomLandingPropertyTests(unittest.TestCase):
    """Random numbers of agents with random appended lines to a few files, random landing order: with the files
    declared ``append`` every line lands exactly once, in landing order, with no stale reads and no conflicts."""

    def test_random_runs(self):
        rng = random.Random(2024)
        for run in range(30):
            files = {f"f{i}.txt": b"head\n" for i in range(3)}
            files[FILE_NAME] = b"*.txt append\n"
            repo = Repository.init(MemoryKV(), files)
            n = rng.randint(2, 9)
            plan = []
            wss = []
            for i in range(n):
                ws = repo.fork(f"w{i}")
                wss.append(ws)
                lines = {}
                for p in rng.sample(sorted(k for k in files if k.endswith(".txt")), rng.randint(1, 3)):
                    lines[p] = [f"w{i}-{p}-{j}\n" for j in range(rng.randint(1, 3))]
                    ws.write(p, ws.read_text(p) + "".join(lines[p]))
                ws.commit(ws.agent_id)
                plan.append(lines)
            order = list(range(n))
            rng.shuffle(order)
            for i in order:
                r = repo.submit(wss[i])
                self.assertTrue(r.landed, (run, i, r.status))
            final = repo.main_files()
            for p in (k for k in files if k.endswith(".txt")):
                expect = "head\n" + "".join("".join(plan[i].get(p, [])) for i in order)
                self.assertEqual(final[p].decode(), expect, (run, p))


if __name__ == "__main__":
    unittest.main()
