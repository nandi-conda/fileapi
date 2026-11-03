"""ASGI entrypoint for `fastapi run` / `fastapi deploy` (FastAPI Cloud). Configured via environment variables.

This entrypoint refuses to start on ephemeral local disk: FastAPI Cloud replaces the disk on every deploy,
restart and scale event, which would silently wipe the files and the claimed token. Set DATABASE_URL, or
set FILEAPI_ROOT explicitly to say a local disk is what you want (e.g. on your own server).
"""

import os

from .app import create_app

if not os.environ.get("DATABASE_URL") and not os.environ.get("FILEAPI_ROOT"):
    raise RuntimeError(
        "No durable storage configured: DATABASE_URL is not set. On FastAPI Cloud the local disk is wiped on "
        "every deploy, so files and the access token would be lost. Run `just cloud-db` to set DATABASE_URL, "
        "or set FILEAPI_ROOT to use a local disk you know is persistent."
    )

app = create_app()
