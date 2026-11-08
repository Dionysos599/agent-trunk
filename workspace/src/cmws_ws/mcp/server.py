"""The AgentTrunk MCP server: the workflow tools, the optional file tools, and the resources.

    agenttrunk mcp --url http://127.0.0.1:8080 --board myboard --agent alice

Add it to an MCP client (for example Claude Code: ``claude mcp add agenttrunk -- agenttrunk mcp ...``). Each
client session is one agent; give every session its own ``--agent`` name so that a restarted session resumes
its task.
"""

from __future__ import annotations

import json
import shlex
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from ..agents.mergequeue import CommandValidator, MergeQueue
from ..agents.worker import Task
from ..kv import HttpKV, KV
from ..repo import MAIN, Repository
from .protocol import McpServer, Resource, ResourceTemplate, Tool, ToolFailure
from .service import TrunkError, TrunkService

from .. import __version__ as VERSION  # noqa: E402

INSTRUCTIONS = """AgentTrunk lets several agents work on one project at the same time without overwriting each other.
Workflow: show_board (what is open) -> claim_task (you get your own private branch of the project) -> do the
work -> submit_task (it is merged into the shared main and checked). Stay inside the paths your task declares.
If submit_task says main changed, call sync_with_main, look at what main changed, adjust, and submit again.
Conflicts come back as markers in the files for you to resolve; keep both sides' lines. If the task lists
'accept' conditions, they are checked against main at the moment you land. Every result has a 'next' field saying what
to do. If you get stuck, release_task so another agent can take it."""

_S = {"type": "string"}


