"""Synchronous bridge from the OVOS proxy to the familia-mcp server.

The proxy is a classic threaded HTTP server; the official MCP SDK is async.
This module runs one asyncio loop in a background thread that owns a single,
long-lived MCP session and exposes two blocking calls to the proxy threads:

    bridge.tool_specs()        -> tools to declare to /ask
    bridge.run_tool_call(call) -> tool result to send back to /ask

Transport is MCP stdio. In production the "command" is an `ssh` invocation to
the MCP Pi, whose authorized_keys entry forces `familia-mcp` (no shell, no
forwarding), so no new network port is opened anywhere:

    MCP_SERVER_COMMAND="ssh -F none -T -o BatchMode=yes -i /etc/familia/mcp_ed25519 \
        -o UserKnownHostsFile=/etc/familia/mcp_known_hosts \
        -o StrictHostKeyChecking=yes familia-mcp@mcpserver.local"

If the MCP Pi is unreachable the bridge reports no tools and the proxy keeps
working exactly as before (documents + general knowledge). Reconnection is
attempted at most every MCP_RETRY_AFTER_S seconds so a dead Pi does not add
latency to every spoken question.

Dependencies: mcp==2.3.0 (same version as the server; see
scripts/requirements-proxy.txt).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import threading
import time
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger("familia.mcp_bridge")


INTERNAL_META_KEY = "familia/internal"


def is_internal(tool: Any) -> bool:
    """Tools the server marks internal are never declared to the model."""
    return bool((getattr(tool, "meta", None) or {}).get(INTERNAL_META_KEY) is True)


def result_content(result: Any, max_chars: int) -> dict[str, Any] | str:
    """Compact a CallToolResult into what /ask accepts (JSON object or text)."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if len(json.dumps(structured, ensure_ascii=False)) <= max_chars:
            return structured
        return json.dumps(structured, ensure_ascii=False)[:max_chars]
    texts = [getattr(c, "text", "") for c in getattr(result, "content", None) or []]
    return "\n".join(t for t in texts if t)[:max_chars]


class McpBridge:
    def __init__(
        self,
        command: str,
        *,
        connect_timeout_s: float = 15.0,
        call_timeout_s: float = 20.0,
        retry_after_s: float = 30.0,
        max_result_chars: int = 4000,
    ):
        argv = shlex.split(command)
        if not argv:
            raise ValueError("MCP server command is empty")
        self._params = StdioServerParameters(command=argv[0], args=argv[1:])
        self.connect_timeout_s = connect_timeout_s
        self.call_timeout_s = call_timeout_s
        self.retry_after_s = retry_after_s
        self.max_result_chars = max_result_chars

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-bridge", daemon=True)
        self._thread.start()

        self._lock = threading.Lock()  # serializes (re)connection attempts
        self._client: Client | None = None
        self._owner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._tools: list[Any] = []
        self._last_attempt = 0.0

    # ------------------------------------------------------------ connection
    async def _session_owner(self, ready: asyncio.Future) -> None:
        """Owns the Client context: anyio requires enter/exit in one task."""
        try:
            async with Client(self._params, read_timeout_seconds=self.call_timeout_s) as client:
                tools, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                self._stop = asyncio.Event()
                ready.set_result((client, tools))
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - report and end the session
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))
            else:
                logger.warning("MCP session ended: %s", exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            # Only the current session may clear the shared client; a stale
            # owner finishing after a reconnect must not drop the new one.
            if self._owner is asyncio.current_task():
                self._client = None

    async def _connect(self) -> None:
        ready: asyncio.Future = self._loop.create_future()
        owner = self._loop.create_task(self._session_owner(ready))
        try:
            client, tools = await asyncio.wait_for(ready, timeout=self.connect_timeout_s)
        except BaseException:
            owner.cancel()
            raise
        self._client, self._tools, self._owner = client, tools, owner

    def _connected(self) -> bool:
        return self._client is not None and self._owner is not None and not self._owner.done()

    def ensure_connected(self) -> bool:
        if self._connected():
            return True
        with self._lock:
            if self._connected():
                return True
            now = time.monotonic()
            if self._last_attempt and now - self._last_attempt < self.retry_after_s:
                return False
            self._last_attempt = now
            fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
            try:
                fut.result(timeout=self.connect_timeout_s + 5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP server not reachable: %s", exc or type(exc).__name__)
                return False
            logger.info("MCP connected: %d tools", len(self._tools))
            return True

    def disconnect(self) -> None:
        stop = self._stop
        if stop is not None:
            self._loop.call_soon_threadsafe(stop.set)
        self._client = None

    def close(self) -> None:
        self.disconnect()
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------- tool API
    def tool_specs(self) -> list[dict[str, Any]]:
        """Tool declarations for /ask (internal tools excluded), or [] when
        the MCP Pi is unreachable."""
        if not self.ensure_connected():
            return []
        return [
            {
                "name": t.name,
                "description": t.description or t.name,
                "input_schema": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in self._tools
            if not is_internal(t)
        ]

    def run_tool_call(self, call: dict[str, Any], *, internal: bool = False) -> dict[str, Any]:
        """Execute one tool request; never raises.

        Model-originated calls (internal=False) may only reach public tools.
        Internal tools (e.g. confirming a parked Home Assistant action) can
        only be called by the proxy itself, after the user said yes.
        """
        tool_use_id = call.get("id")
        name = call.get("name")
        arguments = call.get("input") if isinstance(call.get("input"), dict) else {}

        def error(message: str) -> dict[str, Any]:
            return {"tool_use_id": tool_use_id, "status": "error", "content": message}

        # Only tools this MCP server actually listed may run, whatever the
        # model (or a tampered response) asks for.
        tool = next((t for t in self._tools if t.name == name), None)
        if tool is None or (is_internal(tool) and not internal):
            return error(f"Tool '{name}' is not available.")
        if not self.ensure_connected():
            return error("Home tools are not reachable right now.")
        client = self._client
        fut = asyncio.run_coroutine_threadsafe(client.call_tool(name, arguments), self._loop)
        try:
            result = fut.result(timeout=self.call_timeout_s + 5)
        except Exception as exc:  # noqa: BLE001
            fut.cancel()
            logger.warning("MCP tool %s failed: %s", name, exc or type(exc).__name__)
            self.disconnect()  # force a clean reconnect next time
            return error("The tool call failed or timed out.")
        return {
            "tool_use_id": tool_use_id,
            "status": "error" if result.is_error else "success",
            "content": result_content(result, self.max_result_chars),
        }
