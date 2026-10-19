"""通过只读 IMAP 同步新的买方 RFQ 邮件。"""

import imaplib
import os
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from rfq_schema_agent.agent import FormInferencer, RFQSchemaAgent
from rfq_schema_agent.email_input import RFQEmail
from rfq_schema_agent.repository import IMAPCursorStore, SQLiteFormRepository


@dataclass(frozen=True)
class IMAPSettings:
    host: str
    username: str
    password: str
    buyer_addresses: frozenset[str]
    agent_address: str
    port: int = 993
    mailbox: str = "INBOX"
    timeout_seconds: float = 30

    @classmethod
    def from_dotenv(cls, env_file: str | Path | None = None) -> "IMAPSettings":
        values = dotenv_values(env_file or Path.cwd() / ".env")

        def configured(name: str, default: str = "") -> str:
            return os.environ.get(name) or values.get(name) or default

        buyers = frozenset(
            address.strip().casefold()
            for address in configured("RFQ_BUYER_ADDRESSES").split(",")
            if address.strip()
        )
        required = {
            "IMAP_HOST": configured("IMAP_HOST"),
            "IMAP_USERNAME": configured("IMAP_USERNAME"),
            "IMAP_PASSWORD": configured("IMAP_PASSWORD"),
            "FORGEFLOW_ADDRESS": configured("FORGEFLOW_ADDRESS"),
        }
        missing = [name for name, value in required.items() if not value]
        if not buyers:
            missing.append("RFQ_BUYER_ADDRESSES")
        if missing:
            raise ValueError(f"missing IMAP configuration: {', '.join(missing)}")
        port = int(configured("IMAP_PORT", "993"))
        timeout = float(configured("IMAP_TIMEOUT_SECONDS", "30"))
        if not 1 <= port <= 65535 or timeout <= 0:
            raise ValueError("IMAP port and timeout must be positive")
        return cls(
            host=required["IMAP_HOST"],
            username=required["IMAP_USERNAME"],
            password=required["IMAP_PASSWORD"],
            buyer_addresses=buyers,
            agent_address=required["FORGEFLOW_ADDRESS"].casefold(),
            port=port,
            mailbox=configured("IMAP_MAILBOX", "INBOX"),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    processed: int
    skipped: int
    initialized: bool = False


def _uids(client: imaplib.IMAP4_SSL, after_uid: int | None = None) -> list[int]:
    criteria = ("ALL",) if after_uid is None else ("UID", f"{after_uid + 1}:*")
    status, data = client.uid("SEARCH", None, *criteria)
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError("IMAP UID search failed")
    return sorted(int(uid) for uid in data[0].split())


def _uidvalidity(client: imaplib.IMAP4_SSL) -> str:
    status, data = client.response("UIDVALIDITY")
    if status != "UIDVALIDITY" or not data or not data[0]:
        raise RuntimeError("IMAP mailbox did not provide UIDVALIDITY")
    return data[0].decode("ascii") if isinstance(data[0], bytes) else str(data[0])


def _fetch_message(client: imaplib.IMAP4_SSL, uid: int):
    # BODY.PEEK[] 与只读 SELECT 均不会把邮件标记为已读。
    status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK" or not data:
        raise RuntimeError(f"IMAP fetch failed for UID {uid}")
    payload = next((part[1] for part in data if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
    if payload is None:
        raise RuntimeError(f"IMAP fetch returned no email for UID {uid}")
    return BytesParser(policy=policy.default).parsebytes(payload)


def sync_imap(
    settings: IMAPSettings,
    repository: SQLiteFormRepository,
    inferencer: FormInferencer,
    *,
    include_existing: bool = False,
    batch_size: int = 50,
) -> SyncResult:
    """同步最多 batch_size 封新邮件；失败时停在出错邮件前以便重试。"""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    cursors = IMAPCursorStore(repository)
    mailbox_key = f"{settings.host}/{settings.username}/{settings.mailbox}"
    client = imaplib.IMAP4_SSL(settings.host, settings.port, timeout=settings.timeout_seconds)
    try:
        client.login(settings.username, settings.password)
        status, _ = client.select(settings.mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP mailbox selection failed")
        validity = _uidvalidity(client)
        state = cursors.get(mailbox_key)
        same_mailbox = state is not None and state[0] == validity
        all_uids = _uids(client, state[1] if same_mailbox else None)
        if (state is None or state[0] != validity) and not include_existing:
            cursors.save(mailbox_key, validity, max(all_uids, default=0))
            return SyncResult(0, 0, 0, initialized=True)

        last_uid = state[1] if same_mailbox else 0
        fetched = processed = skipped = 0
        agent = RFQSchemaAgent(inferencer, repository)
        for uid in (value for value in all_uids if value > last_uid):
            if fetched >= batch_size:
                break
            message = _fetch_message(client, uid)
            fetched += 1
            sender = {address.casefold() for _, address in getaddresses(message.get_all("From", []))}
            copied = {address.casefold() for _, address in getaddresses(message.get_all("Cc", []))}
            if len(sender) != 1 or not sender.issubset(settings.buyer_addresses) or settings.agent_address.casefold() not in copied:
                skipped += 1
            else:
                message_id = str(message.get("Message-ID", "")).strip()
                if not message_id:
                    raise ValueError(f"buyer RFQ at IMAP UID {uid} lacks Message-ID")
                email = RFQEmail.from_message(
                    message, thread_id=message_id, agent_address=settings.agent_address
                )
                agent.process(email)
                processed += 1
            # 表单写入成功或邮件确定无关后，才推进游标。
            cursors.save(mailbox_key, validity, uid)
        return SyncResult(fetched, processed, skipped)
    finally:
        client.logout()


def watch_imap(
    settings: IMAPSettings,
    repository: SQLiteFormRepository,
    inferencer: FormInferencer,
    *,
    include_existing: bool = False,
    batch_size: int = 50,
    poll_seconds: float = 5,
    on_result: Callable[[SyncResult], None],
    on_error: Callable[[Exception], None],
    wait: Callable[[float], None] = time.sleep,
) -> None:
    """持续同步；满批立即拉下一批，空闲或失败后等待再试。"""
    if batch_size < 1 or poll_seconds <= 0:
        raise ValueError("batch_size and poll_seconds must be positive")
    while True:
        try:
            result = sync_imap(
                settings, repository, inferencer,
                include_existing=include_existing, batch_size=batch_size,
            )
        except Exception as error:
            on_error(error)
            wait(poll_seconds)
            continue
        include_existing = False
        on_result(result)
        if result.fetched < batch_size:
            wait(poll_seconds)
