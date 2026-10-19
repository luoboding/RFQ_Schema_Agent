import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from rfq_schema_agent.models import (
    Confidence,
    Evidence,
    EvidenceSource,
    FieldDecision,
    Requirement,
    ReviewStatus,
    RFQCollectionForm,
)
from rfq_schema_agent.repository import IMAPCursorStore, RevisionConflictError, SQLiteFormRepository


def sample_form(summary: str = "Quote 100 brackets") -> RFQCollectionForm:
    return RFQCollectionForm(
        thread_id="thread-123",
        rfq_summary=summary,
        suppliers=("vendor@example.com",),
        field_definitions={"unit_price": {"type": "number", "currency_required": True}},
        decisions=(
            FieldDecision(
                "unit_price",
                Requirement.REQUIRED,
                Confidence.HIGH,
                (Evidence(EvidenceSource.BODY, "Please quote a unit price"),),
            ),
        ),
        created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
    )


class SQLiteFormRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "forms.sqlite3"
        self.repository = SQLiteFormRepository(self.database_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_create_and_read_from_new_repository_instance(self):
        original = sample_form()
        self.assertEqual(self.repository.create_if_absent(original), original)
        reopened = SQLiteFormRepository(self.database_path)
        self.assertEqual(reopened.get(original.thread_id), original)
        self.assertIsNone(reopened.get("unknown-thread"))

    def test_repeated_agent_call_keeps_confirmed_buyer_revision(self):
        original = self.repository.create_if_absent(sample_form())
        confirmed = replace(
            original,
            rfq_summary="Buyer approved quote requirements",
            revision=2,
            review_status=ReviewStatus.CONFIRMED,
        )
        self.repository.save_buyer_revision(confirmed, expected_revision=1)

        self.assertEqual(
            self.repository.create_if_absent(sample_form("Agent changed its mind")),
            confirmed,
        )
        self.assertEqual(self.repository.get(original.thread_id), confirmed)
        history = self.repository.history(original.thread_id)
        self.assertEqual([record.form.revision for record in history], [1, 2])
        self.assertEqual([record.actor for record in history], ["agent", "buyer"])
        self.assertEqual(history[0].form, original)
        self.assertIsNotNone(history[1].changed_at.tzinfo)

    def test_stale_buyer_revision_does_not_overwrite_current_form(self):
        original = self.repository.create_if_absent(sample_form())
        revision_two = replace(original, revision=2)
        self.repository.save_buyer_revision(revision_two, expected_revision=1)

        with self.assertRaises(RevisionConflictError):
            self.repository.save_buyer_revision(
                replace(original, rfq_summary="Stale edit", revision=2),
                expected_revision=1,
            )
        self.assertEqual(self.repository.get(original.thread_id), revision_two)
        self.assertEqual(len(self.repository.history(original.thread_id)), 2)

    def test_simultaneous_agent_calls_create_only_one_initial_form(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                self.repository.create_if_absent,
                (sample_form("First request"), sample_form("Second request")),
            ))

        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.repository.history("thread-123")), 1)

    def test_reads_tables_created_by_previous_sqlite_implementation(self):
        original = sample_form()
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS form_revisions ("
                    "thread_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, actor TEXT NOT NULL, changed_at TEXT NOT NULL, "
                    "PRIMARY KEY (thread_id, revision))"
                )
                connection.execute(
                    "INSERT INTO form_revisions VALUES (?, ?, ?, ?, ?)",
                    (original.thread_id, 1, json.dumps(original.to_dict()), "agent", "2026-10-19T00:00:00+00:00"),
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS imap_sync_state ("
                    "mailbox_key TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO imap_sync_state VALUES (?, ?, ?)", ("INBOX", "7", 42))

        reopened = SQLiteFormRepository(legacy_path)
        self.assertEqual(reopened.get(original.thread_id), original)
        self.assertEqual(reopened.history(original.thread_id)[0].form, original)
        self.assertEqual(IMAPCursorStore(reopened).get("INBOX"), ("7", 42))


if __name__ == "__main__":
    unittest.main()
