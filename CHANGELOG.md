# Changelog

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
