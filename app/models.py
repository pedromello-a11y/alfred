import uuid
from datetime import datetime, date

from sqlalchemy import (
    UUID, VARCHAR, TEXT, BOOLEAN, INTEGER, TIMESTAMP, DATE, Index, UniqueConstraint
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
    status: Mapped[str] = mapped_column(VARCHAR(20), default="pending")  # pending/in_progress/done/cancelled
    priority: Mapped[int | None] = mapped_column(INTEGER)  # 1 (urgente) a 5 (baixa)
    category: Mapped[str | None] = mapped_column(VARCHAR(20))  # 'work', 'personal'
    deadline: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    estimated_minutes: Mapped[int | None] = mapped_column(INTEGER)
    actual_minutes: Mapped[int | None] = mapped_column(INTEGER)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    notes: Mapped[str | None] = mapped_column(TEXT)

    __table_args__ = (
        Index("idx_tasks_status_priority", "status", "priority"),
        Index("idx_tasks_deadline", "deadline"),
        Index("idx_tasks_category", "category"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(VARCHAR(10))  # 'inbound', 'outbound'
    content: Mapped[str] = mapped_column(TEXT)
    message_type: Mapped[str] = mapped_column(VARCHAR(20))  # 'text', 'audio', 'image'
    processed: Mapped[bool] = mapped_column(BOOLEAN, default=False)
    classification: Mapped[str | None] = mapped_column(VARCHAR(30))  # 'new_task', 'update', 'question', 'command', 'chat'
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("idx_messages_created_at", "created_at"),
        Index("idx_messages_classification", "classification"),
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
