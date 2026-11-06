"""Merge policies: how a file's concurrent edits combine (doc 12, section 2.3).

Some edits are agreement when they are identical (a semicolon, a rename), some are not (a counter, a version
number), some commute (appended lines). Text cannot tell them apart, so the policy is *declared*, and the safe one
is the default.

``relative``  (default) the edit depends on what the agent read: stale-read validation applies in full.
``converge``  identical edits are agreement: reads of this file are not validated.
``append``    pure insertions at the same place commute: both sides' lines are kept, the side already on main first.

A policy only ever *relaxes* a check the default would have made, and only for the files that match; a file that
matches no rule is ``relative``. Rules come from two places, the later one winning:

1. a file named ``.agenttrunk-merge`` at the project root, read from the **main** being merged into (an agent
   cannot relax checks on its own landing by editing it), lines ``pattern policy``, ``#`` for comments;
2. the task's own declaration, ``"merge": {"stats.py": "relative"}``.

A pattern without a ``/`` matches the file name at any depth (``*.md``); a pattern with a ``/`` matches the whole
path from the project root (a leading ``/`` is allowed and ignored). ``*`` and ``?`` follow ``fnmatch`` and ``*``
also crosses ``/``. A bad line in the file is skipped (and reported by ``parse_rules``): a typo must never turn a
check off, so the fallback is always ``relative``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

from .objects import FILE, ObjectError

RELATIVE, CONVERGE, APPEND = "relative", "converge", "append"
POLICIES = (RELATIVE, CONVERGE, APPEND)
FILE_NAME = ".agenttrunk-merge"
MAX_FILE_BYTES = 64 * 1024
MAX_RULES = 500

Rule = Tuple[str, str]                 # (pattern, policy)


class PolicyError(ValueError):
    """A merge declaration that cannot be used (unknown policy, empty pattern, wrong type)."""


def _clean_pattern(pattern: object) -> str:
    if not isinstance(pattern, str):
        raise PolicyError(f"merge pattern must be text, got {type(pattern).__name__}")
    p = pattern.strip()
    if p.startswith("/"):
        p = p.lstrip("/")
    if not p or any(part == ".." for part in p.split("/")) or "\x00" in p or "\n" in p:
        raise PolicyError(f"bad merge pattern {pattern!r}")
    return p


def validate_rule(pattern: object, policy: object) -> Rule:
    if not isinstance(policy, str) or policy not in POLICIES:
        raise PolicyError(f"unknown merge policy {policy!r} for {pattern!r}; use one of {', '.join(POLICIES)}")
    return _clean_pattern(pattern), policy


def rules_from_declaration(decl: Optional[Mapping[str, object]]) -> Tuple[Rule, ...]:
    """The rules of a task's ``"merge"`` mapping, validated (raises ``PolicyError``)."""
    if decl is None:
        return ()
    if not isinstance(decl, Mapping):
        raise PolicyError("'merge' must be an object mapping a path or pattern to a policy")
    if len(decl) > MAX_RULES:
        raise PolicyError(f"too many merge rules ({len(decl)}; at most {MAX_RULES})")
    return tuple(validate_rule(k, v) for k, v in decl.items())


def rules_to_declaration(rules: Iterable[Rule]) -> dict:
    return {p: pol for p, pol in rules}


def parse_rules(text: str) -> Tuple[List[Rule], List[str]]:
    """Parse the text of a ``.agenttrunk-merge`` file into (rules, problems). Bad lines are skipped, never fatal."""
    rules: List[Rule] = []
    problems: List[str] = []
    if text.startswith("﻿"):
        text = text[1:]
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.rsplit(None, 1)
        if len(parts) != 2:
            problems.append(f"line {n}: expected 'pattern policy', got {s!r}")
            continue
        try:
            rule = validate_rule(parts[0], parts[1])
        except PolicyError as e:
            problems.append(f"line {n}: {e}")
            continue
        if len(rules) >= MAX_RULES:
            problems.append(f"line {n}: more than {MAX_RULES} rules, the rest are ignored")
            break
        rules.append(rule)
    return rules, problems


def matches(pattern: str, path: str) -> bool:
    if "/" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)


class PolicyTable:
    """The rules in force for one landing. ``policy_for(path)`` is the last matching rule's policy, else ``relative``."""

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self.rules: Tuple[Rule, ...] = tuple(rules)

    def policy_for(self, path: str) -> str:
        for pattern, policy in reversed(self.rules):
            if matches(pattern, path):
                return policy
        return RELATIVE

    def __bool__(self) -> bool:
        return any(p != RELATIVE for _, p in self.rules)

    def __repr__(self) -> str:
        return f"PolicyTable({list(self.rules)!r})"

    @classmethod
    def for_tree(cls, store, tree: Optional[str], task_rules: Sequence[Rule] = ()) -> "PolicyTable":
        """The project's file as found in ``tree`` (normally main) followed by the task's own rules."""
        return cls(tuple(project_rules(store, tree)) + tuple(task_rules))


def project_rules(store, tree: Optional[str]) -> List[Rule]:
    """Rules from ``.agenttrunk-merge`` in ``tree``; an unreadable file means no rules (the safe default)."""
    if tree is None:
        return []
    try:
        entry = store.lookup(tree, FILE_NAME)
        if entry is None or entry.mode != FILE:
            return []
        data = store.get_blob(entry.id)
        if len(data) > MAX_FILE_BYTES:
            return []
        rules, _ = parse_rules(data.decode("utf-8"))
        return rules
    except (UnicodeDecodeError, ObjectError, KeyError, ValueError):
        return []


PolicyFn = Callable[[str], str]
