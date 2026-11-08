"""AgentTrunk command line.

    agenttrunk --url http://127.0.0.1:8080 init --from ./project
    agenttrunk --url ... clone alice ./alice-work
    agenttrunk status ./alice-work               # also: pull, push -m MSG, submit, resolve, watch
    agenttrunk --url ... log
    agenttrunk --url ... escalations             # work the merge queue left for a person
    agenttrunk run-task alice "fix the parser" --model qwen3:27b --paths src/parser.py --submit
    agenttrunk run-task bob "add tests" --exec claude -p '{prompt}'     # a command-line agent
    agenttrunk orchestrate r1 --goal "add a CLI flag" --model qwen3:27b --validate-cmd "python3 -m unittest"
    agenttrunk run-status r1                     # plan, claims and result of a run (also how to resume it)
    agenttrunk board b1 --tasks tasks.json       # put tasks on a board that agents claim from
    agenttrunk export ./result                   # write the files of main to a folder
    agenttrunk mcp --url ... --board b1          # serve the workspace to MCP clients (Claude Code, Cursor, ...)

Without installing, run ``bin/agenttrunk`` from the repository, or ``python3 -m agenttrunk`` with
``PYTHONPATH=workspace/src``. The URL defaults to $AGENTTRUNK_URL, then http://127.0.0.1:8080
(``make run-gateway``). The old CMWS_* environment variables are still read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .history import log
from .kv import HttpKV, KVUnavailable
from .repo import MAIN, Repository
from .sync import STATE_DIR, Checkout, Watcher


def _env(name: str, default=None):
    """AGENTTRUNK_<NAME>, falling back to the old CMWS_<NAME>."""
    return os.environ.get(f"AGENTTRUNK_{name}", os.environ.get(f"CMWS_{name}", default))


def _read_tree(root: Path, checkout_like: Checkout = None):
    files = {}
    ignore = Checkout.__new__(Checkout)
    ignore.root, ignore._ignore_patterns = root, None
    for dirpath, dirs, names in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = [d for d in dirs if not ignore.ignored(f"{rel_dir}/{d}".lstrip("/"))]
        for n in names:
            rel = f"{rel_dir}/{n}".lstrip("/")
            p = root / rel
            if not ignore.ignored(rel) and p.is_file() and not p.is_symlink():
                files[rel] = p.read_bytes()
    return files


def _print_escalations(items) -> int:
    for e in items:
        print(f"{e['id']}  {e['branch']}  {e['reason']}  {e['detail']}")
        for stage, status, detail in e.get("steps", []):
            print(f"    {stage:7} {status:14} {detail}")
    print(f"{len(items)} escalation(s)")
    return 0


def _subcommand_at(argv, name):
    """Index of the sub-command if it is ``name``: global options come first, the sub-command is the first plain word."""
    i = 0
    while i < len(argv):
        if argv[i] in ("--url", "--main"):
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return i if argv[i] == name else None
    return None


def _board(a, repo: Repository) -> int:
    from .agents.orchestrator import task_from_dict
    from .mcp import TrunkError, TrunkService
    try:
        svc = TrunkService(repo, a.name, "cli")
        if a.tasks:
            tasks = [task_from_dict(d) for d in json.loads(a.tasks.read_text())]
            created = svc.create_board(tasks)
            print("board created" if created else "board already exists; its tasks were kept")
            for w in svc.warnings(tasks):
                print("warning:", w, file=sys.stderr)
        view = svc.board_view()
    except (TrunkError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"board {view['board']}  main {view['main']}  escalations {view['escalations']}")
    for t in view["tasks"]:
        extra = t.get("by") or ", ".join(t.get("waiting_for", [])) or t.get("reason", "")
        print(f"  {t['id']:20} {t['state']:9} {extra}")
    return 0


def _model(a):
    from .agents import OpenAICompatModel
    host = getattr(a, "llm_host", None)
    headers = {"Host": host} if host else None     # e.g. localhost:11434 behind a reverse proxy
    return OpenAICompatModel(a.model, a.llm_url, headers=headers)


def _run_task(a, repo: Repository) -> int:
    from .agents import CliWorker, LoopWorker, OpenAICompatModel, Task, run_task
    if a.argv:
        worker = CliWorker(a.argv)
    elif a.model:
        worker = LoopWorker(_model(a), name=a.model, max_steps=a.max_steps)
    else:
        print("give --model NAME (served at --llm-url) or --exec COMMAND", file=sys.stderr)
        return 2
    result = run_task(worker, repo, Task(a.agent, a.description, tuple(a.paths)), a.agent)
    print(f"{result.status}: {result.summary or result.error}")
    print(f"steps={result.steps} tokens={result.prompt_tokens}+{result.completion_tokens} "
          f"touched={','.join(result.touched) or '-'}")
    if not result.ok:
        return 1
    if a.submit:
        landed = repo.submit(repo.resume(a.agent), result.summary or None)
        print(landed.status)
        return 0 if landed.landed or landed.status == "noop" else 1
    return 0


def _orchestrate(a, repo: Repository) -> int:
    import json
    import shlex
    from .agents import (CliWorker, CommandValidator, LLMPlanner, LoopWorker, MergeQueue, OpenAICompatModel,
                         Orchestrator, StaticPlanner, TaskGraph)
    from .agents.orchestrator import task_from_dict
    if a.argv:
        worker = CliWorker(a.argv)
    elif a.model:
        worker = LoopWorker(_model(a), name=a.model, max_steps=a.max_steps,
                            checkpoint_every=a.checkpoint_every)
    else:
        print("give --model NAME (served at --llm-url) or --exec COMMAND", file=sys.stderr)
        return 2
    graph, planner = None, None
    if a.tasks:
        graph = TaskGraph([task_from_dict(d) for d in json.loads(a.tasks.read_text())])
    elif a.model:
        planner = LLMPlanner(_model(a))
    validator = CommandValidator(shlex.split(a.validate_cmd)) if a.validate_cmd else None
    merger = redo = None
    if a.escalate and a.model:
        from .agents import merger_worker
        merger, redo = merger_worker(_model(a)), worker      # the same model resolves conflicts and redoes tasks
    reapply = None
    if a.reapply:
        from .agents.ladder import ModelReapplier
        from .agents.llm import CliModel
        if a.reapply == "claude":
            reapply = ModelReapplier(CliModel())
        elif a.model:
            quiet = _model(a)
            quiet.extra["reasoning_effort"] = "none"          # a one-answer question: no thinking budget to run out of
            reapply = ModelReapplier(quiet)
        else:
            print("--reapply model needs --model", file=sys.stderr)
            return 2
    try:
        layers = [int(x) for x in a.layers.split(",") if x.strip()]
        queue = MergeQueue(repo, validator=validator, merger=merger, redo_worker=redo, reapply=reapply,
                           continue_worker=worker if (redo is not None and isinstance(worker, LoopWorker)) else None,
                           layers=layers)
    except ValueError as e:
        print(f"--layers: {e}", file=sys.stderr)
        return 2
    orch = Orchestrator(repo, worker, planner=planner, queue=queue, owner=a.owner, parallel=a.parallel,
                        claim_ttl=a.claim_ttl)
    try:
        summary = orch.run(a.run_id, goal=a.goal, graph=graph)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"run {summary.run_id} ({'resumed' if summary.resumed else 'planned'} by {orch.owner}): main = {summary.main[:12]}")
    for tid, o in summary.report.outcomes.items():
        print(f"  {tid:20} {o.status:9} {o.reason}")
    return 0 if summary.ok else 1


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        return _main(args)
    except KVUnavailable as e:
        url = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == "--url"), _env("URL", "http://127.0.0.1:8080"))
        print(f"agenttrunk: cannot reach the gateway at {url}: {e}\n"
              "Is it running? Start it with:  make run-gateway ARGS=\"--port 8080 --groups 2 --replicas 3\"\n"
              "and point agenttrunk at it with --url or $AGENTTRUNK_URL.", file=sys.stderr)
        return 3


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agenttrunk", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=_env("URL", "http://127.0.0.1:8080"))
    ap.add_argument("--main", default=MAIN, help="name of the main ref")
    ap.add_argument("--version", action="version", version=f"agenttrunk {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create the repository, optionally from a directory")
    p.add_argument("--from", dest="source", type=Path)
    p = sub.add_parser("clone", help="create a local folder for a workspace branch")
    p.add_argument("agent"), p.add_argument("dir", type=Path)
    for name in ("status", "pull", "push", "submit", "watch"):
        p = sub.add_parser(name)
        p.add_argument("dir", type=Path, nargs="?", default=Path("."))
        if name in ("push", "submit"):
            p.add_argument("-m", "--message")
        if name == "watch":
            p.add_argument("--interval", type=float, default=1.0)
            p.add_argument("--debounce", type=float, default=2.0)
    p = sub.add_parser("resolve", help="mark a non-text conflict as handled")
    p.add_argument("dir", type=Path), p.add_argument("path")
    p = sub.add_parser("run-task", help="run one agent on a task in its own branch")
    p.add_argument("agent"), p.add_argument("description")
    p.add_argument("--paths", nargs="*", default=[], help="files or folders the task should stay within")
    p.add_argument("--model", help="model name served at --llm-url (default worker)")
    p.add_argument("--llm-url", default=_env("LLM_URL", "http://127.0.0.1:11434/v1"))
    p.add_argument("--llm-host", default=_env("LLM_HOST"),
                   help="Host header to send (Ollama refuses unknown hosts, so behind a proxy use localhost:11434)")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--exec", dest="argv", nargs=argparse.REMAINDER,
                   help="run this command in a checkout instead of the built-in loop; {prompt} is replaced")
    p.add_argument("--submit", action="store_true", help="land the branch on main afterwards")
    p = sub.add_parser("orchestrate", help="plan a goal, run the tasks in parallel and land them (resumable by run id)")
    p.add_argument("run_id")
    p.add_argument("--goal", help="what to build; split into tasks by the model")
    p.add_argument("--tasks", type=Path, help="JSON file with the task list instead of a goal")
    p.add_argument("--model"), p.add_argument("--llm-url", default=_env("LLM_URL", "http://127.0.0.1:11434/v1"))
    p.add_argument("--llm-host", default=_env("LLM_HOST"),
                   help="Host header to send (Ollama refuses unknown hosts, so behind a proxy use localhost:11434)")
    p.add_argument("--exec", dest="argv", nargs=argparse.REMAINDER, help="use a command-line agent for every task")
    p.add_argument("--validate-cmd", help="command run on the merged tree before main moves (exit 0 = accept)")
    p.add_argument("--escalate", action="store_true",
                   help="when a branch cannot land, let the model resolve the conflict, then redo the task")
    p.add_argument("--reapply", choices=["model", "claude"],
                   help="cost ladder layer 2: when a file both the agent and main changed went stale, ask what it would be "
                        "if the task were done again on main (the model of --model, or `claude -p`) and let a program compare; "
                        "needs --escalate for the rest of the ladder")
    p.add_argument("--layers", default="2,3,4",
                   help="which ladder layers may be used (2 re-apply, 3 continue the agent's conversation, 4 redo); "
                        "a layer also needs what it runs on: --reapply for 2, --escalate with --model for 3 and 4")
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--claim-ttl", type=float, default=300.0,
                   help="seconds a dead orchestrator's tasks stay claimed before another run may take them over")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--checkpoint-every", type=int, default=3, help="tool calls between checkpoints of a task")
    p.add_argument("--owner", help="name of this orchestrator (default: host and random suffix)")
    p = sub.add_parser("run-status", help="plan, claims and result of a run")
    p.add_argument("run_id")
    p = sub.add_parser("board", help="create a board of tasks for agents to claim, or show one")
    p.add_argument("name")
    p.add_argument("--tasks", type=Path, help="JSON file with the tasks")
    p = sub.add_parser("mcp", help="serve the workspace to MCP clients (Claude Code, Cursor, ...); see `agenttrunk mcp -h`",
                       add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("escalations", help="work the merge queue could not land and left for a person")
    p = sub.add_parser("export", help="write the files of main to a folder (to run or inspect the shared result)")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("log", help="history of main")
    p.add_argument("--limit", type=int, default=20)
    argv = sys.argv[1:] if argv is None else list(argv)
    mcp_at = _subcommand_at(argv, "mcp")
    if mcp_at is not None:                              # `mcp` has options of its own; hand them over untouched
        from .mcp.server import main as mcp_main
        glob = ap.parse_args(argv[:mcp_at] + ["log"])   # reuse the global --url / --main parsing
        return mcp_main(["--url", glob.url, "--main", glob.main] + argv[mcp_at + 1:])
    a = ap.parse_args(argv)
    repo_kwargs = {"main": a.main}
    kv = HttpKV(a.url)
    if a.cmd == "init":
        files = _read_tree(a.source) if a.source else None
        repo = Repository.init(kv, files, **repo_kwargs)
        print(f"main = {repo.head()[:12]} ({len(files or {})} files imported)")
        return 0
    repo = Repository(kv, **repo_kwargs)
    if a.cmd == "clone":
        co = Checkout.clone(repo, a.agent, a.dir)
        print(f"cloned ws/{a.agent} at {co.base[:12]} into {a.dir}")
        return 0
    if a.cmd == "run-task":
        return _run_task(a, repo)
    if a.cmd == "orchestrate":
        return _orchestrate(a, repo)
    if a.cmd == "board":
        return _board(a, repo)
    if a.cmd == "run-status":
        from .agents import run_status
        st = run_status(kv, a.run_id)
        if st["plan"] is None:
            print("no such run")
            return 1
        print("goal:", st["plan"]["goal"])
        for t in st["plan"]["tasks"]:
            c = st["claims"].get(f"{a.run_id}-{t['id']}", {})
            state = c.get("outcome", {}).get("status", c.get("state", "pending")) if c else "pending"
            print(f"  {t['id']:20} {state:10} {c.get('owner', '')}")
        if st["result"]:
            print("result:", json.dumps(st["result"], sort_keys=True))
        return 0
    if a.cmd == "escalations":
        from .agents import Escalations
        return _print_escalations(Escalations(kv).list())
    if a.cmd == "export":
        files = repo.main_files()
        a.dir.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            target = a.dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        print(f"{len(files)} files of {a.main} ({repo.head()[:12]}) written to {a.dir}")
        return 0
    if a.cmd == "log":
        for cid, c in log(repo.store, repo.head(), a.limit):
            print(f"{cid[:12]}  {c.author:12}  {c.message}")
        return 0

    co = Checkout.open(repo, a.dir)
    if a.cmd == "status":
        for path, code in co.status().items():
            print(code, path)
        for path, kind in co.unresolved().items():
            print("!", path, f"({kind} conflict)")
    elif a.cmd == "pull":
        r = co.pull()
        print(r.status, *r.updated)
        for path, kind in r.conflicts.items():
            print("!", path, f"({kind} conflict)")
        return 1 if r.conflicts else 0
    elif a.cmd == "push":
        r = co.push(a.message)
        print(r.status, *(r.files or []))
        for path, kind in r.conflicts.items():
            print("!", path, f"({kind} conflict)")
        return 0 if r.status in ("pushed", "nothing") else 1
    elif a.cmd == "submit":
        r = co.submit(a.message)
        status = getattr(r, "status", "?")
        print(status, *(getattr(r, "files", None) or []))
        for c in getattr(r, "conflicts", None) or []:
            print("!", getattr(c, "path", c), getattr(c, "kind", ""))
        for s in getattr(r, "stale_reads", None) or []:
            print("stale read:", s.path)
        return 0 if status in ("fast-forward", "merged", "noop", "pushed", "nothing") else 1
    elif a.cmd == "resolve":
        co.resolve(a.path)
    elif a.cmd == "watch":
        w = Watcher(co, interval=a.interval, debounce=a.debounce,
                    on_event=lambda kind, info: print(time.strftime("%H:%M:%S"), kind,
                                                      getattr(info, "status", ""), flush=True))
        print(f"watching {a.dir} (Ctrl-C to stop)")
        w.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            w.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
