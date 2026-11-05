# MCP demo: ten tasks, several agents, one shared project

A small shopping library (`project/`) with **ten tasks** on a board. Each task is one module to write
(`prices`, `tax`, `cart`, `inventory`, `shipping`, `coupons`, `receipt`, `search`, `validators`, `slugs`) plus
its tests, and every task must also add one line to two **shared files**, `registry.py` and `CHANGELOG.md`.
That is what makes the agents collide: ten agents appending to the end of the same two files.

Each task touches 4 files (module, tests, registry, changelog); the board as a whole changes 22.

The project must end up with all ten modules written, all tests passing, and exactly one line per feature in
each shared file. `check.py` verifies that.

## 1. Try it without any model (2 seconds)

```bash
make run-gateway ARGS="--port 8080 --groups 2 --replicas 3"        # in one terminal, from the repository root
python3 examples/mcp-demo/simulate.py --agents 4                    # in another
```

Four scripted agents, each a real `agenttrunk mcp` process spoken to over MCP, finish the board at the same
time. You see who claimed what, who had to sync because the shared files moved, and the final check.

Add `--chaos` and the run is attacked while it goes on: one replica of each shard group of the store is killed
(and revived later), and the first agent kills its own server process right after writing its files and starts a
new one under the same name. The new process finds the task, the claim and the edits where they were, and the
result is exactly as correct as without chaos.

```bash
python3 examples/mcp-demo/simulate.py --agents 4 --chaos
```

## 2. Try it with real agents (Claude Code, Cursor, ...)

Set up the project, the board, and one folder per agent (alice, bob, carol):

```bash
export AGENTTRUNK_URL=http://127.0.0.1:8080
examples/mcp-demo/setup.sh demo
```

(`setup.sh NAME` makes a fresh project and board called NAME every time, so you can repeat the experiment
with `demo2`, `demo3`, ...)

It prints the commands to start each agent. In three terminals:

```bash
cd examples/mcp-demo/agents/demo/alice && claude
cd examples/mcp-demo/agents/demo/bob   && claude
cd examples/mcp-demo/agents/demo/carol && claude
```

Each folder holds a `.mcp.json` that connects that session to the board as that agent. Claude Code asks you
to approve the project's MCP server the first time; approve it. In another client, put the same server
definition in its MCP configuration (the `command` and `args` are in the file).

Give each session this instruction:

> You are one of several agents sharing a project through the agenttrunk tools. Call show_board, then
> claim_task, do exactly what the task says in the folder it gives you, then call submit_task. If
> submit_task says main moved, call sync_with_main, read what it reports, fix what is needed and submit
> again. Keep going until claim_task says nothing is ready.

In a fourth terminal, watch the board fill in, and when it is done check the result:

```bash
export AGENTTRUNK_URL=http://127.0.0.1:8080
bin/agenttrunk --main demo board demo                       # who holds what, what is done
bin/agenttrunk --main demo log                              # the history of the shared main
bin/agenttrunk --main demo export /tmp/demo-result && python3 examples/mcp-demo/check.py /tmp/demo-result
```

## What to look for

* **The collision.** Every task appends to `registry.py` and `CHANGELOG.md`. When another agent lands first,
  `submit_task` answers `stale`; `sync_with_main` merges main into your folder and shows what main changed in
  the files you also changed; two appended lines at the end of a file conflict, so the files come back with
  conflict markers to resolve. A good agent keeps both lines. A careless one drops the other agent's line;
  `check.py` then reports which feature is missing from the shared files.
* **A failing change never reaches main.** Ask one agent to break a test on purpose before submitting.
  `submit_task` answers `rejected` with the failing output; main does not move.
* **Killing an agent.** Close one agent's terminal while it holds a task, then start `claude` again in the
  same folder. The agent name is the same, so `claim_task` hands you your task and folder back ("you already hold
  this task"), your files are still there, and `submit_task` lands them. Another agent can take the task over only after its claim has been
  silent for `--claim-ttl` seconds (default 900).
* **Giving a task back.** `release_task`, then another agent claims it.
* **Honest paths.** `tasks.json` declares only each task's own two files, so tasks run at the same time and
  collide on the shared files. With `setup.sh demo2 examples/mcp-demo/tasks-honest.json` every task also
  declares `registry.py` and `CHANGELOG.md`: the board then shows them as `waiting`, agents take turns and
  nothing collides. Slower, but no syncing.
* **A client without a file system.** Add `"--file-tools"` to the `args` in `.mcp.json` (and drop `--workdir`):
  the agent then edits through `list_files`, `read_file`, `write_file` and `replace_in_file` instead of a
  folder.

## With a local model instead of Claude Code

```bash
python3 examples/mcp-demo/llm_agent.py --name alice --board demo --main demo --model qwen2.5-coder:7b \
    --llm-url http://127.0.0.1:11434/v1 --workdir ./work --log alice.jsonl
```

Run several (different `--name`) at once; `alice.jsonl` records every tool call. To watch a `rejected`, pass
`--validate-cmd "python3 examples/mcp-demo/strict_check.py"`: the check requires type annotations, which the task text
does not mention.

## Files

| file | what it is |
|---|---|
| `project/` | the starting project: ten stub modules, the two shared files, one smoke test |
| `tasks.json` | the board (collide on the shared files) |
| `tasks-honest.json` | the same board with honest paths (agents take turns) |
| `setup.sh` | project + board + one MCP configuration per agent |
| `simulate.py` | scripted agents over real MCP processes, for trying it without a model |
| `check.py` | verifies a finished result |
| `llm_agent.py` | an MCP client driven by any OpenAI-compatible model (a local Ollama), to try the flow without Claude Code |
| `strict_check.py` | a validation command with a rule the task text never mentions (type annotations), to watch an agent handle `rejected` |
| `solutions.py` | the reference solutions behind `simulate.py` (the real agents never see it) |
