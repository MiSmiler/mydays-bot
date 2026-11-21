type MessageSource = "web" | "qq";
type SessionStatus = "idle" | "thinking" | "tool_call";
type ErrorCode = "busy" | "invalid_prompt" | "prompt_failed" | "pi_unavailable";

type ChatMessage =
  | { role: "user"; text: string; source: MessageSource }
  | { role: "assistant"; text: string };

type ServerMessage =
  | { type: "history"; messages: ChatMessage[] }
  | { type: "user_message"; text: string; source: MessageSource }
  | { type: "assistant_message"; text: string }
  | { type: "status"; status: SessionStatus }
  | { type: "prompt_accepted" }
  | { type: "error"; code: ErrorCode };

const statusLabels = {
  idle: "Idle",
  thinking: "Thinking...",
  tool_call: "Tool Call...",
};
const sourceLabels = { web: "Web", qq: "QQ" };
const errorNotices = {
  busy: "The session is busy. Try again when it is idle.",
  invalid_prompt: "Enter a non-empty message.",
  prompt_failed: "Pi rejected the message. Try again.",
  pi_unavailable: "Pi is unavailable. Restart the server, then refresh the page.",
};

const sessionIndicator = document.querySelector("#session-status")!;
const messages = document.querySelector("#messages")!;
const form = document.querySelector<HTMLFormElement>("#prompt-form")!;
const promptInput = document.querySelector<HTMLTextAreaElement>("#prompt")!;
const send = form.querySelector<HTMLButtonElement>("button")!;
const notice = document.querySelector("#notice")!;
const socket = new WebSocket("/ws");

let sessionStatus: SessionStatus | undefined;
let awaitingResult = false;
let piUnavailable = false;

function updateControls(): void {
  const isConnected = socket.readyState === WebSocket.OPEN;
  const isIdle = sessionStatus === "idle" && !piUnavailable;
  const hasDraft = promptInput.value.trim() !== "";
  promptInput.readOnly = awaitingResult;
  send.disabled = !isConnected || !isIdle || awaitingResult || !hasDraft;
}

function appendMessage(message: ChatMessage): void {
  const item = document.createElement("li");
  const label = document.createElement("strong");
  label.className = "message-role";
  label.textContent = message.role === "user"
    ? `User (${sourceLabels[message.source]})`
    : "Assistant";

  const body = document.createElement("p");
  body.className = "message-body";
  body.textContent = message.text;
  item.append(label, body);
  messages.append(item);
}

promptInput.addEventListener("input", updateControls);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (send.disabled || socket.readyState !== WebSocket.OPEN) return;

  awaitingResult = true;
  notice.textContent = "";
  updateControls();
  socket.send(JSON.stringify({ type: "prompt", text: promptInput.value }));
});

socket.addEventListener("message", (event: MessageEvent<string>) => {
  const message: ServerMessage = JSON.parse(event.data);
  switch (message.type) {
    case "history":
      messages.replaceChildren();
      for (const entry of message.messages) appendMessage(entry);
      break;
    case "user_message":
      appendMessage({ ...message, role: "user" });
      break;
    case "assistant_message":
      appendMessage({ ...message, role: "assistant" });
      break;
    case "status":
      sessionStatus = message.status;
      if (!piUnavailable) sessionIndicator.textContent = statusLabels[sessionStatus];
      break;
    case "prompt_accepted":
      if (awaitingResult) promptInput.value = "";
      awaitingResult = false;
      break;
    case "error":
      awaitingResult = false;
      notice.textContent = errorNotices[message.code];
      if (message.code === "pi_unavailable") {
        piUnavailable = true;
        sessionIndicator.textContent = "Pi unavailable";
      }
      break;
  }
  updateControls();
});

socket.addEventListener("close", () => {
  awaitingResult = false;
  sessionIndicator.textContent = "Connection lost. Refresh the page to reconnect.";
  updateControls();
});
