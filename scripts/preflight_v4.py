import importlib

EXPECTED = {
    "/health",
    "/webhook",
    "/internal/whatsapp/inbound",
    "/whatsapp/inbound",
    "/dashboard/state",
    "/dashboard/focus",
    "/dashboard/tomorrow",
    "/",
}

MODULES = [
    "app.stack_entry_v4",
    "app.routers.webhook_v2",
    "app.routers.internal_whatsapp_v2",
    "app.routers.wa_in_v3",
    "app.routers.dashboard_v2",
    "app.services.alfred_brain_v2",
    "app.services.focus_snapshot",
    "app.services.tomorrow_board",
    "app.cron.final_jobs",
]


def check_imports():
    loaded = []
    for name in MODULES:
        importlib.import_module(name)
        loaded.append(name)
    return loaded


def check_routes():
    mod = importlib.import_module("app.stack_entry_v4")
    app = mod.app
    paths = {getattr(route, "path", None) for route in app.routes}
    paths.discard(None)
    missing = sorted(EXPECTED - paths)
    present = sorted(EXPECTED & paths)
    return {"present": present, "missing": missing, "all_paths": sorted(paths)}


if __name__ == "__main__":
    imports = check_imports()
    routes = check_routes()
    print({"ok": not routes["missing"], "imports": imports, "routes": routes})
