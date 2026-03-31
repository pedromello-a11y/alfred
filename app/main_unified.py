from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from loguru import logger

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.routers.dashboard import router as dashboard_router
from app.routers.health import router as health_router
from app.routers.webhook import router as webhook_router
from app.routers.whatsapp import router as whatsapp_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Alfred starting up...")
    await init_db()
    logger.info("Database initialized.")
    setup_jobs()
    scheduler.start()
    logger.info("Scheduler started.")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Alfred shutting down.")


app = FastAPI(title="Alfred", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(whatsapp_router)
app.include_router(dashboard_router)


@app.get("/")
async def serve_dashboard():
    return FileResponse(Path(__file__).parent.parent / "alfred-dashboard.html")
