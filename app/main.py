"""FastAPI app bootstrap. Routers land in M2/M3/M4."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.config import settings


logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("hub")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "hub starting | primary=%s reports=%s",
        settings.HUB_PRIMARY_DOMAIN,
        settings.HUB_REPORTS_DOMAIN,
    )
    yield
    logger.info("hub shutting down")


app = FastAPI(
    title="Spectrana Hub",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
