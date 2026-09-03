# 合同提取质量与推理指标实验

> **用途：** 对 `data/input/test-data` 中的全部 PDF 顺序执行当前完整合同提取流程，观察结构化提取完成度、失败分布和逐请求推理性能。实验不使用人工标注答案，因此不把结构完整性指标解释为事实准确率。

---

## 用途与假设

实验验证以下假设：

- 当前工作流能够处理测试集中的每份 PDF，并分别形成分类、Core、Clause 和 Retrieval 结果。
- 模型工具协议与程序校验能够把单节点失败隔离在对应类别、字段、条款或问题中。
- 启用 `--enable-per-request-metrics` 的 MLLM 会在响应中提供逐请求 TTFT、排队、生成耗时、ITL 和吞吐指标，应用侧采集器能够完整保留这些数值。
- Embedding 服务能够为成功问题返回配置维度的向量；若服务不提供 vLLM 顶层 `metrics`，实验仍记录客户端耗时和 token。

若任一合同无法完成公共前置阶段，或全部三个业务分支均失败，则当前测试集上的端到端可用性假设不成立。若结果结构完整但缺少人工真值，只能得出运行通过或待人工复核，不能得出准确率通过。

---

## 实验设计

- 输入：默认递归读取 `data/input/test-data/**/*.pdf`，忽略 `.txt` 等非 PDF 文件。
- 顺序：按相对路径字典序逐合同执行，避免不同合同之间的并发负载互相污染。
- 合同内部：复用生产 `AgentContractExtractionExecutor`，保留分类、Core、Clause 和 Retrieval 的既有并发行为。
- 固定条件：目录快照、模型、生成参数、视觉预算和并发配置均来自当前 `.env` 与启动期配置。
- 对照组：无；本实验建立当前实现的单组基线。
- 缓存：不设置 `cache_salt`，保留生产请求的既有前缀缓存行为；样本顺序写入 manifest。

实验只保存合同提取结构、审计、进度和指标，不复制 PDF 图像字节、模型输入消息或 API Key。检索向量仅保存数量、维度和归一化状态，不在实验 JSON 中复制高维浮点数组。

---

## 指标与判定

### 提取效果

| 指标 | 口径 |
| --- | --- |
| 合同完成率 | 公共前置阶段成功且三个业务分支至少一个成功的合同数 / PDF 数。 |
| 分类状态 | `classified`、`unmapped`、`partial` 或失败，以及命中类别数量。 |
| Core 完成度 | `extracted`、`abandoned`、`failed` 字段数量和成功对象数量。 |
| Clause 完成度 | 成功与失败条款数量、成功正文字符数。 |
| Retrieval 完成度 | 问题数量、成功向量数量和合同向量状态。 |
| 结构定位完成度 | 文档单元数量及 `located`、`failed` 数量。 |

程序 Schema、页码和证据校验通过只说明结果满足机器契约。由于没有逐字段、逐条款人工真值，本实验不计算 precision、recall 或事实准确率；最终验证状态最多为“运行通过，质量待人工复核”。

### 推理指标

按全部请求、模型类型和业务阶段分别统计：

- 请求数、成功数、失败数和服务端指标覆盖数。
- prompt、cached、completion、total token 总数。
- 客户端端到端请求耗时的总和、平均值、P50、P95 和最大值。
- vLLM `time_to_first_token_ms`、`queue_time_ms`、`generation_time_ms`、`mean_itl_ms`、`tokens_per_second` 的样本数、平均值、P50、P95 和最大值。

性能指标只用于描述本次远程服务和并发条件下的表现，不替代提取质量判断。

---

## 运行方式

前置条件：

- 从项目根目录创建虚拟环境并安装 `requirements.txt`。
- `.env` 已配置可访问的 `VLLM_MLLM_BASE_URL` 和 `VLLM_EMBEDDING_BASE_URL`。
- MLLM 使用 `--enable-per-request-metrics` 启动，才能返回逐请求服务端指标。

执行全部测试 PDF：

```bash
.venv/bin/python experiment/contract-extraction-quality/run.py
```

只执行前一份样本用于冒烟验证：

```bash
.venv/bin/python experiment/contract-extraction-quality/run.py --max-contracts 1
```

自定义输入目录：

```bash
.venv/bin/python experiment/contract-extraction-quality/run.py \
    --input-dir /absolute/path/to/contracts
```

每次执行都会创建新的 UTC 时间戳目录，不覆盖历史产物。运行中断时，已经完成的单合同原始产物仍保留，但顶层 `result.json` 只在运行器正常收尾时生成。

---

## 产物说明

```text
output/<UTC 时间>/
├── manifest.json
├── result.json
├── samples/
│   └── <序号>-<文档短哈希>/
│       ├── extraction.json
│       ├── inference-metrics.json
│       └── progress.json
└── analysis.md
```

- `manifest.json`：代码版本、样本哈希、目录指纹、去密钥模型配置和执行顺序。
- `result.json`：单合同结构化指标及全局聚合指标。
- `extraction.json`：不含图片字节、公共模型消息和高维向量的模型结果与私有审计。
- `inference-metrics.json`：逐模型请求的客户端耗时、token 和 vLLM 响应指标，不含提示词与回答正文。
- `progress.json`：预处理节点和四类并行任务的实际进度事件。
- `analysis.md`：实验完成后的人工分析；只能追加，不由运行器生成。

---

## 局限性

- 当前 5 份 PDF 不能代表全部合同类型、扫描质量、语言和页数分布。
- 没有人工标注真值，不能判断抽取事实是否正确或遗漏重要条款。
- 远程 vLLM 的共享负载、网络、前缀缓存和首次模型热身会影响耗时。
- 合同按顺序运行，但单合同内部存在生产并发，因此请求级延迟包含真实排队竞争。
- `--enable-per-request-metrics` 可能带来额外 CPU 开销；本实验不与关闭指标的服务作性能对照。
- 原始产物可能包含测试合同提取出的业务内容，必须继续保持在项目本地忽略目录中。
