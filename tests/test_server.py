import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError, PiRpcRejected, Record
from mydays_bot.server import create_app

LOCAL_SOCKET = "ws://127.0.0.1:8000/ws"


class ControlledPi(PiRpcClient):
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.prompts: list[tuple[str, bool]] = []
        self.replies: asyncio.Queue[Record | PiRpcError] = asyncio.Queue()
        self.incoming: asyncio.Queue[Record] = asyncio.Queue()

    async def __aenter__(self):
        self.started += 1
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.closed += 1

    async def prompt(self, message: str, *, queue_if_busy: bool = True) -> Record:
        self.prompts.append((message, queue_if_busy))
        reply = await self.replies.get()
        if isinstance(reply, PiRpcError):
            raise reply
        return reply

    async def events(self) -> AsyncIterator[Record]:
        while True:
            yield await self.incoming.get()


def _status(status: str) -> Record:
    return {"type": "status", "status": status}


def _history(*messages: Record) -> Record:
    return {"type": "history", "messages": list(messages)}


def _prompt(text: str) -> Record:
    return {"type": "prompt", "text": text}


def test_serves_frontend_and_websocket_from_the_same_origin(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir()
    page = '<script type="module" src="/assets/main.js"></script>'
    script = 'new WebSocket("/ws");'
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    (assets / "main.js").write_text(script, encoding="utf-8")

    origin = "http://127.0.0.1:8000"
    with TestClient(
        create_app(ControlledPi, frontend_dist=tmp_path), base_url=origin
    ) as client:
        assert client.get("/").text == page
        assert client.get("/assets/main.js").text == script
        with client.websocket_connect(LOCAL_SOCKET, headers={"origin": origin}) as ws:
            assert ws.receive_json() == _history()
            assert ws.receive_json() == _status("idle")


def test_unbuilt_frontend_explains_how_to_build_without_disabling_websocket(
    tmp_path: Path,
):
    with TestClient(create_app(ControlledPi, frontend_dist=tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 503
        assert "npm run build" in response.text
        with client.websocket_connect(LOCAL_SOCKET) as ws:
            assert ws.receive_json() == _history()
            assert ws.receive_json() == _status("idle")


def test_shares_one_turn_and_only_publishes_assistant_text_after_settlement():
    pi = ControlledPi()
    with TestClient(create_app(lambda: pi)) as client:
        portal = client.portal
        assert portal is not None

        def emit(event: Record) -> None:
            portal.call(pi.incoming.put_nowait, event)

        with (
            client.websocket_connect(LOCAL_SOCKET) as first,
            client.websocket_connect(LOCAL_SOCKET) as second,
        ):
            for socket in (first, second):
                assert socket.receive_json() == _history()
                assert socket.receive_json() == _status("idle")

            first.send_json(_prompt("First"))
            for socket in (first, second):
                assert socket.receive_json() == _status("thinking")
            second.send_json(_prompt("Not queued"))
            assert second.receive_json() == {"type": "error", "code": "busy"}

            portal.call(pi.replies.put_nowait, {"disposition": "started"})
            assert first.receive_json() == {"type": "prompt_accepted"}
            user = {"role": "user", "text": "Recorded by Pi", "source": "web"}
            emit(
                {
                    "type": "message_end",
                    "message": {"role": "user", "content": user["text"]},
                }
            )
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Partial"},
                }
            )
            emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Not for display"},
                            {"type": "text", "text": "Checking."},
                            {"type": "toolCall", "id": "tool-1"},
                        ],
                    },
                }
            )
            emit({"type": "tool_execution_start", "toolCallId": "tool-1"})
            emit({"type": "tool_execution_start", "toolCallId": "tool-2"})
            emit({"type": "tool_execution_end", "toolCallId": "tool-1"})
            for socket in (first, second):
                assert socket.receive_json() == {
                    "type": "user_message",
                    "text": user["text"],
                    "source": "web",
                }
                assert socket.receive_json() == _status("tool_call")

            with client.websocket_connect(LOCAL_SOCKET) as late:
                assert late.receive_json() == _history(user)
                assert late.receive_json() == _status("tool_call")
                emit({"type": "tool_execution_end", "toolCallId": "tool-2"})
                emit(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                )
                emit({"type": "agent_end"})
                for socket in (first, second, late):
                    assert socket.receive_json() == _status("thinking")

                with client.websocket_connect(LOCAL_SOCKET) as not_settled:
                    assert not_settled.receive_json() == _history(user)
                    assert not_settled.receive_json() == _status("thinking")

                    emit({"type": "agent_settled"})
                    text = "Checking.\n\nDone."
                    for socket in (first, second, late, not_settled):
                        assert socket.receive_json() == {
                            "type": "assistant_message",
                            "text": text,
                        }
                        assert socket.receive_json() == _status("idle")

            with client.websocket_connect(LOCAL_SOCKET) as reconnected:
                assert reconnected.receive_json() == _history(
                    user, {"role": "assistant", "text": text}
                )
                assert reconnected.receive_json() == _status("idle")
            assert pi.prompts == [("First", False)]
            assert pi.started == 1
    assert pi.closed == 1


