"""Running a task graph: as many tasks at once as the graph, the leases and the worker pool allow.

For each task that is ready the scheduler takes a path lease, forks a branch from the *current* main,
runs the worker, audits what the worker touched against its declared scope, then lands the branch
and releases the lease. Because a task starts only after everything it depends on or overlaps has
landed, it forks from a main that already contains that work. Landing here is the plain
``Repository.submit``; the validating merge queue replaces it in the next stage.
"""

from __future__ import annotations

import statistics
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..claims import BUSY, COMPLETED, TaskClaims
from ..leases import Audit, LeaseTable
from ..repo import Repository, SubmitResult
from . import fence
from .graph import TaskGraph
from .worker import Task, Worker, WorkerResult, run_task

DONE, FAILED, REJECTED, SKIPPED = "done", "failed", "rejected", "skipped"

Land = Callable[[Repository, Task, WorkerResult], Optional[SubmitResult]]


def land_directly(repo: Repository, task: Task, result: WorkerResult, agent_id: Optional[str] = None) -> Optional[SubmitResult]:
    ws = repo.resume(agent_id or task.id)
    return repo.submit(ws, f"{task.id}: {result.summary.strip().splitlines()[0][:80] if result.summary.strip() else task.description[:60]}",
                       validate=lambda candidate: fence.check())


@dataclass
class Outcome:
    task: Task
    status: str                                   # done | failed | rejected | skipped
    result: Optional[WorkerResult] = None
    landed: Optional[SubmitResult] = None
    audit: Optional[Audit] = None
    reason: str = ""
    started: float = 0.0
    finished: float = 0.0
    agent: str = ""                               # the branch that produced this outcome (a backup has its own)
    hedged: bool = False                          # a backup agent was started for this task


@dataclass
class RunReport:
    outcomes: Dict[str, Outcome] = field(default_factory=dict)
    max_concurrency: int = 0
    width: int = 0                                # the most tasks the graph lets run together
    hedges: int = 0                               # backups started
    hedge_wins: int = 0                           # tasks whose backup, not the original, landed

    @property
    def ok(self) -> bool:
        return all(o.status == DONE for o in self.outcomes.values())

    def by_status(self, status: str) -> List[str]:
        return [t for t, o in self.outcomes.items() if o.status == status]


