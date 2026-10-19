"""通过 SQLAlchemy ORM 持久化表单历史和 IMAP 同步游标。"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import and_, create_engine, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from rfq_schema_agent.models import RFQCollectionForm, ReviewStatus
from rfq_schema_agent.orm import Base, FormRevisionRow, IMAPSyncStateRow


class RevisionConflictError(Exception):
    """采购员提交的修订基于过期版本，或目标线程不存在。"""


@dataclass(frozen=True)
class RevisionRecord:
    form: RFQCollectionForm
    actor: str
    changed_at: datetime


class SQLiteFormRepository:
    """按线程读取当前表单；采购员修改会新增一条不可覆盖的修订。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        database_url = URL.create("sqlite+pysqlite", database=str(Path(database_path).resolve()))
        # 本地任务按操作取连接，归还时立即关闭，避免长期持有 SQLite 文件句柄。
        self.engine = create_engine(database_url, connect_args={"timeout": 5}, poolclass=NullPool)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _latest(session: Session, thread_id: str) -> FormRevisionRow | None:
        return session.scalars(
            select(FormRevisionRow)
            .where(FormRevisionRow.thread_id == thread_id)
            .order_by(FormRevisionRow.revision.desc())
            .limit(1)
        ).first()

    @staticmethod
    def _row(form: RFQCollectionForm, actor: str) -> FormRevisionRow:
        payload = form.to_dict()
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return FormRevisionRow(
            thread_id=form.thread_id,
            revision=form.revision,
            payload=payload,
            actor=actor,
            changed_at=datetime.now(timezone.utc).isoformat(),
        )

    def create_if_absent(self, form: RFQCollectionForm) -> RFQCollectionForm:
        """只接受 Agent 初稿；同一线程已存在时返回最新修订。"""
        if form.revision != 1 or form.review_status is not ReviewStatus.DRAFT:
            raise ValueError("agent creation requires a draft at revision 1")
        row = self._row(form, "agent")
        with self.session_factory.begin() as session:
            # 第 1 版的复合主键使并发创建幂等，不覆盖已确认的后续版本。
            session.execute(
                sqlite_insert(FormRevisionRow)
                .values(
                    thread_id=row.thread_id,
                    revision=row.revision,
                    payload=row.payload,
                    actor=row.actor,
                    changed_at=row.changed_at,
                )
                .on_conflict_do_nothing(index_elements=["thread_id", "revision"])
            )
            current = self._latest(session, form.thread_id)
            if current is None:
                raise RuntimeError("created form could not be read back")
            return RFQCollectionForm.from_dict(current.payload)

    def get(self, thread_id: str) -> RFQCollectionForm | None:
        """读取某条线程的当前修订。"""
        with self.session_factory() as session:
            row = self._latest(session, thread_id)
            return RFQCollectionForm.from_dict(row.payload) if row is not None else None

    def list_latest(self) -> list[RFQCollectionForm]:
        """列出每条线程的当前表单，供本地审核页展示。"""
        latest = (
            select(
                FormRevisionRow.thread_id,
                func.max(FormRevisionRow.revision).label("revision"),
            )
            .group_by(FormRevisionRow.thread_id)
            .subquery()
        )
        with self.session_factory() as session:
            rows = session.scalars(
                select(FormRevisionRow)
                .join(latest, and_(
                    FormRevisionRow.thread_id == latest.c.thread_id,
                    FormRevisionRow.revision == latest.c.revision,
                ))
                .order_by(FormRevisionRow.thread_id)
            ).all()
            return [RFQCollectionForm.from_dict(row.payload) for row in rows]

    def save_buyer_revision(
        self, form: RFQCollectionForm, *, expected_revision: int
    ) -> RFQCollectionForm:
        """采购员修改后追加修订；过期提交不会覆盖现有表单。"""
        if form.revision != expected_revision + 1:
            raise ValueError("new revision must equal expected_revision + 1")
        try:
            with self.session_factory.begin() as session:
                current = self._latest(session, form.thread_id)
                if current is None or current.revision != expected_revision:
                    raise RevisionConflictError("form revision changed or thread does not exist")
                if form.created_at != RFQCollectionForm.from_dict(current.payload).created_at:
                    raise ValueError("created_at cannot change across revisions")
                session.add(self._row(form, "buyer"))
            return form
        except IntegrityError as error:
            raise RevisionConflictError("form revision changed") from error

    def history(self, thread_id: str) -> list[RevisionRecord]:
        """按修订号返回审计历史，包括原始 Agent 表单。"""
        with self.session_factory() as session:
            rows = session.scalars(
                select(FormRevisionRow)
                .where(FormRevisionRow.thread_id == thread_id)
                .order_by(FormRevisionRow.revision)
            ).all()
            return [
                RevisionRecord(
                    form=RFQCollectionForm.from_dict(row.payload),
                    actor=row.actor,
                    changed_at=datetime.fromisoformat(row.changed_at),
                )
                for row in rows
            ]


class IMAPCursorStore:
    """同一 SQLite 数据库中的 IMAP UID 游标。"""

    def __init__(self, repository: SQLiteFormRepository) -> None:
        self.session_factory = repository.session_factory

    def get(self, mailbox_key: str) -> tuple[str, int] | None:
        with self.session_factory() as session:
            row = session.get(IMAPSyncStateRow, mailbox_key)
            return (row.uidvalidity, row.last_uid) if row is not None else None

    def save(self, mailbox_key: str, uidvalidity: str, last_uid: int) -> None:
        with self.session_factory.begin() as session:
            statement = sqlite_insert(IMAPSyncStateRow).values(
                mailbox_key=mailbox_key,
                uidvalidity=uidvalidity,
                last_uid=last_uid,
            )
            session.execute(statement.on_conflict_do_update(
                index_elements=["mailbox_key"],
                set_={"uidvalidity": uidvalidity, "last_uid": last_uid},
            ))
