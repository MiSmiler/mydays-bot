import json
import sys


def send(record):
    line = json.dumps(record, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(line + b"\r\n")
    sys.stdout.buffer.flush()


def respond(request, data):
    send({"type": "response", "id": request["id"], "success": True, "data": data})


def main() -> None:
    first_message = "First\u2028line\u2029last"
    prompt_count = 0
    history_request = None
    for line in sys.stdin.buffer:
        request = json.loads(line)
        match request["type"]:
            case "prompt":
                if request["message"] == "Do not queue":
                    assert "streamingBehavior" not in request
                    respond(request, {"disposition": "started"})
                    continue
                assert request["streamingBehavior"] == "followUp"
                prompt_count += 1
                if prompt_count == 1:
                    assert request["message"] == first_message
                    send(
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {
                                "type": "text_delta",
                                "delta": first_message,
                            },
                        }
                    )
                    respond(request, {"disposition": "started"})
                else:
                    assert request["message"] == "Second"
                    respond(request, {"disposition": "queued"})
            case "get_messages":
                history_request = request
            case "follow_up":
                assert request["message"] == "Third"
                assert history_request is not None
                for stage in ("start", "update", "end"):
                    send({"type": f"tool_execution_{stage}", "toolCallId": "call-1"})
                send({"type": "agent_settled", "aborted": False})
                respond(request, {"disposition": "queued"})
                respond(
                    history_request,
                    {"messages": [{"role": "assistant", "content": "x" * 150_000}]},
                )


if __name__ == "__main__":
    main()
