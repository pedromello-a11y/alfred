from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.routers import health, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Alfred starting up...")
    await init_db()
    logger.info("Database initialized.")
    setup_jobs()
    scheduler.start()
    logger.info("Scheduler started.")
    yield
    scheduler.shutdown(wait=False)
    logger.info("Alfred shutting down.")


app = FastAPI(title="Alfred", lifespan=lifespan)

app.include_router(health.router)
app.include_router(webhook.router)
