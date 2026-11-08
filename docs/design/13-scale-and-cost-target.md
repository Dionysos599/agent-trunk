# Target: correct at 32 agents, fast, and cheap in tokens

**Status: a target and a measurement design. Nothing here is measured yet.** It fixes in advance what will be claimed, how
each number will be produced, and what has to be built to be able to produce it, so that no number is chosen after the fact.

The two claims:

1. **Reliability.** With 32 concurrent agents making about 1,600 line edits in a run, **Z of Z runs end correct**, 20 runs
   without faults and 20 with faults (killed agents, killed and revived storage replicas).
2. **Efficiency.** **N times faster than one agent doing the same work, with only X% more tokens.**

## 1. The workload

One generated project and one task list per run (seeded, so a run can be repeated). About 1,600 line edits in 160 tasks of
about 10 edits each, over 32 agents. Every edit is a deterministic function of the file it edits, so the right answer is
known: the serial replay of the landed tasks (`serial.py`).

| kind of edit | share | what collides | what it tests |
|---|---|---|---|
| independent files | 55% | nothing | the cost of the machinery when there is nothing to protect |
| append to shared files (registry, changelog) | 25% | adjacent inserts | merge policy `append` |
| hot counters and version lines | 12% | the same line changed by many agents | lost updates; transition checks, ladder |
| cross-file rename plus new callers | 8% | text merges, program breaks | the validator and redo |

Every edit carries a unique token, so "edit present exactly once" is checkable without a model.

## 2. Reliability: what "correct" means and what is injected

A run is **correct** only if all of these hold:

* the final tree equals the serial replay of the landed tasks in some order (`check_serializable`);
* every one of the ~1,600 edits is present exactly once, and no task landed twice;
* every state `main` ever held passed the validator, checked **after the run** by replaying the validator over every landing
  commit in `main`'s history (the queue's in-memory `approved` list would not survive a killed process), and the landing
  commits form one chain by ancestry (a branch that synced and then fast-forwarded is not on the first-parent line, so
  "first-parent history" is not the test);
* no claim was held by two owners at once, and every landed task was completed or released.

Faults (the second set of 20 runs): at random moments one replica of each shard group is killed and revived, 4 of the 32
agent processes are killed with `SIGKILL` mid-task and restarted under the same name, and an orchestrating process is killed once and resumed under the same run id (the queue is a library inside each process,
not a service of its own).
The headline is Z/Z in each set; a failing run is reported, never dropped. Z/Z with 20 runs is a statement with a bound,
not a proof: 20 of 20 correct only supports "failure rate below about 15% with 95% confidence" (the rule of three), and the
report says that next to the number. Baselines in the same table: shared folder, a
git-like level (text merge + CI), the 1.0 level, and the full system; for each, correct runs out of 20 and edits lost.

Agents are **scripted** (the deterministic oracles of the evaluation) running as separate processes against the real
replicated store. A language model cannot be run 32 wide on the available hardware; the claim must say "scripted agents".

## 3. Efficiency: speed and tokens, measured so both are honest

**Speed** = time for one agent to do all 160 tasks one after another, divided by time for 32 agents. **Token increase** =
(tokens the 32-agent run spends − tokens one agent spends) / tokens one agent spends; the extra tokens are the work done
again after a collision (re-applied, continued or redone). The one-agent baseline is the `serial` strategy: the same worker,
a fresh context for every task. A single long conversation through all 160 tasks would be a different (and for a real model
usually more expensive) baseline, and the report says which one is used.

Neither number can come from 32 real models here, so they are **trace-driven**:

1. *Record* from real runs (first Claude through `claude -p --output-format json`, later `eco` and `quality`): the seconds
   and tokens of a task of each kind, and the tokens of resolving one collision at each ladder layer (re-apply, continue,
   redo), with the number of samples.
2. *Replay* in the 32-agent run: each scripted agent waits the recorded time (drawn from the recorded distribution) and
   "spends" the recorded tokens. Everything else is real: the store, the queue, validation, merging, the ladder's decisions.
3. *Check* the model at small N: 4 to 8 real agents, real time and real tokens, must land within a stated margin of what
   the replay predicts for the same N. If it does not, the 32-wide number is not published.

