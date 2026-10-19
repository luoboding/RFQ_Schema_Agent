"""Schema Agent 与提取、跟进 Agent 共用的版本化表单契约。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Requirement(StrEnum):
    """字段对本次 RFQ 的必要性；待确认字段暂不参与缺失判断。"""

    REQUIRED = "required"
    OPTIONAL = "optional"
    PENDING_CONFIRMATION = "pending_confirmation"


class Confidence(StrEnum):
    """Schema Agent 对字段必要性判定的置信度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(StrEnum):
    """采购员对整份采集表单的审核状态。"""

    DRAFT = "draft"
    CONFIRMED = "confirmed"


class EvidenceSource(StrEnum):
    """判定依据所在的邮件部分；v1 只支持主题和正文。"""

    SUBJECT = "subject"
    BODY = "body"


@dataclass(frozen=True)
class Evidence:
    """支撑字段判定的邮件原文片段。"""

    source: EvidenceSource
    excerpt: str

    def __post_init__(self) -> None:
        if not self.excerpt.strip():
            raise ValueError("evidence excerpt cannot be empty")


@dataclass(frozen=True)
class FieldDecision:
    """单个字段的必要性、置信度、依据及待确认原因。"""

    field: str
    requirement: Requirement
    confidence: Confidence
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("field name cannot be empty")
        if self.confidence is Confidence.LOW and self.requirement is not Requirement.PENDING_CONFIRMATION:
            # 低置信度不能被自动判成必填或可选，必须交给采购员确认。
            raise ValueError("low-confidence decisions must await confirmation")
        if self.requirement is Requirement.PENDING_CONFIRMATION:
            if self.confidence is not Confidence.LOW or not self.reason.strip():
                raise ValueError("pending decisions need low confidence and a reason")
        if self.requirement is not Requirement.OPTIONAL and not self.evidence:
            raise ValueError("required and pending decisions need source evidence")


@dataclass(frozen=True)
class RFQCollectionForm:
    """某条 RFQ 邮件线程的采集规则，不包含供应商的实际报价。"""

    thread_id: str
    rfq_summary: str
    suppliers: tuple[str, ...]
    field_definitions: dict[str, dict[str, Any]]
    decisions: tuple[FieldDecision, ...]
    created_at: datetime
    revision: int = 1
    review_status: ReviewStatus = ReviewStatus.DRAFT
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        if not self.thread_id.strip() or not self.rfq_summary.strip():
            raise ValueError("thread_id and rfq_summary are required")
        if not self.suppliers or any(not supplier.strip() for supplier in self.suppliers):
            raise ValueError("at least one supplier is required")
        if len(set(self.suppliers)) != len(self.suppliers):
            raise ValueError("suppliers must be unique")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if self.revision < 1:
            raise ValueError("revision must be positive")

        names = [decision.field for decision in self.decisions]
        # 每个启用的字段恰好对应一项判定，避免下游收到相互矛盾的清单。
        if len(names) != len(set(names)) or set(names) != set(self.field_definitions):
            raise ValueError("each field definition needs exactly one decision")
        supported_types = {"number", "integer", "string", "array", "object", "boolean"}
        if any(
            not isinstance(definition, dict) or definition.get("type") not in supported_types
            for definition in self.field_definitions.values()
        ):
            raise ValueError("each field definition needs a supported JSON value type")
        if self.review_status is ReviewStatus.CONFIRMED and self.pending_fields:
            raise ValueError("a confirmed form cannot have pending fields")

    @property
    def required_fields(self) -> list[str]:
        """供 Action Agent 检查供应商回复是否完整。"""
        return [d.field for d in self.decisions if d.requirement is Requirement.REQUIRED]

    @property
    def optional_fields(self) -> list[str]:
        """可提取，但缺失时不触发跟进的字段。"""
        return [d.field for d in self.decisions if d.requirement is Requirement.OPTIONAL]

    @property
    def pending_fields(self) -> list[str]:
        """等待采购员决定是否纳入必填或可选的字段。"""
        return [d.field for d in self.decisions if d.requirement is Requirement.PENDING_CONFIRMATION]

    def to_dict(self) -> dict[str, Any]:
        """生成供下游 Agent 和仪表板使用的 JSON 结构。"""
        return {
            "schema_version": self.schema_version,
            "thread_id": self.thread_id,
            "rfq_summary": self.rfq_summary,
            "suppliers": list(self.suppliers),
            "required_fields": self.required_fields,
            "optional_fields": self.optional_fields,
            "pending_fields": self.pending_fields,
            "field_definitions": self.field_definitions,
            "field_decisions": [
                {
                    "field": d.field,
                    "requirement": d.requirement.value,
                    "confidence": d.confidence.value,
                    "evidence": [
                        {"source": e.source.value, "excerpt": e.excerpt} for e in d.evidence
                    ],
                    "reason": d.reason,
                }
                for d in self.decisions
            ],
            "low_confidence_decisions": [
                # 保留 PRD 的预览字段；内容从逐字段判定派生，避免重复维护。
                {
                    "field": d.field,
                    "reason": d.reason,
                    "suggested_action": "confirm_with_buyer",
                }
                for d in self.decisions
                if d.requirement is Requirement.PENDING_CONFIRMATION
            ],
            "created_at": self.created_at.isoformat(),
            "revision": self.revision,
            "review_status": self.review_status.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RFQCollectionForm":
        """从持久化的 JSON 结构还原表单并重新检查模型约束。"""
        if payload.get("schema_version") != "1.0":
            raise ValueError("unsupported schema_version")

        decisions = tuple(
            FieldDecision(
                field=item["field"],
                requirement=Requirement(item["requirement"]),
                confidence=Confidence(item["confidence"]),
                evidence=tuple(
                    Evidence(EvidenceSource(e["source"]), e["excerpt"])
                    for e in item["evidence"]
                ),
                reason=item["reason"],
            )
            for item in payload["field_decisions"]
        )
        form = cls(
            thread_id=payload["thread_id"],
            rfq_summary=payload["rfq_summary"],
            suppliers=tuple(payload["suppliers"]),
            field_definitions=payload["field_definitions"],
            decisions=decisions,
            created_at=datetime.fromisoformat(payload["created_at"]),
            revision=payload["revision"],
            review_status=ReviewStatus(payload["review_status"]),
        )
        for key in ("required_fields", "optional_fields", "pending_fields"):
            if payload[key] != getattr(form, key):
                raise ValueError(f"inconsistent {key}")
        return form
