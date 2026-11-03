"""ASGI entrypoint for `fastapi run` / `fastapi deploy` (FastAPI Cloud). Configured via environment variables."""

from .app import create_app

app = create_app()
