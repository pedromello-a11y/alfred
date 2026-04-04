# Alfred — Assistente pessoal

## Stack
- Backend: FastAPI + SQLAlchemy async + PostgreSQL
- Frontend: single-file HTML (alfred-dashboard.html)
- WhatsApp: gateway whatsapp-web.js

## Regras pra Claude Code
- Respostas curtas, só código
- Não muda arquivos que não foram pedidos
- Testes: pytest tests/ -x
- Frontend: alfred-dashboard.html (arquivo único)
- Backend entry: main.py
- Dashboard API: app/routers/dashboard.py
- Models: app/models.py
- Database: app/database.py

## Hierarquia de tasks
- Projeto > Entrega > Tarefa (3 níveis max)
- Tarefa avulsa = sem projeto (default)
- Status: active, on_holding, backlog, done, cancelled

## Agenda
- 7 dias (seg-dom)
- 3 tipos de bloco: gcal (intocável), fixado (user), automático (alfred)
- Espaçamento: fator = disponível / estimado (max 2.5x)
- Prioridade por deadline mais próximo
