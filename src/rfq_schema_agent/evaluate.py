"""用标注好的 RFQ 样例评估必填字段、置信度分组和处理延迟。"""

import argparse
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from rfq_schema_agent.agent import FormInferencer, RFQSchemaAgent
from rfq_schema_agent.email_input import RFQEmail
from rfq_schema_agent.models import Confidence, Requirement
from rfq_schema_agent.repository import SQLiteFormRepository


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    subject: str
    body: str
    expected_required_fields: frozenset[str]
    expected_field_requirements: dict[str, Requirement] | None = None


@dataclass(frozen=True)
class DecisionResult:
    field: str
    requirement: str
    confidence: str
    expected_requirement: str | None


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected_required_fields: list[str]
    predicted_required_fields: list[str]
    latency_seconds: float
    decisions: list[DecisionResult]
    error: str | None = None


@dataclass(frozen=True)
class ConfidenceStats:
    predicted: int
    labeled: int
    correct: int
    agreement_rate: float | None
    gold_required_pending: int = 0
    gold_optional_pending: int = 0


@dataclass(frozen=True)
class EvaluationReport:
    precision: float
    recall: float
    p95_latency_seconds: float
    confidence_stats: dict[str, ConfidenceStats]
    cases: list[CaseResult]

    def to_dict(self) -> dict:
        return asdict(self)


def load_cases(path: str | Path) -> list[EvaluationCase]:
    """读取 JSONL 标注集；每行是一封独立的 RFQ。"""
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        gold = item.get("expected_field_requirements")
        if gold is not None:
            if not isinstance(gold, dict):
                raise ValueError("expected_field_requirements must be an object")
            gold = {field: Requirement(value) for field, value in gold.items()}
            if any(value is Requirement.PENDING_CONFIRMATION for value in gold.values()):
                raise ValueError("human labels must be required or optional")
            if any(
                (field in item["expected_required_fields"]) != (value is Requirement.REQUIRED)
                for field, value in gold.items()
            ):
                raise ValueError("field labels conflict with expected_required_fields")
        cases.append(EvaluationCase(
            case_id=item["id"],
            subject=item["subject"],
            body=item["body"],
            expected_required_fields=frozenset(item["expected_required_fields"]),
            expected_field_requirements=gold,
        ))
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("evaluation cases must be nonempty and have unique IDs")
    return cases


def evaluate(inferencer: FormInferencer, cases: Iterable[EvaluationCase]) -> EvaluationReport:
    """运行完整生成与持久化流程；失败样例计入漏判。"""
    results = []
    true_positive = false_positive = false_negative = 0
    confidence_counts = {
        confidence.value: {"predicted": 0, "labeled": 0, "correct": 0,
                           "gold_required_pending": 0, "gold_optional_pending": 0}
        for confidence in Confidence
    }
    with tempfile.TemporaryDirectory() as directory:
        agent = RFQSchemaAgent(inferencer, SQLiteFormRepository(Path(directory) / "forms.sqlite3"))
        for case in cases:
            email = RFQEmail(
                thread_id=case.case_id,
                subject=case.subject,
                body=case.body,
                buyer_email="buyer@example.invalid",
                suppliers=("supplier@example.invalid",),
            )
            started = time.perf_counter()
            try:
                form = agent.process(email)
            except Exception as failure:
                predicted = set()
                decisions = []
                error = type(failure).__name__
            else:
                predicted = set(form.required_fields)
                decisions = []
                gold = case.expected_field_requirements or {}
                for decision in form.decisions:
                    expected_requirement = gold.get(decision.field)
                    decisions.append(DecisionResult(
                        field=decision.field,
                        requirement=decision.requirement.value,
                        confidence=decision.confidence.value,
                        expected_requirement=(expected_requirement.value
                                              if expected_requirement is not None else None),
                    ))
                    counts = confidence_counts[decision.confidence.value]
                    counts["predicted"] += 1
                    if expected_requirement is not None:
                        counts["labeled"] += 1
                        if decision.requirement is Requirement.PENDING_CONFIRMATION:
                            key = ("gold_required_pending" if expected_requirement is Requirement.REQUIRED
                                   else "gold_optional_pending")
                            counts[key] += 1
                        elif decision.requirement is expected_requirement:
                            counts["correct"] += 1
                error = None
            latency = time.perf_counter() - started
            expected = case.expected_required_fields
            true_positive += len(predicted & expected)
            false_positive += len(predicted - expected)
            false_negative += len(expected - predicted)
            results.append(CaseResult(
                case_id=case.case_id,
                expected_required_fields=sorted(expected),
                predicted_required_fields=sorted(predicted),
                latency_seconds=latency,
                decisions=decisions,
                error=error,
            ))

    if not results:
        raise ValueError("at least one evaluation case is required")
    latencies = sorted(result.latency_seconds for result in results)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    return EvaluationReport(
        precision=precision,
        recall=recall,
        p95_latency_seconds=latencies[math.ceil(0.95 * len(latencies)) - 1],
        confidence_stats={
            confidence: ConfidenceStats(
                predicted=counts["predicted"],
                labeled=counts["labeled"],
                correct=counts["correct"],
                agreement_rate=(counts["correct"] / counts["labeled"]
                                if counts["labeled"] and confidence != Confidence.LOW.value else None),
                gold_required_pending=counts["gold_required_pending"],
                gold_optional_pending=counts["gold_optional_pending"],
            )
            for confidence, counts in confidence_counts.items()
        },
        cases=results,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate RFQ Schema Agent with annotated JSONL cases")
    parser.add_argument("--cases", default="examples/synthetic_rfqs.jsonl")
    args = parser.parse_args()

    from rfq_schema_agent.deepseek import DeepSeekModel
    from rfq_schema_agent.inference import ModelFormInferencer

    report = evaluate(ModelFormInferencer(DeepSeekModel()), load_cases(args.cases))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
