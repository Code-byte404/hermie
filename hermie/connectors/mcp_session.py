"""One MCP server process and its client session.

The mcp client (stdio_client, ClientSession) uses anyio cancel scopes that must be entered and exited by the same
asyncio task, while Hermie calls tools from whatever task runs the executor. So a dedicated background task owns the
process and the session; callers put (method, args, future) on a queue. A timeout cancels the owner task, which
closes the session and terminates the server in the task that opened them; the next request starts a new one."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_STOP = "__stop__"
_CLOSE_GRACE_S = 5.0


def _settle(fut: asyncio.Future, *, result: Any = None, error: Optional[BaseException] = None) -> None:
    """Resolve a caller's future unless the caller already gave up on it (cancelled it)."""
    if fut.done():
        return
    if error is not None:
        fut.set_exception(error)
    else:
        fut.set_result(result)


def _leaf_type(e: BaseException) -> str:
    """The type name worth logging: anyio wraps the real error in single-member exception groups."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return type(e).__name__


class McpSession:
    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, errlog_path: Path):
        self.command, self.env, self.cwd, self.errlog_path = list(command), dict(env), Path(cwd), Path(errlog_path)
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- caller side ----

    async def request(self, method: str, *args: Any, timeout: float) -> Any:
        """Call `ClientSession.<method>(*args)` on the server, starting it first if needed. On timeout the server is
        killed and asyncio.TimeoutError raised; a server that dies meanwhile raises ConnectionError."""
        await self._start(timeout)
        task, queue = self._task, self._queue
        fut = asyncio.get_running_loop().create_future()
        queue.put_nowait((method, args, fut))
        try:
            done, _ = await asyncio.wait({fut, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            fut.cancel()                         # the owner task must not set an exception nobody retrieves
            raise
        if fut.done():
            return fut.result()
        if task in done:                         # the server died before answering
            fut.cancel()
            raise ConnectionError("MCP server stopped")
        fut.cancel()
        await self._kill()
        raise asyncio.TimeoutError

    async def aclose(self) -> None:
        """Ask the owner task to close the session and stop the server; kill it if that takes too long."""
        task = self._task
        if task is None or task.done():
            self._task = None
            return
        fut = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((_STOP, (), fut))
        await asyncio.wait({task}, timeout=_CLOSE_GRACE_S)
        await self._kill()

    async def _start(self, timeout: float) -> None:
        async with self._lock:
            if self.running:
                return
            loop = asyncio.get_running_loop()
            self._queue, ready = asyncio.Queue(), loop.create_future()
            self._task = asyncio.ensure_future(self._serve(self._queue, ready))
            try:
                await asyncio.wait_for(asyncio.shield(ready), timeout)
            except BaseException:
                await self._kill()
                raise

    async def _kill(self) -> None:
        """Cancel the owner task and wait until it has closed the session and the process. Never raises the owner
        task's outcome; a cancellation of the caller still propagates."""
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()                     # mark retrieved: _serve only lets KeyboardInterrupt/SystemExit out

    # ---- owner task ----

    async def _serve(self, queue: asyncio.Queue, ready: asyncio.Future) -> None:
        """Own the process and the session for their whole life. Outcomes:
        - the server crashes or fails to start (Exception): log its type, end quietly (`running` becomes False);
        - cancelled by `_kill`: end quietly once the session and the process are closed;
        - any other BaseException (KeyboardInterrupt, SystemExit): propagate.
        Whatever happens, requests still in the queue fail with ConnectionError."""
        self.errlog_path.parent.mkdir(parents=True, exist_ok=True)
        errlog = open(self.errlog_path, "w", encoding="utf-8")
        try:
            await self._session_loop(queue, ready, errlog)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
        except Exception as e:
            log.warning("MCP server %s stopped (%s)", Path(self.command[-1]).name, _leaf_type(e))
            _settle(ready, error=e)
        except BaseException as e:
            _settle(ready, error=RuntimeError(f"MCP server stopped ({_leaf_type(e)})"))
            raise
        finally:
            errlog.close()
            while not queue.empty():             # nobody may wait forever on a dead session
                _, _, fut = queue.get_nowait()
                _settle(fut, error=ConnectionError("MCP server stopped"))

    async def _session_loop(self, queue: asyncio.Queue, ready: asyncio.Future, errlog) -> None:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.shared.exceptions import McpError
        from mcp.types import CONNECTION_CLOSED

        def transport_lost(e: Exception) -> bool:
            if isinstance(e, McpError):
                return e.error.code == CONNECTION_CLOSED
            return isinstance(e, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream))

        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=self.env, cwd=self.cwd)
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                _settle(ready)
                while True:
                    method, args, fut = await queue.get()
                    if method == _STOP:
                        _settle(fut)
                        return
                    if fut.done():               # the caller already gave up
                        continue
                    try:
                        result = await getattr(session, method)(*args)
                    except Exception as e:
                        _settle(fut, error=e)
                        if transport_lost(e):    # the server is gone: end so the next request starts a new one
                            raise ConnectionError("MCP server connection closed") from None
                    else:
                        _settle(fut, result=result)
