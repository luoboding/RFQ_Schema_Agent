import json
import unittest
from datetime import datetime, timezone

from rfq_schema_agent.models import (
    Confidence,
    Evidence,
    EvidenceSource,
    FieldDecision,
    Requirement,
    ReviewStatus,
    RFQCollectionForm,
)


class RFQCollectionFormTests(unittest.TestCase):
    def make_form(self, *, review_status=ReviewStatus.DRAFT):
        return RFQCollectionForm(
            thread_id="thread-123",
            rfq_summary="Quote 100 machined brackets",
            suppliers=("vendor@example.com",),
            field_definitions={
                "unit_price": {"type": "number", "currency_required": True},
                "lead_time_weeks": {"type": "number", "unit": "weeks"},
                "moq": {"type": "number", "unit": "pieces"},
            },
            decisions=(
                FieldDecision(
                    "unit_price",
                    Requirement.REQUIRED,
                    Confidence.HIGH,
                    (Evidence(EvidenceSource.BODY, "Please quote a unit price"),),
                ),
                FieldDecision(
                    "lead_time_weeks",
                    Requirement.PENDING_CONFIRMATION,
                    Confidence.LOW,
                    (Evidence(EvidenceSource.BODY, "Ship as soon as possible"),),
                    "Unclear whether a lead-time quote is required",
                ),
                FieldDecision("moq", Requirement.OPTIONAL, Confidence.HIGH),
            ),
            created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
            review_status=review_status,
        )

    def test_payload_separates_pending_fields(self):
        payload = self.make_form().to_dict()
        self.assertEqual(payload["required_fields"], ["unit_price"])
        self.assertEqual(payload["optional_fields"], ["moq"])
        self.assertEqual(payload["pending_fields"], ["lead_time_weeks"])
        self.assertEqual(payload["field_decisions"][1]["evidence"][0]["excerpt"], "Ship as soon as possible")
        self.assertEqual(payload["low_confidence_decisions"][0]["field"], "lead_time_weeks")
        json.dumps(payload)

    def test_low_confidence_cannot_be_required(self):
        with self.assertRaises(ValueError):
            FieldDecision(
                "nre", Requirement.REQUIRED, Confidence.LOW,
                (Evidence(EvidenceSource.BODY, "tooling cost"),),
            )

    def test_confirmed_form_cannot_have_pending_fields(self):
        with self.assertRaises(ValueError):
            self.make_form(review_status=ReviewStatus.CONFIRMED)

    def test_all_definitions_need_one_decision(self):
        form = self.make_form()
        with self.assertRaises(ValueError):
            RFQCollectionForm(
                thread_id=form.thread_id,
                rfq_summary=form.rfq_summary,
                suppliers=form.suppliers,
                field_definitions=form.field_definitions,
                decisions=form.decisions[:-1],
                created_at=form.created_at,
            )

    def test_rejects_ambiguous_field_definition_type(self):
        form = self.make_form()
        with self.assertRaises(ValueError):
            RFQCollectionForm(
                thread_id=form.thread_id,
                rfq_summary=form.rfq_summary,
                suppliers=form.suppliers,
                field_definitions={**form.field_definitions, "unit_price": {"type": "number|string"}},
                decisions=form.decisions,
                created_at=form.created_at,
            )


if __name__ == "__main__":
    unittest.main()
