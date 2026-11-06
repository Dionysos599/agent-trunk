"""Workers for the evaluation, over a workspace (fork + merge) or a plain shared folder (naive).

``DirFS`` is the file system of the naive strategy: one real folder that every agent reads and writes
directly, nothing isolated, nothing merged, the last writer of a file wins. It offers the same small
interface as ``Workspace`` so that the same agents can work on either.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cmws_ws.agents import ChatModel, ModelError, Task, Toolbox, WorkerResult
from cmws_ws.agents.worker import SYSTEM, task_prompt

REDO = re.compile(r"-redo\d+$")


def base_id(agent_id: str) -> str:
    """'run-t1' and 'run-t1-redo2' are both the task t1."""
    return REDO.sub("", agent_id).split("-", 1)[-1]


class DirFS:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _p(self, path: str) -> Path:
        return self.root / path

    def exists(self, path: str) -> bool:
        return self._p(path).exists()

    def read_text(self, path: str) -> str:
        return self._p(path).read_text()

    def write(self, path: str, data) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{threading.get_ident()}.tmp")       # replaced in one step, so a reader never sees a
        if isinstance(data, bytes):                                         # half-written file (that would be a crash of the
            tmp.write_bytes(data)                                           # harness, not the lost update being measured)
        else:
            tmp.write_text(data)
        os.replace(tmp, p)

    def delete(self, path: str) -> None:
        p = self._p(path)
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    def move(self, src: str, dst: str) -> None:
        self._p(dst).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._p(src), self._p(dst))

    def list(self, directory: str = "") -> List[Tuple[str, str]]:
        d = self._p(directory)
        if not d.is_dir():
            raise FileNotFoundError(directory)
        return sorted((e.name, "dir" if e.is_dir() else "file") for e in d.iterdir())

    def files(self) -> Dict[str, str]:
        out = {}
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                p = Path(dirpath) / n
                out[p.relative_to(self.root).as_posix()] = p.read_text()
        return out


class OracleWorker:
    """A perfect agent that takes its time: it reads, waits, then writes what it computed from what it read.

    The wait is where other agents get their work done, so the read goes stale exactly as it would for a
    model that thinks for a few seconds between reading a file and rewriting it.
    """
    name = "oracle"

    def __init__(self, edits: Dict[str, Callable], latency: float = 0.3, seed: int = 0, untracked: bool = False,
                 assume_reads: bool = False) -> None:
        self.edits, self.latency, self.untracked = edits, latency, untracked
        self.assume_reads = assume_reads
        self._rng, self._lock = random.Random(seed), threading.Lock()

    def _pause(self) -> None:
        with self._lock:
            t = self.latency * (0.5 + self._rng.random())
        time.sleep(t)

    def run(self, task: Task, repo, agent_id: str) -> WorkerResult:
        ws = repo.resume(agent_id)
        self._apply(base_id(agent_id), ws)
        touched = tuple(sorted(ws.status()))
        if self.untracked and self.assume_reads:               # what CliWorker does for a folder-based agent
            from cmws_ws.agents.worker import assume_read
            ws.commit(f"{agent_id}")
            assume_read(repo, agent_id, touched)
            return WorkerResult("done", f"{agent_id} done", repo.resume(agent_id).head, touched)
        return WorkerResult("done", f"{agent_id} done", ws.commit(f"{agent_id}"), touched)

    def run_on(self, task: Task, fs: DirFS) -> WorkerResult:
        self._apply(base_id(task.id), fs)
        return WorkerResult("done", f"{task.id} done")

    def _apply(self, tid: str, fs) -> None:
        edit = self.edits[tid]

        class Delayed:                                   # reads happen now, the write after thinking
            def __init__(s):
                s.pending = []

            def read_text(s, p):
                if self.untracked and hasattr(fs, "_peek"):        # like an agent working in a checkout:
                    return fs._peek(p).decode()                    # its reads are not on record
                return fs.read_text(p)

            def exists(s, p):
                return fs.exists(p)

            def write(s, p, d):
                s.pending.append((p, d))
        view = Delayed()
        edit(view)
        self._pause()
        for p, d in view.pending:
            fs.write(p, d)


def loop_on(model: ChatModel, fs: DirFS, task: Task, max_steps: int = 20, system: str = SYSTEM) -> WorkerResult:
    """The same tool loop as ``LoopWorker``, over a shared folder and without a branch to commit to."""
    tools = Toolbox(fs)                                  # type: ignore[arg-type]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task_prompt(task)}]
    result = WorkerResult("gave_up")
    nudges = 0
    for step in range(1, max_steps + 1):
        try:
            reply = model.chat(messages, tools.specs())
        except ModelError as e:
            result.status, result.error = "error", str(e)
            break
        result.steps = step
        result.prompt_tokens += reply.usage.prompt_tokens
        result.completion_tokens += reply.usage.completion_tokens
        assistant: dict = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            import json
            assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                "name": c.name, "arguments": json.dumps(c.arguments)}} for c in reply.tool_calls]
        messages.append(assistant)
        if not reply.tool_calls:
            nudges += 1
            if nudges > 2:
                break
            messages.append({"role": "user", "content": "Use the tools to make the change, and call finish when done."})
            continue
        nudges = 0
        done = False
        for c in reply.tool_calls:
            out = tools.call(c.name, c.arguments)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out.text})
            done = done or out.finished
        if done:
            result.status = "done"
            break
    else:
        result.status = "step_limit"
    result.touched = tuple(sorted(tools.written))
    return result


def cli_in_dir(argv: Sequence[str], task: Task, root: Path, timeout: float = 900.0) -> WorkerResult:
    """Run a command-line agent directly in the shared folder."""
    prompt = task_prompt(task)
    args = [a.replace("{prompt}", prompt) for a in argv]
    try:
        proc = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return WorkerResult("error", error=str(e))
    if proc.returncode != 0:
        return WorkerResult("error", error=((proc.stderr or "") + (proc.stdout or "")).strip()[-300:])
    return WorkerResult("done", summary=(proc.stdout or "").strip()[-300:])


class SloppyMerger:
    """A merger that settles every conflict by keeping the first side only (the failure seen with a small model).

    It produces files without markers that still import, so only a check for dropped work can notice.
    """
    name = "sloppy-merger"

    def run(self, task: Task, repo, agent_id: str) -> WorkerResult:
        ws = repo.resume(agent_id)
        for path, data in ws.files().items():
            try:
                text = data.decode()
            except UnicodeDecodeError:
                continue
            if "<<<<<<<" in text:
                out, keep = [], True
                for line in text.splitlines(keepends=True):
                    if line.startswith("<<<<<<<"):
                        continue
                    if line.startswith("======="):
                        keep = False
                        continue
                    if line.startswith(">>>>>>>"):
                        keep = True
                        continue
                    if keep:
                        out.append(line)
                ws.write(path, "".join(out))
        return WorkerResult("done", "kept one side", ws.commit(f"{agent_id}: resolved"))
