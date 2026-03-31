import importlib

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

if __name__ == "__main__":
    loaded = []
    for name in MODULES:
        importlib.import_module(name)
        loaded.append(name)
    print({"ok": True, "modules": loaded})
