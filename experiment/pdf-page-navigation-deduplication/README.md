# PDF 分页导航查重实验

## 用途与假设

本实验验证长候选合同进入 `judge_with_page_navigation_agent` 后，模型能否通过有限次查看候选页，将严重缺页的同源上传件判断为 `duplicate`。同时旁路验证同一上传件的尾页加权页面融合向量能否通过当前 Elasticsearch 相似度门槛召回原件，以区分“向量召回失败”和“分页判断失败”。

实验假设如下：

- 从 21 页原件保留第 1、2、11、20、21 页形成的上传件，属于同一合同的不完整副本，按当前业务规则应判为 `duplicate`。
- 上传件与 21 页候选合计页数和视觉 token 超过单次全量判断限制，应稳定进入 `page_navigation_agent`。
- 分页 Agent 应优先查看候选首尾页和必要的中间页，并在 6 次查看、12 个不同候选页和 24 轮模型调用的上限内形成终态。

若目标原件未被 ES 召回，但定向分页判断成功，说明分页判断能力可用而当前融合向量或召回阈值对严重缺页样本过严。若已召回但分页判断失败，则问题位于导航策略、提示词、视觉输入或工具协议。

## 实验设计

候选固定为开发索引中以下 21 页合同：

`金华泰/现象光伏科技201实验室改造项目-合同扫描件_已签章.pdf`

其处理版 PDF 必须位于 `data/contract/<document_id>.pdf`，并且 ES `file_uri`、文件 SHA-256 与 `document_id` 一致。实验从该处理版 PDF 复制物理页 1、2、11、20、21，生成确定性的 5 页缺页版本，再进入生产 `AsyncPDFPreparationService`。

运行顺序如下：

1. 校验目标 ES 文档和本地处理版 PDF 身份。
2. 准备 5 页上传件，调用正式逐页向量化和尾页 1.5 倍融合节点。
3. 调用正式 ES Top 3 阈值召回节点，记录目标是否被召回和直接余弦相似度。
4. 从 ES 按稳定 `document_id` 定向加载目标候选，调用正式路由函数。
5. 仅在正式路由为 `page_navigation_agent` 时调用正式分页导航节点，保留完整工具审计和推理指标。

定向加载不会伪装成真实召回：`result.json` 会分别记录 `target_recalled` 和分页节点结果。这样能够在一次昂贵模型实验中同时获得两个组件的独立结论。

## 指标与判定

- `target_recalled`：目标长合同是否出现在正式 ES Top 3 阈值召回结果中。
- `target_cosine_similarity`：上传件融合向量与 ES 目标 `page_fusion` 向量的直接余弦相似度。
- `route_eligible`：正式路由是否为 `page_navigation_agent`。
- `actual_relation`：分页节点最终关系；人工预期为 `duplicate`。
- `inspection_count`：工具审计中成功的 `inspect_candidate_pages` 调用数。
- `unique_candidate_pages_viewed`：成功查看过的候选物理页去重数。
- `rounds`：分页节点模型轮数。
- `end_to_end_elapsed_ms`：从上传件准备开始到分页判断结束的总耗时。
- `embedding_elapsed_ms`、`retrieval_elapsed_ms` 和 `judgment_elapsed_ms`：三个阶段各自耗时。
- 模型请求耗时、prompt/completion/cached token：由推理指标旁路观测器记录，不与端到端耗时混用。

判定分成两层：

- 分页节点通过：目标存在、文件身份校验通过、正式路由正确、最终关系为 `duplicate` 且未超预算。
- 端到端查重通过：在分页节点通过基础上，目标还必须被正式 ES 召回。

总状态只有两层都通过时为 `passed`；分页节点通过但召回失败时为 `partial`；未形成可靠判断或关系错误时为 `failed`；外部服务不可达时为 `inconclusive`。

## 运行方式

前置条件：

- `.env` 中的 Elasticsearch、MLLM 与 Embedding 服务可达。
- 目标长合同已通过 `scripts/ingest_development_contracts.py` 写入当前正式开发索引。
- 目标处理版 PDF 位于 `data/contract`。

执行：

```bash
.venv/bin/python experiment/pdf-page-navigation-deduplication/run.py
```

每次运行创建新的 UTC 时间戳目录，不覆盖旧产物。

## 产物说明

- `manifest.json`：输入哈希、固定缺页方案、模型、路由、召回及运行环境配置。
- `input.json`：上传件与目标候选的页数、视觉 token 和稳定身份。
- `retrieval.json`：正式 Top 3 召回候选、目标直接余弦相似度与门槛。
- `routing.json`：正式路由决定。
- `judgment.json`：分页节点完整结构化终态与工具调用审计。
- `inference-metrics.json`：不包含合同正文的 Embedding/MLLM 请求指标。
- `result.json`：组件状态、质量判定、预算使用和耗时汇总。
- `analysis.md`：运行完成后的人工分析，只追加且不改写上述原始产物。

## 局限性

- 仅有一份 21 页中文合同和一种固定缺页方案，不能代表生产分布。
- 人工预期来自文件同源关系，没有双人采购或法务盲审。
- 上传件从已处理版原件抽页，因此不覆盖拍照、旋转、印章遮挡、重扫描和版式重排。
- 定向分页判断用于隔离节点能力，不等价于真实召回链路；总状态会单独要求召回通过。
- 远端服务负载、prefix cache 状态和模型随机性会影响耗时与工具轨迹。
