# Update 1.1: consistency and efficiency (planned)

**Status: a plan. Nothing in this document is implemented.** It is an update patch on top of tag `v1.0.0`: it says
what changes, why, how each change will be checked, and in what order. Every number quoted as a measurement was
taken on the 1.0 code and is marked as such.

## 1. Why an update

Version 1.0 is a complete system, but three things came out of using and measuring it that change what the project
should be about.

**1.1 The pain is on the orchestrator, not on the person.** The usual way to run several agents is one main agent
that plans and merges and several sub-agents that work. Then nobody merges by hand; the main agent, an LLM, does.
That is where it hurts: it spends tokens reading every sub-agent's diff, it can resolve a conflict by silently
keeping one side (measured, see 1.3), it cannot tell whether a sub-agent's "done" means anything, and when its
context is compacted it loses the coordination state. The tools should relieve the orchestrator of merging and of
trusting claims, and the saving should be measurable in tokens and rounds.

**1.2 What git already does, and where this system differs.** A branch per agent, three-way merge, a merge queue
that validates the merged result and a compare-and-set on the target branch are all things git hosting offers; they
are not new and the documents must not claim them. What differs, in order of how well it is supported:

| Difference | Support today (1.0 measurements) |
|---|---|
| Read-set validation: a landing is refused if something the agent *read* changed, even when the diffs do not overlap. Git sees which lines were edited, not what the edit was based on | Two agents both turning `COUNT = 0` into `COUNT = 1` are merged by git as one change. With the read set the second is refused. Replaying the tasks serially in landing order reproduces the final `main` in 12 of 12 runs with read-set validation and 0 of 12 without it (5 counter tasks, perfect agents). For appends (the `shared` suite) both give 12 of 12, because appends commute |
| Exactly-once tool calls, fenced task claims, checkpoints, resumable runs | Tested with `kill -9` and killed replicas on the real cluster (24 of 24 runs correct) |
| Forks that cost one reference and need no files on disk | True only for agents that work through the workspace API. In folder mode (what MCP clients use) a fork is materialised: 945 ms for 5000 files against 530 ms for `git worktree add`, so folder mode is *slower* than git. The advantage at scale is **not demonstrated** |
| Self-hosted, no outside service | True, of modest value |
| A replicated coordination store | Not needed to get compare-and-set; it is what makes the coordination service itself fault tolerant |

Two consequences. The read set is only real for API-mode agents; for a folder agent 1.0 approximates it with "every
file it changed" (`assume_read`), which git plus a few lines of script could do too. And the evaluation compared
against a shared folder, which is a weak baseline; a git-like baseline must be a first-class strategy.

**1.3 What the evaluation found.** Work was lost, with nothing failing, in three ways: a merger agent resolving a
conflict by keeping one side, identical edits from folder-based agents merged as agreement, and a task reported done
with no edit. Each was fixed one at a time as a new check. The general lesson (`docs/eval/results.md`): *validated
states in `main` do not mean that every task's effect is in `main`*. Validation checks that the project works, not
that each task's work survived. A systematic answer is needed, not a fourth special case.

### Thesis

> Concurrent agent work is made **reliable by construction** (serializable commits, validated states, exactly-once
> calls, crash recovery). The project **measures what that reliability costs**, and recovers the cost with cheaper
> mechanisms (commutative merging, layered confirmation, incremental retries, speculative validation).

Two pillars, one figure. **Reliability** is how much work is silently lost or ordered wrongly; **efficiency** is what
it costs in tokens, rounds and time as agents are added. They pull against each other: a stricter guarantee means
more refusals, redos and syncs. The figure that carries the update is silent-loss rate against cost, one curve per
isolation level, with the new mechanisms pushing the curve toward the lower left (section 5).

### What we will not claim

* That this replaces git. Merging keeps git's semantics; what is added is on top of them.
* New theory. Optimistic concurrency control, serializability and commutative operations are textbook database
  material. What is new is applying them to LLM agents editing files, with a checkable criterion and measured
  failure modes.
