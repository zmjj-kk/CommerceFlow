"""Additive tables: legacy knowledge_chunks is deliberately untouched."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KBDocument(Base):
    __tablename__ = "kb_documents"
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_uri: Mapped[str] = mapped_column(Text)
    desired_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class KBVersion(Base):
    __tablename__ = "kb_document_versions"
    document_id: Mapped[str] = mapped_column(ForeignKey("kb_documents.document_id"), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    chunk_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KBSourceChunk(Base):
    __tablename__ = "kb_source_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("kb_documents.document_id"), index=True)
    version_id: Mapped[str] = mapped_column(String(64))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)
    prev_chunk_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_chunk_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("document_id", "version_id", "chunk_index"),
                      ForeignKeyConstraint(["document_id", "version_id"],
                                           ["kb_document_versions.document_id", "kb_document_versions.version_id"]))


class KBIndexState(Base):
    __tablename__ = "kb_index_states"
    collection: Mapped[str] = mapped_column(String(128), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("kb_source_chunks.chunk_id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")


class KBActiveVersion(Base):
    __tablename__ = "kb_active_versions"
    collection: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("kb_documents.document_id"), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (ForeignKeyConstraint(["document_id", "version_id"],
                                         ["kb_document_versions.document_id", "kb_document_versions.version_id"]),)
