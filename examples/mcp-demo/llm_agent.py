"""A small MCP client driven by any OpenAI-compatible model (a local Ollama, for instance).

    python3 llm_agent.py --name alice --board demo --main demo --model eco:latest

It starts `agenttrunk mcp` for the given agent name, shows the model the server's tools (translated into the
OpenAI tool format) plus four file tools on the task folder (a Claude Code window has its own file tools; this
stands in for them), and lets the model work until it stops asking for tools. It is what a real client does,
with a model you can run for free: the tool descriptions and the `next` hints are the only guidance.

Every step is appended to --log (JSON lines) so that what the model did can be read afterwards.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "workspace" / "src")]

from cmws_ws.agents import ModelError, OpenAICompatModel   # noqa: E402

PROMPT = """You are one of several agents sharing a project through the agenttrunk tools.
Call show_board, then claim_task, do exactly what the task says in the folder it gives you, then call
submit_task. If submit_task says main moved, call sync_with_main, read what it reports, fix what is needed and
submit again. When a tool answer has a 'next' field, do what it says. Keep going until claim_task says nothing is
ready, then reply with a short summary and no tool call."""

FILE_TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "List the files in your task folder (a path inside it, '' for the top).",
                                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file in your task folder.",
                                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a file in your task folder.",
                                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                                    "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "replace_in_file", "description": "Replace one exact piece of text (it must occur once) in a file of your task folder.",
                                     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                                                    "required": ["path", "old", "new"]}}},
]


class McpProcess:
    def __init__(self, argv, env=None):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  bufsize=1, env=env)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "llm_agent"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("the MCP server closed its output")
        return json.loads(line)

    def tools(self):
        return self.rpc("tools/list")["result"]["tools"]

    def call(self, name, arguments):
        r = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in r:
            return {"ok": False, "error": r["error"]["message"]}
        return r["result"]["structuredContent"]

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                                   # noqa: BLE001
            self.p.kill()


def mcp_to_openai(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
            for t in tools]


class Folder:
    """The file tools, confined to the folder claim_task handed over."""

    def __init__(self):
        self.root = None

    def _p(self, rel):
        if self.root is None:
            raise ValueError("you have no task folder yet: call claim_task first")
        p = (self.root / (rel or "")).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError("that path is outside your task folder")
        return p

    def call(self, name, a):
        try:
            if name == "list_files":
                p = self._p(a.get("path", ""))
                return "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir() if e.name != ".agenttrunk"))
            if name == "read_file":
                return self._p(a["path"]).read_text()
            if name == "write_file":
                p = self._p(a["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(a["content"])
                return f"wrote {len(a['content'])} characters to {a['path']}"
            if name == "replace_in_file":
                p = self._p(a["path"])
                text = p.read_text()
                n = text.count(a["old"])
                if n != 1:
                    return f"error: the text occurs {n} times in {a['path']}; it must occur exactly once"
                p.write_text(text.replace(a["old"], a["new"], 1))
                return "replaced 1 occurrence"
        except (OSError, ValueError, KeyError, TypeError) as e:
            return f"error: {type(e).__name__}: {e}"
        return f"error: unknown tool {name}"


def run_agent(model, mcp: McpProcess, log, prompt=PROMPT, max_steps=60, name="agent"):
    folder = Folder()
    tools = mcp_to_openai(mcp.tools()) + FILE_TOOLS
    mcp_names = {t["function"]["name"] for t in tools} - {t["function"]["name"] for t in FILE_TOOLS}
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "Begin."}]
    stats = {"steps": 0, "tool_calls": 0, "submits": {}, "prompt_tokens": 0, "completion_tokens": 0, "ended": "step limit"}
    nudges = 0
    for step in range(1, max_steps + 1):
        try:
            reply = model.chat(messages, tools)
        except ModelError as e:
            stats["ended"] = f"model error: {e}"
            break
        stats["steps"] = step
        stats["prompt_tokens"] += reply.usage.prompt_tokens
        stats["completion_tokens"] += reply.usage.completion_tokens
        assistant = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                       for c in reply.tool_calls]
        messages.append(assistant)
        if not reply.tool_calls:
            nudges += 1
            log({"step": step, "agent": name, "say": reply.content[:300]})
            if nudges >= 2:
                stats["ended"] = "finished"
                break
            messages.append({"role": "user", "content": "If nothing is left to do, reply with your summary. Otherwise call a tool."})
            continue
        nudges = 0
        for c in reply.tool_calls:
            stats["tool_calls"] += 1
            if c.name in mcp_names:
                out = mcp.call(c.name, c.arguments)
                if out.get("ok") and out.get("folder"):          # claim_task, or task_status after a restart
                    folder.root = Path(out["folder"])
                if c.name == "submit_task":
                    s = out.get("status") or ("error" if out.get("ok") is False else "?")
                    stats["submits"][s] = stats["submits"].get(s, 0) + 1
                text = json.dumps(out)
            else:
                text = folder.call(c.name, c.arguments)
            log({"step": step, "agent": name, "tool": c.name, "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in c.arguments.items()},
                 "result": text[:400]})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": text[:6000]})
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="agent name (keep it the same across restarts)")
    ap.add_argument("--board", required=True)
    ap.add_argument("--main", default="main")
    ap.add_argument("--url", default=os.environ.get("AGENTTRUNK_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--model", default="eco:latest")
    ap.add_argument("--llm-url", default=os.environ.get("AGENTTRUNK_LLM_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--llm-host", default=os.environ.get("AGENTTRUNK_LLM_HOST"))
    ap.add_argument("--workdir", type=Path, default=Path("work"))
    ap.add_argument("--validate-cmd", default="python3 -m unittest discover")
    ap.add_argument("--claim-ttl", default="120")
    ap.add_argument("--prompt", help="replace the instruction given to the model")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--log", type=Path, help="append every step to this file (JSON lines)")
    a = ap.parse_args()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "workspace" / "src"))
    mcp = McpProcess([str(ROOT / "bin" / "agenttrunk"), "--url", a.url, "--main", a.main, "mcp", "--board", a.board, "--agent", a.name,
                      "--workdir", str(a.workdir), "--validate-cmd", a.validate_cmd, "--claim-ttl", a.claim_ttl], env=env)
    model = OpenAICompatModel(a.model, a.llm_url, timeout=600, headers={"Host": a.llm_host} if a.llm_host else None)
    logf = a.log.open("a") if a.log else None

    def log(entry):
        entry["t"] = round(time.time(), 2)
        if logf:
            logf.write(json.dumps(entry) + "\n")
            logf.flush()
    t0 = time.time()
    try:
        stats = run_agent(model, mcp, log, a.prompt or PROMPT, a.max_steps, a.name)
    finally:
        mcp.close()
    stats["seconds"] = round(time.time() - t0, 1)
    stats["agent"] = a.name
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
