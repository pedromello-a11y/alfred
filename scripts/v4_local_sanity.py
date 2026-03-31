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

if __name__ == "__main__":
    mod = importlib.import_module("app.stack_entry_v4")
    app = mod.app
    paths = {getattr(route, "path", None) for route in app.routes}
    paths.discard(None)
    missing = sorted(EXPECTED - paths)
    present = sorted(EXPECTED & paths)
    print({"ok": not missing, "present": present, "missing": missing, "all_paths": sorted(paths)})
