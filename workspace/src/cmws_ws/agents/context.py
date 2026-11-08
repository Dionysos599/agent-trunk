"""Shared context: the project map is computed once per state of ``main`` and handed to every agent.

Each agent that starts on a project reads the same directory listing and the same first lines of the same files before it
does anything of its own. The map costs the model a few hundred tokens once and the store one small document, instead of
every agent spending tool calls on it. It is a *map* (paths, sizes, a first line, the top-level names of a Python file), not
the contents; an agent still reads what it will change.

The map is cached under the id of ``main``'s tree, so a hundred agents starting on the same state share one computation
and a changed project gets a new one. Whether it saves tokens is a measurement (``docs/eval/results.md``), not a claim.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..repo import Repository

TOP_LEVEL = re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
MAX_FILES = 300


def _describe(path: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"{path}  ({len(data)} bytes, binary)"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = f"{path}  ({len(text.splitlines())} lines)"
    names = TOP_LEVEL.findall(text)[:6]
    if names:
        line += "  defines " + ", ".join(names)
    elif first:
        line += f"  starts: {first[:70]}"
    return line


def build_map(files: Dict[str, bytes], max_chars: int = 3000, max_files: int = MAX_FILES) -> str:
    rows: List[str] = []
    paths = sorted(files)
    for path in paths[:max_files]:
        rows.append(_describe(path, files[path]))
    out = ["Project files (a map, not their contents; read what you will change):"]
    used = len(out[0])
    for i, row in enumerate(rows):
        if used + len(row) + 1 > max_chars:
            out.append(f"... and {len(paths) - i} more files")
            break
        out.append(row)
        used += len(row) + 1
    else:
        if len(paths) > max_files:
            out.append(f"... and {len(paths) - max_files} more files")
    return "\n".join(out)


def shared_context(repo: Repository, max_chars: int = 3000, commit: Optional[str] = None) -> str:
    """The map of ``main`` (or ``commit``), computed once and kept in the store under the tree id."""
    commit = commit or repo.head()
    tree = repo.store.get_commit(commit).tree
    key = f"ctx/{tree}/{max_chars}"
    cached = repo.kv.get(key)
    if cached is not None:
        return cached
    text = build_map(repo.files_at(commit), max_chars)
    repo.kv.put_if_absent(key, text)
    return repo.kv.get(key) or text
