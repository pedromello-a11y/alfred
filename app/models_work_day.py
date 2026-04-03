import uuid
from datetime import date, datetime
from sqlalchemy import Column, Date, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from app.database import Base


class WorkDay(Base):
    __tablename__ = "work_days"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date = Column(Date, nullable=False, unique=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    summary_json = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
