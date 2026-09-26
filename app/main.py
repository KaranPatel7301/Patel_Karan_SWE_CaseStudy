import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db import init_db

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)
    app_logger.propagate = False


configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    tickers = ", ".join(company.ticker for company in settings.companies)
    logger.info("schema ready; universe from %s: %s", settings.companies_path, tickers)
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Fundamentals Tracker", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()
