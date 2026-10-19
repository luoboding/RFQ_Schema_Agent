import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from rfq_schema_agent.cli import process_eml_file
from rfq_schema_agent.repository import SQLiteFormRepository


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, instructions: str, rfq_text: str) -> dict:
        self.calls += 1
        return {
            "rfq_summary": "Quote 100 brackets",
            "field_definitions": {"unit_price": {"type": "number", "currency_required": True}},
            "field_decisions": [{
                "field": "unit_price",
                "requirement": "required",
                "confidence": "high",
                "evidence": [{"source": "body", "excerpt": "Please quote"}],
                "reason": "",
            }],
        }


class CLITests(unittest.TestCase):
    def test_eml_file_to_sqlite_and_repeated_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = EmailMessage()
            message["From"] = "buyer@example.com"
            message["To"] = "supplier@example.com"
            message["Cc"] = "agent@example.com"
            message["Subject"] = "RFQ for brackets"
            message.set_content("Please quote 100 brackets.")
            email_path = root / "request.eml"
            email_path.write_bytes(message.as_bytes())
            database_path = root / "local" / "forms.sqlite3"

            model = FakeModel()
            first = process_eml_file(
                email_path,
                thread_id="thread-123",
                agent_address="agent@example.com",
                database_path=database_path,
                model=model,
            )
            second = process_eml_file(
                email_path,
                thread_id="thread-123",
                agent_address="agent@example.com",
                database_path=database_path,
                model=model,
            )

            self.assertEqual(first, second)
            self.assertEqual(first.required_fields, ["unit_price"])
            self.assertEqual(model.calls, 1)
            self.assertEqual(SQLiteFormRepository(database_path).get("thread-123"), first)


if __name__ == "__main__":
    unittest.main()
