"""将买方邮件整理成 Schema Agent 所需的本地输入。"""

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "div", "p", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"div", "p", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def _addresses(header_values: list[str]) -> tuple[str, ...]:
    # 地址统一为小写并去重；显示名不参与供应商身份判断。
    return tuple(dict.fromkeys(address.strip().casefold() for _, address in getaddresses(header_values) if address.strip()))


@dataclass(frozen=True)
class RFQEmail:
    """邮件正文及线程绑定信息；附件 v1 只记录名称，不解析内容。"""

    thread_id: str
    subject: str
    body: str
    buyer_email: str
    suppliers: tuple[str, ...]
    attachment_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.thread_id.strip() or not self.subject.strip() or not self.body.strip():
            raise ValueError("RFQ thread_id, subject and body are required")
        if not self.buyer_email.strip() or not self.suppliers:
            raise ValueError("buyer and supplier addresses are required")

    @classmethod
    def from_message(
        cls, message: EmailMessage, *, thread_id: str, agent_address: str
    ) -> "RFQEmail":
        """仅接受将 ForgeFlow 抄送的买方 RFQ 邮件。"""
        buyer = _addresses(message.get_all("From", []))
        to = _addresses(message.get_all("To", []))
        cc = _addresses(message.get_all("Cc", []))
        agent = agent_address.strip().casefold()
        if not thread_id.strip() or not agent or len(buyer) != 1:
            raise ValueError("thread_id, agent_address and one buyer sender are required")
        if agent not in cc:
            raise ValueError("ForgeFlow must be copied in Cc")

        suppliers = tuple(address for address in dict.fromkeys((*to, *cc)) if address not in {agent, buyer[0]})
        if not suppliers:
            raise ValueError("at least one supplier recipient is required")

        subject = str(message.get("Subject", "")).strip()
        part = message.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(body)
            body = parser.text()
        body = body.strip()
        if not subject or not body:
            raise ValueError("RFQ subject and body are required")

        attachments = tuple(
            filename
            for item in message.iter_attachments()
            if (filename := item.get_filename())
        )
        return cls(thread_id, subject, body, buyer[0], suppliers, attachments)
