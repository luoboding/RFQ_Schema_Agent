# RFQ Schema Agent

ForgeFlow 的 RFQ Schema Agent 从买方原始询价邮件推导一份动态采集表单，并将其绑定到邮件线程，供 Supplier Extraction Agent 和 RFQ Action Agent 使用。

## 当前阶段

项目结构已初始化；邮件解析、字段推导、持久化和仪表板尚未实现。产品范围见 [PRD](docs/PRD.md)。

## 目录

```text
src/rfq_schema_agent/  Python 包
tests/                 自动化测试
docs/                  产品与设计文档
```

## 开发环境

需要 Python 3.11 或更新版本。当前无运行时依赖。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## 建议的下一步

先定义 `RFQCollectionForm`、字段判定和推导依据的数据契约，再实现 Schema Agent 与线程持久化。低置信度决策保留给采购员确认。
