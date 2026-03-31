# Alfred rebuild stack

## Start command

```bash
uvicorn app.rebuild_stack:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.stack
```

## O que entra nessa stack
- `app/rebuild_stack.py`
- `app/services/alfred_brain_unified.py`
- `app/routers/webhook_unified.py`
- `app/routers/internal_whatsapp_unified.py`
- `app/routers/whatsapp.py`
- `app/routers/dashboard_rebuild.py`
- `app/services/tomorrow_board.py`
- `app/cron/morning_briefing_rebuild.py`
- `app/cron/midday_checkin_rebuild.py`
- `app/cron/scheduler_rebuild.py`

## Rotas principais
- `POST /webhook`
- `POST /internal/whatsapp/inbound`
- `POST /whatsapp/inbound`
- `GET /dashboard/state`
- `GET /dashboard/tomorrow`
- `GET /health`

## Smoke test
1. subir com `app.rebuild_stack:app`
2. validar `GET /health`
3. testar `POST /whatsapp/inbound`
4. testar `POST /internal/whatsapp/inbound`
5. testar `POST /webhook`
6. validar `GET /dashboard/state`
7. validar `GET /dashboard/tomorrow`

## Observação
A stack rebuild foi montada em paralelo ao legado porque esta sessão permitiu criar arquivos novos no GitHub, mas bloqueou a sobrescrita de alguns arquivos existentes do fluxo antigo.
