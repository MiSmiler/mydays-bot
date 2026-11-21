import argparse
import asyncio
import json
import sys

import uvicorn

from mydays_bot.pi_rpc import PiRpcClient, PiRpcError
from mydays_bot.server import create_app


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
    parser.add_argument("--host", help="Server bind address (default: 127.0.0.1).")
    args = parser.parse_args()
    if args.serve:
        if args.message is not None:
            parser.error("--serve does not accept a prompt")
        uvicorn.run(create_app(), host=args.host or "127.0.0.1")
        return 0
    if args.host is not None:
        parser.error("--host requires --serve")

    try:
        asyncio.run(_run(args.message))
    except PiRpcError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