def _schema(props: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def build_server(service: TrunkService) -> McpServer:
    srv = McpServer("agenttrunk", VERSION, INSTRUCTIONS)

    def call(fn):
        def run(args: dict):
            try:
                return fn(**args)
            except TrunkError as e:
                raise ToolFailure(str(e))
        return run

    srv.add_tool(Tool("show_board", "List the tasks on the board: which are open, claimed, waiting for another task, done "
                      "or failed, and which one you hold.", _schema({}), call(lambda: service.board_view())))
    srv.add_tool(Tool("claim_task",
                      "Take a task to work on. Without arguments you get the next task that is ready; with task_id a "
                      "specific one. With description (and the files you expect to change in paths) you add a task of "
                      "your own to the board and take it. You get a private branch and, normally, a folder to edit.",
                      _schema({"task_id": {**_S, "description": "id of a task on the board"},
                               "description": {**_S, "description": "what to do, for a task you add yourself"},
                               "paths": {"type": "array", "items": _S, "description": "files or folders you expect to change"}}),
                      call(lambda task_id=None, description=None, paths=None: service.claim(task_id, description, paths))))
    srv.add_tool(Tool("task_status", "Your task's state: what you changed, whether main has moved under you, files with "
                      "conflict markers, and what to do next.", _schema({}), call(lambda: service.status())))
    srv.add_tool(Tool("sync_with_main", "Bring what other agents have landed on main into your branch and folder. Clean "
                      "merges just happen; real conflicts are left as markers. The result lists files both of you changed "
                      "and what main changed in them: check your work still makes sense on top of it.",
                      _schema({}), call(lambda: service.sync())))
    srv.add_tool(Tool("submit_task", "Merge your work into main. It is checked first (the merged result must pass the "
                      "project's validation). Result 'landed' means done; otherwise 'next' says what to do (sync, resolve "
                      "conflicts, fix a failed check).", _schema({"message": {**_S, "description": "short summary of the change"}}),
                      call(lambda message=None: service.submit(message))))
    srv.add_tool(Tool("release_task", "Give up your task without landing it, so another agent can take it. Your branch is kept.",
                      _schema({"reason": _S}), call(lambda reason="": service.release(reason))))
    srv.add_tool(Tool("add_tasks", "For an orchestrating agent: put several tasks on the board at once. Each task is an object with id, "
                      "description, and optionally paths, depends_on, merge (path -> relative|converge|append), accept (checks) and layers.",
                      _schema({"tasks": {"type": "array", "items": {"type": "object"}, "description": "the tasks"}}, ["tasks"]),
                      call(lambda tasks: service.add_tasks(tasks))))
    srv.add_tool(Tool("wait_for_tasks", "Block until the named tasks (default: all on the board) are done, failed or blocked, or until "
                      "timeout seconds (at most 600) pass. With any_of, return as soon as one is settled.",
                      _schema({"task_ids": {"type": "array", "items": _S}, "timeout": {"type": "number"}, "any_of": {"type": "boolean"}}),
                      call(lambda task_ids=None, timeout=60, any_of=False: service.wait_for_tasks(task_ids, timeout, any_of))))
    srv.add_tool(Tool("board_digest", "A compact summary of the board: what landed, what failed and why in one line each, and what needs "
                      "a decision with the conflicting files; no diffs.", _schema({"max_items": {"type": "integer"}}),
                      call(lambda max_items=25: service.board_digest(max_items))))
    srv.add_tool(Tool("explain_escalation", "The full record of one escalated item (from board_digest's needs_decision) with what its "
                      "branch changed.", _schema({"escalation_id": _S}, ["escalation_id"]),
                      call(lambda escalation_id: service.explain_escalation(escalation_id))))
    srv.add_tool(Tool("list_escalations", "Work the system could not land and left for a person.", _schema({}),
                      call(lambda: service.escalations())))

    if service.file_tools:
        seq = {"type": "integer", "description": "optional call number; a repeated number returns the earlier result instead of running again"}

        def ft(name):
            return call(lambda **a: service.file_tool(name, a))
        srv.add_tool(Tool("list_files", "List a folder of your branch ('' is the project root).",
                          _schema({"directory": _S, "seq": seq}), ft("list_files")))
        srv.add_tool(Tool("read_file", "Read a text file from your branch.", _schema({"path": _S, "seq": seq}, ["path"]), ft("read_file")))
        srv.add_tool(Tool("write_file", "Create or completely replace a file.", _schema({"path": _S, "content": _S, "seq": seq}, ["path", "content"]),
                          ft("write_file")))
        srv.add_tool(Tool("replace_in_file", "Replace one exact piece of text (it must occur once) in a file.",
                          _schema({"path": _S, "old": _S, "new": _S, "seq": seq}, ["path", "old", "new"]), ft("replace_in_file")))
        srv.add_tool(Tool("delete_file", "Delete a file or folder.", _schema({"path": _S, "seq": seq}, ["path"]), ft("delete_file")))
        srv.add_tool(Tool("move_file", "Rename or move a file or folder.", _schema({"src": _S, "dst": _S, "seq": seq}, ["src", "dst"]),
                          ft("move_file")))

    def main_tree() -> str:
        return json.dumps({"main": service.repo.head()[:12], "files": service.main_files()}, indent=2)
    srv.resources["agenttrunk://board"] = Resource("agenttrunk://board", "board", "Tasks and who holds them",
                                                   lambda: json.dumps(service.board_view(), indent=2))
    srv.resources["agenttrunk://main"] = Resource("agenttrunk://main", "main", "Files on the shared main", main_tree)
    srv.templates.append(ResourceTemplate("agenttrunk://main/{path}", "file-on-main", "One file as it is on the shared main",
                                          service.read_main))
    return srv


def _arg_parser():
    import argparse
    ap = argparse.ArgumentParser(prog="agenttrunk mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="gateway URL (default $AGENTTRUNK_URL, then http://127.0.0.1:8080)")
    ap.add_argument("--main", default=MAIN, help="name of the main ref")
    ap.add_argument("--board", default="default", help="board name (letters, digits, _; at most 32)")
    ap.add_argument("--tasks", type=Path, help="JSON file with the board's tasks; stored if the board has none yet")
    ap.add_argument("--agent", default=None, help="this agent's name; keep it the same across restarts to resume")
    ap.add_argument("--workdir", type=Path, default=Path("agenttrunk-work"), help="where task folders are created")
    ap.add_argument("--validate-cmd", help="command run on the merged result before main moves (exit 0 = accept)")
    ap.add_argument("--file-tools", action="store_true",
                    help="no folder: offer file tools instead (for clients without a file system)")
    ap.add_argument("--claim-ttl", type=float, default=900.0, help="seconds before an idle agent's task can be taken over")
    return ap


def make_service(a, kv: KV) -> TrunkService:
    repo = Repository(kv, main=a.main)
    validator = CommandValidator(shlex.split(a.validate_cmd)) if a.validate_cmd else None
    queue = MergeQueue(repo, validator=validator)
    owner = a.agent or f"agent-{uuid.uuid4().hex[:6]}"
    svc = TrunkService(repo, a.board, owner, a.workdir, queue, file_tools=a.file_tools, claim_ttl=a.claim_ttl)
    if a.tasks:
        from ..agents.orchestrator import task_from_dict
        svc.create_board([task_from_dict(d) for d in json.loads(a.tasks.read_text())])
    return svc


def main(argv=None, kv: Optional[KV] = None, stdin=None, stdout=None) -> int:
    import os
    a = _arg_parser().parse_args(argv)
    url = a.url or os.environ.get("AGENTTRUNK_URL", os.environ.get("CMWS_URL", "http://127.0.0.1:8080"))
    try:
        svc = make_service(a, kv or HttpKV(url))
    except (TrunkError, ValueError, OSError) as e:
        print(f"agenttrunk mcp: {e}", file=sys.stderr)
        return 2
    print(f"agenttrunk mcp: agent {svc.owner!r} on board {svc.board!r}, store {url}", file=sys.stderr, flush=True)
    build_server(svc).serve(stdin or sys.stdin, stdout or sys.stdout)
    return 0
