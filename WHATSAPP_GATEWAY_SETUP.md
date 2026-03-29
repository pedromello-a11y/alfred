# Setup do gateway WhatsApp do Alfred

## O que entrou no repo
- `wa_gateway/index.js`
- `wa_gateway/session_store.js`
- `wa_gateway/state.js`
- `wa_gateway/package.json`
- `wa_gateway/.gitignore`
- `app/routers/internal_whatsapp.py`
- `app/main_gateway.py`

## O que foi ajustado no app Python
- `app/config.py`
  - `whapi_token` e `pedro_phone` ficaram opcionais
  - adicionados `wa_bridge_shared_secret` e `wa_gateway_url`
- `app/routers/__init__.py`
  - exporta `internal_whatsapp`
- `app/main_gateway.py`
  - sobe o app com a rota interna do gateway
  - mantém startup do scheduler e checagem de jobs perdidos

## Como subir no Railway

### Serviço 1 — Python / FastAPI
Use este start command:

```bash
uvicorn app.main_gateway:app --host 0.0.0.0 --port $PORT
```

Variáveis mínimas:
- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `WA_BRIDGE_SHARED_SECRET`
- `WA_GATEWAY_URL=https://SEU-SERVICO-GATEWAY.up.railway.app`

Observação:
- `WHAPI_TOKEN` e `PEDRO_PHONE` só são necessários se você quiser manter o fluxo legado do Whapi.

### Serviço 2 — Node / Gateway
Crie um novo serviço apontando para `wa_gateway/`.

Start command:

```bash
npm start
```

Variáveis mínimas:
- `DATABASE_URL`
- `ALFRED_API_URL=https://SEU-SERVICO-PYTHON.up.railway.app`
- `WA_BRIDGE_SHARED_SECRET=um-segredo-forte`
- `ALFRED_GROUP_NAME=Alfred` ou `ALFRED_CHAT_ID=<chat_id>`
- `WA_SESSION_ID=alfred`
- `WA_SESSION_TABLE=whatsapp_sessions`

Variáveis opcionais:
- `ALFRED_OUTBOUND_CHAT_ID`
- `PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable`

## Fluxo esperado
1. Mensagem chega no WhatsApp Web
2. Gateway recebe no `message_create`
3. Gateway chama `POST /internal/whatsapp/inbound` no FastAPI
4. FastAPI processa com `message_handler`
5. Gateway recebe `reply` e responde no mesmo chat
6. Cron jobs e outras saídas proativas usam `WA_GATEWAY_URL/send`

## Checklist de validação
1. Subir os dois serviços
2. Abrir `/qr` no gateway
3. Escanear uma vez
4. Confirmar `/health` do gateway com `botStatus=ready`
5. Enviar mensagem no chat/grupo permitido
6. Confirmar resposta do Alfred
7. Testar uma saída proativa manual ou cron
