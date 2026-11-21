import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError, PiRpcRejected, Record

logger = logging.getLogger(__name__)


def create_app(make_pi: Callable[[], PiRpcClient] = PiRpcClient) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[dict[str, _ChatSession]]:
        async with make_pi() as pi, asyncio.TaskGroup() as tasks:
            chat = _ChatSession(pi, tasks)
            events = tasks.create_task(chat.read_events())
            try:
                yield {"chat": chat}
            finally:
                events.cancel()
                chat.cancel_submission()

    app = FastAPI(lifespan=lifespan)
    app.add_api_websocket_route("/ws", _websocket)
    return app


class _Subscriber:
    def __init__(self) -> None:
        self.outbox: asyncio.Queue[Record] = asyncio.Queue(maxsize=64)
        self.overflow = asyncio.get_running_loop().create_future()

    def send(self, message: Record) -> None:
        if self.overflow.done():
            return
        try:
            self.outbox.put_nowait(message)
        except asyncio.QueueFull:
            self.overflow.set_result(None)


class _ChatSession:
    def __init__(self, pi: PiRpcClient, tasks: asyncio.TaskGroup) -> None:
        self._pi = pi
        self._tasks = tasks
        self._subscribers: set[_Subscriber] = set()
        self._history: list[Record] = []
        self._status = "idle"
        self._available = True
        self._submission: asyncio.Task[None] | None = None
        self._assistant_parts: list[str] = []
        self._active_tools: set[str] = set()

    def subscribe(self) -> _Subscriber:
        client = _Subscriber()
        # Snapshot and subscription must happen without yielding to the Pi event reader.
        client.send({"type": "history", "messages": self._history.copy()})
        client.send({"type": "status", "status": self._status})
        if not self._available:
            client.send({"type": "error", "code": "pi_unavailable"})
        self._subscribers.add(client)
        return client

    def unsubscribe(self, client: _Subscriber) -> None:
        self._subscribers.remove(client)
        client.overflow.cancel()

    def submit(self, client: _Subscriber, text: str) -> None:
        if not self._available:
            client.send({"type": "error", "code": "pi_unavailable"})
            return
        submitting = self._submission is not None and not self._submission.done()
        if self._status != "idle" or submitting:
            client.send({"type": "error", "code": "busy"})
            return
        self._set_status("thinking")
        # The Pi turn belongs to the session, not to the socket that submitted it.
        self._submission = self._tasks.create_task(self._send_prompt(client, text))

    def cancel_submission(self) -> None:
        if self._submission is not None:
            self._submission.cancel()

    async def _send_prompt(self, client: _Subscriber, text: str) -> None:
        try:
            reply = await self._pi.prompt(text, queue_if_busy=False)
        except PiRpcRejected as error:
            logger.warning("Pi rejected a prompt: %s", error)
            client.send({"type": "error", "code": "prompt_failed"})
            self._set_status("idle")
            return
        except PiRpcError as error:
            self._mark_unavailable(error)
            return
        if reply.get("disposition") == "handled":
            self._set_status("idle")

    async def read_events(self) -> None:
        try:
            async for event in self._pi.events():
                if self._available:
                    self._apply_event(event)
        except PiRpcError as error:
            self._mark_unavailable(error)

    def _apply_event(self, event: Record) -> None:
        match event["type"]:
            case "message_end":
                self._record_message(event.get("message"))
            case "tool_execution_start" | "tool_execution_end":
                tool_id = event.get("toolCallId")
                if isinstance(tool_id, str):
                    if event["type"] == "tool_execution_start":
                        self._active_tools.add(tool_id)
                    else:
                        self._active_tools.discard(tool_id)
                    self._set_status("tool_call" if self._active_tools else "thinking")
            case "agent_settled":
                if self._assistant_parts:
                    text = "\n\n".join(self._assistant_parts)
                    self._history.append({"role": "assistant", "text": text})
                    self._broadcast({"type": "assistant_message", "text": text})
                self._assistant_parts.clear()
                self._active_tools.clear()
                self._set_status("idle")

    def _record_message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        text = _message_text(message.get("content"))
        if not text:
            return
        match message.get("role"):
            case "user":
                self._history.append({"role": "user", "text": text, "source": "web"})
                self._broadcast({"type": "user_message", "text": text, "source": "web"})
            case "assistant":
                if message.get("stopReason") not in ("error", "aborted"):
                    self._assistant_parts.append(text)

    def _mark_unavailable(self, error: PiRpcError) -> None:
        if not self._available:
            return
        logger.error("Pi RPC is unavailable: %s", error)
        self._available = False
        self._assistant_parts.clear()
        self._active_tools.clear()
        self._broadcast({"type": "error", "code": "pi_unavailable"})
        self._set_status("idle")

    def _set_status(self, status: str) -> None:
        if self._status != status:
            self._status = status
            self._broadcast({"type": "status", "status": status})

    def _broadcast(self, event: Record) -> None:
        for client in self._subscribers:
            client.send(event)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)


async def _websocket(websocket: WebSocket) -> None:
    # Binding to localhost does not stop an unrelated website from opening a socket.
    origin = websocket.headers.get("origin")
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    if origin is not None and origin != f"{scheme}://{websocket.url.netloc}":
        await websocket.close(code=1008)
        return

    chat: _ChatSession = websocket.state.chat
    await websocket.accept()
    client = chat.subscribe()
    sender = asyncio.create_task(_send(websocket, client))
    receiver = asyncio.create_task(_receive(websocket, chat, client))
    try:
        finished, _ = await asyncio.wait(
            (sender, receiver, client.overflow), return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        chat.unsubscribe(client)
        sender.cancel()
        receiver.cancel()
        results = await asyncio.gather(sender, receiver, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(
            result, WebSocketDisconnect
        ):
            raise result
    if client.overflow in finished:
        with suppress(WebSocketDisconnect):
            await websocket.close(code=1013)


async def _send(websocket: WebSocket, client: _Subscriber) -> None:
    while True:
        await websocket.send_json(await client.outbox.get())


async def _receive(
    websocket: WebSocket, chat: _ChatSession, client: _Subscriber
) -> None:
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            return
        text = _read_prompt(frame.get("text"))
        if text is None:
            client.send({"type": "error", "code": "invalid_prompt"})
            continue
        chat.submit(client, text)


def _read_prompt(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "prompt":
        return None
    text = payload.get("text")
    return text if isinstance(text, str) and text.strip() else None