* A general speedup. Adding agents has a serial part (planning, merging, dependencies), conflicts grow with the
  number of agents on shared files, and tokens grow with the number of agents. The claim is a measured frontier.
* That the order of a serial equivalent is the one a person wants (section 2.1).

## 2. Pillar A: reliability through consistency

### 2.1 Isolation levels

The strategies of 1.0 become named levels of one scale, selectable and comparable:

| Level | What it is | Silent losses it permits |
|---|---|---|
| `shared` | all agents in one folder, last writer wins | most |
| `gitlike` | forks, three-way merge, validation of the merged result; **no read set** | derived values written identically (counters, versions) |
| `snapshot` | `gitlike` plus read-set validation (1.0) | work that is a function of what was not read through the API, folder approximation aside |
| `serializable` | `snapshot` plus per-task transition checks (2.4) and the ladder (2.5) | none for the classes checked |

**Serializable agent execution** is defined as: the final `main` equals the result of executing the landed tasks
one after another in landing order, each starting from the result of the previous one. It is a statement about
equivalence to *some* serial order. It does not say the order is the desired one: when task meaning depends on
order, a serial equivalent can still be wrong for the person. That limit goes in the user documentation.

### 2.2 The serializability checker

A tool that, for a recorded run, takes the landing order from the first-parent history of `main`, replays the
tasks' edits serially from the initial files and compares with the final tree. It applies to runs whose agents are
deterministic (the oracle agents of the evaluation). For real agents the same property is approximated by the
acceptance checks of 2.4, because a model's edit cannot be replayed.

* `gitlike` becomes a strategy of the evaluation harness (fork + merge queue with unrecorded reads and no assumed
  reads), replacing the ad-hoc flags used for the one-off comparison in 1.2.
* The checker's verdict is a column of every results table next to lost updates.
* Acceptance: on the `counter` suite the checker reports 12 of 12 for `snapshot` and 0 of 12 for `gitlike`; on
  `shared` both 12 of 12 (they must agree, appends commute); the checker is itself tested on hand-made histories,
  including one that is serializable only in an order other than the landing order.

### 2.3 Merge policies per file

Some edits are agreement when they are identical (a semicolon, a rename), some are not (a counter, a version
number), some commute (appended lines). Text alone cannot tell them apart, so the policy is declared, and the safe
one is the default.

| Policy | Meaning | Mechanism |
|---|---|---|
| `relative` (default) | the edit depends on what the agent read | read-set validation and the transition check; identical edits are *not* taken as agreement |
| `converge` | identical edits are agreement | no read-set validation for this file; identical changes merge once |
| `append` | pure insertions at the same place commute | both sides' inserted lines are kept (union), ordered by landing order |

Declared in the task (`"merge": {"stats.py": "relative"}`) or for paths in a `.agenttrunk-merge` file at the project
root (`registry.py append`, `*.md converge`). A policy only ever *relaxes* a check the default would have made, and
it applies only to the files named; an undeclared file keeps `relative`. `append` applies to a conflict in which
both sides only inserted lines and neither modified or deleted a base line; anything else stays a conflict.
Order-sensitive appends (migration numbers, ids handed out in sequence) are not safe under `append` and must be
left at `relative`; validation catches what it can, and this is documented.

* Expected effect: the demo's 24 syncs for 10 tasks (nearly all on appends to `registry.py` and `CHANGELOG.md`)
  fall to about zero with the two files declared `append`, with no tokens spent and no loss. This is the number to
  confirm, not assume.
* Acceptance: property tests over random edit sequences (insert, modify, delete, move) check that a file with no
  declaration merges exactly as in 1.0; that on a file declared `append` every inserted line is kept exactly once, in
  landing order; and that on a file declared `converge` only textually identical edits are merged as one. The counter
  suite with its file declared `append` is *not* accepted, because its edits modify a line and `append` does not
  apply to that. The mutation sweep covers the new code.

