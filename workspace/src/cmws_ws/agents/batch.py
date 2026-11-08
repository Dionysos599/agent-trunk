"""A merge queue that lands several branches with one validation (doc 12, section 3.2).

The plain queue validates one landing at a time, so N tasks cost N validations in a row, and that serial validation is what
caps the speed of many agents (document 13). ``BatchQueue`` gathers the branches that finish within a short window, builds
one candidate that contains all of them (each merged on top of the previous), checks each branch's own conditions as it is
added (stale reads against the candidate so far, merge, survival, its transition checks), runs your validator **once** on the
final candidate, and moves ``main`` there with one compare-and-set.

What stays exactly as before:

* ``main`` only ever holds a state the validator approved *as it stands*: the states between the members of a batch are never
  published, and the last one is what was validated;
* a branch that cannot join (stale, conflicting, refused by a check, claim lost) is not an error of the batch: it simply
  leaves it and goes through the ordinary chain (merger, re-apply, continue, redo, person) in its own thread;
* if the validator refuses the batch it is split in halves and each half is tried again, down to a single branch, so one bad
  branch costs about log N extra validations and the others still land together;
* if ``main`` moves while the batch is being validated the batch is rebuilt on top of the new ``main``.

Correctness does not depend on this class: its only way to move ``main`` is a compare-and-set from the ``main`` it built on.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..history import is_ancestor
from ..objects import Commit
from ..repo import Repository
from . import fence
from .mergequeue import EMPTY, Landing, MergeQueue, Step
from .worker import Task, WorkerResult


@dataclass
class _Member:
    task: Task
    result: Optional[WorkerResult]
    agent_id: str
    message: str
    guard: Optional[Callable[[], Optional[str]]]
    event: threading.Event = field(default_factory=threading.Event)
    landing: Optional[Landing] = None      # set when the batch landed (or refused for good) this branch
    reason: str = ""                       # why it left the batch, for the record


class BatchQueue(MergeQueue):
    def __init__(self, repo: Repository, *args, window: float = 0.05, max_batch: int = 8, retries: int = 3, **kwargs) -> None:
        super().__init__(repo, *args, **kwargs)
        if max_batch < 1 or retries < 1 or window < 0:
            raise ValueError("max_batch and retries must be at least 1 and window not negative")
        self.window, self.max_batch, self.retries = window, max_batch, retries
        self._pending: List[_Member] = []
        self._plock = threading.Lock()
        self._leading = False
        self.batch_stats: Dict[str, int] = {"batches": 0, "landed": 0, "validations": 0, "left": 0, "splits": 0, "rebuilds": 0}
        self._slock = threading.Lock()

    def _count(self, key: str, n: int = 1) -> None:
        with self._slock:
            self.batch_stats[key] += n

    # ---- entry ----------------------------------------------------------------------------------

    def land(self, task: Task, result: Optional[WorkerResult] = None, agent_id: Optional[str] = None) -> Landing:
        agent_id = agent_id or task.id
        summary = (result.summary.strip().splitlines()[0][:80] if result and result.summary.strip() else task.description[:60])
        member = _Member(task, result, agent_id, f"{task.id}: {summary}", fence._current.get())
        with self._plock:
            self._pending.append(member)
            lead = not self._leading
            if lead:
                self._leading = True
        if lead:
            self._lead()
        member.event.wait()
        if member.landing is not None:
            return member.landing
        return super().land(task, result, agent_id)          # it left the batch: the ordinary chain, in this thread

    def _lead(self) -> None:
        """The first arrival waits a moment for company, then lands whoever is there, until nobody is waiting."""
        time.sleep(self.window)
        while True:
            with self._plock:
                batch = self._pending[:self.max_batch]
                del self._pending[:len(batch)]
                if not batch:
                    self._leading = False
                    return
            try:
                self._process(batch)
            except Exception:                                  # noqa: BLE001 - whatever went wrong, nobody may be left waiting
                pass
            finally:
                for m in batch:
                    m.event.set()

    # ---- one batch --------------------------------------------------------------------------------

    def _process(self, batch: List[_Member]) -> None:
        self._count("batches")
        members = []
        for m in batch:
            if m.result is not None and not m.result.ok:
                m.reason = m.result.error or m.result.status
            elif self.require_changes and m.result is not None and m.result.commit is None:
                m.reason = EMPTY                                # the ordinary chain records "done without a change"
            else:
                members.append(m)
        self._land_group(members)

    def _lost(self, m: _Member) -> Landing:
        return Landing("lost", branch=m.agent_id, detail=fence.LOST, steps=[Step("fence", "lost", fence.LOST)])

    def _land_group(self, members: List[_Member]) -> None:
        for _ in range(self.retries):
            if not members:
                return
            with self._lock:
                main0 = self.repo.head()
                included, candidate = self._build(members, main0)
                if not included:
                    return
                self._count("validations")
                problem = self._run_validator(candidate, main0)
                if problem is None:
                    gone = [m for m in included if m.guard is not None and m.guard() is not None]
                    if gone:                                    # a claim was taken over while validating: rebuild without it
                        for m in gone:
                            m.landing = self._lost(m)
                        members = [m for m in members if m not in gone]
                        self._count("rebuilds")
                        continue
                    if self.repo.refs.cas(self.repo.main, main0, candidate).ok:
                        for m in included:
                            self.repo.resume(m.agent_id)._adopt(candidate, candidate)
                            m.landing = Landing("landed", via="batch", commit=candidate, branch=m.agent_id,
                                                steps=[Step("batch", "landed", f"{len(included)} branches, one validation")])
                        self._count("landed", len(included))
                        return
                    self._count("rebuilds")                    # main moved: start again on top of it
                    members = included
                    continue
            if len(included) == 1:                             # refused alone: it is the culprit, the chain will say why
                included[0].reason = problem
                self._count("left")
                return
            self._count("splits")
            half = len(included) // 2
            self._land_group(included[:half])
            self._land_group(included[half:])
            return
        for m in members:                                       # out of retries: leave them to the ordinary chain
            if m.landing is None:
                m.reason = "main kept moving"

    def _build(self, members: List[_Member], main0: str):
        """Merge the members one by one on top of ``main0``. Returns (those that joined, the candidate commit)."""
        repo, store = self.repo, self.repo.store
        cand, joined = main0, []
        for m in members:
            if m.guard is not None and m.guard() is not None:
                m.landing = self._lost(m)
                continue
            ws = repo.resume(m.agent_id)
            if ws.dirty or is_ancestor(store, ws.head, cand):
                m.reason = "nothing to land or uncommitted edits"
                self._count("left")
                continue
            policy = repo.policy_table(cand, m.task.merge if self.policies else ())
            if self.isolation == "snapshot" and cand != ws.fork_base and repo.stale_reads(ws, cand, policy):
                m.reason = "stale reads"
                self._count("left")
                continue
            if is_ancestor(store, cand, ws.head):
                new = ws.head
            else:
                merged = repo.merge_into(ws.head, cand, f"ws/{m.agent_id}", repo.main, policy=policy)
                if not merged.clean:
                    m.reason = "conflict"
                    self._count("left")
                    continue
                new = store.put_commit(Commit(merged.tree, (cand, ws.head), m.agent_id, f"Merge ws/{m.agent_id}: {m.message}",
                                              repo.clock()))
            problem = self._precheck(new, cand, ws, m.task)
            if problem is not None:
                m.reason = problem
                self._count("left")
                continue
            cand = new
            joined.append(m)
        return joined, cand
