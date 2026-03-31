# Pós-deploy — Stack V4

## Stack ativa esperada
- entrypoint: `app.stack_entry_v4:app`
- procfile: `Procfile.v4`

## Verificações imediatas
1. `GET /health` responde 200
2. `GET /dashboard/state` responde 200
3. `GET /dashboard/focus` responde 200
4. `GET /dashboard/tomorrow` responde 200
5. `POST /whatsapp/inbound` responde `status=ok` ou `ignored`
6. `POST /internal/whatsapp/inbound` responde `status=ok` ou `duplicate`
7. `POST /webhook` responde `status=ok`

## Banco
Confirmar em `messages`:
- grava inbound
- grava outbound
- `classification` preenchida
- deduplicação por `message_id` funcionando

## Scheduler
Confirmar que iniciou sem erro e que os jobs estão registrados:
- 07:00 preview
- 09:00 briefing
- 09:30 nudge
- 10:00 nudge final
- 13:00 check-in
- 14:00 plano B
- 21:00 fechamento

## Se algo quebrar
1. voltar temporariamente para a stack anterior usada no Railway
2. comparar erro com os módulos da v4:
   - `app/stack_entry_v4.py`
   - `app/routers/webhook_v2.py`
   - `app/routers/internal_whatsapp_v2.py`
   - `app/routers/wa_in_v3.py`
   - `app/routers/dashboard_v2.py`