### 2.4 Acceptance checks: free and nearly free evidence

Cheapest first, all with zero model tokens.

**Survival check (automatic).** For each landing: every line the agent added is present in the merged result; every
line it removed is absent. It needs no authoring and catches a merger or a merge that dropped one side. It cannot
catch the counter case (the added line `COUNT = 1` is present), which is why the next check exists.

**Transition checks (task-supplied).** A check over the pair *(main before this landing, main after)*:

| Kind | Example |
|---|---|
| `increases` | `stats.py` matches `COUNT = (\d+)` and the number is the old number plus 1 |
| `contains` / `absent` | the line is present after, absent before |
| `command` | a command passes on the merged tree and, optionally, a command's output changed in a stated way |

The reference point is **`main` immediately before the landing, not the agent's fork base**; against the fork base
the check would fail whenever anyone else landed first. State checks ("this line ends with a semicolon") are the
special case needing no old value. For the identical-edit counter, old `COUNT` is 1 and the candidate is 1, so
`1 == 1 + 1` fails and the landing is refused at once. Checks are written by the planner or the person; templates
(above) lower the chance of a wrong one, and a check that cannot fail is reported (a lint run against the unmodified
tree). Where no check exists, the ladder (2.5) decides.

Format in the task JSON: `"accept": [{"kind": "increases", "path": "stats.py", "pattern": "COUNT = (\\d+)", "by": 1}]`.
The queue runs a task's checks in a temporary folder holding `old/` and `new/` trees. A task with no checks
behaves as in 1.0 plus the survival check.

Acceptance: the counter suite is refused by the transition check with zero model tokens; a merger that keeps one
side is refused by the survival check; both without being told which suite it is.

### 2.5 The cost ladder

When the identical-edit question remains (no check, a changed file, other writers), the system goes up by cost and
never accepts without evidence:

| Layer | Cost | What it does |
|---|---|---|
| 0 | none | survival check |
| 1 | none | the task's transition checks; if they pass the task is complete, no model is asked |
| 2 | tens of tokens | **re-apply and compare**: tell the agent its task, what it changed on the old copy, and the line now on `main`; ask what the line would be if it carried out its task again on the current `main`; the *program* compares the answer with the current line. Equal means agreement, different means apply its answer |
| 3 | one short exchange | continue the agent's own conversation with one message (the upstream diff of the files it touched), instead of starting it again |
| 4 | the whole task | redo from the new `main` (what 1.0 does at every conflict) |
| 5 | a person | escalation |

Layer 2 deliberately does not ask "is your goal met? yes/no". A model asked that about a counter will tend to look at
`COUNT = 1`, see the number its change produced, and say yes; its target was "plus one", not "one". Asking for the
*result of re-applying* and comparing it removes the dependence on the model judging itself. This is a hypothesis
with an experiment attached: `workspace/eval/confirm_probe.py` compares the two ways of asking on four cases (a
semicolon and a rename, where agreement is right; a counter and a version bump, where it is wrong), 8 samples each, on
the local models. It has been run (8 samples per cell, temperature 0.7, tokens are prompt plus completion):

| case (is agreement right?) | eco 9B: ask yes/no | eco 9B: re-apply and compare | quality 27B: ask yes/no | quality 27B: re-apply and compare |
|---|---|---|---|---|
| counter (wrong) | 1/8 correct, ~2300 tok | **8/8**, ~375 tok | 0/8, ~390 tok | **8/8**, ~160 tok |
| version bump (wrong) | 0/8, ~1200 tok | **8/8**, ~420 tok | 0/8, ~270 tok | **8/8**, ~180 tok |
| semicolon (right) | 8/8, ~470 tok | 8/8, ~625 tok | 8/8, ~290 tok | 8/8, ~260 tok |
| rename (right) | 6/8, ~1860 tok | 8/8, ~1110 tok | 8/8, ~275 tok | 8/8, ~260 tok |

