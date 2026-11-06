"""The operations an agent needs, independent of how they are offered (MCP tools, JSON on the command line).

An agent claims a task from a *board*, works on its own branch of the shared project, and submits. A board
is the same thing as an orchestrator run: tasks are stored under ``runs/<board>/plan`` and claimed with the
same expiring, fenced claims, so people and the ``orchestrate`` command can share one board.

Nothing about an agent is kept in this object between calls except what a restart can rebuild: the claim,
the lease, the branch and the record of which task the owner holds all live in the store. Two ways to work:

* **folder** (default): the branch is checked out into a folder and the agent edits it with its own tools;
* **memory** (``file_tools=True``): for clients without a file system; the server offers file tools over
  the branch, numbered (``seq``) so that a retried call runs once, and checkpoints them.

Every method returns a JSON-serialisable dict with ``ok`` and, whenever the agent has to do something, a
``next`` sentence telling it what.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import kvdoc
from ..agents import fence
from ..agents.graph import InvalidGraph, TaskGraph
from ..agents.mergequeue import Escalations, MergeQueue
from ..agents.orchestrator import RUN_ID, agent_id, namespaced, plan_key, task_from_dict, task_to_dict
from ..agents.session import Session
from ..agents.worker import Task, assume_read, task_prompt
from ..checks import unfalsifiable
from ..claims import BUSY, COMPLETED, TaskClaims
from ..diff import changed_files, unified_all
from ..history import is_ancestor, merge_base
from ..leases import leases_for
from ..objects import Commit
from ..policy import FILE_NAME as MERGE_FILE, parse_rules
from ..repo import NoSuchWorkspace, Repository
from ..sync import MARKERS, Checkout

CAP = 6000                       # characters of upstream diff handed to an agent
CHECKPOINT_EVERY = 5             # memory mode: tool calls between checkpoints
OPEN, CLAIMED, DONE, FAILED, BLOCKED = "open", "claimed", "done", "failed", "blocked"


class TrunkError(Exception):
    """Something the caller did wrong or cannot do now; the message says what to do instead."""


def _short(commit: Optional[str]) -> Optional[str]:
    return commit[:12] if commit else None


class TrunkService:
    def __init__(self, repo: Repository, board: str = "default", owner: str = "agent", workdir: Path = Path("agenttrunk-work"),
                 queue: Optional[MergeQueue] = None, file_tools: bool = False, claim_ttl: float = 900.0,
                 lease_ttl: Optional[float] = None, clock: Callable[[], float] = time.time) -> None:
        if not RUN_ID.match(board):
            raise TrunkError(f"bad board name {board!r}: letters, digits and _, at most 32")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", owner):
            raise TrunkError(f"bad agent name {owner!r}: letters, digits, _ . - (at most 64)")
        self.repo, self.board, self.owner, self.workdir = repo, board, owner, Path(workdir)
        self.queue = queue or MergeQueue(repo)
        self.file_tools, self.clock = file_tools, clock
        self.claims = TaskClaims(repo.kv, ttl=claim_ttl, clock=clock, prefix=f"runs/{board}/")
        self.leases = leases_for(repo, ttl=lease_ttl or claim_ttl, clock=clock)
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.RLock()

    # ---- keys ---------------------------------------------------------------------------

    @property
    def _active_key(self) -> str:
        return f"runs/{self.board}/active/{self.owner}"

    @property
    def _adhoc_key(self) -> str:
        return f"runs/{self.board}/adhoc"

    # ---- the board ----------------------------------------------------------------------

    def create_board(self, tasks: List[Task]) -> bool:
        """Store the board's tasks unless the board already has a plan. Returns whether this call created it."""
        TaskGraph(tasks)                                       # validates ids, dependencies and cycles
        doc = json.dumps({"goal": None, "created": self.clock(), "tasks": [task_to_dict(t) for t in tasks]},
                         sort_keys=True)
        stored, _ = self.repo.kv.put_if_absent(plan_key(self.board), doc)
        return stored

    def warnings(self, tasks: List[Task]) -> List[str]:
        """Things worth a second look in a board before agents start: acceptance checks that already pass on main (they
        cannot tell whether the task did its work) and bad lines in the project's ``.agenttrunk-merge``."""
        out: List[str] = []
        files = self.repo.main_files()
        for t in tasks:
            out += [f"task {t.id}: {w}" for w in unfalsifiable(t.accept, files)]
        data = files.get(MERGE_FILE)
        if data is not None:
            try:
                out += [f"{MERGE_FILE}: {p}" for p in parse_rules(data.decode("utf-8"))[1]]
            except UnicodeDecodeError:
                out.append(f"{MERGE_FILE} is not UTF-8 text and is ignored")
        return out

    def tasks(self) -> List[Task]:
        """Tasks in declaration order: the stored plan, then tasks agents added themselves."""
        raw = self.repo.kv.get(plan_key(self.board))
        planned = [task_from_dict(d) for d in json.loads(raw)["tasks"]] if raw else []
        adhoc = [task_from_dict(d) for d in kvdoc.load(self.repo.kv, self._adhoc_key, [])]
        return planned + adhoc

    def _graph(self) -> TaskGraph:
        try:
            return namespaced(self.board, TaskGraph(self.tasks()))
        except InvalidGraph as e:
            raise TrunkError(f"the board's tasks are invalid: {e}")

    def _states(self, graph: TaskGraph) -> Dict[str, dict]:
        snap, now = self.claims.snapshot(), self.clock()
        out: Dict[str, dict] = {}
        for t in graph.tasks:
            e = snap.get(t.id)
            if e is None or e["state"] == "free" or (e["state"] == "claimed" and e["expires"] <= now):
                out[t.id] = {"state": OPEN}
            elif e["state"] == "completed":
                st = e["outcome"].get("status", FAILED)
                out[t.id] = {"state": DONE if st == "done" else FAILED, "by": e["owner"], "commit": _short(e["outcome"].get("commit")),
                             "reason": e["outcome"].get("reason", "")}
            else:
                out[t.id] = {"state": CLAIMED, "by": e["owner"], "expires_in": round(e["expires"] - now)}
        return out

    def board_view(self) -> dict:
        graph = self._graph()
        states = self._states(graph)
        finished = {t for t, s in states.items() if s["state"] in (DONE, FAILED)}
        started = {t for t, s in states.items() if s["state"] == CLAIMED}
        ready = {t.id for t in graph.ready(finished, started)}
        failed = {t for t, s in states.items() if s["state"] == FAILED}
        cut = len(self.board) + 1
        rows = []
        for t in graph.tasks:
            s = dict(states[t.id])
            if s["state"] == OPEN:
                if any(d in failed for d in t.depends_on):
                    s["state"] = BLOCKED
                    s["reason"] = "a task it depends on failed"
                elif t.id not in ready:
                    waiting = sorted(b[cut:] for b in graph.blockers(t.id, finished))
                    s["state"] = "waiting"
                    s["waiting_for"] = waiting
            rows.append({"id": t.id[cut:], "description": t.description, "paths": list(t.paths),
                         "depends_on": [d[cut:] for d in t.depends_on], **s})
        mine = kvdoc.load(self.repo.kv, self._active_key, None)
        return {"ok": True, "board": self.board, "agent": self.owner, "main": _short(self.repo.head()),
                "tasks": rows, "yours": mine["task"] if mine else None,
                "escalations": len(Escalations(self.repo.kv).list())}

    # ---- claiming -----------------------------------------------------------------------

    def _active(self) -> Optional[dict]:
        return kvdoc.load(self.repo.kv, self._active_key, None) or None       # "{}" marks "no task"

    def _task_of(self, a: dict) -> Optional[Task]:
        try:
            return self._graph().by_id.get(a["full"])
        except TrunkError:
            return None

    def _need_active(self) -> dict:
        a = self._active()
        if a is None:
            raise TrunkError("you hold no task. Call claim_task first (show_board lists what is open).")
        return a

    def claim(self, task: Optional[str] = None, description: Optional[str] = None,
              paths: Optional[List[str]] = None) -> dict:
        """Take the next ready task (or a named one). With ``description``, put a new task of your own on the board."""
        with self._lock:
            held = self._active()
            if held is not None:
                if (task is None or task == held["task"]) and not description:
                    return self._resume(held)                     # asking again for what you hold: hand it back
                raise TrunkError(f"you already hold task {held['task']!r}. submit_task it or release_task it first.")
            if description:
                task = self._add_adhoc(task, description, paths or [])
            graph = self._graph()
            states = self._states(graph)
            finished = {t for t, s in states.items() if s["state"] in (DONE, FAILED)}
            started = {t for t, s in states.items() if s["state"] == CLAIMED}
            ready = graph.ready(finished, started)
            cut = len(self.board) + 1
            if task is not None:
                full = agent_id(self.board, task)
                if full not in graph.by_id:
                    raise TrunkError(f"no task {task!r} on board {self.board!r}.")
                if states[full]["state"] != OPEN:
                    raise TrunkError(f"task {task!r} is {states[full]['state']}"
                                     + (f" (by {states[full].get('by')})" if states[full].get("by") else "") + ".")
                if graph.by_id[full] not in ready:
                    waiting = sorted(b[cut:] for b in graph.blockers(full, finished))
                    raise TrunkError(f"task {task!r} must wait for: {', '.join(waiting)}.")
                candidates = [graph.by_id[full]]
            else:
                candidates = ready
            for t in candidates:
                got = self.claims.claim(t.id, self.owner)
                if got.status in (COMPLETED, BUSY):
                    continue
                lease = self.leases.acquire(t.id, t.paths)
                if not lease.granted:
                    self.claims.release(t.id, self.owner, got.epoch)
                    continue
                return self._start(t, got.epoch)
            raise TrunkError("no task is ready to claim right now. show_board says what is claimed, waiting or done; "
                             "try again later, or give claim_task a description to add a task of your own.")

    def _add_adhoc(self, task_id: Optional[str], description: str, paths: List[str]) -> str:
        for _ in range(20):
            existing = {t.id for t in self.tasks()}
            tid = task_id
            if tid is None:
                n = 1
                while f"adhoc-{n}" in existing:
                    n += 1
                tid = f"adhoc-{n}"
            if tid in existing:
                raise TrunkError(f"a task {tid!r} is already on the board.")
            new = Task(tid, description, tuple(paths))
            try:
                namespaced(self.board, TaskGraph(self.tasks() + [new]))      # id syntax, length, dependencies
            except InvalidGraph as e:
                raise TrunkError(f"cannot add task {tid!r}: {e}")

            def add(doc: list) -> str:
                if any(d["id"] == tid for d in doc):
                    return "exists"                                           # someone took the name first
                doc.append(task_to_dict(new))
                return "added"
            if kvdoc.update(self.repo.kv, self._adhoc_key, add, []) == "added":
                return tid
            if task_id is not None:
                raise TrunkError(f"a task {tid!r} is already on the board.")
        raise TrunkError("could not add the task; try again.")

    def _prepare(self, branch: str) -> Optional[str]:
        """Make sure the branch and its folder (or session) exist; return the folder, None in memory mode."""
        try:
            self.repo.resume(branch)
        except NoSuchWorkspace:
            self.repo.fork(branch)
        if self.file_tools:
            if branch not in self._sessions:
                self._sessions[branch] = Session.open(self.repo, branch)
            return None
        root = self.workdir / branch
        if root.exists() and any(root.iterdir()):
            Checkout.open(self.repo, root).pull()
        else:
            Checkout.clone(self.repo, branch, root)
        return str(root.resolve())

    def _describe(self, t: Task, folder: Optional[str], resumed: bool = False) -> dict:
        cut = len(self.board) + 1
        where = (f"Edit the files in {folder} with your normal tools." if folder else
                 "Use list_files, read_file, write_file and replace_in_file; every call may carry a seq number.")
        lead = "You already hold this task; here it is again. " if resumed else ""
        out = {"ok": True, "task": {"id": t.id[cut:], "description": t.description, "paths": list(t.paths),
                                    "prompt": task_prompt(Task(t.id, t.description, t.paths, t.context, (), (), t.accept))},
               "branch": f"ws/{t.id}", "folder": folder, "main": _short(self.repo.head()),
               "next": f"{lead}{where} Stay inside the declared paths. When done call submit_task; if it says main moved, "
                       "call sync_with_main, check the result, then submit_task."}
        if t.accept:
            out["task"]["accept"] = [c.describe() for c in t.accept]
        if resumed:
            out["resumed"] = True
        return out

    def _start(self, t: Task, epoch: int) -> dict:
        folder = self._prepare(t.id)
        kvdoc.update(self.repo.kv, self._active_key, lambda d: (d.clear(), d.update(
            task=t.id[len(self.board) + 1:], full=t.id, epoch=epoch, mode="memory" if self.file_tools else "folder",
            folder=folder, since=self.clock())), {})
        return self._describe(t, folder)

    def _resume(self, held: dict) -> dict:
        t = self._graph().by_id.get(held["full"])
        if t is None:
            raise TrunkError(f"your task {held['task']!r} is no longer on the board; release_task it.")
        return self._describe(t, self._prepare(held["full"]), resumed=True)

    def release(self, reason: str = "") -> dict:
        with self._lock:
            a = self._need_active()
            self.claims.release(a["full"], self.owner, a["epoch"])
            self.leases.release(a["full"])
            self._sessions.pop(a["full"], None)
            self.repo.kv.put(self._active_key, "{}")
            return {"ok": True, "released": a["task"], "next": "The branch is kept; claim_task takes the next task."}

    # ---- looking at your own work ---------------------------------------------------------

    def _checkout(self, a: dict) -> Checkout:
        return Checkout.open(self.repo, a["folder"])

    def _marked(self, a: dict) -> List[str]:
        """Files holding unresolved conflict markers."""
        if a["mode"] == "folder":
            root = Path(a["folder"])
            out = []
            co = self._checkout(a)
            for rel, _ in co._walk():
                p = root / rel
                try:
                    if p.is_file() and MARKERS.search(p.read_text(encoding="utf-8")):
                        out.append(rel)
                except (UnicodeDecodeError, OSError):
                    continue
            return sorted(out)
        ws = self._session(a).ws
        out = []
        for path, data in ws.files().items():
            try:
                if MARKERS.search(data.decode("utf-8")):
                    out.append(path)
            except UnicodeDecodeError:
                continue
        return sorted(out)

    def _session(self, a: dict) -> Session:
        s = self._sessions.get(a["full"])
        if s is None:
            s = self._sessions[a["full"]] = Session.open(self.repo, a["full"])
        return s

    def status(self) -> dict:
        with self._lock:
            a = self._need_active()
            ws = self._session(a).ws if a["mode"] == "memory" else self.repo.resume(a["full"])
            store = self.repo.store
            main = self.repo.head()
            local = (self._checkout(a).status() if a["mode"] == "folder" else ws.status())
            mine = {c.path for c in ws.changes_since_fork()} | set(local)
            upstream = set(changed_files(store, store.get_commit(ws.fork_base).tree, store.get_commit(main).tree))
            moved = main != ws.fork_base and not is_ancestor(store, main, ws.head)
            overlap = sorted(mine & upstream) if moved else []
            holds = self.claims.holds(a["full"], self.owner, a["epoch"])
            res = {"ok": True, "task": a["task"], "branch": f"ws/{a['full']}", "folder": a["folder"],
                   "changed": sorted(mine), "uncommitted": dict(sorted(local.items())),
                   "main": _short(main), "main_moved": moved, "changed_on_both_sides": overlap,
                   "conflict_markers_in": self._marked(a), "claim_held": holds}
            if not holds:
                res["next"] = "Your claim on this task was taken over. release_task and claim another."
            elif res["conflict_markers_in"]:
                res["next"] = "Resolve the conflict markers in those files, then submit_task."
            elif overlap:
                res["next"] = "Main changed files you also changed. Call sync_with_main before submit_task."
            else:
                res["next"] = "submit_task when you are done."
            return res

    # ---- catching up ---------------------------------------------------------------------

    def sync(self) -> dict:
        """Merge main into your branch (and folder). Conflicts are left as markers for you to resolve."""
        with self._lock:
            a = self._need_active()
            self._save_work(a, "work in progress")
            ws = self._session(a).ws if a["mode"] == "memory" else self.repo.resume(a["full"])
            store = self.repo.store
            main = self.repo.head()
            fork_tree = store.get_commit(ws.fork_base).tree
            mine = set(changed_files(store, fork_tree, store.get_commit(ws.head).tree))
            theirs = changed_files(store, fork_tree, store.get_commit(main).tree)
            if main == ws.fork_base or main in self._ancestors(ws.head):
                return {"ok": True, "status": "up-to-date", "next": "Nothing new on main. submit_task when done."}
            both = sorted(mine & set(theirs))
            diff = unified_all(store, fork_tree, store.get_commit(main).tree) if both else ""
            if is_ancestor(store, ws.head, main):
                ws._adopt(main, main)
                status, conflicts = "fast-forward", []
            else:
                task = self._task_of(a)
                policy = self.repo.policy_table(main, task.merge if task is not None and self.queue.policies else ())
                before_sync, base_commit = ws.head, merge_base(store, ws.head, main)
                merged = self.repo.merge_into(ws.head, main, f"ws/{a['full']}", "main", keep_going=True, policy=policy)
                tree = merged.tree
                cid = store.put_commit(Commit(tree, (ws.head, main), a["full"],
                                              f"Merge main into ws/{a['full']}" + (" (conflicts marked)" if merged.conflicts else ""),
                                              self.repo.clock()))
                ws._adopt(cid, main)
                conflicts = sorted(c.path for c in merged.conflicts)
                status = "merged-with-conflicts" if conflicts else "merged"
                if conflicts and base_commit is not None:
                    # remember what was merged, so that submit can check that the resolution kept both sides' lines
                    kvdoc.update(self.repo.kv, self._active_key, lambda d: d.update(
                        synced={"base": base_commit, "mine": before_sync, "main": main, "paths": conflicts}), {})
            if a["mode"] == "folder":
                pulled = self._checkout(a).pull()
                brought = pulled.updated
            else:
                brought = sorted(theirs)
                self._session(a).checkpoint(message="merged main")      # so that a restart keeps the merge
            res = {"ok": True, "status": status, "brought_in": sorted(brought), "changed_on_both_sides": both,
                   "conflicts": conflicts}
            if both:
                res["main_changes_to_those_files"] = diff[:CAP] + ("\n[... cut off]" if len(diff) > CAP else "")
            if conflicts:
                res["next"] = ("Resolve the conflict markers (<<<<<<<, =======, >>>>>>>) in: " + ", ".join(conflicts)
                               + ", keeping the intent of both sides, then submit_task.")
            elif both:
                res["next"] = ("Main and you both changed: " + ", ".join(both) + ". The merge was clean, but check that "
                               "your change still does what the task asks given main's changes (re-read those files; a "
                               "change that main already contains must not be assumed to be yours), then submit_task.")
            else:
                res["next"] = "Merged cleanly. submit_task when done."
            return res

    @staticmethod
    def _ancestors_of(store, commit):
        from ..history import ancestors
        return ancestors(store, commit)

    def _ancestors(self, commit: str):
        return self._ancestors_of(self.repo.store, commit)

    def _save_work(self, a: dict, message: str) -> None:
        """Get everything the agent has done onto its branch."""
        if a["mode"] == "folder":
            marked = self._marked(a)
            if marked:
                raise TrunkError("These files still contain conflict markers: " + ", ".join(marked)
                                 + ". Resolve them first.")
            pushed = self._checkout(a).push(message)
            if pushed.status in ("blocked", "conflict", "contended"):
                raise TrunkError(f"could not save your folder to the branch ({pushed.status}: {sorted(pushed.conflicts)}).")
        else:
            self._session(a).checkpoint(message=message)

    # ---- landing --------------------------------------------------------------------------

    def submit(self, message: Optional[str] = None) -> dict:
        with self._lock:
            a = self._need_active()
            if not self.claims.holds(a["full"], self.owner, a["epoch"]):
                raise TrunkError("Your claim on this task was taken over by another agent; release_task and claim another.")
            marked = self._marked(a)
            if marked:
                raise TrunkError("These files still contain conflict markers: " + ", ".join(marked)
                                 + ". Resolve them, then submit_task again.")
            text = message or f"{a['task']}"
            self._save_work(a, text)
            ws = self.repo.resume(a["full"])
            changed = [c.path for c in ws.changes_since_fork()]
            if not changed:
                return {"ok": False, "status": "nothing-to-land",
                        "next": "Your branch has no changes. Do the task (edit files), then submit_task; or release_task."}
            lost = self._lost_in_resolution(a, ws)
            if lost:
                return {"ok": False, "status": "rejected", "detail": lost,
                        "next": ("Your resolution of the conflicts dropped work: " + lost + "\nPut those lines back (the "
                                 "result must keep what both you and main added), then submit_task again.")}
            assume_read(self.repo, a["full"], changed)        # a file you changed is a file you depended on
            owner, epoch, full = self.owner, a["epoch"], a["full"]
            guard = lambda: None if self.claims.holds(full, owner, epoch) else fence.LOST        # noqa: E731
            try:
                with fence.fenced(guard), self.queue.for_task(self._task_of(a)):
                    r = self.queue.attempt(full, text)
            except fence.ClaimLost:
                raise TrunkError("Your claim on this task was taken over while landing; nothing was landed.")
            return self._landing_result(a, r, changed)

    def _lost_in_resolution(self, a: dict, ws) -> str:
        """After a sync that left conflict markers: did the resolution keep both sides' added lines?"""
        rec = a.get("synced")
        if not rec or not self.queue.survival:
            return ""
        try:
            return self.queue._survival(rec["base"], rec["mine"], rec["main"], ws.head, rec.get("paths"))
        except Exception:                                    # noqa: BLE001 - a missing commit must not block landing
            return ""

    def _landing_result(self, a: dict, r, changed: List[str]) -> dict:
        if r.landed or r.status == "noop":
            self.claims.complete(a["full"], self.owner, a["epoch"], {"status": "done", "reason": "", "commit": r.commit})
            self.leases.release(a["full"])
            self._sessions.pop(a["full"], None)
            self.repo.kv.put(self._active_key, "{}")
            if a["mode"] == "folder":
                try:
                    self._checkout(a).pull()
                except Exception:                                  # the folder is a convenience; the landing is done
                    pass
            return {"ok": True, "status": "landed" if r.landed else "already-landed", "commit": _short(r.commit),
                    "via": r.status, "files": sorted(changed), "main": _short(self.repo.head()),
                    "next": "Done. claim_task takes the next task."}
        res: dict = {"ok": False, "status": r.status}
        if r.status == "stale":
            paths = [s.path for s in r.stale_reads]
            res.update(stale_paths=paths, next=(
                "Main changed files you depend on: " + ", ".join(paths) + ". Call sync_with_main, read what main "
                "changed, adjust your work if needed, then submit_task again."))
        elif r.status == "conflict":
            paths = [c.path for c in r.conflicts]
            res.update(conflicts=[[c.path, c.kind] for c in r.conflicts], next=(
                "Your changes conflict with main in: " + ", ".join(paths) + ". Call sync_with_main to get the "
                "conflicts as markers, resolve them, then submit_task again."))
        elif r.status == "rejected":
            res.update(detail=r.detail, next=("The merged result failed validation:\n" + r.detail[-1500:]
                                              + "\nFix it (sync_with_main first if main moved), then submit_task again."))
        elif r.status == "contended":
            res["next"] = "main is changing quickly; submit_task again."
        else:
            res["next"] = f"Landing returned {r.status!r}."
        return res

    # ---- the people side -----------------------------------------------------------------

    def escalations(self) -> dict:
        items = Escalations(self.repo.kv).list()
        return {"ok": True, "count": len(items), "items": items[-20:]}

    # ---- main, for resources --------------------------------------------------------------

    def main_files(self) -> List[str]:
        return sorted(self.repo.main_files())

    def read_main(self, path: str) -> str:
        files = self.repo.main_files()
        if path not in files:
            raise TrunkError(f"no file {path!r} on main")
        return files[path].decode("utf-8", "replace")

    # ---- memory mode: file tools over the branch ----------------------------------------

    FILE_TOOLS = ("list_files", "read_file", "write_file", "replace_in_file", "delete_file", "move_file")

    def file_tool(self, name: str, arguments: dict) -> dict:
        if not self.file_tools:
            raise TrunkError("file tools are off: the branch is a folder, edit it directly (start the server with "
                             "--file-tools for clients without a file system).")
        with self._lock:
            a = self._need_active()
            s = self._session(a)
            args = dict(arguments)
            seq = args.pop("seq", None)
            if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int)):
                raise TrunkError("seq must be an integer")
            out = s.call(seq if seq is not None else s.next_seq(), name, args)
            if s.since_checkpoint >= CHECKPOINT_EVERY:
                s.checkpoint()
            return {"ok": out.ok, "result": out.text, "seq": s.seq}
