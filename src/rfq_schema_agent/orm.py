"""RFQ 表单历史与 IMAP 游标的 SQLAlchemy ORM 映射。"""

from typing import Any

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FormRevisionRow(Base):
    __tablename__ = "form_revisions"

    thread_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    changed_at: Mapped[str] = mapped_column(Text, nullable=False)


class IMAPSyncStateRow(Base):
    __tablename__ = "imap_sync_state"

    mailbox_key: Mapped[str] = mapped_column(Text, primary_key=True)
    uidvalidity: Mapped[str] = mapped_column(Text, nullable=False)
    last_uid: Mapped[int] = mapped_column(Integer, nullable=False)
