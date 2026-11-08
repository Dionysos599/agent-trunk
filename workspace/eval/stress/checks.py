"""What "correct" means for a stress run, decided by programs only (nothing is asked of a model).

A run is **correct** only if every one of these holds:

* the final tree equals the serial replay of the landed tasks in landing order (``serial.check_serializable``);
* every written line is present exactly once, every counter and the version equal their exact totals, and no task landed twice;
* every state ``main`` ever held satisfied the invariant, **checked after the run** over every commit reachable from ``main``
  (not from a list the queue kept in memory, which would not survive a killed process);
* the landing commits form one chain by ancestry, and every task's claim ended completed as done.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Sequence

from cmws_ws.claims import TaskClaims
from cmws_ws.history import ancestors
from cmws_ws.serial import check_serializable

from . import workload as W

SUFFIX = re.compile(r"-(redo\d+|hedge)$")


def task_of(run_id: str):
    prefix = run_id + "-"

    def fn(author: str) -> Optional[str]:
        return SUFFIX.sub("", author)[len(prefix):] if author.startswith(prefix) else None
    return fn


def count_lines(files: Dict[str, bytes]) -> Counter:
    c: Counter = Counter()
    for path, data in files.items():
        if path.startswith(("own/", "shared/")):
            c.update(ln for ln in data.decode("utf-8", "replace").splitlines() if ln.startswith("tok "))
    return c


def parse_counters(files: Dict[str, bytes]) -> dict:
    text = files.get(W.COUNTER_FILE, b"").decode("utf-8", "replace")
    vals = {k: int(v) for k, v in re.findall(r"^(C\d+|TOTAL) = (\d+)$", text, re.M)}
    ver = re.search(r"^VERSION = (\d+)$", files.get(W.VERSION_FILE, b"").decode("utf-8", "replace"), re.M)
    return {"counters": {int(k[1:]): v for k, v in vals.items() if k != "TOTAL"}, "total": vals.get("TOTAL"),
            "version": int(ver.group(1)) if ver else None}


def edits_lost(files: Dict[str, bytes], tasks: Sequence[W.StressTask], landed: Optional[set] = None) -> dict:
    """Lost and duplicated edits, each written line counted once and each missing counter increment once.

    With ``landed`` (the tasks the system reported done) the missing edits are split in two: **silent** losses belong to a task
    that was reported done (the dangerous kind: nothing failed) and **pending** ones belong to a task that did not land (it was
    handed to a person; nothing was lost, the work is not finished)."""
    landed = {t.id for t in tasks} if landed is None else landed
    exp = W.expected(tasks)
    seen = count_lines(files)
    owner = {tok: t.id for t in tasks for tok in t.tokens}
    missing_toks = [t for t in exp["tokens"] if seen[t] == 0]
    twice = sum(seen[t] - 1 for t in exp["tokens"] if seen[t] > 1)
    stranger = sum(1 for t in seen if t not in owner)
    got = parse_counters(files)
    # increments: what the landed counter tasks promised, against what the file holds
    promised = {k: sum(t.increments.get(k, 0) for t in tasks if t.kind == "counter" and t.id in landed) for k in range(W.COUNTERS)}
    all_inc = exp["counters"]
    lost_incr = sum(max(0, all_inc[k] - got["counters"].get(k, 0)) for k in all_inc)
    over_incr = sum(max(0, got["counters"].get(k, 0) - all_inc[k]) for k in all_inc)
    silent_incr = sum(max(0, promised[k] - got["counters"].get(k, 0)) for k in all_inc)
    versions_landed = sum(1 for t in tasks if t.kind == "version" and t.id in landed)
    lost_ver = max(0, exp["version"] - (got["version"] or 0))
    over_ver = max(0, (got["version"] or 0) - exp["version"])
    silent_ver = max(0, versions_landed - (got["version"] or 0))
    silent_lines = sum(1 for t in missing_toks if owner[t] in landed)
    return {"lines_missing": len(missing_toks), "lines_twice": twice, "lines_unknown": stranger, "increments_lost": lost_incr,
            "increments_extra": over_incr, "versions_lost": lost_ver, "versions_extra": over_ver,
            "edits_lost": len(missing_toks) + lost_incr + lost_ver, "edits_duplicated": twice + over_incr + over_ver,
            "edits_lost_silent": silent_lines + silent_incr + silent_ver,
            "edits_pending": (len(missing_toks) - silent_lines) + (lost_incr - silent_incr) + (lost_ver - silent_ver)}


def invalid_states(repo, limit: Optional[int] = None) -> List[str]:
    """Commits reachable from main whose tree breaks the invariant (all of them: the check does not trust any list)."""
    bad = []
    for cid in sorted(ancestors(repo.store, repo.head())):
        problem = W.invariant(repo.files_at(cid))
        if problem:
            bad.append(f"{cid[:10]}: {problem}")
            if limit and len(bad) >= limit:
                break
    return bad


def claims_state(kv, run_id: str, tasks: Sequence[W.StressTask]) -> dict:
    snap = TaskClaims(kv, ttl=1e9, prefix=f"runs/{run_id}/").snapshot()
    done = sum(1 for t in tasks if snap.get(f"{run_id}-{t.id}", {}).get("state") == "completed"
               and snap[f"{run_id}-{t.id}"]["outcome"].get("status") == "done")
    bad = {t.id: snap.get(f"{run_id}-{t.id}", {}).get("outcome") or snap.get(f"{run_id}-{t.id}", {}).get("state")
           for t in tasks if not (snap.get(f"{run_id}-{t.id}", {}).get("state") == "completed"
                                  and snap[f"{run_id}-{t.id}"]["outcome"].get("status") == "done")}
    return {"claims_done": done, "claims_expected": len(tasks), "claims_not_done": dict(list(bad.items())[:5])}


def verdict(repo, kv, run_id: str, tasks: Sequence[W.StressTask], initial: Dict[str, str], serial: bool = True,
            landed: Optional[set] = None) -> dict:
    files = repo.main_files()
    out = edits_lost(files, tasks, landed)
    out["invariant_violations"] = invalid_states(repo, limit=5)
    out.update(claims_state(kv, run_id, tasks))
    if serial:
        edits = {t.id: t.apply for t in tasks}
        v = check_serializable(repo, initial, edits, task_of(run_id), search=False)
        out.update(serial_ok=v.ok, serial_error=v.error, serial_landed=len(v.order), chain=v.linear,
                   serial_differing=v.differing[:5])
    else:
        out.update(serial_ok=None, serial_landed=None, chain=None)
    out["state_invariant_ok"] = not out["invariant_violations"]
    out["correct"] = bool(
        out["edits_lost"] == 0 and out["edits_duplicated"] == 0 and out["lines_unknown"] == 0
        and out["state_invariant_ok"] and out["claims_done"] == out["claims_expected"]
        and (out["serial_ok"] is not False) and (out["chain"] is not False))
    return out
