import asyncio
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError

pytestmark = pytest.mark.asyncio

PI_DOUBLE_PATH = Path(__file__).resolve().parent / "fixtures" / "pi_rpc_double.py"


async def test_launches_pi_with_fixed_model_and_without_tools_or_optional_resources(
    monkeypatch: pytest.MonkeyPatch,
):
    launch = AsyncMock(side_effect=FileNotFoundError("Pi executable not found"))
    with monkeypatch.context() as patches:
        patches.setattr(shutil, "which", lambda _executable: "pi")
        patches.setattr(asyncio, "create_subprocess_exec", launch)
        with pytest.raises(PiRpcError, match="Could not start Pi RPC"):
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
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-mcp",
        "--no-themes",
        "--no-context-files",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )


async def test_routes_replies_and_preserves_lf_framing():
    command = (sys.executable, "-u", str(PI_DOUBLE_PATH))
    async with asyncio.timeout(5), PiRpcClient(command) as pi:
        text = "First\u2028line\u2029last"
        assert await pi.prompt(text) == {"disposition": "started"}
        assert await pi.prompt("Second") == {"disposition": "queued"}

        async with asyncio.TaskGroup() as tasks:
            history = tasks.create_task(pi.get_messages())
            follow_up = tasks.create_task(pi.follow_up("Third"))
        assert follow_up.result() == {"disposition": "queued"}
        assert history.result()[0]["content"] == "x" * 150_000

        events = pi.events()
        delta = await anext(events)
        assert delta["assistantMessageEvent"]["delta"] == text
        for stage in ("start", "update", "end"):
            assert (await anext(events))["type"] == f"tool_execution_{stage}"
        assert (await anext(events))["type"] == "agent_settled"

    with pytest.raises(StopAsyncIteration):
        await anext(events)


async def test_prompt_can_opt_out_of_follow_up_on_the_wire():
    command = (sys.executable, "-u", str(PI_DOUBLE_PATH))
    async with PiRpcClient(command) as pi:
        reply = await pi.prompt("Do not queue", queue_if_busy=False)
        assert reply == {"disposition": "started"}


async def test_unexpected_exit_unblocks_command_and_events():
    command = (sys.executable, "-c", "import sys; sys.stdin.readline()")
    async with asyncio.timeout(5), PiRpcClient(command) as pi:
        with pytest.raises(PiRpcError, match="stdout closed unexpectedly"):
            await pi.get_messages()
        with pytest.raises(PiRpcError, match="stdout closed unexpectedly"):
            await anext(pi.events())


async def test_reports_nonzero_shutdown_status():
    command = (sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(7)")
    with pytest.raises(PiRpcError, match="exited with status 7"):
        async with PiRpcClient(command):
            pass
