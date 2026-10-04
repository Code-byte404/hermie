"""One MCP server process and its client session.

The mcp client (stdio_client, ClientSession) uses anyio cancel scopes that must be entered and exited by the same
asyncio task, while Hermie calls tools from whatever task runs the executor. So a dedicated background task (the
owner) holds the process and the session; callers put (method, args, future) on its queue. A timeout of the call in
flight cancels the owner, which closes the session and terminates the server in the task that opened them; the next
request starts a new one. A pump between the server's stdout and the session notices a server that dies while idle,
so `running` turns False at once and the next request starts a fresh server instead of failing.

Errors callers see from `request`:
- `McpConnectionError` (a ConnectionError): the server could not start, went away, or was closed. A request that never
  reached a dead server is retried once on a new one, so an idle server death costs no failed request.
- `asyncio.TimeoutError`: the timeout ran out (starting, waiting in the queue, or in the call itself).
- anything else (e.g. `mcp.shared.exceptions.McpError` from a live server) passes through unchanged.

Child environment: the given `env`, plus whatever mcp's `stdio_client` merges in underneath it from its
`get_default_environment()` (on macOS/Linux HOME, LOGNAME, PATH, SHELL, TERM, USER from Hermie's environment; a key
given in `env` wins). Nothing else from `os.environ` reaches the server.

Server stderr goes to `errlog_path`: truncated on the first start in each Hermie process, appended across restarts
within the process (crash output stays), started over once it passes ~1 MB. The `mcp` logger is set to CRITICAL when
the first session starts: mcp logs a snippet of non-JSON server stdout through logger.exception."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_STOP = "__stop__"          # queue item from aclose: end the session cleanly
_LOST = "__lost__"          # queue item from the pump: the server's stdout closed
# mcp's own teardown after the grace can take 2 s (exit on stdin EOF) + 2 s (SIGTERM) before SIGKILL; grace plus
# that stays inside Hermie's per-connector close bound (core.CLOSE_TIMEOUT_S, 8 s)
_CLOSE_GRACE_S = 1.0
_ERRLOG_MAX_BYTES = 1_000_000
_started_logs: set[Path] = set()   # errlog paths already started over in this process


class McpConnectionError(ConnectionError):
    """The MCP server could not be started, went away, or was closed."""


class _NotSent(McpConnectionError):
    """The request never reached the server (it was dead or closing); safe to retry on a new one."""


def _settle(fut: Optional[asyncio.Future], *, result: Any = None, error: Optional[BaseException] = None) -> None:
    """Resolve a caller's future unless the caller already gave up on it (cancelled it)."""
    if fut is None or fut.done():
        return
    if error is not None:
        fut.set_exception(error)
    else:
        fut.set_result(result)


