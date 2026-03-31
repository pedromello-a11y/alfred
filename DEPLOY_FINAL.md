# Alfred final stack

## Start command

```bash
uvicorn app.stack_entry:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.final
```

## Stack ativa
- `app/stack_entry.py`
- `app/cron/final_jobs.py`
- `app/cron/morning_briefing_rebuild.py`
- `app/cron/midday_checkin_rebuild.py`
- `app/cron/end_of_day_stack.py`
- `app/services/alfred_brain_unified.py`
- `app/services/tomorrow_board.py`
- `app/routers/webhook_unified.py`
- `app/routers/internal_whatsapp_unified.py`
- `app/routers/whatsapp.py`
- `app/routers/dashboard_rebuild.py`

## Rotas
- `GET /health`
- `POST /webhook`
- `POST /internal/whatsapp/inbound`
- `POST /whatsapp/inbound`
- `GET /dashboard/state`
- `GET /dashboard/tomorrow`

## Limpeza da base
```bash
python scripts/stack_clean.py
```
