import argparse
import asyncio
import json
import sys

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError


async def _run(message: str | None) -> None:
    async with PiRpcClient() as pi:
        if message is None:
            print(json.dumps(await pi.get_messages()))
            return

        result = await pi.prompt(message)
        if result.get("disposition") == "handled":
            return

        async for event in pi.events():
            print(json.dumps(event), flush=True)
            if event["type"] == "agent_settled":
                return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Pi RPC session.")
    parser.add_argument(
        "message",
        nargs="?",
        help="Send a prompt and print its JSONL events. With no prompt, print history.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.message))
    except PiRpcError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