def _mark_retrieved(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()


def _leaf_type(e: BaseException) -> str:
    """The type name worth logging: anyio wraps the real error in single-member exception groups."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return type(e).__name__


class _Owner:
    """One server generation: the owner task, its queue and its state."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Future = loop.create_future()
        self.ready.add_done_callback(_mark_retrieved)   # a starter that gave up must not leave it unretrieved
        self.task: Optional[asyncio.Task] = None
        self.closing = False                            # killed, closed, or the transport is lost
        self.lost = False                               # the server's stdout closed
        self.inflight: Optional[asyncio.Future] = None  # the caller future whose call is being sent/awaited

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done() and not self.closing


class McpSession:
    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, errlog_path: Path):
        self.command, self.env, self.cwd, self.errlog_path = list(command), dict(env), Path(cwd), Path(errlog_path)
        self._owner: Optional[_Owner] = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._owner is not None and self._owner.alive

    # ---- caller side ----

    async def request(self, method: str, *args: Any, timeout: float) -> Any:
        """Call `ClientSession.<method>(*args)` on the server, starting it first if needed. `timeout` covers the
        whole request (start included). See the module docstring for the errors."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            owner = await self._start(deadline)
            return await self._call(owner, method, args, deadline)
        except _NotSent:                         # the server died before the call reached it: one fresh try
            owner = await self._start(deadline)
            return await self._call(owner, method, args, deadline)

    async def aclose(self) -> None:
        """Ask the owner to close the session and stop the server; kill it if that takes too long."""
        owner = self._owner
        if owner is None:
            return
        if not owner.task.done():
            owner.closing = True
            owner.queue.put_nowait((_STOP, (), None))
            await asyncio.wait({owner.task}, timeout=_CLOSE_GRACE_S)
        await self._kill(owner)

    async def _start(self, deadline: float) -> _Owner:
        loop = asyncio.get_running_loop()
        async with self._lock:
            old = self._owner
            if old is not None and old.alive:
                return old
            if old is not None and not old.task.done():       # still closing: let it finish first
                done, _ = await asyncio.wait({old.task}, timeout=max(deadline - loop.time(), 0))
                if not done:
                    raise asyncio.TimeoutError
            owner = _Owner(loop)
            owner.task = asyncio.ensure_future(self._serve(owner))
            self._owner = owner
            try:
                done, _ = await asyncio.wait({owner.ready}, timeout=max(deadline - loop.time(), 0))
            except asyncio.CancelledError:
                await self._kill(owner)
                raise
            if not done:
                await self._kill(owner)
                raise asyncio.TimeoutError
            if owner.ready.exception() is not None:
                await self._kill(owner)
                raise owner.ready.exception()
            return owner

    async def _call(self, owner: _Owner, method: str, args: tuple, deadline: float) -> Any:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        owner.queue.put_nowait((method, args, fut))
        try:
            await asyncio.wait({fut, owner.task}, timeout=max(deadline - loop.time(), 0),
                               return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            fut.cancel()                         # the owner skips it or drops its outcome
            raise
        if fut.done():
            return fut.result()
        fut.cancel()
        if owner.task.done():                    # fallback; the owner normally settles every future it holds
            raise McpConnectionError("MCP server stopped")
        if owner.inflight is fut:                # the call itself hung: the server is stuck
            await self._kill(owner)
        raise asyncio.TimeoutError               # (a call still queued is just dropped; the server lives on)

    async def _kill(self, owner: _Owner) -> None:
        """Cancel this owner and wait until it has closed the session and the process. Never raises the owner's
        outcome; a cancellation of the caller still propagates."""
        owner.closing = True
        task = owner.task
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if self._owner is owner:
            self._owner = None
        if not task.cancelled():
            task.exception()                     # mark retrieved: _serve only lets KeyboardInterrupt/SystemExit out

    # ---- owner task ----

    def _open_errlog(self):
        """Server stderr: started over on the first start in this process, then appended across restarts (the crash
        output stays), started over again once it passes ~1 MB."""
        self.errlog_path.parent.mkdir(parents=True, exist_ok=True)
        key = self.errlog_path.resolve()
        try:
            too_big = self.errlog_path.stat().st_size > _ERRLOG_MAX_BYTES
        except FileNotFoundError:
            too_big = False
        f = open(self.errlog_path, "w" if too_big or key not in _started_logs else "a", encoding="utf-8")
        _started_logs.add(key)
        return f

    async def _serve(self, owner: _Owner) -> None:
        """Own the process and the session for their whole life. Outcomes:
        - the server crashes or fails to start (Exception): log its type, end quietly (`running` becomes False);
        - cancelled by `_kill`: end quietly once the session and the process are closed;
        - any other BaseException (KeyboardInterrupt, SystemExit): propagate.
        A starter never sees CancelledError it did not cause: `ready` gets McpConnectionError instead. Whatever
        happens, the call in flight fails with McpConnectionError and queued requests with _NotSent."""
        errlog = None
        try:
            errlog = self._open_errlog()
            await self._session_loop(owner, errlog)
        except asyncio.CancelledError:
            _settle(owner.ready, error=McpConnectionError("MCP server start cancelled"))
        except Exception as e:
            if asyncio.current_task().cancelling():
                # mcp's teardown can raise (e.g. BrokenResourceError from its stdout reader) in place of the
                # CancelledError: still our own kill, not a crash
                _settle(owner.ready, error=McpConnectionError("MCP server start cancelled"))
                return
            log.warning("MCP server %s stopped (%s)", Path(self.command[-1]).name, _leaf_type(e))
            _settle(owner.ready, error=McpConnectionError(f"MCP server failed to start ({_leaf_type(e)})"))
        except BaseException as e:
            _settle(owner.ready, error=McpConnectionError(f"MCP server stopped ({_leaf_type(e)})"))
            raise
        finally:
            owner.closing = True
            if errlog is not None:
                errlog.close()
            _settle(owner.inflight, error=McpConnectionError("MCP server stopped"))
            while not owner.queue.empty():       # nobody may wait forever on a dead session
                _, _, fut = owner.queue.get_nowait()
                _settle(fut, error=_NotSent("MCP server stopped before the request was sent"))

    async def _session_loop(self, owner: _Owner, errlog) -> None:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        logging.getLogger("mcp").setLevel(logging.CRITICAL)   # see the module docstring
        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=self.env, cwd=self.cwd)
        async with stdio_client(params, errlog=errlog) as (read, write):
            to_session, session_read = anyio.create_memory_object_stream(0)
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._pump, read, to_session, owner)
                async with ClientSession(session_read, write) as session:
                    await session.initialize()
                    _settle(owner.ready)
                    await self._serve_requests(owner, session)
                tg.cancel_scope.cancel()

    @staticmethod
    async def _pump(read, to_session, owner: _Owner) -> None:
        """Forward server messages to the session; when the server's stdout closes, mark the owner lost at once."""
        try:
            async with to_session:
                async for message in read:
                    await to_session.send(message)
        except Exception:                        # the session side closed first; nothing left to forward to
            pass
        finally:
            owner.lost = owner.closing = True
            owner.queue.put_nowait((_LOST, (), None))

    @staticmethod
    async def _serve_requests(owner: _Owner, session) -> None:
        import anyio
        from mcp.shared.exceptions import McpError
        from mcp.types import CONNECTION_CLOSED

        while True:
            method, args, fut = await owner.queue.get()
            if method == _STOP:
                return
            if method == _LOST:
                raise McpConnectionError("MCP server connection lost")
            if fut.done():                       # the caller already gave up
                continue
            if owner.lost:                       # queued before the pump's _LOST marker: do not send it
                _settle(fut, error=_NotSent("MCP server stopped before the request was sent"))
                raise McpConnectionError("MCP server connection lost")
            owner.inflight = fut
            try:
                result = await getattr(session, method)(*args)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                _settle(fut, error=_NotSent("MCP server stopped before the request was sent"))
                raise McpConnectionError("MCP server connection lost") from None
            except anyio.EndOfStream:
                _settle(fut, error=McpConnectionError("MCP server connection lost"))
                raise McpConnectionError("MCP server connection lost") from None
            except McpError as e:
                if e.error.code != CONNECTION_CLOSED:
                    _settle(fut, error=e)        # a tool-level error from a live server
                    owner.inflight = None
                    continue
                _settle(fut, error=McpConnectionError("MCP server connection lost"))
                raise McpConnectionError("MCP server connection lost") from None
            except Exception as e:
                _settle(fut, error=e)
            else:
                _settle(fut, result=result)
            owner.inflight = None
