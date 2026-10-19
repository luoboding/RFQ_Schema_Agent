import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from rfq_schema_agent.imap_ingest import IMAPSettings, SyncResult, sync_imap, watch_imap
from rfq_schema_agent.inference import FormProposal
from rfq_schema_agent.models import Confidence, Evidence, EvidenceSource, FieldDecision, Requirement
from rfq_schema_agent.repository import SQLiteFormRepository


def email_bytes(uid: int, *, sender: str = "buyer@example.com") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "supplier@example.com"
    message["Cc"] = "agent@example.com"
    message["Subject"] = "RFQ brackets"
    message["Message-ID"] = f"<rfq-{uid}@example.com>"
    message.set_content("Please quote 100 brackets.")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages: dict[int, bytes]) -> None:
        self.messages = messages
        self.validity = b"123"
        self.readonly = None
        self.fetch_commands = []
        self.search_commands = []
        self.logged_out = False

    def login(self, username: str, password: str):
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str):
        return "UIDVALIDITY", [self.validity]

    def uid(self, command: str, *args):
        if command == "SEARCH":
            self.search_commands.append(args)
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.messages))]
        if command == "FETCH":
            self.fetch_commands.append(args[1])
            uid = int(args[0])
            return "OK", [(b"BODY[]", self.messages[uid])]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


class FakeInferencer:
    def __init__(self) -> None:
        self.calls = 0

    def infer(self, subject: str, body: str) -> FormProposal:
        self.calls += 1
        return FormProposal(
            "Quote brackets",
            {"unit_price": {"type": "number"}},
            (FieldDecision(
                "unit_price", Requirement.REQUIRED, Confidence.HIGH,
                (Evidence(EvidenceSource.BODY, "Please quote"),),
            ),),
        )


class FailsOnceInferencer(FakeInferencer):
    def infer(self, subject: str, body: str) -> FormProposal:
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("temporary model failure")
        return super().infer(subject, body)


class IMAPIngestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteFormRepository(Path(self.directory.name) / "forms.sqlite3")
        self.settings = IMAPSettings(
            host="imap.example.com",
            username="agent@example.com",
            password="test-password",
            buyer_addresses=frozenset({"buyer@example.com"}),
            agent_address="agent@example.com",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_initial_sync_starts_with_future_mail_and_keeps_mail_unread(self):
        client = FakeIMAP({1: email_bytes(1)})
        inferencer = FakeInferencer()
        with patch("rfq_schema_agent.imap_ingest.imaplib.IMAP4_SSL", return_value=client):
            first = sync_imap(self.settings, self.repository, inferencer)
            client.messages[2] = email_bytes(2)
            second = sync_imap(self.settings, self.repository, inferencer)
            third = sync_imap(self.settings, self.repository, inferencer)

        self.assertTrue(first.initialized)
        self.assertEqual(second.processed, 1)
        self.assertEqual(third.fetched, 0)
        self.assertEqual(inferencer.calls, 1)
        self.assertTrue(client.readonly)
        self.assertEqual(client.search_commands[1], (None, "UID", "2:*"))
        self.assertEqual(client.fetch_commands, ["(BODY.PEEK[])"])
        self.assertTrue(client.logged_out)
        self.assertIsNotNone(self.repository.get("<rfq-2@example.com>"))
        self.assertIsNone(self.repository.get("<rfq-1@example.com>"))

    def test_backfill_skips_nonbuyer_and_processes_rfq(self):
        client = FakeIMAP({1: email_bytes(1, sender="supplier@example.com"), 2: email_bytes(2)})
        inferencer = FakeInferencer()
        with patch("rfq_schema_agent.imap_ingest.imaplib.IMAP4_SSL", return_value=client):
            result = sync_imap(self.settings, self.repository, inferencer, include_existing=True)
        self.assertEqual((result.fetched, result.processed, result.skipped), (2, 1, 1))
        self.assertEqual(inferencer.calls, 1)

    def test_failed_message_is_retried_without_advancing_cursor(self):
        client = FakeIMAP({1: email_bytes(1)})
        inferencer = FailsOnceInferencer()
        with patch("rfq_schema_agent.imap_ingest.imaplib.IMAP4_SSL", return_value=client):
            sync_imap(self.settings, self.repository, inferencer)
            client.messages[2] = email_bytes(2)
            with self.assertRaises(RuntimeError):
                sync_imap(self.settings, self.repository, inferencer)
            result = sync_imap(self.settings, self.repository, inferencer)

        self.assertEqual(result.processed, 1)
        self.assertEqual(client.fetch_commands, ["(BODY.PEEK[])", "(BODY.PEEK[])"])
        self.assertIsNotNone(self.repository.get("<rfq-2@example.com>"))

    def test_reads_imap_settings_from_dotenv(self):
        env_file = Path(self.directory.name) / ".env"
        env_file.write_text(
            "IMAP_HOST=imap.example.com\nIMAP_USERNAME=agent@example.com\n"
            "IMAP_PASSWORD=test-password\nFORGEFLOW_ADDRESS=agent@example.com\n"
            "RFQ_BUYER_ADDRESSES=buyer@example.com, second@example.com\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            settings = IMAPSettings.from_dotenv(env_file)
        self.assertEqual(settings.port, 993)
        self.assertEqual(settings.buyer_addresses, frozenset({"buyer@example.com", "second@example.com"}))

    def test_watch_drains_full_batch_then_waits(self):
        results = [SyncResult(2, 2, 0), SyncResult(1, 1, 0)]
        waits = []

        def stop_after_wait(seconds):
            waits.append(seconds)
            raise KeyboardInterrupt

        with patch("rfq_schema_agent.imap_ingest.sync_imap", side_effect=results) as sync:
            with self.assertRaises(KeyboardInterrupt):
                watch_imap(
                    self.settings, self.repository, FakeInferencer(),
                    include_existing=True, batch_size=2, poll_seconds=3,
                    on_result=lambda result: None,
                    on_error=lambda error: self.fail(str(error)),
                    wait=stop_after_wait,
                )

        self.assertEqual(waits, [3])
        self.assertTrue(sync.call_args_list[0].kwargs["include_existing"])
        self.assertFalse(sync.call_args_list[1].kwargs["include_existing"])

    def test_watch_retries_after_sync_error(self):
        results = []
        errors = []
        calls = iter([RuntimeError("temporary failure"), SyncResult(0, 0, 0)])
        waits = []

        def wait_or_stop(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise KeyboardInterrupt

        with patch("rfq_schema_agent.imap_ingest.sync_imap", side_effect=calls) as sync:
            with self.assertRaises(KeyboardInterrupt):
                watch_imap(
                    self.settings, self.repository, FakeInferencer(),
                    include_existing=True, poll_seconds=3,
                    on_result=results.append,
                    on_error=errors.append,
                    wait=wait_or_stop,
                )

        self.assertEqual(len(errors), 1)
        self.assertEqual(results, [SyncResult(0, 0, 0)])
        self.assertEqual(waits, [3, 3])
        self.assertTrue(sync.call_args_list[1].kwargs["include_existing"])


if __name__ == "__main__":
    unittest.main()
