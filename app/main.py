"""Entry point único e definitivo do Alfred."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.services.alert_scheduler import start_scheduler
from app.routers.admin import router as admin_router
from app.routers.auth_google import router as auth_google_router
from app.routers.dashboard_state import router as dashboard_state_router
from app.routers.dashboard_tasks import router as dashboard_tasks_router
from app.routers.dashboard_agenda import router as dashboard_agenda_router
from app.routers.dashboard_misc import router as dashboard_misc_router
from app.routers.health import router as health_router
from app.routers.internal_whatsapp import router as internal_whatsapp_router
from app.routers.webhook import router as webhook_router
from app.routers.whatsapp import router as whatsapp_router

logger = logging.getLogger("alfred")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    setup_jobs()
    scheduler.start()
    start_scheduler()
    # Sync GCal on startup so calendar is populated immediately
    try:
        from app.cron.gcal_sync import run as gcal_sync_run
        await gcal_sync_run()
    except Exception:
        logger.exception("Erro ao sincronizar GCal no startup")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Alfred", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(internal_whatsapp_router)
app.include_router(whatsapp_router)
app.include_router(dashboard_state_router)
app.include_router(dashboard_tasks_router)
app.include_router(dashboard_agenda_router)
app.include_router(dashboard_misc_router)
app.include_router(auth_google_router)
app.include_router(admin_router)


@app.get("/")
async def serve_dashboard():
    html_path = Path(__file__).parent.parent / "alfred-dashboard.html"
    content = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
