"""The cost ladder below the automatic merge (doc 12, section 2.5).

When a branch cannot land as it stands the queue goes up by cost and never accepts without evidence:

* layer 2, **re-apply and compare** (this module): for a file the agent changed and main changed too, ask what the
  file would be if the agent carried out its task again on the current main, and let the *program* compare the answer
  with the file on main. Equal means agreement (the change is already there); different means the answer is the
  resolution. It costs one short exchange per file instead of a whole task, and it deliberately never asks "is your
  goal met?", which small models answer "yes" to the identical-edit counter almost every time (``confirm_probe.py``);
* layer 3, **continue the agent's own conversation** (``LoopWorker.resume``): the branch is merged with main and the
  agent gets one message with what main changed, instead of starting again;
* layer 4 is the redo of 1.0; layer 5 is a person.

Everything a layer produces still goes through the whole landing check (merge, survival, transition checks, the
validator), so a wrong answer costs a refusal, not a silent loss. Layers 2 and 3 are off unless configured.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from ..textmerge import hunks, split_lines
from .llm import ChatModel, ModelError

MAX_CHARS = 8000          # a file larger than this is not re-applied (the answer would be the whole file)
MAX_DIFF = 4000
SLACK = 5                 # lines the answer may differ from main by, however small the agent's own change was
FACTOR = 3                # ... or this many times the size of the agent's own change


@dataclass(frozen=True)
class Reapplied:
    text: Optional[str]                  # the file as the agent says it would be now; None when there was no usable answer
    prompt_tokens: int = 0
    completion_tokens: int = 0
    note: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Reapplier(Protocol):
    def reapply(self, task, path: str, base: str, mine: str, main: str) -> Reapplied: ...


def build_prompt(task, path: str, base: str, mine: str, main: str, max_diff: int = MAX_DIFF) -> str:
    diff = "".join(difflib.unified_diff(split_lines(base), split_lines(mine), "before", "after", n=2))
    if len(diff) > max_diff:
        diff = diff[:max_diff] + "\n[... cut off]\n"
    return (f"Your task was: {task.description.strip()}\n\n"
            f"On an older copy of {path} you made this change:\n{diff}\n"
            f"Main has moved. This is {path} on the current main:\n```\n{main}```\n\n"
            f"If you now carried out your task again on the current main, what would {path} be afterwards? "
            f"Answer with the complete file in one fenced code block and nothing else.")


def extract_block(text: str) -> Optional[str]:
    """The content between the first opening fence and the last closing fence of a reply, or None."""
    lines = text.splitlines(keepends=True)
    opens = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
    if len(opens) < 2:
        return None
    return "".join(lines[opens[0] + 1:opens[-1]])


def match_style(answer: str, main: str) -> str:
    """Give the answer main's line-ending style and final-newline habit, which a model's reply cannot be trusted to keep."""
    if "\r\n" in main and "\r" not in answer:
        answer = answer.replace("\n", "\r\n")
    ends_main = main.endswith("\n")
    if ends_main and not answer.endswith("\n") and answer:
        answer += "\r\n" if "\r\n" in main else "\n"
    if not ends_main and answer.endswith("\n"):
        answer = answer[:-2] if answer.endswith("\r\n") else answer[:-1]
    return answer


def _changed_lines(a: str, b: str) -> int:
    return sum(max(h.end - h.start, len(h.lines)) for h in hunks(split_lines(a), split_lines(b)))


def plausible(base: str, mine: str, main: str, answer: str) -> bool:
    """A re-application changes about as much as the agent's own change did; a rewrite of the whole file is refused."""
    return _changed_lines(main, answer) <= max(SLACK, FACTOR * _changed_lines(base, mine))


class ModelReapplier:
    """Layer 2 with any chat model (a local model, or ``CliModel`` over ``claude -p``)."""

    def __init__(self, model: ChatModel, max_chars: int = MAX_CHARS) -> None:
        self.model, self.max_chars = model, max_chars

    def reapply(self, task, path: str, base: str, mine: str, main: str) -> Reapplied:
        if len(main) > self.max_chars or len(base) > self.max_chars:
            return Reapplied(None, note=f"{path} is larger than {self.max_chars} characters")
        try:
            reply = self.model.chat([{"role": "user", "content": build_prompt(task, path, base, mine, main)}], [])
        except ModelError as e:
            return Reapplied(None, note=f"model failed: {e}")
        block = extract_block(reply.content)
        u = reply.usage
        if block is None:
            return Reapplied(None, u.prompt_tokens, u.completion_tokens, "the answer had no code block")
        return Reapplied(block, u.prompt_tokens, u.completion_tokens)


class FnReapplier:
    """Layer 2 for tests and for oracle agents: ``fn(task, path, main_text)`` returns the file after the task."""

    def __init__(self, fn: Callable[[object, str, str], Optional[str]]) -> None:
        self.fn = fn

    def reapply(self, task, path: str, base: str, mine: str, main: str) -> Reapplied:
        return Reapplied(self.fn(task, path, main))


LAYERS = (2, 3, 4)


def validate_layers(layers) -> Optional[tuple]:
    """A task's ``"layers"`` declaration: None (use the queue's), or a list of distinct numbers from {2, 3, 4}."""
    if layers is None:
        return None
    if not isinstance(layers, (list, tuple)):
        raise ValueError("'layers' must be a list such as [2, 3, 4]")
    out = []
    for n in layers:
        if isinstance(n, bool) or not isinstance(n, int) or n not in LAYERS:
            raise ValueError(f"unknown layer {n!r}; use numbers from {LAYERS}")
        if n in out:
            raise ValueError(f"layer {n} is listed twice")
        out.append(n)
    return tuple(out)
