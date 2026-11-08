"""The MCP server: the protocol layer, the trunk service behind the tools, and several agents at once."""

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from cmws_ws.agents import CommandValidator, MergeQueue
from cmws_ws.agents.worker import Task
from cmws_ws.kv import MemoryKV
from cmws_ws.mcp import TrunkError, TrunkService
from cmws_ws.mcp import server as mcp_server
from cmws_ws.mcp.protocol import McpServer, Resource, ResourceTemplate, Tool, ToolFailure, _check
from cmws_ws.mcp.server import build_server
from cmws_ws.repo import Repository


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- protocol ---------------------------------------------------------------------------------------

def rpc(method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        msg["id"] = id
    if params is not None:
        msg["params"] = params
    return msg


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.srv = McpServer("t", "1.0", "do things")
        self.log = []
        self.srv.log = self.log.append
        self.srv.add_tool(Tool("echo", "echo", {"type": "object", "properties": {
            "text": {"type": "string"}, "n": {"type": "integer"}, "flag": {"type": "boolean"},
            "xs": {"type": "array", "items": {"type": "string"}}, "mode": {"type": "string", "enum": ["a", "b"]}},
            "required": ["text"]}, lambda a: {"ok": True, **a}))
        self.srv.add_tool(Tool("boom", "raises", {"type": "object", "properties": {}}, lambda a: 1 / 0))
        self.srv.add_tool(Tool("fails", "fails", {"type": "object", "properties": {}},
                               lambda a: (_ for _ in ()).throw(ToolFailure("nope", {"hint": "x"}))))
        self.srv.add_tool(Tool("soft", "ok false", {"type": "object", "properties": {}}, lambda a: {"ok": False, "why": "y"}))

    def call(self, name, args=None):
        return self.srv.handle(rpc("tools/call", {"name": name, "arguments": args}))

    def test_initialize_agrees_on_a_version_and_describes_itself(self):
        for asked, expect in (("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"), ("2025-06-18", "2025-06-18"),
                              ("1999-01-01", "2025-06-18"), (None, "2025-06-18")):
            r = self.srv.handle(rpc("initialize", {"protocolVersion": asked, "capabilities": {}, "clientInfo": {"name": "c"}}))["result"]
            self.assertEqual(r["protocolVersion"], expect)
        self.assertEqual(r["serverInfo"], {"name": "t", "version": "1.0"})
        self.assertEqual(r["instructions"], "do things")
        self.assertIn("tools", r["capabilities"])
        self.assertIn("resources", r["capabilities"])

    def test_notifications_get_no_answer_and_requests_always_do(self):
        self.assertIsNone(self.srv.handle(rpc("notifications/initialized", id=None)))
        self.assertIsNone(self.srv.handle(rpc("notifications/cancelled", {"requestId": 1}, id=None)))
        self.assertIsNone(self.srv.handle(rpc("something/unknown", id=None)))           # unknown notification: ignored
        self.assertEqual(self.srv.handle(rpc("ping"))["result"], {})
        self.assertEqual(self.srv.handle(rpc("ping", id=0))["id"], 0)                 # id 0 is an id
        self.assertEqual(self.srv.handle(rpc("ping", id="abc"))["id"], "abc")

    def test_errors(self):
        r = self.srv.handle(rpc("nope"))
        self.assertEqual(r["error"]["code"], -32601)
        self.assertEqual(self.srv.handle({"hello": 1})["error"]["code"], -32600)
        self.assertEqual(self.srv.handle([1])["error"]["code"], -32600)
        self.assertEqual(self.srv.handle({"jsonrpc": "1.0", "method": "ping", "id": 1})["error"]["code"], -32600)
        self.assertEqual(self.srv.handle({"jsonrpc": "2.0", "method": "ping", "id": 1, "params": [1]})["error"]["code"], -32602)
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "id": 1, "result": {}}))   # a reply to us: ignore

    def test_tools_list(self):
        tools = self.srv.handle(rpc("tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["echo", "boom", "fails", "soft"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["text"])
        self.assertTrue(all({"name", "description", "inputSchema"} <= set(t) for t in tools))

    def test_a_call_returns_text_and_structured_content(self):
        r = self.call("echo", {"text": "hi", "n": 3})["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(json.loads(r["content"][0]["text"]), {"ok": True, "text": "hi", "n": 3})
        self.assertEqual(r["structuredContent"], {"ok": True, "text": "hi", "n": 3})
        self.assertEqual(r["content"][0]["type"], "text")

    def test_argument_checking(self):
        for args, fragment in (({}, "missing required argument 'text'"), ({"text": 1}, "must be of type string"),
                               ({"text": "a", "n": "x"}, "'n' must be of type integer"),
                               ({"text": "a", "n": True}, "'n' must be of type integer"),
                               ({"text": "a", "flag": "yes"}, "type boolean"),
                               ({"text": "a", "xs": [1]}, "list of strings"), ({"text": "a", "xs": "a"}, "type array"),
                               ({"text": "a", "mode": "c"}, "one of ['a', 'b']"), ({"text": "a", "extra": 1}, "unknown argument 'extra'")):
            r = self.call("echo", args)["result"]
            self.assertTrue(r["isError"], args)
            self.assertIn(fragment, json.loads(r["content"][0]["text"])["error"])
        self.assertFalse(self.call("echo", {"text": "a", "flag": True, "xs": ["q"], "mode": "b", "n": 0})["result"]["isError"])

    def test_bad_calls_are_protocol_errors(self):
        self.assertEqual(self.call("nope")["error"]["code"], -32602)
        self.assertEqual(self.srv.handle(rpc("tools/call", {"name": 5}))["error"]["code"], -32602)
        self.assertEqual(self.srv.handle(rpc("tools/call", {"name": "echo", "arguments": [1]}))["error"]["code"], -32602)
        self.assertFalse(self.call("soft", None)["result"]["isError"] is False)        # no arguments is fine, ok:false is an error

    def test_tool_failures_become_error_results(self):
        r = self.call("fails")["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(json.loads(r["content"][0]["text"]), {"ok": False, "error": "nope", "hint": "x"})
        r = self.call("boom")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("ZeroDivisionError", r["content"][0]["text"])
        self.assertTrue(self.log)                                                    # and it was logged, not raised
        self.assertTrue(self.call("soft")["result"]["isError"])

    def test_resources(self):
        self.srv.resources["x://a"] = Resource("x://a", "a", "the a", lambda: "AAA")
        self.srv.templates.append(ResourceTemplate("x://f/{p}", "f", "file", lambda p: p.upper()))
        listed = self.srv.handle(rpc("resources/list"))["result"]["resources"]
        self.assertEqual((listed[0]["uri"], listed[0]["mimeType"]), ("x://a", "application/json"))
        self.assertEqual(self.srv.handle(rpc("resources/templates/list"))["result"]["resourceTemplates"][0]["uriTemplate"], "x://f/{p}")
        self.assertEqual(self.srv.handle(rpc("resources/read", {"uri": "x://a"}))["result"]["contents"][0]["text"], "AAA")
        self.assertEqual(self.srv.handle(rpc("resources/read", {"uri": "x://f/dir/q.py"}))["result"]["contents"][0]["text"], "DIR/Q.PY")
        for bad in ({"uri": "x://zzz"}, {"uri": "x://f/"}, {}, {"uri": 5}):
            self.assertEqual(self.srv.handle(rpc("resources/read", bad))["error"]["code"], -32602, bad)

    def test_a_failing_template_is_an_error_not_a_crash(self):
        self.srv.templates.append(ResourceTemplate("y://{p}", "y", "y", lambda p: (_ for _ in ()).throw(KeyError(p))))
        self.assertEqual(self.srv.handle(rpc("resources/read", {"uri": "y://k"}))["error"]["code"], -32602)

    def test_the_serve_loop(self):
        lines = [json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"})), "", "   ", "{not json",
                 json.dumps(rpc("notifications/initialized", id=None)), json.dumps(rpc("ping", id=7)),
                 json.dumps([rpc("ping", id=8), rpc("ping", id=9), rpc("notifications/initialized", id=None)]),
                 json.dumps(rpc("tools/call", {"name": "echo", "arguments": {"text": "é☃"}}, id=10))]
        out = io.StringIO()
        self.srv.serve(io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(out.getvalue().splitlines()), 5)                         # exactly one line per answer
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertIsNone(replies[1]["id"])
        self.assertEqual(replies[2]["id"], 7)
        self.assertEqual([r["id"] for r in replies[3]], [8, 9])                       # a batch is answered as a batch
        self.assertEqual(json.loads(replies[4]["result"]["content"][0]["text"])["text"], "é☃")

    def test_deeply_nested_input_does_not_kill_the_loop(self):
        out = io.StringIO()
        self.srv.serve(io.StringIO("[" * 200000 + "\n" + json.dumps(rpc("ping", id=2)) + "\n"), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 2)

    def test_schema_check_helper(self):
        self.assertIsNone(_check({"properties": {}}, {}))
        self.assertIn("unknown argument", _check({"properties": {}}, {"a": 1}))
        self.assertIn("none", _check({"properties": {}}, {"a": 1}))


# ---- the service ------------------------------------------------------------------------------------

def make(kv=None, files=None, validator=None, clock=None, **kw):
    kv = kv or MemoryKV()
    Repository.init(kv, files or {"stats.py": b"COUNT = 0\n", "lib.py": b"x = 1\n"})
    return kv


class Env:
    """Several agents (each its own TrunkService, as separate MCP servers would be) on one store."""

    def __init__(self, files=None, validate=None, clock=None, **kw):
        self.kv = MemoryKV()
        Repository.init(self.kv, files or {"stats.py": b"COUNT = 0\n", "lib.py": b"x = 1\n", "other.py": b"y = 1\n"})
        self.tmp = tempfile.TemporaryDirectory()
        self.validate, self.clock, self.kw = validate, clock or Clock(), kw

    def svc(self, name, board="b1", **kw):
        repo = Repository(self.kv)
        queue = MergeQueue(repo, validator=CommandValidator([sys.executable, "-c", self.validate]) if self.validate else None)
        return TrunkService(repo, board, name, Path(self.tmp.name) / name, queue, clock=self.clock, **{**self.kw, **kw})

    def close(self):
        self.tmp.cleanup()


def write(info, name, text):
    (Path(info["folder"]) / name).write_text(text)


def read(info, name):
    return (Path(info["folder"]) / name).read_text()


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.a = self.env.svc("alice")
        self.a.create_board([Task("inc1", "bump", ("stats.py",)), Task("inc2", "bump again", ("stats.py",)),
                             Task("lib", "edit lib", ("lib.py",)), Task("after", "after lib", ("other.py",), "", ("lib",))])

    def states(self, svc=None):
        return {t["id"]: t["state"] for t in (svc or self.a).board_view()["tasks"]}

    def test_states_open_waiting_claimed_done_and_failed(self):
        self.assertEqual(self.states(), {"inc1": "open", "inc2": "waiting", "lib": "open", "after": "waiting"})
        view = self.a.board_view()
        self.assertEqual(view["tasks"][1]["waiting_for"], ["inc1"])
        self.assertEqual(view["tasks"][3]["waiting_for"], ["lib"])
        c = self.a.claim("inc1")
        self.assertEqual(self.states()["inc1"], "claimed")
        self.assertEqual(self.a.board_view()["yours"], "inc1")
        self.assertEqual(self.env.svc("bob").board_view()["yours"], None)
        write(c, "stats.py", "COUNT = 1\n")
        self.assertEqual(self.a.submit()["status"], "landed")
        self.assertEqual(self.states(), {"inc1": "done", "inc2": "open", "lib": "open", "after": "waiting"})
        self.assertEqual(self.a.board_view()["tasks"][0]["by"], "alice")

    def test_a_failed_task_blocks_its_dependents(self):
        repo = Repository(self.env.kv)
        self.a.claims.claim("b1-lib", "x")
        e = self.a.claims.snapshot()["b1-lib"]["epoch"]
        self.a.claims.complete("b1-lib", "x", e, {"status": "failed", "reason": "boom"})
        st = {t["id"]: t for t in self.a.board_view()["tasks"]}
        self.assertEqual((st["lib"]["state"], st["lib"]["reason"], st["lib"]["commit"]), ("failed", "boom", None))
        self.assertEqual((st["after"]["state"], st["after"]["reason"]), ("blocked", "a task it depends on failed"))

    def test_the_next_ready_task_is_claimed_in_declaration_order(self):
        self.assertEqual(self.a.claim()["task"]["id"], "inc1")
        b = self.env.svc("bob")
        self.assertEqual(b.claim()["task"]["id"], "lib")                          # inc2 waits for inc1, "after" for lib
        with self.assertRaises(TrunkError) as cm:
            self.env.svc("carol").claim()
        self.assertIn("no task is ready", str(cm.exception))

    def test_claiming_a_named_task(self):
        with self.assertRaises(TrunkError) as cm:
            self.a.claim("nope")
        self.assertIn("no task 'nope'", str(cm.exception))
        with self.assertRaises(TrunkError) as cm:
            self.a.claim("inc2")
        self.assertIn("must wait for: inc1", str(cm.exception))
        c = self.a.claim("lib")
        self.assertEqual((c["task"]["id"], c["branch"]), ("lib", "ws/b1-lib"))
        with self.assertRaises(TrunkError) as cm:
            self.env.svc("bob").claim("lib")
        self.assertIn("is claimed (by alice)", str(cm.exception))

    def test_one_task_at_a_time(self):
        first = self.a.claim()
        again = self.a.claim()                                                  # asking again hands the same task back
        self.assertEqual((again["task"]["id"], again["resumed"], again["folder"]), ("inc1", True, first["folder"]))
        self.assertIn("You already hold this task", again["next"])
        self.assertEqual(self.a.claim("inc1")["resumed"], True)                 # naming it is the same thing
        for kwargs in ({"task": "lib"}, {"description": "another", "paths": ["z.py"]}):
            with self.assertRaises(TrunkError) as cm:                           # asking for something else is refused
                self.a.claim(**kwargs)
            self.assertIn("already hold task 'inc1'", str(cm.exception))

    def test_a_task_you_add_yourself(self):
        b = self.env.svc("bob")
        c = b.claim(description="touch new", paths=["new.py"])
        self.assertEqual(c["task"]["id"], "adhoc-1")
        self.assertEqual(c["task"]["paths"], ["new.py"])
        self.assertIn("adhoc-1", self.env.kv.get("runs/b1/adhoc"))                    # where agents' own tasks are kept
        self.assertIn("adhoc-1", self.states())
        self.assertEqual(self.states()["adhoc-1"], "claimed")
        b.release()
        with self.assertRaises(TrunkError) as cm:                                   # overlaps an earlier open task: it goes first
            b.claim(description="another", paths=["lib.py"], task="mine")
        self.assertIn("must wait for: lib", str(cm.exception))

    def test_adhoc_tasks_with_overlapping_paths_wait_for_each_other(self):
        b, c = self.env.svc("bob"), self.env.svc("carol")
        b.claim(description="first", paths=["shared.py"])
        with self.assertRaises(TrunkError) as cm:
            c.claim(description="second", paths=["shared.py"])
        self.assertIn("must wait for", str(cm.exception))
        self.assertEqual(c.claim(description="third", paths=["unrelated.py"])["task"]["id"], "adhoc-3")

    def test_adhoc_ids_and_validation(self):
        b = self.env.svc("bob")
        with self.assertRaises(TrunkError) as dup:
            b.claim(task="inc1", description="duplicate id")
        self.assertIn("already on the board", str(dup.exception))
        with self.assertRaises(TrunkError):
            b.claim(task="bad id!", description="x")
        self.assertEqual(b.claim(task="custom", description="named", paths=["z.py"])["task"]["id"], "custom")
        b.release()
        with self.assertRaises(TrunkError) as cm:
            b.claim(task="t" * 62, description="too long once the board name is added")
        self.assertIn("cannot add task", str(cm.exception))
        self.assertEqual([t["id"] for t in b.board_view()["tasks"]][-1], "custom")      # a refused task is not stored

    def test_board_and_agent_names_are_checked(self):
        for bad in ("", "a-b", "x" * 33, "a b"):
            with self.assertRaises(TrunkError):
                self.env.svc("x", board=bad)
        for bad in ("", "../x", "a b", "-x"):
            with self.assertRaises(TrunkError):
                self.env.svc(bad)

    def test_create_board_says_whether_it_made_the_board(self):
        self.assertTrue(self.env.svc("bob", board="fresh").create_board([Task("a", "x")]))
        self.assertIsNotNone(self.env.kv.get("runs/fresh/plan"))

    def test_create_board_keeps_an_existing_plan(self):
        self.assertFalse(self.a.create_board([Task("zzz", "z")]))
        self.assertEqual([t["id"] for t in self.a.board_view()["tasks"]], ["inc1", "inc2", "lib", "after"])
        with self.assertRaises(Exception):
            self.env.svc("bob", board="other").create_board([Task("a", "x", (), "", ("missing",))])

    def test_an_empty_board(self):
        view = self.env.svc("bob", board="empty").board_view()
        self.assertEqual(view["tasks"], [])
        with self.assertRaises(TrunkError):
            self.env.svc("bob", board="empty").claim()

    def test_eight_agents_claim_eight_tasks_without_sharing_one(self):
        env = Env()
        self.addCleanup(env.close)
        env.svc("a0").create_board([Task(f"t{i}", "x", (f"f{i}.py",)) for i in range(8)])
        got, barrier = [], threading.Barrier(8)

        def go(i):
            barrier.wait()
            got.append(env.svc(f"a{i}").claim()["task"]["id"])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), [f"t{i}" for i in range(8)])


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(validate="import stats; assert isinstance(stats.COUNT, int)")
        self.addCleanup(self.env.close)
        self.a, self.b = self.env.svc("alice"), self.env.svc("bob")
        self.a.create_board([Task("t1", "bump", ("1.m",)), Task("t2", "bump", ("2.m",)), Task("t3", "lib", ("lib.py",))])

    def main(self, name="stats.py"):
        return Repository(self.env.kv).main_files()[name].decode()

    def test_submit_lands_completes_and_refreshes_the_folder(self):
        c = self.a.claim()
        self.assertTrue(Path(c["folder"]).is_dir())
        self.assertIn("COUNT = 0", read(c, "stats.py"))
        write(c, "stats.py", "COUNT = 1\n")
        write(c, "new.py", "n = 1\n")
        r = self.a.submit("one more")
        self.assertEqual((r["status"], r["files"]), ("landed", ["new.py", "stats.py"]))
        self.assertEqual(self.main(), "COUNT = 1\n")
        self.assertIn("one more", [m for _, m in [(0, Repository(self.env.kv).store.get_commit(
            Repository(self.env.kv).head()).message)]][0])
        with self.assertRaises(TrunkError) as cm:
            self.a.submit()
        self.assertIn("hold no task", str(cm.exception))
        self.assertEqual(self.a.board_view()["tasks"][0]["state"], "done")
        self.assertEqual(self.a.leases.holders(), {})

    def test_nothing_to_land(self):
        self.a.claim()
        r = self.a.submit()
        self.assertEqual((r["ok"], r["status"]), (False, "nothing-to-land"))
        self.assertEqual(self.a.board_view()["tasks"][0]["state"], "claimed")     # still yours

    def test_status_reports_changes_and_what_to_do(self):
        c = self.a.claim()
        st = self.a.status()
        self.assertEqual((st["changed"], st["main_moved"], st["claim_held"]), ([], False, True))
        self.assertEqual(st["next"], "submit_task when you are done.")
        write(c, "stats.py", "COUNT = 5\n")
        st = self.a.status()
        self.assertEqual((st["changed"], st["uncommitted"]), (["stats.py"], {"stats.py": "M"}))
        self.assertEqual(st["next"], "submit_task when you are done.")

    def test_a_validation_failure_is_reported_and_main_does_not_move(self):
        c = self.a.claim()
        before = Repository(self.env.kv).head()
        write(c, "stats.py", "COUNT = 'x'\n")
        r = self.a.submit()
        self.assertEqual((r["ok"], r["status"]), (False, "rejected"))
        self.assertIn("AssertionError", r["detail"])
        self.assertIn("failed validation", r["next"])
        self.assertEqual(Repository(self.env.kv).head(), before)
        write(c, "stats.py", "COUNT = 2\n")
        self.assertEqual(self.a.submit()["status"], "landed")

    def test_the_same_edit_by_two_agents_is_stale_not_silently_lost(self):
        ca, cb = self.a.claim(), self.b.claim()
        write(ca, "stats.py", "COUNT = 1\n")
        write(cb, "stats.py", "COUNT = 1\n")                       # both read 0 and wrote 1
        self.assertEqual(self.a.submit()["status"], "landed")
        r = self.b.submit()
        self.assertEqual((r["status"], r["stale_paths"]), ("stale", ["stats.py"]))
        self.assertIn("sync_with_main", r["next"])
        st = self.b.status()
        self.assertEqual((st["main_moved"], st["changed_on_both_sides"]), (True, ["stats.py"]))
        s = self.b.sync()
        self.assertEqual(s["status"], "merged")
        self.assertEqual(s["changed_on_both_sides"], ["stats.py"])
        self.assertIn("-COUNT = 0\n+COUNT = 1", s["main_changes_to_those_files"])   # it sees what main changed
        self.assertIn("check that your change still does what the task asks", s["next"])
        self.assertEqual(read(cb, "stats.py"), "COUNT = 1\n")        # the merge took the two identical edits as one
        write(cb, "stats.py", "COUNT = 2\n")                          # so the agent, told to look, adds its own
        self.assertEqual(self.b.submit()["status"], "landed")
        self.assertEqual(self.main(), "COUNT = 2\n")

    def test_conflicting_edits_come_back_as_markers_and_must_be_resolved(self):
        ca, cb = self.a.claim(), self.b.claim()
        write(ca, "stats.py", "COUNT = 1\n")
        write(cb, "stats.py", "COUNT = 5\n")
        self.a.submit()
        self.assertEqual(self.b.submit()["status"], "stale")
        s = self.b.sync()
        self.assertEqual((s["status"], s["conflicts"]), ("merged-with-conflicts", ["stats.py"]))
        self.assertIn("<<<<<<<", read(cb, "stats.py"))
        self.assertEqual(self.b.status()["conflict_markers_in"], ["stats.py"])
        self.assertIn("Resolve", self.b.status()["next"])
        for call in (self.b.submit, self.b.sync):
            with self.assertRaises(TrunkError) as cm:
                call()
            self.assertIn("conflict markers: stats.py" if call == self.b.sync else "conflict markers", str(cm.exception))
        write(cb, "stats.py", "COUNT = 6\n")
        self.assertEqual(self.b.submit()["status"], "landed")
        self.assertEqual(self.main(), "COUNT = 6\n")

    def test_sync_variants(self):
        ca, cb = self.a.claim("t1"), self.b.claim("t3")
        self.assertEqual(self.b.sync()["status"], "up-to-date")
        write(ca, "stats.py", "COUNT = 1\n")
        self.a.submit()
        s = self.b.sync()                                          # b changed nothing: a plain fast-forward
        self.assertEqual((s["status"], s["brought_in"]), ("fast-forward", ["stats.py"]))
        self.assertEqual(read(cb, "stats.py"), "COUNT = 1\n")
        write(cb, "lib.py", "x = 2\n")
        c2 = self.a.claim("t2")
        write(c2, "stats.py", "COUNT = 2\n")
        self.a.submit()
        s = self.b.sync()                                          # disjoint changes merge cleanly
        self.assertEqual((s["status"], s["changed_on_both_sides"], s["conflicts"]), ("merged", [], []))
        self.assertIn("Merged cleanly", s["next"])
        self.assertEqual((read(cb, "stats.py"), read(cb, "lib.py")), ("COUNT = 2\n", "x = 2\n"))
        self.assertEqual(self.b.submit()["status"], "landed")
        self.assertEqual((self.main(), self.main("lib.py")), ("COUNT = 2\n", "x = 2\n"))

    def test_release_frees_the_task_and_keeps_the_branch(self):
        c = self.a.claim("t1")
        write(c, "stats.py", "COUNT = 9\n")
        r = self.a.release("changed my mind")
        self.assertEqual(r["released"], "t1")
        self.assertEqual(self.a.board_view()["yours"], None)
        self.assertEqual(self.a.leases.holders(), {})
        self.assertIsNotNone(Repository(self.env.kv).refs.get("ws/b1-t1"))
        c2 = self.b.claim("t1")                                      # and someone else can take it
        self.assertEqual(c2["task"]["id"], "t1")
        with self.assertRaises(TrunkError):
            self.a.release()

    def test_the_server_can_restart_without_losing_the_task(self):
        c = self.a.claim("t1")
        write(c, "stats.py", "COUNT = 1\n")
        again = self.env.svc("alice")                                # a new process for the same agent
        self.assertEqual(again.status()["uncommitted"], {"stats.py": "M"})   # the edit is in the folder, read back by the new server
        back = again.claim()                                         # the restarted agent is handed its task and folder
        self.assertEqual((back["task"]["id"], back["resumed"], back["folder"]), ("t1", True, c["folder"]))
        with self.assertRaises(TrunkError) as cm:
            again.claim("t2")
        self.assertIn("already hold task 't1'", str(cm.exception))
        self.assertEqual(again.submit()["status"], "landed")         # the folder has the edit, the store has the claim
        self.assertEqual(self.main(), "COUNT = 1\n")

    def test_a_claim_taken_over_cannot_land(self):
        c = self.a.claim("t1")
        write(c, "stats.py", "COUNT = 1\n")
        before = Repository(self.env.kv).head()
        self.env.clock.t += 5000                                     # alice went silent past her claim's lifetime
        self.assertEqual(self.b.claim("t1")["task"]["id"], "t1")      # bob takes the task over
        st = self.a.status()
        self.assertFalse(st["claim_held"])
        self.assertIn("taken over", st["next"])
        with self.assertRaises(TrunkError) as cm:
            self.a.submit()
        self.assertIn("was taken over by another agent", str(cm.exception))        # refused before anything was tried
        self.assertEqual(Repository(self.env.kv).head(), before)

    def test_the_claim_is_checked_again_at_the_last_moment(self):
        c = self.a.claim("t1")
        write(c, "stats.py", "COUNT = 1\n")
        env, a = self.env, self.a
        original = a.queue.attempt

        def attempt(agent_id, message):
            env.clock.t += 5000
            self.b.claim("t1")                                       # taken over after submit's own check
            return original(agent_id, message)
        a.queue.attempt = attempt
        before = Repository(self.env.kv).head()
        with self.assertRaises(TrunkError) as cm:
            a.submit()
        self.assertIn("taken over while landing", str(cm.exception))
        self.assertEqual(Repository(self.env.kv).head(), before)

    def test_escalations_are_listed(self):
        from cmws_ws.agents import Escalations
        Escalations(self.env.kv).add({"task": "x", "branch": "ws/x", "reason": "conflict", "detail": "d", "steps": []})
        r = self.a.escalations()
        self.assertEqual((r["count"], r["items"][0]["task"]), (1, "x"))
        self.assertEqual(self.a.board_view()["escalations"], 1)

    def test_main_files_and_reading_main(self):
        self.assertEqual(self.a.main_files(), ["lib.py", "other.py", "stats.py"])
        self.assertEqual(self.a.read_main("stats.py"), "COUNT = 0\n")
        with self.assertRaises(TrunkError):
            self.a.read_main("nope.py")

    def test_a_resumed_task_whose_folder_was_deleted_is_checked_out_again(self):
        import shutil
        c = self.a.claim("t1")
        self.a.release()
        self.a.claim("t1")
        shutil.rmtree(c["folder"])
        back = self.a.claim()
        self.assertTrue(back["resumed"])
        self.assertTrue((Path(back["folder"]) / "stats.py").exists())

    def test_resuming_a_task_that_left_the_board_says_so(self):
        c = self.a.claim("t1")
        self.a.repo.kv.put("runs/b1/plan", json.dumps({"goal": None, "created": 0, "tasks": [
            {"id": "t2", "description": "y", "paths": ["2.m"], "context": "", "depends_on": []}]}))
        with self.assertRaises(TrunkError) as cm:
            self.a.claim()
        self.assertIn("no longer on the board", str(cm.exception))

    def test_a_returning_agent_picks_its_folder_up_again(self):
        c = self.a.claim("t1")
        write(c, "stats.py", "COUNT = 7\n")
        self.a.release()
        c2 = self.a.claim("t1")                                      # same folder, work still there
        self.assertEqual(c2["folder"], c["folder"])
        self.assertEqual(read(c2, "stats.py"), "COUNT = 7\n")

    def test_marker_files_that_were_never_conflicts_are_not_blocked_unless_they_contain_markers(self):
        c = self.a.claim("t1")
        write(c, "doc.md", "a line\n=======\nnot a conflict, just a rule under a title\n")
        write(c, "stats.py", "COUNT = 1\n")
        self.assertEqual(self.a.submit()["status"], "landed")


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.a = self.env.svc("alice", file_tools=True)
        self.a.create_board([Task("t1", "bump", ("1.m",)), Task("t2", "bump", ("2.m",))])

    def test_the_tools_edit_the_branch_and_submit_lands_it(self):
        c = self.a.claim()
        self.assertIsNone(c["folder"])
        self.assertIn("list_files", c["next"])
        self.assertTrue(self.a.file_tool("read_file", {"path": "stats.py"})["ok"])
        r = self.a.file_tool("replace_in_file", {"path": "stats.py", "old": "COUNT = 0", "new": "COUNT = 1"})
        self.assertEqual((r["ok"], r["seq"]), (True, 2))
        self.assertEqual(self.a.status()["changed"], ["stats.py"])
        self.assertEqual(self.a.submit("done")["status"], "landed")
        self.assertEqual(Repository(self.env.kv).main_files()["stats.py"], b"COUNT = 1\n")

    def test_a_repeated_seq_returns_the_first_result_and_changes_nothing(self):
        self.a.claim()
        first = self.a.file_tool("replace_in_file", {"path": "stats.py", "old": "COUNT = 0", "new": "COUNT = 1", "seq": 1})
        again = self.a.file_tool("replace_in_file", {"path": "stats.py", "old": "COUNT = 0", "new": "COUNT = 1", "seq": 1})
        self.assertEqual((first["ok"], again["ok"], again["result"]), (True, True, first["result"]))
        gap = self.a.file_tool("list_files", {"seq": 5})
        self.assertFalse(gap["ok"])
        self.assertIn("expected call 2", gap["result"])
        with self.assertRaises(TrunkError):
            self.a.file_tool("list_files", {"seq": "x"})
        with self.assertRaises(TrunkError):
            self.a.file_tool("list_files", {"seq": True})

    def test_a_restart_resumes_from_the_last_checkpoint(self):
        self.a.claim()
        for i in range(5):                                           # the fifth call checkpoints
            self.a.file_tool("write_file", {"path": f"f{i}.txt", "content": str(i)})
        self.a.file_tool("write_file", {"path": "late.txt", "content": "x"})          # not yet checkpointed
        again = self.env.svc("alice", file_tools=True)               # the server process was replaced
        out = again.file_tool("list_files", {"directory": ""})
        self.assertEqual(out["seq"], 6)                              # numbering continues from the checkpoint
        names = out["result"].splitlines()
        self.assertIn("f4.txt", names)
        self.assertNotIn("late.txt", names)                          # lost with the process, as designed
        again.file_tool("write_file", {"path": "late.txt", "content": "x", "seq": 7})   # redone, numbered afresh
        self.assertEqual(again.submit()["status"], "landed")
        self.assertIn("late.txt", Repository(self.env.kv).main_files())

    def test_file_tools_need_the_flag_and_a_task(self):
        plain = self.env.svc("bob")
        with self.assertRaises(TrunkError) as cm:
            plain.file_tool("list_files", {})
        self.assertIn("file tools are off", str(cm.exception))
        with self.assertRaises(TrunkError) as cm:
            self.a.file_tool("list_files", {})
        self.assertIn("claim_task", str(cm.exception))

    def test_sync_and_conflicts_work_without_a_folder(self):
        b = self.env.svc("bob", file_tools=True)
        self.a.claim("t1")
        b.claim("t2")
        self.a.file_tool("write_file", {"path": "stats.py", "content": "COUNT = 1\n"})
        self.assertEqual(self.a.submit()["status"], "landed")
        b.file_tool("write_file", {"path": "stats.py", "content": "COUNT = 5\n"})
        self.assertEqual(b.submit()["status"], "stale")
        s = b.sync()
        self.assertEqual((s["status"], s["conflicts"]), ("merged-with-conflicts", ["stats.py"]))
        self.assertIn("<<<<<<<", b.file_tool("read_file", {"path": "stats.py"})["result"])
        with self.assertRaises(TrunkError):
            b.submit()
        b.file_tool("write_file", {"path": "stats.py", "content": "COUNT = 6\n"})
        self.assertEqual(b.submit()["status"], "landed")


# ---- the tools as a client sees them -----------------------------------------------------------------

class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        svc = self.env.svc("alice")
        svc.create_board([Task("t1", "bump", ("1.m",))])
        self.srv = build_server(svc)

    def call(self, name, args=None):
        r = self.srv.handle(rpc("tools/call", {"name": name, "arguments": args or {}}))["result"]
        return r["isError"], r["structuredContent"]

    def test_the_tool_set(self):
        names = [t["name"] for t in self.srv.handle(rpc("tools/list"))["result"]["tools"]]
        self.assertEqual(names, ["show_board", "claim_task", "task_status", "sync_with_main", "submit_task",
                                 "release_task", "add_tasks", "wait_for_tasks", "board_digest", "explain_escalation",
                                 "list_escalations"])
        files = build_server(self.env.svc("bob", file_tools=True))
        self.assertEqual([t["name"] for t in files.handle(rpc("tools/list"))["result"]["tools"]][-6:],
                         list(TrunkService.FILE_TOOLS))

    def test_a_whole_task_through_the_tools(self):
        err, board = self.call("show_board")
        self.assertEqual((err, [t["state"] for t in board["tasks"]]), (False, ["open"]))
        err, claimed = self.call("claim_task")
        self.assertFalse(err)
        (Path(claimed["folder"]) / "stats.py").write_text("COUNT = 1\n")
        err, st = self.call("task_status")
        self.assertEqual(st["changed"], ["stats.py"])
        err, done = self.call("submit_task", {"message": "bump"})
        self.assertEqual((err, done["status"]), (False, "landed"))

    def test_mistakes_come_back_as_readable_errors(self):
        err, r = self.call("submit_task")
        self.assertTrue(err)
        self.assertIn("claim_task first", r["error"])
        err, r = self.call("claim_task", {"task_id": "nope"})
        self.assertTrue(err)
        err, r = self.call("claim_task", {"paths": "not-a-list"})
        self.assertIn("type array", r["error"])
        self.call("claim_task")
        err, r = self.call("submit_task")                            # claimed but nothing changed
        self.assertTrue(err)
        self.assertEqual(r["status"], "nothing-to-land")

    def test_resources_show_main_and_the_board(self):
        main = json.loads(self.srv.handle(rpc("resources/read", {"uri": "agenttrunk://main"}))["result"]["contents"][0]["text"])
        self.assertEqual(main["files"], ["lib.py", "other.py", "stats.py"])
        board = json.loads(self.srv.handle(rpc("resources/read", {"uri": "agenttrunk://board"}))["result"]["contents"][0]["text"])
        self.assertEqual(board["board"], "b1")
        one = self.srv.handle(rpc("resources/read", {"uri": "agenttrunk://main/stats.py"}))["result"]["contents"][0]["text"]
        self.assertEqual(one, "COUNT = 0\n")
        self.assertEqual(self.srv.handle(rpc("resources/read", {"uri": "agenttrunk://main/nope.py"}))["error"]["code"], -32602)

    def test_the_server_object_is_built_from_arguments(self):
        out = io.StringIO()
        kv = MemoryKV()
        Repository.init(kv, {"a.py": b"1\n"})
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.json"
            tasks.write_text(json.dumps([{"id": "x", "description": "do x", "paths": ["a.py"]}]))
            stdin = io.StringIO(json.dumps(rpc("tools/call", {"name": "show_board", "arguments": {}})) + "\n")
            code = mcp_server.main(["--board", "bb", "--agent", "zed", "--tasks", str(tasks), "--workdir", tmp,
                                    "--validate-cmd", "python3 -c pass"], kv=kv, stdin=stdin, stdout=out)
            self.assertEqual(code, 0)
        board = json.loads(out.getvalue())["result"]["structuredContent"]
        self.assertEqual((board["board"], board["agent"], [t["id"] for t in board["tasks"]]), ("bb", "zed", ["x"]))

    def test_bad_startup_arguments_exit_cleanly(self):
        self.assertEqual(mcp_server.main(["--board", "bad-name"], kv=MemoryKV(), stdin=io.StringIO(""), stdout=io.StringIO()), 2)


# ---- agents at once ----------------------------------------------------------------------------------

class ManyAgentsTests(unittest.TestCase):
    """Agents that behave as an MCP client would: claim, edit, submit, and on 'stale' sync and look again."""

    def run_agents(self, n, rounds=1):
        env = Env(validate="import stats; assert isinstance(stats.COUNT, int)")
        self.addCleanup(env.close)
        env.svc("a0").create_board([Task(f"t{i}", "add one to COUNT in stats.py", (f"{i}.m",)) for i in range(n)])
        errors, steps = [], []

        def agent(i):
            try:
                svc = env.svc(f"agent{i}")
                c = svc.claim()
                for _ in range(40):
                    # "add one to whatever main holds": read the value you would be adding to
                    base = int(svc.read_main("stats.py").split("=")[1])
                    write(c, "stats.py", f"COUNT = {base + 1}\n")
                    r = svc.submit(f"agent{i}")
                    steps.append(r["status"])
                    if r["status"] == "landed":
                        return
                    if r["status"] == "stale":
                        s = svc.sync()
                        if s["status"] == "merged-with-conflicts":
                            continue                       # the loop rewrites the file with the new value anyway
                    elif r["status"] != "contended":
                        errors.append(r)
                        return
                errors.append("gave up")
            except Exception as e:                           # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=agent, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return env, errors, steps

    def test_concurrent_agents_lose_no_update(self):
        for round_ in range(4):
            env, errors, steps = self.run_agents(6)
            self.assertEqual(errors, [], (round_, errors))
            self.assertEqual(Repository(env.kv).main_files()["stats.py"], b"COUNT = 6\n", (round_, steps))
            self.assertGreater(steps.count("stale"), 0, "the agents did collide")
            view = env.svc("x").board_view()
            self.assertEqual({t["state"] for t in view["tasks"]}, {"done"})

    def test_every_landing_was_checked(self):
        env = Env(validate="import stats, sys; sys.exit(0 if stats.COUNT != 3 else 1)")
        self.addCleanup(env.close)
        a = env.svc("a")
        a.create_board([Task(f"t{i}", "x", (f"{i}.m",)) for i in range(4)])
        for i in range(4):
            svc = env.svc(f"w{i}")
            c = svc.claim()
            write(c, "stats.py", f"COUNT = {i + 1}\n")
            r = svc.submit()
            if i == 2:
                self.assertEqual(r["status"], "rejected")        # COUNT = 3 is refused by the project's own check
                write(c, "stats.py", "COUNT = 30\n")
                self.assertEqual(svc.submit()["status"], "landed")
        self.assertEqual(Repository(env.kv).main_files()["stats.py"], b"COUNT = 4\n")


if __name__ == "__main__":
    unittest.main()


# ---- hardening found by a mutation sweep ---------------------------------------------------------------

import contextlib                                                    # noqa: E402

from cmws_ws.leases import LeaseTable                                # noqa: E402
from cmws_ws.repo import SubmitResult                                # noqa: E402


class ProtocolDetailTests(unittest.TestCase):
    def setUp(self):
        self.srv = McpServer("t", "1.0")
        self.logged = []
        self.srv.log = self.logged.append
        self.srv.add_tool(Tool("lst", "list", {"type": "object", "properties": {}}, lambda a: ["é", 1]))

    def test_capabilities_are_declared_exactly(self):
        r = self.srv.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}))["result"]
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False},
                                             "resources": {"subscribe": False, "listChanged": False}})
        self.assertNotIn("instructions", r)                                       # none were given

    def test_non_dict_results_have_text_but_no_structured_content(self):
        r = self.srv.handle(rpc("tools/call", {"name": "lst"}))["result"]
        self.assertNotIn("structuredContent", r)
        self.assertEqual(json.loads(r["content"][0]["text"]), ["é", 1])
        self.assertIn("é", r["content"][0]["text"])                              # not escaped as é
        self.assertFalse(r["isError"])

    def test_an_unexpected_error_inside_a_method_is_logged_and_answered(self):
        def boom(p):
            raise RuntimeError("bug")
        self.srv._m_ping = boom
        r = self.srv.handle(rpc("ping"))
        self.assertEqual((r["error"]["code"], r["error"]["message"]), (-32603, "RuntimeError: bug"))
        self.assertTrue(any("internal error in ping" in m for m in self.logged))
        self.assertIsNone(self.srv.handle(rpc("ping", id=None)))                  # a notification is never answered

    def test_a_crashing_tool_reports_ok_false(self):
        self.srv.add_tool(Tool("boom", "x", {"type": "object", "properties": {}}, lambda a: 1 / 0))
        r = self.srv.handle(rpc("tools/call", {"name": "boom"}))["result"]
        self.assertEqual(r["structuredContent"]["ok"], False)
        self.assertTrue(any("tool boom failed" in m for m in self.logged))

    def test_output_is_utf8_text_lines_and_is_flushed_after_each_message(self):
        class Out(io.StringIO):
            flushes = 0

            def flush(self):
                Out.flushes += 1
                super().flush()
        out = Out()
        self.srv.serve(io.StringIO(json.dumps(rpc("tools/call", {"name": "lst"}, id=3)) + "\n"
                                   + json.dumps(rpc("ping", id=4)) + "\n"), out)
        self.assertEqual(Out.flushes, 2)
        self.assertIn("é", out.getvalue())                                       # no \u escapes on the wire either
        self.assertNotIn("\\u", out.getvalue())

    def test_a_batch_of_notifications_gets_no_reply(self):
        out = io.StringIO()
        self.srv.serve(io.StringIO(json.dumps([rpc("notifications/initialized", id=None)] * 2) + "\n"), out)
        self.assertEqual(out.getvalue(), "")


