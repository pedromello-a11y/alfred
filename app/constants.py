"""Constantes compartilhadas entre todos os módulos do Alfred."""

# ── Status ────────────────────────────────────────────────────────────────────
ACTIVE_STATUSES = ("active", "pending", "in_progress")
FINAL_STATUSES = ("done", "cancelled", "dropped", "delegated", "archived")

# ── Horário de trabalho ───────────────────────────────────────────────────────
WORK_START_HOUR = 8
WORK_END_HOUR = 20
WORK_START_MINUTES = WORK_START_HOUR * 60   # 480
WORK_END_MINUTES = WORK_END_HOUR * 60       # 1200
DAY_GROSS_MINUTES = WORK_END_MINUTES - WORK_START_MINUTES  # 720

# ── Scheduler ─────────────────────────────────────────────────────────────────
WORK_DAYS = [0, 1, 2, 3, 4]  # SEG a SEX
MAX_DAY_LOAD = 0.80
BUFFER_PREFERRED_START = 14 * 60  # 840
BUFFER_PREFERRED_END = 16 * 60    # 960
MICRO_BUFFER_MINUTES = 5  # entre tasks consecutivas

# ── Task types que viram blocos na agenda ─────────────────────────────────────
SCHEDULABLE_TASK_TYPES = ("task",)  # NUNCA "project" ou "deliverable"
