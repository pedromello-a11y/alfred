"""Entry point único e definitivo do Alfred."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.routers.admin import router as admin_router
from app.routers.auth_google import router as auth_google_router
from app.routers.dashboard import router as dashboard_router
from app.routers.health import router as health_router
from app.routers.internal_whatsapp import router as internal_whatsapp_router
from app.routers.webhook import router as webhook_router
from app.routers.whatsapp import router as whatsapp_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    setup_jobs()
    scheduler.start()
    # Sync GCal on startup so calendar is populated immediately
    try:
        from app.cron.gcal_sync import run as gcal_sync_run
        await gcal_sync_run()
    except Exception:
        pass
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Alfred", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(internal_whatsapp_router)
app.include_router(whatsapp_router)
app.include_router(dashboard_router)
app.include_router(auth_google_router)
app.include_router(admin_router)


@app.get("/")
async def serve_dashboard():
    return FileResponse(Path(__file__).parent.parent / "alfred-dashboard.html")
