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
   - Both the server and the one-shot CLI run `pi` with all tools disabled (`--no-tools`). Tools can be enabled later as needed.
   - Each startup creates a new session by default (persisted to disk by `pi`).
   - The `pi` session is the source of truth for conversation history.
   - The server accepts only one prompt at a time. While a turn is in progress, new prompts are rejected, never queued, and never added to the conversation.

2. **WebSocket Protocol (Browser <-> Python Server)**:
   - **Client -> Server**:
     - `{"type": "prompt", "text": "..."}`
   - **Server -> Connecting Client**:
     - `{"type": "history", "messages": [...]}`, followed by the current `status`. History excludes partial assistant text.
   - **Server -> Submitting Client**:
     - `{"type": "prompt_accepted"}` after `pi` accepts the prompt.
     - `{"type": "error", "code": "busy"}` when a prompt is rejected because a turn is in progress. Invalid prompts and rejections from `pi` use `invalid_prompt` and `prompt_failed` respectively.
   - **Server -> All Connected Clients**:
     - `{"type": "error", "code": "pi_unavailable"}` if the RPC session becomes unavailable. New connections to an unavailable session also receive this error.
     - `{"type": "user_message", "text": "...", "source": "web" | "qq"}`
     - `{"type": "assistant_message", "text": "..."}` with complete assistant text after the turn settles.
     - `{"type": "status", "status": "idle" | "thinking" | "tool_call"}`

3. **Message Source Tracking (`web` vs `qq`)**:
   - Every user message displays its origin (`web` or `qq`) in the Web UI.
   - Messages originating from QQ are broadcast in real time to any open Web UI tabs, and the full conversation history preserves the source tag.

---

## 4. Phased Implementation Roadmap

> **Instructions for implementing agents**: Do NOT implement the entire plan in a single task. Execute **one phase (and one task within that phase) at a time**, verify it works, and check with the user before moving to the next task.

### Phase 1: Local Web Chat + `pi` RPC Core (Current Target)

Goal: Deliver a working local web chat connected to a single `pi --mode rpc` session, with minimal raw HTML and Vanilla TypeScript.

- **Task 1.1: Python Project & `pi` RPC Bridge**
  - Initialize the Python project with `uv` and a CLI entry point.
  - Implement `PiRpcClient` around `asyncio.create_subprocess_exec("pi", "--mode", "rpc")`.
  - Note: Split `stdout` strictly on `\n` (LF), not generic Unicode line separators, per `pi` RPC specification.
  - Support sending `prompt`, `follow_up`, and `get_messages`, and consuming `message_update`, tool execution events, and `agent_settled`. Phase 1's WebSocket endpoint does not use follow-up queueing.

- **Task 1.2: FastAPI Server & WebSocket Hub**
  - Build the FastAPI server and WebSocket endpoint (`/ws`). Preserve the existing one-shot CLI and add `uv run mydays-bot --serve`, which listens only on `127.0.0.1:8000` during local development.
  - Accept only `127.0.0.1:8000` and `localhost:8000` as WebSocket `Host` values. If a request includes an `Origin`, it must match the server origin.
  - Send Pi-backed conversation history and the current status on connect. Accept one prompt at a time and report busy rejections only to the submitting client.
  - Broadcast user messages and status changes to all connected clients. After the turn settles, broadcast the complete assistant reply without streaming text deltas.

- **Task 1.3: Minimal HTML + TypeScript Frontend**
  - Set up a minimal Vite + Vanilla TypeScript frontend (unstyled plain HTML).
  - Render history, live user messages, complete assistant replies, and the status indicator (`Thinking...`, `Tool Call...`). Show a busy notice when a prompt is rejected.
  - Allow drafting while the session is busy, but disable sending. Make the draft read-only while a submission is pending; clear it on acceptance, or leave it intact on rejection or disconnect. To reconnect, refresh the page; unsent drafts are not persisted across refreshes.
  - Serve Vite's build from FastAPI at `/`, with a same-origin `/ws` connection. LAN deployment and bundling frontend assets in the Python wheel are deferred.

#### Run the local web chat

Build the frontend from the repository root with Node.js 22.12 or later:

```sh
npm --prefix frontend ci
npm --prefix frontend run build
uv run mydays-bot --serve
```

Open `http://127.0.0.1:8000/`. Node.js is only needed to build the frontend. If the frontend was not built before startup, `/` explains how to build it and `/ws` remains available; restart the server after the build.

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
- **Task 3.3**: Stream assistant text to WebSocket clients.
- **Task 3.4**: Queue prompts while `pi` is busy instead of rejecting them.
