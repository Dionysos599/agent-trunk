"""Replay the merge history of a real git repository: how often do the cases that matter occur?

    python3 workspace/eval/replay.py --repo PATH [--limit 500] [--out rows.jsonl] [--table]

For every two-parent merge commit the two sides are the concurrent work: everything each parent changed since the merge
base. (That is the closest thing a local clone has to "two pull requests that were open at the same time"; a clone does not
record pull requests.) Each pair is classified with this project's own text merge, using only the repository's history,
no model and no cost:

  disjoint            the sides changed different files: nothing could collide
  same-file-clean     both changed a file, the line-level merge is clean (git accepts it)
  identical-edit      both made *the same* change to a file (git takes it as agreement; for a counter or a version
                      number that is the silent lost update)
  structural          modify/delete, add/add or binary on both sides
  conflict            overlapping text edits: git stops and a person resolves

and four counters that say what each way of coordinating would have done:

  git_stops               conflict or structural: a person is needed whatever the tool
  readset_would_refuse    both changed a file, so a landing that treats every changed file as a read (folder agents) would
                          have been refused as stale and redone, although git accepted it
  append_would_resolve    conflicts that are only both sides inserting lines (what the `append` policy resolves)
  human_edited            the committed merge differs from the clean text merge: the person changed something the merge
                          did not (a sign that the textually clean merge was not what was meant)

What it cannot say: whether a clean merge *passed the repository's tests* (that needs a checkout and the project's command);
pairs are merge commits, so work that was rebased or squashed before landing is not seen.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src")]

from cmws_ws.textmerge import merge_text          # noqa: E402

MAX_FILES = 300
MAX_BYTES = 1_000_000


def git(repo: Path, *args: str, check: bool = True) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace')[-200:]}")
    return p.stdout


def merge_commits(repo: Path, limit: int) -> List[Tuple[str, str, str]]:
    out = git(repo, "log", "--merges", "--format=%H %P", f"--max-count={limit}").decode().splitlines()
    rows = []
    for line in out:
        parts = line.split()
        if len(parts) == 3:                                   # exactly two parents; octopus merges are left out
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def changed(repo: Path, base: str, tip: str) -> Dict[str, str]:
    out = git(repo, "diff", "--name-status", "--no-renames", "-z", base, tip).split(b"\0")
    res, i = {}, 0
    while i + 1 < len(out):
        status, path = out[i].decode(), out[i + 1].decode(errors="replace")
        res[path] = status[0]
        i += 2
    return res


def blob(repo: Path, rev: str, path: str) -> Optional[bytes]:
    p = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def text(data: Optional[bytes]) -> Optional[str]:
    if data is None or len(data) > MAX_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def classify(repo: Path, merge: Tuple[str, str, str]) -> dict:
    sha, p1, p2 = merge
    base = git(repo, "merge-base", p1, p2, check=False).decode().strip()
    row = {"merge": sha[:12], "class": "disjoint", "files_a": 0, "files_b": 0, "common": 0, "identical": 0, "conflicts": 0,
           "structural": 0, "append_resolvable": 0, "human_edited": 0, "clean_same_file": 0}
    if not base:
        row["class"] = "unrelated"
        return row
    a, b = changed(repo, base, p1), changed(repo, base, p2)
    common = sorted(set(a) & set(b))
    row.update(files_a=len(a), files_b=len(b), common=len(common))
    for path in common[:MAX_FILES]:
        sa, sb = a[path], b[path]
        ta, tb = text(blob(repo, p1, path)), text(blob(repo, p2, path))
        tbase = text(blob(repo, base, path)) if sa != "A" and sb != "A" else ""
        if sa == "D" or sb == "D" or ta is None or tb is None or tbase is None:
            row["structural"] += 1
            continue
        if ta == tb:
            row["identical"] += 1
            continue
        r = merge_text(tbase, ta, tb)
        if r.clean:
            row["clean_same_file"] += 1
            committed = text(blob(repo, sha, path))
            if committed is not None and committed != r.text:
                row["human_edited"] += 1
        else:
            row["conflicts"] += 1
            if merge_text(tbase, ta, tb, append=True).clean:
                row["append_resolvable"] += 1
    if row["conflicts"] or row["structural"]:
        row["class"] = "conflict" if row["conflicts"] else "structural"
    elif row["identical"]:
        row["class"] = "identical-edit"
    elif row["clean_same_file"]:
        row["class"] = "same-file-clean"
    return row


def summarize(rows: List[dict]) -> dict:
    n = len(rows)
    classes = Counter(r["class"] for r in rows)
    conflict_pairs = [r for r in rows if r["conflicts"]]
    return {
        "pairs": n, "classes": dict(classes),
        "git_stops": sum(1 for r in rows if r["class"] in ("conflict", "structural")),
        "readset_would_refuse": sum(1 for r in rows if r["common"] > 0 and r["class"] in ("same-file-clean", "identical-edit")),
        "identical_edit_pairs": classes.get("identical-edit", 0),
        "append_would_resolve": sum(1 for r in conflict_pairs if r["append_resolvable"] == r["conflicts"]),
        "conflict_pairs": len(conflict_pairs),
        "human_edited_pairs": sum(1 for r in rows if r["human_edited"]),
        "clean_same_file_pairs": sum(1 for r in rows if r["clean_same_file"]),
    }


def table(summary: dict, name: str = "") -> str:
    n = max(summary["pairs"], 1)
    pct = lambda k: f"{100 * k / n:.0f}%"           # noqa: E731
    c = summary["classes"]
    rows = [("pairs of concurrent work (merge commits)", summary["pairs"], ""),
            ("disjoint files: nothing could collide", c.get("disjoint", 0), pct(c.get("disjoint", 0))),
            ("same file, text merge clean", c.get("same-file-clean", 0), pct(c.get("same-file-clean", 0))),
            ("same change made on both sides (identical edit)", c.get("identical-edit", 0), pct(c.get("identical-edit", 0))),
            ("textual conflict", c.get("conflict", 0), pct(c.get("conflict", 0))),
            ("modify/delete, add/add or binary", c.get("structural", 0), pct(c.get("structural", 0))),
            ("-> git needs a person", summary["git_stops"], pct(summary["git_stops"])),
            ("-> a folder agent's read set would refuse (git accepted)", summary["readset_would_refuse"], pct(summary["readset_would_refuse"])),
            ("-> conflicts that `append` alone would resolve", summary["append_would_resolve"], f"of {summary['conflict_pairs']} conflicting pairs"),
            ("-> the committed merge differs from the clean text merge", summary["human_edited_pairs"], pct(summary["human_edited_pairs"]))]
    head = f"| {name or 'repository'} | pairs | share |\n|---|---|---|\n"
    return head + "\n".join(f"| {a} | {b} | {c_} |" for a, b, c_ in rows[1:]) + f"\n\n({summary['pairs']} merge commits)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--table", action="store_true")
    a = ap.parse_args(argv)
    rows = []
    sink = a.out.open("a") if a.out else None
    try:
        _replay(a, rows, sink)
    finally:
        if sink:
            sink.close()
    s = summarize([r for r in rows if r["class"] != "error"])
    print(json.dumps(s))
    if a.table:
        print(table(s, a.repo.name))
    return 0


def _replay(a, rows, sink) -> None:
    for m in merge_commits(a.repo, a.limit):
        try:
            r = classify(a.repo, m)
        except RuntimeError as e:
            r = {"merge": m[0][:12], "class": "error", "error": str(e)[:100], "common": 0, "conflicts": 0, "identical": 0,
                 "structural": 0, "append_resolvable": 0, "human_edited": 0, "clean_same_file": 0}
        r["repo"] = a.repo.name
        rows.append(r)
        if sink:
            sink.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    sys.exit(main())
