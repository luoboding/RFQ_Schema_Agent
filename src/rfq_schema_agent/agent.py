"""从买方 RFQ 邮件生成并保存采集表单的应用流程。"""

from datetime import datetime, timezone
from typing import Protocol

from rfq_schema_agent.email_input import RFQEmail
from rfq_schema_agent.inference import FormProposal
from rfq_schema_agent.models import EvidenceSource, RFQCollectionForm
from rfq_schema_agent.repository import SQLiteFormRepository


class FormInferencer(Protocol):
    def infer(self, subject: str, body: str) -> FormProposal: ...


class RFQSchemaAgent:
    def __init__(self, inferencer: FormInferencer, repository: SQLiteFormRepository) -> None:
        self.inferencer = inferencer
        self.repository = repository

    def process(self, email: RFQEmail) -> RFQCollectionForm:
        """同一线程只推导初稿一次；再次触发直接返回当前表单。"""
        existing = self.repository.get(email.thread_id)
        if existing is not None:
            return existing

        proposal = self.inferencer.infer(email.subject, email.body)
        for decision in proposal.decisions:
            for evidence in decision.evidence:
                original = email.subject if evidence.source is EvidenceSource.SUBJECT else email.body
                if evidence.excerpt not in original:
                    raise ValueError(f"evidence for {decision.field} is not in the RFQ email")

        form = RFQCollectionForm(
            thread_id=email.thread_id,
            rfq_summary=proposal.rfq_summary,
            suppliers=email.suppliers,
            field_definitions=proposal.field_definitions,
            decisions=proposal.decisions,
            created_at=datetime.now(timezone.utc),
        )
        return self.repository.create_if_absent(form)