class Scheduler:
    def __init__(self, repo: Repository, leases: LeaseTable, worker: Worker, parallel: int = 4,
                 land: Land = land_directly, lease_ttl: Optional[float] = None, poll: float = 0.05,
                 patience: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, claims: Optional[TaskClaims] = None,
                 owner: str = "scheduler", claim_ttl: Optional[float] = None,
                 heartbeat: Optional[float] = None, cap_to_width: bool = True, hedge_after: Optional[float] = None,
                 hedge_min_peers: int = 3, hedge_min_seconds: float = 0.0, hedge_slots: int = 1,
                 landed_check: Optional[Callable[[Task], Optional[str]]] = None) -> None:
        """``cap_to_width``: never run more agents than the graph lets run together. ``hedge_after`` (a factor k): when a
        running task has taken more than k times the median of the tasks finished so far (at least ``hedge_min_peers`` of
        them, and ``hedge_min_seconds``), start one backup agent on its own branch; the first to land wins, the other is
        released without landing. At most ``hedge_slots`` backups run at once."""
        if hedge_after is not None and hedge_after <= 0:
            raise ValueError("hedge_after must be a positive factor")
        self.cap_to_width, self.hedge_after = cap_to_width, hedge_after
        self.landed_check = landed_check              # task -> commit if an earlier owner already landed it (for takeovers)
        self.hedge_min_peers, self.hedge_min_seconds, self.hedge_slots = hedge_min_peers, hedge_min_seconds, hedge_slots
        self.repo, self.leases, self.worker, self.parallel = repo, leases, worker, parallel
        self.land, self.lease_ttl, self.poll, self.patience = land, lease_ttl, poll, patience
        self._sleep, self._clock = sleep, clock
        self._last_beat = clock()
        self.claims, self.owner, self.claim_ttl = claims, owner, claim_ttl
        ttl = claim_ttl if claim_ttl is not None else (claims.ttl if claims is not None else 0)
        self.heartbeat = heartbeat if heartbeat is not None else (ttl / 3 if claims is not None else None)

    def run(self, graph: TaskGraph) -> RunReport:
        report = RunReport()
        report.width = graph.width()
        parallel = min(self.parallel, report.width) if self.cap_to_width and report.width else self.parallel
        finished: Set[str] = set()              # done, failed, rejected or skipped: no longer in the way
        landed_ok: Set[str] = set()
        started: Set[str] = set()
        running: Dict[Future, Task] = {}
        copies: Dict[Future, Tuple[str, bool]] = {}      # future -> (agent branch, is a backup)
        begun: Dict[str, float] = {}                     # task -> when its original agent started
        hedged: Set[str] = set()
        peers: List[float] = []                          # durations of tasks that landed, for the straggler rule
        gates: Dict[str, threading.Lock] = {}            # one landing at a time per task (original and backup)
        landed_by: Dict[str, str] = {}                   # task -> the branch that landed it
        gauge = threading.Lock()
        live = [0]
        epochs: Dict[str, int] = {}             # task -> epoch of the claim we hold on it
        lost: Set[str] = set()                  # tasks whose claim another owner has taken over

        def work(task: Task, agent: str, backup: bool) -> Outcome:
            out = Outcome(task, FAILED, started=self._clock(), agent=agent, hedged=task.id in hedged or backup)
            try:
                with gauge:
                    live[0] += 1
                    report.max_concurrency = max(report.max_concurrency, live[0])
                result = run_task(self.worker, self.repo, task, agent)
                out.result = result
                out.audit = self.leases.audit(task.id, result.touched, task.paths)
                if not result.ok:
                    out.reason = result.error or result.status
                elif task.id in lost:
                    out.reason = fence.LOST
                else:
                    if self.claims is not None:
                        epoch = epochs[task.id]
                        guard = lambda: None if self.claims.holds(task.id, self.owner, epoch) else fence.LOST  # noqa: E731
                    else:
                        guard = lambda: None  # noqa: E731
                    with gates[task.id]:                      # the original and its backup never land at the same time
                        if task.id in landed_by:
                            out.status, out.reason = FAILED, "superseded: another agent already landed this task"
                            return out
                        with fence.fenced(guard):
                            landed = self.land(self.repo, task, result) if not backup else self.land(
                                self.repo, task, result, agent_id=agent)
                        out.landed = landed
                        if landed is None or landed.landed or landed.status == "noop":
                            out.status = DONE
                            landed_by[task.id] = agent
                        elif getattr(landed, "detail", "") == fence.LOST:
                            out.status, out.reason = FAILED, fence.LOST
                            lost.add(task.id)
                        else:
                            out.status, out.reason = REJECTED, landed.status
            except Exception as e:                               # a bug in a worker must not stall the run
                out.reason = f"{type(e).__name__}: {e}"
            finally:
                with gauge:
                    live[0] -= 1
            out.finished = self._clock()
            return out

        def settle(task: Task, out: Outcome) -> None:
            """Record the task's outcome once, complete its claim and release its lease. Not in a finally clause on
            purpose: a process that dies before this leaves its claim and lease behind, to expire, as a crash would."""
            if self.claims is not None and task.id not in lost:
                self.claims.complete(task.id, self.owner, epochs[task.id], {
                    "status": out.status, "reason": out.reason,
                    "commit": out.landed.commit if out.landed is not None else None})
            if task.id not in lost:
                self.leases.release(task.id)
            out.hedged = task.id in hedged                 # known only now: the backup may have started after the original did
            report.outcomes[task.id] = out
            finished.add(task.id)
            if out.status == DONE:
                landed_ok.add(task.id)
                peers.append(out.finished - begun.get(task.id, out.started))
                if out.agent != task.id:
                    report.hedge_wins += 1

        def maybe_hedge(pool: ThreadPoolExecutor) -> None:
            if self.hedge_after is None or len(peers) < self.hedge_min_peers:
                return
            limit = statistics.median(peers) * self.hedge_after
            now = self._clock()
            backups = sum(1 for f, (a, b) in copies.items() if b and not f.done())
            for fut, task in list(running.items()):
                if backups >= self.hedge_slots:
                    break
                if copies[fut][1] or task.id in hedged or task.id in finished or task.id in lost:
                    continue
                if now - begun[task.id] > max(limit, self.hedge_min_seconds):
                    hedged.add(task.id)
                    report.hedges += 1
                    backups += 1
                    agent = f"{task.id}-hedge"
                    nf = pool.submit(work, task, agent, True)
                    running[nf] = task
                    copies[nf] = (agent, True)

        # with hedging, a copy that lost the race keeps its thread until it ends, so there is room for those as well
        pool = ThreadPoolExecutor(max_workers=parallel + max(0, self.hedge_slots) + (parallel if self.hedge_after else 0))
        try:
            idle_since: Optional[float] = None
            while len(finished) < len(graph.tasks):
                self._skip_dead(graph, finished, landed_ok, report)
                if len(finished) == len(graph.tasks):
                    break
                for task in graph.ready(finished, started):
                    if sum(1 for f, (a, b) in copies.items() if not b and not f.done() and running[f].id not in finished) >= parallel:
                        break              # a backup, and an original whose backup already landed the task, do not count
                    if self.claims is not None:
                        claim = self.claims.claim(task.id, self.owner, self.claim_ttl)
                        if claim.status == COMPLETED:               # an earlier run already finished it
                            self._adopt(task, claim.outcome, report, finished, landed_ok)
                            continue
                        if claim.status == BUSY:
                            continue                                # another owner is running it
                        epochs[task.id] = claim.epoch
                        if claim.epoch > 1 and self.landed_check is not None:
                            # taken over from an owner that died: it may have been killed right after landing, before it
                            # recorded that. Running the task again would apply it twice, so look at main first.
                            done_commit = self.landed_check(task)
                            if done_commit:
                                out = Outcome(task, DONE, reason="landed by an earlier owner", started=self._clock(),
                                              finished=self._clock(), agent=task.id)
                                begun[task.id] = out.started
                                self.claims.complete(task.id, self.owner, epochs[task.id],
                                                     {"status": "done", "reason": out.reason, "commit": done_commit})
                                report.outcomes[task.id] = out
                                finished.add(task.id)
                                landed_ok.add(task.id)
                                started.add(task.id)
                                continue
                    got = self.leases.acquire(task.id, task.paths, self.lease_ttl)
                    if not got.granted:
                        if self.claims is not None:
                            self.claims.release(task.id, self.owner, epochs[task.id])
                        continue                                  # someone outside this run holds the files
                    started.add(task.id)
                    gates[task.id] = threading.Lock()
                    begun[task.id] = self._clock()
                    fut = pool.submit(work, task, task.id, False)
                    running[fut] = task
                    copies[fut] = (task.id, False)
                maybe_hedge(pool)
                self._beat(running, epochs, lost)
                if running:
                    idle_since = None
                    done, _ = wait(list(running), timeout=self.poll, return_when=FIRST_COMPLETED)
                    for fut in done:
                        task = running.pop(fut)
                        out = fut.result()
                        if task.id in finished:
                            continue                                # the other copy of this task settled it already
                        siblings = [f for f, t in running.items() if t.id == task.id]
                        if out.status != DONE and siblings:
                            continue                                # wait for the other copy before calling the task failed
                        settle(task, out)
                else:
                    idle_since = idle_since if idle_since is not None else self._clock()
                    if self._clock() - idle_since > self.patience:
                        for t in graph.tasks:
                            if t.id not in finished:
                                report.outcomes[t.id] = Outcome(t, SKIPPED, reason="path lease held elsewhere")
                                finished.add(t.id)
                        break
                    self._sleep(self.poll)
        finally:
            # A copy that lost the race is not killed (a thread cannot be), but the run does not wait for it: its result is
            # discarded and the landing gate refuses it. Without hedging the pool is simply drained as before.
            pool.shutdown(wait=not hedged)
        return report

    def _adopt(self, task: Task, outcome: dict, report: RunReport, finished: Set[str], landed_ok: Set[str]) -> None:
        status = outcome.get("status", FAILED)
        report.outcomes[task.id] = Outcome(task, status, reason=outcome.get("reason", "") or "completed earlier")
        finished.add(task.id)
        if status == DONE:
            landed_ok.add(task.id)

    def _beat(self, running: Dict[Future, Task], epochs: Dict[str, int], lost: Set[str]) -> None:
        """Keep the claims and leases of running tasks alive; note the claims that were taken over."""
        if self.claims is None or self.heartbeat is None:
            return
        now = self._clock()
        if now - self._last_beat < self.heartbeat:
            return
        self._last_beat = now
        for task in list(running.values()):
            if task.id in lost:
                continue
            if not self.claims.renew(task.id, self.owner, epochs[task.id], self.claim_ttl):
                lost.add(task.id)
                continue
            if not self.leases.renew(task.id, self.lease_ttl):
                self.leases.acquire(task.id, task.paths, self.lease_ttl)   # it ran out meanwhile

    @staticmethod
    def _skip_dead(graph: TaskGraph, finished: Set[str], landed_ok: Set[str], report: RunReport) -> None:
        """A task whose dependency failed can never run; mark it (and its dependents) skipped."""
        changed = True
        while changed:
            changed = False
            for t in graph.tasks:
                if t.id in finished:
                    continue
                bad = [d for d in t.depends_on if d in finished and d not in landed_ok]
                if bad:
                    report.outcomes[t.id] = Outcome(t, SKIPPED, reason=f"dependency {bad[0]} did not land")
                    finished.add(t.id)
                    changed = True
