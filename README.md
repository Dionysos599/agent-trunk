# AgentTrunk: A Consistent Multiagent Work System

Let several AI agents work on the same code base at the same time without stepping on each other.

Each agent gets its own private copy of the project (a *fork*, which costs one reference, not a copy of
the files). When it is done, its work is merged back through a single, strongly consistent commit point:
changes that merge cleanly and pass your checks land, everything else is retried, resolved or handed to a
person. Nothing an agent does can corrupt the shared state, and a crashed agent or orchestrator can be
resumed.

Underneath is a replicated, sharded key/value store that survives the loss of replicas. On top of it sits a
versioned workspace (content-addressed files, compare-and-set references, three-way merge) and an
orchestrator that plans a goal into tasks, runs agents in parallel, and lands their results.

```
goal ──► planner ──► tasks ──► scheduler ──► agents (each on its own fork)
                                                 │
                              validating merge queue ──► main
                                                 │
                    replicated key/value store (survives replica failures)
```

You can use it in four ways:

| You want to | Use |
|---|---|
| let agents in Claude Code, Cursor or any other MCP client share one project safely | the MCP server, `agenttrunk mcp` |
| hand a goal to several agents and get validated results | the `agenttrunk orchestrate` command |
| a git-like workspace for agents (fork, edit, merge, stale-read detection), embedded in your own program | the Python library `cmws_ws` |
| a linearizable, fault-tolerant key/value store with compare-and-set | the HTTP gateway |

## Requirements

