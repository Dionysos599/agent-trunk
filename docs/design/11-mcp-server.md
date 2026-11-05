# The MCP server

`agenttrunk mcp` lets agents in any MCP client (Claude Code, Cursor, a chat client with MCP support) share one
project through the workspace, merge queue and claims described in the earlier documents.

## Why MCP, and why workflow tools rather than file tools

Three ways to offer the workspace to an agent were compared: one integrated command (what `orchestrate` is),
a command-line tool the agent calls through a shell, and an MCP server. Throughput is decided by the
compare-and-set on `main` and the validation in the merge queue, not by the interface, so the choice is about
who can use it and how reliably.

* A coding agent such as Claude Code or Cursor already has strong tools for reading and editing files. Offering
  `read_file` / `write_file` over MCP would duplicate them, worse. What it lacks is *the workflow*: which task
  is mine, is my branch behind, can I land, what do I do when I cannot. So the primary tools are workflow tools
  and the branch is handed over as a **folder** the agent edits with its own tools.
* A client with no file system (a chat interface) gets the file tools with `--file-tools`, as an option.
* The command line stays (it is the same operations for scripts), the integrated command stays (for "give a
  goal, wait for the result"). All of them call one core.

## Tools

| tool | meaning |
|---|---|
| `show_board` | tasks with a state: open, claimed (by whom, expiring in), waiting (for which tasks), done, failed, blocked |
| `claim_task` | next ready task, a named one, or a new one the agent adds (`description`, `paths`); returns the task, the branch and the folder |
| `task_status` | changed files, uncommitted edits, whether `main` moved, files changed on both sides, files with conflict markers |
| `sync_with_main` | merge `main` into the branch and the folder |
| `submit_task` | validated landing, completes the claim |
| `release_task` | give the task back (the branch is kept) |
| `list_escalations` | what the system left for a person |

Every result carries `ok` and a `next` sentence. A failed call is an MCP error result (`isError`) with the
reason in text, so the model reads it and acts; nothing is raised into the protocol except malformed requests.
Resources: `agenttrunk://board`, `agenttrunk://main`, and the template `agenttrunk://main/{path}`.

## State lives in the store, not in the process

The server object holds nothing that a restart cannot rebuild. The claim, the lease, the branch, the folder
bookkeeping and the record of which task an agent holds (`runs/<board>/active/<agent>`) are all in the
replicated store or on disk. Consequences:

* any number of server processes can run, on any machine that can reach the gateway;
* restarting a server (or the client) with the same `--agent` name resumes the task: `claim_task` answers "you
  already hold task X", the folder still has the edits, `submit_task` lands them;
* a different process cannot take the task while the claim is live; after `--claim-ttl` seconds of silence it
  can, and the old owner's `submit_task` is then refused (the epoch no longer matches).

The one exception is memory mode (`--file-tools`): the edits live in a workspace overlay in the server process,
so they are checkpointed (commit plus a record, every 5 calls and at sync and submit) and a restart goes back
to the last checkpoint. Calls carry optional `seq` numbers; a repeated number returns the earlier result.

## A board is an orchestrator run

Tasks are stored under `runs/<board>/plan` and claimed in `runs/<board>/claims` exactly like an orchestrator
run's, with the same `BOARD-TASK` naming, so one board can be worked by `agenttrunk orchestrate`, by MCP agents,
or both. Tasks an agent adds itself go to `runs/<board>/adhoc`. Declared paths decide order: a task whose paths
overlap an earlier open or running task waits for it; a task with no paths touches the whole project and
waits for everything.

## Landing and the lessons of the evaluation

`submit_task` does what the merge queue does for an orchestrated task, except that the *agent* plays the part of
the merger and the redo:

1. the work is saved to the branch; files with conflict markers refuse the submit;
2. every file the agent changed is recorded as a read at the fork base (`assume_read`). An agent working in a
   folder never says what it read, and two identical edits would otherwise merge as agreement and lose an
   increment (evaluation, gap 2). So when `main` changed a file the agent also changed, the submit answers
   `stale`;
3. one validated attempt (`MergeQueue.attempt`): merge with `main`, run the validation command on the result,
   compare-and-set. The claim is checked again inside the validation step, so a replaced owner cannot land;
4. `landed` completes the claim, releases the lease and refreshes the folder. Otherwise the answer is `stale`,
   `conflict`, `rejected` or `contended`, each with what to do.

`sync_with_main` is how the agent recovers. A clean merge just happens; conflicts are written as markers; and
because a clean text merge can still leave a change that main already contains (the two identical edits above),
it lists the files both sides changed together with what `main` changed in them (up to 6000 characters), and
tells the agent to check that its own change still does what the task asks. `submit_task` after a sync lands
if nothing moved again. An empty branch is never submitted (`nothing-to-land`), because "done" without a change
is not done.

## Protocol

JSON-RPC 2.0 over stdio, one message per line, implemented in about 200 lines without a dependency
(`mcp/protocol.py`). Supported: `initialize` (protocol versions 2025-06-18, 2025-03-26 and 2024-11-05; the
client's version is echoed if known), `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`,
`resources/templates/list`, and notifications (ignored). Arguments are checked against the tool's schema
(required, types, enum, no unknown keys) and a bad call comes back as an error result the model can read. Batches
are answered as batches. A parse error is answered with `-32700` and the loop carries on; a bug in a tool is
logged to stderr and returned as an error result, never raised.

## How this was tested

* Protocol: negotiation for every version, notifications versus requests (including id 0), every error code,
  schema checking case by case, tool failures and crashes, resources, the serve loop on a byte stream with
  blank lines, bad JSON, batches, non-ASCII text and a 200,000-deep nested input.
* Service: every board state, ordering by declared paths, naming and limits, ad-hoc tasks (a refused task is
  not stored), eight agents claiming eight tasks at once (each gets a different one), landing, validation
  failure, the same edit by two agents (stale, then sync shows what main changed, then the agent's own
  increment lands), conflict markers (submit and sync refuse until resolved), release, restart of the server,
  takeover after expiry, the claim checked at the last moment, memory mode with seq replay and checkpoint
  recovery.
* Agents at once: six agents (threads, each its own service object as separate servers would be) all adding one
  to the same counter, in four rounds: the counter ended at 6 every time, with collisions in every round.
* Real processes on the real cluster: four `agenttrunk mcp` processes speak JSON-RPC over pipes; a replica of
  each shard group is killed meanwhile; one agent process is killed with its edit unsubmitted, restarted under
  the same name, finds its task and lands; the final counter is 4 and every task is done.

## What the demo found

Running the ten-task demo with several simulations at once on one store showed that agents sometimes stalled
for minutes, with tasks `open` and nobody able to claim them. The cause: path leases were kept in one table
for the whole store, so a lease left behind by an agent that had been killed in *another project* (a different
`main`) blocked the same file names here until it expired. Leases are now kept per project (per `main` ref;
the default `main` keeps its old key), so projects sharing a store do not block each other, while boards and
runs on the same project still do, which is the point of leases. Run ids, board names and branch names
(`ws/<id>`) are still store-wide: use distinct ones for distinct projects.

## Limits

* Transport is stdio only. Agents on another machine can run their own `agenttrunk mcp` pointed at the same
  gateway (the state is in the store), but there is no HTTP transport and no authentication; do not expose the
  gateway to a network you do not trust.
* A folder agent is told to check what `main` changed; it cannot be forced to. Its work is validated by your
  command, not by the task's intent. A task-level acceptance check is the general answer.
* One task at a time per agent name.
* The task board is filled by a person (a file or `agenttrunk board`) or by the agents themselves; there is no
  planner behind MCP (use `orchestrate --goal` for that).
