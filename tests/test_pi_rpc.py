import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError

PI_DOUBLE_PATH = Path(__file__).resolve().parent / "fixtures" / "pi_rpc_double.py"


class PiRpcClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_launches_pi_with_fixed_model_and_without_optional_resources(self):
        with (
            patch("mydays_bot.pi_rpc.shutil.which", return_value="pi"),
            patch(
                "mydays_bot.pi_rpc.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("Pi executable not found"),
            ) as launch,
            self.assertRaisesRegex(PiRpcError, "Could not start Pi RPC"),
        ):
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
        command = (sys.executable, "-u", str(PI_DOUBLE_PATH))
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
                self.assertEqual(
                    (await anext(events))["type"], f"tool_execution_{stage}"
                )
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
