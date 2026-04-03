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
    "app.main",
    "app.routers.webhook",
    "app.routers.internal_whatsapp",
    "app.routers.whatsapp",
    "app.routers.dashboard",
    "app.services.brain",
    "app.services.focus_snapshot",
    "app.services.tomorrow_board",
    "app.cron.scheduler",
]


def check_imports():
    loaded = []
    for name in MODULES:
        importlib.import_module(name)
        loaded.append(name)
    return loaded


def check_routes():
    mod = importlib.import_module("app.main")
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
