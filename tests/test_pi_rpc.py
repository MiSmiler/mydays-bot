import asyncio
import sys
import unittest
from unittest.mock import patch

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError


PI_DOUBLE = r'''
import json
import sys


def send(record):
    line = json.dumps(record, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(line + b"\r\n")
    sys.stdout.buffer.flush()


def respond(request, data):
    send({"type": "response", "id": request["id"], "success": True, "data": data})


first_message = "First\u2028line\u2029last"
prompt_count = 0
history_request = None
for line in sys.stdin.buffer:
    request = json.loads(line)
    match request["type"]:
        case "prompt":
            assert request["streamingBehavior"] == "followUp"
            prompt_count += 1
            if prompt_count == 1:
                assert request["message"] == first_message
                send({"type": "message_update", "assistantMessageEvent": {
                    "type": "text_delta", "delta": first_message,
                }})
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
            respond(history_request, {
                "messages": [{"role": "assistant", "content": "x" * 150_000}],
            })
'''


class PiRpcClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_launches_pi_with_fixed_model_and_without_optional_resources(self):
        with (
            patch("mydays_bot.pi_rpc.shutil.which", return_value="pi"),
            patch(
                "mydays_bot.pi_rpc.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("Pi executable not found"),
            ) as launch,
        ):
            with self.assertRaisesRegex(PiRpcError, "Could not start Pi RPC"):
                async with PiRpcClient():
                    pass

        launch.assert_awaited_once_with(
            "pi",
            "--mode",
            "rpc",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-mcp",
            "--no-themes",
            "--no-context-files",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

    async def test_routes_replies_and_preserves_lf_framing(self):
        command = (sys.executable, "-u", "-c", PI_DOUBLE)
        async with asyncio.timeout(5), PiRpcClient(command) as pi:
            text = "First\u2028line\u2029last"
            self.assertEqual(await pi.prompt(text), {"disposition": "started"})
            self.assertEqual(await pi.prompt("Second"), {"disposition": "queued"})

            async with asyncio.TaskGroup() as tasks:
                history = tasks.create_task(pi.get_messages())
                follow_up = tasks.create_task(pi.follow_up("Third"))
            self.assertEqual(follow_up.result(), {"disposition": "queued"})
            self.assertEqual(history.result()[0]["content"], "x" * 150_000)

            events = pi.events()
            delta = await anext(events)
            self.assertEqual(delta["assistantMessageEvent"]["delta"], text)
            for stage in ("start", "update", "end"):
                self.assertEqual((await anext(events))["type"], f"tool_execution_{stage}")
            self.assertEqual((await anext(events))["type"], "agent_settled")

        with self.assertRaises(StopAsyncIteration):
            await anext(events)

    async def test_unexpected_exit_unblocks_command_and_events(self):
        command = (sys.executable, "-c", "import sys; sys.stdin.readline()")
        async with asyncio.timeout(5), PiRpcClient(command) as pi:
            with self.assertRaisesRegex(PiRpcError, "stdout closed unexpectedly"):
                await pi.get_messages()
            with self.assertRaisesRegex(PiRpcError, "stdout closed unexpectedly"):
                await anext(pi.events())

    async def test_reports_nonzero_shutdown_status(self):
        command = (sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(7)")
        with self.assertRaisesRegex(PiRpcError, "exited with status 7"):
            async with PiRpcClient(command):
                pass
