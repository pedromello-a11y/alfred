# Alfred stack v2

## Start command

```bash
uvicorn app.stack_entry_v2:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.v2
```

## Inclui
- `app/stack_entry_v2.py`
- `app/cron/final_jobs.py`
- `app/services/alfred_brain_unified.py`
- `app/services/focus_snapshot.py`
- `app/services/tomorrow_board.py`
- `app/routers/webhook_unified.py`
- `app/routers/internal_whatsapp_unified.py`
- `app/routers/whatsapp.py`
- `app/routers/dashboard_v2.py`

## Rotas
- `GET /health`
- `POST /webhook`
- `POST /internal/whatsapp/inbound`
- `POST /whatsapp/inbound`
- `GET /dashboard/state`
- `GET /dashboard/focus`
- `GET /dashboard/tomorrow`
