"""仅在本机开放的表单预览和采购员审核页面。"""

import html
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from rfq_schema_agent.models import Requirement, ReviewStatus, RFQCollectionForm
from rfq_schema_agent.repository import RevisionConflictError, SQLiteFormRepository
from rfq_schema_agent.review import save_review


STYLE = """<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#17212b}
a{color:#075baa}table{border-collapse:collapse;width:100%}td,th{padding:.65rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}
.card{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}.pending{border-color:#ba7900;background:#fff9e8}
small{color:#48535e}select,button{font:inherit;padding:.35rem}button{margin-top:1rem}
</style>"""


def _page(title: str, content: str) -> bytes:
    return ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{STYLE}<body>"
            f"<h1>{html.escape(title)}</h1>{content}</body></html>").encode("utf-8")


def render_index(forms: list[RFQCollectionForm]) -> bytes:
    rows = []
    for form in forms:
        href = "/form?thread_id=" + quote(form.thread_id, safe="")
        status = "已确认" if form.review_status is ReviewStatus.CONFIRMED else "待审核"
        rows.append(
            f"<tr><td><a href='{html.escape(href, quote=True)}'>{html.escape(form.rfq_summary)}</a>"
            f"<br><small>{html.escape(form.thread_id)}</small></td>"
            f"<td>{status}</td><td>{len(form.pending_fields)}</td><td>{form.revision}</td></tr>"
        )
    content = ("<p>本机保存的 RFQ 采集表单</p><table><thead><tr><th>询价</th><th>状态</th>"
               "<th>待确认字段</th><th>版本</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table>")
    return _page("RFQ 表单审核", content)


def render_form(form: RFQCollectionForm, csrf_token: str) -> bytes:
    cards = []
    for decision in form.decisions:
        options = []
        choices = [Requirement.REQUIRED, Requirement.OPTIONAL]
        if decision.requirement is Requirement.PENDING_CONFIRMATION:
            choices.append(Requirement.PENDING_CONFIRMATION)
        labels = {Requirement.REQUIRED: "必填", Requirement.OPTIONAL: "可选",
                  Requirement.PENDING_CONFIRMATION: "待确认"}
        for choice in choices:
            selected = " selected" if choice is decision.requirement else ""
            options.append(f"<option value='{choice.value}'{selected}>{labels[choice]}</option>")
        evidence = "".join(
            f"<li>{html.escape(item.source.value)}：{html.escape(item.excerpt)}</li>"
            for item in decision.evidence
        )
        pending_class = " pending" if decision.requirement is Requirement.PENDING_CONFIRMATION else ""
        cards.append(
            f"<section class='card{pending_class}'><h2>{html.escape(decision.field)}</h2>"
            f"<p>类型：{html.escape(str(form.field_definitions[decision.field].get('type')))} "
            f"· 置信度：{html.escape(decision.confidence.value)}</p>"
            f"<p>{html.escape(decision.reason)}</p><ul>{evidence}</ul>"
            f"<label>必要性 <select name='field:{html.escape(decision.field, quote=True)}'>"
            f"{''.join(options)}</select></label></section>"
        )
    suppliers = "、".join(html.escape(value) for value in form.suppliers)
    content = (
        "<p><a href='/'>返回列表</a></p>"
        f"<p>{html.escape(form.rfq_summary)}</p>"
        f"<p>线程：{html.escape(form.thread_id)}<br>供应商：{suppliers}<br>版本：{form.revision}</p>"
        "<form action='/review' method='post'>"
        f"<input type='hidden' name='csrf_token' value='{html.escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='thread_id' value='{html.escape(form.thread_id, quote=True)}'>"
        f"<input type='hidden' name='revision' value='{form.revision}'>"
        + "".join(cards)
        + "<label><input type='checkbox' name='confirm' value='yes'>确认整份表单"
        "（需先处理全部待确认字段）</label><br><button type='submit'>保存新版本</button></form>"
    )
    return _page("审核 RFQ 采集表单", content)


def make_handler(repository: SQLiteFormRepository) -> type[BaseHTTPRequestHandler]:
    """创建带独立 CSRF 令牌的 HTTP 处理器。"""
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # 不把邮件线程 ID 写入终端访问日志。

        def _allowed_host(self) -> bool:
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def _send(self, status: int, body: bytes, *, location: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._allowed_host():
                self._send(403, _page("拒绝访问", "<p>无效的 Host。</p>"))
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                self._send(200, render_index(repository.list_latest()))
            elif parsed.path == "/form":
                thread_ids = parse_qs(parsed.query).get("thread_id", [])
                form = repository.get(thread_ids[0]) if len(thread_ids) == 1 else None
                if form is None:
                    self._send(404, _page("未找到", "<p>表单不存在。</p>"))
                else:
                    self._send(200, render_form(form, csrf_token))
            else:
                self._send(404, _page("未找到", "<p>页面不存在。</p>"))

        def do_POST(self) -> None:
            if not self._allowed_host():
                self._send(403, _page("拒绝访问", "<p>无效的 Host。</p>"))
                return
            if urlsplit(self.path).path != "/review":
                self._send(404, _page("未找到", "<p>页面不存在。</p>"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 65536 or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                self._send(400, _page("请求错误", "<p>表单请求无效。</p>"))
                return
            try:
                values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

                def one(name: str) -> str:
                    items = values.get(name, [])
                    if len(items) != 1:
                        raise ValueError(f"invalid {name}")
                    return items[0]

                if not secrets.compare_digest(one("csrf_token"), csrf_token):
                    self._send(403, _page("拒绝访问", "<p>令牌无效。</p>"))
                    return
                requirements = {
                    key.removeprefix("field:"): Requirement(one(key))
                    for key in values if key.startswith("field:")
                }
                thread_id = one("thread_id")
                save_review(
                    repository, thread_id,
                    expected_revision=int(one("revision")),
                    requirements=requirements,
                    confirm=values.get("confirm") == ["yes"],
                )
            except RevisionConflictError:
                self._send(409, _page("版本冲突", "<p>表单已更新，请返回列表重新打开。</p>"))
                return
            except (ValueError, UnicodeDecodeError) as error:
                self._send(400, _page("请求错误", f"<p>{html.escape(str(error))}</p>"))
                return
            self._send(303, b"", location="/form?thread_id=" + quote(thread_id, safe=""))

    return Handler


def serve_dashboard(repository: SQLiteFormRepository, *, port: int = 8765) -> None:
    """在 127.0.0.1 启动审核页，按 Ctrl+C 停止。"""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    server = HTTPServer(("127.0.0.1", port), make_handler(repository))
    try:
        print(f"RFQ 审核页：http://127.0.0.1:{port}/", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
