# RFQCollectionForm 数据契约（v1.0）

Python 定义位于 `src/rfq_schema_agent/models.py`。调用 `RFQCollectionForm.to_dict()` 得到供下游 Agent 和仪表板消费的 JSON 对象。

## 判定状态

| `requirement` | 顶层列表 | 下游含义 |
|---|---|---|
| `required` | `required_fields` | Extraction Agent 提取；Action Agent 检查缺失 |
| `optional` | `optional_fields` | 可提取，不触发缺失跟进 |
| `pending_confirmation` | `pending_fields` | 仪表板提示采购员；确认前不计入必填或可选 |

低置信度判定必须进入 `pending_confirmation`，并提供 `reason` 和至少一段邮件依据。必填判定也必须附依据。依据由 `source`（`subject` 或 `body`）和 `excerpt` 组成。v1 暂不将附件作为可引用的依据来源。

`field_decisions` 是逐字段判定的完整记录；三个字段列表和 `low_confidence_decisions` 都由它派生，避免相互冲突。`field_definitions` 的键必须与判定字段一一对应，字段名允许 RFQ 特有的新字段。字段定义目前沿用 PRD 中的 JSON 元数据形状，例如 `{"type":"number","currency_required":true}`；具体报价值格式将在 Extraction Agent 接口设计时细化。

## 线程与版本

- `thread_id` 标识一条 RFQ 邮件线程；一个表单关联一个或多个供应商。
- `schema_version` 表示接口格式版本，当前为 `1.0`。
- `revision` 表示同一线程表单的修订次数，从 1 开始；SQLite 存储保留所有历史版本及修改来源。
- `review_status` 为 `draft` 或 `confirmed`。仍有待确认字段的表单不能标为 `confirmed`。
- `created_at` 必须包含时区。后续修订的时间和操作者由持久化审计记录保存。

`SQLiteFormRepository.create_if_absent()` 只写入第 1 版 Agent 草稿。重复调用会返回已有的最新版本，包括采购员已确认的版本。`save_buyer_revision()` 使用预期修订号检查并发修改，每次成功修改都追加新版本；`history()` 可读取审计记录。调用方仍需负责验证执行“采购员修改”的用户身份。

本地审核页提交时必须为每个现有字段指定必要性。采购员处理低置信度的待确认字段后，当前修订将其置信度标为 `high`，表示该判定已由采购员确认；Agent 原始的低置信度判定仍保留在上一修订的审计记录中。只有没有待确认字段时，整份表单才能标记为 `confirmed`。

模型输出现在会解析成 `FieldDecision`，并检查每段依据确实出现在邮件主题或正文中。字段定义内部格式的严格校验以及真实模型接入属于后续步骤。