class ServerArgumentTests(unittest.TestCase):
    def run_main(self, argv, env=None, stdin=""):
        kv = MemoryKV()
        Repository.init(kv, {"a.py": b"1\n"})
        err, out = io.StringIO(), io.StringIO()
        saved = {k: os.environ.pop(k, None) for k in ("AGENTTRUNK_URL", "CMWS_URL")}
        os.environ.update(env or {})
        try:
            with contextlib.redirect_stderr(err):
                code = mcp_server.main(argv, kv=kv, stdin=io.StringIO(stdin), stdout=out)
        finally:
            for k in ("AGENTTRUNK_URL", "CMWS_URL"):
                os.environ.pop(k, None)
                if saved[k] is not None:
                    os.environ[k] = saved[k]
        return code, err.getvalue(), out.getvalue()

    def test_it_runs_without_a_task_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, out = self.run_main(["--workdir", tmp, "--agent", "x"], stdin=json.dumps(rpc("ping")) + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"], {})
        self.assertIn("agent 'x' on board 'default'", err)

    def test_the_store_address_comes_from_the_environment_when_not_given(self):
        for env, expect in (({"AGENTTRUNK_URL": "http://new:1"}, "http://new:1"), ({"CMWS_URL": "http://old:1"}, "http://old:1"),
                            ({"AGENTTRUNK_URL": "http://new:1", "CMWS_URL": "http://old:1"}, "http://new:1"), ({}, "http://127.0.0.1:8080")):
            code, err, _ = self.run_main(["--agent", "x"], env=env)
            self.assertIn(f"store {expect}", err, env)
        code, err, _ = self.run_main(["--agent", "x", "--url", "http://given:2"], env={"AGENTTRUNK_URL": "http://new:1"})
        self.assertIn("store http://given:2", err)

    def test_startup_problems_are_reported_on_stderr_with_exit_status_2(self):
        code, err, out = self.run_main(["--board", "bad name"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("agenttrunk mcp: bad board name", err)
        code, err, _ = self.run_main(["--tasks", "/no/such/file.json"])
        self.assertEqual(code, 2)
        self.assertIn("agenttrunk mcp:", err)

    def test_the_tool_schemas_forbid_unknown_arguments(self):
        env = Env()
        self.addCleanup(env.close)
        tools = build_server(env.svc("x")).handle(rpc("tools/list"))["result"]["tools"]
        for t in tools:
            self.assertIs(t["inputSchema"]["additionalProperties"], False, t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_file_tools_work_through_the_server(self):
        env = Env()
        self.addCleanup(env.close)
        svc = env.svc("x", file_tools=True)
        svc.create_board([Task("t", "d", ("a.m",))])
        srv = build_server(svc)

        def call(name, **args):
            return srv.handle(rpc("tools/call", {"name": name, "arguments": args}))["result"]["structuredContent"]
        call("claim_task")
        self.assertEqual(call("write_file", path="n.txt", content="N")["ok"], True)
        self.assertIn("n.txt", call("list_files", directory="")["result"])
        self.assertEqual(call("read_file", path="n.txt")["result"], "N")
        self.assertEqual(call("replace_in_file", path="n.txt", old="N", new="M")["ok"], True)
        self.assertEqual(call("move_file", src="n.txt", dst="m.txt")["ok"], True)
        self.assertEqual(call("delete_file", path="m.txt")["ok"], True)
        self.assertEqual(call("write_file", path="x.txt")["ok"], False)             # a missing argument is an error result


import os                                                                           # noqa: E402


class ServiceDetailTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(validate="import stats")
        self.addCleanup(self.env.close)
        self.a, self.b = self.env.svc("alice"), self.env.svc("bob")
        self.a.create_board([Task("t1", "x", ("1.m",)), Task("t2", "y", ("2.m",))])

    def main(self, name="stats.py"):
        return Repository(self.env.kv).main_files()[name].decode()

    def test_a_task_whose_files_another_agent_holds_is_skipped_and_its_claim_given_back(self):
        LeaseTable(self.env.kv, clock=self.env.clock).acquire("someone-else", ["1.m"])
        c = self.a.claim()
        self.assertEqual(c["task"]["id"], "t2")                                      # t1 was skipped
        self.assertEqual(self.a.claims.snapshot()["b1-t1"]["state"], "free")         # not left claimed
        LeaseTable(self.env.kv, clock=self.env.clock).release("someone-else")
        self.assertEqual(self.b.claim()["task"]["id"], "t1")                         # and can be claimed now

    def test_a_task_claimed_by_a_rival_in_the_meantime_is_skipped(self):
        real = self.a.claims.claim
        calls = []

        def claim(task, owner, ttl=None):
            calls.append(task)
            if task == "b1-t1":
                self.b.claims.claim("b1-t1", "bob")                                  # bob gets there first
            return real(task, owner, ttl)
        self.a.claims.claim = claim
        self.assertEqual(self.a.claim()["task"]["id"], "t2")
        self.assertEqual(calls[:2], ["b1-t1", "b1-t2"])

    def test_adhoc_names_survive_a_race(self):
        real_tasks = self.a.tasks
        stale = [2]                                                                   # the name check and the validation

        def tasks():
            got = real_tasks()
            if stale[0]:
                stale[0] -= 1
                return [t for t in got if not t.id.startswith("adhoc")]               # a view from before the rival's add
            return got
        self.b.claim(description="rival", paths=["r.py"])                            # takes adhoc-1
        self.a.tasks = tasks
        c = self.a.claim(description="mine", paths=["m.py"])
        self.assertEqual(c["task"]["id"], "adhoc-2")                                  # the race was noticed, the next name used

    def test_an_explicit_adhoc_name_taken_in_a_race_is_refused(self):
        self.b.claim(task="same", description="rival", paths=["r.py"])
        real_tasks = self.a.tasks
        stale = [2]

        def tasks():
            got = real_tasks()
            if stale[0]:
                stale[0] -= 1
                return [t for t in got if t.id not in ("b1-same", "same")]
            return got
        self.a.tasks = tasks
        with self.assertRaises(TrunkError) as cm:
            self.a.claim(task="same", description="mine", paths=["m.py"])
        self.assertIn("already on the board", str(cm.exception))

    def test_results_say_ok_and_the_board_shows_when_a_claim_expires(self):
        c = self.a.claim()
        self.assertIs(c["ok"], True)
        row = [t for t in self.a.board_view()["tasks"] if t["id"] == "t1"][0]
        self.assertEqual((row["state"], row["by"], row["expires_in"]), ("claimed", "alice", 900))
        self.env.clock.t += 100
        self.assertEqual([t for t in self.a.board_view()["tasks"] if t["id"] == "t1"][0]["expires_in"], 800)
        self.assertIs(self.a.sync()["ok"], True)
        self.assertEqual(self.a.sync()["status"], "up-to-date")
        self.assertIs(self.a.sync()["ok"], True)
        self.assertIs(self.a.escalations()["ok"], True)
        self.assertIs(self.a.release()["ok"], True)

    def test_a_second_sync_is_up_to_date(self):
        ca, cb = self.a.claim("t1"), self.b.claim("t2")
        write(ca, "new.txt", "n\n")
        self.a.submit()
        self.assertEqual(self.b.sync()["status"], "fast-forward")
        self.assertEqual(self.b.sync()["status"], "up-to-date")
        write(cb, "other.txt", "o\n")
        self.assertEqual(self.b.sync()["status"], "up-to-date")                       # nothing new on main
        self.assertEqual(self.b.status()["main_moved"], False)

    def test_the_landed_folder_is_refreshed_with_what_was_merged(self):
        ca, cb = self.a.claim("t1"), self.b.claim("t2")
        write(ca, "from_alice.txt", "a\n")
        self.a.submit()
        write(cb, "from_bob.txt", "b\n")
        self.assertEqual(self.b.submit()["status"], "landed")                           # merged onto alice's work
        self.assertEqual(read(cb, "from_alice.txt"), "a\n")                              # and bob's folder now has it

    def test_re_claiming_a_branch_brings_its_folder_up_to_date(self):
        ca = self.a.claim("t1")
        write(ca, "one.txt", "1\n")
        self.a.release()
        ws = Repository(self.env.kv).resume("b1-t1")                                     # someone else adds to the branch
        ws.write("elsewhere.txt", "e\n")
        ws.commit("elsewhere")
        c2 = self.a.claim("t1")
        self.assertEqual(read(c2, "elsewhere.txt"), "e\n")

    def test_a_released_task_in_memory_mode_is_reopened_fresh(self):
        env = Env()
        self.addCleanup(env.close)
        m = env.svc("mem", file_tools=True)
        m.create_board([Task("t", "d", ("a.m",))])
        m.claim()
        m.file_tool("write_file", {"path": "x.txt", "content": "x"})
        m.release()
        ws = Repository(env.kv).resume("b1-t")
        ws.write("outside.txt", "o")
        ws.commit("outside")
        m.claim()
        self.assertIn("outside.txt", m.file_tool("list_files", {"directory": ""})["result"])   # not the cached session

    def test_a_memory_mode_sync_is_recorded_so_a_restart_keeps_the_merge(self):
        env = Env()
        self.addCleanup(env.close)
        m, other = env.svc("mem", file_tools=True), env.svc("oth")
        m.create_board([Task("t", "d", ("a.m",)), Task("u", "e", ("b.m",))])
        m.claim("t")
        co = other.claim("u")
        write(co, "stats.py", "COUNT = 4\n")
        other.submit()
        m.file_tool("write_file", {"path": "mine.txt", "content": "m"})
        self.assertEqual(m.sync()["status"], "merged")
        restarted = env.svc("mem", file_tools=True)
        out = restarted.file_tool("list_files", {"directory": ""})
        self.assertEqual(out["seq"], 2)                                               # numbering carried on, not restarted
        files = out["result"]
        self.assertIn("mine.txt", files)
        self.assertEqual(restarted.file_tool("read_file", {"path": "stats.py"})["result"], "COUNT = 4\n")

    def test_the_next_step_texts_match_the_situation(self):
        ca, cb = self.a.claim("t1"), self.b.claim("t2")
        write(ca, "stats.py", "COUNT = 1\n")
        write(cb, "stats.py", "COUNT = 5\n")
        self.a.submit()
        self.b.submit()
        self.assertIn("Main changed files you also changed", self.b.status()["next"])
        self.assertIn("sync_with_main", self.b.status()["next"])
        s = self.b.sync()
        self.assertTrue(s["next"].startswith("Resolve the conflict markers"))
        self.assertIn("stats.py", s["next"])
        self.assertIn("keeping the intent of both sides", s["next"])
        self.assertIn("Resolve", self.b.status()["next"])

    def test_a_large_upstream_diff_is_cut_off(self):
        ca, cb = self.a.claim("t1"), self.b.claim("t2")
        write(ca, "stats.py", "".join(f"LINE_{i} = {i}\n" for i in range(2000)) + "COUNT = 1\n")
        self.a.submit()
        write(cb, "stats.py", "COUNT = 2\n")
        s = self.b.sync()
        text = s["main_changes_to_those_files"]
        self.assertTrue(text.endswith("[... cut off]"))
        self.assertLessEqual(len(text), 6000 + len("\n[... cut off]"))

    def test_every_landing_status_maps_to_advice(self):
        c = self.a.claim("t1")
        write(c, "x.txt", "x\n")
        real = self.a.queue.attempt
        cases = {
            "stale": SubmitResult("stale", stale_reads=[type("S", (), {"path": "p.txt"})()]),
            "conflict": SubmitResult("conflict", conflicts=[type("C", (), {"path": "q.txt", "kind": "text"})()]),
            "rejected": SubmitResult("rejected", detail="no good"),
            "contended": SubmitResult("contended"),
            "weird": SubmitResult("weird"),
        }
        for status, result in cases.items():
            self.a.queue.attempt = lambda agent, message, r=result: r
            out = self.a.submit()
            self.assertEqual((out["ok"], out["status"]), (False, status))
            self.assertTrue(out["next"])
            if status == "weird":
                self.assertEqual(out["next"], "Landing returned 'weird'.")
        self.a.queue.attempt = lambda agent, message: cases["conflict"]
        out = self.a.submit()
        self.assertEqual(out["conflicts"], [["q.txt", "text"]])
        self.assertIn("q.txt", out["next"])
        self.a.queue.attempt = lambda agent, message: cases["stale"]
        out = self.a.submit()
        self.assertEqual(out["stale_paths"], ["p.txt"])
        self.assertIn("p.txt", out["next"])
        self.a.queue.attempt = lambda agent, message: cases["rejected"]
        self.assertEqual(self.a.submit()["detail"], "no good")
        self.assertIn("no good", self.a.submit()["next"])
        self.assertIn("main is changing quickly", (setattr(self.a.queue, "attempt", lambda a, m: cases["contended"]), self.a.submit())[1]["next"])
        self.a.queue.attempt = real
        self.assertEqual(self.a.submit()["status"], "landed")

    def test_a_folder_that_cannot_be_saved_to_the_branch_is_reported(self):
        from cmws_ws import sync as sync_module
        c = self.a.claim("t1")
        write(c, "x.txt", "x\n")
        original = sync_module.Checkout.push
        sync_module.Checkout.push = lambda self_, message=None, **kw: sync_module.PushResult("contended")
        try:
            with self.assertRaises(TrunkError) as cm:
                self.a.submit()
        finally:
            sync_module.Checkout.push = original
        self.assertIn("could not save your folder", str(cm.exception))
        self.assertIn("contended", str(cm.exception))


class LeaseScopeTests(unittest.TestCase):
    """Leases are about the files of one project (one main ref), not of the whole store."""

    def two_projects(self):
        kv = MemoryKV()
        for name in ("projA", "projB"):
            Repository.init(kv, {"f.py": b"1\n"}, main=name)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def svc(main, board, who):
            repo = Repository(kv, main=main)
            return TrunkService(repo, board, who, Path(tmp.name) / who, MergeQueue(repo))
        return kv, svc

    def test_projects_sharing_a_store_do_not_block_each_other(self):
        kv, svc = self.two_projects()
        a, b = svc("projA", "ba", "alice"), svc("projB", "bb", "bob")
        a.create_board([Task("t", "x", ("f.py",))])
        b.create_board([Task("t", "x", ("f.py",))])
        self.assertEqual(a.claim()["task"]["id"], "t")
        self.assertEqual(b.claim()["task"]["id"], "t")                      # same file name, other project: no conflict
        self.assertIn("projA/leases", kv.keys())
        self.assertIn("projB/leases", kv.keys())

    def test_boards_on_the_same_project_do_block_each_other_on_the_same_files(self):
        kv, svc = self.two_projects()
        a, c = svc("projA", "ba", "alice"), svc("projA", "bc", "carol")
        a.create_board([Task("t", "x", ("f.py",))])
        c.create_board([Task("u", "x", ("f.py",))])
        a.claim()
        with self.assertRaises(TrunkError) as cm:
            c.claim()
        self.assertIn("no task is ready", str(cm.exception))                # another board holds f.py of the same project

    def test_the_default_project_keeps_the_original_lease_key(self):
        kv = MemoryKV()
        repo = Repository.init(kv, {"f.py": b"1\n"})
        svc = TrunkService(repo, "b", "x", Path(tempfile.mkdtemp()), MergeQueue(repo))
        svc.create_board([Task("t", "x", ("f.py",))])
        svc.claim()
        self.assertIn("leases", kv.keys())

    def test_orchestrated_runs_on_two_projects_do_not_wait_for_each_other(self):
        from cmws_ws.agents import Orchestrator, TaskGraph, WorkerResult
        kv = MemoryKV()
        for name in ("projA", "projB"):
            Repository.init(kv, {"f.py": b"1\n"}, main=name)
        results = {}

        class W:
            name = "w"

            def run(self, task, repo, agent_id):
                ws = repo.resume(agent_id)
                ws.write("f.py", f"{repo.main}\n")
                return WorkerResult("done", "x", ws.commit("x"), ("f.py",))

        def go(main):
            repo = Repository(kv, main=main)
            o = Orchestrator(repo, W(), owner=main, poll=0.01, patience=0.3, claim_ttl=60)
            results[main] = o.run(f"r{main[-1]}", graph=TaskGraph([Task("t", "x", ("f.py",))]))   # run ids are store-wide
        ts = [threading.Thread(target=go, args=(m,)) for m in ("projA", "projB")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertTrue(all(r.ok for r in results.values()), {m: r.report.outcomes for m, r in results.items()})
        self.assertEqual(Repository(kv, main="projB").main_files()["f.py"], b"projB\n")
