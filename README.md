# RFQ Schema Agent

ForgeFlow 的 RFQ Schema Agent 从买方原始询价邮件推导一份动态采集表单，并将其绑定到邮件线程，供 Supplier Extraction Agent 和 RFQ Action Agent 使用。

## 当前阶段

项目结构、表单数据契约、基于 SQLAlchemy ORM 的本地 SQLite 存储、邮件处理流程、DeepSeek 适配器、IMAP 同步和本地审核页已建立。产品范围见 [PRD](docs/PRD.md)，当前进度见 [阶段任务与完成情况](docs/phase-plan.md)，契约说明见 [数据契约](docs/data-contract.md)。

## 目录

```text
src/rfq_schema_agent/  邮件输入、模型推导、表单模型、Agent 编排和 SQLite 存储
tests/                 自动化测试
docs/                  产品与设计文档
```

## 开发环境

需要 Python 3.11 或更新版本。DeepSeek 配置通过 `python-dotenv` 从项目根目录的 `.env` 读取；系统环境变量优先于 `.env`。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
```

在 `.env` 中填写 `DEEPSEEK_API_KEY`。`DEEPSEEK_BASE_URL` 默认为官方 HTTPS 地址；可选的 `DEEPSEEK_MODEL` 默认为 `deepseek-flash`，`DEEPSEEK_TIMEOUT_SECONDS` 默认为 25。`.env` 已被 Git 忽略，不应提交真实密钥。

## 建议的下一步

配置自己的 DeepSeek 密钥与 IMAP 邮箱后验证真实邮件流程，再收集匿名 RFQ 与人工标注，检验 PRD 的召回率、精确率和延迟目标。

## 处理流程

调用方提供邮件内容、邮件系统的 `thread_id` 和 ForgeFlow 的抄送地址，构造 `RFQEmail`。`RFQSchemaAgent.process()` 先读取已有表单；首次处理时只把主题和正文传给 `ModelFormInferencer`，校验模型给出的原文依据，再将草稿写入 SQLite。重复触发返回当前修订。

模型适配器需实现 `generate_json(instructions: str, rfq_text: str) -> dict`。`DeepSeekModel` 已实现这一接口，使用 DeepSeek 的 JSON 输出模式；测试通过模拟响应验证请求格式，没有发送真实网络请求。HTML 邮件会转换为文本，附件仅记录文件名，v1 不解析附件内容。

## 本地邮件与 IMAP

可先用本地 `.eml` 文件运行完整流程；`thread_id` 应来自邮件服务，重复使用同一 ID 会返回已有表单：

```bash
.venv/bin/python -m rfq_schema_agent.cli process --email path/to/rfq.eml --thread-id my-thread --agent-address agent@example.com
.venv/bin/python -m rfq_schema_agent.cli show --thread-id my-thread
```

IMAP 同步需要在 `.env` 中填写 `IMAP_HOST`、`IMAP_USERNAME`、`IMAP_PASSWORD`、`FORGEFLOW_ADDRESS` 和 `RFQ_BUYER_ADDRESSES`（多个买方地址用逗号分隔）。默认连接 993 端口并只读访问 `INBOX`，通过 `BODY.PEEK[]` 抓取邮件。仅处理发件人属于买方名单、且 ForgeFlow 位于 CC 的邮件；原始 RFQ 的 `Message-ID` 用作内部线程 ID。

```bash
.venv/bin/python -m rfq_schema_agent.cli sync-imap
```

首次运行默认记录当前最新 UID，从之后的新邮件开始。若要在**首次运行前**处理邮箱中的既有邮件，使用 `sync-imap --include-existing`；每次最多处理 50 封，可用 `--batch-size` 调整。同步游标保存在同一个本地 SQLite 文件中，处理失败的邮件会在下次运行时重试。

持续监听可运行：

```bash
.venv/bin/python -m rfq_schema_agent.cli watch-imap --poll-seconds 5
```

`watch-imap` 默认每 5 秒检查一次；满批后立即继续读取下一批，发生错误时输出到标准错误并等待下一轮重试。按 Ctrl+C 退出。首次启动时也可指定 `--include-existing` 和 `--batch-size`；`--include-existing` 仅用于首次成功同步。此命令需在运行期间保持进程存活。

## 本地审核页

处理至少一封 RFQ 后，在另一终端运行：

```bash
.venv/bin/python -m rfq_schema_agent.cli serve --port 8765
```

打开 `http://127.0.0.1:8765/`。页面列出当前表单、字段判定、推导原因和邮件原文依据。采购员可把待确认字段改为必填或可选，再保存草稿或勾选确认整份表单。每次保存追加修订；旧页面提交会得到版本冲突提示。服务仅绑定本机地址，默认没有用户登录功能，请只在受信任的本机使用。按 Ctrl+C 停止。

## 评估

合成样例位于 `examples/synthetic_rfqs.jsonl`，用于检查流程和指标计算，不代表真实准确率。配置 `.env` 后，从项目根目录运行以下命令会向 DeepSeek 发送样例邮件的主题和正文，并输出逐例结果、必填字段精确率与召回率、P95 处理延迟：

```bash
.venv/bin/python -m rfq_schema_agent.evaluate --cases examples/synthetic_rfqs.jsonl
```

要衡量 PRD 指标，应另建匿名标注集，通过 `--cases` 指向该 JSONL 文件。每行至少包含 `id`、`subject`、`body` 和 `expected_required_fields`。如需分析置信度，再由采购员添加可选的逐字段标注：

```json
{"id":"rfq-1","subject":"询价","body":"请报价，尽快交货","expected_required_fields":["unit_price"],"expected_field_requirements":{"unit_price":"required","lead_time_weeks":"optional"}}
```

`expected_field_requirements` 可以只标注已审核的字段；未标注字段不参与置信度一致率计算。报告中 `high`、`medium` 的 `agreement_rate` 是其判定与人工标注相符的比例，`low` 的待确认项分别统计人工最终认定为必填或可选的数量。它们是样本统计，不能把模型自报的“高／中／低”解释为经过校准的概率。必填字段精确率和召回率仍根据 `expected_required_fields` 计算。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```
