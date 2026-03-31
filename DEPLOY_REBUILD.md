# Alfred rebuild — ativação

## Entry point novo
Use o app novo em vez do `app.main:app` legado.

### Start command
```bash
uvicorn app.main_rebuild:app --host 0.0.0.0 --port $PORT
```

Ou use o arquivo:

```bash
Procfile.rebuild
```

## Rotas ativas na versão rebuild
- `POST /webhook` → `app/routers/webhook_unified.py`
- `POST /internal/whatsapp/inbound` → `app/routers/internal_whatsapp_unified.py`
- `POST /whatsapp/inbound` → `app/routers/whatsapp.py`
- `GET /dashboard/*` → router atual do dashboard
- `GET /health`

## Cérebro usado
Todos os canais acima passam por:

```python
app/services/alfred_brain_unified.py
```

que centraliza o processamento em um único ponto antes do executor atual.

## Ordem de teste
1. subir com `app.main_rebuild:app`
2. testar `GET /health`
3. testar `POST /whatsapp/inbound`
4. testar `POST /internal/whatsapp/inbound`
5. testar `POST /webhook`
6. validar gravação de inbound/outbound em `messages`

## Observação
A versão rebuild foi montada em paralelo porque a sessão atual permitiu criar arquivos novos no GitHub, mas não sobrescrever com segurança alguns arquivos existentes do fluxo legado.
