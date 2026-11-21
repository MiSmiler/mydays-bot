import argparse
import asyncio
import json
import sys

import uvicorn

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError
from mydays_bot.server import SERVER_HOST, SERVER_PORT, create_app


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
    parser.add_argument(
        "--serve", action="store_true", help="Start the WebSocket server."
    )
    args = parser.parse_args()
    if args.serve:
        if args.message is not None:
            parser.error("--serve does not accept a prompt")
        uvicorn.run(create_app(), host=SERVER_HOST, port=SERVER_PORT)
        return 0

    try:
        asyncio.run(_run(args.message))
    except PiRpcError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
