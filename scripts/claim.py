"""Claim a fresh fileapi deployment: python scripts/claim.py https://<host>"""

import sys

import anyio
from mcp import Client


async def main(base: str) -> bool:
    async with Client(base.rstrip("/") + "/mcp") as c:
        r = await c.call_tool("claim_server", {})
    print(r.content[0].text)
    return not r.is_error


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/claim.py https://<host>")
    sys.exit(0 if anyio.run(main, sys.argv[1]) else 1)