Every figure is labelled with its source: replay of Claude traces, or real. Ablations in the same table: ladder off (redo
only), ladder on; merge policies off, on; batched validation off, on.

### Why the target for speed is what it is

The queue validates one landing at a time. With task time `t`, validation time `v`, `T` tasks and `N` agents, one agent takes
about `T(t+v)`, `N` agents about `T·t/N + T·v`. For `t = 2 s`, `v = 0.3 s`, `T = 160`: one agent 368 s, 32 agents 10 s of work
plus 48 s of serial validation: **about 6.3 times at `N = 32`, and never more than `(t+v)/v`, about 7.7 times, however
many agents there are.** To go further the validation has to stop being
serial: validate a batch of non-overlapping candidates in one run, and speculatively validate candidate `k+1` on top of `k`
while `k` runs (Zuul and GitHub's merge queue do this). That is what the efficiency mechanisms (U5) are for, and it is the
only way the speed claim moves. Batching must not weaken the guarantee of document 9: `main` only ever holds a state that
was validated *as it stands*. A batch of non-overlapping candidates is merged and validated as one candidate state, and a
failing batch is split until the culprit is alone; speculative validation of `k+1` on top of `k` is discarded and redone if
`k` fails or `main` moves differently. The correctness checks of section 2 run unchanged on the batched variant.

### Why the target for tokens is what it is

A collision costs a redo of one task (about 1 unit) without the ladder, about 0.05 to 0.2 with it (to be measured). If a
fraction `c` of tasks collide once, the token increase is about `c × cost per collision`. The hot and shared edits are about a
third of the workload; the policies and checks remove the shared appends from `c`; the ladder cuts the cost of the rest.

## 4. Targets (to be met, then replaced by the measured numbers)

| | target | stretch | if missed |
|---|---|---|---|
| correct runs, no faults | 20/20 | | a failure is a bug to fix, not a number to hide |
| correct runs, with faults | 20/20 | | same |
| edits lost, full system | 0 | | |
| edits lost, shared folder and git-like level | reported as measured | | |
| speed at 32 agents, ladder and batching on | at least 8x | 15x | report the measured curve and the serial-validation limit |
| token increase over one agent | at most 10% | 5% | report the ladder-off figure beside it |
| token increase with the ladder off | reported | | the gap is the ladder's contribution |

## 5. What has to be built

| # | Work | Done when |
|---|---|---|
| U4 (done, Claude pending) | the ladder: re-apply and compare, continue the conversation, per-task layers, per-layer statistics, `claude -p` as a model | tokens per resolution by layer measured on `eco` and `quality` (`docs/eval/results.md`: layer 2 cut tokens 55 to 65%, layer 3 did not pay off); Claude to be measured |
| U5 | batched and speculative validation, width cap, hedging | the speed curve over 1 to 32 agents, with the validation floor visible |
| S1 | trace recorder: per-kind task time and tokens, per-layer collision cost, from real runs | traces for Claude, then `eco` and `quality`, stored with sample counts. The per-layer collision cost of the local models is already in `workspace/eval/results/ollama_ladder.jsonl` (the `ladder` field of each row) |
| S2 | stress harness: workload generator, 32 agent processes against the real cluster, fault injection (agents, replicas, queue), the checks of section 2, results as rows | 20 + 20 runs produce the table; a deliberately broken variant (lossy merge, no read set) fails its checks |
| S3 | replay mode: agents that wait and spend from a trace; small-N validation against real agents | prediction within the stated margin at N = 4 and 8 |
| U6, U7 | tools for an orchestrating agent; replay of real git history | as in document 12 |
| optional | per-key linearizability checker over recorded gateway histories | a recorded history of at least 100,000 operations passes, and an injected stale read fails |

## 6. Risks

* **Serial validation caps speed** (section 3). If batching cannot pass about 8x, the claim is the measured curve.
* **Cluster capacity.** 32 agents landing every few seconds is many store operations; if the gateway saturates, the stress
  runs use more groups, and the capacity limit is itself a result.
* **Scripted agents** are not models: the reliability claim is about the system's guarantees, not about model quality, and
  says so.
* **Trace validity.** A replay is only as good as the traces; hence the small-N check against real agents.
* **Availability of the models.** Claude runs from the author's terminal; the local models are served intermittently and one
  at a time (two at once swap in and out of memory and slow each other).
