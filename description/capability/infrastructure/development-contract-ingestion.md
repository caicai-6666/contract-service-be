# 开发合同入库脚本

> **用途：** 使用正式合同提取执行器处理 `data/contract` 中的 PDF，并将开发测试文档写入配置的 Elasticsearch 合同索引。

---

## 行为边界

脚本位于 `scripts/ingest_development_contracts.py`，用于开发环境构造可检索合同数据，不是人工审核接口。合同分类、Core、条款和问题融合向量均来自正式提取流程；审核人和入库时间由脚本提供。

页面视觉向量复用 `contract-near-duplicate-v1` 输入契约：每页并发向量化，尾页权重为 `1.5`，其余页面权重为 `1.0`，加权平均后再次执行 L2 归一化。处理版 PDF 保存为 `data/contract/<document_id>.pdf`，其中 `document_id` 是处理版字节的 SHA-256；ES 中的 `file_uri` 固定写为 `/<document_id>.pdf`，PDF 查重加载器将其直接拼接到 `data/contract` 文件根目录。

脚本默认审核人为 `jason`，入库时间使用运行时的 `Asia/Shanghai` 当前时间。相同合同再次运行时默认跳过已存在的 ES `_id`，便于失败后续跑；传入 `--overwrite` 才会重新提取并更新测试文档。

---

## 使用方式

确保 MLLM、Embedding 和 Elasticsearch 均可由当前 `.env` 访问，然后执行：

```bash
.venv/bin/python scripts/ingest_development_contracts.py
```

可显式覆盖输入目录、审核人或目标索引：

```bash
.venv/bin/python scripts/ingest_development_contracts.py \
  --input-dir data/contract \
  --reviewer jason \
  --index contracts-v1
```

只处理一份文件时使用 `--file`；处理版默认仍统一保存到 `data/contract`：

```bash
.venv/bin/python scripts/ingest_development_contracts.py \
  --file data/input/test-data/大肯科技合同.pdf
```

默认目标为 `ELASTICSEARCH_INDEX_NAME`。脚本写入的是开发测试数据；不要在生产环境使用 mock 审核信息。

---

## 写入结构

写入文档遵循[合同 Elasticsearch 文档结构](../../architecture/data/contract-elasticsearch-document.md)：

- `_id` 与 `document_id` 使用处理版 PDF 哈希。
- `classification` 只保留最终分类身份和场景。
- `core` 按定义中的稳定 `code` 投影，仅保存成功提取值。
- `clauses` 只保存成功提取的条款结构与正文。
- `vectors.question_fusion` 来自正式问题生成与向量融合流程。
- `vectors.page_fusion` 来自逐页视觉向量的尾页加权融合。

单份合同失败时脚本打印错误并继续处理下一份。Core、条款、问题融合向量或页面融合向量失败时均不写入该合同；条款结果为 `failed` 或没有任何成功条款时也会拒绝入库，避免用 ES 写入成功掩盖提取失败。已有文档默认跳过，可用 `--overwrite` 强制重跑。
