"""Recorded costs of real models, to be replayed by scripted agents.

A trace holds, per model, samples of what the real runs cost: the seconds and tokens of one task, and the tokens of settling one
collision at each layer of the ladder. They are summarised from the recorded evaluation rows (``eval/results/*.jsonl``): a run of
independent tasks gives the time and tokens of a task; the ladder runs give the tokens per collision at layer 2 and per redo
(layer 4). Seconds for a layer are derived from its tokens at the model's own measured speed.

``Trace.load`` returns an object whose ``task_latency``, ``task_tokens`` and ``layer2_tokens`` draw from those samples with a
seeded generator, so a replay is repeatable. ``compress`` multiplies every duration (real tasks take tens of seconds; a 32-wide
run of 160 of them would take an hour), and the coordination itself is *not* compressed, which makes the replayed speedups
conservative: relative to the work, the overhead looks larger than it really is.
"""

from __future__ import annotations

import json
import random
import statistics as st
import threading
import hashlib
from pathlib import Path
from typing import Dict, List, Optional

RESULTS = Path(__file__).resolve().parent.parent / "results"


def _rows(name: str) -> List[dict]:
    p = RESULTS / name
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _ladder(r: dict, n: int, key: str) -> float:
    d = r.get("ladder") or {}
    d = d.get(str(n), d.get(n)) or {}
    return d.get(key, 0)


def build(model: str) -> dict:
    """Summarise the recorded rows of ``model`` ('eco' or 'quality') into samples. Empty lists when nothing was recorded."""
    tag = model
    levels = [r for r in _rows(f"ollama_{model}_levels.jsonl") if r.get("suite") == "disjoint"] if model == "eco" else []
    base = [r for r in _rows("ollama_eco.jsonl" if model == "eco" else "ollama_quality.jsonl") if r.get("suite") == "disjoint"]
    ladder = [r for r in _rows("ollama_ladder.jsonl") if r.get("tag", "").startswith(model + "-")]
    # one task: a run of 4 independent tasks in parallel takes about one task's time and 4 tasks' tokens
    task = [{"seconds": r["seconds"], "tokens": r["tokens"] / max(1, r["tasks"])} for r in (base or levels) if r.get("tokens")]
    if task:                                              # the first run after loading a model includes the loading time
        med = st.median(t["seconds"] for t in task)
        task = [t for t in task if t["seconds"] <= 3 * med]
    l2 = [_ladder(r, 2, "tokens") / _ladder(r, 2, "tried") for r in ladder if _ladder(r, 2, "tried") and "layer2" in r["tag"]]
    l4 = [_ladder(r, 4, "tokens") / _ladder(r, 4, "tried") for r in ladder if _ladder(r, 4, "tried") and r["tag"].endswith("redo")]
    return {"model": model, "task": task, "layer2_tokens": l2, "redo_tokens": l4,
            "tokens_per_second": (st.mean(t["tokens"] / t["seconds"] for t in task) if task else 0.0)}


def write(path: Path) -> dict:
    out = {m: build(m) for m in ("eco", "quality")}
    path.write_text(json.dumps(out, indent=1))
    return out


class Trace:
    def __init__(self, data: dict, seed: int = 0, compress: float = 1.0) -> None:
        if not data.get("task"):
            raise ValueError(f"the trace for {data.get('model')!r} has no recorded tasks")
        self.d, self.compress, self.seed = data, compress, seed
        self.rng, self.lock = random.Random(seed), threading.Lock()

    @classmethod
    def load(cls, path: str, model: str, seed: int = 0, compress: float = 1.0) -> "Trace":
        return cls(json.loads(Path(path).read_text())[model], seed, compress)

    def _pick(self, items):
        with self.lock:
            return self.rng.choice(items)

    def _for_task(self, task) -> dict:
        """A task always gets the same sample (in every process, every run with this seed), so that a single agent and 32 agents
        do exactly the same work and any difference in tokens is the coordination's."""
        key = int.from_bytes(hashlib.sha256(f"{self.seed}-{getattr(task, 'id', task)}".encode()).digest()[:8], "big")
        return self.d["task"][key % len(self.d["task"])]

    def task_latency(self, task) -> float:
        return self._for_task(task)["seconds"] * self.compress

    def task_tokens(self, task) -> int:
        return int(self._for_task(task)["tokens"])

    def layer2_tokens(self):
        t = self._pick(self.d["layer2_tokens"] or [0.0])
        return int(t * 0.9), int(t * 0.1)

    def mean_task_tokens(self) -> float:
        return st.mean(t["tokens"] for t in self.d["task"])
