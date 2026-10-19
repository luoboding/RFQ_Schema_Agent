"""本地 .eml 邮件处理和表单查询入口。"""

import argparse
import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Sequence

from rfq_schema_agent.agent import RFQSchemaAgent
from rfq_schema_agent.dashboard import serve_dashboard
from rfq_schema_agent.deepseek import DeepSeekModel
from rfq_schema_agent.email_input import RFQEmail
from rfq_schema_agent.imap_ingest import IMAPSettings, sync_imap, watch_imap
from rfq_schema_agent.inference import ModelFormInferencer, StructuredTextModel
from rfq_schema_agent.models import RFQCollectionForm
from rfq_schema_agent.repository import SQLiteFormRepository


def process_eml_file(
    email_path: str | Path,
    *,
    thread_id: str,
    agent_address: str,
    database_path: str | Path,
    model: StructuredTextModel | None = None,
) -> RFQCollectionForm:
    """解析一封本地邮件，首次生成表单；重复调用返回当前修订。"""
    message = BytesParser(policy=policy.default).parsebytes(Path(email_path).read_bytes())
    email = RFQEmail.from_message(message, thread_id=thread_id, agent_address=agent_address)
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    repository = SQLiteFormRepository(database)

    # 已处理的线程无需密钥和新的模型请求。
    existing = repository.get(email.thread_id)
    if existing is not None:
        return existing
    inferencer = ModelFormInferencer(model if model is not None else DeepSeekModel())
    return RFQSchemaAgent(inferencer, repository).process(email)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Process local RFQ email and inspect saved collection forms")
    parser.add_argument("--db", default="data/rfq_forms.sqlite3", help="Local SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)

    process = commands.add_parser("process", help="Process one buyer RFQ saved as .eml")
    process.add_argument("--email", required=True, help="Path to the .eml file")
    process.add_argument("--thread-id", required=True, help="Thread ID from the email provider")
    process.add_argument("--agent-address", required=True, help="ForgeFlow address in Cc")

    show = commands.add_parser("show", help="Show the current form for a thread")
    show.add_argument("--thread-id", required=True)

    serve = commands.add_parser("serve", help="Open the local buyer review dashboard")
    serve.add_argument("--port", type=int, default=8765)

    sync = commands.add_parser("sync-imap", help="Read new buyer RFQs from IMAP")
    sync.add_argument("--include-existing", action="store_true", help="Process existing mail on first sync")
    sync.add_argument("--batch-size", type=int, default=50)

    watch = commands.add_parser("watch-imap", help="Continuously poll IMAP for new buyer RFQs")
    watch.add_argument("--include-existing", action="store_true", help="Process existing mail on first sync")
    watch.add_argument("--batch-size", type=int, default=50)
    watch.add_argument("--poll-seconds", type=float, default=5)

    args = parser.parse_args(argv)
    if args.command == "process":
        form = process_eml_file(
            args.email,
            thread_id=args.thread_id,
            agent_address=args.agent_address,
            database_path=args.db,
        )
    elif args.command == "show":
        form = SQLiteFormRepository(args.db).get(args.thread_id)
        if form is None:
            parser.error(f"no form found for thread {args.thread_id}")
    elif args.command == "serve":
        database = Path(args.db)
        database.parent.mkdir(parents=True, exist_ok=True)
        try:
            serve_dashboard(SQLiteFormRepository(database), port=args.port)
        except KeyboardInterrupt:
            pass
        return
    else:
        database = Path(args.db)
        database.parent.mkdir(parents=True, exist_ok=True)
        settings = IMAPSettings.from_dotenv()
        repository = SQLiteFormRepository(database)
        inferencer = ModelFormInferencer(DeepSeekModel())
        if args.command == "sync-imap":
            result = sync_imap(
                settings, repository, inferencer,
                include_existing=args.include_existing,
                batch_size=args.batch_size,
            )
            print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        else:
            try:
                watch_imap(
                    settings, repository, inferencer,
                    include_existing=args.include_existing,
                    batch_size=args.batch_size,
                    poll_seconds=args.poll_seconds,
                    on_result=lambda result: print(json.dumps(result.__dict__, ensure_ascii=False), flush=True),
                    on_error=lambda error: print(f"IMAP sync failed: {error}", file=sys.stderr, flush=True),
                )
            except KeyboardInterrupt:
                pass
        return
    print(json.dumps(form.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