* Java 17 or newer and `make` (the store and gateway)
* Python 3.10 or newer, standard library only (the workspace, the agents and the command line)
* For real agents: any OpenAI-compatible chat endpoint with tool calling (for example a local
  [Ollama](https://ollama.com)), or a command-line agent such as `claude`. No paid service is required by
  the project itself.

## Quick start

Build and start the store with an HTTP gateway (2 shard groups of 3 replicas each, in one process):

```bash
make
make run-gateway ARGS="--port 8080 --groups 2 --replicas 3"
```

Make the `agenttrunk` command available (it is a small wrapper in this repository; or install it with
`pip install -e workspace`, which needs no other package):

```bash
export PATH="$PWD/bin:$PATH"
export AGENTTRUNK_URL=http://127.0.0.1:8080      # where the gateway is; this is also the default
```

Put a project in it and look at it:

```bash
agenttrunk init --from ./my-project
agenttrunk log
```

Give a goal to agents (here a local Ollama model; the merged result must pass your test command before
`main` moves):

```bash
agenttrunk orchestrate run1 \
    --goal "Add a div(a, b) function to mathx.py that raises ValueError on zero, with tests" \
    --model qwen2.5-coder:7b --parallel 3 --escalate \
    --validate-cmd "python3 -m unittest discover"
agenttrunk run-status run1
```

The model splits the goal into tasks, the tasks run in parallel on their own forks, and each finished
branch is merged into `main` only if the merged tree passes `--validate-cmd`. Run the same command again
with the same run id after a crash and it resumes instead of starting over.

## Using it from Claude Code, Cursor and other MCP clients

`agenttrunk mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. Every client
session that connects is one agent: it takes a task from a shared board, gets its own private branch of the
project (normally as a folder it edits with its own tools), and submits; the merge is checked against your
validation command before `main` moves. Several sessions, on this machine or on several, share one project
without overwriting each other.

Put the tasks on a board (optional: agents can also add tasks of their own):

```bash
cat > tasks.json <<'JSON'
[{"id": "api",  "description": "Add a /health endpoint", "paths": ["server.py"]},
 {"id": "docs", "description": "Document the endpoint",  "paths": ["README.md"], "depends_on": ["api"]}]
JSON
agenttrunk board myboard --tasks tasks.json
```

Add the server to each client, one name per agent (keep the name the same across restarts and the agent
resumes its task). For Claude Code:

```bash
claude mcp add agenttrunk -- agenttrunk mcp --board myboard --agent alice \
    --validate-cmd "python3 -m unittest discover"
```

or in any client's MCP configuration:

```json
{"mcpServers": {"agenttrunk": {"command": "agenttrunk",
  "args": ["mcp", "--board", "myboard", "--agent", "alice", "--validate-cmd", "python3 -m unittest discover"]}}}
```

The agent then has these tools; every answer says what to do next:

| tool | what it does |
|---|---|
| `show_board` | which tasks are open, claimed, waiting for another task, done or failed |
| `claim_task` | take the next ready task (or a named one, or add one of your own with `description` and `paths`); you get a private branch and a folder |
| `task_status` | what you changed, whether `main` moved under you, files with conflict markers |
| `sync_with_main` | bring other agents' landed work into your branch; real conflicts become markers; it shows what `main` changed in files you also changed |
| `submit_task` | merge into `main` after the check: `landed`, or what to do (sync, resolve, fix the failed check) |
| `release_task` | give the task back |
| `list_escalations` | work the system left for a person |

Resources: `agenttrunk://board`, `agenttrunk://main` and `agenttrunk://main/{path}`. Options: `--workdir`
(where task folders go), `--claim-ttl` (how long a silent agent keeps its task), `--tasks`, `--main`, `--url`.
For a client without a file system add `--file-tools`: the branch is then edited through `list_files`,
`read_file`, `write_file`, `replace_in_file`, `delete_file` and `move_file`, each of which accepts a
`seq` number so that a retried call runs once. The same workflow without MCP is available to scripts as
`agenttrunk board`, `agenttrunk clone` / `push` / `submit` (see below).

A ready-made test project with ten tasks that collide on two shared files, a setup script that writes one MCP
configuration per agent, scripted agents to try it without a model (with an optional chaos mode that kills replicas
and an agent's server while it runs), and a checker is in [examples/mcp-demo](examples/mcp-demo). `agenttrunk export DIR` writes the files of `main` to a folder.

## Using the workspace from Python

```python
from cmws_ws.kv import HttpKV                 # or MemoryKV() for an in-process store
from cmws_ws.repo import Repository

repo = Repository.init(HttpKV("http://127.0.0.1:8080"), {"app.py": b"print('hi')\n"})

alice, bob = repo.fork("alice"), repo.fork("bob")     # private snapshots of main
alice.write("app.py", "print('hello')\n")
alice.commit("friendlier greeting")
bob.write("README.md", "# demo\n")
bob.commit("add readme")

print(repo.submit(alice).status)    # fast-forward
print(repo.submit(bob).status)      # merged  (bob's work is merged onto alice's)
```

`submit` merges the branch with whatever landed in the meantime and moves `main` with a compare-and-set, so
concurrent submissions form one history. It reports instead of guessing when it cannot land:

| status | meaning |
|---|---|
| `fast-forward`, `merged` | landed |
| `noop` | nothing to land, or it is already in `main` |
| `conflict` | the same lines were changed differently; the result lists the conflicting paths |
| `stale` | a file the agent read has changed since, even if the agent's edits do not overlap |
| `rejected` | your `validate` function refused the merged result |

Stale-read detection matters for agents that read a value and write one derived from it (a counter, a
version number): a plain textual merge would silently lose one of two concurrent increments.

## Running agents

An agent is anything that implements `run(task, repo, agent_id)`; two are included.

* **`LoopWorker`**: a tool-calling loop over any OpenAI-compatible endpoint, with file tools (list, read,
  write, replace, delete, move) and a `finish` call. With `checkpoint_every=n` it commits and records its
  conversation every `n` tool calls and resumes from the last record after a crash.
* **`CliWorker`**: checks the branch out into a folder, runs a command-line agent there and pushes the
  result, e.g. `CliWorker(["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"])`.

```python
from cmws_ws.agents import (LoopWorker, MergeQueue, CommandValidator, OpenAICompatModel,
                            Orchestrator, Task, TaskGraph, merger_worker)

model = OpenAICompatModel("qwen2.5-coder:7b", "http://127.0.0.1:11434/v1")
worker = LoopWorker(model, checkpoint_every=3)
queue = MergeQueue(repo,
                   validator=CommandValidator(["python3", "-m", "unittest", "discover"]),
                   merger=merger_worker(model),        # tries to resolve conflicts
                   redo_worker=worker,                 # then redoes the task on the new main
                   max_redos=3)                        # then a person (see `cli escalations`)
tasks = TaskGraph([Task("api", "Add a /health endpoint", ("server.py",)),
                   Task("docs", "Document the endpoint", ("README.md",), depends_on=("api",))])
summary = Orchestrator(repo, worker, queue=queue, parallel=4).run("run1", graph=tasks)
print(summary.landed, summary.failed, summary.skipped)
```

A task lists the files it expects to touch. Tasks with disjoint files run at the same time; tasks that
overlap run one after another, each starting from a `main` that already contains the earlier work. If a
task touches more than it declared, it is noted, and the merge machinery still protects `main`.

### What happens to a branch that cannot land

1. automatic three-way merge (line-level merge for text, rename-aware for files);
2. a merger agent that sees both sides' intent and the conflict markers;
3. a redo of the task on top of what landed, with a summary of what changed;
4. a durable record for a person: `agenttrunk escalations`.

At every step the merged result must pass your validator, and a resolution that silently keeps only one
side, a branch that changed nothing although the agent said "done", and files an agent changed without
reading are all treated as failures to retry, not as success.

### Telling the system what your edits mean

Plain text cannot say whether two edits to a file are independent, so by default the system is careful: a file an
agent read and that someone else changed since is *stale* and the landing waits. Two optional declarations let you say
more, and each only ever relaxes or adds a check for the files you name.

**Merge policies.** A file `.agenttrunk-merge` at the project root (read from `main`, so an agent cannot relax its own
landing) lists a pattern and a policy per line; a task can also declare `"merge": {"registry.py": "append"}`.

```
registry.py   append       # lines added at the same place by several agents are all kept, in landing order
CHANGELOG.md  append
*.md          converge     # identical edits are agreement
```

| policy | meaning |
|---|---|
| `relative` (default) | an edit may depend on what the agent read: stale reads are refused |
| `converge` | identical edits are agreement; reads of this file are not validated |
| `append` | pure insertions at the same place commute; both sides' lines are kept. A modified or deleted line is still a conflict |

`append` is for files where order does not matter (a registry, a changelog). Do not use it for numbered migrations or
ids handed out in sequence. In the ten-task demo, declaring the two shared files `append` takes the number of syncs from
24 to 0 (`python3 examples/mcp-demo/simulate.py --append`).

**Acceptance checks.** A task can say what must be true *because it landed*, compared with `main` immediately before
the landing:

```json
{"id": "count", "description": "Increase COUNT by one", "paths": ["stats.py"],
 "accept": [{"kind": "increases", "path": "stats.py", "pattern": "COUNT = (\\d+)", "by": 1},
            {"kind": "contains", "path": "registry.py", "text": "REGISTRY['count']"}]}
```

Kinds: `contains`, `absent`, `increases`, `decreases`, `exists`, `missing`, `command` (runs without a shell, in a folder
holding the result; optionally also compares a number in its output between the old and the new tree). The checks are
validated strictly when the task is declared, they cost no model tokens, and a failure is reported to the agent as
`rejected` with the reason. `agenttrunk board` warns about a check that already passes on the current `main` (it could
not show that the task did its work).

Independently of any declaration, a landing is also refused when lines that either side *added or removed on purpose*
did not survive the merge (for example a merger agent that keeps one side).

### Surviving crashes

* Tool calls carry sequence numbers and run once; a retried call returns the recorded answer.
* Tasks are claimed with an expiring claim; if an orchestrator dies, another one (or the same one,
  restarted) takes its tasks over once the claim runs out, and the replaced owner can no longer land.
* Re-running `orchestrate` with the same run id skips finished tasks and continues the rest.
  `--claim-ttl` sets how long a dead orchestrator's tasks stay claimed (default 300 s).

## Working in a local folder

Any editor or tool can work on a branch through a plain folder:

```bash
agenttrunk clone alice ./alice-work
cd alice-work                   # edit files with anything
agenttrunk status               # also: pull, push -m "message", submit, resolve <path>, watch
```

`watch` keeps the folder and the branch in sync in the background. Conflicts appear as markers in the files
and block pushing until you resolve them; edits made while offline are merged when the store is reachable
again.

## The key/value store

The gateway exposes the store over HTTP, including compare-and-set and fault injection:

```bash
curl -X PUT  --data 'hello'  localhost:8080/kv/greeting          # write
curl                         localhost:8080/kv/greeting          # read (404 if missing)
curl -X POST --data 'v1'     localhost:8080/kv/head/cas          # create: the key must not exist
curl -X POST --data 'v2'     'localhost:8080/kv/head/cas?expected=v1'   # advance if it still holds v1
curl -X POST --data 'alice'  localhost:8080/kv/owner/put-if-absent
curl -X POST 'localhost:8080/admin/kill?node=server1-2'          # cut a replica off
curl -X POST 'localhost:8080/admin/revive?node=server1-2'
```

The conditional operations answer `{"success":true|false,"value":...}` where `value` is what the key holds
afterwards. Add `--data-dir /some/dir` to the gateway arguments and a restarted cluster keeps its data.
A majority of replicas in each group must be reachable for operations to complete.

## Command reference

`agenttrunk [--url URL] [--main REF] COMMAND`, with the URL defaulting to `$AGENTTRUNK_URL`, then
`http://127.0.0.1:8080`. Without installing: `bin/agenttrunk`, or `PYTHONPATH=workspace/src python3 -m agenttrunk`.
(The old `CMWS_*` environment variables and `python3 -m cmws_ws.cli` still work; the Python package is
`cmws_ws`.)

| command | what it does |
|---|---|
| `init [--from DIR]` | create the repository, optionally from a folder |
| `log [--limit N]` | history of `main` |
| `clone AGENT DIR`, `status`, `pull`, `push`, `submit`, `resolve`, `watch` | work on a branch in a folder |
| `run-task AGENT "description" --model M \| --exec CMD…` | run one agent on a task in its own branch (`--submit` to land it) |
| `orchestrate RUN_ID --goal G \| --tasks FILE.json` | plan, run in parallel, validate and land; resumable |
| `run-status RUN_ID` | the plan, each task's state and the result of a run |
| `board NAME [--tasks FILE]` | create a board of tasks for agents to claim, or show it |
| `mcp [--board B --agent A ...]` | serve the workspace to MCP clients (see above) |
| `export DIR` | write the files of `main` to a folder |
| `escalations` | work the merge queue could not land and left for a person |

`orchestrate` options: `--model` and `--llm-url` (default `http://127.0.0.1:11434/v1`, or `$AGENTTRUNK_LLM_URL`),
`--exec CMD…` to use a command-line agent for every task, `--validate-cmd`, `--parallel`, `--escalate`
(let the model resolve conflicts and redo tasks), `--checkpoint-every`, `--claim-ttl`, `--owner`, and
`--llm-host` for a reverse proxy in front of Ollama that needs a `Host` header such as `localhost:11434`.
`--tasks` takes a JSON list of `{"id", "description", "paths", "depends_on"}`.

Task ids are letters, digits, `_` and `-`; run ids are letters, digits and `_` (at most 32). Inside a run a
task is named `RUN-TASK`, so two runs can use the same task names.

## Evaluation

`workspace/eval/` compares coordination strategies on tasks that collide on purpose (a lost update, adjacent
edits, a rename that text-merges into a broken program) with perfect agents, a local model and a mix of a
local model and a command-line agent, and injects faults into the real store. The results and what they
mean are in [docs/eval/results.md](docs/eval/results.md). In short: agents sharing one folder lose updates
and can leave a broken program; fork + validating merge queue lost none and left `main` valid in every
run once the checks described above were in place, at a few percent overhead.

![Updates lost per run, by coordination strategy](docs/eval/lost-updates.svg)

```bash
python3 workspace/eval/run_eval.py --backends oracle --reps 5 --out out.jsonl && python3 workspace/eval/report.py out.jsonl
```

## Build and test

```bash
make                              # compile everything
make test-workspace               # workspace, agents, MCP, evaluation and cluster integration tests
python3 run-tests.py -l 5         # the store's own tests (unit, integration, gateway, durability)
python3 run-tests.py -l 4 -p 2    # sharded store suite (also -l 1 .. -l 3)
```

Cross-group multi-key transactions are out of scope; the suite under `ShardStorePart2Test` is not expected
to pass.

## Layout

```
modules/rpc-kvstore       at-most-once layer and the key/value state machine
modules/primary-backup    primary-backup replication with a view service
modules/paxos             multi-Paxos replicated state machine
modules/sharded-kv        sharded store with live reconfiguration
modules/gateway           HTTP front end and in-process cluster
workspace/src/cmws_ws     versioned workspace, merge, sync, leases, claims, agents, MCP server, CLI
bin/agenttrunk            the command, runnable from a checkout without installing
workspace/eval            evaluation harness and recorded results
workspace/tests           tests, including runs against the real gateway with replicas killed
docs/design               design documents
docs/eval                 evaluation report
jars/                     third-party and test-harness binaries
```

Protocol logic never performs I/O: a core returns effects (`Send`, `SetTimer`, `Persist`, …) and a thin node
adapter carries them out, which is what makes the replication code testable.

## Origin

See [NOTICE](NOTICE).
