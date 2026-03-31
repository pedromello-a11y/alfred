# V4 em uma tela

## Stack a usar

```bash
uvicorn app.stack_entry_v4:app --host 0.0.0.0 --port $PORT
```

## Arquivos principais
- `app/stack_entry_v4.py`
- `Procfile.v4`
- `USE_THIS_STACK.md`
- `REPO_MAP_V4.md`

## Antes do deploy
```bash
python scripts/check_stack_v4_imports.py
python scripts/v4_local_sanity.py
python scripts/stack_clean.py
```

## Depois do deploy
```bash
python scripts/v4_check.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
```

## Endpoints que precisam responder
- `/health`
- `/webhook`
- `/internal/whatsapp/inbound`
- `/whatsapp/inbound`
- `/dashboard/state`
- `/dashboard/focus`
- `/dashboard/tomorrow`
