import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from itertools import count
from typing import Any, Self

type Record = dict[str, Any]

_DEFAULT_COMMAND = (
    "pi",
    "--mode",
    "rpc",
    "--provider",
    "deepseek",
    "--model",
    "deepseek-flash",
    "--no-tools",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-mcp",
    "--no-themes",
    "--no-context-files",
)


class PiRpcError(RuntimeError):
    pass


class PiRpcRejected(PiRpcError):
    pass


class PiRpcClient:
    """Run one Pi RPC session for the lifetime of an async context."""

    def __init__(self, command: tuple[str, ...] = _DEFAULT_COMMAND):
        self._command = command
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._failure: str | None = None
        self._request_ids = count(1)
        self._pending: dict[str, asyncio.Future[Record | None]] = {}
        self._events: asyncio.Queue[Record | None] = asyncio.Queue()
        self._prompt_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        if self._process is not None:
            raise PiRpcError("This Pi RPC client has already been started")

        executable, *arguments = self._command
        executable = shutil.which(executable) or executable
        creation_flags = 0
        if sys.platform == "win32":
            # Let the parent close Pi's stdin instead of sharing its console Ctrl+C.
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                creationflags=creation_flags,
            )
        except OSError as error:
            raise PiRpcError(f"Could not start Pi RPC: {error}") from error

        assert self._process.stdout is not None
        self._reader = asyncio.create_task(self._read_stdout(self._process.stdout))
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, *_exc_info: object
    ) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        assert self._reader is not None

        self._closing = True
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        await self._reader
        if exc_type is None and process.returncode != 0:
            raise PiRpcError(f"Pi RPC exited with status {process.returncode}")

    async def prompt(self, message: str, *, queue_if_busy: bool = True) -> Record:
        async with self._prompt_lock:
            if not queue_if_busy:
                return await self._request("prompt", message=message)
            return await self._request(
                "prompt", message=message, streamingBehavior="followUp"
            )

    async def follow_up(self, message: str) -> Record:
        """Explicitly queue a follow-up for an active run."""
        async with self._prompt_lock:
            return await self._request("follow_up", message=message)

    async def get_messages(self) -> list[Record]:
        data = await self._request("get_messages")
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise PiRpcError("Pi RPC get_messages returned no message list")
        return messages

    async def events(self) -> AsyncIterator[Record]:
        """Yield session events in wire order for a single consumer."""
        if self._process is None:
            raise PiRpcError("Pi RPC has not been started")
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)
        if self._failure is not None:
            raise PiRpcError(self._failure)

    async def _request(self, command: str, **fields: object) -> Record:
        process = self._process
        if self._failure is not None:
            raise PiRpcError(self._failure)
        if process is None or process.returncode is not None or self._closing:
            raise PiRpcError("Pi RPC is not running")
        assert process.stdin is not None

        request_id = str(next(self._request_ids))
        record = {"type": command, "id": request_id, **fields}
        line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        reply = asyncio.get_running_loop().create_future()
        self._pending[request_id] = reply
        try:
            async with asyncio.timeout(30):
                process.stdin.write(line)
                await process.stdin.drain()
                response = await reply
        except TimeoutError as error:
            raise PiRpcError(f"Pi RPC {command} timed out") from error
        except OSError as error:
            raise PiRpcError(f"Could not write Pi RPC {command}: {error}") from error
        finally:
            del self._pending[request_id]

        if response is None:
            raise PiRpcError(self._failure or "Pi RPC closed before responding")
        if response.get("success") is not True:
            raise PiRpcRejected(f"Pi RPC {command} failed: {response.get('error')}")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def _read_stdout(self, stdout: asyncio.StreamReader) -> None:
        try:
            async for line in _read_frames(stdout):
                record = json.loads(line.decode("utf-8"))
                if not isinstance(record, dict) or not isinstance(
                    record.get("type"), str
                ):
                    raise PiRpcError("Pi RPC stdout returned an invalid record")
                if record["type"] != "response":
                    self._events.put_nowait(record)
                    continue

                request_id = record.get("id")
                if isinstance(request_id, str):
                    reply = self._pending.get(request_id)
                    if reply is not None and not reply.done():
                        reply.set_result(record)

            if not self._closing:
                self._failure = "Pi RPC stdout closed unexpectedly"
        except PiRpcError as error:
            self._failure = str(error)
        except (OSError, ValueError):
            self._failure = "Could not read a Pi RPC record from stdout"
        finally:
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_result(None)
            self._events.put_nowait(None)


async def _read_frames(stdout: asyncio.StreamReader) -> AsyncIterator[bytes]:
    # History can exceed StreamReader's line limit, so assemble LF frames in chunks.
    pending = bytearray()
    while chunk := await stdout.read(64 * 1024):
        parts = chunk.split(b"\n")
        pending.extend(parts[0])
        if len(parts) == 1:
            continue
        yield bytes(pending)
        for line in parts[1:-1]:
            yield line
        pending = bytearray(parts[-1])
    if pending:
        raise PiRpcError("Pi RPC stdout closed in the middle of a record")
