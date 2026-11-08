# Changelog

## Unreleased (toward 1.1.0)

The first three steps of the 1.1 update (`docs/design/12-update-consistency-and-efficiency.md`): the strategies of the
evaluation become named isolation levels with a checker, files can declare how concurrent edits combine, and a landing
is checked for lost work without spending a model token.

* **Isolation levels and a serializability checker** (`serial.py`, `MergeQueue(isolation="snapshot" | "gitlike")`): the
  final `main` is compared with the serial replay of the landed tasks in landing order, and with every other order when it
  differs. `gitlike` (text merge + validation, no read set) is the baseline the evaluation compares against; the results
  tables have a `serializable` column.
* **Merge policies** (`policy.py`): `relative` (default), `converge`, `append`, declared per file in `.agenttrunk-merge`
  (read from `main`) or in a task's `"merge"`. `agenttrunk`'s ten-task demo needs 0 syncs instead of 24 with the two
  shared files declared `append` (`simulate.py --append`).
* **Survival check** (`survival.py`): lines either side added or removed on purpose must survive a merge; catches a merger
  that keeps one side, and an agent that drops main's lines while resolving a conflict (MCP `submit_task` answers
  `rejected`).
* **Transition checks** (`checks.py`): a task's `"accept"` list (`contains`, `absent`, `increases`, `decreases`,
  `exists`, `missing`, `command`) is checked against `main` immediately before the landing; strictly validated when the
  task is declared; `agenttrunk board` warns about checks that already pass. The identical-edit counter is refused with no
  model tokens.
* `textmerge.hunks` trims lines the two texts share at both ends before diffing (long files with a small edit: about 40 ms
  for 60,000 lines); `DirFS.write` in the evaluation harness replaces files atomically.

* **Cost ladder** (`agents/ladder.py`): for a stale landing the queue can first ask the agent what the file would be if
  it did its task again on the current main and compare the answer by program (layer 2), then continue the agent's own
  conversation with one message (layer 3), before redoing the whole task (layer 4). Per-task `"layers"`, per-layer
  statistics (`ladder_report`), `orchestrate --reapply --layers`. Off unless configured. A race in which main moved
  again while the model answered, found by the tests, is closed. `CliWorker` and the new `CliModel` read token use from
  `claude -p --output-format json`. With `eco` and `quality`, layer 2 cut total tokens by 55 to 65% on the racing
  suites; layer 3 did not pay off and stays off by default. `OpenAICompatModel(extra=...)` passes extra request fields
  (layer 2 asks with `reasoning_effort: none`).

## 1.0.0

The first complete version: a strongly consistent shared trunk for several AI agents, from the replicated store
up to an MCP server.

### What is in it

* **Replicated key/value store** (Java): at-most-once RPC, primary-backup, multi-Paxos, a sharded store with live
  reconfiguration, compare-and-set and put-if-absent, an HTTP gateway with replica fault injection, and an optional
  write-ahead log.
* **Versioned workspace** (Python, standard library only): content-addressed files, compare-and-set references,
  forks that cost one reference, three-way merge with rename handling, stale-read detection, local checkouts kept in
  sync with a branch.
* **Agents**: a tool-calling worker for any OpenAI-compatible endpoint, a worker for command-line agents, a task
  graph, path leases, a parallel scheduler, a validating merge queue (merger agent, redo, person), exactly-once tool
  calls, checkpoints, fenced task claims and a resumable orchestrator.
* **MCP server** (`agenttrunk mcp`): a task board, claim / sync / submit workflow tools, optional file tools, resources
  for the board and for `main`; the `agenttrunk` command and a ten-task demo (`examples/mcp-demo`), with an optional
  chaos mode that kills replicas and an agent's server process while it runs.
* **Evaluation** (`workspace/eval`, `docs/eval`): four collision suites, four strategies, perfect agents, a local model
  and a mix of a local model and a command-line agent, fault injection on the real cluster, and the figures.

### What the evaluation found

Fork + validating merge queue lost no update and never left `main` broken, where agents sharing one folder lost
3.3 of 5 updates per run. A mixed run (local model plus the `claude` command line) exposed three ways in which work
was lost without anything failing (a merger agent that keeps one side, identical edits from folder-based agents merged
as agreement, a task reported done with no edit); all three are fixed and tested. See `docs/eval/results.md`.

### Known limits

* The merge queue is a library that lives in the calling process; only the compare-and-set on `main` is shared.
* Every landing runs the validation on the whole merged tree, one landing at a time.
* A folder agent's reads are approximated by "every file it changed"; only agents that go through the workspace API
  have a true read set.
* The MCP server speaks stdio only; there is no HTTP transport and no authentication.
* Run ids, board names and branch names are store-wide; use distinct ones for distinct projects.
* The real-model results use one small local model and the `claude` command line, 3 runs per cell.
* Cross-group multi-key transactions of the original course store are out of scope.

The planned next step is described in `docs/design/12-update-consistency-and-efficiency.md`.
