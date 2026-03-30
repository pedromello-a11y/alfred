import uuid
from datetime import datetime, date

from sqlalchemy import (
    UUID, VARCHAR, TEXT, BOOLEAN, INTEGER, FLOAT, TIMESTAMP, DATE, Index, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(VARCHAR(500), nullable=False)
    description: Mapped[str | None] = mapped_column(TEXT)
    origin: Mapped[str | None] = mapped_column(VARCHAR(20))  # 'manual', 'jira', 'gchat'
    origin_ref: Mapped[str | None] = mapped_column(VARCHAR(100))
    status: Mapped[str] = mapped_column(VARCHAR(20), default="pending")  # pending/in_progress/done/cancelled/delegated/dropped
    priority: Mapped[int | None] = mapped_column(INTEGER)  # 1 (urgente) a 5 (baixa)
    category: Mapped[str | None] = mapped_column(VARCHAR(20))  # 'work', 'personal'
    deadline: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    estimated_minutes: Mapped[int | None] = mapped_column(INTEGER)
    actual_minutes: Mapped[int | None] = mapped_column(INTEGER)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    notes: Mapped[str | None] = mapped_column(TEXT)
    times_planned: Mapped[int] = mapped_column(INTEGER, default=0)
    last_planned: Mapped[date | None] = mapped_column(DATE)
    is_boss_fight: Mapped[bool] = mapped_column(BOOLEAN, default=False)
    importance: Mapped[int | None] = mapped_column(INTEGER)  # 1-5, impacto na vida/carreira
    effort_type: Mapped[str | None] = mapped_column(VARCHAR(20))  # 'quick', 'logistics', 'project'

    __table_args__ = (
        Index("idx_tasks_status_priority", "status", "priority"),
        Index("idx_tasks_deadline", "deadline"),
        Index("idx_tasks_category", "category"),
    )


class AgendaBlock(Base):
    __tablename__ = "agenda_blocks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(VARCHAR(300), nullable=False)
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    block_type: Mapped[str] = mapped_column(VARCHAR(30), default="focus")  # focus/meeting/break/admin/personal
    source: Mapped[str | None] = mapped_column(VARCHAR(30))  # manual/gcal/system
    status: Mapped[str] = mapped_column(VARCHAR(20), default="planned")  # planned/done/cancelled
    linked_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("idx_agenda_blocks_start_at", "start_at"),
        Index("idx_agenda_blocks_block_type", "block_type"),
    )


class DumpItem(Base):
    __tablename__ = "dump_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_text: Mapped[str] = mapped_column(TEXT, nullable=False)
    rewritten_title: Mapped[str] = mapped_column(VARCHAR(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(TEXT)
    category: Mapped[str | None] = mapped_column(VARCHAR(80))
    subcategory: Mapped[str | None] = mapped_column(VARCHAR(80))
    confidence: Mapped[float | None] = mapped_column(FLOAT)
    status: Mapped[str] = mapped_column(VARCHAR(20), default="categorized")  # categorized/unknown/reviewed
    source: Mapped[str | None] = mapped_column(VARCHAR(30))
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_dump_items_category", "category"),
        Index("idx_dump_items_status", "status"),
        Index("idx_dump_items_created_at", "created_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(VARCHAR(10))  # 'inbound', 'outbound'
    content: Mapped[str] = mapped_column(TEXT)
    message_type: Mapped[str] = mapped_column(VARCHAR(20))  # 'text', 'audio', 'image'
    processed: Mapped[bool] = mapped_column(BOOLEAN, default=False)
    classification: Mapped[str | None] = mapped_column(VARCHAR(30))  # 'new_task', 'update', 'question', 'command', 'chat'
    whapi_id: Mapped[str | None] = mapped_column(VARCHAR(100), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("idx_messages_created_at", "created_at"),
        Index("idx_messages_classification", "classification"),
        UniqueConstraint("whapi_id", name="uq_messages_whapi_id"),
    )


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_type: Mapped[str] = mapped_column(VARCHAR(20))  # 'daily', 'weekly', 'monthly'
    content: Mapped[str] = mapped_column(TEXT)
    period_start: Mapped[date] = mapped_column(DATE)
    period_end: Mapped[date] = mapped_column(DATE)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    superseded: Mapped[bool] = mapped_column(BOOLEAN, default=False)

    __table_args__ = (
        Index("idx_memories_type_period", "memory_type", "period_start"),
    )


class DailyPlan(Base):
    __tablename__ = "daily_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_date: Mapped[date] = mapped_column(DATE, unique=True)
    plan_content: Mapped[str] = mapped_column(TEXT)
    tasks_planned: Mapped[dict | None] = mapped_column(JSONB)
    tasks_completed: Mapped[dict | None] = mapped_column(JSONB)
    score: Mapped[int | None] = mapped_column(INTEGER)
    consolidated: Mapped[bool] = mapped_column(BOOLEAN, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("plan_date", name="uq_daily_plans_plan_date"),
    )


class JiraCache(Base):
    __tablename__ = "jira_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jira_key: Mapped[str] = mapped_column(VARCHAR(20), unique=True)
    summary: Mapped[str] = mapped_column(VARCHAR(500))
    status: Mapped[str | None] = mapped_column(VARCHAR(50))
    priority: Mapped[str | None] = mapped_column(VARCHAR(20))
    deadline: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    project_name: Mapped[str | None] = mapped_column(VARCHAR(100))
    description_summary: Mapped[str | None] = mapped_column(TEXT)
    last_synced: Mapped[datetime | None] = mapped_column(TIMESTAMP)

    __table_args__ = (
        UniqueConstraint("jira_key", name="uq_jira_cache_jira_key"),
    )


class Streak(Base):
    __tablename__ = "streaks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    streak_date: Mapped[date] = mapped_column(DATE, unique=True)
    tasks_completed: Mapped[int] = mapped_column(INTEGER, default=0)
    points: Mapped[int] = mapped_column(INTEGER, default=0)
    streak_count: Mapped[int] = mapped_column(INTEGER, default=0)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model: Mapped[str] = mapped_column(VARCHAR(50), nullable=False)
    input_tokens: Mapped[int] = mapped_column(INTEGER, nullable=False)
    output_tokens: Mapped[int] = mapped_column(INTEGER, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(FLOAT, nullable=False)
    call_type: Mapped[str | None] = mapped_column(VARCHAR(30))
    context_sent: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("idx_api_usage_created_at", "created_at"),
    )


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(VARCHAR(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(TEXT, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("key", name="uq_settings_key"),
    )


class PlayerStat(Base):
    __tablename__ = "player_stats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attribute: Mapped[str] = mapped_column(VARCHAR(20), unique=True, nullable=False)
    xp: Mapped[int] = mapped_column(INTEGER, default=0)
    level: Mapped[int] = mapped_column(INTEGER, default=1)
    prestige: Mapped[int] = mapped_column(INTEGER, default=0)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("attribute", name="uq_player_stats_attribute"),
    )


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(VARCHAR(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(100), nullable=False)
    description: Mapped[str | None] = mapped_column(TEXT)
    unlocked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, default=None)

    __table_args__ = (
        UniqueConstraint("code", name="uq_achievements_code"),
    )
