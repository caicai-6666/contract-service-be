# 合同图像相似检索工具

通过当前会话已准入附件或共享正式合同的完整 PDF 页面，检索 ES `vectors.page_fusion` 中的相似合同。已注册 Agent Core，工具和结果渲染器位于 `agent_core/tool/contract_image_search.py`，由 `CommunicationFileTools` 注入文件查看器、ES 客户端和 SQLite 元数据存储。

---

## 搜索与查看

| 工具 | 参数 | 返回 | SSE |
| --- | --- | --- | --- |
| `search_contracts_by_image` | `source_type`、`file_id`、可选 `result_id`、`top_k` | 成功状态、`result_id`、数量、源文件页数及检索上限，不附带第一页 | `local-search` |
| `view_contract_search_results` | `result_id`、可选 `page` | 分页信息与可折叠页面引用 | `thinking` |

`source_type=session` 使用当前会话附件完整 UUID；`contract` 使用合同完整 SHA-256。不得缩写 ID、传文件路径、使用其他会话附件或将结果集 ID 当作源文件 ID。一次搜索只使用一份完整文件，不接收任意图片 URL、单页范围或自然语言查询。当前文件工具支持的格式为 PDF。

`top_k` 默认 10，范围 1–50。相似度门槛由程序沿用合同提取查重的 `PDF_DEDUP_MINIMUM_RECALL_COSINE_SIMILARITY`（默认 0.60），模型不能指定。该值是原始余弦召回下限，不是最终重复判定阈值。源文件为合同则排除自身，源文件为附件时允许召回同内容的已入库合同。

查看时省略页码：首次第 1 页，其后下一页；显式页码可重看。非法页返回 `invalid_page`，默认翻过末尾返回 `end_of_results`，均不移动游标。零结果返回成功状态、数量 0 和未找到提示，不包含结果集 ID，不占用 LRU，无需调用查看工具。引用无效、驱逐或会话释放返回明确错误，不自动重新查询。

---

## 全页编码与范围

`FileViewer.snapshot_pages()` 在缓存租约内渲染全部物理页，复用已缓存图像且不推进阅读游标；模型已被 LRU 驱逐时通过原有读取器自动重建。附件经历史服务校验当前会话归属和准入状态，优先读取驻留上传字节，归档后回退磁盘。合同读取核对文件身份并检查 SQLite `ready` 状态。

图片检索与查重共用 `pdf_deduplication.node.encode_pdf_pages()`：

1. 所有物理页必须从 1 连续编号，不允许部分页失败后继续发布结果。
2. 复用 `contract-near-duplicate-v2` 单页图像编码指令，页面逐一 L2 归一化。
3. 普通页面权重 1，物理尾页权重 1.5，加权平均后再次 L2 归一化。
4. 使用现有 Embedding 模型、维度、图像渲染预算及全局并发限制；响应模型、数量、维度和数值不合法时失败。失败或取消清理其余页面请求后关闭客户端。

新查询不复用 ES 中源合同的存量向量，而是基于当前读取的完整页面重新编码。源页面只供 Embedding 使用，不向 Agent Core 注入全部图片。会话驱逐会撤销权限，迟到的检索结果不能重新入池。

---

## 候选范围与排名融合

可选 `result_id` 接受当前会话的图片结果（`contract-search:`）或合同检索最终候选（`contract-query:`）。程序从驻留池解析完整合同 ID，作为 ES kNN 的前置过滤条件；不传或 null 才查询全库。关系结果、其他会话、已驱逐或未知引用报错，绝不回退全库。编码后及发布前再次检查引用，避免等待期间失效的结果被重新发布。

本轮按原始图像相似度选出最多 `top_k` 条，再对幸存候选融合历史排名与本轮排名。采用共享加权 RRF：`w / (k + 历史名次) + (1-w) / (k + 本轮名次)`，历史在幸存集合内重新排名，同分同名次。历史没有相关性排名时直接采用本轮原始分数。配置的召回门槛始终约束本轮原始余弦，不约束 RRF 分数。

新结果保存在图片结果池，页面区分综合 RRF 与本轮原始图像余弦；可继续传给图片检索或统一合同检索。检索回执仅返回引用和数量，分页方式不变。

---

## ES 召回与展示

查询固定使用 `vectors.page_fusion` kNN；预取最多 `top_k × 4`（上限 200）个候选，再按 SQLite 正式目录过滤，去重、按分数降序和合同 ID 稳定排序，最终最多返回 `top_k` 条。ES 超时或分片失败作为失败反馈，不当作成功空结果。

该字段采用 cosine mapping，返回 ES 分数按 `2 × _score - 1` 转为原始余弦相似度。页面展示完整合同 ID、SQLite 中最新名称和摘要及分数；合同已删除或不再 ready 时保留失效槽位，不将旧正文继续展示。

这是有限的近邻召回，不是全库完整集合；可见性过滤后可能不足 `top_k`，不自动扩大到穷尽全库。图像相似不证明重复、条款一致或法律关系成立；需要查看原文核实。

---

## 结果驻留与配置

每个驻留会话持有独立 `ContractSearchResults` LRU，仅保存有序合同 ID、分数、检索类型和翻页位置，不缓存摘要或完整渲染页。搜索返回引用，查看页面使用带签名的 `ToolPageReference`，接入现有 foldable 展示和折叠机制。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PDF_DEDUP_MINIMUM_RECALL_COSINE_SIMILARITY` | 0.60 | 与合同提取查重共用的原始余弦召回下限，范围 -1–1 |
| `COMMUNICATION_CONTRACT_SEARCH_CACHE_MAX_QUERIES` | 10 | 每个会话最多驻留的检索结果集数量 |
| `COMMUNICATION_CONTRACT_SEARCH_PAGE_SIZE` | 5 | 每页展示合同数量 |
| `COMMUNICATION_CONTRACT_RETRIEVAL_RRF_K` | 60 | 共享排名融合平滑常数 |
| `COMMUNICATION_CONTRACT_RETRIEVAL_HISTORY_WEIGHT` | 0.5 | 历史排名权重，范围 0–1 |

池容量、页大小和 RRF 平滑常数必须为正整数，已写入后端和部署环境示例。会话删除、驱逐或服务关闭释放结果集；不跨重启持久化。图片结果可与[统一合同检索](contract-retrieval.md)的最终结果相互限定范围并继承排名。

---

## 验证与边界

离线测试覆盖：真实多页 PDF 渲染及尾页融合、未落盘内存附件、文件缓存驱逐重建与游标不变、来源 ID 和 Schema 描述、ES 查询参数、自身排除、ready 过滤、空结果、失败反馈、分数换算、分页与非法页、签名、LRU、实时元数据、会话驱逐，以及正式 Agent Core 工具调用到下一轮上下文注入。使用模拟 Embedding/ES 响应，不代表已验证真实模型的召回质量。

本次新增离线验证覆盖两种父结果引用、配置化 RRF、原始门槛、失效引用和查询期间驱逐；未执行真实模型与 ES 联调。

