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

You can use it at three levels:

| You want to | Use |
|---|---|
| a linearizable, fault-tolerant key/value store with compare-and-set | the HTTP gateway |
| a git-like workspace for agents (fork, edit, merge, stale-read detection), embedded in your own program | the Python library `cmws_ws` |
| to hand a goal to several agents and get validated results | the `orchestrate` command |

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

Put a project in it and look at it:

```bash
export PYTHONPATH=workspace/src
python3 -m cmws_ws.cli --url http://127.0.0.1:8080 init --from ./my-project
python3 -m cmws_ws.cli --url http://127.0.0.1:8080 log
```

Give a goal to agents (here a local Ollama model; the merged result must pass your test command before
`main` moves):

```bash
python3 -m cmws_ws.cli --url http://127.0.0.1:8080 orchestrate run1 \
    --goal "Add a div(a, b) function to mathx.py that raises ValueError on zero, with tests" \
    --model qwen2.5-coder:7b --parallel 3 --escalate \
    --validate-cmd "python3 -m unittest discover"
python3 -m cmws_ws.cli --url http://127.0.0.1:8080 run-status run1
```

The model splits the goal into tasks, the tasks run in parallel on their own forks, and each finished
branch is merged into `main` only if the merged tree passes `--validate-cmd`. Run the same command again
with the same run id after a crash and it resumes instead of starting over.

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
4. a durable record for a person: `python3 -m cmws_ws.cli --url ... escalations`.

At every step the merged result must pass your validator, and a resolution that silently keeps only one
side, a branch that changed nothing although the agent said "done", and files an agent changed without
reading are all treated as failures to retry, not as success.

### Surviving crashes

* Tool calls carry sequence numbers and run once; a retried call returns the recorded answer.
* Tasks are claimed with an expiring claim; if an orchestrator dies, another one (or the same one,
  restarted) takes its tasks over once the claim runs out, and the replaced owner can no longer land.
* Re-running `orchestrate` with the same run id skips finished tasks and continues the rest.
  `--claim-ttl` sets how long a dead orchestrator's tasks stay claimed (default 300 s).

## Working in a local folder

Any editor or tool can work on a branch through a plain folder:

```bash
python3 -m cmws_ws.cli --url http://127.0.0.1:8080 clone alice ./alice-work
cd alice-work                   # edit files with anything
python3 -m cmws_ws.cli status   # also: pull, push -m "message", submit, resolve <path>, watch
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

`python3 -m cmws_ws.cli [--url URL] [--main REF] COMMAND`, with the URL defaulting to `$CMWS_URL`, then
`http://127.0.0.1:8080`.

| command | what it does |
|---|---|
| `init [--from DIR]` | create the repository, optionally from a folder |
| `log [--limit N]` | history of `main` |
| `clone AGENT DIR`, `status`, `pull`, `push`, `submit`, `resolve`, `watch` | work on a branch in a folder |
| `run-task AGENT "description" --model M \| --exec CMD…` | run one agent on a task in its own branch (`--submit` to land it) |
| `orchestrate RUN_ID --goal G \| --tasks FILE.json` | plan, run in parallel, validate and land; resumable |
| `run-status RUN_ID` | the plan, each task's state and the result of a run |
| `escalations` | work the merge queue could not land and left for a person |

`orchestrate` options: `--model` and `--llm-url` (default `http://127.0.0.1:11434/v1`, or `$CMWS_LLM_URL`),
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

```bash
python3 workspace/eval/run_eval.py --backends oracle --reps 5 --out out.jsonl && python3 workspace/eval/report.py out.jsonl
```

## Build and test

```bash
make                              # compile everything
make test-workspace               # workspace, agents, evaluation and cluster integration tests
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
workspace/src/cmws_ws     versioned workspace, merge, sync, leases, claims, agents, CLI
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
