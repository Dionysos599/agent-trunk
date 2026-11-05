"""A small MCP server over stdio: JSON-RPC 2.0, one message per line, no dependencies.

Implements what a tool server needs: ``initialize``, ``ping``, ``tools/list``, ``tools/call``,
``resources/list``, ``resources/read`` and ``resources/templates/list``. Messages that are notifications are
accepted and ignored. Logging goes to stderr; stdout carries protocol messages only.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, IO, List, Optional

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], Any]            # returns a dict; raising ToolFailure marks an error result


@dataclass
class Resource:
    uri: str
    name: str
    description: str
    read: Callable[[], str]
    mime: str = "application/json"


@dataclass
class ResourceTemplate:
    template: str                              # e.g. agenttrunk://main/{path}
    name: str
    description: str
    read: Callable[[str], str]
    mime: str = "text/plain"

    def match(self, uri: str) -> Optional[str]:
        prefix = self.template.split("{", 1)[0]
        return uri[len(prefix):] if uri.startswith(prefix) and len(uri) > len(prefix) else None


class ToolFailure(Exception):
    """A tool call that did not work out; the text goes back to the model as an error result."""

    def __init__(self, message: str, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.data = data or {}


@dataclass
class McpServer:
    name: str
    version: str
    instructions: str = ""
    tools: Dict[str, Tool] = field(default_factory=dict)
    resources: Dict[str, Resource] = field(default_factory=dict)
    templates: List[ResourceTemplate] = field(default_factory=list)
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True)

    def add_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    # ---- one message ---------------------------------------------------------------------

    def handle(self, msg: Any) -> Optional[dict]:
        """Answer one decoded message; None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                return None                                     # a response to something we never asked
            return self._error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "not a JSON-RPC request")
        method, params, mid = msg["method"], msg.get("params") or {}, msg.get("id")
        is_request = "id" in msg
        if not isinstance(params, dict):
            return self._error(mid, INVALID_PARAMS, "params must be an object") if is_request else None
        fn = getattr(self, "_m_" + method.replace("/", "_").replace("-", "_"), None)
        if fn is None:
            return self._error(mid, METHOD_NOT_FOUND, f"unknown method {method!r}") if is_request else None
        try:
            result = fn(params)
        except _Invalid as e:
            return self._error(mid, INVALID_PARAMS, str(e)) if is_request else None
        except Exception as e:                                   # a bug here must not kill the server
            self.log(f"internal error in {method}: {type(e).__name__}: {e}")
            return self._error(mid, INTERNAL, f"{type(e).__name__}: {e}") if is_request else None
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # ---- methods ---------------------------------------------------------------------------

    def _m_initialize(self, p: dict) -> dict:
        asked = p.get("protocolVersion")
        version = asked if asked in SUPPORTED else SUPPORTED[0]
        out = {"protocolVersion": version,
               "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
               "serverInfo": {"name": self.name, "version": self.version}}
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    def _m_ping(self, p: dict) -> dict:
        return {}

    def _m_notifications_initialized(self, p: dict) -> None:
        return None

    def _m_notifications_cancelled(self, p: dict) -> None:
        return None

    def _m_tools_list(self, p: dict) -> dict:
        return {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                          for t in self.tools.values()]}

    def _m_tools_call(self, p: dict) -> dict:
        name, args = p.get("name"), p.get("arguments")
        if not isinstance(name, str) or name not in self.tools:
            raise _Invalid(f"unknown tool {name!r}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _Invalid("arguments must be an object")
        tool = self.tools[name]
        try:
            problem = _check(tool.schema, args)
            if problem:
                raise ToolFailure(problem)
            data = tool.handler(args)
            failed = isinstance(data, dict) and data.get("ok") is False
            return self._content(data, failed)
        except ToolFailure as e:
            return self._content({"ok": False, "error": str(e), **e.data}, True)
        except Exception as e:
            self.log(f"tool {name} failed: {type(e).__name__}: {e}")
            return self._content({"ok": False, "error": f"{type(e).__name__}: {e}"}, True)

    @staticmethod
    def _content(data: Any, is_error: bool) -> dict:
        text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=str)
        out = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if isinstance(data, dict):
            out["structuredContent"] = json.loads(json.dumps(data, default=str))
        return out

    def _m_resources_list(self, p: dict) -> dict:
        return {"resources": [{"uri": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime}
                              for r in self.resources.values()]}

    def _m_resources_templates_list(self, p: dict) -> dict:
        return {"resourceTemplates": [{"uriTemplate": t.template, "name": t.name, "description": t.description,
                                       "mimeType": t.mime} for t in self.templates]}

    def _m_resources_read(self, p: dict) -> dict:
        uri = p.get("uri")
        if not isinstance(uri, str):
            raise _Invalid("uri is required")
        if uri in self.resources:
            r = self.resources[uri]
            return {"contents": [{"uri": uri, "mimeType": r.mime, "text": r.read()}]}
        for t in self.templates:
            arg = t.match(uri)
            if arg is not None:
                try:
                    return {"contents": [{"uri": uri, "mimeType": t.mime, "text": t.read(arg)}]}
                except Exception as e:
                    raise _Invalid(str(e))
        raise _Invalid(f"no resource {uri!r}")

    # ---- the loop ------------------------------------------------------------------------

    def serve(self, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
        lock = threading.Lock()

        def send(obj) -> None:
            with lock:
                stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                stdout.flush()
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, RecursionError):
                send(self._error(None, PARSE_ERROR, "invalid JSON"))
                continue
            if isinstance(msg, list):                            # a batch (older protocol versions)
                replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                if replies:
                    send(replies)
                continue
            reply = self.handle(msg)
            if reply is not None:
                send(reply)


class _Invalid(Exception):
    pass


def _check(schema: dict, args: dict) -> Optional[str]:
    """The little JSON-Schema subset the tools use: required keys, basic types, enums, no unknown keys."""
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required argument {key!r}"
    for key, value in args.items():
        if key not in props:
            return f"unknown argument {key!r}; allowed: {', '.join(props) or 'none'}"
        want = props[key].get("type")
        ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
              "boolean": isinstance(value, bool), "array": isinstance(value, list),
              "object": isinstance(value, dict)}.get(want, True)
        if not ok:
            return f"argument {key!r} must be of type {want}"
        if want == "array" and props[key].get("items", {}).get("type") == "string" and not all(isinstance(x, str) for x in value):
            return f"argument {key!r} must be a list of strings"
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"argument {key!r} must be one of {props[key]['enum']}"
    return None
