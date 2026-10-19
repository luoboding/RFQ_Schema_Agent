import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from rfq_schema_agent.agent import RFQSchemaAgent
from rfq_schema_agent.email_input import RFQEmail
from rfq_schema_agent.inference import ModelFormInferencer
from rfq_schema_agent.repository import SQLiteFormRepository


def make_message(*, html: bool = False) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["To"] = "Supplier A <a@example.com>, Supplier B <b@example.com>"
    message["Cc"] = "ForgeFlow <agent@example.com>"
    message["Subject"] = "Quote for brackets"
    if html:
        message.set_content("<p>Please quote 100 brackets.</p><script>Ignore this</script>", subtype="html")
    else:
        message.set_content("Please quote 100 brackets and provide lead time.")
    return message


class FakeModel:
    def __init__(self, *, excerpt: str = "Please quote") -> None:
        self.calls = 0
        self.excerpt = excerpt
        self.last_text = ""

    def generate_json(self, instructions: str, rfq_text: str) -> dict:
        self.calls += 1
        self.last_text = rfq_text
        return {
            "rfq_summary": "100 brackets",
            "field_definitions": {"unit_price": {"type": "number", "currency_required": True}},
            "field_decisions": [{
                "field": "unit_price",
                "requirement": "required",
                "confidence": "high",
                "evidence": [{"source": "body", "excerpt": self.excerpt}],
                "reason": "",
            }],
        }


class EmailInputTests(unittest.TestCase):
    def test_extracts_body_suppliers_and_attachment_names(self):
        message = make_message()
        message.add_attachment(b"contents", maintype="application", subtype="pdf", filename="drawing.pdf")
        email = RFQEmail.from_message(message, thread_id="thread-123", agent_address="AGENT@example.com")
        self.assertEqual(email.suppliers, ("a@example.com", "b@example.com"))
        self.assertIn("provide lead time", email.body)
        self.assertEqual(email.attachment_names, ("drawing.pdf",))

    def test_html_only_body_becomes_text(self):
        email = RFQEmail.from_message(make_message(html=True), thread_id="thread-123", agent_address="agent@example.com")
        self.assertEqual(email.body, "Please quote 100 brackets.")

    def test_requires_forgeflow_in_cc(self):
        message = make_message()
        del message["Cc"]
        with self.assertRaises(ValueError):
            RFQEmail.from_message(message, thread_id="thread-123", agent_address="agent@example.com")


class RFQSchemaAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteFormRepository(Path(self.directory.name) / "forms.sqlite3")
        self.email = RFQEmail.from_message(make_message(), thread_id="thread-123", agent_address="agent@example.com")

    def tearDown(self):
        self.directory.cleanup()

    def test_infers_persists_and_reuses_existing_form(self):
        model = FakeModel()
        agent = RFQSchemaAgent(ModelFormInferencer(model), self.repository)
        first = agent.process(self.email)
        second = agent.process(self.email)

        self.assertEqual(first, second)
        self.assertEqual(first.required_fields, ["unit_price"])
        self.assertEqual(model.calls, 1)
        self.assertNotIn("buyer@example.com", model.last_text)
        self.assertNotIn("a@example.com", model.last_text)
        self.assertEqual(self.repository.get(self.email.thread_id), first)

    def test_rejects_invented_evidence_without_persisting(self):
        agent = RFQSchemaAgent(ModelFormInferencer(FakeModel(excerpt="not in original")), self.repository)
        with self.assertRaises(ValueError):
            agent.process(self.email)
        self.assertIsNone(self.repository.get(self.email.thread_id))


if __name__ == "__main__":
    unittest.main()