The hypothesis holds on these four cases, for both models. Asked "is your goal met?", the models said yes to the
counter and the version bump almost every time (1 of 16 and 0 of 16 correct), exactly the silent loss the layer exists
to prevent; asked what the line would be after re-applying the task, and compared by the program, all 64 samples were
right, with fewer tokens on the cases where agreement is wrong. So the yes/no question must not be used, and
re-apply-and-compare is a sound basis for layer 2. This is four hand-made cases on two models, not a measurement of
real code, so **layer 2 stays off by default** until U4 measures it on the replay set; what the probe changes is that
U4 is now worth building. The default for an uncertain case is "go up a layer", never "accept".

Layer 3 is mostly free where it already exists: in the MCP flow `sync_with_main` *is* an incremental continuation, the
agent proceeds in its own context. The saving is in the orchestrated path, where the system redoes work for the
agent: `LoopWorker` already records the whole conversation (`Session`), so a resumed call with one added message
replaces a restart. For a folder agent run once by `CliWorker` there is no conversation to resume and layer 3 does
not apply.

Acceptance: per resolution, tokens by layer against the 1.0 full redo; the share of cases settled at each layer; and
no rise in silent loss (the serializability and lost-update columns do not move).

### 2.6 History checking under faults (stretch)

A Jepsen-style harness: while the chaos mode of the demo kills replicas and agents, record every claim, landing and
completion with its time, then check the history: `main` linear, no task landed twice, no claim held by two owners at
once, every landed task completed or released. Cheap to add to `simulate.py --chaos`; it would turn "it worked in 24
runs" into "the recorded history satisfied these invariants in N runs". Not required for 1.1.

## 3. Pillar B: scalability and efficiency

### 3.1 What scaling means, and what limits it

Three different things are called scaling; only the first is a goal of this update.

| Direction | Question | Status |
|---|---|---|
| more agents at once | speedup, efficiency (speedup / N), token overhead | with simulated latency, 8 agents: 6.0x, coordination overhead about 5%; no real-model measurement possible on the available hardware |
| bigger projects | cost of fork, folder materialisation, validation | measured: fork 0.01 ms (no files), folder 945 ms for 5000 files, a commit of one file 0.1 ms (not comparable to `git commit`, which includes starting a process) |
| more machines | fault tolerance and throughput of the coordination service | tolerance tested; throughput not measured |

What limits more agents, honestly:

1. `main` is one key, and every landing is a compare-and-set on it. Sharding does not help a single hot key.
2. Every landing runs the validation on the whole merged tree, and landings are serialised, so N tasks cost at least
   N validations in a row.
3. Under contention, retries grow with N. With five tasks racing for one line, redo budgets of 0, 1, 3 and 6 landed
   all five in 0, 0, 40 and 100 percent of runs: the budget needed is close to the number of racers.
4. Each validation reads every file of the candidate tree.
5. Amdahl: planning, merging and dependency chains are serial.

### 3.2 Mechanisms

| Mechanism | Targets | Idea |
|---|---|---|
| Width cap | tokens | `TaskGraph` can compute the largest set of tasks that can run at once; never start more agents than that |
| `append` / `converge` merging (2.3) | rounds, tokens | do not make agents sync over lines that commute |
| Layered confirmation, incremental retry (2.5) | tokens per conflict | replace the full redo |
| Straggler hedging | tail latency | when a task runs past k times the median of its peers, start one backup agent; the first to pass verification wins and the other is released. Backups only for stragglers, so tokens rise only where time is lost |
| Shared context broadcast | tokens | the project map and conventions are computed once, stored in the store, handed to every agent instead of each agent reading the same files. Measure how much it saves before claiming anything |
| Speculative, batched merge queue | landing throughput | validate candidate k+1 on top of candidate k while k is still being validated; land a batch when the last is green; bisect on failure. This is how large merge queues keep throughput up |
| Impact-limited validation | validation time | run only the checks that cover the changed files, with the full run as a periodic gate. Optional, because it trades safety |
| Sparse folders | folder cost | materialise only declared paths plus what the agent reads, instead of all files |

