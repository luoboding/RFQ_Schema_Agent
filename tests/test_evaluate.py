import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rfq_schema_agent.evaluate import EvaluationCase, evaluate, load_cases
from rfq_schema_agent.inference import FormProposal
from rfq_schema_agent.models import Confidence, Evidence, EvidenceSource, FieldDecision, Requirement


class PartialInferencer:
    def infer(self, subject: str, body: str) -> FormProposal:
        field = "unit_price" if "quote" in body else "moq"
        excerpt = "quote" if field == "unit_price" else "MOQ"
        return FormProposal(
            rfq_summary="test RFQ",
            field_definitions={field: {"type": "number"}},
            decisions=(FieldDecision(
                field=field,
                requirement=Requirement.REQUIRED,
                confidence=Confidence.HIGH,
                evidence=(Evidence(EvidenceSource.BODY, excerpt),),
            ),),
        )


class ConfidenceInferencer:
    def infer(self, subject: str, body: str) -> FormProposal:
        return FormProposal(
            rfq_summary="test RFQ",
            field_definitions={
                "unit_price": {"type": "number"},
                "moq": {"type": "integer"},
                "lead_time_weeks": {"type": "number"},
            },
            decisions=(
                FieldDecision("unit_price", Requirement.REQUIRED, Confidence.HIGH,
                              (Evidence(EvidenceSource.BODY, "quote"),)),
                FieldDecision("moq", Requirement.REQUIRED, Confidence.MEDIUM,
                              (Evidence(EvidenceSource.BODY, "MOQ"),)),
                FieldDecision("lead_time_weeks", Requirement.PENDING_CONFIRMATION, Confidence.LOW,
                              (Evidence(EvidenceSource.BODY, "soon"),), "ambiguous"),
            ),
        )


class EvaluationTests(unittest.TestCase):
    def test_loads_synthetic_cases(self):
        cases = load_cases("examples/synthetic_rfqs.jsonl")
        self.assertEqual(len(cases), 3)
        self.assertIn("unit_price", cases[0].expected_required_fields)

    def test_reports_micro_precision_recall_and_latency(self):
        cases = [
            EvaluationCase("one", "RFQ", "Please quote", frozenset({"unit_price", "lead_time_weeks"})),
            EvaluationCase("two", "RFQ", "Please state MOQ", frozenset({"unit_price"})),
        ]
        report = evaluate(PartialInferencer(), cases)
        self.assertEqual(report.precision, 0.5)
        self.assertEqual(report.recall, 1 / 3)
        self.assertEqual(len(report.cases), 2)
        self.assertGreaterEqual(report.p95_latency_seconds, 0)

    def test_confidence_report_uses_only_explicit_human_field_labels(self):
        case = EvaluationCase(
            "one", "RFQ", "Please quote; MOQ; ship soon",
            frozenset({"unit_price", "lead_time_weeks"}),
            {"unit_price": Requirement.REQUIRED,
             "moq": Requirement.OPTIONAL,
             "lead_time_weeks": Requirement.REQUIRED},
        )
        report = evaluate(ConfidenceInferencer(), [case])
        self.assertEqual(report.confidence_stats["high"].agreement_rate, 1.0)
        self.assertEqual(report.confidence_stats["medium"].agreement_rate, 0.0)
        self.assertIsNone(report.confidence_stats["low"].agreement_rate)
        self.assertEqual(report.confidence_stats["low"].gold_required_pending, 1)
        self.assertEqual(report.cases[0].decisions[2].expected_requirement, "required")

        unlabeled = EvaluationCase("two", "RFQ", case.body, case.expected_required_fields)
        unlabeled_report = evaluate(ConfidenceInferencer(), [unlabeled])
        self.assertEqual(unlabeled_report.confidence_stats["high"].predicted, 1)
        self.assertEqual(unlabeled_report.confidence_stats["high"].labeled, 0)
        self.assertIsNone(unlabeled_report.confidence_stats["high"].agreement_rate)

    def test_loader_rejects_conflicting_human_label(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(
                '{"id":"one","subject":"RFQ","body":"quote","expected_required_fields":'
                '["unit_price"],"expected_field_requirements":{"unit_price":"optional"}}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
