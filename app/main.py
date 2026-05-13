"""FastAPI app bootstrap. Routers land in M2/M3/M4."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.config import settings
from app.errors import HubError, hub_error_handler
from app.jobs.scheduler import start_scheduler
from app.internal.api import router as internal_router
from app.reports.api import router as reports_router
from app.reports.render import iframe_router, viewer_router


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
    scheduler = start_scheduler()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("hub shutting down")


app = FastAPI(
    title="Spectrana Hub",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_exception_handler(HubError, hub_error_handler)  # type: ignore[arg-type]
app.include_router(reports_router)
app.include_router(iframe_router)
app.include_router(viewer_router)
app.include_router(internal_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
