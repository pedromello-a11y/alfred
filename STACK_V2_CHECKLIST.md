# Stack V2 — checklist de ativação

## 1. Start command

```bash
uvicorn app.stack_entry_v2:app --host 0.0.0.0 --port $PORT
```

Ou use:

```bash
Procfile.v2
```

## 2. Limpar task lixo antes do primeiro teste (opcional, recomendado)

```bash
python scripts/stack_clean.py
```

## 3. Validar health

```bash
GET /health
```

## 4. Validar entradas
- `POST /whatsapp/inbound`
- `POST /internal/whatsapp/inbound`
- `POST /webhook`

## 5. Validar dashboard
- `GET /dashboard/state`
- `GET /dashboard/focus`
- `GET /dashboard/tomorrow`

## 6. Rodar smoke test

```bash
python scripts/test_stack_v2.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
```

## 7. Confirmar no banco
- grava inbound em `messages`
- grava outbound em `messages`
- evita duplicata por `message_id`
- mantém classification preenchida

## 8. Cron esperado
- 07:00 preview
- 09:00 briefing
- 09:30 nudge
- 10:00 nudge final
- 13:00 check-in
- 14:00 plano B
- 21:00 fechamento
