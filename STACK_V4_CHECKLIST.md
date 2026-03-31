# Stack V4 — checklist

## Start command

```bash
uvicorn app.stack_entry_v4:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.v4
```

## Antes do primeiro teste

```bash
python scripts/stack_clean.py
```

## Smoke test

```bash
python scripts/test_v3_fix.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
```

## Validar
- `GET /health`
- `POST /webhook`
- `POST /internal/whatsapp/inbound`
- `POST /whatsapp/inbound`
- `GET /dashboard/state`
- `GET /dashboard/focus`
- `GET /dashboard/tomorrow`

## Cron esperado
- 07:00 preview
- 09:00 briefing
- 09:30 nudge
- 10:00 nudge final
- 13:00 check-in
- 14:00 plano B
- 21:00 fechamento
