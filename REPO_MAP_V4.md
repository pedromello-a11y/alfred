# Repo map — stack v4

## Use estes arquivos

### Entry point
- `app/stack_entry_v4.py`

### Procfile
- `Procfile.v4`

### Deploy docs
- `USE_THIS_STACK.md`
- `DEPLOY_V4.md`
- `STACK_V4_CHECKLIST.md`
- `POST_DEPLOY_V4.md`
- `WHEN_YOU_ARE_BACK.md`

### Routers da stack v4
- `app/routers/webhook_v2.py`
- `app/routers/internal_whatsapp_v2.py`
- `app/routers/wa_in_v3.py`
- `app/routers/dashboard_v2.py`

### Services da stack v4
- `app/services/alfred_brain_v2.py`
- `app/services/focus_snapshot.py`
- `app/services/tomorrow_board.py`

### Scheduler / cron
- `app/cron/final_jobs.py`
- `app/cron/morning_briefing_rebuild.py`
- `app/cron/midday_checkin_rebuild.py`
- `app/cron/end_of_day_stack.py`

### Scripts úteis
- `scripts/stack_clean.py`
- `scripts/v4_check.py`
- `scripts/check_stack_v4_imports.py`

## Arquivos de transição
Eles podem continuar no repo, mas não são a stack recomendada agora:
- `app/main_unified.py`
- `app/main_rebuild.py`
- `app/main_rebuild_v2.py`
- `app/rebuild_stack.py`
- `app/stack_entry.py`
- `app/stack_entry_v2.py`
- `app/stack_entry_v3.py`
- `Procfile.unified`
- `Procfile.rebuild`
- `Procfile.stack`
- `Procfile.final`
- `DEPLOY_REBUILD.md`
- `DEPLOY_STACK.md`
- `DEPLOY_FINAL.md`
- `DEPLOY_V2.md`
- `DEPLOY_V3.md`

## Observação
A v4 é a escolha atual porque é a versão mais coerente entre os três canais de entrada, foco operacional e dashboard v2.
