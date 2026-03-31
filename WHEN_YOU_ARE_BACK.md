# Quando você voltar ao computador

## Use esta stack

```bash
uvicorn app.stack_entry_v4:app --host 0.0.0.0 --port $PORT
```

Ou configure o Railway para usar:

```bash
Procfile.v4
```

## Ordem recomendada

1. rodar limpeza básica:
```bash
python scripts/stack_clean.py
```

2. subir a stack v4

3. rodar checker rápido:
```bash
python scripts/v4_check.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
```

4. validar manualmente:
- `/health`
- `/dashboard/state`
- `/dashboard/focus`
- `/dashboard/tomorrow`
- `/whatsapp/inbound`
- `/internal/whatsapp/inbound`
- `/webhook`

## Arquivos canônicos da versão atual
- `USE_THIS_STACK.md`
- `DEPLOY_V4.md`
- `STACK_V4_CHECKLIST.md`
- `POST_DEPLOY_V4.md`
