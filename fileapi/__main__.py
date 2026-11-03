"""Run the server: `python -m fileapi` (HTTP) or `python -m fileapi stdio`."""

import os
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        from .mcp_server import build_mcp
        from .storage import Storage

        storage = Storage(os.environ.get("FILEAPI_ROOT", "./data"))
        storage.init()
        build_mcp(storage).run("stdio")
        return

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
