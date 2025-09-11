from __future__ import annotations

import os
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    TEXT,
    String,
    DateTime,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


# -------------------- SQLAlchemy setup --------------------

def _db_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./ai_ta.db")


engine = create_engine(_db_url(), future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


# -------------------- SQLAlchemy Model --------------------

class AIUseReportORM(Base):
    __tablename__ = "ai_use_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid_pkg.uuid4()))
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    style: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    length: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    markdown: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    # Use SQLAlchemy JSON so SQLite transparently serializes to TEXT
    jsonld: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    model_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tool_calls: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    prompt_hashes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)


# -------------------- Pydantic Schemas --------------------

class ToolCallMeta(BaseModel):
    name: str
    inputs_summary: str


class AIUseReport(BaseModel):
    id: str
    chat_id: str
    created_at: datetime
    style: Optional[str] = None
    length: Optional[str] = None
    markdown: Optional[str] = None
    jsonld: Optional[Any] = None
    model_fingerprint: Optional[str] = None
    tool_calls: list[ToolCallMeta] = Field(default_factory=list)
    prompt_hashes: list[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


def init_db() -> None:
    """Create tables if they don't exist (dev convenience)."""
    Base.metadata.create_all(engine)
