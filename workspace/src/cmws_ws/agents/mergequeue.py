"""The merge queue: the only way work reaches main in an orchestrated run.

A finished branch is landed one at a time. Each landing merges the branch with the current main,
runs the validators on *the merged result* (not on the branch alone: two changes that each pass can
break together), and moves main only if they pass. Main therefore only ever holds states a validator
has approved, however many agents finish at once.

Between merging and validating, two cheap deterministic checks run (no model, no subprocess unless a task asks):
the **survival check** (lines either side added or removed on purpose must have survived, ``survival.py``) and the
task's **transition checks** (``accept``, over main before and after this landing, ``checks.py``). Merge
policies declared by the project or the task (``policy.py``) apply while merging.

``isolation`` picks how strictly reads are validated: ``snapshot`` (default, what 1.0 does) refuses a landing whose
reads went stale; ``gitlike`` merges text only, like a branch and a pull request, and is there as the baseline
the evaluation compares against.

When a branch cannot land the queue escalates, cheapest first:

1. the automatic three-way merge (inside ``Repository.submit``);
2. a *merger* agent, which gets the branch merged with main, conflict markers in place, and the
   intent of both sides, and writes the resolution;
3. a *redo*: the task is run again from the new main, told what landed in the meantime;
4. a person: the branch and the reasons are recorded and nothing further happens automatically.

The queue's lock only serializes validate-and-land inside this process. Correctness never depends
on it: the final step is a compare-and-set of main, so several queues, or an agent landing directly,
cannot corrupt anything; they only cost each other a retry.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set

from ..checks import run_checks
from ..diff import changed_files, diff_trees, unified_all
from ..history import commits_between, merge_base
from ..kv import KV
from ..objects import Commit
from ..repo import Repository, SubmitResult
from ..survival import survival_problems
from . import fence
from .ladder import Reapplier, match_style, plausible, validate_layers
from .worker import DONE, LoopWorker, Task, Worker, WorkerResult, assume_read, run_task

MARKER = re.compile(r"^(<<<<<<< |>>>>>>> )", re.M)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    output: str = ""


class Validator(Protocol):
    def check(self, files: Dict[str, bytes], changed: List[str]) -> Verdict: ...


class FnValidator:
    def __init__(self, fn: Callable[[Dict[str, bytes], List[str]], Verdict]) -> None:
        self.fn = fn

    def check(self, files, changed):
        return self.fn(files, changed)


class CommandValidator:
    """Writes the candidate tree to a folder and runs a command there (for example the test suite).
    Exit status 0 accepts."""

    def __init__(self, argv: Sequence[str], timeout: float = 600.0) -> None:
        self.argv, self.timeout = list(argv), timeout

    def check(self, files, changed):
        with tempfile.TemporaryDirectory(prefix="cmws-validate-") as tmp:
            for path, data in files.items():
                target = Path(tmp) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            try:
                proc = subprocess.run(self.argv, cwd=tmp, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return Verdict(False, f"validation timed out after {self.timeout:.0f}s")
            except OSError as e:
                return Verdict(False, f"cannot run {self.argv[0]}: {e}")
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()[-3000:]
            return Verdict(proc.returncode == 0, out)


@dataclass
class Step:
    stage: str          # auto | reapply | merger | continue | redo | human
    status: str
    detail: str = ""
    tokens: int = 0     # model tokens this step spent (when the worker or reapplier reports them)


@dataclass
class Landing:
    status: str                              # landed | noop | escalated
    via: str = "auto"                        # which stage got it in (auto | reapply | merger | continue | redo | human)
    commit: Optional[str] = None
    steps: List[Step] = field(default_factory=list)
    branch: str = ""                         # the branch that landed or was left for a person
    escalation: Optional[str] = None
    detail: str = ""

    @property
    def landed(self) -> bool:
        return self.status == "landed"

    @property
    def tokens(self) -> int:
        return sum(st.tokens for st in self.steps)


class Escalations:
    """Durable list of work left for a person (bounded, append by compare-and-set)."""

    KEY = "mq/escalations"
    MAX = 500

    def __init__(self, kv: KV, prefix: str = "", clock: Callable[[], float] = time.time) -> None:
        self.kv, self.key, self.clock = kv, prefix + self.KEY, clock

    def add(self, entry: dict) -> str:
        entry = dict(entry, at=self.clock())
        entry["id"] = f"esc-{int(entry['at'] * 1000)}-{entry['task']}"
        for _ in range(200):
            raw = self.kv.get(self.key)
            items = json.loads(raw) if raw else []
            new = json.dumps((items + [entry])[-self.MAX:], sort_keys=True)
            ok, _ = self.kv.cas(self.key, raw, new) if raw is not None else self.kv.put_if_absent(self.key, new)
            if ok:
                return entry["id"]
        raise RuntimeError("escalation list is too contended")

    def list(self) -> List[dict]:
        raw = self.kv.get(self.key)
        return json.loads(raw) if raw else []


MERGER_SYSTEM = """You resolve merge conflicts in a software project through file tools.
Two engineers changed the same files at the same time. One change is already on the main line; the other
is being merged in. In conflicted text files you will see blocks:
<<<<<<< ours  ...the incoming engineer's version...  ||||||| base ...original...  =======  ...main's version...  >>>>>>> main
Write each conflicted file so that BOTH changes take effect and the result is consistent code or text,
removing every marker line. For a file that one side deleted and the other edited, decide what the intent
of both implies and either keep the edited file or delete it. Do not touch files that have no conflict.
Call finish with one line describing how you combined the changes."""


def merger_worker(model, max_steps: int = 20) -> LoopWorker:
    return LoopWorker(model, name="merger", max_steps=max_steps, system=MERGER_SYSTEM)


def _has_new_markers(candidate: Dict[str, bytes], main: Dict[str, bytes]) -> List[str]:
    bad = []
    for path, data in candidate.items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if MARKER.search(text):
            before = main.get(path)
            try:
                if before is not None and MARKER.search(before.decode("utf-8")):
                    continue
            except UnicodeDecodeError:
                pass
            bad.append(path)
    return sorted(bad)


EMPTY = "the worker finished without changing anything"


class MergeQueue:
    ISOLATIONS = ("snapshot", "gitlike")

    def __init__(self, repo: Repository, validator: Optional[Validator] = None, merger: Optional[Worker] = None,
                 redo_worker: Optional[Worker] = None, max_redos: int = 1,
                 escalations: Optional[Escalations] = None, keep_both_sides: bool = True,
                 require_changes: bool = True, isolation: str = "snapshot", survival: bool = True,
                 accept_checks: bool = True, policies: bool = True, reapply: Optional[Reapplier] = None,
                 continue_worker=None, layers: Sequence[int] = (2, 3, 4), max_reapply: int = 4, starve_after: int = 2, backoff: float = 0.0) -> None:
        if isolation not in self.ISOLATIONS:
            raise ValueError(f"unknown isolation {isolation!r}; use one of {', '.join(self.ISOLATIONS)}")
        self.repo, self.validator, self.merger = repo, validator, merger
        self.isolation, self.survival = isolation, survival
        self.accept_checks, self.policies = accept_checks, policies
        self.reapply, self.continue_worker, self.max_reapply = reapply, continue_worker, max_reapply
        self.starve_after = starve_after
        self.backoff = backoff                          # seconds; random waits between attempts so racers spread out
        self.layers = validate_layers(list(layers)) or ()
        self.ladder: Dict[int, Dict[str, int]] = {n: {"tried": 0, "settled": 0, "tokens": 0} for n in (2, 3, 4)}
        self._stat_lock = threading.Lock()
        self._ctx = threading.local()                    # the task being landed on this thread (for its rules and checks)
        self.keep_both_sides = keep_both_sides
        self.require_changes = require_changes
        self.redo_worker, self.max_redos = redo_worker, max_redos
        self.escalations = escalations or Escalations(repo.kv)
        self._lock = threading.RLock()
        self.approved: List[str] = []                 # every candidate a validator accepted, for audit

    # ---- validating and landing one branch ----------------------------------------------

    def _survival(self, base: str, mine: str, main: str, result: str, only: Optional[Sequence[str]] = None) -> str:
        """Problems of the survival check between four commits, as one text ('' when all survived)."""
        store = self.repo.store
        t = {k: store.get_commit(c).tree for k, c in (("base", base), ("mine", mine), ("main", main), ("result", result))}
        paths = {c.path for tree in (t["mine"], t["main"]) for c in diff_trees(store, t["base"], tree)}
        if only is not None:
            paths &= set(only)
        read = lambda tree: {p: d for p in paths if (d := store.read_file(tree, p)) is not None}     # noqa: E731
        found = survival_problems(read(t["base"]), read(t["mine"]), read(t["main"]), read(t["result"]),
                                  "this change", "main")
        return "; ".join(found)

    def _precheck(self, candidate: str, main: str, ws, task: Optional[Task]) -> Optional[str]:
        """The cheap deterministic checks on a candidate built on ``main``: conflict markers, the survival check and the
        task's transition checks. Returns a refusal, or None."""
        store = self.repo.store
        files = self.repo.files_at(candidate)
        before = self.repo.files_at(main)
        markers = _has_new_markers(files, before)
        if markers:
            return "unresolved conflict markers in " + ", ".join(markers)
        if self.survival and ws is not None:
            try:
                base = merge_base(store, main, ws.head)
                lost_work = self._survival(base, ws.head, main, candidate) if base is not None else ""
            except Exception as e:                       # noqa: BLE001 - a check that cannot run refuses, it never waves through
                return f"the survival check could not run: {type(e).__name__}: {e}"
            if lost_work:
                return "work was lost in the merge: " + lost_work
        if self.accept_checks and task is not None and task.accept:
            try:
                failed = run_checks(task.accept, before, files)
            except Exception as e:                       # noqa: BLE001
                return f"the acceptance checks could not run: {type(e).__name__}: {e}"
            if failed:
                return "acceptance check failed: " + "; ".join(failed)
        return None

    def _run_validator(self, candidate: str, main: str) -> Optional[str]:
        """Your validation command on the whole candidate tree; refusal text or None. Records the candidate as approved."""
        store = self.repo.store
        if self.validator is not None:
            files = self.repo.files_at(candidate)
            changed = sorted(changed_files(store, store.get_commit(main).tree, store.get_commit(candidate).tree))
            verdict = self.validator.check(files, changed)
            if not verdict.ok:
                return verdict.output or "validation failed"
        self.approved.append(candidate)
        return None

    def _validate(self, candidate: str, before_commit: Optional[str] = None, ws=None, task: Optional[Task] = None) -> Optional[str]:
        lost = fence.check()
        if lost is not None:
            return lost                                  # the claim moved on: do not let this land
        main = before_commit or self.repo.head()
        problem = self._precheck(candidate, main, ws, task)
        if problem is not None:
            return problem
        return self._run_validator(candidate, main)

    def _try(self, agent_id: str, message: Optional[str]) -> SubmitResult:
        task = getattr(self._ctx, "task", None)
        with self._lock:
            ws = self.repo.resume(agent_id)
            rules = task.merge if (task is not None and self.policies) else ()
            r = self.repo.submit(ws, message, allow_stale=self.isolation == "gitlike", task_rules=rules,
                                 validate_against=lambda cand, before: self._validate(cand, before, ws, task))
        if r.status == "rejected" and r.detail == fence.LOST:
            raise fence.ClaimLost()
        return r

    def attempt(self, agent_id: str, message: Optional[str] = None, task: Optional[Task] = None) -> SubmitResult:
        """One try at landing a branch, validated, with no merger, no redo and no escalation.

        For a caller that is itself an agent and can act on the answer (``conflict``, ``stale``,
        ``rejected``, ``contended``). Raises ``fence.ClaimLost`` if a fence is installed and refuses.
        ``task`` brings its merge rules and acceptance checks into this one landing.
        """
        with self.for_task(task if task is not None else getattr(self._ctx, "task", None)):
            return self._try(agent_id, message)

    @contextmanager
    def for_task(self, task: Optional[Task]):
        """Within the block, landings on this thread use ``task``'s merge rules and acceptance checks."""
        previous = getattr(self._ctx, "task", None)
        self._ctx.task = task
        try:
            yield self
        finally:
            self._ctx.task = previous

    # ---- the escalation chain -----------------------------------------------------------

    def land(self, task: Task, result: Optional[WorkerResult] = None, agent_id: Optional[str] = None) -> Landing:
        previous = getattr(self._ctx, "task", None)
        self._ctx.task = task
        try:
            return self._land(task, result, agent_id)
        except fence.ClaimLost:                           # stop at once: no merger, no redo, no escalation
            return Landing("lost", branch=agent_id or task.id, detail=fence.LOST,
                           steps=[Step("fence", "lost", fence.LOST)])
        finally:
            self._ctx.task = previous

    def _land(self, task: Task, result: Optional[WorkerResult], agent_id: Optional[str]) -> Landing:
        agent_id = agent_id or task.id
        summary = (result.summary.strip().splitlines()[0][:80] if result and result.summary.strip()
                   else task.description[:60])
        message = f"{task.id}: {summary}"
        landing = Landing("escalated", branch=agent_id)
        orig_base = self.repo.resume(agent_id).fork_base       # what the agent saw; the merger stage moves it
        if self.require_changes and result is not None and result.ok and result.commit is None:
            r = SubmitResult("empty", detail=EMPTY)           # "done" without a single edit is not a landing
            landing.steps.append(Step("auto", r.status, r.detail))
        else:
            r = self._try(agent_id, message)
            landing.steps.append(Step("auto", r.status, self._describe(r)))
            if r.landed or r.status == "noop":
                return self._done(landing, r, "auto", agent_id)
            if r.status == "contended":
                r = self._try(agent_id, message)             # main was moving; one more go
                landing.steps.append(Step("auto", r.status, self._describe(r)))
                if r.landed or r.status == "noop":
                    return self._done(landing, r, "auto", agent_id)

        if r.status == "stale" and self._layer(task, 2) and self.reapply is not None:
            for attempt in range(self.max_reapply):                  # main may move again while the model answers
                self._pause(attempt)
                # A task that keeps losing the race (a hot counter with many agents) is no longer left to chance: from its
                # ``starve_after``-th attempt the answer and the landing happen without anyone else landing in between here.
                with (self._lock if attempt >= self.starve_after else nullcontext()):
                    rr2 = self._reapply_stage(task, agent_id, r, landing, message)
                if rr2 is None:
                    break
                if rr2.landed or rr2.status == "noop":
                    return self._done(landing, rr2, "reapply", agent_id)
                r = rr2
                if r.status != "stale":
                    break

        if self.merger is not None and r.status in ("conflict", "rejected"):
            mr = self._merger_stage(task, agent_id, orig_base, r, landing, message)
            if mr is not None:
                if mr.landed or mr.status == "noop":
                    return self._done(landing, mr, "merger", agent_id)
                mr.conflicts = mr.conflicts or r.conflicts      # keep what the person needs to see
                r = mr

        if self._layer(task, 3) and self.continue_worker is not None and result is not None and result.transcript:
            cr = self._continue_stage(task, agent_id, result, orig_base, r, landing, message)
            if cr is not None:
                if cr.landed or cr.status == "noop":
                    return self._done(landing, cr, "continue", agent_id)
                r = cr

        if self.redo_worker is not None and self._layer(task, 4):
            for n in range(1, self.max_redos + 1):
                self._pause(n)
                redo_id = self._redo_stage(task, n, orig_base, landing, r)
                if redo_id is None:
                    continue
                rr = self._try(redo_id, message)
                landing.steps.append(Step("redo", rr.status, self._describe(rr)))
                if rr.landed or rr.status == "noop":
                    self._stat(4, 0, settled=True, tried=False)
                    return self._done(landing, rr, "redo", redo_id)
                r = rr
        return self._human(task, agent_id, landing, r)

    @staticmethod
    def _describe(r: SubmitResult) -> str:
        if r.status == "conflict":
            return "; ".join(f"{c.path} ({c.kind})" for c in r.conflicts)
        if r.status == "stale":
            return "; ".join(s.path for s in r.stale_reads)
        return r.detail

    @staticmethod
    def _done(landing: Landing, r: SubmitResult, via: str, branch: str) -> Landing:
        landing.status = "noop" if r.status == "noop" else "landed"
        landing.via, landing.commit, landing.branch = via, r.commit, branch
        return landing

    def _merger_stage(self, task: Task, agent_id: str, orig_base: str, r: SubmitResult,
                      landing: Landing, message: str) -> Optional[SubmitResult]:
        repo = self.repo
        ws = repo.resume(agent_id)
        main = repo.head()
        rules = task.merge if self.policies else ()
        merged = repo.merge_into(ws.head, main, f"ws/{agent_id}", "main", keep_going=True,
                                 policy=repo.policy_table(main, rules))
        if merged.clean and r.status == "conflict":
            return self._try(agent_id, message)                # main moved and the conflict went away
        cid = repo.store.put_commit(Commit(merged.tree, (ws.head, main), agent_id,
                                           f"Merge main into ws/{agent_id} (conflicts marked)", repo.clock()))
        branch_head = ws.head
        ws._adopt(cid, main)
        paths = tuple(sorted(c.path for c in merged.conflicts))
        upstream = self._upstream_summary(orig_base, main)
        problem = (f"Validation of the merged result failed:\n{r.detail}\n" if r.status == "rejected" else "")
        t = Task(f"{agent_id}-merge",
                 f"Resolve the merge of this change into main.\nThe change being merged: {task.description}\n"
                 f"What landed on main meanwhile:\n{upstream}\n{problem}"
                 f"Files with conflicts: {', '.join(paths) or '(none marked; fix the validation failure)'}.",
                 paths or tuple(), "")
        res = self.merger.run(t, repo, agent_id)
        landing.steps.append(Step("merger", res.status, res.summary or res.error))
        if not res.ok:
            return None
        if self.keep_both_sides:
            resolved = repo.resume(agent_id).head
            dropped = self._dropped_a_side(resolved, branch_head, main, [c.path for c in merged.conflicts])
            if not dropped and self.survival and r.status == "conflict":
                # only the files that were in conflict: after a validation failure the merger may rewrite anything
                base = merge_base(repo.store, branch_head, main)
                if base is not None:
                    dropped = self._survival(base, branch_head, main, resolved, [c.path for c in merged.conflicts])
            if dropped:
                landing.steps.append(Step("merger", "discarded-side", dropped))
                return None
        rr = self._try(agent_id, message)
        landing.steps.append(Step("merger", f"land:{rr.status}", self._describe(rr)))
        return rr

    def _dropped_a_side(self, resolved: str, branch_head: str, main: str, paths: List[str]) -> str:
        """A resolution that equals one side's whole file threw the other side's change away.

        Validation cannot see this (the program still runs, one task's effect is just gone), so the queue
        refuses it and lets the task be redone on top of what landed. Returns a description, or "".
        """
        after, mine, theirs = (self.repo.files_at(c) for c in (resolved, branch_head, main))
        for path in paths:
            a, m, t = after.get(path), mine.get(path), theirs.get(path)
            if a is None or m is None or t is None or m == t:
                continue                                  # deletions and identical sides are judged elsewhere
            if a == m:
                return f"{path}: the resolution is the branch's version, main's changes were dropped"
            if a == t:
                return f"{path}: the resolution is main's version, the branch's changes were dropped"
        return ""

    def _upstream_summary(self, since: str, main: str) -> str:
        store = self.repo.store
        lines = [f"- {store.get_commit(c).message}" for c in commits_between(store, since, main)][:20]
        return "\n".join(lines) or "(nothing recorded)"

    def _redo_stage(self, task: Task, n: int, orig_base: str, landing: Landing, last: SubmitResult) -> Optional[str]:
        repo = self.repo
        store = repo.store
        diff = unified_all(store, store.get_commit(orig_base).tree, store.get_commit(repo.head()).tree)
        if len(diff) > 8000:
            diff = diff[:8000] + "\n[... diff cut off]"
        redo_id = f"{task.id}-redo{n}"
        why = (f"Your earlier attempt could not be merged: {self._describe(last) or last.status}. "
               if last is not None else "")
        t = Task(redo_id, task.description, task.paths,
                 (task.context + "\n\n" if task.context else "") + why +
                 "Since then these changes landed on the main line; the project you see already includes them. "
                 "Do the task again on top of them:\n" + (diff or "(no textual changes)"),
                 task.depends_on, task.merge, task.accept, task.layers)
        res = run_task(self.redo_worker, repo, t, redo_id)
        spent = res.prompt_tokens + res.completion_tokens
        landing.steps.append(Step("redo", res.status, res.summary or res.error, spent))
        self._stat(4, spent, settled=False)
        if res.ok and res.commit is None and self.require_changes:
            landing.steps.append(Step("redo", "empty", EMPTY))
            return None                                       # it did nothing again: try once more or give up
        return redo_id if res.ok else None

    # ---- the cost ladder: layers 2 and 3 -----------------------------------------------------

    def _pause(self, attempt: int) -> None:
        """A random wait before the ``attempt``-th retry (none for the first), growing with the attempt number."""
        if self.backoff > 0 and attempt > 0:
            time.sleep(random.uniform(0, self.backoff * 2 ** min(attempt, 5)))

    def _layer(self, task: Task, n: int) -> bool:
        allowed = task.layers if task.layers is not None else self.layers
        return n in allowed

    def _stat(self, layer: int, tokens: int, settled: bool, tried: bool = True) -> None:
        with self._stat_lock:
            row = self.ladder[layer]
            row["tried"] += 1 if tried else 0
            row["settled"] += 1 if settled else 0
            row["tokens"] += tokens

    def ladder_report(self) -> Dict[int, Dict[str, int]]:
        with self._stat_lock:
            return {n: dict(row) for n, row in self.ladder.items()}

    def _reapply_stage(self, task: Task, agent_id: str, r: SubmitResult, landing: Landing,
                       message: str) -> Optional[SubmitResult]:
        """Layer 2: for every stale file the agent also changed, ask what it would be if the task were done again on
        main, compare with main, and land the resolution (or the agreement) through the normal checks."""
        repo, store = self.repo, self.repo.store
        ws = repo.resume(agent_id)
        main = repo.head()
        skip = lambda why: (landing.steps.append(Step("reapply", "skipped", why)), None)[1]       # noqa: E731
        if not r.stale_reads or any(s.kind != "file" for s in r.stale_reads):
            return skip("a directory listing went stale; a file cannot be re-applied")
        head_tree = store.get_commit(ws.head).tree
        texts: Dict[str, tuple] = {}
        for s_ in r.stale_reads:
            mine_e = store.lookup(head_tree, s_.path)
            mine_id = mine_e.id if mine_e is not None and mine_e.mode == "file" else None
            if s_.observed is None or s_.current is None or mine_id is None or mine_id == s_.observed:
                return skip(f"{s_.path}: the agent did not change it or it was added or removed, nothing to re-apply")
            try:
                texts[s_.path] = tuple(store.get_blob(b).decode("utf-8") for b in (s_.observed, mine_id, s_.current))
            except UnicodeDecodeError:
                return skip(f"{s_.path} is not text")
        spent, chosen = 0, {}
        for path, (base_t, mine_t, main_t) in sorted(texts.items()):
            ans = self.reapply.reapply(task, path, base_t, mine_t, main_t)
            spent += ans.tokens
            if ans.text is None:
                self._stat(2, spent, settled=False)
                landing.steps.append(Step("reapply", "no-answer", f"{path}: {ans.note}", spent))
                return None
            new = match_style(ans.text, main_t)
            if new != main_t and not plausible(base_t, mine_t, main_t, new):
                self._stat(2, spent, settled=False)
                landing.steps.append(Step("reapply", "implausible", f"{path}: the answer rewrites far more than the change did", spent))
                return None
            chosen[path] = new
        policy = repo.policy_table(main, task.merge if self.policies else ())
        merged = repo.merge_into(ws.head, main, f"ws/{agent_id}", "main", keep_going=True, policy=policy)
        others = [c.path for c in merged.conflicts if c.path not in chosen]
        if others:
            self._stat(2, spent, settled=False)
            landing.steps.append(Step("reapply", "conflicts-elsewhere", ", ".join(others), spent))
            return None
        tree = store.update(merged.tree, {p: store.put_blob(t.encode("utf-8")) for p, t in chosen.items()})
        agreed = [p for p, t in chosen.items() if t == texts[p][2]]
        if tree == store.get_commit(main).tree:                      # nothing left to land: the change is on main already
            if self.accept_checks and task.accept:
                files = repo.files_at(main)
                failed = run_checks(task.accept, files, files)       # agreement must still satisfy the task's own conditions
                if failed:
                    self._stat(2, spent, settled=False)
                    landing.steps.append(Step("reapply", "agreement-refused", "; ".join(failed), spent))
                    return None
            self._stat(2, spent, settled=True)
            landing.steps.append(Step("reapply", "agreement", ", ".join(sorted(chosen)), spent))
            return SubmitResult("noop", detail="agreement")
        cid = store.put_commit(Commit(tree, (ws.head, main), agent_id, f"Merge main into ws/{agent_id} (re-applied)", repo.clock()))
        ws._adopt(cid, main)
        assume_read(repo, agent_id, chosen)           # the answer depends on main's file as it is now: if it moves again, stale again
        rr = self._try(agent_id, message)
        ok = rr.landed or rr.status == "noop"
        self._stat(2, spent, settled=ok)
        how = ("agreed on " + ", ".join(agreed) + "; " if agreed else "") + "re-applied " + ", ".join(sorted(set(chosen) - set(agreed)))
        landing.steps.append(Step("reapply", f"land:{rr.status}", how.strip("; ") + ((" | " + self._describe(rr)) if not ok else ""), spent))
        return rr

    def _continue_stage(self, task: Task, agent_id: str, result: WorkerResult, orig_base: str, r: SubmitResult,
                        landing: Landing, message: str) -> Optional[SubmitResult]:
        """Layer 3: merge main into the branch (conflicts marked) and give the agent's own conversation one message."""
        repo = self.repo
        ws = repo.resume(agent_id)
        main = repo.head()
        rules = task.merge if self.policies else ()
        merged = repo.merge_into(ws.head, main, f"ws/{agent_id}", "main", keep_going=True, policy=repo.policy_table(main, rules))
        cid = repo.store.put_commit(Commit(merged.tree, (ws.head, main), agent_id,
                                           f"Merge main into ws/{agent_id}" + (" (conflicts marked)" if merged.conflicts else ""),
                                           repo.clock()))
        ws._adopt(cid, main)
        paths = sorted(c.path for c in merged.conflicts)
        why = self._describe(r) or r.status
        note = (f"Your change could not be landed as it was: {r.status} ({why}). Main moved while you worked; your branch now "
                "contains it, merged into your work. "
                + (f"These files have conflict markers to resolve, keeping what both sides intended: {', '.join(paths)}. " if paths else "")
                + "What landed on main meanwhile:\n" + self._upstream_summary(orig_base, main)
                + "\nCheck that your change still does what the task asks on top of it, fix what is needed, and finish.")
        res = self.continue_worker.resume(task, repo, agent_id, result.transcript, note)
        spent = res.prompt_tokens + res.completion_tokens
        if not res.ok or (res.commit is None and self.require_changes):
            self._stat(3, spent, settled=False)
            landing.steps.append(Step("continue", res.status if not res.ok else "empty", res.summary or res.error or EMPTY, spent))
            return None
        assume_read(repo, agent_id, [c.path for c in repo.resume(agent_id).changes_since_fork()])
        rr = self._try(agent_id, message)
        ok = rr.landed or rr.status == "noop"
        self._stat(3, spent, settled=ok)
        landing.steps.append(Step("continue", f"land:{rr.status}", (res.summary or "") if ok else self._describe(rr), spent))
        return rr

    def _human(self, task: Task, agent_id: str, landing: Landing, r: SubmitResult) -> Landing:
        conflicts = [[c.path, c.kind] for c in r.conflicts]
        eid = self.escalations.add({
            "task": task.id, "branch": f"ws/{agent_id}", "reason": r.status, "detail": self._describe(r),
            "conflicts": conflicts, "steps": [[s.stage, s.status, s.detail[:300]] for s in landing.steps]})
        landing.status, landing.via, landing.escalation = "escalated", "human", eid
        landing.detail = r.status
        landing.steps.append(Step("human", "escalated", eid))
        return landing

    # ---- scheduler integration ------------------------------------------------------------

    def as_land(self):
        """Adapter for ``Scheduler(land=...)``."""
        def land(repo, task, result, agent_id=None):
            return self.land(task, result, agent_id)
        return land
