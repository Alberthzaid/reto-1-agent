"""FastAPI entrypoint. Boots without secrets or network access."""

import logging

from dotenv import load_dotenv
from fastapi import FastAPI

from agent.config import Settings
from agent.observability.log_setup import register_secret, setup_logging

log = logging.getLogger(__name__)

load_dotenv()
settings = Settings.from_env()
setup_logging(settings.log_level)
for _secret in (settings.anthropic_api_key, settings.team_token, settings.desktop_password):
    register_secret(_secret)

app = FastAPI(title="Agent-QA")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