### 3.3 Agents as parallel processes

The aim ("weak models, really in parallel, like CPU processes, mindful of tokens") separates into kinds of
parallelism, only some of which suit weak models:

| Kind | Fits weak models? | Notes |
|---|---|---|
| data parallel: the same operation on many files (types, tests, an API migration) | very much | subtasks are small and independent; the best case for near-linear speedup |
| speculative: K agents on the same task, first verified result wins | yes, when a cheap verifier (tests) exists | buys success rate and tail latency with K times the tokens; hedging (3.2) is its economical form |
| pipeline: write, test, review overlapped | moderately | dependencies |

A simple cost model, to be fitted, not assumed: time(N) = plan + work/N x (1 + conflict(N)) + merge, and tokens(N) =
plan + N x shared_context + work_tokens x (1 + redo(N)). The result to report is the **time against tokens
frontier and its knee**, not "more is faster". The weak spot is decomposition quality: a model that splits a goal
badly wastes every agent after it, so planning is measured as its own serial part.

**Hardware.** The available local server serves one request at a time, so real parallel speedup cannot be shown
on it. Options, in the order to try: let the server run several slots (`OLLAMA_NUM_PARALLEL=4` with a small context
such as 8192 so the per-slot memory fits; one GPU can batch several sequences for a large total throughput gain);
small samples with the command-line agent; and scaling curves with simulated latency, labelled as simulation.

### 3.4 An interface for the orchestrating agent

The MCP server grows tools for the main agent, so that it never reads a diff:

| Tool | Purpose |
|---|---|
| `add_tasks` | put a list of tasks (with paths, dependencies, merge policies, acceptance checks) on a board |
| `wait_for_tasks` | block, with a timeout, until named tasks are done, failed or need a decision |
| `board_digest` | a compact verified summary: landed with their checks passed, failed with the reason in a line, those needing a decision with the minimal context (the conflicting hunk and both intents), token counts |
| `explain_escalation` | the full record of one escalated item, on request |

Measured against merging in the orchestrator's own context: orchestrator tokens and rounds, with silent losses
counted for both.

## 4. Evaluation plan