def test_bad_prompts_and_pi_rejection_leave_session_available():
    pi = ControlledPi()
    with (
        TestClient(create_app(lambda: pi)) as client,
        client.websocket_connect(LOCAL_SOCKET) as socket,
    ):
        assert client.portal is not None
        assert socket.receive_json() == _history()
        assert socket.receive_json() == _status("idle")
        socket.send_text("not JSON")
        socket.send_json(_prompt("  "))
        socket.send_bytes(b"binary")
        for _ in range(3):
            assert socket.receive_json() == {"type": "error", "code": "invalid_prompt"}

        socket.send_json(_prompt("Rejected"))
        assert socket.receive_json() == _status("thinking")
        client.portal.call(pi.replies.put_nowait, PiRpcRejected("Rejected by Pi"))
        assert socket.receive_json() == {"type": "error", "code": "prompt_failed"}
        assert socket.receive_json() == _status("idle")

        socket.send_json(_prompt("Next"))
        assert socket.receive_json() == _status("thinking")
        client.portal.call(pi.replies.put_nowait, {"disposition": "handled"})
        assert socket.receive_json() == {"type": "prompt_accepted"}
        assert socket.receive_json() == _status("idle")
        assert pi.prompts == [("Rejected", False), ("Next", False)]


def test_turn_continues_after_the_submitting_socket_disconnects():
    pi = ControlledPi()
    with TestClient(create_app(lambda: pi)) as client:
        assert client.portal is not None
        with client.websocket_connect(LOCAL_SOCKET) as sender:
            assert sender.receive_json() == _history()
            assert sender.receive_json() == _status("idle")
            sender.send_json(_prompt("Question"))
            assert sender.receive_json() == _status("thinking")

        with client.websocket_connect(LOCAL_SOCKET) as observer:
            assert observer.receive_json() == _history()
            assert observer.receive_json() == _status("thinking")
            client.portal.call(pi.replies.put_nowait, {"disposition": "started"})
            for role, text in (("user", "Question"), ("assistant", "Reply")):
                client.portal.call(
                    pi.incoming.put_nowait,
                    {
                        "type": "message_end",
                        "message": {"role": role, "content": text},
                    },
                )
            client.portal.call(pi.incoming.put_nowait, {"type": "agent_settled"})
            assert observer.receive_json() == {
                "type": "user_message",
                "text": "Question",
                "source": "web",
            }
            assert observer.receive_json() == {
                "type": "assistant_message",
                "text": "Reply",
            }
            assert observer.receive_json() == _status("idle")


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000"])
def test_accepts_same_origin_connections_on_loopback(host: str):
    with (
        TestClient(create_app(ControlledPi)) as client,
        client.websocket_connect(
            f"ws://{host}/ws", headers={"origin": f"http://{host}"}
        ) as socket,
    ):
        assert socket.receive_json() == _history()
        assert socket.receive_json() == _status("idle")


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8000", "http://another.invalid:8000"),
        ("another.invalid:8000", "http://another.invalid:8000"),
        ("another.invalid:8000", None),
    ],
)
def test_rejects_untrusted_hosts_and_origins(host: str, origin: str | None):
    headers = {"origin": origin} if origin is not None else {}
    with (
        TestClient(create_app(ControlledPi)) as client,
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect(f"ws://{host}/ws", headers=headers),
    ):
        pytest.fail("Untrusted socket was accepted")
    assert rejected.value.code == 1008
