"""Any stdio MCP server as a phase 1 connector: two proxy tools, `<name>(tool, arguments)` and `<name>_help(tool)`,
behind an explicit tool allowlist, with a hook for connector-specific argument resolution. The server starts on the
first call (McpSession), runs outside the sandbox with a fixed environment and a Hermie-owned working directory."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .base import ConnectorContext, ConnectorResult, ConnectorTool, Status
from .mcp_session import McpSession
from .preview import cap_text, preview_json

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    title: str
    command: list[str]
    env: dict[str, str]
    allow_tools: frozenset[str]
    instructions: str
    cwd: Path


def _default_session(spec: McpServerSpec) -> McpSession:
    return McpSession(spec.command, spec.env, spec.cwd, spec.cwd / f"{spec.name}.stderr.log")


def _text(result: Any) -> str:
    return "\n".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")


class McpConnector:
    def __init__(self, spec: McpServerSpec, *, timeout: float, preview_chars: int,
                 session_factory: Optional[Callable[[McpServerSpec], McpSession]] = None):
        self.spec, self.name, self.title = spec, spec.name, spec.title
        self.timeout, self.preview_chars = timeout, preview_chars
        self._factory = session_factory or _default_session
        self._session: Optional[McpSession] = None
        self._tool_info: Optional[dict] = None     # name -> (description, input schema), per session of Hermie

    # ------------------------------------------------------------ protocol
    def status(self) -> Status:
        return Status("ready")

    def instructions(self) -> str:
        return self.spec.instructions

    def tools(self) -> list[ConnectorTool]:
        allowed = sorted(self.spec.allow_tools)
        return [
            ConnectorTool(self.name, f"Call a read-only {self.title} tool: tool=<name>, arguments=<object>.",
                          {"type": "object", "properties": {"tool": {"type": "string", "enum": allowed},
                                                            "arguments": {"type": "object"}},
                           "required": ["tool"]}, self._call),
            ConnectorTool(f"{self.name}_help", f"Show the description and arguments of a {self.title} tool.",
                          {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]},
                          self._help),
        ]

    async def prepare_args(self, tool: str, args: dict, ctx: ConnectorContext) -> tuple[dict, Optional[str]]:
        """Hook for subclasses: resolve arguments (e.g. names to IDs). A string return value is a refusal message."""
        return args, None

    # ------------------------------------------------------------ transport
    async def request(self, method: str, *args: Any) -> Any:
        if self._session is None:
            self._session = self._factory(self.spec)
        return await self._session.request(method, *args, timeout=self.timeout)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()

    def _refusal(self, what: str) -> ConnectorResult:
        return ConnectorResult(f"Refused: {what}. Allowed tools: {', '.join(sorted(self.spec.allow_tools))}",
                               label="refused", ok=False)

    # ------------------------------------------------------------ tools
    async def _call(self, args: dict, ctx: ConnectorContext) -> ConnectorResult:
        tool = str(args.get("tool", ""))
        if tool not in self.spec.allow_tools:
            return self._refusal(f"`{tool}` is not an allowed read-only {self.title} tool")
        arguments = args.get("arguments") if args.get("arguments") is not None else {}
        if not isinstance(arguments, dict):
            return ConnectorResult("`arguments` must be an object (a JSON dictionary of the tool's arguments).",
                                   label=tool, ok=False)
        arguments, problem = await self.prepare_args(tool, dict(arguments), ctx)
        if problem:
            return ConnectorResult(problem, label=tool, ok=False)
        try:
            result = await self.request("call_tool", tool, arguments)
        except asyncio.TimeoutError:
            return ConnectorResult(f"{self.title} timed out after {self.timeout:.0f}s; narrow the date range or the "
                                   "dimensions and try again.", label=tool, ok=False)
        except Exception as e:
            log.error("MCP connector %s: %s failed (%s)", self.name, tool, type(e).__name__)
            if type(e).__name__ == "McpError":     # the server answered with a protocol error: the tool failed
                return ConnectorResult(cap_text(f"{tool} failed: {e}", 2000), label=tool, ok=False)
            return ConnectorResult(f"{self.title} is not reachable ({type(e).__name__}: {e}).", label=tool, ok=False)
        text = _text(result)
        if getattr(result, "isError", False):
            return ConnectorResult(f"{tool} failed:\n{cap_text(text, 2000)}", label=tool, ok=False)
        saved = ctx.room_path(f"{self.name}-{tool}.json")
        saved.write_text(text, encoding="utf-8")
        return ConnectorResult(preview_json(text, self.preview_chars), (saved,), label=tool)

    async def _help(self, args: dict, ctx: ConnectorContext) -> ConnectorResult:
        tool = str(args.get("tool", ""))
        if tool not in self.spec.allow_tools:
            return self._refusal(f"`{tool}` is not an allowed {self.title} tool")
        try:
            if self._tool_info is None:
                listed = await self.request("list_tools")
                self._tool_info = {t.name: (t.description or "", t.inputSchema) for t in listed.tools}
        except asyncio.TimeoutError:
            return ConnectorResult(f"{self.title} timed out after {self.timeout:.0f}s.", label="help", ok=False)
        except Exception as e:
            log.error("MCP connector %s: list_tools failed (%s)", self.name, type(e).__name__)
            return ConnectorResult(f"{self.title} is not reachable ({type(e).__name__}).", label="help", ok=False)
        if tool not in self._tool_info:
            return ConnectorResult(f"{self.title} does not offer `{tool}`.", label="help", ok=False)
        desc, schema = self._tool_info[tool]
        return ConnectorResult(cap_text(f"{desc}\n\nArguments (JSON schema):\n{json.dumps(schema, indent=1)}",
                                        self.preview_chars), label="help")
