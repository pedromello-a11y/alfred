# Alfred — Deploy

## Start command

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Procfile

```
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Variáveis de ambiente obrigatórias

| Variável | Descrição |
|---|---|
| `DATABASE_URL` | PostgreSQL async (`postgresql+asyncpg://...`) |
| `ANTHROPIC_API_KEY` | Claude API key |

## Variáveis opcionais

| Variável | Descrição |
|---|---|
| `JIRA_BASE_URL` | Ex: `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Email Jira |
| `JIRA_API_TOKEN` | Token Jira |
| `GOOGLE_REFRESH_TOKEN` | Google Calendar |
| `GOOGLE_CLIENT_ID` | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | Google OAuth |
| `WHAPI_TOKEN` | Token Whapi |
| `WHAPI_API_URL` | Ex: `https://gate.whapi.cloud` |
| `MY_WHATSAPP` | Número Pedro (ex: `5521999999999`) |
| `WA_BRIDGE_SHARED_SECRET` | Secret bridge gateway |
| `WA_GATEWAY_URL` | URL gateway whatsapp-web.js |

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/` | Dashboard HTML |
| GET | `/dashboard/state` | Estado completo do dashboard |
| GET | `/dashboard/focus` | Focus snapshot |
| GET | `/dashboard/tomorrow` | Tomorrow board |
| POST | `/dashboard/action` | Ação em task (concluir, excluir, adiar) |
| POST | `/dashboard/create-task` | Criar task |
| POST | `/dashboard/task-edit` | Editar task |
| POST | `/webhook` | Webhook Whapi |
| POST | `/whatsapp/inbound` | Inbound bridge |
| POST | `/internal/whatsapp/inbound` | Inbound gateway interno |

## Cron jobs (seg-sex, horário BRT)

| Horário | Job |
|---|---|
| 07:00 | Preview matinal |
| 09:00 | Briefing completo |
| 09:30 | Nudge ritual |
| 10:00 | Nudge final |
| 13:00 | Check-in do meio-dia |
| 14:00 | Plano B |
| 21:00 | Fechamento noturno |

## Smoke test

```bash
python scripts/preflight_v4.py
```
