"""An MCP server, so Claude can ask this app questions instead of guessing.

Speaks JSON-RPC 2.0 over stdin/stdout with the standard library. No SDK, no new
dependency: the protocol is a handful of methods, and adding a package to an app
whose whole premise is that it costs nothing and has no build step would be a
poor trade for the fifty lines it saves.

What the model can and cannot do is decided in assistant.py, not here. This file
is transport: read a message, dispatch it, write a reply. The database is opened
through SQLite's read-only mode, so the guarantee holds even if this file has a
bug in it.

Run it with `finasst mcp`. To connect Claude Desktop, add to
claude_desktop_config.json:

    {"mcpServers": {"fin-asst": {"command": "/absolute/path/to/finasst",
                                 "args": ["mcp"]}}}

Nothing is exposed to a network: the transport is this process's own pipes.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Optional

from . import assistant

SERVER_NAME = "fin-asst"
SERVER_VERSION = "1.0.0"

# Revisions this server is happy to speak. A client asking for one of these gets
# it echoed back; anything else gets our newest and can decide what to do.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class Server:
    def __init__(self, db_path=None, stdin=None, stdout=None):
        self.db_path = db_path
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._conn = None

    # -- database ---------------------------------------------------------
    def conn(self):
        """Opened lazily and kept, so a missing database is reported as an
        answer to a question rather than a crash at start-up."""
        if self._conn is None:
            self._conn = assistant.open_db(self.db_path)
        return self._conn

    # -- transport --------------------------------------------------------
    def send(self, message: dict) -> None:
        self.stdout.write(json.dumps(message) + "\n")
        self.stdout.flush()

    def reply(self, request_id, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def fail(self, request_id, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": code, "message": message}})

    def run(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self.fail(None, PARSE_ERROR, f"Invalid JSON: {exc}")
                continue
            try:
                self.handle(message)
            except Exception:                      # never take the pipe down
                self.fail(message.get("id"), INTERNAL_ERROR, traceback.format_exc(limit=3))
        return 0

    # -- dispatch ---------------------------------------------------------
    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and get no reply, by the spec.
        if request_id is None:
            return

        if method == "initialize":
            wanted = params.get("protocolVersion")
            self.reply(request_id, {
                "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": assistant.INSTRUCTIONS,
            })
        elif method == "ping":
            self.reply(request_id, {})
        elif method == "tools/list":
            self.reply(request_id, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]}
                for t in assistant.TOOLS
            ]})
        elif method == "tools/call":
            self.call_tool(request_id, params)
        elif method in ("resources/list", "prompts/list"):
            # Declared unsupported in capabilities, but some clients ask anyway.
            self.reply(request_id, {"resources": [], "prompts": []})
        else:
            self.fail(request_id, METHOD_NOT_FOUND, f"Unsupported method: {method}")

    def call_tool(self, request_id, params: dict) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = assistant.call(self.conn(), name, arguments)
        except assistant.DataNotReady as exc:
            self.tool_error(request_id, str(exc))
            return
        except FileNotFoundError as exc:
            self.tool_error(request_id, str(exc))
            return
        except KeyError as exc:
            self.tool_error(request_id, str(exc).strip('"'))
            return
        except TypeError as exc:
            self.tool_error(request_id, f"Wrong arguments for {name}: {exc}")
            return
        except Exception as exc:
            # A tool error is an answer the model can react to, not a transport
            # failure -- so it goes back as a result with isError, per the spec.
            self.tool_error(request_id, f"{type(exc).__name__}: {exc}")
            return

        self.reply(request_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            "isError": False,
        })

    def tool_error(self, request_id, message: str) -> None:
        self.reply(request_id, {
            "content": [{"type": "text", "text": message}], "isError": True,
        })


def main(db_path: Optional[str] = None) -> int:
    return Server(db_path=db_path).run()
