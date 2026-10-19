"""采购员在本地审核字段必要性的应用逻辑。"""

from dataclasses import replace
from typing import Mapping

from rfq_schema_agent.models import Confidence, Requirement, ReviewStatus, RFQCollectionForm
from rfq_schema_agent.repository import RevisionConflictError, SQLiteFormRepository


def save_review(
    repository: SQLiteFormRepository,
    thread_id: str,
    *,
    expected_revision: int,
    requirements: Mapping[str, Requirement],
    confirm: bool,
) -> RFQCollectionForm:
    """保存整份字段判定；确认时不允许留下待确认项。"""
    current = repository.get(thread_id)
    if current is None or current.revision != expected_revision:
        raise RevisionConflictError("form revision changed or thread does not exist")
    if set(requirements) != set(current.field_definitions):
        raise ValueError("review must include every existing field exactly once")

    decisions = []
    for decision in current.decisions:
        requirement = Requirement(requirements[decision.field])
        if requirement is Requirement.PENDING_CONFIRMATION and decision.requirement is not Requirement.PENDING_CONFIRMATION:
            raise ValueError("only existing pending fields may remain pending")
        decisions.append(replace(
            decision,
            requirement=requirement,
            confidence=(Confidence.HIGH if decision.requirement is Requirement.PENDING_CONFIRMATION
                        and requirement is not Requirement.PENDING_CONFIRMATION else decision.confidence),
        ))

    updated = replace(
        current,
        decisions=tuple(decisions),
        revision=current.revision + 1,
        review_status=ReviewStatus.CONFIRMED if confirm else ReviewStatus.DRAFT,
    )
    return repository.save_buyer_revision(updated, expected_revision=expected_revision)