**Strategies:** `serial`, `shared`, `gitlike`, `snapshot` (1.0's fork + merge queue), then `snapshot` with each
mechanism turned on (policies, checks, ladder, hedging, speculative queue). The comparison against `gitlike` is the
one that answers "why not git".

**New suites**, each a small project with an exact oracle, so every outcome is decided by a program:

| Suite | What it contains |
|---|---|
| `hotfiles` | the shared files every change touches: a registry, a changelog, an export list, route table |
| `derived` | values computed from other values: counters, version bumps, sequence numbers |
| `convergent` | edits that are the same on purpose: a formatting rule, a rename |
| `fanout` | many independent files, one operation on each (data parallel) |

The four suites of 1.0 stay for continuity.

**Metrics:** lost updates; serializability violations; `main` broken; person escalations; worker tokens and
orchestrator tokens; rounds (syncs and redos per task); wall time; speedup and efficiency against N in {1, 2, 4, 8, 16}.

**The figure:** for each isolation level and each mechanism, silent-loss rate against cost (tokens, and separately
rounds). Plus the scaling curves (speedup and token ratio against N) for `fanout`, `hotfiles` and a dependency chain.

**Replay of real history (no model, no cost).** Take an open-source repository's history and find pairs of pull
requests that were open at the same time and touched the same file. Classify each pair: textual conflict in git;
same file, no overlap; flagged by read-set / changed-file validation but not by git; the merged result fails the
repository's tests. This answers what the controlled suites cannot: how often the cases that matter occur in real
code. It is also the evidence most likely to settle "is git enough".

**Real models:** the local models (the evaluation server is serial, so these runs show correctness and tokens, not
speedup) and the `claude` command line in small samples; every table says how many runs and which model.

## 5. Compatibility and rollout

* No breaking change. Defaults stay conservative (`relative`; checks run only where declared; layer 2 off). A
  project that declares nothing behaves as in 1.0 plus the survival check.
* New configuration is optional: `.agenttrunk-merge`, `"merge"` and `"accept"` in tasks, `--confirm` for layer 2.
* The Python package stays `cmws_ws`; the command stays `agenttrunk`; the version becomes 1.1.0.
* Considered, **not** part of this update, each a separate decision: exporting `main` as a real git repository with
  history (and accepting one as a fork source); a zero-setup single-machine store on SQLite so the Java cluster
  becomes an optional high-availability mode; an HTTP transport and authentication for MCP; the merge queue as a
  service of its own.

## 6. Work breakdown and acceptance

| # | Work | Done when |
|---|---|---|
| U1 | `gitlike` strategy; serializability checker; both in the harness and the tables | the 12/12 against 0/12 result reproduces; the checker passes its hand-made histories; `docs/eval` shows the new column |
| U2 | merge policies (`relative`, `converge`, `append`) in the merge engine, tasks and the MCP server | the demo's syncs fall from 24 to about zero with the two shared files declared `append` and nothing lost; property tests and the mutation sweep pass |
| U3 | survival check and transition checks | the counter case is refused with no tokens; a merger keeping one side is refused; a check that cannot fail is reported |
| U4 | the ladder: re-apply and compare (after the probe), incremental continuation | tokens per resolution against full redo are measured by layer; silent-loss columns do not move |
| U5 | width cap, hedging, shared context, speculative queue | scaling curves and token ratios for the three workloads, simulated and, where possible, real |
| U6 | orchestrator tools and the digest | orchestrator tokens against merging in its own context, on the same tasks |
| U7 | replay study of real history; the cost-against-loss figure; the report | the figure, the replay classification table and the updated `docs/eval/results.md` |

U1 comes first because it needs no model and its output is the vertical axis of every later figure. U2 and U3 follow
for the same reason. U4 waits for the probe. U5 depends on what hardware allows.

## 7. Risks and open questions

* **A weak model writing acceptance checks.** The checks can be wrong or too weak. Templates, a "cannot fail"
  detector, and keeping the survival check automatic mitigate it; they do not remove it.
* **Policy misdeclared.** `append` on an order-sensitive file loses meaning silently. The default stays `relative`, and
  validation remains the net.
* **False refusals.** The folder approximation (every changed file counts as read) refuses two edits to different
  parts of one file. `append` and block-level preconditions reduce it; how much is a measurement for U2.
* **Layer 2 on real code.** The probe supports it on four constructed cases with two local models (above); whether it
  holds on the messier changes of real code is what U4's measurement is for, and it stays off until then.
* **Parallel speedup on the available hardware.** May only be demonstrable by simulation; the report must say so.
* **Decomposition quality** of weak planners bounds everything after it.
* **Whether the cases occur in real code** is the question the replay study answers, and the answer may be "rarely".
  That would be a result worth reporting: it would say where git is enough.

## 8. Measurements already taken (on 1.0)

| What | Result |
|---|---|
| fork without files, 5000-file project | 0.01 ms |
| folder materialisation of the same, `git worktree add` | 945 ms, 530 ms |
| final `main` equals serial replay in landing order, counter suite | 12/12 with read set, 0/12 without |
| the same, shared suite (appends) | 12/12 and 12/12 |
| work lost per run, `counter`, perfect agents | shared folder 3.3 of 5; fork + merge queue 0; without read tracking 2.8 |
| a local model plus `claude`, three fixes in turn | lost per run 1.7 and 1.0, then 1.7 and 0.3, then 0 and 0 (counter, shared) |
| fault injection (killed replicas, killed orchestrator, both), 24 runs | 24 of 24 correct |
| three real Claude Code sessions, ten-task board through MCP | 10 of 10 done, 7 collisions all resolved with both sides kept, 43 tests passing (one run) |
| demo syncs for ten tasks with four scripted agents | 24 |
