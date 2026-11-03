"""Get a fileapi access token and store it in .env.

    python scripts/get_token.py https://<host> [current-token]

No current token: calls claim_server (works once, on a fresh server).
With the current token: calls rotate_token (the old token keeps working for 60 seconds).
The new token is written to .env as FILEAPI_ACCESS_TOKEN, and the URL as FILEAPI_URL.
"""

import os
import re
import sys
from pathlib import Path

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ENV = Path(".env")


async def get_token(base: str, current: str | None) -> tuple[str | None, str]:
    headers = {"Authorization": f"Bearer {current}"} if current else {}
    transport = streamable_http_client(f"{base}/mcp", http_client=httpx2.AsyncClient(headers=headers))
    async with Client(transport) as c:
        r = await c.call_tool("rotate_token" if current else "claim_server", {})
    text = r.content[0].text
    if r.is_error:
        return None, text
    m = re.search(r"\bfa_[A-Za-z0-9_-]+", text)
    return (m.group(0) if m else None), text


def write_env(values: dict[str, str]) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0] not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/").removesuffix("/mcp")
    current = sys.argv[2] if len(sys.argv) == 3 and sys.argv[2] else None
    token, text = anyio.run(get_token, base, current)
    if not token:
        print(text, file=sys.stderr)
        if not current and "already claimed" in text:
            print("Put the current token in .env as FILEAPI_ACCESS_TOKEN=... and run this again to rotate it.",
                  file=sys.stderr)
        return 1
    write_env({"FILEAPI_URL": base, "FILEAPI_ACCESS_TOKEN": token})
    print(f"{'Rotated' if current else 'Claimed'}: new token saved to .env as FILEAPI_ACCESS_TOKEN.")
    if current:
        print("The old token stops working in 60 seconds. Update your clients (just connect-info).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
