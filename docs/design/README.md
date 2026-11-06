# Design documents

Read them in order; each builds on the one before.

| # | Document | What it covers |
|---|---|---|
| 01 | [Exactly-once RPC](01-exactly-once-rpc.md) | at-most-once command execution over an unreliable network |
| 02 | [Primary-backup](02-primary-backup.md) | single-failure tolerance with a view service |
| 03 | [Paxos](03-paxos.md) | the replicated, linearizable log; protocol logic as a pure state machine |
| 04 | [Sharded store](04-sharded-store.md) | replica groups, shard migration, live reconfiguration |
| 05 | [Workspace engine](05-workspace-engine.md) | content-addressed objects, compare-and-set references, forks, three-way merge, stale-read detection |
| 06 | [Local sync](06-local-sync.md) | a real folder kept in sync with a branch: pull, push, watch, conflict markers |
| 07 | [Workers](07-workers.md) | a tool-calling agent loop and command-line agents working on their own branch |
| 08 | [Scheduling and leases](08-scheduling-and-leases.md) | task graph, path leases, the parallel scheduler, the planner |
| 09 | [Merge queue](09-merge-queue.md) | validating the merged result; merger agent, redo, person |
| 10 | [Reliability and the orchestrator](10-reliability-and-orchestrator.md) | exactly-once tool calls, checkpoints, fenced claims, resumable runs |
| 11 | [MCP server](11-mcp-server.md) | the task board and workflow tools for agents in any MCP client |
| 12 | [Update: consistency and efficiency](12-update-consistency-and-efficiency.md) | the 1.1 update: U1 to U3 (isolation levels, merge policies, survival and transition checks) are implemented, U4 to U7 are planned |

The evaluation is in [../eval/results.md](../eval/results.md).
