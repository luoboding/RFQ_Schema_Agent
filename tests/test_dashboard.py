import io
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from rfq_schema_agent.dashboard import make_handler, render_index
from rfq_schema_agent.models import (
    Confidence, Evidence, EvidenceSource, FieldDecision, Requirement,
    ReviewStatus, RFQCollectionForm,
)
from rfq_schema_agent.repository import RevisionConflictError, SQLiteFormRepository
from rfq_schema_agent.review import save_review


def sample_form() -> RFQCollectionForm:
    return RFQCollectionForm(
        thread_id="<rfq@example.com>",
        rfq_summary="Quote <brackets>",
        suppliers=("vendor@example.com",),
        field_definitions={"unit_price": {"type": "number"}, "lead_time_weeks": {"type": "number"}},
        decisions=(
            FieldDecision("unit_price", Requirement.REQUIRED, Confidence.HIGH,
                          (Evidence(EvidenceSource.BODY, "Please quote"),)),
            FieldDecision("lead_time_weeks", Requirement.PENDING_CONFIRMATION, Confidence.LOW,
                          (Evidence(EvidenceSource.BODY, "as soon as possible"),), "Unclear timing"),
        ),
        created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
    )


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteFormRepository(Path(self.directory.name) / "forms.sqlite3")
        self.repository.create_if_absent(sample_form())

    def tearDown(self):
        self.directory.cleanup()

    def test_review_requires_resolution_and_keeps_audit_history(self):
        current = sample_form()
        with self.assertRaises(ValueError):
            save_review(
                self.repository, current.thread_id, expected_revision=1,
                requirements={"unit_price": Requirement.REQUIRED,
                              "lead_time_weeks": Requirement.PENDING_CONFIRMATION},
                confirm=True,
            )
        self.assertEqual(len(self.repository.history(current.thread_id)), 1)

        saved = save_review(
            self.repository, current.thread_id, expected_revision=1,
            requirements={"unit_price": Requirement.REQUIRED,
                          "lead_time_weeks": Requirement.OPTIONAL},
            confirm=True,
        )
        self.assertEqual(saved.review_status, ReviewStatus.CONFIRMED)
        self.assertEqual(saved.optional_fields, ["lead_time_weeks"])
        self.assertEqual(saved.decisions[1].confidence, Confidence.HIGH)
        self.assertEqual([item.actor for item in self.repository.history(current.thread_id)], ["agent", "buyer"])
        self.assertEqual(self.repository.list_latest(), [saved])
        with self.assertRaises(RevisionConflictError):
            save_review(
                self.repository, current.thread_id, expected_revision=1,
                requirements={"unit_price": Requirement.REQUIRED,
                              "lead_time_weeks": Requirement.REQUIRED},
                confirm=True,
            )

    def test_html_escapes_buyer_content(self):
        content = render_index(self.repository.list_latest()).decode("utf-8")
        self.assertIn("Quote &lt;brackets&gt;", content)
        self.assertNotIn("Quote <brackets>", content)

    def test_http_review_checks_csrf_and_saves_revision(self):
        class FakeSocket:
            def __init__(self, request):
                self.input = io.BytesIO(request)
                self.output = io.BytesIO()

            def makefile(self, mode, buffering=None):
                return self.input

            def sendall(self, data):
                self.output.write(data)

        class FakeServer:
            server_port = 8765

        handler = make_handler(self.repository)

        def request(method, path, body=b""):
            headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n"
                       f"Content-Length: {len(body)}\r\n"
                       "Content-Type: application/x-www-form-urlencoded\r\n\r\n").encode("ascii")
            socket = FakeSocket(headers + body)
            handler(socket, ("127.0.0.1", 12345), FakeServer())
            return socket.output.getvalue()

        response = request("GET", "/form?thread_id=%3Crfq%40example.com%3E")
        self.assertIn(b"HTTP/1.0 200 OK", response)
        token = re.search(rb"name='csrf_token' value='([^']+)'", response).group(1).decode()
        fields = {
            "csrf_token": "wrong", "thread_id": "<rfq@example.com>", "revision": "1",
            "field:unit_price": "required", "field:lead_time_weeks": "optional", "confirm": "yes",
        }
        denied = request("POST", "/review", urlencode(fields).encode())
        self.assertIn(b"HTTP/1.0 403 Forbidden", denied)
        fields["csrf_token"] = token
        saved = request("POST", "/review", urlencode(fields).encode())
        self.assertIn(b"HTTP/1.0 303 See Other", saved)
        self.assertEqual(self.repository.get("<rfq@example.com>").review_status, ReviewStatus.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
