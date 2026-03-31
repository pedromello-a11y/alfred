# Use esta stack

## Stack recomendada agora

Use a **v4**.

### Start command

```bash
uvicorn app.stack_entry_v4:app --host 0.0.0.0 --port $PORT
```

Ou:

```bash
Procfile.v4
```

## Por que esta
- é a versão mais coerente entre webhook, gateway interno e `/whatsapp/inbound`
- usa dashboard v2
- usa tomorrow board
- usa foco operacional na resposta

## Teste rápido

```bash
python scripts/stack_clean.py
python scripts/v4_check.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
```
