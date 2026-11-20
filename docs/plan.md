# mydays-bot Architecture & Implementation Plan

## 1. Project Overview

Build a self-hosted personal chat bot server running on a home miniPC, accessible across the local network via a web browser and externally via a QQ Bot.

The server wraps `pi` (`pi --mode rpc`) as its underlying agent engine. It maintains **a single active session** in the foreground: both the LAN Web UI and the QQ Bot connect to this same session as parallel input/output channels.

## 2. Tech Stack

- **Backend**: Python (managed by `uv`), `FastAPI` + `uvicorn`, packaged with a CLI entry point.
- **Agent Engine**: `pi --mode rpc` running as a long-lived asynchronous subprocess (`asyncio.create_subprocess_exec`).
- **Frontend**: Plain HTML + Vanilla TypeScript (bundled with Vite; no CSS or UI framework initially).
- **Web <-> Server Transport**: WebSocket.
- **QQ <-> Server Transport**: Official QQ Bot (`bot.q.qq.com`) outbound WebSocket client connection (no public IP or domain required on the miniPC).

## 3. Core Architecture

```
[ Web Browser(s) ] ──(WebSocket)──┐
                                  ▼
                            [ Python Server ] <──(stdin/stdout JSONL)──> [ pi --mode rpc ]
                                  ▲
[ QQ Official Bot ] ──(QQ WS API)─┘
```

### Key Design Decisions

1. **Single Active Session**:
   - The Python server spawns one `pi --mode rpc` subprocess on startup.
   - Each startup creates a new session by default (persisted to disk by `pi`).
   - If a new user prompt arrives while `pi` is busy streaming a response, the server sends it as a `follow_up` command (or queues it) rather than failing.

2. **WebSocket Protocol (Browser <-> Python Server)**:
   - **Client -> Server**:
     - `{"type": "prompt", "text": "..."}`
   - **Server -> Client (Broadcast to all connected browsers)**:
     - `{"type": "history", "messages": [...]}` (sent on initial WebSocket connection)
     - `{"type": "user_message", "text": "...", "source": "web" | "qq"}`
     - `{"type": "assistant_delta", "delta": "..."}`
     - `{"type": "status", "status": "idle" | "thinking" | "tool_call"}`

3. **Message Source Tracking (`web` vs `qq`)**:
   - Every user message displays its origin (`web` or `qq`) in the Web UI.
   - Messages originating from QQ are broadcast in real time to any open Web UI tabs, and the full conversation history preserves the source tag.

---

## 4. Phased Implementation Roadmap

> **Instructions for implementing agents**: Do NOT implement the entire plan in a single task. Execute **one phase (and one task within that phase) at a time**, verify it works, and check with the user before moving to the next task.

### Phase 1: LAN Web Chat + `pi` RPC Core (Current Target)

Goal: Deliver a working local web chat connected to a single `pi --mode rpc` session, with minimal raw HTML and Vanilla TypeScript.

- **Task 1.1: Python Project & `pi` RPC Bridge**
  - Initialize the Python project with `uv` and a CLI entry point.
  - Implement `PiRpcClient` around `asyncio.create_subprocess_exec("pi", "--mode", "rpc")`.
  - Note: Split `stdout` strictly on `\n` (LF), not generic Unicode line separators, per `pi` RPC specification.
  - Support sending `prompt`, `follow_up` (when busy), and `get_messages`, and consuming `message_update`, tool execution events, and `agent_settled`.

- **Task 1.2: FastAPI Server & WebSocket Hub**
  - Build the FastAPI server and WebSocket endpoint (`/ws`).
  - Maintain connected WebSocket clients, send conversation history on connect, accept user prompts, and broadcast `user_message`, `assistant_delta`, and `status` (`idle` / `thinking` / `tool_call`) events from `pi`.

- **Task 1.3: Minimal HTML + TypeScript Frontend**
  - Set up a minimal Vite + Vanilla TypeScript frontend (unstyled plain HTML).
  - Render history, live user messages, streaming assistant text deltas, and the status indicator (`Thinking...`, `Tool Call...`).
  - Serve or proxy the frontend so it can be accessed from any browser on the LAN.

---

### Phase 2: QQ Bot Integration (Deferred until Phase 1 is verified)

Goal: Connect the official QQ Bot (`bot.q.qq.com`) to the same active `pi` session.

- **Task 2.1: Message Source Persistence**
  - finalize how `source: "web" | "qq"` is stored alongside session messages (either via prefix tagging in the prompt or sidecar/custom entry mapping) so history reloads preserve the `[QQ]` badge in the Web UI.

- **Task 2.2: QQ Bot Channel Adapter**
  - Connect to the official QQ Bot WebSocket gateway from the Python server.
  - When a QQ message arrives: forward it to `PiRpcClient` and broadcast `{"type": "user_message", "text": "...", "source": "qq"}` to all Web UI clients.
  - When `pi` emits `agent_settled` for a QQ-initiated (or shared) turn: collect the final assistant text and send it back to QQ via the QQ Bot API.

---

### Phase 3: Enhancements (Future)

- **Task 3.1**: CLI `--resume` support to restore previous `pi` sessions on server startup.
- **Task 3.2**: Web UI styling and expandable details for `Thinking` and `Tool Call` blocks.
