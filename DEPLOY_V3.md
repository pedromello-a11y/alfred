# Alfred stack v3

## Start command

```bash
uvicorn app.stack_entry_v3:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.v3
```

## Inclui
- `app/stack_entry_v3.py`
- `app/services/alfred_brain_v2.py`
- `app/services/focus_snapshot.py`
- `app/services/tomorrow_board.py`
- `app/routers/webhook_v2.py`
- `app/routers/internal_whatsapp_v2.py`
- `app/routers/whatsapp.py`
- `app/routers/dashboard_v2.py`
- `app/cron/final_jobs.py`

## Rotas
- `GET /health`
- `POST /webhook`
- `POST /internal/whatsapp/inbound`
- `POST /whatsapp/inbound`
- `GET /dashboard/state`
- `GET /dashboard/focus`
- `GET /dashboard/tomorrow`
